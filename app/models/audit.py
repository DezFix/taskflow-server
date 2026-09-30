"""Журнал действий и системные настройки."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import IdMixin, TimestampMixin, utcnow
from app.models.base import Base


class AuditLog(Base, IdMixin, TimestampMixin):
    """Кто, что и когда сделал. Пишется на значимые операции."""

    __tablename__ = "audit_logs"

    user_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    action: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    entity_type: Mapped[str | None] = mapped_column(String(60), nullable=True)
    entity_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True, default=dict)


class Setting(Base, IdMixin, TimestampMixin):
    """Пара ключ-значение для изменяемых во время работы параметров."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    value: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True, default=dict)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)


class Label(Base, IdMixin, TimestampMixin):
    """Метка для группировки задач."""

    __tablename__ = "labels"

    name: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    color: Mapped[str] = mapped_column(String(20), nullable=False, default="grey")
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_system: Mapped[bool] = mapped_column(nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False, default=utcnow)


class LabelColor:
    """Палитра, которую понимает клиент."""

    GREY = "grey"
    BLUE = "blue"
    GREEN = "green"
    YELLOW = "yellow"
    ORANGE = "orange"
    RED = "red"
    PURPLE = "purple"

    ALL = (GREY, BLUE, GREEN, YELLOW, ORANGE, RED, PURPLE)
