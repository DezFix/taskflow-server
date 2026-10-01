"""Начальные данные: системные роли и метки."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.logging_setup import get_logger
from app.models import Label, Role, Setting
from app.permissions import SYSTEM_ROLES
from app.security import new_id

logger = get_logger("seed")

DEFAULT_LABELS: tuple[tuple[str, str], ...] = (
    ("Срочное", "red"),
    ("Авария", "red"),
    ("Плановое обслуживание", "orange"),
    ("Оборудование", "purple"),
    ("Сеть", "blue"),
    ("ПО и лицензии", "green"),
)


async def create_system_roles(session: AsyncSession) -> list[Role]:
    """Создаёт системные роли, если их ещё нет. Идемпотентно."""
    created: list[Role] = []
    existing = {role.key: role for role in (await session.scalars(select(Role))).all()}

    for key, (description, is_system, permissions) in SYSTEM_ROLES.items():
        if key in existing:
            continue
        role = Role(
            id=new_id(),
            key=key,
            title=key,
            description=description,
            permissions=list(permissions),
            is_system=is_system,
        )
        session.add(role)
        created.append(role)

    if created:
        await session.flush()
        logger.info("system_roles_created", count=len(created))
    return created


async def create_defaults(session: AsyncSession) -> None:
    """Метки по умолчанию. Запускается при первой инициализации."""
    existing_labels = {label.name for label in (await session.scalars(select(Label))).all()}
    for name, color in DEFAULT_LABELS:
        if name in existing_labels:
            continue
        session.add(Label(id=new_id(), name=name, color=color, is_system=True))

    await session.flush()
    logger.info("defaults_created", labels=len(DEFAULT_LABELS))


async def ensure_settings(session: AsyncSession) -> None:
    existing = {s.key for s in (await session.scalars(select(Setting))).all()}
    defaults = {
        "app_name": {"value": "TaskFlow"},
        "app_name_ru": {"value": "TaskFlow"},
        "voice": {
            "value": {
                "enabled": True,
                "model": "base",
                "language": "ru",
            }
        },
    }
    for key, payload in defaults.items():
        if key in existing:
            continue
        session.add(Setting(id=new_id(), key=key, value=payload["value"], description=key))
    await session.flush()


async def seed_all(session: AsyncSession) -> None:
    await create_system_roles(session)
    await create_defaults(session)
    await ensure_settings(session)
