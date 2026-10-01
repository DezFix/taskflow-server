"""Сервис задач: создание, изменение, фильтры, история."""

from __future__ import annotations

import base64
import binascii
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Select, and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import utcnow
from app.errors import bad_request, forbidden, not_found
from app.models import (
    Task,
    TaskComment,
    TaskHistory,
    TaskPriority,
    TaskStatus,
    TaskTag,
    User,
)
from app.security import new_id

#: Набор полей, попадающих в журнал изменений и в уведомление клиенту.
HISTORY_FIELDS = {
    "title": "Заголовок",
    "description": "Описание",
    "status": "Статус",
    "priority": "Приоритет",
    "assignee_id": "Исполнитель",
    "due_at": "Срок",
    "estimated_hours": "Оценка часов",
}

_HISTORY_RENDERERS: dict[str, Any] = {
    "status": lambda v: TaskStatus(v).title if v in {s.value for s in TaskStatus} else v,
    "priority": lambda v: (
        TaskPriority(v).title if v in {p.value for p in TaskPriority} else v
    ),
    "due_at": lambda v: v.isoformat() if hasattr(v, "isoformat") else str(v),
}


def encode_cursor(seq: int) -> str:
    return base64.urlsafe_b64encode(str(seq).encode()).decode().rstrip("=")


def decode_cursor(cursor: str | None) -> int | None:
    if not cursor:
        return None
    try:
        padding = "=" * (-len(cursor) % 4)
        return int(base64.urlsafe_b64decode(cursor + padding).decode())
    except (ValueError, binascii.Error, UnicodeDecodeError):
        raise bad_request("bad_cursor", "Курсор пагинации повреждён") from None


def _render(field: str, value: Any) -> str | None:
    if value is None:
        return None
    renderer = _HISTORY_RENDERERS.get(field)
    if renderer:
        return renderer(value)
    if field == "assignee_id":
        return str(value)
    text = str(value)
    return text[:500]


async def next_task_seq(session: AsyncSession) -> int:
    """Монотонный номер задачи. Читаем максимум и увеличиваем.

    Для небольшого отдела этого достаточно; при большой нагрузке
    счётчик стоит вынести в отдельную таблицу.
    """
    current = await session.scalar(select(func.max(Task.seq)))
    return (current or 0) + 1


async def bump_counters(
    session: AsyncSession,
    task: Task,
    *,
    comments: int = 0,
    attachments: int = 0,
) -> None:
    """Увеличивает счётчики задачи одним UPDATE.

    Раньше счётчик читали в объекте и записывали целиком. Два
    одновременных комментария оба прочитали одно значение, оба записали
    «плюс один», и счётчик навсегда расходился с числом строк в
    task_comments. Счётчик показывается в списке задач, а реальный
    список комментариев — в карточке, и данные противоречили друг другу.
    """
    if not comments and not attachments:
        return
    values: dict[str, int] = {}
    if comments:
        values["comments_count"] = Task.comments_count + comments
    if attachments:
        values["attachments_count"] = Task.attachments_count + attachments
    await session.execute(update(Task).where(Task.id == task.id).values(**values))


async def get_tags(session: AsyncSession, names: list[str]) -> list[TaskTag]:
    """Находит или создаёт метки. Повтор в одном запросе убираем."""
    cleaned: list[str] = []
    for name in names:
        value = (name or "").strip()
        if value and value.lower() not in {c.lower() for c in cleaned}:
            cleaned.append(value[:60])
    if not cleaned:
        return []

    existing = (
        await session.scalars(select(TaskTag).where(TaskTag.name.in_(cleaned)))
    ).all()
    by_name = {tag.name.lower(): tag for tag in existing}

    result: list[TaskTag] = []
    for name in cleaned:
        tag = by_name.get(name.lower())
        if tag is None:
            tag = TaskTag(id=new_id(), name=name)
            session.add(tag)
            by_name[name.lower()] = tag
        result.append(tag)
    await session.flush()
    return result


