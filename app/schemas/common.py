"""Общие части схем: пагинация, метаданные, базовые ответы."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_serializer

T = TypeVar("T")


class Schema(BaseModel):
    """Базовая схема: запрещаем лишние поля и умеем читать из ORM."""

    model_config = ConfigDict(from_attributes=True, populate_by_name=True, extra="ignore")


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


def to_utc(value: datetime | None) -> datetime | None:
    """Naive-время из БД трактуем как UTC и добавляем таймзону для клиента."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class UTCTimestamps(Schema):
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @field_serializer("created_at", "updated_at")
    def _ser(self, value: datetime | None) -> str | None:
        dt = to_utc(value)
        return dt.isoformat().replace("+00:00", "Z") if dt else None


class Page(Schema, Generic[T]):
    """Курсорная пагинация: курсор надёжнее offset при вставках."""

    items: list[T]
    next_cursor: str | None = None
    has_more: bool = False
    total: int | None = None


class SimplePage(Schema, Generic[T]):
    items: list[T]
    total: int
    page: int = 1
    per_page: int = 50


class Message(Schema):
    ok: bool = True
    detail: str | None = None


class CountByKey(Schema):
    key: str
    title: str
    count: int


class ErrorBody(Schema):
    code: str
    message: str
    details: dict | list | None = None


class ErrorResponse(Schema):
    error: ErrorBody


class PaginationParams(Schema):
    limit: int = Field(50, ge=1, le=200)
    cursor: str | None = None
