"""Сотрудники: карточки, создание, роли, сброс пароля."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app.deps import SessionDep, require_perm
from app.errors import bad_request, conflict, forbidden, not_found
from app.models import Position, Role, User
from app.realtime import EVENT_USER_UPDATED, hub
from app.schemas import (
    ResetPasswordOut,
    ResetPasswordRequest,
    UserCreate,
    UserCreatedOut,
    UserOut,
    UserUpdate,
)
from app.security import new_id
from app.serializers import user_out
from app.services import audit as audit_service
from app.services import auth as auth_service

router = APIRouter(prefix="/users", tags=["users"])


async def _load_user(session: SessionDep, user_id: str) -> User:
    user = await session.scalar(
        select(User)
        .where(User.id == user_id)
        .options(selectinload(User.roles), selectinload(User.position))
    )
    if user is None:
        raise not_found("user_not_found", "Сотрудник не найден") from None
    return user


async def _resolve_roles(session: SessionDep, role_ids: list[str]) -> list[Role]:
    if not role_ids:
        return []
    roles = (await session.scalars(select(Role).where(Role.id.in_(role_ids)))).all()
    if len(roles) != len(set(role_ids)):
        raise bad_request("role_invalid", "Некоторые роли не найдены")
    return list(roles)


@router.get(
    "",
    response_model=list[UserOut],
    summary="Список сотрудников",
    description="Поиск по имени, логину и должности. Фильтры — по активности и роли.",
)
async def list_users(
    session: SessionDep,
    search: str | None = Query(default=None, max_length=100),
    is_active: bool | None = None,
    position_id: str | None = None,
    role_id: str | None = None,
    _: User = Depends(require_perm("users.view")),
) -> list[UserOut]:
    stmt = select(User).options(selectinload(User.roles), selectinload(User.position))

    if is_active is not None:
        stmt = stmt.where(User.is_active.is_(is_active))
    if position_id:
        stmt = stmt.where(User.position_id == position_id)
    if role_id:
        stmt = stmt.where(User.roles.any(Role.id == role_id))
    if search:
        pattern = f"%{search.strip().lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(User.full_name).like(pattern),
                func.lower(User.username).like(pattern),
                func.lower(func.coalesce(User.job_title, "")).like(pattern),
            )
        )

    users = (await session.scalars(stmt.order_by(User.full_name))).all()
    return [user_out(user) for user in users]


@router.post(
    "",
    response_model=UserCreatedOut,
    status_code=201,
    summary="Создать сотрудника",
    description=(
        "Создаёт учётную запись и выдаёт временный пароль. "
        "Пароль показывается один раз — передайте его сотруднику."
    ),
)
async def create_user(
    payload: UserCreate,
    request: Request,
    session: SessionDep,
    actor: User = Depends(require_perm("users.create")),
) -> UserCreatedOut:
    username = auth_service.validate_username(payload.username)
    if await auth_service.get_user_by_username(session, username):
        raise conflict("username_taken", "Такой логин уже занят") from None
    if payload.email:
        existing = await session.scalar(select(User).where(User.email == payload.email))
        if existing:
            raise conflict("email_taken", "Этот email уже используется") from None
    if payload.position_id:
        position = await session.get(Position, payload.position_id)
        if position is None:
            raise bad_request("position_invalid", "Должность не найдена")

    roles = await _resolve_roles(session, payload.role_ids)
    if not roles:
        raise bad_request("role_required", "Укажите хотя бы одну роль")

    temporary = payload.password or auth_service.generate_temporary_password()
    auth_service.validate_password_strength(temporary)

    user = User(
        id=new_id(),
        username=username,
        full_name=payload.full_name.strip(),
        email=payload.email,
        phone=(payload.phone or "").strip() or None,
        job_title=(payload.job_title or "").strip() or None,
        position_id=payload.position_id,
        password_hash=auth_service.hash_password(temporary),
        is_active=True,
        must_change_password=payload.must_change_password,
    )
    user.roles = roles
    session.add(user)

    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise conflict("username_taken", "Такой логин уже занят") from None
    await audit_service.log_action(
        session,
        user=actor,
        action="user.create",
        entity_type="user",
        entity_id=user.id,
        ip_address=audit_service.client_ip(request),
        details={"username": username, "roles": [r.key for r in roles]},
    )

    return UserCreatedOut(
        **user_out(user).model_dump(),
        temporary_password=temporary,
    )


@router.get("/{user_id}", response_model=UserOut, summary="Карточка сотрудника")
async def get_user(
    user_id: str,
    session: SessionDep,
    _: User = Depends(require_perm("users.view")),
) -> UserOut:
    return user_out(await _load_user(session, user_id))


@router.patch(
    "/{user_id}",
    response_model=UserOut,
    summary="Изменить сотрудника",
    description="Перевод на другую должность, смена ролей и блокировка учётной записи.",
)
async def update_user(
    user_id: str,
    payload: UserUpdate,
    request: Request,
    session: SessionDep,
    actor: User = Depends(require_perm("users.edit")),
) -> UserOut:
    user = await _load_user(session, user_id)
    data = payload.model_dump(exclude_unset=True)

    if "is_active" in data and data["is_active"] is False:
        if user.id == actor.id:
            raise forbidden("self_deactivate", "Нельзя деактивировать свою учётную запись") from None
        if user.is_superuser:
            # Последнего администратора оставлять нельзя — иначе сервер
            # останется без управления.
            admins = await session.scalar(
                select(func.count(User.id)).where(
                    User.is_superuser.is_(True),
                    User.is_active.is_(True),
                    User.id != user.id,
                )
            )
            if not admins:
                raise conflict(
                    "last_admin",
                    "Это единственный активный администратор. Назначьте другого.",
                )

    new_position = None
    if "position_id" in data and data["position_id"]:
        new_position = await session.get(Position, data["position_id"])
        if new_position is None:
            raise bad_request("position_invalid", "Должность не найдена")

    new_roles = None
    if "role_ids" in data and data["role_ids"] is not None:
        new_roles = await _resolve_roles(session, data["role_ids"])
        if not new_roles:
            raise bad_request("role_required", "Укажите хотя бы одну роль")

    changed: list[str] = []
    for field, value in data.items():
        if field in ("role_ids", "position_id"):
            continue
        if getattr(user, field) != value:
            setattr(user, field, value)
            changed.append(field)

    if "position_id" in data:
        if user.position_id != data["position_id"]:
            changed.append("position_id")
        user.position_id = data["position_id"]
        # Объект должности нужен сериализатору сразу: в async ленивой
        # загрузки нет, и обращение вызвало бы дополнительный запрос.
        user.position = new_position

    if new_roles is not None:
        if {r.id for r in user.roles} != {r.id for r in new_roles}:
            changed.append("role_ids")
        user.roles = new_roles

    await session.flush()

    await audit_service.log_action(
        session,
        user=actor,
        action="user.update",
        entity_type="user",
        entity_id=user.id,
        ip_address=audit_service.client_ip(request),
        details={"changed": changed},
    )

    # Клиент сотрудника сразу получает обновление профиля и прав.
    if changed:
        refreshed = await _load_user(session, user.id)
        await hub.send_to_user(
            user.id, EVENT_USER_UPDATED, user_out(refreshed).model_dump()
        )

    return user_out(user)


@router.post(
    "/{user_id}/reset-password",
    response_model=ResetPasswordOut,
    summary="Сбросить пароль сотрудника",
    description=(
        "Выдаёт временный пароль. Если не передан свой — генерируется "
        "автоматически и показывается один раз."
    ),
)
async def reset_password(
    user_id: str,
    request: Request,
    payload: ResetPasswordRequest,
    session: SessionDep,
    actor: User = Depends(require_perm("users.reset_password")),
) -> ResetPasswordOut:
    user = await _load_user(session, user_id)

    temporary = payload.new_password or auth_service.generate_temporary_password()
    await auth_service.set_password(
        session, user, temporary, must_change=payload.must_change_password
    )

    # Старые сессии гасим: пароль сменился — значит прежние входы недействительны.
    revoked = await auth_service.revoke_all_sessions(session, user.id)

    await audit_service.log_action(
        session,
        user=actor,
        action="user.reset_password",
        entity_type="user",
        entity_id=user.id,
        ip_address=audit_service.client_ip(request),
        details={"revoked_sessions": revoked},
    )

    return ResetPasswordOut(
        user_id=user.id,
        temporary_password=temporary,
        must_change_password=payload.must_change_password,
    )


@router.post(
    "/{user_id}/deactivate",
    response_model=UserOut,
    summary="Деактивировать сотрудника",
    description=(
        "Учётная запись отключается, но история его задач и сообщений сохраняется. "
        "Мягкое удаление: ничего не пропадает из отчётов."
    ),
)
async def deactivate_user(
    user_id: str,
    request: Request,
    session: SessionDep,
    actor: User = Depends(require_perm("users.delete")),
) -> UserOut:
    user = await _load_user(session, user_id)
    if user.id == actor.id:
        raise forbidden("self_deactivate", "Нельзя деактивировать себя") from None
    if user.is_superuser and user.is_active:
        admins = await session.scalar(
            select(func.count(User.id)).where(
                User.is_superuser.is_(True),
                User.is_active.is_(True),
                User.id != user.id,
            )
        )
        if not admins:
            raise conflict("last_admin", "Это единственный активный администратор") from None
    user.is_active = False
    await auth_service.revoke_all_sessions(session, user.id)
    await session.flush()

    await audit_service.log_action(
        session,
        user=actor,
        action="user.deactivate",
        entity_type="user",
        entity_id=user.id,
        ip_address=audit_service.client_ip(request),
    )
    return user_out(user)


@router.post(
    "/{user_id}/activate",
    response_model=UserOut,
    summary="Вернуть сотрудника",
)
async def activate_user(
    user_id: str,
    session: SessionDep,
    actor: User = Depends(require_perm("users.edit")),
) -> UserOut:
    user = await _load_user(session, user_id)
    user.is_active = True
    await session.flush()

    await audit_service.log_action(
        session, user=actor, action="user.activate", entity_type="user", entity_id=user_id
    )
    return user_out(user)


@router.get(
    "/stats/overview",
    summary="Сводка по отделу",
    description="Количество активных и заблокированных сотрудников — для главного экрана.",
)
async def stats_overview(
    session: SessionDep,
    _: User = Depends(require_perm("users.view")),
) -> dict:
    active = await session.scalar(
        select(func.count(User.id)).where(User.is_active.is_(True))
    )
    inactive = await session.scalar(
        select(func.count(User.id)).where(User.is_active.is_(False))
    )
    unassigned = await session.scalar(
        select(func.count(User.id)).where(
            User.is_active.is_(True), User.position_id.is_(None)
        )
    )
    return {
        "active": int(active or 0),
        "inactive": int(inactive or 0),
        "without_position": int(unassigned or 0),
        "total": int(active or 0) + int(inactive or 0),
    }
