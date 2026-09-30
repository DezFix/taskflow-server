"""Тесты чата: диалоги, группы, сообщения, прочтение, файлы, голосовые."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from tests.conftest import TEST_PNG

pytestmark = pytest.mark.asyncio


def auth(users, role: str) -> dict[str, str]:  # noqa: ANN001
    return {"Authorization": f"Bearer {users.tokens[role]}"}


async def _open_direct(client: AsyncClient, users, actor: str, other: str) -> str:
    response = await client.post(
        "/api/v1/chats/direct",
        json={"user_id": users.ids[other]},
        headers=auth(users, actor),
    )
    assert response.status_code == 200, response.text
    return response.json()["id"]


async def test_direct_chat_is_created_once(client: AsyncClient, users) -> None:
    first = await _open_direct(client, users, "staff", "staff2")
    second = await _open_direct(client, users, "staff", "staff2")
    assert first == second


async def test_direct_chat_title_is_counterpart_name(client: AsyncClient, users) -> None:
    chat_id = await _open_direct(client, users, "staff", "staff2")
    detail = await client.get(f"/api/v1/chats/{chat_id}", headers=auth(users, "staff"))
    assert detail.status_code == 200
    assert detail.json()["title"] == "Staff2"


async def test_cannot_chat_with_yourself(client: AsyncClient, users) -> None:
    response = await client.post(
        "/api/v1/chats/direct",
        json={"user_id": users.ids["staff"]},
        headers=auth(users, "staff"),
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "self_chat"


async def test_cannot_open_chat_with_inactive_user(client: AsyncClient, users) -> None:
    await client.post(
        f"/api/v1/users/{users.ids['staff2']}/deactivate", headers=auth(users, "head")
    )
    response = await client.post(
        "/api/v1/chats/direct",
        json={"user_id": users.ids["staff2"]},
        headers=auth(users, "staff"),
    )
    assert response.status_code == 404


async def test_third_party_cannot_read_chat(client: AsyncClient, users) -> None:
    chat_id = await _open_direct(client, users, "staff", "staff2")
    response = await client.get(f"/api/v1/chats/{chat_id}", headers=auth(users, "head"))
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "chat_access_denied"


async def test_send_and_read_message(client: AsyncClient, users) -> None:
    chat_id = await _open_direct(client, users, "staff", "staff2")
    sent = await client.post(
        f"/api/v1/chats/{chat_id}/messages",
        json={"body": "Привет, нужна помощь с принтером"},
        headers=auth(users, "staff"),
    )
    assert sent.status_code == 201
    assert sent.json()["body"] == "Привет, нужна помощь с принтером"
    assert sent.json()["seq"] == 1
    assert sent.json()["sender"]["id"] == users.ids["staff"]

    history = await client.get(
        f"/api/v1/chats/{chat_id}/messages", headers=auth(users, "staff2")
    )
    assert len(history.json()["items"]) == 1


async def test_empty_message_rejected(client: AsyncClient, users) -> None:
    chat_id = await _open_direct(client, users, "staff", "staff2")
    response = await client.post(
        f"/api/v1/chats/{chat_id}/messages",
        json={"body": "   "},
        headers=auth(users, "staff"),
    )
    assert response.status_code == 422


async def test_unread_counter(client: AsyncClient, users) -> None:
    chat_id = await _open_direct(client, users, "staff", "staff2")
    await client.post(
        f"/api/v1/chats/{chat_id}/messages",
        json={"body": "Первое"},
        headers=auth(users, "staff"),
    )
    await client.post(
        f"/api/v1/chats/{chat_id}/messages",
        json={"body": "Второе"},
        headers=auth(users, "staff"),
    )

    unread = await client.get("/api/v1/chats/unread/total", headers=auth(users, "staff2"))
    assert unread.json()["unread"] == 2

    read = await client.post(
        f"/api/v1/chats/{chat_id}/read", json={"seq": 2}, headers=auth(users, "staff2")
    )
    assert read.status_code == 200

    after = await client.get("/api/v1/chats/unread/total", headers=auth(users, "staff2"))
    assert after.json()["unread"] == 0


async def test_read_does_not_affect_sender(client: AsyncClient, users) -> None:
    """Свои сообщения отметить прочитанными нельзя — счётчик не должен уменьшаться."""
    chat_id = await _open_direct(client, users, "staff", "staff2")
    await client.post(
        f"/api/v1/chats/{chat_id}/messages",
        json={"body": "Своё"},
        headers=auth(users, "staff"),
    )
    await client.post(
        f"/api/v1/chats/{chat_id}/read", json={"seq": 10}, headers=auth(users, "staff")
    )
    unread = await client.get("/api/v1/chats/unread/total", headers=auth(users, "staff"))
    assert unread.json()["unread"] == 0


async def test_chat_list_shows_last_message(client: AsyncClient, users) -> None:
    chat_id = await _open_direct(client, users, "staff", "staff2")
    await client.post(
        f"/api/v1/chats/{chat_id}/messages",
        json={"body": "Последнее"},
        headers=auth(users, "staff"),
    )
    chats = await client.get("/api/v1/chats", headers=auth(users, "staff"))
    assert chats.status_code == 200
    chat = next(c for c in chats.json() if c["id"] == chat_id)
    assert chat["last_message"]["body"] == "Последнее"
    assert chat["unread_count"] == 0


async def test_group_chat_lifecycle(client: AsyncClient, users) -> None:
    created = await client.post(
        "/api/v1/chats/groups",
        json={
            "title": "Отдел ИТ",
            "member_ids": [users.ids["staff"], users.ids["staff2"]],
        },
        headers=auth(users, "head"),
    )
    assert created.status_code == 201
    chat_id = created.json()["id"]
    assert created.json()["kind"] == "group"
    assert len(created.json()["members"]) == 3

    await client.post(
        f"/api/v1/chats/{chat_id}/messages",
        json={"body": "Всем привет"},
        headers=auth(users, "head"),
    )

    # Оба сотрудника видят сообщение.
    for actor in ("staff", "staff2"):
        history = await client.get(
            f"/api/v1/chats/{chat_id}/messages", headers=auth(users, actor)
        )
        assert len(history.json()["items"]) == 1

    removed = await client.patch(
        f"/api/v1/chats/{chat_id}",
        json={"remove_member_ids": [users.ids["staff2"]]},
        headers=auth(users, "head"),
    )
    assert removed.status_code == 200

    denied = await client.get(f"/api/v1/chats/{chat_id}", headers=auth(users, "staff2"))
    assert denied.status_code == 403


async def test_group_owner_cannot_be_removed(client: AsyncClient, users) -> None:
    created = await client.post(
        "/api/v1/chats/groups",
        json={"title": "Группа", "member_ids": [users.ids["staff"]]},
        headers=auth(users, "head"),
    )
    chat_id = created.json()["id"]
    response = await client.patch(
        f"/api/v1/chats/{chat_id}",
        json={"remove_member_ids": [users.ids["head"]]},
        headers=auth(users, "head"),
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "cannot_remove_owner"


async def test_direct_chat_is_immutable(client: AsyncClient, users) -> None:
    chat_id = await _open_direct(client, users, "staff", "staff2")
    response = await client.patch(
        f"/api/v1/chats/{chat_id}", json={"title": "Новое"}, headers=auth(users, "staff")
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "direct_chat_immutable"


async def test_staff_cannot_create_group(client: AsyncClient, users) -> None:
    response = await client.post(
        "/api/v1/chats/groups",
        json={"title": "Своя группа", "member_ids": [users.ids["staff2"]]},
        headers=auth(users, "staff"),
    )
    assert response.status_code == 403


async def test_edit_own_message(client: AsyncClient, users) -> None:
    chat_id = await _open_direct(client, users, "staff", "staff2")
    sent = await client.post(
        f"/api/v1/chats/{chat_id}/messages",
        json={"body": "Опечатка"},
        headers=auth(users, "staff"),
    )
    edited = await client.patch(
        f"/api/v1/chats/messages/{sent.json()['id']}",
        json={"body": "Исправлено"},
        headers=auth(users, "staff"),
    )
    assert edited.status_code == 200
    assert edited.json()["body"] == "Исправлено"
    assert edited.json()["edited_at"] is not None


async def test_cannot_edit_foreign_message(client: AsyncClient, users) -> None:
    chat_id = await _open_direct(client, users, "staff", "staff2")
    sent = await client.post(
        f"/api/v1/chats/{chat_id}/messages",
        json={"body": "Чужое"},
        headers=auth(users, "staff"),
    )
    response = await client.patch(
        f"/api/v1/chats/messages/{sent.json()['id']}",
        json={"body": "Вмешательство"},
        headers=auth(users, "staff2"),
    )
    assert response.status_code == 403


async def test_delete_message_clears_body(client: AsyncClient, users) -> None:
    chat_id = await _open_direct(client, users, "staff", "staff2")
    sent = await client.post(
        f"/api/v1/chats/{chat_id}/messages",
        json={"body": "Удалить меня"},
        headers=auth(users, "staff"),
    )
    deleted = await client.delete(
        f"/api/v1/chats/messages/{sent.json()['id']}", headers=auth(users, "staff")
    )
    assert deleted.status_code == 200

    history = await client.get(f"/api/v1/chats/{chat_id}/messages", headers=auth(users, "staff"))
    assert history.json()["items"][0]["deleted_at"] is not None


async def test_send_photo_to_chat(client: AsyncClient, users) -> None:
    chat_id = await _open_direct(client, users, "staff", "staff2")
    response = await client.post(
        f"/api/v1/chats/{chat_id}/files",
        files={"file": ("photo.png", TEST_PNG, "image/png")},
        data={"body": "Вот скриншот"},
        headers=auth(users, "staff"),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["kind"] == "image"
    assert body["attachment"]["name"] == "photo.png"
    assert body["attachment"]["size_bytes"] == len(TEST_PNG)
    assert body["body"] == "Вот скриншот"


async def test_voice_message_gets_transcript_record(client: AsyncClient, users) -> None:
    """Голосовое принимается сразу, а распознавание ставится в очередь."""
    chat_id = await _open_direct(client, users, "staff", "staff2")
    # Минимальный корректный WAV: 44 байта заголовка + немного тишины.
    wav = (
        b"RIFF$\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00"
        b"\x40\x1f\x00\x00\x80>\x00\x00\x02\x00\x10\x00data\x00\x00\x00\x00"
    )
    response = await client.post(
        f"/api/v1/chats/{chat_id}/voice",
        files={"file": ("voice.wav", wav, "audio/wav")},
        data={"duration_sec": "2.5"},
        headers=auth(users, "staff"),
    )
    assert response.status_code == 201, response.text
    message = response.json()
    assert message["kind"] == "voice"
    assert message["voice_duration_sec"] == 2.5
    assert message["transcript"] is not None
    # Распознавание в тестах выключено, поэтому статус «пропущено».
    assert message["transcript"]["status"] == "skipped"


async def test_reply_to_message(client: AsyncClient, users) -> None:
    chat_id = await _open_direct(client, users, "staff", "staff2")
    first = await client.post(
        f"/api/v1/chats/{chat_id}/messages",
        json={"body": "Вопрос"},
        headers=auth(users, "staff"),
    )
    reply = await client.post(
        f"/api/v1/chats/{chat_id}/messages",
        json={"body": "Ответ", "reply_to_id": first.json()["id"]},
        headers=auth(users, "staff2"),
    )
    assert reply.status_code == 201
    assert reply.json()["reply_to_id"] == first.json()["id"]


async def test_reply_to_foreign_chat_rejected(client: AsyncClient, users) -> None:
    chat_a = await _open_direct(client, users, "staff", "staff2")
    chat_b = await _open_direct(client, users, "head", "staff")
    message = await client.post(
        f"/api/v1/chats/{chat_a}/messages",
        json={"body": "Тут"},
        headers=auth(users, "staff"),
    )
    response = await client.post(
        f"/api/v1/chats/{chat_b}/messages",
        json={"body": "Ответ туда", "reply_to_id": message.json()["id"]},
        headers=auth(users, "head"),
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "reply_invalid"


async def test_voice_capabilities_endpoint(client: AsyncClient, users) -> None:
    response = await client.get("/api/v1/chats/voice/capabilities", headers=auth(users, "staff"))
    assert response.status_code == 200
    body = response.json()
    assert "m4a" in body["formats"]
    assert body["enabled"] is False


async def test_message_pagination(client: AsyncClient, users) -> None:
    chat_id = await _open_direct(client, users, "staff", "staff2")
    for index in range(5):
        await client.post(
            f"/api/v1/chats/{chat_id}/messages",
            json={"body": f"Сообщение {index}"},
            headers=auth(users, "staff"),
        )

    page = await client.get(
        f"/api/v1/chats/{chat_id}/messages?limit=2", headers=auth(users, "staff")
    )
    body = page.json()
    assert len(body["items"]) == 2
    assert body["has_more"] is True
    # Новые сообщения первыми.
    assert body["items"][0]["body"] == "Сообщение 4"
