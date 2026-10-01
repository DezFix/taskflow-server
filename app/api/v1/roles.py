"""Роли и матрица прав."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func, select

from app.deps import SessionDep, ensure_can_grant, require_perm
from app.errors import conflict, forbidden, not_found
from app.models import Role, User, user_roles
from app.permissions import SYSTEM_ROLE_KEYS
from app.schemas import OkMessage, RoleCreate, RoleOut, RoleUpdate
from app.security import new_id
from app.serializers import role_out
from app.services import audit as audit_service

router = APIRouter(prefix="/roles", tags=["roles"])


def _slugify_key(title: str) -> str:
    from slugify import slugify

    return slugify(title, allow_unicode=False) or f"role-{new_id()[:6]}"


async def _users_count(session: SessionDep, role_id: str) -> int:
    count = await session.scalar(
        select(func.count()).select_from(user_roles).where(user_roles.c.role_id == role_id)
    )
    return int(count or 0)


@router.get(
    "",
    response_model=list[RoleOut],
    summary="Список ролей",
)
async def list_roles(
    session: SessionDep,
    _: User = Depends(require_perm("roles.view")),
) -> list[RoleOut]:
    roles = (await session.scalars(select(Role).order_by(Role.is_system.desc(), Role.title))).all()
    return [role_out(role, await _users_count(session, role.id)) for role in roles]


@router.post("", response_model=RoleOut, status_code=201, summary="Создать роль")
async def create_role(
    payload: RoleCreate,
    session: SessionDep,
    user: User = Depends(require_perm("roles.create")),
) -> RoleOut:
    key = (payload.key or _slugify_key(payload.title)).strip()
    if key in SYSTEM_ROLE_KEYS:
        raise conflict(
            "system_role_key",
            "Этот ключ зарезервирован системной ролью",
        )

    existing = await session.scalar(select(Role).where(Role.key == key))
    if existing is not None:
        raise conflict("role_exists", "Роль с таким ключом уже есть")

    # Нельзя создать роль сильнее себя: иначе `roles.create` был бы
    # второй дорогой к эскалации наряду с правкой существующей роли.
    ensure_can_grant(user, set(payload.permissions))

    role = Role(
        id=new_id(),
        key=key,
        title=payload.title.strip(),
        description=(payload.description or "").strip() or None,
        permissions=list(payload.permissions),
        is_system=False,
    )
    session.add(role)
    await session.flush()

    await audit_service.log_action(
        session,
        user=user,
        action="role.create",
        entity_type="role",
        entity_id=role.id,
        details={"title": role.title, "permissions": len(role.permissions)},
    )
    return role_out(role, 0)


@router.get("/{role_id}", response_model=RoleOut, summary="Карточка роли")
async def get_role(
    role_id: str,
    session: SessionDep,
    _: User = Depends(require_perm("roles.view")),
) -> RoleOut:
    role = await session.get(Role, role_id)
    if role is None:
        raise not_found("role_not_found", "Роль не найдена")
    return role_out(role, await _users_count(session, role_id))


@router.patch(
    "/{role_id}",
    response_model=RoleOut,
    summary="Изменить роль",
    description=(
        "Системную роль можно переименовать и менять права у неё, "
        "но нельзя удалить."
    ),
)
async def update_role(
    role_id: str,
    payload: RoleUpdate,
    session: SessionDep,
    user: User = Depends(require_perm("roles.edit")),
) -> RoleOut:
    role = await session.get(Role, role_id)
    if role is None:
        raise not_found("role_not_found", "Роль не найдена")

    data = payload.model_dump(exclude_unset=True)

    # Проверяем итоговый набор прав, а не только присланный: иначе
    # можно было бы оставить в роли право, которого у актора нет,
    # просто не передавая его в теле запроса.
    resulting = set(role.permissions or [])
    if "permissions" in data:
        resulting = set(data["permissions"] or [])
    ensure_can_grant(user, resulting)

    for field, value in data.items():
        setattr(role, field, value)
    await session.flush()

    await audit_service.log_action(
        session,
        user=user,
        action="role.update",
        entity_type="role",
        entity_id=role.id,
        details={"fields": list(data.keys())},
    )
    return role_out(role, await _users_count(session, role_id))


@router.delete("/{role_id}", response_model=OkMessage, summary="Удалить роль")
async def delete_role(
    role_id: str,
    session: SessionDep,
    user: User = Depends(require_perm("roles.delete")),
) -> OkMessage:
    role = await session.get(Role, role_id)
    if role is None:
        raise not_found("role_not_found", "Роль не найдена")

    if role.is_system:
        raise forbidden("system_role", "Системную роль удалить нельзя")

    members = await _users_count(session, role_id)
    if members:
        raise conflict(
            "role_in_use",
            f"Роль назначена сотрудникам: {members}. Сначала снимите её с них.",
        )

    await session.delete(role)
    await session.flush()

    await audit_service.log_action(
        session, user=user, action="role.delete", entity_type="role", entity_id=role_id
    )
    return OkMessage(detail="Роль удалена")


@router.get(
    "/permissions/matrix",
    summary="Матрица прав",
    description="Какие права выданы каждой ролью — для экрана настройки ролей.",
)
async def permissions_matrix(
    session: SessionDep,
    _: User = Depends(require_perm("roles.view")),
) -> dict:
    from app.permissions import ALL_PERMISSIONS

    roles = (await session.scalars(select(Role).order_by(Role.title))).all()
    return {
        "total": len(ALL_PERMISSIONS),
        "groups": _group_permissions(),
        "roles": [
            {
                "id": role.id,
                "key": role.key,
                "title": role.title,
                "is_system": role.is_system,
                "permissions": list(role.permissions or []),
            }
            for role in roles
        ],
    }


def _group_permissions() -> list[dict]:
    from app.permissions import ALL_PERMISSIONS

    groups: dict[str, list[dict]] = {}
    for permission in ALL_PERMISSIONS:
        groups.setdefault(permission.group, []).append(
            {
                "key": permission.key,
                "title": permission.title,
                "description": permission.description,
            }
        )
    return [{"title": title, "items": items} for title, items in groups.items()]
