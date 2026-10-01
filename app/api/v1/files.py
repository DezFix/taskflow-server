"""Загрузка и выдача файлов: фотоотчёты, документы, аудио."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select

from app.config import get_settings
from app.deps import CurrentUser, SessionDep, has_perm, require_perm
from app.errors import forbidden, not_found
from app.models import Attachment, Message, User
from app.schemas import FileOut, OkMessage
from app.services import audit as audit_service
from app.services import files as files_service

router = APIRouter(prefix="/files", tags=["files"])

#: Приватные файлы не кэшируем.
#:
#: Раньше здесь стояло `private, max-age=86400`, что перекрывало общий
#: no-store для API: содержимое оставалось в дисковом кэше браузера на
#: сутки, а адрес вложения — это только идентификатор без привязки к
#: пользователю. На общем компьютере следующий сотрудник того же профиля
#: мог получить файл из кэша без проверки прав.
DOWNLOAD_HEADERS = {
    "Cache-Control": "private, no-store, max-age=0",
    "X-Content-Type-Options": "nosniff",
}


async def _load_attachment(session: SessionDep, file_id: str) -> Attachment:
    attachment = await session.get(Attachment, file_id)
    if attachment is None or attachment.is_deleted:
        raise not_found("file_not_found", "Файл не найден")
    return attachment


@router.post(
    "",
    response_model=FileOut,
    status_code=201,
    summary="Загрузить файл",
    description=(
        "Фотоотчёт, документ или аудио. Расширение проверяется по белому списку, "
        "размер ограничен настройкой MAX_UPLOAD_MB. "
        "Параметр task_id привязывает файл к задаче."
    ),
)
async def upload_file(
    session: SessionDep,
    file: Annotated[UploadFile, File(description="Файл для загрузки")],
    task_id: Annotated[
        str | None, Form(description="Идентификатор задачи для привязки файла")
    ] = None,
    user: User = Depends(require_perm("files.upload")),
) -> FileOut:
    # Читаем с ограничением по ходу: лимит проверяется до того, как тело
    # запроса целиком окажется в памяти.
    data = await files_service.read_upload(file)

    attachment = await files_service.save_upload(
        data,
        file.filename or "file",
        user,
        mime_type=file.content_type,
        task_id=task_id,
    )
    await file.close()
    session.add(attachment)
    await session.flush()

    await audit_service.log_action(
        session,
        user=user,
        action="file.upload",
        entity_type="attachment",
        entity_id=attachment.id,
        details={"name": attachment.original_name, "size": attachment.size_bytes},
    )
    return _file_out(attachment)


def _file_out(attachment: Attachment) -> FileOut:
    meta = attachment.meta or {}
    from app.schemas.common import to_utc

    return FileOut(
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
        checksum=attachment.checksum,
        task_id=attachment.task_id,
        uploader_id=attachment.uploader_id,
        created_at=to_utc(attachment.created_at) or attachment.created_at,
    )


@router.get(
    "/{file_id}",
    response_model=FileOut,
    summary="Метаданные файла",
    description="Свои файлы доступны всегда; чужие — с правом files.view_all.",
)
async def get_file_info(
    file_id: str,
    session: SessionDep,
    user: CurrentUser,
) -> FileOut:
    attachment = await _load_attachment(session, file_id)
    _ensure_can_read(attachment, user, has_perm(user, "files.view_all"))
    return _file_out(attachment)


@router.get(
    "/{file_id}/download",
    summary="Скачать файл",
    description=(
        "Право files.view_all есть не у всех. Файлы, загруженные "
        "самим сотрудником, доступны ему всегда."
    ),
)
async def download_file(
    file_id: str,
    session: SessionDep,
    user: CurrentUser,
):
    attachment = await _load_attachment(session, file_id)
    _ensure_can_read(attachment, user, has_perm(user, "files.view_all"))

    path = files_service.file_path(attachment)
    return FileResponse(
        path,
        # Тип отдаём строго по расширению, а не из базы: даже если в базу
        # попало подделанное значение, браузер не выполнит файл как скрипт.
        media_type=files_service.detect_mime_type(attachment.original_name),
        filename=attachment.original_name,
        headers=DOWNLOAD_HEADERS,
    )


@router.get(
    "/{file_id}/preview",
    summary="Превью изображения",
    description="Отдаёт сжатое превью, а если его нет — оригинал.",
)
async def preview_file(
    file_id: str,
    session: SessionDep,
    user: CurrentUser,
):
    attachment = await _load_attachment(session, file_id)
    _ensure_can_read(attachment, user, has_perm(user, "files.view_all"))

    path = files_service.preview_path(attachment)
    if path is None:
        # Превью нет — отдаём оригинал, клиент справится.
        path = files_service.file_path(attachment)
        return FileResponse(
            path,
            media_type=files_service.detect_mime_type(attachment.original_name),
            headers=DOWNLOAD_HEADERS,
        )

    return FileResponse(path, media_type="image/jpeg", headers=DOWNLOAD_HEADERS)


def _ensure_can_read(attachment: Attachment, user, can_view_all: bool) -> None:
    """Свои файлы сотрудник видит всегда, чужие — только с правом files.view_all."""
    if can_view_all or attachment.uploader_id == user.id:
        return
    raise forbidden("file_access_denied", "Нет доступа к этому файлу")


@router.get(
    "/attachments/for-task/{task_id}",
    response_model=list[FileOut],
    summary="Файлы задачи",
    description="Все вложения задачи: фотоотчёты и документы.",
)
async def task_files(
    task_id: str,
    session: SessionDep,
    user: User = Depends(require_perm("tasks.view_all")),
) -> list[FileOut]:
    rows = (
        await session.scalars(
            select(Attachment)
            .where(Attachment.task_id == task_id, Attachment.is_deleted.is_(False))
            .order_by(Attachment.created_at)
        )
    ).all()
    return [_file_out(row) for row in rows]


@router.delete(
    "/{file_id}",
    response_model=OkMessage,
    summary="Удалить файл",
    description="Свои файлы сотрудник удаляет сам, чужие — только с правом files.view_all.",
)
async def delete_file(
    file_id: str,
    session: SessionDep,
    user: CurrentUser,
) -> OkMessage:
    attachment = await _load_attachment(session, file_id)
    if attachment.uploader_id != user.id and not has_perm(user, "files.view_all"):
        raise forbidden("file_delete_denied", "Можно удалять только свои файлы")

    # Файл может быть в сообщении чата — помечаем удалённым, но не стираем,
    # чтобы история переписки осталась целой.
    used_in_message = await session.scalar(
        select(Message.id).where(Message.attachment_id == file_id).limit(1)
    )
    attachment.is_deleted = True
    # Пока не стираем. Коммит здесь ещё не прошёл, и при откате файл
    # остался бы удалённым с диска, а в базе — живым: фотоотчёт пропадал бы
    # безвозвратно, и ссылка на него вела в file_missing.
    pending_unlink = None if used_in_message else attachment

    await audit_service.log_action(
        session,
        user=user,
        action="file.delete",
        entity_type="attachment",
        entity_id=file_id,
    )
    # Сначала фиксируем признак удаления, потом убираем файл с диска.
    await session.commit()

    if pending_unlink is not None:
        files_service.delete_files(pending_unlink)
    return OkMessage(detail="Файл удалён")


@router.get("/storage/usage", summary="Занятое место")
async def storage_usage(
    _: User = Depends(require_perm("settings.view")),
) -> dict:
    usage = files_service.storage_usage()
    return {
        **usage,
        "human": files_service.human_size(usage["bytes"]),
        "max_upload_mb": get_settings().max_upload_mb,
    }
