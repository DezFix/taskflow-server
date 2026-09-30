"""Зависимости FastAPI: текущий пользователь, проверка прав."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Depends, Request, WebSocket
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_session
from app.errors import forbidden, not_found, unauthorized
from app.models import User
from app.security import decode_token

# auto_error=False — чтобы самим формировать единый формат ошибки.
bearer_scheme = HTTPBearer(auto_error=False, scheme_name="JWT")

SessionDep = Annotated[AsyncSession, Depends(get_session)]


async def _load_user(session: AsyncSession, user_id: str) -> User:
    user = await session.scalar(
        select(User).where(User.id == user_id).options(selectinload(User.roles))
    )
    if user is None:
        raise unauthorized("user_missing", "Пользователь не найден")
    if not user.is_active:
        raise forbidden("account_disabled", "Учётная запись отключена")
    return user


async def get_current_user(
    request: Request,
    session: SessionDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)] = None,
) -> User:
    token = _token_from_request(request, credentials)
    payload = decode_token(token, "access")
    if payload is None:
        raise unauthorized("invalid_token", "Токен недействителен или истёк")
    user_id = payload.get("sub")
    if not user_id:
        raise unauthorized("invalid_token", "Токен недействителен")
    user = await _load_user(session, str(user_id))
    request.state.user_id = user.id
    return user


def _token_from_request(
    request: Request, credentials: HTTPAuthorizationCredentials | None
) -> str:
    if credentials is not None and credentials.credentials:
        return credentials.credentials
    # Запасной путь для клиентов, которые не умеют слать заголовок.
    header = request.headers.get("X-Access-Token")
    if header:
        return header.strip()
    raise unauthorized("missing_token", "Требуется авторизация")


CurrentUser = Annotated[User, Depends(get_current_user)]
OptionalUser = Annotated[User | None, Depends(get_current_user)]


def require_perm(*permissions: str) -> Callable[..., Awaitable[User]]:
    """Зависимость: пользователь должен иметь все перечисленные права.

    Объявляется один раз на группу эндпоинтов, поэтому набор прав виден
    в OpenAPI и может быть выгружен в клиент.

    Использование в роутере: `user: User = Depends(require_perm("tasks.create"))`
    """

    async def _dependency(user: Annotated[User, Depends(get_current_user)]) -> User:
        granted = user.permissions
        missing = [p for p in permissions if p not in granted]
        if missing:
            raise forbidden(
                "missing_permission",
                "Недостаточно прав: " + ", ".join(missing),
                {"required": list(permissions), "missing": missing},
            )
        return user

    return _dependency


def has_perm(user: User, *permissions: str) -> bool:
    granted = user.permissions
    return all(p in granted for p in permissions)


def has_any_perm(user: User, *permissions: str) -> bool:
    granted = user.permissions
    return any(p in granted for p in permissions)


def can_manage(user: User) -> bool:
    """Управленческий доступ: видит весь отдел, а не только свои задачи."""
    return has_perm(user, "tasks.view_all")


async def user_from_websocket(websocket: WebSocket, session: AsyncSession) -> User:
    """Аутентификация WebSocket: токен приходит в query-строке."""
    token = websocket.query_params.get("token")
    if not token:
        raise unauthorized("missing_token", "Требуется токен")
    payload = decode_token(token, "access")
    if payload is None:
        raise unauthorized("invalid_token", "Токен недействителен или истёк")
    user = await _load_user(session, str(payload.get("sub", "")))
    return user


async def get_optional_user(
    request: Request,
    session: SessionDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)] = None,
) -> User | None:
    """Для эндпоинтов, которые работают и без входа (скачивание по токену)."""
    try:
        token = _token_from_request(request, credentials)
    except unauthorized.__mro__[0]:
        return None
    payload = decode_token(token, "access")
    if payload is None:
        return None
    return await _load_user(session, str(payload.get("sub", "")))


async def load_user_or_404(session: AsyncSession, user_id: str) -> User:
    user = await session.get(User, user_id)
    if user is None:
        raise not_found("user_not_found", "Сотрудник не найден")
    return user
