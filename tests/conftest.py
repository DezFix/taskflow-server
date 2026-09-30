"""Общие фикстуры тестов: изолированная БД, клиент HTTP, роли и пользователи."""

from __future__ import annotations

import asyncio
import os
import tempfile
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

# Переменные окружения выставляем до импорта приложения: настройки читаются
# один раз и кэшируются.
_TMP_DIR = Path(tempfile.mkdtemp(prefix="taskflow-tests-"))
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{(_TMP_DIR / 'test.db').as_posix()}"
os.environ["STORAGE_DIR"] = str(_TMP_DIR / "storage")
os.environ["MODELS_DIR"] = str(_TMP_DIR / "models")
os.environ["BACKUP_DIR"] = str(_TMP_DIR / "backups")
os.environ["JWT_SECRET"] = "test-secret-key-not-for-production-0123456789abcdef"
os.environ["VOICE_ENABLED"] = "false"
os.environ["WEB_ROOT"] = ""
os.environ["MAX_UPLOAD_MB"] = "2"
os.environ["PASSWORD_MIN_LENGTH"] = "8"

from sqlalchemy import select

from app.database import Base, dispose_engine, get_engine, get_sessionmaker  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Role, User  # noqa: E402
from app.security import hash_password, new_id  # noqa: E402

ADMIN_PASSWORD = "AdminPass123"
HEAD_PASSWORD = "HeadPass123"
STAFF_PASSWORD = "StaffPass123"

TEST_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000a49444154789c6300010000050001"
    "0d0a2db40000000049454e44ae426082"
)


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest_asyncio.fixture(scope="session", autouse=True)
async def prepare_database() -> AsyncIterator[None]:
    """Схема создаётся один раз на всю сессию тестов."""
    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield
    await dispose_engine()


@pytest_asyncio.fixture
async def clean_database(prepare_database: None) -> AsyncIterator[None]:
    """Очищает таблицы и заново сеет стартовые данные перед каждым тестом.

    Так тесты не зависят друг от друга, а должности и метки по умолчанию
    присутствуют ровно так же, как в работающем сервере.
    """
    from app.services.seed import seed_all

    # SQLite не умеет DELETE из нескольких таблиц сразу, поэтому по одной.
    # Порядок обратный: сначала зависимые, затем корневые.
    for table in reversed(Base.metadata.sorted_tables):
        async with get_engine().begin() as conn:
            await conn.execute(table.delete())

    async with get_sessionmaker()() as session:
        await seed_all(session)
        await session.commit()

    yield


@pytest_asyncio.fixture
async def client(clean_database: None) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


# --- Данные для тестов ---


@dataclass
class Users:
    """Логины и токены подготовленных пользователей."""

    admin: str
    head: str
    staff: str
    staff2: str
    tokens: dict[str, str]
    ids: dict[str, str]


async def _role_by_key(session, key: str) -> Role:  # noqa: ANN001
    """Берёт системную роль, созданную seed-данными."""
    role = await session.scalar(select(Role).where(Role.key == key))
    assert role is not None, f"Системная роль {key} не создана"
    return role


async def _create_user(  # noqa: ANN202
    session,
    username: str,
    password: str,
    roles: list[Role],
    *,
    full_name: str | None = None,
    is_superuser: bool = False,
) -> User:
    user = User(
        id=new_id(),
        username=username,
        full_name=full_name or username.capitalize(),
        password_hash=hash_password(password),
        is_active=True,
        is_superuser=is_superuser,
        must_change_password=False,
    )
    user.roles = roles
    session.add(user)
    await session.flush()
    return user


async def _login(client: AsyncClient, username: str, password: str) -> str:
    response = await client.post(
        "/api/v1/auth/login",
        json={"identifier": username, "password": password, "device_name": "pytest"},
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


@pytest_asyncio.fixture
async def users(client: AsyncClient) -> Users:
    """Создаёт администратора, главу отдела и двух сотрудников.

    Роли берутся из стартовых данных, поэтому тесты проверяют ровно те
    права, которые получит сотрудник в реальной установке.
    """
    async with get_sessionmaker()() as session:
        admin_role = await _role_by_key(session, "Администратор")
        head_role = await _role_by_key(session, "Глава отдела")
        staff_role = await _role_by_key(session, "Сотрудник")

        admin = await _create_user(
            session, "admin", ADMIN_PASSWORD, [admin_role], is_superuser=True
        )
        head = await _create_user(session, "head", HEAD_PASSWORD, [head_role])
        staff = await _create_user(session, "staff", STAFF_PASSWORD, [staff_role])
        staff2 = await _create_user(session, "staff2", STAFF_PASSWORD, [staff_role])
        await session.commit()

        ids = {"admin": admin.id, "head": head.id, "staff": staff.id, "staff2": staff2.id}

    tokens = {
        "admin": await _login(client, "admin", ADMIN_PASSWORD),
        "head": await _login(client, "head", HEAD_PASSWORD),
        "staff": await _login(client, "staff", STAFF_PASSWORD),
        "staff2": await _login(client, "staff2", STAFF_PASSWORD),
    }

    return Users(
        admin="admin",
        head="head",
        staff="staff",
        staff2="staff2",
        tokens=tokens,
        ids=ids,
    )


@pytest_asyncio.fixture
async def auth(users: Users) -> dict[str, dict[str, str]]:
    """Готовые заголовки Authorization для каждой роли."""
    return {
        key: {"Authorization": f"Bearer {token}"} for key, token in users.tokens.items()
    }


@pytest.fixture
def anyio_run() -> Iterator[None]:  # pragma: no cover
    yield


def run_async(coro) -> None:  # pragma: no cover
    asyncio.run(coro)
