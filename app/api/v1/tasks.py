"""Задачи: список, карточка, изменение, комментарии, история."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.database import utcnow
from app.deps import SessionDep, has_perm, require_perm
from app.errors import bad_request, forbidden
from app.models import Attachment, Task, TaskComment, TaskPriority, TaskStatus, User
from app.realtime import (
    EVENT_TASK_CREATED,
    EVENT_TASK_DELETED,
    EVENT_TASK_UPDATED,
    hub,
)
from app.schemas import (
    AttachmentBrief,
    CommentCreate,
    CommentOut,
    OkMessage,
    Page,
    TaskAssign,
    TaskCreate,
    TaskDetailOut,
    TaskOut,
    TaskStatusChange,
    TaskSummaryOut,
    TaskUpdate,
    TaskUserLoadRow,
    UserLoadOut,
)
from app.security import new_id
from app.serializers import attachment_brief, comment_out, task_detail_out, task_out
from app.services import audit as audit_service
from app.services import reports as reports_service
from app.services import tasks as task_service

router = APIRouter(prefix="/tasks", tags=["tasks"])


def _brief_map(attachments: list[Attachment]) -> dict[str, AttachmentBrief]:
    result: dict[str, AttachmentBrief] = {}
    for attachment in attachments:
        brief = attachment_brief(attachment)
        if brief:
            result[attachment.id] = brief
    return result


async def _load_attachments(
    session: SessionDep, task: Task | None = None, attachment_ids: list[str] | None = None
) -> list[Attachment]:
    ids: set[str] = set(attachment_ids or [])
    if task is not None:
        for comment in task.comments:
            ids.update(comment.attachment_ids or [])
    if not ids:
        return []
    rows = (
        await session.scalars(select(Attachment).where(Attachment.id.in_(ids)))
    ).all()
    return [row for row in rows if not row.is_deleted]


def _can_edit(task: Task, user: User) -> bool:
    return has_perm(user, "tasks.edit_any") or (
        has_perm(user, "tasks.edit_assigned") and task.assignee_id == user.id
    )


def _can_assign(user: User) -> bool:
    return has_perm(user, "tasks.assign")


@router.get(
    "",
    response_model=Page[TaskOut],
    summary="Список задач",
    description=(
        "Курсорная пагинация. Сотрудник без права 'tasks.view_all' "
        "видит только задачи, где он исполнитель или автор."
    ),
)
async def list_tasks(
    session: SessionDep,
    user: User = Depends(require_perm("tasks.view")),
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = None,
    search: str | None = Query(None, max_length=200),
    status: list[TaskStatus] = Query(default=[]),
    priority: list[TaskPriority] = Query(default=[]),
    assignee_id: str | None = None,
    author_id: str | None = None,
    tag: str | None = None,
    due_from: datetime | None = None,
    due_to: datetime | None = None,
    overdue: bool | None = None,
    is_archived: bool = False,
) -> Page[TaskOut]:
    can_see_all = has_perm(user, "tasks.view_all")
    params = task_service_filter(
        search=search,
        status=status,
        priority=priority,
        assignee_id=assignee_id,
        author_id=author_id,
        tag=tag,
        due_from=due_from,
        due_to=due_to,
        overdue=overdue,
        is_archived=is_archived,
    )

    stmt = (
        select(Task)
        .options(
            selectinload(Task.author).selectinload(User.roles),
            selectinload(Task.assignee).selectinload(User.roles),
            selectinload(Task.tags),
        )
        .where(Task.is_archived.is_(is_archived))
    )
    stmt = task_service.apply_filters(stmt, params)
    stmt = task_service.visibility_filter(stmt, user, can_see_all)

    # Курсор по монотонному seq: страницы не «плывут» при новых задачах.
    cursor_seq = task_service.decode_cursor(cursor)
    if cursor_seq is not None:
        stmt = stmt.where(Task.seq < cursor_seq)

    total = await session.scalar(
        select(func.count()).select_from(
            stmt.order_by(None).options().subquery()
        )
    )

    tasks = list(
        (
            await session.scalars(
                stmt.order_by(Task.seq.desc()).limit(limit + 1)
            )
        ).all()
    )
    has_more = len(tasks) > limit
    items = tasks[:limit]

    return Page[TaskOut](
        items=[task_out(task) for task in items],
        next_cursor=task_service.encode_cursor(items[-1].seq) if has_more and items else None,
        has_more=has_more,
        total=int(total or 0),
    )


@router.post(
    "",
    response_model=TaskOut,
    status_code=201,
    summary="Создать задачу",
    description="Можно сразу назначить исполнителя, срок и метки.",
)
async def create_task(
    payload: TaskCreate,
    session: SessionDep,
    user: User = Depends(require_perm("tasks.create")),
) -> TaskOut:
    if payload.assignee_id and not _can_assign(user):
        raise forbidden("assign_denied", "Нет прав назначать исполнителя")
    if payload.status != TaskStatus.NEW and not has_perm(user, "tasks.edit_any"):
        raise forbidden("status_denied", "Нет прав задавать статус при создании")

    task = await task_service.create_task(session, user, payload)
    result = task_out(task)

    recipients = _task_recipients(task, user)
    await hub.send_to_users(recipients, EVENT_TASK_CREATED, result.model_dump())

    await audit_service.log_action(
        session,
        user=user,
        action="task.create",
        entity_type="task",
        entity_id=task.id,
        details={"title": task.title, "assignee_id": task.assignee_id},
    )
    return result


def _task_recipients(task: Task, actor: User) -> set[str]:
    """Кому слать событие: исполнитель и автор, если это разные люди."""
    recipients = {task.author_id}
    if task.assignee_id:
        recipients.add(task.assignee_id)
    recipients.discard(actor.id)
    return recipients


def task_service_filter(**kwargs) -> object:  # noqa: ANN202
    from app.schemas.task import TaskFilter

    return TaskFilter(**kwargs)


@router.get("/{task_id}", response_model=TaskDetailOut, summary="Карточка задачи")
async def get_task(
    task_id: str,
    session: SessionDep,
    user: User = Depends(require_perm("tasks.view")),
) -> TaskDetailOut:
    task = await task_service.load_task(session, task_id)
    task_service.ensure_can_view(task, user, has_perm(user, "tasks.view_all"))
    attachments = await _load_attachments(session, task)
    return task_detail_out(task, _brief_map(attachments))


@router.patch(
    "/{task_id}",
    response_model=TaskOut,
    summary="Изменить задачу",
    description="Все изменения попадают в историю задачи.",
)
async def update_task(
    task_id: str,
    payload: TaskUpdate,
    session: SessionDep,
    user: User = Depends(require_perm("tasks.view")),
) -> TaskOut:
    task = await task_service.load_task(session, task_id)
    task_service.ensure_can_view(task, user, has_perm(user, "tasks.view_all"))
    task_service.ensure_can_edit(
        task,
        user,
        can_edit_any=has_perm(user, "tasks.edit_any"),
        can_edit_assigned=has_perm(user, "tasks.edit_assigned"),
    )

    if payload.model_fields_set:
        fields = payload.model_fields_set
        if "assignee_id" in fields and not _can_assign(user):
            raise forbidden("assign_denied", "Нет прав менять исполнителя")
        if "is_archived" in fields and not has_perm(user, "tasks.delete"):
            raise forbidden("archive_denied", "Нет прав архивировать задачи")

    changes = await task_service.update_task(session, task, user, payload)
    if changes:
        for recipient in _task_recipients(task, user):
            await hub.send_to_user(
                recipient,
                EVENT_TASK_UPDATED,
                {
                    "task_id": task.id,
                    "changes": [
                        {"field": c["field"], "new": str(c["new"])} for c in changes
                    ],
                    "task": task_out(task).model_dump(),
                },
            )
    return task_out(task)


@router.post(
    "/{task_id}/status",
    response_model=TaskOut,
    summary="Сменить статус",
    description=(
        "Комментарий в запросе сохраняется в задаче — удобно писать, "
        "почему работа принята или возвращена."
    ),
)
async def change_status(
    task_id: str,
    payload: TaskStatusChange,
    session: SessionDep,
    user: User = Depends(require_perm("tasks.view")),
) -> TaskOut:
    task = await task_service.load_task(session, task_id)
    task_service.ensure_can_edit(
        task,
        user,
        can_edit_any=has_perm(user, "tasks.edit_any"),
        can_edit_assigned=has_perm(user, "tasks.edit_assigned"),
    )
    await task_service.change_status(session, task, user, payload.status, payload.comment)

    for recipient in _task_recipients(task, user):
        await hub.send_to_user(
            recipient,
            EVENT_TASK_UPDATED,
            {"task_id": task.id, "status": task.status.value, "task": task_out(task).model_dump()},
        )
    return task_out(task)


@router.post(
    "/{task_id}/assign",
    response_model=TaskOut,
    summary="Назначить исполнителя",
    description="Сотрудника можно оставить без исполнителя — задача попадёт в «не назначенные».",
)
async def assign_task(
    task_id: str,
    payload: TaskAssign,
    session: SessionDep,
    user: User = Depends(require_perm("tasks.assign")),
) -> TaskOut:
    task = await task_service.load_task(session, task_id)
    await task_service.assign_task(session, task, user, payload.assignee_id, payload.due_at)

    if payload.comment and payload.comment.strip():
        note = TaskComment(
            id=new_id(),
            task_id=task.id,
            author_id=user.id,
            body=payload.comment.strip(),
        )
        note.author = user
        session.add(note)
        task.comments_count += 1
        await session.flush()

    for recipient in _task_recipients(task, user):
        await hub.send_to_user(
            recipient,
            EVENT_TASK_UPDATED,
            {"task_id": task.id, "task": task_out(task).model_dump()},
        )
    return task_out(task)


@router.get("/{task_id}/comments", response_model=list[CommentOut], summary="Комментарии")
async def list_comments(
    task_id: str,
    session: SessionDep,
    user: User = Depends(require_perm("tasks.view")),
) -> list[CommentOut]:
    task = await task_service.load_task(session, task_id)
    task_service.ensure_can_view(task, user, has_perm(user, "tasks.view_all"))
    attachments = await _load_attachments(session, task)
    brief = _brief_map(attachments)
    comments = sorted(task.comments, key=lambda c: c.created_at)
    return [comment_out(comment, brief) for comment in comments]


@router.post(
    "/{task_id}/comments",
    response_model=CommentOut,
    status_code=201,
    summary="Добавить комментарий или фотоотчёт",
    description=(
        "Комментарий с вложениями и признаком 'отчёт о выполнении'. "
        "Отчёт переводит задачу на проверку."
    ),
)
async def add_comment(
    task_id: str,
    payload: CommentCreate,
    session: SessionDep,
    user: User = Depends(require_perm("tasks.view")),
) -> CommentOut:
    task = await task_service.load_task(session, task_id)
    task_service.ensure_can_view(task, user, has_perm(user, "tasks.view_all"))
    task_service.ensure_can_edit(
        task,
        user,
        can_edit_any=has_perm(user, "tasks.edit_any"),
        can_edit_assigned=has_perm(user, "tasks.edit_assigned"),
    )

    attachments = await _load_attachments(session, attachment_ids=payload.attachment_ids)
    found_ids = {a.id for a in attachments}
    missing = [a for a in payload.attachment_ids if a not in found_ids]
    if missing:
        raise bad_request("attachment_missing", "Некоторые вложения не найдены")

    # Вложения прикрепляются только те, кто их загрузил: иначе сотрудник
    # присвоил бы чужой фотоотчёт, просто указав его идентификатор.
    foreign = [a for a in attachments if a.uploader_id != user.id]
    if foreign:
        raise forbidden(
            "attachment_not_yours",
            "Прикрепить можно только те файлы, которые загрузили вы",
            {"attachment_ids": [a.id for a in foreign]},
        )

    # Привязываем вложения к задаче, чтобы отчёты собирались в одном месте.
    for attachment in attachments:
        attachment.task_id = task.id

    comment = await task_service.add_comment(
        session,
        task,
        user,
        payload.body,
        payload.attachment_ids,
        payload.is_work_report,
    )

    await hub.send_to_users(
        _task_recipients(task, user),
        EVENT_TASK_UPDATED,
        {"task_id": task.id, "comment_id": comment.id, "task": task_out(task).model_dump()},
    )

    await audit_service.log_action(
        session,
        user=user,
        action="task.comment",
        entity_type="task",
        entity_id=task.id,
        details={"is_work_report": payload.is_work_report, "files": len(payload.attachment_ids)},
    )

    return comment_out(comment, _brief_map(attachments))


@router.get(
    "/{task_id}/history",
    response_model=list[dict],
    summary="История изменений",
)
async def task_history(
    task_id: str,
    session: SessionDep,
    user: User = Depends(require_perm("tasks.view")),
) -> list[dict]:
    task = await task_service.load_task(session, task_id)
    task_service.ensure_can_view(task, user, has_perm(user, "tasks.view_all"))
    from app.serializers import history_out

    return [history_out(entry).model_dump() for entry in sorted(task.history, key=lambda e: e.created_at)]


@router.delete(
    "/{task_id}",
    response_model=OkMessage,
    summary="Удалить задачу",
    description="Задача помечается удалённой. История остаётся в базе.",
)
async def delete_task(
    task_id: str,
    session: SessionDep,
    user: User = Depends(require_perm("tasks.delete")),
) -> OkMessage:
    task = await task_service.load_task(session, task_id)
    if not task.is_archived and task.status != TaskStatus.CANCELLED:
        # Физически удаляем только явно отменённые: так ничего не пропадёт молча.
        raise bad_request(
            "cancel_first",
            "Перед удалением отмените задачу: установите статус «Отменена»",
        )

    task.is_archived = True
    await session.flush()

    await hub.send_to_users(
        _task_recipients(task, user),
        EVENT_TASK_DELETED,
        {"task_id": task.id},
    )
    await audit_service.log_action(
        session, user=user, action="task.delete", entity_type="task", entity_id=task_id
    )
    return OkMessage(detail="Задача удалена")


@router.post("/{task_id}/archive", response_model=TaskOut, summary="Архивировать")
async def archive_task(
    task_id: str,
    session: SessionDep,
    user: User = Depends(require_perm("tasks.delete")),
) -> TaskOut:
    task = await task_service.load_task(session, task_id)
    task.is_archived = True
    await session.flush()
    return task_out(task)


# --- Отчёты по задачам ---


@router.get("/reports/summary", response_model=TaskSummaryOut, summary="Сводка по задачам")
async def tasks_summary(
    session: SessionDep,
    user: User = Depends(require_perm("tasks.reports")),
) -> TaskSummaryOut:
    return TaskSummaryOut(
        **await reports_service.summary(session, user, has_perm(user, "tasks.view_all"))
    )


@router.get("/reports/user-load", response_model=UserLoadOut, summary="Нагрузка сотрудников")
async def user_load(
    session: SessionDep,
    period_from: datetime | None = None,
    period_to: datetime | None = None,
    user: User = Depends(require_perm("tasks.reports")),
) -> UserLoadOut:
    data = await reports_service.user_load(
        session, has_perm(user, "tasks.view_all"), period_from=period_from, period_to=period_to
    )
    from app.serializers import user_brief

    rows = []
    for row in data["rows"]:
        rows.append(
            TaskUserLoadRow(
                user=user_brief(row["user"]),
                assigned=row["assigned"],
                in_progress=row["in_progress"],
                completed=row["completed"],
                overdue=row["overdue"],
                completion_rate=row["completion_rate"],
                comments_count=row.get("comments_count", 0),
                reports_count=row.get("reports_count", 0),
            )
        )
    return UserLoadOut(rows=rows, period_from=data["period_from"], period_to=data["period_to"])


@router.get("/tags/list", response_model=list[dict], summary="Список меток")
async def list_tags(
    session: SessionDep,
    _: User = Depends(require_perm("tasks.view")),
) -> list[dict]:
    from app.models import TaskTag

    tags = (await session.scalars(select(TaskTag).order_by(TaskTag.name))).all()
    return [{"id": tag.id, "name": tag.name, "color": tag.color} for tag in tags]


@router.get("/stats/my", response_model=dict, summary="Моя сводка")
async def my_stats(
    session: SessionDep,
    user: User = Depends(require_perm("tasks.view")),
) -> dict:
    now = utcnow()
    mine = select(func.count(Task.id)).where(Task.assignee_id == user.id)
    active = await session.scalar(
        select(func.count(Task.id)).where(
            Task.assignee_id == user.id,
            Task.status.in_([TaskStatus.NEW, TaskStatus.IN_PROGRESS, TaskStatus.REVIEW]),
        )
    )
    done = await session.scalar(
        select(func.count(Task.id)).where(
            Task.assignee_id == user.id, Task.status == TaskStatus.DONE
        )
    )
    overdue = await session.scalar(
        select(func.count(Task.id)).where(
            Task.assignee_id == user.id,
            Task.due_at.is_not(None),
            Task.due_at < now,
            Task.completed_at.is_(None),
        )
    )
    total = await session.scalar(mine)
    return {
        "total": int(total or 0),
        "active": int(active or 0),
        "done": int(done or 0),
        "overdue": int(overdue or 0),
    }
