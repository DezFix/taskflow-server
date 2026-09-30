"""Схемы чата, сообщений и файлов."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, field_serializer, field_validator

from app.schemas.auth import UserBrief
from app.schemas.common import Schema, to_utc
from app.schemas.task import AttachmentBrief


class TranscriptOut(Schema):
    id: str
    status: str
    text: str | None = None
    language: str | None = None
    model_name: str | None = None
    duration_sec: float | None = None
    confidence: float | None = None
    error: str | None = None
    is_manual: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @field_serializer("created_at", "updated_at")
    def _ser(self, value: datetime | None) -> str | None:
        dt = to_utc(value)
        return dt.isoformat().replace("+00:00", "Z") if dt else None


class MessageOut(Schema):
    id: str
    chat_id: str
    seq: int
    kind: str
    body: str | None = None
    sender: UserBrief
    attachment: AttachmentBrief | None = None
    transcript: TranscriptOut | None = None
    transcript_text: str | None = None
    voice_duration_sec: float | None = None
    status: str
    reply_to_id: str | None = None
    edited_at: datetime | None = None
    deleted_at: datetime | None = None
    created_at: datetime

    @field_serializer("created_at", "edited_at", "deleted_at")
    def _ser(self, value: datetime | None) -> str | None:
        dt = to_utc(value)
        return dt.isoformat().replace("+00:00", "Z") if dt else None


class ChatOut(Schema):
    id: str
    kind: str
    title: str | None = None
    avatar_url: str | None = None
    members: list[UserBrief] = []
    last_message: MessageOut | None = None
    last_message_at: datetime | None = None
    unread_count: int = 0
    is_admin: bool = False
    is_muted: bool = False
    created_at: datetime

    @field_serializer("last_message_at", "created_at")
    def _ser(self, value: datetime | None) -> str | None:
        dt = to_utc(value)
        return dt.isoformat().replace("+00:00", "Z") if dt else None


class DirectChatCreate(Schema):
    user_id: str


class GroupChatCreate(Schema):
    title: str = Field(min_length=2, max_length=200)
    member_ids: list[str] = Field(min_length=1, max_length=200)
    avatar_file_id: str | None = None


class GroupUpdate(Schema):
    title: str | None = Field(default=None, min_length=2, max_length=200)
    member_ids: list[str] | None = None
    add_member_ids: list[str] | None = None
    remove_member_ids: list[str] | None = None


class ChatMemberOut(Schema):
    user: UserBrief
    is_admin: bool = False
    last_read_seq: int = 0
    joined_at: datetime | None = None

    @field_serializer("joined_at")
    def _ser(self, value: datetime | None) -> str | None:
        dt = to_utc(value)
        return dt.isoformat().replace("+00:00", "Z") if dt else None


class ChatDetailOut(ChatOut):
    members_detail: list[ChatMemberOut] = []
    created_by_id: str | None = None


class TextMessageCreate(Schema):
    body: str = Field(min_length=1, max_length=8000)
    reply_to_id: str | None = None

    @field_validator("body")
    @classmethod
    def _trim(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Сообщение не может быть пустым")
        return value


class MarkReadRequest(Schema):
    seq: int = Field(ge=0, description="Прочитать всё до этой последовательности")


class MessageEdit(Schema):
    body: str = Field(min_length=1, max_length=8000)


# --- Файлы ---


class FileOut(Schema):
    id: str
    kind: str
    name: str
    mime_type: str
    size_bytes: int
    url: str
    preview_url: str | None = None
    width: int | None = None
    height: int | None = None
    duration_sec: float | None = None
    checksum: str | None = None
    task_id: str | None = None
    uploader_id: str
    created_at: datetime

    @field_serializer("created_at")
    def _ser(self, value: datetime) -> str:
        dt = to_utc(value)
        return dt.isoformat().replace("+00:00", "Z") if dt else ""


# --- Голос ---


class VoiceSettingsOut(Schema):
    enabled: bool
    model: str
    language: str
    compute_type: str
    available_models: list[str]
    models_dir: str


class VoiceSettingsUpdate(Schema):
    enabled: bool | None = None
    model: str | None = None
    language: str | None = None
    compute_type: str | None = None


class VoiceJobOut(Schema):
    message_id: str
    chat_id: str
    status: str
    text: str | None = None
    error: str | None = None
    model_name: str | None = None
