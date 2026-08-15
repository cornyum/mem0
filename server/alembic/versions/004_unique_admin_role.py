"""Enforce at-most-one admin via partial unique index

Revision ID: 004
Revises: 003
Create Date: 2026-04-21

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from db import TABLE_PREFIX

revision: str = "004"
down_revision: Union[str, None] = "003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

INDEX_NAME = f"ix_{TABLE_PREFIX}users_only_one_admin"


def upgrade() -> None:
    # MySQL 8 has no partial indexes; there the at-most-one-admin rule is
    # enforced by the application layer instead.
    if op.get_bind().dialect.name != "postgresql":
        return
    op.create_index(
        INDEX_NAME,
        f"{TABLE_PREFIX}users",
        ["role"],
        unique=True,
        postgresql_where=sa.text("role = 'admin'"),
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.drop_index(INDEX_NAME, table_name=f"{TABLE_PREFIX}users")
