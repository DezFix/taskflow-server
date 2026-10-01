"""Убрать должности

Должности дублировали роли и ничего не решали: прав они не давали,
а таблица positions существовала только ради подписи у сотрудника.
Роли остаются единственным источником прав.

Ревизия удаляет:
- таблицу positions;
- users.position_id и users.job_title;
- ключи прав positions.* из списка прав существующих ролей.

Откат возвращает пустую таблицу должностей и обе колонки: значения
не восстановить, поэтому откат приводит схему к рабочему виду,
но не к прежним данным.

Revision ID: b7c1d9e4f2a3
Revises: 8557cb5d26f9
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "b7c1d9e4f2a3"
down_revision: str | None = "8557cb5d26f9"
branch_labels: str | None = None
depends_on: str | None = None

_DROPPED_PERMISSIONS = ("positions.view", "positions.create", "positions.edit", "positions.delete")


def upgrade() -> None:
    # Права хранятся в roles.permissions как JSON-список. Чистим до
    # удаления колонок, иначе система прав будет знать о ключах,
    # которых больше нет.
    connection = op.get_bind()
    roles = sa.table(
        "roles",
        sa.column("id", sa.String),
        sa.column("permissions", sa.JSON),
    )
    for row in connection.execute(sa.select(roles.c.id, roles.c.permissions)).all():
        current = list(row.permissions or [])
        cleaned = [key for key in current if key not in _DROPPED_PERMISSIONS]
        if cleaned != current:
            connection.execute(
                roles.update().where(roles.c.id == row.id).values(permissions=cleaned)
            )

    # SQLite не умеет DROP COLUMN без пересборки таблицы, поэтому
    # колонки снимаем через batch_alter_table.
    with op.batch_alter_table("users") as batch:
        batch.drop_column("position_id")
        batch.drop_column("job_title")

    op.drop_table("positions")


def downgrade() -> None:
    op.create_table(
        "positions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("title", sa.String(length=160), nullable=False),
        sa.Column("description", sa.String(length=1000), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_positions")),
        sa.UniqueConstraint("title", name="uq_positions_title"),
    )

    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("job_title", sa.String(length=160), nullable=True))
        batch.add_column(sa.Column("position_id", sa.String(length=36), nullable=True))
        batch.create_foreign_key(
            op.f("fk_users_position_id_positions"),
            "positions",
            ["position_id"],
            ["id"],
            ondelete="SET NULL",
        )
