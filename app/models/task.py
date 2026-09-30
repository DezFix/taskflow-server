"""Задачи, комментарии, история, метки."""

from __future__ import annotations

import enum
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    Table,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import IdMixin, TimestampMixin, utcnow
from app.models.base import Base

if TYPE_CHECKING:
    from app.models.user import User


class TaskStatus(str, enum.Enum):
    NEW = "new"
    IN_PROGRESS = "in_progress"
    REVIEW = "review"
    DONE = "done"
    CANCELLED = "cancelled"

    @property
    def title(self) -> str:
        return {
            "new": "Новая",
            "in_progress": "В работе",
            "review": "На проверке",
            "done": "Выполнена",
            "cancelled": "Отменена",
        }[self.value]

    @property
    def is_open(self) -> bool:
        return self not in (TaskStatus.DONE, TaskStatus.CANCELLED)


class TaskPriority(str, enum.Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"

    @property
    def title(self) -> str:
        return {"low": "Низкий", "normal": "Обычный", "high": "Высокий", "urgent": "Срочный"}[
            self.value
        ]

    @property
    def weight(self) -> int:
        return {"low": 0, "normal": 1, "high": 2, "urgent": 3}[self.value]


task_tag_links = Table(
    "task_tag_links",
    Base.metadata,
    Column("task_id", String(36), ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True),
    Column("tag_id", String(36), ForeignKey("task_tags.id", ondelete="CASCADE"), primary_key=True),
)


class TaskTag(Base, IdMixin, TimestampMixin):
    __tablename__ = "task_tags"

    name: Mapped[str] = mapped_column(String(60), nullable=False, unique=True)
    color: Mapped[str | None] = mapped_column(String(20), nullable=True)

    tasks: Mapped[list[Task]] = relationship(secondary=task_tag_links, back_populates="tags")


class Task(Base, IdMixin, TimestampMixin):
    """Задача отдела."""

    __tablename__ = "tasks"

    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[TaskStatus] = mapped_column(
        Enum(TaskStatus, native_enum=False, length=20),
        nullable=False,
        default=TaskStatus.NEW,
        index=True,
    )
    priority: Mapped[TaskPriority] = mapped_column(
        Enum(TaskPriority, native_enum=False, length=20),
        nullable=False,
        default=TaskPriority.NORMAL,
        index=True,
    )

    author_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    assignee_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )

    due_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    estimated_hours: Mapped[float | None] = mapped_column(nullable=True)

    # Счётчик для курсорной пагинации: монотонный, не зависит от дат.
    seq: Mapped[int] = mapped_column(nullable=False, default=0, index=True)
    is_archived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Держим счётчики в колонках: ленивая загрузка связей в async-коде
    # невозможна, а подсчитывать комментарии на каждой странице списка дорого.
    comments_count: Mapped[int] = mapped_column(nullable=False, default=0)
    attachments_count: Mapped[int] = mapped_column(nullable=False, default=0)

    author: Mapped[User] = relationship(foreign_keys=[author_id], lazy="joined")
    assignee: Mapped[User | None] = relationship(foreign_keys=[assignee_id], lazy="joined")
    tags: Mapped[list[TaskTag]] = relationship(
        secondary=task_tag_links, back_populates="tasks", lazy="selectin"
    )
    # Комментарии и историю грузим явно там, где они нужны (load_task).
    comments: Mapped[list[TaskComment]] = relationship(
        back_populates="task", cascade="all, delete-orphan", lazy="raise"
    )
    history: Mapped[list[TaskHistory]] = relationship(
        back_populates="task", cascade="all, delete-orphan", lazy="raise"
    )

    @property
    def is_overdue(self) -> bool:
        if self.due_at is None or self.completed_at is not None:
            return False
        return self.due_at < utcnow()


class TaskComment(Base, IdMixin, TimestampMixin):
    """Комментарий к задаче. Может содержать фотоотчёт."""

    __tablename__ = "task_comments"

    task_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    author_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    attachment_ids: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    is_work_report: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    task: Mapped[Task] = relationship(back_populates="comments", lazy="raise")
    author: Mapped[User] = relationship(lazy="joined", foreign_keys=[author_id])


class TaskHistory(Base, IdMixin):
    """Запись журнала изменений задачи."""

    __tablename__ = "task_history"

    task_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    field: Mapped[str] = mapped_column(String(40), nullable=False)
    old_value: Mapped[str | None] = mapped_column(String(500), nullable=True)
    new_value: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, index=True
    )

    task: Mapped[Task] = relationship(back_populates="history", lazy="raise")
    user: Mapped[User | None] = relationship(lazy="joined")

    __table_args__ = (Index("ix_task_history_task_created", "task_id", "created_at"),)
