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


def _v3_storage_mode() -> bool:
    """True under the v3 storage modes (design §1/§12.1): ONLY_VDB must not
    create any SQL memory tables; HYBRID only creates the two recall-sidecar
    tables via the runtime ensure_schema (§8). The legacy ctx family is left
    for databases that already have it (read-only, one release cycle, §9)."""
    import os

    return (os.environ.get("MEMORY_STORAGE_MODE") or "ONLY_VDB").strip().upper() in (
        "ONLY_VDB",
        "HYBRID_STORAGE",
        "HYBRIRD_STORAGE",
    )

CTX_PREFIX = f"{TABLE_PREFIX}ctx_"


def upgrade() -> None:
    if _v3_storage_mode():
        return
    from mem0.context.tables import build_p2_metadata

    build_p2_metadata(CTX_PREFIX).create_all(bind=op.get_bind())


def downgrade() -> None:
    from mem0.context.tables import build_p2_metadata

    build_p2_metadata(CTX_PREFIX).drop_all(bind=op.get_bind())
