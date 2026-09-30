"""Интеграционные тесты WebSocket-канала на живом сервере.

Сервер поднимается в фоне на случайном порту, клиент подключается
по-настоящему — так проверяется весь путь: авторизация, handshake,
события и ping/pong.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket

import pytest
import pytest_asyncio
import websockets
from sqlalchemy import select

from app.database import get_sessionmaker
from app.main import app
from app.models import Role, User
from app.security import create_access_token, hash_password, new_id
from app.services.seed import SYSTEM_ROLES

pytestmark = pytest.mark.asyncio


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest_asyncio.fixture
async def live_server():
    """Запускает uvicorn в фоне и выдаёт базовый ws-адрес.

    Движок базы привязан к конкретному циклу событий, поэтому после
    остановки сервера освобождаем его: следующий тест получит новый
    цикл и новые соединения.
    """
    import uvicorn

    from app.database import dispose_engine

    await dispose_engine()

    port = _free_port()
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        lifespan="on",
        ws_ping_interval=None,
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())

    # Ждём, пока сервер действительно начнёт слушать.
    for _ in range(200):
        if server.started:
            break
        await asyncio.sleep(0.05)
    else:  # pragma: no cover - защита от зависания
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        raise RuntimeError("Сервер не запустился")

    try:
        yield f"ws://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=10)
        await dispose_engine()


async def _make_user(username: str, *, superuser: bool = True) -> str:
    async with get_sessionmaker()() as session:
        role = await session.scalar(select(Role).where(Role.key == "Тест WS"))
        if role is None:
            role = Role(
                id=new_id(),
                key="Тест WS",
                title="Тест WS",
                permissions=list(SYSTEM_ROLES["Глава отдела"][2]),
                is_system=False,
            )
            session.add(role)
            await session.flush()

        user = User(
            id=new_id(),
            username=username,
            full_name=username.capitalize(),
            password_hash=hash_password("WsPass123"),
            is_active=True,
            is_superuser=superuser,
            must_change_password=False,
        )
        user.roles.append(role)
        session.add(user)
        await session.commit()
        return user.id


async def _expect_reject(url: str, code: int) -> None:
    """Подключается, читает и ждёт закрытия с нужным кодом.

    Сервер принимает соединение и сразу закрывает его с кодом ошибки:
    так клиент получает конкретную причину, а не безликое «отклонено».
    """
    async with websockets.connect(url) as socket:
        with pytest.raises(websockets.exceptions.ConnectionClosed) as info:
            await asyncio.wait_for(socket.recv(), timeout=10)
    assert info.value.rcvd is not None, "Соединение закрыто без кода"
    assert info.value.rcvd.code == code


async def test_ws_rejects_missing_token(live_server: str) -> None:
    await _expect_reject(f"{live_server}/api/v1/ws", 4401)


async def test_ws_rejects_invalid_token(live_server: str) -> None:
    await _expect_reject(f"{live_server}/api/v1/ws?token=garbage", 4401)


async def test_ws_rejects_refresh_token(live_server: str) -> None:
    """Access-токеном нельзя подменить refresh-токен."""
    from app.security import create_refresh_token

    _, refresh = create_refresh_token(user_id=new_id(), session_id=new_id())
    await _expect_reject(f"{live_server}/api/v1/ws?token={refresh}", 4401)


async def test_ws_handshake_and_ping(live_server: str) -> None:
    user_id = await _make_user("ws_ping")
    token, _ = create_access_token(user_id=user_id)

    async with websockets.connect(f"{live_server}/api/v1/ws?token={token}") as socket:
        ready = json.loads(await socket.recv())
        assert ready["type"] == "ready"
        assert ready["data"]["user_id"] == user_id

        await socket.send(json.dumps({"type": "ping"}))
        response = json.loads(await socket.recv())
        assert response["type"] == "pong"


async def test_ws_rejects_unknown_event_type(live_server: str) -> None:
    user_id = await _make_user("ws_bad_type")
    token, _ = create_access_token(user_id=user_id)

    async with websockets.connect(f"{live_server}/api/v1/ws?token={token}") as socket:
        await socket.recv()  # ready
        await socket.send(json.dumps({"type": "выдумка"}))
        error = json.loads(await socket.recv())
        assert error["type"] == "error"
        assert "выдумка" in error["data"]["message"]


async def test_ws_rejects_invalid_json(live_server: str) -> None:
    user_id = await _make_user("ws_bad_json")
    token, _ = create_access_token(user_id=user_id)

    async with websockets.connect(f"{live_server}/api/v1/ws?token={token}") as socket:
        await socket.recv()  # ready
        await socket.send("не json")
        error = json.loads(await socket.recv())
        assert error["type"] == "error"
        assert "JSON" in error["data"]["message"]


async def test_ws_receives_new_message_event(live_server: str) -> None:
    """Событие message.created приходит подписчику, даже если он в другом потоке."""
    import httpx

    user_id = await _make_user("ws_listener")
    token, _ = create_access_token(user_id=user_id)

    async with httpx.AsyncClient(base_url=f"http://{live_server.replace('ws://', '')}") as http:
        login = await http.post(
            "/api/v1/auth/login",
            json={"identifier": "ws_listener", "password": "WsPass123"},
        )
        assert login.status_code == 200
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

        me = await http.get("/api/v1/auth/me", headers=headers)
        my_id = me.json()["id"]

        users_list = await http.get("/api/v1/users?search=ws_", headers=headers)
        counterpart = next(
            item["id"]
            for item in users_list.json()
            if item["id"] != my_id and item["is_active"]
        )

        chat = await http.post(
            "/api/v1/chats/direct", json={"user_id": counterpart}, headers=headers
        )
        assert chat.status_code == 200, chat.text

        async with websockets.connect(f"{live_server}/api/v1/ws?token={token}") as socket:
            await socket.recv()  # ready

            sent = await http.post(
                f"/api/v1/chats/{chat.json()['id']}/messages",
                json={"body": "Сообщение через реалтайм"},
                headers=headers,
            )
            assert sent.status_code == 201, sent.text

            event = json.loads(await asyncio.wait_for(socket.recv(), timeout=10))
            assert event["type"] == "message.created"
            assert event["data"]["body"] == "Сообщение через реалтайм"
            assert event["data"]["chat_id"] == chat.json()["id"]


async def test_ws_read_event_notifies_sender(live_server: str) -> None:
    """Собеседник отмечает сообщение прочитанным — автор получает уведомление.

    Свои сообщения отметить прочитанными нельзя, поэтому проверяем
    именно сценарий «прочитал собеседник».
    """
    import httpx

    author_id = await _make_user("ws_author")
    reader_id = await _make_user("ws_reader2")
    author_token, _ = create_access_token(user_id=author_id)
    reader_token, _ = create_access_token(user_id=reader_id)

    base = f"http://{live_server.replace('ws://', '')}"
    async with httpx.AsyncClient(base_url=base) as http:
        author_login = await http.post(
            "/api/v1/auth/login",
            json={"identifier": "ws_author", "password": "WsPass123"},
        )
        author_headers = {
            "Authorization": f"Bearer {author_login.json()['access_token']}"
        }
        reader_login = await http.post(
            "/api/v1/auth/login",
            json={"identifier": "ws_reader2", "password": "WsPass123"},
        )
        reader_headers = {
            "Authorization": f"Bearer {reader_login.json()['access_token']}"
        }

        chat = await http.post(
            "/api/v1/chats/direct",
            json={"user_id": reader_id},
            headers=author_headers,
        )
        assert chat.status_code == 200, chat.text
        chat_id = chat.json()["id"]

        sent = await http.post(
            f"/api/v1/chats/{chat_id}/messages",
            json={"body": "Прочитай, пожалуйста"},
            headers=author_headers,
        )
        assert sent.status_code == 201
        message_id = sent.json()["id"]

        unread = await http.get("/api/v1/chats/unread/total", headers=reader_headers)
        assert unread.json()["unread"] == 1

        # Сокет автора открыт и ждёт события о прочтении.
        async with websockets.connect(
            f"{live_server}/api/v1/ws?token={author_token}"
        ) as author_socket:
            assert json.loads(await author_socket.recv())["type"] == "ready"

            # Собеседник подтверждает прочтение через сокет.
            async with websockets.connect(
                f"{live_server}/api/v1/ws?token={reader_token}"
            ) as reader_socket:
                await reader_socket.recv()
                await reader_socket.send(
                    json.dumps(
                        {
                            "type": "read",
                            "data": {"chat_id": chat_id, "up_to_seq": sent.json()["seq"]},
                        }
                    )
                )

                event = json.loads(
                    await asyncio.wait_for(author_socket.recv(), timeout=10)
                )

        assert event["type"] == "message.read"
        assert event["data"]["chat_id"] == chat_id
        assert event["data"]["user_id"] == reader_id
        assert message_id in event["data"]["message_ids"]

        await asyncio.sleep(0.3)
        after = await http.get("/api/v1/chats/unread/total", headers=reader_headers)
        assert after.json()["unread"] == 0

        detail = await http.get(f"/api/v1/chats/{chat_id}", headers=reader_headers)
        members = detail.json()["members_detail"]
        mine = next(m for m in members if m["user"]["id"] == reader_id)
        assert mine["last_read_seq"] >= sent.json()["seq"]


async def test_ws_read_cannot_exceed_last_sequence(live_server: str) -> None:
    """Клиент не может отметить прочитанным то, чего ещё не существует."""
    import httpx

    await _make_user("ws_bounded")
    base = f"http://{live_server.replace('ws://', '')}"
    async with httpx.AsyncClient(base_url=base) as http:
        login = await http.post(
            "/api/v1/auth/login",
            json={"identifier": "ws_bounded", "password": "WsPass123"},
        )
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        me = await http.get("/api/v1/auth/me", headers=headers)
        my_id = me.json()["id"]

        users_list = await http.get("/api/v1/users?search=ws_", headers=headers)
        counterpart = next(
            item["id"]
            for item in users_list.json()
            if item["id"] != my_id and item["is_active"]
        )
        chat = await http.post(
            "/api/v1/chats/direct", json={"user_id": counterpart}, headers=headers
        )
        chat_id = chat.json()["id"]

        token, _ = create_access_token(user_id=my_id)
        async with websockets.connect(f"{live_server}/api/v1/ws?token={token}") as socket:
            await socket.recv()
            # Просим отметить всё до 9999, хотя сообщений ещё нет.
            await socket.send(
                json.dumps({"type": "read", "data": {"chat_id": chat_id, "up_to_seq": 9999}})
            )
            await asyncio.sleep(0.5)

        detail = await http.get(f"/api/v1/chats/{chat_id}", headers=headers)
        members = detail.json()["members_detail"]
        mine = next(m for m in members if m["user"]["id"] == my_id)
        # Отметка не должна уехать за пределы реальных сообщений.
        assert mine["last_read_seq"] == 0


async def test_ws_logout_all_sends_revocation(live_server: str) -> None:
    """Выход со всех устройств разрывает канал событием session.revoked."""
    import httpx

    user_id = await _make_user("ws_revoke")
    token, _ = create_access_token(user_id=user_id)

    base = f"http://{live_server.replace('ws://', '')}"
    async with httpx.AsyncClient(base_url=base) as http:
        login = await http.post(
            "/api/v1/auth/login",
            json={"identifier": "ws_revoke", "password": "WsPass123"},
        )
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

        async with websockets.connect(f"{live_server}/api/v1/ws?token={token}") as socket:
            assert json.loads(await socket.recv())["type"] == "ready"

            response = await http.post("/api/v1/auth/logout-all", headers=headers)
            assert response.status_code == 200

            event = json.loads(await asyncio.wait_for(socket.recv(), timeout=10))
            assert event["type"] == "session.revoked"
            assert event["data"]["reason"] == "logout_all"
