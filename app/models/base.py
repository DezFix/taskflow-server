"""Общие базовые классы моделей."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base, IdMixin, TimestampMixin, utcnow


class UUIDMixin(IdMixin):
    """Идентификатор — строка UUID, одинаково работает в любой БД."""

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))


class SoftDeleteMixin:
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)


class JSONMixin:
    """JSON-колонка. Переносимо между SQLite, PostgreSQL и MySQL."""

    def json_field(self) -> Mapped[dict[str, Any] | None]:
        return mapped_column(JSON, nullable=True, default=dict)


class CreatedAtMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


__all__ = [
    "Base",
    "IdMixin",
    "TimestampMixin",
    "UUIDMixin",
    "SoftDeleteMixin",
    "JSONMixin",
    "CreatedAtMixin",
]
