"""Вложения: фотоотчёты, документы, аудио голосовых."""

from __future__ import annotations

import enum
from typing import TYPE_CHECKING

from sqlalchemy import (
    JSON,
    Boolean,
    Enum,
    ForeignKey,
    Integer,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import IdMixin, TimestampMixin
from app.models.base import Base

if TYPE_CHECKING:
    from app.models.user import User


class AttachmentKind(str, enum.Enum):
    IMAGE = "image"
    DOCUMENT = "document"
    AUDIO = "audio"
    OTHER = "other"


class Attachment(Base, IdMixin, TimestampMixin):
    """Файл на диске. Имя на диске случайное, оригинальное хранится отдельно."""

    __tablename__ = "attachments"

    kind: Mapped[AttachmentKind] = mapped_column(
        Enum(AttachmentKind, native_enum=False, length=20), nullable=False, index=True
    )
    original_name: Mapped[str] = mapped_column(String(255), nullable=False)
    stored_name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    mime_type: Mapped[str] = mapped_column(String(150), nullable=False, default="application/octet-stream")
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    checksum: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    # Превью для быстрого отображения в списках.
    preview_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    uploader_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Ссылка на задачу, если файл загружен как отчёт по ней.
    task_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=True, index=True
    )

    # Произвольные метаданные: длительность аудио, размеры изображения и т.п.
    meta: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=dict)
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    uploader: Mapped[User] = relationship(lazy="joined")

    @property
    def extension(self) -> str:
        if "." not in self.original_name:
            return ""
        return "." + self.original_name.rsplit(".", 1)[1].lower()
