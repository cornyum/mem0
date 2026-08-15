"""Create the P2 context table family

Revision ID: 008
Revises: 007
Create Date: 2026-08-15

Source store (raw-fact journal with per-scope monotonic positions),
lineage edges (source/artifact), and handoff artifacts (design §4/§7).
Drives mem0.context.tables.build_p2_metadata — single source of truth.
"""

from typing import Sequence, Union

from alembic import op

from db import TABLE_PREFIX

revision: str = "008"
down_revision: Union[str, None] = "007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

CTX_PREFIX = f"{TABLE_PREFIX}ctx_"


def upgrade() -> None:
    from mem0.context.tables import build_p2_metadata

    build_p2_metadata(CTX_PREFIX).create_all(bind=op.get_bind())


def downgrade() -> None:
    from mem0.context.tables import build_p2_metadata

    build_p2_metadata(CTX_PREFIX).drop_all(bind=op.get_bind())
