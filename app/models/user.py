"""Пользователи, роли, должности, refresh-сессии."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Table,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import IdMixin, TimestampMixin, utcnow
from app.models.base import Base

user_roles = Table(
    "user_roles",
    Base.metadata,
    Column(
        "user_id",
        String(36),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "role_id",
        String(36),
        ForeignKey("roles.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)


class Position(Base, IdMixin, TimestampMixin):
    """Должность: системный инженер, тестировщик, аналитик и т.п."""

    __tablename__ = "positions"

    title: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    users: Mapped[list[User]] = relationship(back_populates="position")

    __table_args__ = (UniqueConstraint("title", name="uq_positions_title"),)


class Role(Base, IdMixin, TimestampMixin):
    """Роль с набором прав. Системные роли помечены is_system."""

    __tablename__ = "roles"

    key: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Список ключей прав, например ["tasks.view_all", "tasks.create"].
    permissions: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    users: Mapped[list[User]] = relationship(
        secondary=user_roles, back_populates="roles", lazy="selectin"
    )

    @property
    def permission_set(self) -> set[str]:
        return set(self.permissions or [])


class User(Base, IdMixin, TimestampMixin):
    """Сотрудник. Удаление мягкое: is_active=False, история сохраняется."""

    __tablename__ = "users"

    username: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True)
    full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(40), nullable=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    avatar_file_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    job_title: Mapped[str | None] = mapped_column(String(160), nullable=True)

    position_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("positions.id", ondelete="SET NULL"), nullable=True
    )

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_superuser: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    must_change_password: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Защита от перебора пароля
    failed_login_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    password_changed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    position: Mapped[Position | None] = relationship(back_populates="users", lazy="joined")
    roles: Mapped[list[Role]] = relationship(
        secondary=user_roles, back_populates="users", lazy="selectin"
    )

    @property
    def permissions(self) -> set[str]:
        """Собранные права из всех ролей. Суперпользователю доступно всё."""
        if self.is_superuser:
            from app.permissions import PERMISSION_KEYS

            return set(PERMISSION_KEYS)
        result: set[str] = set()
        for role in self.roles:
            result |= role.permission_set
        return result

    @property
    def display_name(self) -> str:
        if self.job_title:
            return f"{self.full_name} ({self.job_title})"
        return self.full_name

    def is_locked(self) -> bool:
        return self.locked_until is not None and self.locked_until > utcnow()


class RefreshSession(Base, IdMixin, TimestampMixin):
    """Активная сессия устройства. Отзыв = удаление строки."""

    __tablename__ = "refresh_sessions"

    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    device_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(400), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    user: Mapped[User] = relationship(lazy="joined")
