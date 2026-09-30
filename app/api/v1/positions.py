"""Должности: создание, правка, архивирование."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.deps import SessionDep, require_perm
from app.errors import conflict, not_found
from app.models import Position, User
from app.schemas import OkMessage, PositionCreate, PositionOut, PositionUpdate
from app.security import new_id
from app.serializers import position_out

router = APIRouter(prefix="/positions", tags=["positions"])


@router.get(
    "",
    response_model=list[PositionOut],
    summary="Список должностей",
    description="Доступно всем авторизованным: сотрудник должен видеть название своей должности.",
)
async def list_positions(
    session: SessionDep,
    _: User = Depends(require_perm("positions.view")),
) -> list[PositionOut]:
    rows = (
        await session.execute(
            select(Position, func.count(User.id))
            .outerjoin(User, User.position_id == Position.id)
            .group_by(Position.id)
            .order_by(Position.sort_order, Position.title)
        )
    ).all()
    return [position_out(position, int(count)) for position, count in rows]


@router.post(
    "",
    response_model=PositionOut,
    status_code=201,
    summary="Создать должность",
)
async def create_position(
    payload: PositionCreate,
    session: SessionDep,
    _: User = Depends(require_perm("positions.create")),
) -> PositionOut:
    position = Position(
        id=new_id(),
        title=payload.title.strip(),
        description=(payload.description or "").strip() or None,
        sort_order=payload.sort_order,
    )
    session.add(position)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise conflict("position_exists", "Должность с таким названием уже есть") from None
    return position_out(position, 0)


@router.get("/{position_id}", response_model=PositionOut, summary="Карточка должности")
async def get_position(
    position_id: str,
    session: SessionDep,
    _: User = Depends(require_perm("positions.view")),
) -> PositionOut:
    position = await session.get(Position, position_id)
    if position is None:
        raise not_found("position_not_found", "Должность не найдена") from None
    count = await session.scalar(
        select(func.count(User.id)).where(User.position_id == position_id)
    )
    return position_out(position, int(count or 0))


@router.patch("/{position_id}", response_model=PositionOut, summary="Изменить должность")
async def update_position(
    position_id: str,
    payload: PositionUpdate,
    session: SessionDep,
    _: User = Depends(require_perm("positions.edit")),
) -> PositionOut:
    position = await session.get(Position, position_id)
    if position is None:
        raise not_found("position_not_found", "Должность не найдена") from None
    data = payload.model_dump(exclude_unset=True)
    if "title" in data and data["title"]:
        data["title"] = data["title"].strip()

    for field, value in data.items():
        setattr(position, field, value)

    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise conflict("position_exists", "Должность с таким названием уже есть") from None
    count = await session.scalar(
        select(func.count(User.id)).where(User.position_id == position_id)
    )
    return position_out(position, int(count or 0))


@router.delete(
    "/{position_id}",
    response_model=OkMessage,
    summary="Удалить должность",
    description="Должность с сотрудниками удалить нельзя — сначала переведите их.",
)
async def delete_position(
    position_id: str,
    session: SessionDep,
    _: User = Depends(require_perm("positions.delete")),
) -> OkMessage:
    position = await session.get(Position, position_id)
    if position is None:
        raise not_found("position_not_found", "Должность не найдена") from None
    assigned = await session.scalar(
        select(func.count(User.id)).where(User.position_id == position_id)
    )
    if assigned:
        raise conflict(
            "position_in_use",
            f"На этой должности работает сотрудников: {assigned}. "
            "Сначала переведите их на другую должность или деактивируйте.",
        )

    await session.delete(position)
    await session.flush()
    return OkMessage(detail="Должность удалена")
