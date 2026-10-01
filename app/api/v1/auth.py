"""Аутентификация: настройка, вход, токены, профиль."""

from __future__ import annotations

from fastapi import APIRouter, Request
from sqlalchemy import func, select

from app.config import get_settings
from app.deps import CurrentUser, SessionDep
from app.errors import AppError, conflict, not_found
from app.models import Role, Setting, User
from app.permissions import SYSTEM_ROLES
from app.realtime import EVENT_SESSION_REVOKED, hub
from app.schemas import (
    ChangePasswordRequest,
    LoginRequest,
    OkMessage,
    RefreshRequest,
    SessionInfo,
    SetupRequest,
    TokenPair,
    UpdateProfileRequest,
    UserMe,
)
from app.security import hash_password, new_id
from app.serializers import user_me
from app.services import audit as audit_service
from app.services import auth as auth_service
from app.services import rate_limit
from app.services.seed import create_system_roles

router = APIRouter(prefix="/auth", tags=["auth"])

SETUP_DONE_KEY = "setup_completed"


async def is_setup_required(session: SessionDep) -> bool:
    """Система считается настроенной, если есть пользователь."""
    count = await session.scalar(select(func.count(User.id)))
    return not count


@router.post(
    "/setup",
    response_model=TokenPair,
    summary="Первоначальная настройка",
    description=(
        "Создаёт администратора и системные роли. Доступен только пока "
        "в системе нет ни одного пользователя."
    ),
)
async def setup(payload: SetupRequest, request: Request, session: SessionDep) -> TokenPair:
    if not await is_setup_required(session):
        raise conflict("already_setup", "Система уже настроена")

    username = auth_service.validate_username(payload.username)
    auth_service.validate_password_strength(payload.password)

    await create_system_roles(session)

    admin_role = await session.scalar(select(Role).where(Role.key == "Администратор"))
    admin = User(
        id=new_id(),
        username=username,
        full_name=payload.full_name.strip(),
        email=payload.email,
        password_hash=hash_password(payload.password),
        is_superuser=True,
        is_active=True,
        must_change_password=False,
    )
    if admin_role is not None:
        admin.roles.append(admin_role)
    session.add(admin)
    await session.flush()

    if payload.organization_name:
        session.add(
            Setting(
                id=new_id(),
                key="organization_name",
                value={"name": payload.organization_name.strip()},
            )
        )
    session.add(Setting(id=new_id(), key=SETUP_DONE_KEY, value={"at": None}))

    await audit_service.log_action(
        session,
        user=admin,
        action="system.setup",
        entity_type="user",
        entity_id=admin.id,
        ip_address=audit_service.client_ip(request),
        details={"username": username},
    )

    return await auth_service.create_session(
        session,
        admin,
        device_name=(payload.organization_name or "Установка"),
        user_agent=request.headers.get("user-agent"),
        ip_address=audit_service.client_ip(request),
    )


@router.post("/login", response_model=TokenPair, summary="Вход по логину или email")
async def login(payload: LoginRequest, request: Request, session: SessionDep) -> TokenPair:
    # Ограничение частоты до самой проверки пароля: блокировка учётной
    # записи защищает от подбора, но не ограничивает скорость попыток.
    settings = get_settings()
    rate_limit.check_identifier(
        payload.identifier,
        limit=settings.max_failed_logins * 3,
        window_minutes=settings.lockout_minutes,
    )
    ip_address = audit_service.client_ip(request)
    rate_limit.check_ip(
        ip_address,
        limit=settings.login_ip_attempts_per_minute,
        window_minutes=1,
    )
    user = await auth_service.authenticate(session, payload.identifier, payload.password)
    rate_limit.forget(payload.identifier, ip_address)
    await audit_service.log_action(
        session,
        user=user,
        action="auth.login",
        entity_type="user",
        entity_id=user.id,
        details={"username": user.username},
        ip_address=ip_address,
    )
    return await auth_service.create_session(
        session,
        user,
        device_name=payload.device_name,
        user_agent=request.headers.get("user-agent"),
        ip_address=ip_address,
    )


@router.post("/refresh", response_model=TokenPair, summary="Обновить токен")
async def refresh(payload: RefreshRequest, session: SessionDep) -> TokenPair:
    return await auth_service.rotate_session(session, payload.refresh_token)


@router.post("/logout", response_model=OkMessage, summary="Выход с устройства")
async def logout(payload: RefreshRequest, session: SessionDep) -> OkMessage:
    # Авторизация по access-токену здесь не требуется: клиент отправляет
    # только refresh-токен, и требование заголовка сломало бы выход.
    from app.models import RefreshSession

    record = await session.scalar(
        select(RefreshSession).where(
            RefreshSession.token_hash == auth_service.hash_token(payload.refresh_token)
        )
    )
    await auth_service.revoke_session(session, payload.refresh_token)
    # Выход с устройства в журнале не оставался: по журналу нельзя было
    # понять, кто и откуда выходил.
    await audit_service.log_action(
        session,
        user_id=record.user_id if record else None,
        action="auth.logout",
        entity_type="user",
        entity_id=record.user_id if record else "",
    )
    return OkMessage(detail="Вы вышли из системы")


