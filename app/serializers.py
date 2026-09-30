"""Преобразование моделей в схемы API. Держим в одном месте."""

from __future__ import annotations

from app.models import (
    Attachment,
    Message,
    Position,
    Role,
    Task,
    TaskComment,
    TaskHistory,
    TaskPriority,
    TaskStatus,
    User,
)
from app.schemas.auth import PositionBrief, RoleBrief, UserBrief, UserMe
from app.schemas.chat import AttachmentBrief, MessageOut, TranscriptOut
from app.schemas.task import (
    CommentOut,
    HistoryOut,
    TagOut,
    TaskDetailOut,
    TaskOut,
)


def avatar_url(user: User | None) -> str | None:
    if user is None or not user.avatar_file_id:
        return None
    return f"/api/v1/files/{user.avatar_file_id}/download"


def user_brief(user: User | None) -> UserBrief | None:
    if user is None:
        return None
    position = (
        PositionBrief(id=user.position.id, title=user.position.title)
        if user.position
        else None
    )
    return UserBrief(
        id=user.id,
        username=user.username,
        full_name=user.full_name,
        job_title=user.job_title,
        avatar_url=avatar_url(user),
        position=position,
        is_active=user.is_active,
    )


def role_brief(role: Role) -> RoleBrief:
    return RoleBrief(id=role.id, key=role.key, title=role.title, permissions=list(role.permissions or []))


def attachment_brief(attachment: Attachment | None) -> AttachmentBrief | None:
    if attachment is None or attachment.is_deleted:
        return None
    meta = attachment.meta or {}
    return AttachmentBrief(
        id=attachment.id,
        kind=attachment.kind.value,
        name=attachment.original_name,
        mime_type=attachment.mime_type,
        size_bytes=attachment.size_bytes,
        url=f"/api/v1/files/{attachment.id}/download",
        preview_url=(
            f"/api/v1/files/{attachment.id}/preview" if attachment.preview_name else None
        ),
        width=meta.get("width"),
        height=meta.get("height"),
        duration_sec=meta.get("duration_sec"),
    )


def message_out(message: Message) -> MessageOut:
    from app.schemas.common import to_utc

    transcript = None
    if message.transcript is not None:
        transcript = TranscriptOut.model_validate(message.transcript)

    return MessageOut(
        id=message.id,
        chat_id=message.chat_id,
        seq=message.seq,
        kind=message.kind,
        body=message.body,
        sender=user_brief(message.sender),
        attachment=attachment_brief(message.attachment),
        transcript=transcript,
        transcript_text=message.transcript_text,
        voice_duration_sec=message.voice_duration_sec,
        status=message.status.value,
        reply_to_id=message.reply_to_id,
        edited_at=to_utc(message.edited_at),
        deleted_at=to_utc(message.deleted_at),
        created_at=to_utc(message.created_at) or message.created_at,
    )


def comment_out(comment: TaskComment, attachments_by_id: dict[str, AttachmentBrief]) -> CommentOut:
    return CommentOut(
        id=comment.id,
        task_id=comment.task_id,
        body=comment.body,
        author=user_brief(comment.author),
        attachments=[attachments_by_id[a] for a in (comment.attachment_ids or []) if a in attachments_by_id],
        is_work_report=comment.is_work_report,
        created_at=comment.created_at,
        updated_at=comment.updated_at,
    )


def history_out(entry: TaskHistory) -> HistoryOut:
    return HistoryOut(
        id=entry.id,
        field=entry.field,
        old_value=entry.old_value,
        new_value=entry.new_value,
        created_at=entry.created_at,
        user=user_brief(entry.user),
    )


def task_out(task: Task) -> TaskOut:
    return TaskOut(
        id=task.id,
        title=task.title,
        description=task.description,
        status=task.status,
        status_title=TaskStatus(task.status).title,
        priority=task.priority,
        priority_title=TaskPriority(task.priority).title,
        author=user_brief(task.author),
        assignee=user_brief(task.assignee),
        due_at=task.due_at,
        completed_at=task.completed_at,
        estimated_hours=task.estimated_hours,
        tags=[TagOut(id=t.id, name=t.name, color=t.color) for t in task.tags],
        is_overdue=task.is_overdue,
        comments_count=task.comments_count,
        attachments_count=task.attachments_count,
        is_archived=task.is_archived,
        created_at=task.created_at,
        updated_at=task.updated_at,
    )


def task_detail_out(task: Task, attachments_by_id: dict[str, AttachmentBrief]) -> TaskDetailOut:
    base = task_out(task)
    return TaskDetailOut(
        **base.model_dump(),
        comments=[comment_out(c, attachments_by_id) for c in task.comments],
        history=[history_out(h) for h in sorted(task.history, key=lambda e: e.created_at)],
        attachments=list(attachments_by_id.values()),
    )


def position_out(position: Position, users_count: int = 0) -> PositionOut:  # noqa: F821
    from app.schemas.user import PositionOut

    return PositionOut(
        id=position.id,
        title=position.title,
        description=position.description,
        sort_order=position.sort_order,
        is_active=position.is_active,
        users_count=users_count,
        created_at=position.created_at,
    )


def role_out(role: Role, users_count: int = 0) -> RoleOut:  # noqa: F821
    from app.schemas.user import RoleOut

    return RoleOut(
        id=role.id,
        key=role.key,
        title=role.title,
        description=role.description,
        permissions=list(role.permissions or []),
        is_system=role.is_system,
        users_count=users_count,
        created_at=role.created_at,
    )


def user_out(user: User) -> UserOut:  # noqa: F821
    from app.schemas.user import UserOut

    position = (
        PositionBrief(id=user.position.id, title=user.position.title)
        if user.position
        else None
    )
    return UserOut(
        id=user.id,
        username=user.username,
        full_name=user.full_name,
        job_title=user.job_title,
        avatar_url=avatar_url(user),
        position=position,
        is_active=user.is_active,
        email=user.email,
        phone=user.phone,
        roles=[role_brief(r) for r in user.roles],
        permissions=sorted(user.permissions),
        is_superuser=user.is_superuser,
        must_change_password=user.must_change_password,
        last_login_at=user.last_login_at,
        created_at=user.created_at,
    )


def user_me(user: User) -> UserMe:
    position = (
        PositionBrief(id=user.position.id, title=user.position.title)
        if user.position
        else None
    )
    return UserMe(
        id=user.id,
        username=user.username,
        full_name=user.full_name,
        email=user.email,
        phone=user.phone,
        job_title=user.job_title,
        avatar_url=avatar_url(user),
        position=position,
        roles=[role_brief(r) for r in user.roles],
        permissions=sorted(user.permissions),
        is_active=user.is_active,
        is_superuser=user.is_superuser,
        must_change_password=user.must_change_password,
        last_login_at=user.last_login_at,
        created_at=user.created_at,
    )