def apply_filters(stmt: Select, params: Any) -> Select:
    """Переносит параметры фильтра в WHERE. Одна точка правды для всех выборок."""
    if params.is_archived is not None:
        stmt = stmt.where(Task.is_archived.is_(params.is_archived))

    if params.status:
        stmt = stmt.where(Task.status.in_(params.status))
    if params.priority:
        stmt = stmt.where(Task.priority.in_(params.priority))
    if params.assignee_id:
        stmt = stmt.where(Task.assignee_id == params.assignee_id)
    if params.author_id:
        stmt = stmt.where(Task.author_id == params.author_id)

    if params.tag:
        stmt = stmt.join(Task.tags).where(TaskTag.name == params.tag)

    if params.due_from:
        stmt = stmt.where(Task.due_at.is_not(None), Task.due_at >= params.due_from)
    if params.due_to:
        stmt = stmt.where(Task.due_at.is_not(None), Task.due_at <= params.due_to)

    if params.overdue:
        now = utcnow()
        stmt = stmt.where(
            Task.due_at.is_not(None),
            Task.due_at < now,
            Task.completed_at.is_(None),
            # Закрытые задачи просроченными не считаются. Условие на
            # два неравенства через and_, а не or_: через or_ оно
            # всегда истинно и пропускало бы выполненные задачи.
            and_(
                Task.status != TaskStatus.DONE,
                Task.status != TaskStatus.CANCELLED,
            ),
        )

    if params.search:
        pattern = f"%{params.search.strip().lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(Task.title).like(pattern),
                func.lower(func.coalesce(Task.description, "")).like(pattern),
            )
        )

    return stmt


def visibility_filter(stmt: Select, user: User, can_see_all: bool) -> Select:
    """Сотрудник без прав видно только свои задачи."""
    if can_see_all:
        return stmt
    return stmt.where(or_(Task.assignee_id == user.id, Task.author_id == user.id))


def touch(entity: Any) -> Any:
    """Проставляет updated_at вручную.

    Если оставить onupdate=func.now(), SQLAlchemy помечает колонку
    просроченной и перечитывает её отдельным запросом — в async это
    приводит к ошибке greenlet. Явное присваивание избавляет от лишнего IO.
    """
    entity.updated_at = utcnow()
    return entity


async def record_change(
    session: AsyncSession,
    task: Task,
    user_id: str,
    field: str,
    old_value: Any,
    new_value: Any,
) -> None:
    if field not in HISTORY_FIELDS:
        return
    if _render(field, old_value) == _render(field, new_value):
        return
    session.add(
        TaskHistory(
            id=new_id(),
            task_id=task.id,
            user_id=user_id,
            field=field,
            old_value=_render(field, old_value),
            new_value=_render(field, new_value),
            created_at=utcnow(),
        )
    )


async def create_task(session: AsyncSession, author: User, payload: Any) -> Task:
    assignee = None
    if payload.assignee_id:
        assignee = await session.get(User, payload.assignee_id)
        if assignee is None or not assignee.is_active:
            raise bad_request("assignee_invalid", "Исполнитель не найден или отключён")

    task = Task(
        id=new_id(),
        title=payload.title.strip(),
        description=(payload.description or "").strip() or None,
        status=payload.status,
        priority=payload.priority,
        author_id=author.id,
        assignee_id=payload.assignee_id,
        due_at=payload.due_at,
        estimated_hours=payload.estimated_hours,
        seq=await next_task_seq(session),
    )
    # Связи задаём объектами, а не только идентификаторами: в async-коде
    # ленивая загрузка недоступна, и сериализатору нужен готовый автор.
    task.author = author
    if assignee is not None:
        task.assignee = assignee
    if task.status == TaskStatus.DONE:
        task.completed_at = utcnow()

    # Метки задаём всегда, даже пустым списком: иначе обращение к task.tags
    # в сериализаторе вызовет ленивую загрузку, недоступную в async.
    task.tags = await get_tags(session, payload.tags or [])

    session.add(task)
    await session.flush()
    await record_change(session, task, author.id, "title", None, task.title)
    return task


async def load_task(session: AsyncSession, task_id: str) -> Task:
    task = await session.scalar(
        select(Task)
        .where(Task.id == task_id)
        .options(
            selectinload(Task.author).selectinload(User.roles),
            selectinload(Task.assignee).selectinload(User.roles),
            selectinload(Task.tags),
            selectinload(Task.comments).selectinload(TaskComment.author).selectinload(User.roles),
            selectinload(Task.history).selectinload(TaskHistory.user).selectinload(User.roles),
        )
    )
    if task is None:
        raise not_found("task_not_found", "Задача не найдена") from None
    return task


def ensure_can_view(task: Task, user: User, can_see_all: bool) -> None:
    if can_see_all:
        return
    if task.assignee_id == user.id or task.author_id == user.id:
        return
    raise forbidden("task_access_denied", "Нет доступа к этой задаче") from None
def ensure_can_edit(task: Task, user: User, can_edit_any: bool, can_edit_assigned: bool) -> None:
    if can_edit_any:
        return
    if can_edit_assigned and task.assignee_id == user.id:
        return
    if task.author_id == user.id and can_edit_assigned:
        return
    raise forbidden("task_edit_denied", "Нет прав на изменение этой задачи") from None
