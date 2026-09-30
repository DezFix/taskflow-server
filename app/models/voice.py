"""Расшифровка голосовых сообщений (faster-whisper на сервере)."""

from __future__ import annotations

import enum
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import IdMixin, TimestampMixin
from app.models.base import Base

if TYPE_CHECKING:
    from app.models.chat import Message


class TranscriptStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


class Transcript(Base, IdMixin, TimestampMixin):
    """Результат распознавания одного голосового сообщения."""

    __tablename__ = "transcripts"

    message_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("messages.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    status: Mapped[TranscriptStatus] = mapped_column(
        Enum(TranscriptStatus, native_enum=False, length=20),
        nullable=False,
        default=TranscriptStatus.PENDING,
        index=True,
    )
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    language: Mapped[str | None] = mapped_column(String(10), nullable=True)
    model_name: Mapped[str | None] = mapped_column(String(40), nullable=True)
    compute_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    duration_sec: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Уверенность модели в среднем по сегментам, 0..1
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    is_manual: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    message: Mapped[Message] = relationship(back_populates="transcript")
