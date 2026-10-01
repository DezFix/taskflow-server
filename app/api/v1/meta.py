"""Метаданные сервера: версия, состояние установки, каталог прав."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func, select

from app.config import get_settings
from app.deps import SessionDep, has_perm, require_perm
from app.models import Role, User
from app.permissions import ALL_PERMISSIONS
from app.schemas.auth import ServerInfo
from app.schemas.user import PermissionOut

router = APIRouter(tags=["meta"])

SERVER_VERSION = "1.0.0"
APP_NAME = "TaskFlow"


@router.get(
    "/meta/info",
    response_model=ServerInfo,
    summary="Информация о сервере",
    description=(
        "Клиент вызывает этот эндпоинт при вводе адреса, чтобы убедиться, "
        "что по этому адресу открыт TaskFlow, и узнать, требуется ли "
        "первоначальная настройка."
    ),
)
async def server_info(session: SessionDep) -> ServerInfo:
    settings = get_settings()
    # Установленная система — если в базе есть хотя бы один пользователь.
    users_count = await session.scalar(select(func.count(User.id)))
    return ServerInfo(
        name=APP_NAME,
        version=SERVER_VERSION,
        requires_setup=not users_count,
        voice_enabled=settings.voice_enabled,
        voice_model=settings.voice_model,
    )


@router.get(
    "/meta/permissions",
    response_model=list[PermissionOut],
    summary="Каталог прав",
    description="Полный список прав с названиями и группами. Клиент строит по нему матрицу.",
)
async def list_permissions() -> list[PermissionOut]:
    return [
        PermissionOut(key=p.key, title=p.title, group=p.group, description=p.description)
        for p in ALL_PERMISSIONS
    ]


@router.get(
    "/meta/health",
    summary="Проверка доступности",
    description="Простой ответ без обращения к базе. Пригоден для проверки связи.",
)
async def health() -> dict[str, str]:
    return {"status": "ok", "service": APP_NAME, "version": SERVER_VERSION}


@router.get(
    "/meta/system-roles",
    response_model=list[str],
    summary="Системные роли",
)
async def system_roles(session: SessionDep) -> list[str]:
    from app.permissions import SYSTEM_ROLE_KEYS

    rows = await session.scalars(
        select(Role.key).where(Role.is_system.is_(True), Role.key.in_(SYSTEM_ROLE_KEYS))
    )
    found = set(rows)
    # Роли создаются при первом старте; если БД пуста, показываем ожидаемые.
    return sorted(found or set(SYSTEM_ROLE_KEYS))


@router.get(
    "/meta/permission-matrix",
    summary="Матрица прав",
    description=(
        "Какие права выданы каждой ролью. Требуется право roles.view: "
        "матрица показывает распределение прав по отделу."
    ),
)
async def permission_matrix(
    session: SessionDep,
    user: User = Depends(require_perm("roles.view")),
) -> dict:
    """Сводная таблица: какие права есть у какой роли. Для настроек в UI."""
    from app.permissions import PERMISSION_TITLES

    rows = (await session.scalars(select(Role).order_by(Role.is_system.desc(), Role.title))).all()
    matrix = []
    for role in rows:
        granted = set(role.permissions or [])
        matrix.append(
            {
                "id": role.id,
                "key": role.key,
                "title": role.title,
                "is_system": role.is_system,
                "permissions": [
                    # Ключ может быть удалён из permissions.py, пока в базе
                    # ролей он ещё остался: прямой доступ к словарю давал
                    # 500 на /meta/permission-matrix. get с заглушкой
                    # показывает такое право отдельной меткой.
                    {
                        "key": key,
                        "title": PERMISSION_TITLES.get(key, "Неизвестное право"),
                        "unknown": key not in PERMISSION_TITLES,
                        "granted": key in granted,
                    }
                    for key in sorted(granted)
                ],
                "granted_count": len(granted),
            }
        )
    return {"roles": matrix, "total_permissions": len(ALL_PERMISSIONS)}


def user_can_manage(user: User) -> bool:
    return has_perm(user, "roles.view")
