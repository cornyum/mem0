"""Create refresh_token_jtis table for single-use refresh tokens

Revision ID: 005
Revises: 004
Create Date: 2026-04-21

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from db import TABLE_PREFIX

revision: str = "005"
down_revision: Union[str, None] = "004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    table = f"{TABLE_PREFIX}refresh_token_jtis"
    op.create_table(
        table,
        sa.Column("jti", sa.Uuid(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey(f"{TABLE_PREFIX}users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(f"ix_{table}_expires_at", table, ["expires_at"])


def downgrade() -> None:
    table = f"{TABLE_PREFIX}refresh_token_jtis"
    op.drop_index(f"ix_{table}_expires_at", table_name=table)
    op.drop_table(table)
