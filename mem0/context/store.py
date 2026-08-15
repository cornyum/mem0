"""Authoritative context store (design §4–§5).

All mutations are single-transaction and multi-worker safe across processes:

- **CAS**: every write ends with one atomic statement —
  ``UPDATE memory_heads SET revision = revision + 1
  WHERE scope_key = ? AND artifact_id = ? AND revision = :expected``.
  The revision is read (plain SELECT, no lock) at transaction start; if a
  concurrent writer committed first, the WHERE matches zero rows, the
  rowcount check raises :class:`~mem0.context.errors.RevisionConflictError`
  (HTTP 409), and the whole transaction rolls back — including any inserts
  done under the stale read. This is the same single-UPDATE CAS PowerContext
  uses (pc_artifact_heads).
- **One designed retry policy**: unconditional writes
  (``expected_revision=None``) retry a bounded number of times with short
  randomized backoff — under concurrent writers on one scope the losers
  converge (duplicate content becomes a no-op; distinct content lands on a
  later attempt; binding-creation IntegrityErrors re-read the winner's
  binding, since an in-transaction re-read would be a stale snapshot on
  MySQL REPEATABLE READ and an aborted transaction on PostgreSQL).
  Exhaustion surfaces the conflict. Writes with an explicit
  ``expected_revision`` never retry — the conflict is the caller's signal.

Timestamps are timezone-aware UTC. Reference/category lists are stored as
canonical JSON text for dialect-portable semantics.
"""

import json
import random
import time
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import and_, exc, insert, select, update
from sqlalchemy.engine import Engine

from mem0.context import analyzer, tables
from mem0.context.errors import EntryNotFoundError, RevisionConflictError
from mem0.context.hashing import MAX_TEXT_BYTES, entry_content_hash
from mem0.context.scope import SCOPE_FIELDS, ScopeIdentity

ACTIVE = "active"
INACTIVE = "inactive"
NATIVE = "native"
BACKFILL = "backfill"

CREATED = "created"
UPDATED = "updated"
NOOP = "noop"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return str(uuid.uuid4())


@dataclass(frozen=True)
class EntryVersionView:
    entry_id: str
    entry_version_id: str
    version: int
    previous_version_id: Optional[str]
    kind: str
    text: str
    source_refs: list[str]
    artifact_refs: list[str]
    categories: list[str]
    entry_content_hash: str
    created_in_revision: int
    provenance: str
    created_at: datetime


@dataclass(frozen=True)
class RememberOutcome:
    outcome: str  # "created" | "updated" | "noop"
    artifact_id: str
    revision: int
    entry: Optional[EntryVersionView] = None


@dataclass(frozen=True)
class ChangeRecord:
    entry_id: str
    entry_version_id: str
    version: int
    kind: str
    entry_content_hash: str
    created_in_revision: int
    provenance: str
    created_at: datetime
    next_cursor: Optional[int] = None


