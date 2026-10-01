"""Журнал действий."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import AuditLog, User
from app.security import new_id


def client_ip(request) -> str | None:  # noqa: ANN001
    """IP клиента.

    Заголовкам прокси верим только когда сервер сам за ними стоит.
    Раньше `X-Forwarded-For` принимался от кого угодно, поэтому любой
    клиент мог подписать в журнале произвольный адрес, и расследование
    инцидента по журналу становилось недостоверным.
    """
    peer = request.client.host if request.client else None
    if _is_trusted_proxy(peer):
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            return forwarded.split(",")[0].strip()[:64]
        real_ip = request.headers.get("X-Real-IP")
        if real_ip:
            return real_ip.strip()[:64]
    return peer


def _is_trusted_proxy(peer: str | None) -> bool:
    """Собственный адрес сервера или адрес из списка доверенных."""
    if not peer:
        return False
    trusted = {item.strip() for item in get_settings().trusted_proxies.split(",") if item.strip()}
    return peer in trusted


async def log_action(
    session: AsyncSession,
    *,
    user: User | None = None,
    # Идентификатор без загруженного пользователя: нужен для выхода,
    # где известен только владелец refresh-токена.
    user_id: str | None = None,
    username: str | None = None,
    action: str,
    entity_type: str | None = None,
    entity_id: str | None = None,
    ip_address: str | None = None,
    details: dict[str, Any] | None = None,
) -> AuditLog:
    entry = AuditLog(
        id=new_id(),
        user_id=user.id if user else user_id,
        username=user.username if user else username,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        ip_address=ip_address,
        details=details or {},
    )
    session.add(entry)
    await session.flush()
    return entry


async def list_entries(
    session: AsyncSession,
    *,
    limit: int = 100,
    offset: int = 0,
    action: str | None = None,
    user_id: str | None = None,
    entity_type: str | None = None,
) -> tuple[list[AuditLog], int]:
    filters = []
    if action:
        filters.append(AuditLog.action == action)
    if user_id:
        filters.append(AuditLog.user_id == user_id)
    if entity_type:
        filters.append(AuditLog.entity_type == entity_type)

    total = await session.scalar(
        select(func.count()).select_from(AuditLog).where(*filters)
    )
    rows = (
        await session.scalars(
            select(AuditLog)
            .where(*filters)
            .order_by(AuditLog.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return list(rows), int(total or 0)


def describe(entry: AuditLog) -> str:
    """Человекочитаемая строка для интерфейса журнала."""
    actor = entry.username or "система"
    target = f" {entry.entity_type}:{entry.entity_id}" if entry.entity_type else ""
    return f"{actor} — {entry.action}{target}"


def to_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)
