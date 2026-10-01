"""Схемы аутентификации и профиля."""

from __future__ import annotations

from datetime import datetime

from pydantic import EmailStr, Field, field_validator

from app.schemas.common import Schema, to_utc


class SetupRequest(Schema):
    """Мастер первого запуска: создаёт администратора и базовые роли."""

    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=8, max_length=128)
    full_name: str = Field(min_length=2, max_length=200)
    email: EmailStr | None = None
    organization_name: str | None = Field(default=None, max_length=120)


class LoginRequest(Schema):
    # Принимаем логин или email — сотрудник не должен запоминать разницу.
    identifier: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=1, max_length=128)
    device_name: str | None = Field(default=None, max_length=200)


class RefreshRequest(Schema):
    refresh_token: str = Field(min_length=10)


class TokenPair(Schema):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    user_id: str
    session_id: str


class RoleBrief(Schema):
    id: str
    key: str
    title: str
    permissions: list[str] = []


class UserBrief(Schema):
    """Лёгкая карточка сотрудника для списков и чатов."""

    id: str
    username: str
    full_name: str
    avatar_url: str | None = None
    is_active: bool = True

    @field_validator("avatar_url", mode="before")
    @classmethod
    def _avatar(cls, value: str | None) -> str | None:
        return value


class UserMe(Schema):
    id: str
    username: str
    full_name: str
    email: str | None = None
    phone: str | None = None
    avatar_url: str | None = None
    roles: list[RoleBrief] = []
    permissions: list[str] = []
    is_active: bool
    is_superuser: bool = False
    must_change_password: bool = False
    last_login_at: datetime | None = None
    created_at: datetime | None = None

    @field_validator("last_login_at", "created_at", mode="after")
    @classmethod
    def _ser_dt(cls, value: datetime | None) -> datetime | None:
        return to_utc(value)

    def is_workspace_head(self) -> bool:
        """Главное рабочее пространство = управленческие права."""
        return "settings.manage_roles" in self.permissions or "users.create" in self.permissions


class ChangePasswordRequest(Schema):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


class UpdateProfileRequest(Schema):
    full_name: str | None = Field(default=None, min_length=2, max_length=200)
    email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=40)


class SessionInfo(Schema):
    id: str
    device_name: str | None = None
    ip_address: str | None = None
    created_at: datetime | None = None
    last_used: datetime | None = None
    expires_at: datetime | None = None
    is_current: bool = False


class ServerInfo(Schema):
    """Ответ на /api/v1/meta/info — клиент проверяет адрес при входе."""

    name: str = "TaskFlow"
    version: str
    requires_setup: bool
    voice_enabled: bool
    voice_model: str
    auth_methods: list[str] = ["password"]
