"""Create the Review Inbox tables

Revision ID: 009
Revises: 008
Create Date: 2026-08-15

Candidate heads/versions (design §7, RFC 0050): untrusted proposals that
stay out of retrieval until an atomic approve creates the entry in the
same transaction. Drives mem0.context.tables.build_candidate_metadata.
"""

from typing import Sequence, Union

from alembic import op

from db import TABLE_PREFIX

revision: str = "009"
down_revision: Union[str, None] = "008"
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
    from mem0.context.tables import build_candidate_metadata

    build_candidate_metadata(CTX_PREFIX).create_all(bind=op.get_bind())


def downgrade() -> None:
    from mem0.context.tables import build_candidate_metadata

    build_candidate_metadata(CTX_PREFIX).drop_all(bind=op.get_bind())
