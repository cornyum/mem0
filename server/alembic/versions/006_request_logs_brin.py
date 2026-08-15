"""Swap request_logs.created_at btree index for a BRIN index

Revision ID: 006
Revises: 005
Create Date: 2026-04-21

"""

from typing import Sequence, Union

from alembic import op

from db import TABLE_PREFIX

revision: str = "006"
down_revision: Union[str, None] = "005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

INDEX_NAME = f"ix_{TABLE_PREFIX}request_logs_created_at"
TABLE_NAME = f"{TABLE_PREFIX}request_logs"


def upgrade() -> None:
    # BRIN is a PostgreSQL-only index type; on MySQL 8 the btree index
    # created in 002 is kept as-is.
    if op.get_bind().dialect.name != "postgresql":
        return
    op.drop_index(INDEX_NAME, table_name=TABLE_NAME)
    op.execute(f"CREATE INDEX {INDEX_NAME} ON {TABLE_NAME} USING BRIN (created_at)")


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.drop_index(INDEX_NAME, table_name=TABLE_NAME)
    op.create_index(INDEX_NAME, TABLE_NAME, ["created_at"])
