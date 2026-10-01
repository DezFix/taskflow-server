"""Ограничение частоты попыток входа.

Внешних библиотек для этого в проекте нет, а перебор паролей без
ограничения частоты — самый доступный способ взлома: учётная запись
блокируется на 15 минут, но повторять цикл можно бесконечно.

Хранилище в памяти процесса: этого достаточно для офисного сервера с
одним инстансом. При нескольких процессах за общим PostgreSQL лимит
перестанет быть общим — это осознанный компромисс, чтобы не тянуть
Redis ради одного эндпоинта.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque

from app.errors import too_many_requests

# Ключ -> время попыток. Чистится по мере вытекания окна.
_attempts: dict[str, deque[float]] = defaultdict(deque)
# Последняя уборка, чтобы не обходить весь словарь на каждый запрос.
_last_sweep = 0.0

_SWEEP_AFTER_SECONDS = 60.0


def _sweep(now: float, window: float) -> None:
    global _last_sweep  # noqa: PLW0603
    if now - _last_sweep < _SWEEP_AFTER_SECONDS:
        return
    _last_sweep = now
    for key, stamps in list(_attempts.items()):
        cutoff = now - window
        while stamps and stamps[0] < cutoff:
            stamps.popleft()
        if not stamps:
            del _attempts[key]


def _too_many(key: str, limit: int, window: float, message: str) -> None:
    now = time.monotonic()
    _sweep(now, window)
    stamps = _attempts[key]
    if len(stamps) >= limit:
        wait = int(window - (now - stamps[0])) + 1
        raise too_many_requests(
            "too_many_attempts",
            f"{message} Повторите через {wait} с.",
            {"retry_after": wait},
        )
    stamps.append(now)


def check_identifier(identifier: str, limit: int, window_minutes: int) -> None:
    """Ограничивает попытки входа по одному логину.

    Счётчик неудачных попыток в самой учётной записи защищает от подбора,
    но не ограничивает скорость: обход блокировки возможен и через
    массовые попытки по разным логинам с одного адреса.
    """
    value = (identifier or "").strip().lower()
    if not value:
        return
    _too_many(
        f"id:{value}",
        limit,
        window_minutes * 60.0,
        "Слишком много попыток входа для этого логина.",
    )


def check_ip(ip: str | None, limit: int, window_minutes: int) -> None:
    """Ограничивает суммарную частоту входа с одного адреса."""
    if not ip:
        return
    _too_many(
        f"ip:{ip}",
        limit,
        window_minutes * 60.0,
        "Слишком много попыток входа с этого адреса.",
    )


def forget(identifier: str, ip: str | None = None) -> None:
    """Сбрасывает счётчики конкретного входа.

    Сбрасывать всё нельзя: успешный вход одного сотрудника обнулял бы
    счётчики перебора посторонних, и атакующий обходил бы лимит,
    войдя под своим вторым аккаунтом.
    """
    _attempts.pop(f"id:{(identifier or '').strip().lower()}", None)
    if ip:
        _attempts.pop(f"ip:{ip}", None)


def reset() -> None:
    """Очищает все счётчики. Только для тестов."""
    _attempts.clear()