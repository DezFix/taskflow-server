"""WebSocket-канал реалтайма: чат, задачи, статусы расшифровки."""

from __future__ import annotations

import asyncio
import contextlib

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.database import get_sessionmaker
from app.deps import user_from_websocket
from app.errors import AppError
from app.logging_setup import get_logger
from app.realtime import HEARTBEAT_SECONDS, hub
from app.security import decode_token

logger = get_logger("ws")

router = APIRouter()


async def _reject(websocket: WebSocket, code: int, reason: str) -> None:
    """Закрывает сокет с кодом ошибки.

    Закрывать нужно после accept(): иначе Starlette отвечает HTTP 403,
    и клиент не видит код — только «соединение отклонено».
    """
    await websocket.accept()
    await websocket.close(code=code, reason=reason)


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    """Канал реалтайма.

    Токен передаётся в query-строке, потому что браузерный WebSocket
    не умеет задавать заголовки. Протокол:
      клиент → {"type": "ping"} / {"type": "read", "data": {...}}
      сервер  → события из realtime.hub и {"type": "pong"}
    """
    token = websocket.query_params.get("token")
    if not token:
        await _reject(websocket, 4401, "Требуется токен")
        return

    payload = decode_token(token, "access")
    if payload is None:
        await _reject(websocket, 4401, "Токен недействителен")
        return

    user_id = str(payload.get("sub", ""))
    try:
        async with get_sessionmaker()() as session:
            # Проверка нужна, чтобы отключённый сотрудник не держал
            # открытый канал. Идентификатор берём из загруженной
            # записи, а не из токена: так они не разойдутся.
            user = await user_from_websocket(websocket, session)
            user_id = user.id
    except AppError as exc:
        await _reject(websocket, 4403, exc.message)
        return
    except Exception:
        logger.exception("ws_auth_failed")
        await _reject(websocket, 4400, "Ошибка авторизации")
        return

    await hub.connect(user_id, websocket)
    await websocket.send_json({"type": "ready", "data": {"user_id": user_id}})

    heartbeat = asyncio.create_task(_heartbeat(websocket))
    try:
        while True:
            raw = await websocket.receive_text()
            await _handle_client_message(websocket, user_id, raw)
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("ws_loop_failed", user_id=user_id)
    finally:
        heartbeat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat
        await hub.disconnect(user_id, websocket)


async def _heartbeat(websocket: WebSocket) -> None:
    """Пинг каждые 25 секунд: иначе прокси и NAT рвут простаивающее соединение."""
    try:
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            await websocket.send_json({"type": "ping", "data": {}})
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    except Exception:
        logger.debug("heartbeat_failed")


async def _handle_client_message(websocket: WebSocket, user_id: str, raw: str) -> None:
    import json

    try:
        message = json.loads(raw)
    except json.JSONDecodeError:
        await websocket.send_json(
            {"type": "error", "data": {"message": "Некорректный JSON"}}
        )
        return

    kind = message.get("type")

    if kind == "ping":
        await websocket.send_json({"type": "pong", "data": {}})
        return

    if kind == "read":
        data = message.get("data") or {}
        await _mark_read(websocket, user_id, str(data.get("chat_id", "")), data.get("up_to_seq"))
        return

    if kind == "typing":
        # Событие о наборе текста просто пересылаем участникам чата.
        data = message.get("data") or {}
        chat_id = str(data.get("chat_id", ""))
        if chat_id:
            from app.services.chat import member_ids

            async with get_sessionmaker()() as session:
                members = await member_ids(session, chat_id)
            members.discard(user_id)
            await hub.send_to_users(
                members,
                "chat.typing",
                {
                    "chat_id": chat_id,
                    "user_id": user_id,
                    "is_typing": bool(data.get("is_typing", True)),
                },
            )
        return

    await websocket.send_json(
        {"type": "error", "data": {"message": f"Неизвестный тип: {kind}"}}
    )


async def _mark_read(
    websocket: WebSocket, user_id: str, chat_id: str, up_to_seq: object
) -> None:
    """Отметка прочтения прямо из сокета — не заставляем клиент дёргать REST."""
    from sqlalchemy import select

    from app.models import ChatMember, Message
    from app.realtime import EVENT_MESSAGE_READ
    from app.services.chat import load_chat_for_member

    if not chat_id:
        return

    async with get_sessionmaker()() as session:
        try:
            chat = await load_chat_for_member(session, chat_id, user_id)
        except AppError:
            return

        member = await session.scalar(
            select(ChatMember).where(
                ChatMember.chat_id == chat_id, ChatMember.user_id == user_id
            )
        )
        if member is None:
            return

        last_seq = await session.scalar(
            select(Message.seq)
            .where(Message.chat_id == chat_id)
            .order_by(Message.seq.desc())
            .limit(1)
        )
        # Клиент может прислать произвольное значение — ограничиваем
        # номером последнего сообщения, чтобы отметка не уехала вперёд.
        if isinstance(up_to_seq, (int, float, str)) and str(up_to_seq).isdigit():
            target = min(int(up_to_seq), last_seq or 0)
        else:
            target = last_seq or 0

        if target <= member.last_read_seq:
            return

        unread = (
            await session.scalars(
                select(Message).where(
                    Message.chat_id == chat_id,
                    Message.seq > member.last_read_seq,
                    Message.seq <= target,
                    Message.sender_id != user_id,
                    Message.deleted_at.is_(None),
                )
            )
        ).all()

        for message in unread:
            message.status = "read"
        member.last_read_seq = target
        await session.commit()

        if unread:
            members = {m.user_id for m in chat.members if m.left_at is None}
            await hub.send_to_users(
                members,
                EVENT_MESSAGE_READ,
                {
                    "chat_id": chat_id,
                    "user_id": user_id,
                    "message_ids": [m.id for m in unread],
                    "up_to_seq": target,
                },
            )
