"""Отчёты по задачам и нагрузке сотрудников."""

from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta

from sqlalchemy import Select, func, select  # noqa: F401
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import utcnow
from app.models import Task, TaskComment, TaskPriority, TaskStatus, User

WEEK = timedelta(days=7)


async def summary(session: AsyncSession, user: User, can_see_all: bool) -> dict:
    """Сводка по задачам отдела за всё время и за неделю."""
    scope: list = [Task.is_archived.is_(False)]
    if not can_see_all:
        scope.append(Task.assignee_id == user.id)

    async def count(*extra) -> int:  # noqa: ANN001
        stmt = select(func.count(Task.id)).where(*scope, *extra)
        return int(await session.scalar(stmt) or 0)

    total = await count()

    by_status: dict[str, int] = {s.value: 0 for s in TaskStatus}
    for status, value in (
        await session.execute(
            select(Task.status, func.count(Task.id)).where(*scope).group_by(Task.status)
        )
    ).all():
        by_status[status.value if hasattr(status, "value") else str(status)] = int(value)

    by_priority: dict[str, int] = {p.value: 0 for p in TaskPriority}
    for priority, value in (
        await session.execute(
            select(Task.priority, func.count(Task.id)).where(*scope).group_by(Task.priority)
        )
    ).all():
        by_priority[priority.value if hasattr(priority, "value") else str(priority)] = int(value)

    week_ago = utcnow() - WEEK
    created_week = await count(Task.created_at >= week_ago)
    completed_week = await count(Task.completed_at.is_not(None), Task.completed_at >= week_ago)
    overdue = await count(
        Task.due_at.is_not(None),
        Task.due_at < utcnow(),
        Task.completed_at.is_(None),
        Task.status != TaskStatus.DONE,
    )
    unassigned = await count(Task.assignee_id.is_(None), Task.status != TaskStatus.CANCELLED)

    done_int = by_status.get(TaskStatus.DONE.value, 0)
    cancelled_int = by_status.get(TaskStatus.CANCELLED.value, 0)
    closed = done_int + cancelled_int
    completion_rate = round(done_int / closed * 100, 1) if closed else 0.0

    return {
        "total": total,
        "by_status": by_status,
        "by_priority": by_priority,
        "created_this_week": created_week,
        "completed_this_week": completed_week,
        "overdue": overdue,
        "unassigned": unassigned,
        "completion_rate": completion_rate,
    }


async def user_load(
    session: AsyncSession,
    can_see_all: bool,
    *,
    period_from: datetime | None = None,
    period_to: datetime | None = None,
) -> dict:
    """Нагрузка по сотрудникам: сколько задач и в каком состоянии."""
    users = (
        await session.scalars(
            select(User)
            .where(User.is_active.is_(True))
            .options(selectinload(User.roles))
            .order_by(User.full_name)
        )
    ).all()

    rows: list[dict] = []
    now = utcnow()
    for member in users:
        filters = [Task.assignee_id == member.id]
        if period_from:
            filters.append(Task.created_at >= period_from)
        if period_to:
            filters.append(Task.created_at <= period_to)

        counts: dict[str, int] = {}
        result = await session.execute(
            select(Task.status, func.count(Task.id))
            .where(*filters, Task.is_archived.is_(False))
            .group_by(Task.status)
        )
        for status, count in result:
            key = status.value if hasattr(status, "value") else str(status)
            counts[key] = int(count)

        assigned = sum(counts.values())
        completed = counts.get(TaskStatus.DONE.value, 0)
        closed = completed + counts.get(TaskStatus.CANCELLED.value, 0)

        overdue = await session.scalar(
            select(func.count(Task.id)).where(
                Task.assignee_id == member.id,
                Task.due_at.is_not(None),
                Task.due_at < now,
                Task.completed_at.is_(None),
                Task.status != TaskStatus.DONE,
                Task.is_archived.is_(False),
            )
        )

        comments_count = (
            await session.scalar(
                select(func.count(TaskComment.id))
                .join(Task, Task.id == TaskComment.task_id)
                .where(Task.assignee_id == member.id, TaskComment.is_work_report.is_(True))
            )
            if can_see_all
            else 0
        )

        rows.append(
            {
                "user": member,
                "assigned": assigned,
                "in_progress": counts.get(TaskStatus.IN_PROGRESS.value, 0)
                + counts.get(TaskStatus.REVIEW.value, 0),
                "completed": completed,
                "overdue": int(overdue or 0),
                "completion_rate": round(completed / closed * 100, 1) if closed else 0.0,
                "reports_count": int(comments_count or 0),
            }
        )

    return {"rows": rows, "period_from": period_from, "period_to": period_to}


async def export_csv(
    session: AsyncSession,
    can_see_all: bool,
    *,
    user_id: str | None = None,
    period_from: datetime | None = None,
    period_to: datetime | None = None,
) -> str:
    """Выгрузка задач в CSV для Excel. Сотрудник видит только свои
    задачи, руководитель — по фильтру периода."""
    filters = [Task.is_archived.is_(False)]
    if not can_see_all:
        # Сотрудник выгружает только своё: подставлять нужно его
        # идентификатор, который передаёт вызывающий код.
        if user_id is None:
            raise ValueError("Для выгрузки сотрудника нужен user_id")
        filters.append(Task.assignee_id == user_id)

    if period_from:
        filters.append(Task.created_at >= period_from)
    if period_to:
        filters.append(Task.created_at <= period_to)

    tasks = (
        await session.scalars(
            select(Task)
            .where(*filters)
            .options(
                selectinload(Task.assignee),
                selectinload(Task.author),
                selectinload(Task.tags),
            )
            .order_by(Task.created_at.desc())
        )
    ).all()

    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";", lineterminator="\n")
    writer.writerow(
        [
            "ID",
            "Заголовок",
            "Статус",
            "Приоритет",
            "Исполнитель",
            "Автор",
            "Срок",
            "Создана",
            "Завершена",
            "Просрочена",
            "Метки",
            "Часы",
        ]
    )
    for task in tasks:
        writer.writerow(
            [
                task.id,
                task.title,
                TaskStatus(task.status).title,
                TaskPriority(task.priority).title,
                task.assignee.full_name if task.assignee else "",
                task.author.full_name if task.author else "",
                task.due_at.strftime("%Y-%m-%d %H:%M") if task.due_at else "",
                task.created_at.strftime("%Y-%m-%d %H:%M") if task.created_at else "",
                task.completed_at.strftime("%Y-%m-%d %H:%M") if task.completed_at else "",
                "да" if task.is_overdue else "нет",
                ", ".join(tag.name for tag in task.tags),
                task.estimated_hours or "",
            ]
        )
    return buffer.getvalue()
