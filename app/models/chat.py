"""Чаты: личные диалоги, группы, сообщения."""

from __future__ import annotations

import enum
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import IdMixin, TimestampMixin
from app.models.base import Base

if TYPE_CHECKING:
    from app.models.file import Attachment
    from app.models.user import User
    from app.models.voice import Transcript


class ChatKind(str, enum.Enum):
    DIRECT = "direct"
    GROUP = "group"


class MessageStatus(str, enum.Enum):
    SENT = "sent"
    DELIVERED = "delivered"
    READ = "read"
    FAILED = "failed"


class Chat(Base, IdMixin, TimestampMixin):
    """Диалог или группа. У личного диалога title не используется."""

    __tablename__ = "chats"

    kind: Mapped[ChatKind] = mapped_column(
        Enum(ChatKind, native_enum=False, length=20), nullable=False, index=True
    )
    title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    avatar_file_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_by_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    is_archived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    members: Mapped[list[ChatMember]] = relationship(
        back_populates="chat", cascade="all, delete-orphan", lazy="selectin"
    )
    messages: Mapped[list[Message]] = relationship(
        back_populates="chat", cascade="all, delete-orphan"
    )

    @property
    def is_direct(self) -> bool:
        return self.kind == ChatKind.DIRECT


class ChatMember(Base, IdMixin, TimestampMixin):
    """Участник чата. Для личного диалога участников ровно двое."""

    __tablename__ = "chat_members"

    chat_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("chats.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    last_read_seq: Mapped[int] = mapped_column(nullable=False, default=0)
    is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_muted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    left_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    chat: Mapped[Chat] = relationship(back_populates="members")
    user: Mapped[User] = relationship(lazy="joined")

    __table_args__ = (
        UniqueConstraint("chat_id", "user_id", name="uq_chat_members_chat_user"),
        Index("ix_chat_members_user", "user_id", "chat_id"),
    )


class Message(Base, IdMixin, TimestampMixin):
    """Сообщение: текст, файл, голосовое или голосовое с расшифровкой."""

    __tablename__ = "messages"

    chat_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("chats.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sender_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False, default="text")

    # Ссылка на единственный вложительный файл (фото, документ или аудио).
    attachment_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("attachments.id", ondelete="SET NULL"), nullable=True
    )
    # Голосовое расшифровано на сервере; дублируется для быстрого чтения.
    transcript_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    voice_duration_sec: Mapped[float | None] = mapped_column(nullable=True)

    # Монотонный номер внутри чата: основа для пагинации и отметок прочтения.
    seq: Mapped[int] = mapped_column(nullable=False, default=0, index=True)
    status: Mapped[MessageStatus] = mapped_column(
        Enum(MessageStatus, native_enum=False, length=20), nullable=False, default=MessageStatus.SENT
    )
    reply_to_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("messages.id", ondelete="SET NULL"), nullable=True
    )
    edited_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    chat: Mapped[Chat] = relationship(back_populates="messages")
    sender: Mapped[User] = relationship(lazy="joined")
    attachment: Mapped[Attachment | None] = relationship(lazy="joined")
    transcript: Mapped[Transcript | None] = relationship(
        back_populates="message", cascade="all, delete-orphan", lazy="selectin", uselist=False
    )

    __table_args__ = (Index("ix_messages_chat_seq", "chat_id", "seq"),)

    @property
    def is_voice(self) -> bool:
        return self.kind == "voice"

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None