class ContextStore:
    """Authoritative store for one ctx table family bound to an engine."""

    def __init__(self, engine: Engine, prefix: str = tables.DEFAULT_TABLE_PREFIX):
        self.engine = engine
        self.prefix = prefix
        self.names = tables.build_table_names(prefix)
        self.metadata = tables.build_metadata(prefix)
        self.t_bindings = self.metadata.tables[self.names["bindings"]]
        self.t_versions = self.metadata.tables[self.names["entry_versions"]]
        self.t_heads = self.metadata.tables[self.names["entry_heads"]]
        self.t_memory_heads = self.metadata.tables[self.names["memory_heads"]]

    # -- schema lifecycle ---------------------------------------------------

    def create_tables(self) -> None:
        """Create the ctx family. The server normally goes through alembic
        (which drives the same metadata); this path serves tests and
        standalone SDK deployments."""
        self.metadata.create_all(bind=self.engine)

    # -- write path -----------------------------------------------------------

    def remember_entry(
        self,
        scope: ScopeIdentity,
        *,
        kind: str,
        text: str,
        categories: tuple[str, ...] | list[str] = (),
        source_refs: tuple[str, ...] | list[str] = (),
        artifact_refs: tuple[str, ...] | list[str] = (),
        expected_revision: Optional[int] = None,
        provenance: str = NATIVE,
    ) -> RememberOutcome:
        """Idempotent explicit append (design §5.1). Same content active in
        the scope ⇒ no-op without bumping the revision."""
        return self._run_cas(
            lambda: self._remember_entry_once(
                scope,
                kind=kind,
                text=text,
                categories=categories,
                source_refs=source_refs,
                artifact_refs=artifact_refs,
                expected_revision=expected_revision,
                provenance=provenance,
            ),
            expected_revision,
        )

    def _remember_entry_once(
        self,
        scope: ScopeIdentity,
        *,
        kind: str,
        text: str,
        categories: tuple[str, ...] | list[str] = (),
        source_refs: tuple[str, ...] | list[str] = (),
        artifact_refs: tuple[str, ...] | list[str] = (),
        expected_revision: Optional[int] = None,
        provenance: str = NATIVE,
    ) -> RememberOutcome:
        if not kind or len(kind) > tables.KIND_FIELD_MAX:
            raise ValueError(f"kind must be 1..{tables.KIND_FIELD_MAX} characters")
        if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
            raise ValueError(f"text exceeds {MAX_TEXT_BYTES} UTF-8 bytes")
        content_hash = entry_content_hash(
            kind=kind,
            text=text,
            source_refs=source_refs,
            artifact_refs=artifact_refs,
            categories=categories,
        )
        searchable = analyzer.analyze(text)

        with self.engine.begin() as conn:
            artifact_id = self._get_or_create_binding(conn, scope)
            revision = self._read_memory_revision(conn, scope.scope_key, artifact_id)
            if expected_revision is not None and revision != expected_revision:
                raise RevisionConflictError(
                    scope_key=scope.scope_key,
                    artifact_id=artifact_id,
                    expected_revision=expected_revision,
                    current_revision=revision,
                )

            existing = self._active_head_id_by_hash(conn, scope.scope_key, artifact_id, content_hash)
            if existing is not None:
                # Idempotent no-op: same content already active in this scope.
                entry = self._entry_view(conn, scope.scope_key, artifact_id, existing)
                return RememberOutcome(NOOP, artifact_id, revision, entry)

            entry_id = _new_id()
            entry_version_id = _new_id()
            next_revision = revision + 1
            now = _utcnow()
            conn.execute(
                insert(self.t_versions).values(
                    scope_key=scope.scope_key,
                    artifact_id=artifact_id,
                    entry_id=entry_id,
                    version=1,
                    entry_version_id=entry_version_id,
                    previous_version_id=None,
                    kind=kind,
                    text=text,
                    source_refs=_dump_list(source_refs),
                    artifact_refs=_dump_list(artifact_refs),
                    categories=_dump_list(categories),
                    entry_content_hash=content_hash,
                    created_in_revision=next_revision,
                    provenance=provenance,
                    created_at=now,
                )
            )
            conn.execute(
                insert(self.t_heads).values(
                    **_identity_values(scope),
                    artifact_id=artifact_id,
                    entry_id=entry_id,
                    head_revision=1,
                    entry_version_id=entry_version_id,
                    entry_content_hash=content_hash,
                    state=ACTIVE,
                    searchable_text=searchable,
                    vector_id=None,
                    pending_embed=True,
                    created_at=now,
                    updated_at=now,
                )
            )
            self._bump_memory_revision(conn, scope.scope_key, artifact_id, revision)

        entry = self.get_entry_version(scope, entry_id, entry_version_id)
        return RememberOutcome(CREATED, artifact_id, next_revision, entry)

    def revise_entry(
        self,
        scope: ScopeIdentity,
        entry_id: str,
        *,
        kind: str,
        text: str,
        categories: tuple[str, ...] | list[str] = (),
        source_refs: tuple[str, ...] | list[str] = (),
        artifact_refs: tuple[str, ...] | list[str] = (),
        expected_revision: Optional[int] = None,
    ) -> RememberOutcome:
        """New content version for an entry: version = previous + 1, the old
        version stays queryable forever (design §5.1)."""
        return self._run_cas(
            lambda: self._revise_entry_once(
                scope,
                entry_id,
                kind=kind,
                text=text,
                categories=categories,
                source_refs=source_refs,
                artifact_refs=artifact_refs,
                expected_revision=expected_revision,
            ),
            expected_revision,
        )

    def _revise_entry_once(
        self,
        scope: ScopeIdentity,
        entry_id: str,
        *,
        kind: str,
        text: str,
        categories: tuple[str, ...] | list[str] = (),
        source_refs: tuple[str, ...] | list[str] = (),
        artifact_refs: tuple[str, ...] | list[str] = (),
        expected_revision: Optional[int] = None,
    ) -> RememberOutcome:
        if not kind or len(kind) > tables.KIND_FIELD_MAX:
            raise ValueError(f"kind must be 1..{tables.KIND_FIELD_MAX} characters")
        if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
            raise ValueError(f"text exceeds {MAX_TEXT_BYTES} UTF-8 bytes")
        content_hash = entry_content_hash(
            kind=kind,
            text=text,
            source_refs=source_refs,
            artifact_refs=artifact_refs,
            categories=categories,
        )

        with self.engine.begin() as conn:
            artifact_id = self._require_binding(conn, scope)
            revision = self._read_memory_revision(conn, scope.scope_key, artifact_id)
            if expected_revision is not None and revision != expected_revision:
                raise RevisionConflictError(
                    scope_key=scope.scope_key,
                    artifact_id=artifact_id,
                    expected_revision=expected_revision,
                    current_revision=revision,
                )

            head = self._require_head(conn, scope.scope_key, artifact_id, entry_id)
            if head.entry_content_hash == content_hash and head.state == ACTIVE:
                entry = self._entry_view(conn, scope.scope_key, artifact_id, entry_id)
                return RememberOutcome(NOOP, artifact_id, revision, entry)
            owner = self._active_head_id_by_hash(conn, scope.scope_key, artifact_id, content_hash)
            if owner is not None and owner != entry_id:
                raise RevisionConflictError(
                    scope_key=scope.scope_key,
                    artifact_id=artifact_id,
                    expected_revision=None,
                    current_revision=revision,
                )

            entry_version_id = _new_id()
            next_version = head.head_revision + 1
            next_revision = revision + 1
            now = _utcnow()
            conn.execute(
                insert(self.t_versions).values(
                    scope_key=scope.scope_key,
                    artifact_id=artifact_id,
                    entry_id=entry_id,
                    version=next_version,
                    entry_version_id=entry_version_id,
                    previous_version_id=head.entry_version_id,
                    kind=kind,
                    text=text,
                    source_refs=_dump_list(source_refs),
                    artifact_refs=_dump_list(artifact_refs),
                    categories=_dump_list(categories),
                    entry_content_hash=content_hash,
                    created_in_revision=next_revision,
                    provenance=NATIVE,
                    created_at=now,
                )
            )
            conn.execute(
                update(self.t_heads)
                .where(
                    and_(
                        self.t_heads.c.scope_key == scope.scope_key,
                        self.t_heads.c.artifact_id == artifact_id,
                        self.t_heads.c.entry_id == entry_id,
                    )
                )
                .values(
                    head_revision=next_version,
                    entry_version_id=entry_version_id,
                    entry_content_hash=content_hash,
                    searchable_text=analyzer.analyze(text),
                    pending_embed=True,
                    updated_at=now,
                )
            )
            self._bump_memory_revision(conn, scope.scope_key, artifact_id, revision)

        entry = self.get_entry_version(scope, entry_id, entry_version_id)
        return RememberOutcome(CREATED, artifact_id, next_revision, entry)

    def set_entry_state(
        self,
        scope: ScopeIdentity,
        entry_id: str,
        *,
        active: bool,
        expected_revision: Optional[int] = None,
    ) -> RememberOutcome:
        """retire (active=False) / reactivate (active=True). A state flip
        bumps the memory revision (manifest change) but never creates a
        content version (design ADR-5); both directions are idempotent."""
        return self._run_cas(
            lambda: self._set_entry_state_once(
                scope,
                entry_id,
                active=active,
                expected_revision=expected_revision,
            ),
            expected_revision,
        )

    def _set_entry_state_once(
        self,
        scope: ScopeIdentity,
        entry_id: str,
        *,
        active: bool,
        expected_revision: Optional[int] = None,
    ) -> RememberOutcome:
        target = ACTIVE if active else INACTIVE
        with self.engine.begin() as conn:
            artifact_id = self._require_binding(conn, scope)
            revision = self._read_memory_revision(conn, scope.scope_key, artifact_id)
            if expected_revision is not None and revision != expected_revision:
                raise RevisionConflictError(
                    scope_key=scope.scope_key,
                    artifact_id=artifact_id,
                    expected_revision=expected_revision,
                    current_revision=revision,
                )

            head = self._require_head(conn, scope.scope_key, artifact_id, entry_id)
            if head.state == target:
                entry = self._entry_view(conn, scope.scope_key, artifact_id, entry_id)
                return RememberOutcome(NOOP, artifact_id, revision, entry)

            if active and head.state == INACTIVE:
                # Re-activating retired content: if another active entry now
                # owns the same hash, the flip is a no-op pointing at it —
                # at most one active entry per content hash per artifact.
                owner = self._active_head_id_by_hash(conn, scope.scope_key, artifact_id, head.entry_content_hash)
                if owner is not None and owner != entry_id:
                    entry = self._entry_view(conn, scope.scope_key, artifact_id, owner)
                    return RememberOutcome(NOOP, artifact_id, revision, entry)

            next_revision = revision + 1
            conn.execute(
                update(self.t_heads)
                .where(
                    and_(
                        self.t_heads.c.scope_key == scope.scope_key,
                        self.t_heads.c.artifact_id == artifact_id,
                        self.t_heads.c.entry_id == entry_id,
                    )
                )
                .values(state=target, updated_at=_utcnow())
            )
            self._bump_memory_revision(conn, scope.scope_key, artifact_id, revision)

        entry = self.get_entry_version(scope, entry_id, head.entry_version_id)
        return RememberOutcome(UPDATED, artifact_id, next_revision, entry)

    # -- read path --------------------------------------------------------------

    def get_entry_version(self, scope: ScopeIdentity, entry_id: str, entry_version_id: str) -> EntryVersionView:
        with self.engine.connect() as conn:
            artifact_id = self._require_binding(conn, scope)
            return self._entry_version_view(conn, scope.scope_key, artifact_id, entry_id, entry_version_id)

    def get_head(self, scope: ScopeIdentity, entry_id: str) -> dict[str, Any]:
        with self.engine.connect() as conn:
            artifact_id = self._require_binding(conn, scope)
            row = (
                conn.execute(
                    select(self.t_heads).where(
                        and_(
                            self.t_heads.c.scope_key == scope.scope_key,
                            self.t_heads.c.artifact_id == artifact_id,
                            self.t_heads.c.entry_id == entry_id,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise EntryNotFoundError(f"Entry {entry_id} not found in scope")
            return dict(row)

    def list_changes(
        self,
        scope: ScopeIdentity,
        *,
        since_revision: int = 0,
        limit: int = 200,
        cursor: Optional[int] = None,
    ) -> list[ChangeRecord]:
        """Version-chain tail after ``since_revision``/``cursor``, ordered by
        revision then entry id; the last record carries ``next_cursor`` when
        more pages remain."""
        floor = max(since_revision, cursor or 0)
        with self.engine.connect() as conn:
            artifact_id = self._require_binding(conn, scope)
            rows = (
                conn.execute(
                    select(self.t_versions)
                    .where(
                        and_(
                            self.t_versions.c.scope_key == scope.scope_key,
                            self.t_versions.c.artifact_id == artifact_id,
                            self.t_versions.c.created_in_revision > floor,
                        )
                    )
                    .order_by(self.t_versions.c.created_in_revision, self.t_versions.c.entry_id)
                    .limit(limit + 1)
                )
                .mappings()
                .all()
            )
        has_more = len(rows) > limit
        rows = rows[:limit]
        records = [
            ChangeRecord(
                entry_id=row.entry_id,
                entry_version_id=row.entry_version_id,
                version=row.version,
                kind=row.kind,
                entry_content_hash=row.entry_content_hash,
                created_in_revision=row.created_in_revision,
                provenance=row.provenance,
                created_at=row.created_at,
            )
            for row in rows
        ]
        if records and has_more:
            records[-1] = replace(records[-1], next_cursor=records[-1].created_in_revision)
        return records

    def find_heads(
        self,
        scope: ScopeIdentity,
        *,
        state: Optional[str] = ACTIVE,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Heads for recall merging: identity columns matched on every id the
        caller supplied (mem0 subset semantics, D6) — absent ids unconstrained."""
        clauses = [getattr(self.t_heads.c, name) == value for name, value in scope.fields.items()]
        if state is not None:
            clauses.append(self.t_heads.c.state == state)
        with self.engine.connect() as conn:
            return [
                dict(row)
                for row in conn.execute(select(self.t_heads).where(and_(*clauses)).limit(limit)).mappings().all()
            ]

    def iter_pending_embed(self, *, limit: int = 500) -> list[dict[str, Any]]:
        """Heads whose vector projection is stale — input for reconciliation."""
        with self.engine.connect() as conn:
            return [
                dict(row)
                for row in conn.execute(select(self.t_heads).where(self.t_heads.c.pending_embed).limit(limit))
                .mappings()
                .all()
            ]

    def pending_entries_with_text(
        self, scope: ScopeIdentity, *, limit: int = 500
    ) -> list[dict[str, Any]]:
        """Active heads still awaiting their vector projection, joined with
        the authoritative text of their head version — the merge source for
        recall's read-your-writes guarantee (design §2.2). The pending set
        is bounded by reconciliation, so a plain bounded scan is the right
        shape; no dialect-specific FTS SQL is needed for correctness."""
        clauses = [
            getattr(self.t_heads.c, name) == value for name, value in scope.fields.items()
        ]
        clauses.extend([self.t_heads.c.state == ACTIVE, self.t_heads.c.pending_embed])
        join_on = (
            (self.t_versions.c.scope_key == self.t_heads.c.scope_key)
            & (self.t_versions.c.artifact_id == self.t_heads.c.artifact_id)
            & (self.t_versions.c.entry_id == self.t_heads.c.entry_id)
            & (self.t_versions.c.entry_version_id == self.t_heads.c.entry_version_id)
        )
        with self.engine.connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    select(
                        self.t_heads.c.entry_id,
                        self.t_heads.c.entry_version_id,
                        self.t_heads.c.entry_content_hash,
                        self.t_heads.c.searchable_text,
                        self.t_versions.c.kind,
                        self.t_versions.c.text,
                        self.t_versions.c.categories,
                    )
                    .select_from(self.t_heads.join(self.t_versions, join_on))
                    .where(and_(*clauses))
                    .limit(limit)
                )
                .mappings()
                .all()
            ]

    # -- projection binding (post-commit, separate transaction) -----------------

    def bind_vector(
        self,
        scope: ScopeIdentity,
        entry_id: str,
        *,
        vector_id: Optional[str],
        pending_embed: bool,
        expected_entry_version_id: Optional[str] = None,
    ) -> None:
        """Bind the vector projection pointer post-commit. The optional
        ``expected_entry_version_id`` guard prevents a stale projection
        write (started before a concurrent revise) from clearing the
        pending flag of a newer version: zero rows matched means the head
        moved on and the reconciliation pass owns the projection now."""
        clauses = [
            self.t_heads.c.scope_key == scope.scope_key,
            self.t_heads.c.artifact_id == self._binding_artifact_id_or_none(scope),
            self.t_heads.c.entry_id == entry_id,
        ]
        if expected_entry_version_id is not None:
            clauses.append(self.t_heads.c.entry_version_id == expected_entry_version_id)
        with self.engine.begin() as conn:
            conn.execute(
                update(self.t_heads)
                .where(and_(*clauses))
                .values(vector_id=vector_id, pending_embed=pending_embed, updated_at=_utcnow())
            )

    def claim_pending(
        self, scope: ScopeIdentity, entry_id: str, entry_version_id: str
    ) -> bool:
        """Reconciliation claim (design §5.2): flip ``pending_embed`` to
        False with a conditional UPDATE — exactly one concurrent worker
        wins per entry; losers see rowcount 0 and skip. On projection
        failure the winner releases via :meth:`bind_vector` with
        ``pending_embed=True``."""
        clauses = [
            self.t_heads.c.scope_key == scope.scope_key,
            self.t_heads.c.artifact_id == self._binding_artifact_id_or_none(scope),
            self.t_heads.c.entry_id == entry_id,
            self.t_heads.c.entry_version_id == entry_version_id,
            self.t_heads.c.pending_embed == True,  # noqa: E712 — SQL boolean
        ]
        with self.engine.begin() as conn:
            result = conn.execute(
                update(self.t_heads).where(and_(*clauses)).values(pending_embed=False)
            )
            return result.rowcount == 1

    def get_head_by_vector_id(self, vector_id: str) -> Optional[dict[str, Any]]:
        """Reverse lookup for dual-write wiring (design §5.4): find the
        authoritative head bound to a legacy vector row id."""
        with self.engine.connect() as conn:
            row = (
                conn.execute(
                    select(self.t_heads).where(self.t_heads.c.vector_id == vector_id).limit(1)
                )
                .mappings()
                .first()
            )
            return dict(row) if row else None

    def get_artifact_id(self, scope: ScopeIdentity) -> str:
        """The artifact bound to this write-scope — used by expand() to
        validate the citation's artifact reference."""
        artifact_id = self._binding_artifact_id_or_none(scope)
        if artifact_id is None:
            raise EntryNotFoundError(f"No memory artifact bound for scope {scope.fields!r}")
        return artifact_id

    def _binding_artifact_id_or_none(self, scope: ScopeIdentity) -> Optional[str]:
        with self.engine.connect() as conn:
            return conn.execute(
                select(self.t_bindings.c.artifact_id).where(self.t_bindings.c.scope_key == scope.scope_key)
            ).scalar_one_or_none()

    # -- internals ----------------------------------------------------------------

    def _run_cas(self, operation, expected_revision: Optional[int]):
        """Unconditional writes (expected_revision=None) retry a bounded
        number of times with short randomized backoff: under a cold-start
        stampede on one scope (many workers, first writes) a single retry
        still loses races, and the idempotent-remember contract (design
        §5.1) wants convergence, not 409s. The bound keeps genuine
        contention visible eventually. An explicit CAS expectation is
        never retried — the conflict is the caller's signal."""
        attempts = 1 if expected_revision is not None else 4
        for attempt in range(attempts):
            try:
                return operation()
            except (RevisionConflictError, exc.IntegrityError):
                if attempt + 1 == attempts:
                    raise
                time.sleep(random.uniform(0.005, 0.020))

    def _get_or_create_binding(self, conn, scope: ScopeIdentity) -> str:
        row = conn.execute(
            select(self.t_bindings.c.artifact_id).where(self.t_bindings.c.scope_key == scope.scope_key)
        ).scalar_one_or_none()
        if row is not None:
            return row
        artifact_id = _new_id()
        conn.execute(
            insert(self.t_bindings).values(
                **_identity_values(scope),
                artifact_id=artifact_id,
                created_at=_utcnow(),
            )
        )
        conn.execute(insert(self.t_memory_heads).values(scope_key=scope.scope_key, artifact_id=artifact_id, revision=0))
        return artifact_id
        # A concurrent binding creation surfaces as IntegrityError from this
        # transaction; it is retried at the boundary by _run_cas (see module
        # docstring) — an in-transaction re-read would be stale/aborted on
        # the production dialects.

    def _require_binding(self, conn, scope: ScopeIdentity) -> str:
        row = conn.execute(
            select(self.t_bindings.c.artifact_id).where(self.t_bindings.c.scope_key == scope.scope_key)
        ).scalar_one_or_none()
        if row is None:
            raise EntryNotFoundError(f"No memory artifact bound for scope {scope.fields!r}")
        return row

    def _read_memory_revision(self, conn, scope_key: str, artifact_id: str) -> int:
        """Read the current CAS revision. Plain SELECT — concurrency is
        resolved by the conditional UPDATE at the end of the transaction,
        so no pessimistic lock is taken here."""
        return conn.execute(
            select(self.t_memory_heads.c.revision).where(
                and_(
                    self.t_memory_heads.c.scope_key == scope_key,
                    self.t_memory_heads.c.artifact_id == artifact_id,
                )
            )
        ).scalar_one()

    def _bump_memory_revision(self, conn, scope_key: str, artifact_id: str, expected: int) -> None:
        """The serialization point of every write: one atomic conditional
        UPDATE. Zero rows matched ⇒ a concurrent writer committed first ⇒
        RevisionConflictError and full transaction rollback."""
        result = conn.execute(
            update(self.t_memory_heads)
            .where(
                and_(
                    self.t_memory_heads.c.scope_key == scope_key,
                    self.t_memory_heads.c.artifact_id == artifact_id,
                    self.t_memory_heads.c.revision == expected,
                )
            )
            .values(revision=self.t_memory_heads.c.revision + 1)
        )
        if result.rowcount != 1:
            raise RevisionConflictError(scope_key=scope_key, artifact_id=artifact_id, expected_revision=expected)

    def _active_head_id_by_hash(self, conn, scope_key: str, artifact_id: str, content_hash: str):
        return conn.execute(
            select(self.t_heads.c.entry_id)
            .where(
                and_(
                    self.t_heads.c.scope_key == scope_key,
                    self.t_heads.c.artifact_id == artifact_id,
                    self.t_heads.c.entry_content_hash == content_hash,
                    self.t_heads.c.state == ACTIVE,
                )
            )
            .limit(1)
        ).scalar_one_or_none()

    def _require_head(self, conn, scope_key: str, artifact_id: str, entry_id: str) -> "_HeadRef":
        row = (
            conn.execute(
                select(
                    self.t_heads.c.entry_id,
                    self.t_heads.c.head_revision,
                    self.t_heads.c.entry_version_id,
                    self.t_heads.c.entry_content_hash,
                    self.t_heads.c.state,
                ).where(
                    and_(
                        self.t_heads.c.scope_key == scope_key,
                        self.t_heads.c.artifact_id == artifact_id,
                        self.t_heads.c.entry_id == entry_id,
                    )
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise EntryNotFoundError(f"Entry {entry_id} not found in scope")
        return _HeadRef(
            entry_id=row.entry_id,
            head_revision=row.head_revision,
            entry_version_id=row.entry_version_id,
            entry_content_hash=row.entry_content_hash,
            state=row.state,
        )

    def _entry_view(self, conn, scope_key: str, artifact_id: str, entry_id: str) -> EntryVersionView:
        row = (
            conn.execute(
                select(self.t_versions)
                .where(
                    and_(
                        self.t_versions.c.scope_key == scope_key,
                        self.t_versions.c.artifact_id == artifact_id,
                        self.t_versions.c.entry_id == entry_id,
                    )
                )
                .order_by(self.t_versions.c.version.desc())
                .limit(1)
            )
            .mappings()
            .one()
        )
        return _row_to_view(row)

    def _entry_version_view(
        self, conn, scope_key: str, artifact_id: str, entry_id: str, entry_version_id: str
    ) -> EntryVersionView:
        row = (
            conn.execute(
                select(self.t_versions).where(
                    and_(
                        self.t_versions.c.scope_key == scope_key,
                        self.t_versions.c.artifact_id == artifact_id,
                        self.t_versions.c.entry_id == entry_id,
                        self.t_versions.c.entry_version_id == entry_version_id,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise EntryNotFoundError(f"Entry version {entry_version_id} not found for entry {entry_id}")
        return _row_to_view(row)


@dataclass(frozen=True)
class _HeadRef:
    entry_id: str
    head_revision: int = 0
    entry_version_id: Optional[str] = None
    entry_content_hash: Optional[str] = None
    state: Optional[str] = None


def _row_to_view(row) -> EntryVersionView:
    return EntryVersionView(
        entry_id=row.entry_id,
        entry_version_id=row.entry_version_id,
        version=row.version,
        previous_version_id=row.previous_version_id,
        kind=row.kind,
        text=row.text,
        source_refs=json.loads(row.source_refs),
        artifact_refs=json.loads(row.artifact_refs),
        categories=json.loads(row.categories),
        entry_content_hash=row.entry_content_hash,
        created_in_revision=row.created_in_revision,
        provenance=row.provenance,
        created_at=row.created_at,
    )


def _identity_values(scope: ScopeIdentity) -> dict[str, Any]:
    values = {name: scope.fields.get(name) for name in SCOPE_FIELDS}
    values["scope_key"] = scope.scope_key
    return values


def _dump_list(values) -> str:
    return json.dumps(sorted(str(value) for value in values))