@router.post(
    "/logout-all",
    response_model=OkMessage,
    summary="Выход со всех устройств",
    description=(
        "Гасит все refresh-токены и разрывает открытые WebSocket-соединения: "
        "на других устройствах придёт событие о завершении сессии."
    ),
)
async def logout_all(
    user: CurrentUser,
    session: SessionDep,
    request: Request,
) -> OkMessage:
    count = await auth_service.revoke_all_sessions(session, user.id)
    await hub.send_to_user(
        user.id,
        EVENT_SESSION_REVOKED,
        {"reason": "logout_all", "user_id": user.id},
    )
    await audit_service.log_action(
        session,
        user=user,
        action="auth.logout_all",
        ip_address=audit_service.client_ip(request),
        details={"revoked_sessions": count},
    )
    return OkMessage(detail=f"Завершено сессий: {count}")


@router.get("/me", response_model=UserMe, summary="Текущий пользователь")
async def me(user: CurrentUser) -> UserMe:
    return user_me(user)


@router.patch("/me", response_model=UserMe, summary="Обновить свой профиль")
async def update_me(
    payload: UpdateProfileRequest, user: CurrentUser, session: SessionDep
) -> UserMe:
    data = payload.model_dump(exclude_unset=True)
    for field, value in data.items():
        setattr(user, field, value)
    await session.flush()
    return user_me(user)


@router.post(
    "/change-password",
    response_model=OkMessage,
    summary="Сменить пароль",
    description="После смены пароля остальные устройства остаются в системе.",
)
async def change_password(
    payload: ChangePasswordRequest, user: CurrentUser, session: SessionDep
) -> OkMessage:
    await auth_service.change_password(
        session, user, payload.current_password, payload.new_password
    )
    # Смена пароля в журнале не оставалась: скомпрометированный токен мог
    # поменять пароль, и следов бы не осталось.
    await audit_service.log_action(
        session,
        user=user,
        action="auth.password_change",
        entity_type="user",
        entity_id=user.id,
    )
    return OkMessage(detail="Пароль изменён")


@router.post(
    "/change-password-all",
    response_model=OkMessage,
    summary="Сменить пароль и выйти со всех устройств",
)
async def change_password_all(
    payload: ChangePasswordRequest, user: CurrentUser, session: SessionDep
) -> OkMessage:
    await auth_service.change_password(
        session, user, payload.current_password, payload.new_password
    )
    count = await auth_service.revoke_all_sessions(session, user.id)
    await audit_service.log_action(
        session,
        user=user,
        action="auth.password_change_all",
        entity_type="user",
        entity_id=user.id,
        details={"revoked_sessions": count},
    )
    return OkMessage(detail=f"Пароль изменён, завершено сессий: {count}")


@router.get(
    "/sessions",
    response_model=list[SessionInfo],
    summary="Активные сессии",
    description="Список устройств, с которых выполнен вход. Можно отозвать лишние.",
)
async def list_sessions(
    request: Request, user: CurrentUser, session: SessionDep
) -> list[SessionInfo]:
    from app.models import RefreshSession
    from app.security import decode_token

    # Текущая сессия определяется по access-токену из заголовка.
    # Раньше брались поля объекта User, которых там нет, поэтому
    # отметка «сейчас» не появлялась никогда.
    current_sid: str | None = None
    header = request.headers.get("Authorization", "")
    if header.lower().startswith("bearer "):
        try:
            payload = decode_token(header[7:].strip(), "access")
            current_sid = payload.get("sid")
        except AppError:
            current_sid = None

    rows = (
        await session.scalars(
            select(RefreshSession)
            .where(RefreshSession.user_id == user.id, RefreshSession.revoked_at.is_(None))
            .order_by(RefreshSession.created_at.desc())
        )
    ).all()

    result = []
    for record in rows:
        result.append(
            SessionInfo(
                id=record.id,
                device_name=record.device_name,
                ip_address=record.ip_address,
                created_at=record.created_at,
                last_used=record.updated_at,
                expires_at=record.expires_at,
                is_current=bool(current_sid) and current_sid == record.id,
            )
        )
    return result


@router.delete(
    "/sessions/{session_id}",
    response_model=OkMessage,
    summary="Отозвать сессию",
)
async def revoke_session(session_id: str, user: CurrentUser, session: SessionDep) -> OkMessage:
    from app.models import RefreshSession

    record = await session.get(RefreshSession, session_id)
    if record is None or record.user_id != user.id:
        raise not_found("session_not_found", "Сессия не найдена")
    if record.revoked_at is None:
        record.revoked_at = func.now()
        await session.flush()
    return OkMessage(detail="Сессия завершена")


@router.get("/password-policy", summary="Требования к паролю")
async def password_policy() -> dict:
    settings = get_settings()
    return {
        "min_length": settings.password_min_length,
        "requires_letters": True,
        "requires_digits": True,
        "max_failed_logins": settings.max_failed_logins,
        "lockout_minutes": settings.lockout_minutes,
    }


def _roles_count() -> int:
    return len(SYSTEM_ROLES)
