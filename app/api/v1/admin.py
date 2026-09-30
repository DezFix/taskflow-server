"""Администрирование: журнал действий, резервные копии, выгрузка."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from fastapi.responses import PlainTextResponse
from sqlalchemy import select

from app.config import get_settings
from app.deps import SessionDep, has_perm, require_perm
from app.models import AuditLog, User
from app.schemas import OkMessage
from app.schemas.common import to_utc
from app.services import backup as backup_service
from app.services import reports as reports_service

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get(
    "/audit",
    summary="Журнал действий",
    description=(
        "Кто, что и когда сделал. Записи добавляются на создание и изменение "
        "сотрудников, ролей, задач, файлов и настроек."
    ),
)
async def audit_log(
    session: SessionDep,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    action: str | None = None,
    user_id: str | None = None,
    entity_type: str | None = None,
    _: User = Depends(require_perm("settings.audit_log")),
) -> dict:
    entries, total = await _list_audit(
        session, limit=limit, offset=offset, action=action, user_id=user_id, entity_type=entity_type
    )
    return {
        "items": [_audit_item(entry) for entry in entries],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


async def _list_audit(
    session: SessionDep,
    *,
    limit: int,
    offset: int,
    action: str | None,
    user_id: str | None,
    entity_type: str | None,
) -> tuple[list[AuditLog], int]:
    from sqlalchemy import func

    filters = []
    if action:
        filters.append(AuditLog.action == action)
    if user_id:
        filters.append(AuditLog.user_id == user_id)
    if entity_type:
        filters.append(AuditLog.entity_type == entity_type)

    total = await session.scalar(
        select(func.count()).select_from(AuditLog).where(*filters)
    )
    rows = (
        await session.scalars(
            select(AuditLog)
            .where(*filters)
            .order_by(AuditLog.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return list(rows), int(total or 0)


def _audit_item(entry: AuditLog) -> dict:
    return {
        "id": entry.id,
        "user_id": entry.user_id,
        "username": entry.username,
        "action": entry.action,
        "entity_type": entry.entity_type,
        "entity_id": entry.entity_id,
        "ip_address": entry.ip_address,
        "details": entry.details or {},
        "created_at": (to_utc(entry.created_at) or entry.created_at).isoformat().replace(
            "+00:00", "Z"
        ),
    }


@router.get(
    "/audit/actions",
    summary="Список действий в журнале",
    description="Для фильтра в интерфейсе.",
)
async def audit_actions(
    session: SessionDep,
    _: User = Depends(require_perm("settings.audit_log")),
) -> list[str]:

    rows = await session.scalars(select(AuditLog.action).distinct().order_by(AuditLog.action))
    return list(rows)


@router.post(
    "/backup",
    summary="Создать резервную копию",
    description=(
        "Архив включает базу данных и все загруженные файлы. "
        "Для SQLite копия делается через WAL-чекпоинт, поэтому неповреждённая."
    ),
)
async def create_backup(
    include_files: bool = Query(default=True),
    _: User = Depends(require_perm("settings.backup")),
) -> dict:
    return await backup_service.create_backup(include_files=include_files)


@router.get("/backups", summary="Список резервных копий")
async def list_backups(
    _: User = Depends(require_perm("settings.backup")),
) -> list[dict]:
    return backup_service.list_backups()


@router.post("/backups/{backup_id}/restore", summary="Восстановить из копии")
async def restore_backup(
    backup_id: str,
    _: User = Depends(require_perm("settings.backup")),
) -> dict:
    return await backup_service.restore_backup(backup_id)


@router.delete("/backups/{backup_id}", response_model=OkMessage, summary="Удалить копию")
async def delete_backup(
    backup_id: str,
    _: User = Depends(require_perm("settings.backup")),
) -> OkMessage:
    backup_service.delete_backup(backup_id)
    return OkMessage(detail="Резервная копия удалена")


@router.get(
    "/reports/tasks.csv",
    summary="Выгрузка задач в CSV",
    description=(
        "Разделитель — точка с запятой и UTF-8 с BOM: русский Excel "
        "открывает файл без мастера импорта."
    ),
    response_class=PlainTextResponse,
)
async def export_tasks(
    session: SessionDep,
    period_from: datetime | None = None,
    period_to: datetime | None = None,
    user: User = Depends(require_perm("tasks.reports")),
) -> PlainTextResponse:
    content = await reports_service.export_csv(
        session,
        can_see_all=has_perm(user, "tasks.view_all"),
        user_id=user.id,
        period_from=period_from,
        period_to=period_to,
    )
    return PlainTextResponse(
        "\ufeff" + content,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="taskflow-tasks.csv"',
        },
    )


@router.get("/system/info", summary="Сведения о системе")
async def system_info(
    _: User = Depends(require_perm("settings.view")),
) -> dict:
    import platform
    import sys
    from datetime import datetime as _dt

    import sqlalchemy

    settings = get_settings()
    # Человекочитаемое имя СУБД без драйвера: "sqlite", "postgresql", "mysql".
    scheme = settings.database_url.split("://")[0]
    return {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "sqlalchemy": sqlalchemy.__version__,
        "database": scheme.split("+")[0],
        "database_driver": scheme,
        "database_is_sqlite": settings.is_sqlite,
        "server_time": _dt.now().isoformat(timespec="seconds"),
        "data_dir": str(settings.path("./data")),
        "voice_enabled": settings.voice_enabled,
        "voice_model": settings.voice_model,
        "max_upload_mb": settings.max_upload_mb,
    }


@router.post(
    "/maintenance/cleanup",
    summary="Очистить временные файлы",
    description="Удаляет .part-файлы, оставшиеся после прерванной загрузки.",
)
async def cleanup(
    _: User = Depends(require_perm("settings.edit")),
) -> dict:
    from app.services import files as files_service

    return {"removed": files_service.cleanup_orphans()}
