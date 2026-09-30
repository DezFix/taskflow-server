"""Хаб WebSocket-соединений и рассылка событий."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections import defaultdict
from typing import Any

from fastapi import WebSocket

from app.logging_setup import get_logger

logger = get_logger("realtime")

HEARTBEAT_SECONDS = 25


class RealtimeHub:
    """Держит соединения пользователей и рассылает им события.

    Ключ — id пользователя, значение — набор его сокетов: у одного человека
    может быть открыто приложение на телефоне и вкладка в браузере.
    """

    def __init__(self) -> None:
        self._connections: dict[str, set[WebSocket]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def connect(self, user_id: str, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self._connections[user_id].add(websocket)
        logger.info("ws_connected", user_id=user_id, total=len(self._connections[user_id]))

    async def disconnect(self, user_id: str, websocket: WebSocket) -> None:
        async with self._lock:
            sockets = self._connections.get(user_id)
            if sockets:
                sockets.discard(websocket)
                if not sockets:
                    self._connections.pop(user_id, None)
        logger.info("ws_disconnected", user_id=user_id)

    def is_online(self, user_id: str) -> bool:
        return bool(self._connections.get(user_id))

    def online_users(self) -> set[str]:
        return set(self._connections.keys())

    async def send_to_user(self, user_id: str, event_type: str, data: Any) -> None:
        await self._broadcast({user_id}, event_type, data)

    async def send_to_users(self, user_ids: set[str] | list[str], event_type: str, data: Any) -> None:
        await self._broadcast(set(user_ids), event_type, data)

    async def broadcast_all(self, event_type: str, data: Any) -> None:
        async with self._lock:
            targets = set(self._connections.keys())
        await self._broadcast(targets, event_type, data)

    async def _broadcast(self, user_ids: set[str], event_type: str, data: Any) -> None:
        if not user_ids:
            return
        payload = json.dumps({"type": event_type, "data": data}, ensure_ascii=False, default=str)

        async with self._lock:
            sockets: list[WebSocket] = []
            for user_id in user_ids:
                sockets.extend(self._connections.get(user_id, ()))

        dead: list[tuple[str, WebSocket]] = []
        for socket in sockets:
            try:
                await socket.send_text(payload)
            except Exception:
                # Сокет мог умереть между проверкой и отправкой — чистим молча.
                user_id = next(
                    (uid for uid, group in self._connections.items() if socket in group), None
                )
                if user_id:
                    dead.append((user_id, socket))

        if dead:
            for user_id, socket in dead:
                await self.disconnect(user_id, socket)


hub = RealtimeHub()

# Названия событий — единый контракт с клиентом.
EVENT_MESSAGE_CREATED = "message.created"
EVENT_MESSAGE_UPDATED = "message.updated"
EVENT_MESSAGE_DELETED = "message.deleted"
EVENT_MESSAGE_READ = "message.read"
EVENT_CHAT_UPDATED = "chat.updated"
EVENT_TASK_CREATED = "task.created"
EVENT_TASK_UPDATED = "task.updated"
EVENT_TASK_DELETED = "task.deleted"
EVENT_TRANSCRIPT_READY = "transcript.ready"
EVENT_USER_UPDATED = "user.updated"
EVENT_SESSION_REVOKED = "session.revoked"
EVENT_PING = "ping"


async def ping_all() -> None:
    await hub.broadcast_all(EVENT_PING, {"ts": None})


@contextlib.asynccontextmanager
async def safe_send(websocket: WebSocket, message: dict[str, Any]):  # noqa: ANN201
    try:
        await websocket.send_json(message)
    except Exception:
        logger.debug("ws_send_failed")
