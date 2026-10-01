"""Подключение к базе данных, фабрика сессий, базовый декларативный класс."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, MetaData, String, event
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import (
    AsyncAttrs,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.config import get_settings

# Единый согласованный набор ограничений: внешние ключи ссылаются на
# одноимённые колонки, это избавляет от конфликтов имён при join'ах.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(AsyncAttrs, DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    def to_dict(self) -> dict[str, Any]:
        return {c.name: getattr(self, c.name) for c in self.__table__.columns}


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class TimestampMixin:
    """Даты создания и изменения.

    Значения вычисляются на стороне Python, а не СУБД: server-side onupdate
    заставляет SQLAlchemy помечать колонку просроченной и перечитывать её
    отдельным запросом, что в async-коде даёт MissingGreenlet.
    """

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow
    )


class IdMixin:
    # Явная длина обязательна: без неё MySQL трактует колонку как
    # VARCHAR без размера и отказывается создавать таблицу.
    id: Mapped[str] = mapped_column(String(36), primary_key=True)


_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def _build_engine() -> AsyncEngine:
    settings = get_settings()
    kwargs: dict[str, Any] = {"echo": settings.db_echo, "future": True}

    if settings.is_sqlite:
        # SQLite по умолчанию игнорирует внешние ключи и пишет под блокировкой.
        # WAL даёт параллельное чтение, а busy_timeout снимает конкурентные записи.
        connect_args = {"check_same_thread": False, "timeout": 30}
        kwargs["connect_args"] = connect_args
        engine = create_async_engine(settings.database_url, **kwargs)

        @event.listens_for(engine.sync_engine, "connect")
        def _sqlite_pragmas(dbapi_connection, _record):  # noqa: ANN001
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.close()

        return engine

    # PostgreSQL/MySQL: увеличенный пул, т.к. соединение дороже, чем у SQLite.
    kwargs.update({"pool_size": 10, "max_overflow": 20, "pool_pre_ping": True, "pool_recycle": 1800})
    return create_async_engine(settings.database_url, **kwargs)


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = _build_engine()
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            bind=get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )
    return _sessionmaker


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI-зависимость: сессия на запрос с откатом при ошибке."""
    async with get_sessionmaker()() as session:
        try:
            yield session
            await commit_with_retry(session)
        except Exception:
            await session.rollback()
            raise


#: Сколько раз повторять коммит при временной блокировке SQLite.
COMMIT_RETRIES = 3
COMMIT_RETRY_DELAY = 0.2


async def commit_with_retry(session: AsyncSession) -> None:
    """Коммитит с повтором при временной блокировке базы.

    В SQLite пишет только один поток. Обработчик держит транзакцию
    открытой, пока рассылает события и генерирует превью, поэтому второй
    запрос на запись получает `database is locked`. Это не ошибка
    сотрудника, а очередь writers, и повтор её снимает.

    `busy_timeout` не помогает: он не действует, когда снимок чтения
    прочитан до коммита другого писателя.
    """
    for attempt in range(COMMIT_RETRIES):
        try:
            await session.commit()
            return
        except OperationalError as error:
            message = str(error).lower()
            if "locked" not in message and "busy" not in message:
                raise
            if attempt == COMMIT_RETRIES - 1:
                raise
            await session.rollback()
            await asyncio.sleep(COMMIT_RETRY_DELAY * (attempt + 1))


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None
