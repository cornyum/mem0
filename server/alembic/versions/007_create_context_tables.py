"""Create the authoritative context table family

Revision ID: 007
Revises: 006
Create Date: 2026-08-15

Drives mem0.context.tables.build_metadata (single source of truth for the
portable DDL) under the server's app-DB prefix, then adds the
dialect-specific FTS accelerator on entry_heads.searchable_text:

- MySQL 8: plain FULLTEXT index (the application-layer analyzer already
  emits whitespace-separated [0-9a-z] tokens — an ngram parser here would
  re-split them and bloat the index; design ADR-6).
- PostgreSQL: GIN index over to_tsvector('simple', searchable_text).
- Other dialects (SQLite in tests): no accelerator; the tables still work,
  the FTS sidecar query path is added in P1.
"""

from typing import Sequence, Union

from alembic import op

from db import TABLE_PREFIX

revision: str = "007"
down_revision: Union[str, None] = "006"
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
HEADS_TABLE = f"{CTX_PREFIX}entry_heads"
FTS_INDEX = f"ft_{CTX_PREFIX}entry_heads_searchable"


def upgrade() -> None:
    if _v3_storage_mode():
        return
    from mem0.context.tables import build_metadata

    build_metadata(CTX_PREFIX).create_all(bind=op.get_bind())

    bind = op.get_bind()
    if bind.dialect.name == "mysql":
        op.execute(f"CREATE FULLTEXT INDEX {FTS_INDEX} ON {HEADS_TABLE} (searchable_text)")
    elif bind.dialect.name == "postgresql":
        from sqlalchemy import text

        op.create_index(
            FTS_INDEX,
            HEADS_TABLE,
            [text("to_tsvector('simple', searchable_text)")],
            postgresql_using="gin",
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "mysql":
        op.execute(f"DROP INDEX {FTS_INDEX} ON {HEADS_TABLE}")
    elif bind.dialect.name == "postgresql":
        op.drop_index(FTS_INDEX, table_name=HEADS_TABLE)

    from mem0.context.tables import build_metadata

    build_metadata(CTX_PREFIX).drop_all(bind=bind)