async def update_task(session: AsyncSession, task: Task, user: User, payload: Any) -> list[dict]:
    """Применяет частичное обновление и пишет историю. Возвращает изменения."""
    changes: list[dict] = []
    data = payload.model_dump(exclude_unset=True)

    new_assignee: User | None = None
    if "assignee_id" in data and data["assignee_id"]:
        new_assignee = await session.get(User, data["assignee_id"])
        if new_assignee is None or not new_assignee.is_active:
            raise bad_request("assignee_invalid", "Исполнитель не найден или отключён")

    for field, value in data.items():
        if field in ("tags", "assignee_id"):
            continue
        if not hasattr(task, field):
            continue
        old = getattr(task, field)
        if old == value:
            continue
        setattr(task, field, value)
        await record_change(session, task, user.id, field, old, value)
        changes.append({"field": field, "old": old, "new": value})

    if "assignee_id" in data:
        old_assignee_id = task.assignee_id
        if old_assignee_id != data["assignee_id"]:
            await record_change(
                session, task, user.id, "assignee_id", old_assignee_id, data["assignee_id"]
            )
            changes.append(
                {"field": "assignee_id", "old": old_assignee_id, "new": data["assignee_id"]}
            )
        task.assignee_id = data["assignee_id"]
        # Объект исполнителя нужен сериализатору сразу, без ленивой загрузки.
        task.assignee = new_assignee

    if "tags" in data and data["tags"] is not None:
        task.tags = await get_tags(session, data["tags"])
        changes.append({"field": "tags", "old": None, "new": data["tags"]})

    if changes:
        touch(task)
    await session.flush()
    return changes


async def change_status(
    session: AsyncSession, task: Task, user: User, new_status: TaskStatus, comment: str | None
) -> None:
    old_status = task.status
    if old_status == new_status:
        return

    task.status = new_status
    if new_status == TaskStatus.DONE:
        task.completed_at = utcnow()
    else:
        task.completed_at = None
    touch(task)

    await record_change(session, task, user.id, "status", old_status.value, new_status.value)
    await session.flush()

    if comment and comment.strip():
        note = TaskComment(
            id=new_id(),
            task_id=task.id,
            author_id=user.id,
            body=comment.strip(),
            is_work_report=new_status == TaskStatus.DONE,
        )
        note.author = user
        session.add(note)
        await bump_counters(session, task, comments=1)
        await session.flush()


async def assign_task(
    session: AsyncSession, task: Task, user: User, assignee_id: str | None, due_at: datetime | None
) -> None:
    new_assignee: User | None = None
    if assignee_id:
        new_assignee = await session.get(User, assignee_id)
        if new_assignee is None or not new_assignee.is_active:
            raise bad_request("assignee_invalid", "Исполнитель не найден или отключён")

    if task.assignee_id != assignee_id:
        await record_change(session, task, user.id, "assignee_id", task.assignee_id, assignee_id)
        task.assignee_id = assignee_id
        task.assignee = new_assignee

    if due_at is not None and task.due_at != due_at:
        await record_change(session, task, user.id, "due_at", task.due_at, due_at)
        task.due_at = due_at

    touch(task)
    await session.flush()


async def add_comment(
    session: AsyncSession,
    task: Task,
    user: User,
    body: str | None,
    attachment_ids: list[str],
    is_work_report: bool,
) -> TaskComment:
    if not body and not attachment_ids:
        raise bad_request("comment_empty", "Комментарий должен содержать текст или вложение")

    comment = TaskComment(
        id=new_id(),
        task_id=task.id,
        author_id=user.id,
        body=(body or "").strip() or None,
        attachment_ids=list(attachment_ids),
        is_work_report=is_work_report,
    )
    comment.author = user
    session.add(comment)
    await bump_counters(session, task, comments=1, attachments=len(attachment_ids))
    await session.flush()

    # Отчёт о выполнении переводит задачу в «на проверке» — это ожидаемое
    # поведение для руководителя, который затем примет или вернёт работу.
    if is_work_report and task.status in (TaskStatus.NEW, TaskStatus.IN_PROGRESS):
        old_status = task.status
        task.status = TaskStatus.REVIEW
        await record_change(session, task, user.id, "status", old_status.value, task.status.value)
        touch(task)

    await session.flush()
    return comment


def overdue_window(days: int = 7) -> datetime:
    return utcnow() - timedelta(days=days)


def task_visibility_predicate(user: User, can_see_all: bool):  # noqa: ANN202
    if can_see_all:
        return None
    return and_(or_(Task.assignee_id == user.id, Task.author_id == user.id))
