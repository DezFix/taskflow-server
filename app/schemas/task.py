"""Схемы задач, комментариев и отчётов."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, field_serializer, field_validator

from app.models.task import TaskPriority, TaskStatus
from app.schemas.auth import UserBrief
from app.schemas.common import Schema, to_utc


class TagOut(Schema):
    id: str
    name: str
    color: str | None = None


class AttachmentBrief(Schema):
    id: str
    kind: str
    name: str
    mime_type: str
    size_bytes: int
    url: str | None = None
    preview_url: str | None = None
    width: int | None = None
    height: int | None = None
    duration_sec: float | None = None


class HistoryOut(Schema):
    id: str
    field: str
    old_value: str | None = None
    new_value: str | None = None
    created_at: datetime
    user: UserBrief | None = None

    @field_serializer("created_at")
    def _ser_created(self, value: datetime) -> str:
        dt = to_utc(value)
        return dt.isoformat().replace("+00:00", "Z") if dt else ""


class CommentOut(Schema):
    id: str
    task_id: str
    body: str | None = None
    author: UserBrief
    attachments: list[AttachmentBrief] = []
    is_work_report: bool = False
    created_at: datetime
    updated_at: datetime | None = None

    @field_serializer("created_at", "updated_at")
    def _ser(self, value: datetime | None) -> str | None:
        dt = to_utc(value)
        return dt.isoformat().replace("+00:00", "Z") if dt else None


class TaskOut(Schema):
    id: str
    title: str
    description: str | None = None
    status: TaskStatus
    status_title: str
    priority: TaskPriority
    priority_title: str
    author: UserBrief
    assignee: UserBrief | None = None
    due_at: datetime | None = None
    completed_at: datetime | None = None
    estimated_hours: float | None = None
    tags: list[TagOut] = []
    is_overdue: bool = False
    comments_count: int = 0
    attachments_count: int = 0
    is_archived: bool = False
    created_at: datetime
    updated_at: datetime | None = None

    @field_serializer("due_at", "completed_at", "created_at", "updated_at")
    def _ser_dt(self, value: datetime | None) -> str | None:
        dt = to_utc(value)
        return dt.isoformat().replace("+00:00", "Z") if dt else None


class TaskDetailOut(TaskOut):
    comments: list[CommentOut] = []
    history: list[HistoryOut] = []
    attachments: list[AttachmentBrief] = []


class TaskCreate(Schema):
    title: str = Field(min_length=3, max_length=300)
    description: str | None = Field(default=None, max_length=20000)
    assignee_id: str | None = None
    status: TaskStatus = TaskStatus.NEW
    priority: TaskPriority = TaskPriority.NORMAL
    due_at: datetime | None = None
    estimated_hours: float | None = Field(default=None, gt=0, le=10000)
    tags: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("due_at")
    @classmethod
    def _tz_aware(cls, value: datetime | None) -> datetime | None:
        """Приводим к UTC-naive: в БД у нас единое время без таймзон."""
        if value is None:
            return None
        if value.tzinfo is not None:
            from datetime import UTC

            return value.astimezone(UTC).replace(tzinfo=None)
        return value


class TaskUpdate(Schema):
    title: str | None = Field(default=None, min_length=3, max_length=300)
    description: str | None = Field(default=None, max_length=20000)
    assignee_id: str | None = None
    priority: TaskPriority | None = None
    due_at: datetime | None = None
    estimated_hours: float | None = Field(default=None, gt=0, le=10000)
    tags: list[str] | None = Field(default=None, max_length=20)
    is_archived: bool | None = None


class TaskStatusChange(Schema):
    status: TaskStatus
    comment: str | None = Field(default=None, max_length=4000)


class TaskAssign(Schema):
    assignee_id: str | None = None
    due_at: datetime | None = None
    comment: str | None = Field(default=None, max_length=4000)


class CommentCreate(Schema):
    body: str | None = Field(default=None, max_length=20000)
    attachment_ids: list[str] = Field(default_factory=list, max_length=20)
    is_work_report: bool = False

    @field_validator("body")
    @classmethod
    def _not_empty(cls, value: str | None) -> str | None:
        if value is not None:
            value = value.strip()
        return value or None


class TaskFilter(Schema):
    search: str | None = Field(default=None, max_length=200)
    status: list[TaskStatus] = Field(default_factory=list)
    priority: list[TaskPriority] = Field(default_factory=list)
    assignee_id: str | None = None
    author_id: str | None = None
    tag: str | None = None
    due_from: datetime | None = None
    due_to: datetime | None = None
    overdue: bool | None = None
    is_archived: bool = False


# --- Отчёты ---


class TaskSummaryOut(Schema):
    total: int
    by_status: dict[str, int]
    by_priority: dict[str, int]
    created_this_week: int
    completed_this_week: int
    overdue: int
    unassigned: int
    completion_rate: float


class UserLoadRow(Schema):
    user: UserBrief
    assigned: int
    in_progress: int
    completed: int
    overdue: int
    completion_rate: float
    comments_count: int = 0
    reports_count: int = 0


class UserLoadOut(Schema):
    rows: list[UserLoadRow]
    period_from: datetime | None = None
    period_to: datetime | None = None


#: Имя, под которым роутер задач обращается к строке нагрузки.
TaskUserLoadRow = UserLoadRow
