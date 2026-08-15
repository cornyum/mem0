"""Authoritative ctx_* table family (design §4).

Portable SQLAlchemy Core definitions — SQLite (tests), MySQL 8 (primary
deployment) and PostgreSQL (compat tier) all run the same DDL. The MySQL
FULLTEXT index on ``entry_heads.searchable_text`` is dialect-specific and
belongs to the server's alembic migration, not to this portable metadata:
it is an optional projection accelerator, never schema the code depends on.

Model (design §4):
- ``memory_bindings``: one memory artifact per write-scope (scope_key).
- ``entry_versions``: immutable content versions — never UPDATEd.
- ``entry_heads``: rebuildable head projection, carries the dedup index,
  FTS text, vector pointer and pending_embed flag.
- ``memory_heads``: the CAS revision counter per artifact.

No cross-table foreign keys: all invariants (version = previous + 1, head
points at an existing version, binding exists before heads) are enforced in
the single transaction that writes them, which keeps the DDL identical
across MySQL/PostgreSQL/SQLite.
"""

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
)

DEFAULT_TABLE_PREFIX = "agentar_mem_ctx_"

KIND_FIELD_MAX = 64
SCOPE_KEY_LEN = 64
ID_LEN = 256
UUID_LEN = 36
HASH_LEN = 64
STATE_LEN = 16
PROVENANCE_LEN = 16


def build_table_names(prefix: str = DEFAULT_TABLE_PREFIX) -> dict[str, str]:
    return {
        "bindings": f"{prefix}memory_bindings",
        "entry_versions": f"{prefix}entry_versions",
        "entry_heads": f"{prefix}entry_heads",
        "memory_heads": f"{prefix}memory_heads",
    }


def _identity_columns() -> list[Column]:
    """Entity columns preserve mem0's any-subset filter semantics (D6);
    scope_key is the write-time id-set fingerprint used for CAS/dedup."""
    return [
        Column("scope_key", String(SCOPE_KEY_LEN), nullable=False),
        Column("tenant_id", String(ID_LEN)),
        Column("user_id", String(ID_LEN)),
        Column("agent_id", String(ID_LEN)),
        Column("run_id", String(ID_LEN)),
        Column("session_id", String(ID_LEN)),
    ]


def build_metadata(prefix: str = DEFAULT_TABLE_PREFIX) -> MetaData:
    """Build the ctx table family under ``prefix``. Fresh metadata per call so
    different prefixes (server app DB vs standalone SDK) never collide."""
    names = build_table_names(prefix)
    metadata = MetaData()

    Table(
        names["bindings"],
        metadata,
        *_identity_columns(),
        Column("artifact_id", String(UUID_LEN), nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False),
        UniqueConstraint("scope_key", name=f"uq_{names['bindings']}_scope_key"),
        Index(
            f"ix_{names['bindings']}_identity",
            "tenant_id",
            "user_id",
            "agent_id",
            "run_id",
            "session_id",
        ),
    )

    Table(
        names["entry_versions"],
        metadata,
        Column("scope_key", String(SCOPE_KEY_LEN), primary_key=True),
        Column("artifact_id", String(UUID_LEN), primary_key=True),
        Column("entry_id", String(UUID_LEN), primary_key=True),
        Column("version", Integer, primary_key=True),
        Column("entry_version_id", String(UUID_LEN), nullable=False),
        Column("previous_version_id", String(UUID_LEN)),
        Column("kind", String(KIND_FIELD_MAX), nullable=False),
        Column("text", Text, nullable=False),
        Column("source_refs", Text, nullable=False, default="[]"),
        Column("artifact_refs", Text, nullable=False, default="[]"),
        Column("categories", Text, nullable=False, default="[]"),
        Column("entry_content_hash", String(HASH_LEN), nullable=False),
        Column("created_in_revision", Integer, nullable=False),
        Column("provenance", String(PROVENANCE_LEN), nullable=False, default="native"),
        Column("created_at", DateTime(timezone=True), nullable=False),
        UniqueConstraint(
            "scope_key",
            "artifact_id",
            "entry_id",
            "entry_version_id",
            name=f"uq_{names['entry_versions']}_version_id",
        ),
        Index(
            f"ix_{names['entry_versions']}_revision",
            "scope_key",
            "artifact_id",
            "created_in_revision",
        ),
    )

    Table(
        names["entry_heads"],
        metadata,
        # Identity entity columns live here too (denormalized from the
        # binding) so subset-filtered recall and the FTS sidecar query stay
        # single-table (design §4: all ctx tables carry the scope prefix).
        Column("scope_key", String(SCOPE_KEY_LEN), primary_key=True),
        Column("tenant_id", String(ID_LEN)),
        Column("user_id", String(ID_LEN)),
        Column("agent_id", String(ID_LEN)),
        Column("run_id", String(ID_LEN)),
        Column("session_id", String(ID_LEN)),
        Column("artifact_id", String(UUID_LEN), primary_key=True),
        Column("entry_id", String(UUID_LEN), primary_key=True),
        Column("head_revision", Integer, nullable=False),
        Column("entry_version_id", String(UUID_LEN), nullable=False),
        Column("entry_content_hash", String(HASH_LEN), nullable=False),
        Column("state", String(STATE_LEN), nullable=False),
        Column("searchable_text", Text, nullable=False),
        Column("vector_id", String(UUID_LEN)),
        Column("pending_embed", Boolean, nullable=False, default=False),
        Column("created_at", DateTime(timezone=True), nullable=False),
        Column("updated_at", DateTime(timezone=True), nullable=False),
        CheckConstraint("state IN ('active', 'inactive')", name=f"ck_{names['entry_heads']}_state"),
        # Dedup lookup path: same content in the same write-scope must find
        # the existing head (design §2.2 write flow step 2).
        Index(
            f"ix_{names['entry_heads']}_dedup",
            "scope_key",
            "artifact_id",
            "entry_content_hash",
        ),
        Index(f"ix_{names['entry_heads']}_state", "scope_key", "artifact_id", "state"),
        Index(
            f"ix_{names['entry_heads']}_identity",
            "tenant_id",
            "user_id",
            "agent_id",
            "run_id",
            "session_id",
        ),
    )

    Table(
        names["memory_heads"],
        metadata,
        Column("scope_key", String(SCOPE_KEY_LEN), primary_key=True),
        Column("artifact_id", String(UUID_LEN), primary_key=True),
        Column("revision", Integer, nullable=False),
    )

    return metadata
