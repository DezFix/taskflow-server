"""Схемы сотрудников, должностей и ролей."""

from __future__ import annotations

from datetime import datetime

from pydantic import EmailStr, Field, field_validator

from app.schemas.auth import RoleBrief, UserBrief
from app.schemas.common import Schema, to_utc

# --- Роли ---


class RoleCreate(Schema):
    title: str = Field(min_length=2, max_length=120)
    key: str | None = Field(default=None, max_length=80)
    description: str | None = Field(default=None, max_length=500)
    permissions: list[str] = Field(default_factory=list)

    @field_validator("permissions")
    @classmethod
    def _check_permissions(cls, value: list[str]) -> list[str]:
        from app.errors import bad_request
        from app.permissions import PERMISSION_KEYS

        unknown = [p for p in value if p not in PERMISSION_KEYS]
        if unknown:
            raise bad_request(
                "unknown_permission",
                "Неизвестные права: " + ", ".join(sorted(unknown)),
                {"unknown": sorted(unknown)},
            )
        return sorted(set(value))


class RoleUpdate(Schema):
    title: str | None = Field(default=None, min_length=2, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    permissions: list[str] | None = None

    @field_validator("permissions")
    @classmethod
    def _check_permissions(cls, value: list[str] | None) -> list[str] | None:
        from app.errors import bad_request
        from app.permissions import PERMISSION_KEYS

        if value is None:
            return None
        unknown = [p for p in value if p not in PERMISSION_KEYS]
        if unknown:
            raise bad_request(
                "unknown_permission",
                "Неизвестные права: " + ", ".join(sorted(unknown)),
                {"unknown": sorted(unknown)},
            )
        return sorted(set(value))


class RoleOut(Schema):
    id: str
    key: str
    title: str
    # Машиночитаемый идентификатор системной роли: 'admin', 'head'
    # или 'staff'. Ключ в базе русский, а клиент переводит название
    # по этому полю, поэтому язык хранилища не влияет на интерфейс.
    i18n_key: str | None = None
    description: str | None = None
    permissions: list[str] = []
    is_system: bool = False
    users_count: int = 0
    created_at: datetime | None = None

    @field_validator("created_at", mode="after")
    @classmethod
    def _ser(cls, value: datetime | None) -> datetime | None:
        return to_utc(value)


class PermissionOut(Schema):
    key: str
    title: str
    group: str
    description: str = ""


# --- Сотрудники ---


class UserCreate(Schema):
    username: str = Field(min_length=3, max_length=64)
    full_name: str = Field(min_length=2, max_length=200)
    email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=40)
    role_ids: list[str] = Field(default_factory=list)
    password: str | None = Field(default=None, min_length=8, max_length=128)
    must_change_password: bool = True

    @field_validator("username")
    @classmethod
    def _norm_username(cls, value: str) -> str:
        from app.services.auth import validate_username

        return validate_username(value)


class UserUpdate(Schema):
    full_name: str | None = Field(default=None, min_length=2, max_length=200)
    email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=40)
    role_ids: list[str] | None = None
    is_active: bool | None = None


class UserOut(UserBrief):
    email: str | None = None
    phone: str | None = None
    roles: list[RoleBrief] = []
    permissions: list[str] = []
    is_superuser: bool = False
    must_change_password: bool = False
    last_login_at: datetime | None = None
    created_at: datetime | None = None

    @field_validator("last_login_at", "created_at", mode="after")
    @classmethod
    def _ser(cls, value: datetime | None) -> datetime | None:
        return to_utc(value)


class UserCreatedOut(UserOut):
    """При создании сотрудника один раз возвращаем временный пароль."""

    temporary_password: str | None = None


class ResetPasswordRequest(Schema):
    new_password: str | None = Field(default=None, min_length=8, max_length=128)
    must_change_password: bool = True


class ResetPasswordOut(Schema):
    user_id: str
    temporary_password: str
    must_change_password: bool = True


class UserFilter(Schema):
    search: str | None = None
    is_active: bool | None = None
    role_id: str | None = None
