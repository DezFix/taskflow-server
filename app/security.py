"""Хеширование паролей, JWT, генерация идентификаторов."""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from app.config import get_settings

TokenType = Literal["access", "refresh"]

_hasher = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1)

JWT_ALGORITHM = "HS256"


def new_id() -> str:
    """Строковый UUID4: переносимо между БД и не выдаёт объём данных."""
    return str(uuid.uuid4())


def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


# --- Пароли ---


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        _hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False
    return True


def password_needs_rehash(password_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


# --- JWT ---


def _now() -> datetime:
    return datetime.now(UTC)


def _secret() -> str:
    return get_settings().resolve_jwt_secret()


def create_access_token(*, user_id: str, extra: dict[str, Any] | None = None) -> tuple[str, int]:
    """Возвращает (token, ttl_seconds)."""
    settings = get_settings()
    ttl = timedelta(minutes=settings.access_token_minutes)
    now = _now()
    payload: dict[str, Any] = {
        "sub": user_id,
        "type": "access",
        "iat": int(now.timestamp()),
        "exp": int((now + ttl).timestamp()),
        "jti": new_id(),
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, _secret(), algorithm=JWT_ALGORITHM), int(ttl.total_seconds())


def create_refresh_token(*, user_id: str, session_id: str) -> tuple[str, int]:
    settings = get_settings()
    ttl = timedelta(days=settings.refresh_token_days)
    now = _now()
    payload = {
        "sub": user_id,
        "sid": session_id,
        "type": "refresh",
        "iat": int(now.timestamp()),
        "exp": int((now + ttl).timestamp()),
        "jti": new_id(),
    }
    return jwt.encode(payload, _secret(), algorithm=JWT_ALGORITHM), int(ttl.total_seconds())


def decode_token(token: str, expected_type: TokenType | None = None) -> dict[str, Any] | None:
    """Возвращает payload либо None, если токен невалиден или истёк."""
    try:
        payload = jwt.decode(token, _secret(), algorithms=[JWT_ALGORITHM])
    except jwt.PyJWTError:
        return None
    if expected_type and payload.get("type") != expected_type:
        return None
    return payload
