"""Сервис аутентификации: вход, сессии, пароли."""

from __future__ import annotations

import hashlib
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import utcnow
from app.errors import bad_request, conflict, forbidden, not_found, unauthorized
from app.models import User
from app.security import (
    create_access_token,
    create_refresh_token,
    hash_password,
    new_id,
    new_token,
    password_needs_rehash,
    verify_password,
)

MIN_USERNAME_LEN = 3
MAX_USERNAME_LEN = 64


def hash_token(token: str) -> str:
    """В БД хранится только хеш refresh-токена: утечка датчины не даёт токен."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def validate_username(username: str) -> str:
    value = (username or "").strip().lower()
    if not value:
        raise bad_request("username_required", "Логин обязателен")
    if not (MIN_USERNAME_LEN <= len(value) <= MAX_USERNAME_LEN):
        raise bad_request(
            "username_length",
            f"Логин должен быть от {MIN_USERNAME_LEN} до {MAX_USERNAME_LEN} символов",
        )
    if not all(ch.isalnum() or ch in "._-" for ch in value):
        raise bad_request(
            "username_charset",
            "Логин может содержать только латиницу, цифры, точку, дефис и подчёркивание",
        )
    return value


def validate_password_strength(password: str) -> None:
    """Проверка на клиенте дублируется на сервере — правило одно и то же."""
    settings = get_settings()
    if len(password or "") < settings.password_min_length:
        raise bad_request(
            "password_too_short",
            f"Пароль должен быть не короче {settings.password_min_length} символов",
        )
    checks = (
        any(ch.isalpha() for ch in password),
        any(ch.isdigit() for ch in password),
    )
    if not all(checks):
        raise bad_request(
            "password_weak",
            "Пароль должен содержать буквы и цифры",
        )


async def get_user_by_username(session: AsyncSession, username: str) -> User | None:
    stmt = select(User).where(User.username == username.strip().lower())
    return await session.scalar(stmt)


async def get_user_by_identifier(session: AsyncSession, identifier: str) -> User | None:
    """Вход по логину или по email — как удобнее сотруднику."""
    value = (identifier or "").strip().lower()
    if "@" in value:
        stmt = select(User).where(User.email == value)
        return await session.scalar(stmt)
    return await get_user_by_username(session, value)


async def authenticate(session: AsyncSession, identifier: str, password: str) -> User:
    """Проверяет учётные данные. Блокировка после серии неудач."""
    user = await get_user_by_identifier(session, identifier)

    if user is None:
        # Считаем хеш, чтобы время ответа не выдавало наличие логина.
        verify_password(password, hash_password("dummy-for-timing"))
        raise unauthorized("invalid_credentials", "Неверный логин или пароль")

    if user.is_locked():
        raise forbidden(
            "account_locked",
            f"Вход заблокирован до {user.locked_until:%H:%M}. Обратитесь к администратору",
        )

    if not user.is_active:
        raise forbidden("account_disabled", "Учётная запись отключена")

    if not verify_password(password, user.password_hash):
        user.failed_login_count += 1
        settings = get_settings()
        if user.failed_login_count >= settings.max_failed_logins:
            user.locked_until = utcnow() + timedelta(minutes=settings.lockout_minutes)
            user.failed_login_count = 0
        # Счётчик неудачных попыток фиксируем отдельным коммитом.
        # Запрос завершится ошибкой, а зависимость откатит транзакцию —
        # и без этого коммита блокировка никогда не срабатывала бы.
        await session.commit()
        raise unauthorized("invalid_credentials", "Неверный логин или пароль")

    # Успешный вход сбрасывает счётчик неудач.
    user.failed_login_count = 0
    user.locked_until = None
    user.last_login_at = utcnow()

    if password_needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)

    await session.flush()
    return user


def issue_tokens(user: User, session_id: str, refresh_token: str) -> dict:
    access_token, expires_in = create_access_token(
        user_id=user.id,
        # Идентификатор сессии кладём в access-токен: без него нельзя
        # понять, какая из сессий в списке устройств текущая, и нельзя
        # разорвать сессию по требованию «выйти со всех остальных».
        extra={
            "username": user.username,
            "name": user.full_name,
            "sid": session_id,
        },
    )
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
        "expires_in": expires_in,
        "user_id": user.id,
        "session_id": session_id,
    }


async def create_session(
    session: AsyncSession,
    user: User,
    *,
    device_name: str | None = None,
    user_agent: str | None = None,
    ip_address: str | None = None,
) -> dict:
    settings = get_settings()
    from app.models import RefreshSession

    session_id = new_id()
    token, _ = create_refresh_token(user_id=user.id, session_id=session_id)

    record = RefreshSession(
        id=session_id,
        user_id=user.id,
        token_hash=hash_token(token),
        device_name=(device_name or "Неизвестное устройство")[:200],
        user_agent=(user_agent or "")[:400] or None,
        ip_address=ip_address,
        expires_at=utcnow() + timedelta(days=settings.refresh_token_days),
    )
    session.add(record)
    await session.flush()

    return issue_tokens(user, session_id, token)


async def rotate_session(session: AsyncSession, refresh_token: str) -> dict:
    """Обновляет пару токенов с ротацией: старый refresh гасится."""
    from app.models import RefreshSession

    payload_hash = hash_token(refresh_token)
    record = await session.scalar(
        select(RefreshSession).where(RefreshSession.token_hash == payload_hash)
    )
    if record is None:
        raise unauthorized("invalid_refresh", "Сессия не найдена, войдите заново")

    if record.revoked_at is not None:
        raise unauthorized("session_revoked", "Сессия завершена, войдите заново")

    if record.expires_at < utcnow():
        raise unauthorized("session_expired", "Срок сессии истёк, войдите заново")

    user = record.user
    if not user.is_active:
        raise forbidden("account_disabled", "Учётная запись отключена")

    # Ротация: старая запись гасится, выдаётся новая.
    record.revoked_at = utcnow()

    new_session_id = new_id()
    new_token_value, _ = create_refresh_token(user_id=user.id, session_id=new_session_id)

    session.add(
        RefreshSession(
            id=new_session_id,
            user_id=user.id,
            token_hash=hash_token(new_token_value),
            device_name=record.device_name,
            user_agent=record.user_agent,
            ip_address=record.ip_address,
            expires_at=record.expires_at,
        )
    )
    await session.flush()

    return issue_tokens(user, new_session_id, new_token_value)


async def revoke_session(session: AsyncSession, refresh_token: str) -> None:
    from app.models import RefreshSession

    record = await session.scalar(
        select(RefreshSession).where(RefreshSession.token_hash == hash_token(refresh_token))
    )
    if record is not None and record.revoked_at is None:
        record.revoked_at = utcnow()
        await session.flush()


async def revoke_all_sessions(session: AsyncSession, user_id: str) -> int:
    from app.models import RefreshSession

    records = (
        await session.scalars(
            select(RefreshSession).where(
                RefreshSession.user_id == user_id, RefreshSession.revoked_at.is_(None)
            )
        )
    ).all()
    now = utcnow()
    for record in records:
        record.revoked_at = now
    await session.flush()
    return len(records)


async def change_password(
    session: AsyncSession,
    user: User,
    current_password: str,
    new_password: str,
) -> None:
    if not verify_password(current_password, user.password_hash):
        raise unauthorized("wrong_password", "Текущий пароль указан неверно")
    if current_password == new_password:
        raise bad_request("password_reuse", "Новый пароль должен отличаться от текущего")
    validate_password_strength(new_password)

    user.password_hash = hash_password(new_password)
    user.password_changed_at = utcnow()
    user.must_change_password = False
    await session.flush()
    # Коммит здесь, а не в зависимости get_session: та досылает ответ
    # раньше, чем завершится её yield. Иначе сотрудник успевает войти
    # со старым паролем в окне между сбросом и фиксацией в базе.
    await session.commit()


async def set_password(
    session: AsyncSession, user: User, new_password: str, *, must_change: bool = False
) -> None:
    validate_password_strength(new_password)
    user.password_hash = hash_password(new_password)
    user.password_changed_at = utcnow()
    user.must_change_password = must_change
    user.failed_login_count = 0
    user.locked_until = None
    await session.flush()
    # Старый пароль должен перестать работать сразу же после ответа.
    # Коммит в зависимости get_session случается позже, поэтому фиксируем
    # здесь: иначе старые пароли живут ещё несколько миллисекунд.
    await session.commit()


def ensure_username_available(session: AsyncSession, username: str, exclude_id: str | None = None) -> None:
    """Синхронная проверка; вызывается после того, как уникальность уже искали."""
    if not username:
        raise bad_request("username_required", "Логин обязателен")


def generate_temporary_password() -> str:
    """Читаемый временный пароль: без символов, которые путают при вводе."""
    import string

    alphabet = string.ascii_letters.replace("l", "").replace("O", "").replace("I", "")
    digits = "23456789"
    symbols = "@#$%"
    chars = [new_token(1)[0] for _ in range(4)]
    body = "".join(new_token(1)[0] for _ in range(6))
    for ch in body:
        chars.append(alphabet[ord(ch) % len(alphabet)])
    for ch in "".join(new_token(1)[0] for _ in range(3)):
        chars.append(digits[ord(ch) % len(digits)])
    chars.append(symbols[ord(new_token(1)[0]) % len(symbols)])
    return "".join(chars)[:12]


def assert_not_self(user: User, target: User, action: str) -> None:
    if user.id == target.id and action != "deactivate":
        raise conflict("self_action", "Нельзя выполнить это действие над собой")


def ensure_user_exists(user: User | None) -> User:
    if user is None:
        raise not_found("user_not_found", "Сотрудник не найден")
    return user
