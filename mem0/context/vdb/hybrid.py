"""HYBRID_STORAGE SQL sidecar (design §8): the minimal recall copy.

Two tables only — ``memory_recall_head`` (per-entry searchable copy) and
``memory_recall_checkpoint`` (per-scope revision watermark). The
FallbackReconciler replays ES ``event`` documents in revision order; the
checkpoint and the head rows commit in ONE SQL transaction. The copy is
asynchronous: ES writes never wait for it and its failures never fail an ES
write. FTS fallback search answers only with a historical snapshot declared
via ``as_of_revision`` (design §6.3).
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

from sqlalchemy import text
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)

IDENTITY_COLUMNS = ("tenant_id", "user_id", "agent_id", "run_id", "session_id")


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")


class HybridSidecar:
    """SQL recall copy + FTS fallback over the deployment app DB engine."""

    def __init__(self, engine: Engine, *, table_prefix: str, ensure_schema: bool = True):
        self.engine = engine
        self.head_table = f"{table_prefix}memory_recall_head"
        self.checkpoint_table = f"{table_prefix}memory_recall_checkpoint"
        self.dialect = engine.dialect.name  # mysql | postgresql | sqlite
        if ensure_schema:
            self.ensure_schema()

    # -- schema (design §8) -------------------------------------------------------

    def ensure_schema(self) -> None:
        head_ddl = self._head_ddl()
        checkpoint_ddl = f"""
        CREATE TABLE IF NOT EXISTS {self.checkpoint_table} (
            scope_key VARCHAR(64) PRIMARY KEY,
            published_revision BIGINT NOT NULL DEFAULT 0,
            as_of_at DATETIME(6) NULL,
            last_verified_at DATETIME(6) NULL
        )
        """
        with self.engine.begin() as conn:
            conn.execute(text(head_ddl))
            conn.execute(text(checkpoint_ddl))
            if self.dialect == "mysql":
                # Idempotent DDL: MySQL has no CREATE FULLTEXT IF NOT EXISTS and
                # a duplicate CREATE waits on the metadata lock held by other
                # pooled connections — check first, create only when missing.
                existing = conn.execute(
                    text(
                        f"SELECT COUNT(*) FROM information_schema.statistics "
                        f"WHERE table_schema = DATABASE() AND table_name = '{self.head_table}' "
                        f"AND index_type = 'FULLTEXT'"
                    )
                ).scalar()
                if not existing:
                    conn.execute(
                        text(
                            f"CREATE FULLTEXT INDEX ft_{self.head_table[:40]}_searchable "
                            f"ON {self.head_table} (searchable_text)"
                        )
                    )
            elif self.dialect == "postgresql":
                conn.execute(
                    text(
                        f"CREATE INDEX IF NOT EXISTS ix_{self.head_table[:50]}_fts "
                        f"ON {self.head_table} (to_tsvector('simple', searchable_text))"
                    )
                )

    def _head_ddl(self) -> str:
        if self.dialect != "mysql":  # postgresql, sqlite (tests): portable DDL
            return f"""
            CREATE TABLE IF NOT EXISTS {self.head_table} (
                scope_key VARCHAR(64) NOT NULL,
                tenant_id VARCHAR(256), user_id VARCHAR(256), agent_id VARCHAR(256),
                run_id VARCHAR(256), session_id VARCHAR(256),
                entry_id VARCHAR(64) NOT NULL,
                entry_version_id VARCHAR(64) NOT NULL,
                state VARCHAR(16) NOT NULL,
                text TEXT NOT NULL,
                searchable_text TEXT NOT NULL,
                kind VARCHAR(64),
                categories TEXT,
                scope_revision BIGINT NOT NULL,
                updated_at TIMESTAMP NULL,
                PRIMARY KEY (scope_key, entry_id)
            )
            """
        return f"""
        CREATE TABLE IF NOT EXISTS {self.head_table} (
            scope_key VARCHAR(64) NOT NULL,
            tenant_id VARCHAR(256) NULL, user_id VARCHAR(256) NULL, agent_id VARCHAR(256) NULL,
            run_id VARCHAR(256) NULL, session_id VARCHAR(256) NULL,
            entry_id VARCHAR(64) NOT NULL,
            entry_version_id VARCHAR(64) NOT NULL,
            state VARCHAR(16) NOT NULL,
            text MEDIUMTEXT NOT NULL,
            searchable_text TEXT NOT NULL,
            kind VARCHAR(64) NULL,
            categories VARCHAR(1024) NULL,
            scope_revision BIGINT NOT NULL,
            updated_at DATETIME(6) NULL,
            PRIMARY KEY (scope_key, entry_id)
        ) CHARACTER SET utf8mb4
        """

    def healthy(self) -> bool:
        try:
            with self.engine.connect() as conn:
                conn.execute(text(f"SELECT 1 FROM {self.checkpoint_table} LIMIT 1"))
            return True
        except Exception:
            return False

    # -- FallbackReconciler (design §8) ---------------------------------------------

    def sync_all(self, store, *, batch: int = 200) -> Dict[str, int]:
        totals = {"scopes": 0, "events_applied": 0, "rows_upserted": 0, "rows_deleted": 0}
        for scope in store.scan_all_scopes(limit=1000):
            counts = self.sync_scope(store, scope.scope_key, batch=batch)
            if counts["events_applied"] or counts["rows_upserted"] or counts["rows_deleted"]:
                totals["scopes"] += 1
                for key in ("events_applied", "rows_upserted", "rows_deleted"):
                    totals[key] += counts[key]
        return totals

    def sync_scope(self, store, scope_key: str, *, batch: int = 200) -> Dict[str, int]:
        """Replay events above the checkpoint; checkpoint + head rows commit
        atomically (design §8)."""
        counts = {"events_applied": 0, "rows_upserted": 0, "rows_deleted": 0}
        while True:
            checkpoint = self._get_checkpoint(scope_key)
            scope = store.get_scope(scope_key)
            target_revision = scope.published_revision if scope else checkpoint
            if checkpoint >= target_revision:
                return counts
            events = store.list_events(scope_key, since_revision=checkpoint, limit=batch)
            if not events:
                # ES is authoritative: no events but a higher watermark means
                # events were trimmed; just advance the checkpoint.
                self._advance_checkpoint(scope_key, target_revision)
                return counts

            touched: Dict[str, str] = {}
            for event in events:
                counts["events_applied"] += 1
                entry_id = event.get("entry_id")
                if event.get("event_type") == "purged":
                    touched[entry_id] = "purged"
                else:
                    touched[entry_id] = event.get("state_after") or "active"
            last_revision = int(events[-1]["scope_revision"])

            with self.engine.begin() as conn:
                for entry_id, state in touched.items():
                    if state == "purged":
                        conn.execute(
                            text(
                                f"DELETE FROM {self.head_table} "
                                f"WHERE scope_key = :scope_key AND entry_id = :entry_id"
                            ),
                            {"scope_key": scope_key, "entry_id": entry_id},
                        )
                        counts["rows_deleted"] += 1
                        continue
                    head = store.get_head(scope_key, entry_id)
                    if head is None:
                        continue
                    conn.execute(
                        text(self._upsert_sql()),
                        self._head_row(head),
                    )
                    counts["rows_upserted"] += 1
                conn.execute(
                    text(
                        f"INSERT INTO {self.checkpoint_table} (scope_key, published_revision, as_of_at, last_verified_at) "
                        f"VALUES (:scope_key, :revision, :now, :now) "
                        f"ON DUPLICATE KEY UPDATE published_revision = :revision, as_of_at = :now, last_verified_at = :now"
                        if self.dialect == "mysql"
                        else f"INSERT INTO {self.checkpoint_table} (scope_key, published_revision, as_of_at, last_verified_at) "
                        f"VALUES (:scope_key, :revision, :now, :now) "
                        f"ON CONFLICT (scope_key) DO UPDATE SET published_revision = :revision, as_of_at = :now, last_verified_at = :now"
                    ),
                    {"scope_key": scope_key, "revision": last_revision, "now": _now()},
                )
            if len(events) < batch:
                return counts

    def _upsert_sql(self) -> str:
        if self.dialect == "mysql":
            return (
                f"INSERT INTO {self.head_table} (scope_key, tenant_id, user_id, agent_id, run_id, session_id, "
                f"entry_id, entry_version_id, state, text, searchable_text, kind, categories, scope_revision, updated_at) "
                f"VALUES (:scope_key, :tenant_id, :user_id, :agent_id, :run_id, :session_id, :entry_id, "
                f":entry_version_id, :state, :text, :searchable_text, :kind, :categories, :scope_revision, :updated_at) "
                f"ON DUPLICATE KEY UPDATE entry_version_id=VALUES(entry_version_id), state=VALUES(state), "
                f"text=VALUES(text), searchable_text=VALUES(searchable_text), kind=VALUES(kind), "
                f"categories=VALUES(categories), scope_revision=VALUES(scope_revision), updated_at=VALUES(updated_at)"
            )
        return (
            f"INSERT INTO {self.head_table} (scope_key, tenant_id, user_id, agent_id, run_id, session_id, "
            f"entry_id, entry_version_id, state, text, searchable_text, kind, categories, scope_revision, updated_at) "
            f"VALUES (:scope_key, :tenant_id, :user_id, :agent_id, :run_id, :session_id, :entry_id, "
            f":entry_version_id, :state, :text, :searchable_text, :kind, :categories, :scope_revision, :updated_at) "
            f"ON CONFLICT (scope_key, entry_id) DO UPDATE SET entry_version_id=EXCLUDED.entry_version_id, "
            f"state=EXCLUDED.state, text=EXCLUDED.text, searchable_text=EXCLUDED.searchable_text, "
            f"kind=EXCLUDED.kind, categories=EXCLUDED.categories, scope_revision=EXCLUDED.scope_revision, "
            f"updated_at=EXCLUDED.updated_at"
        )

    @staticmethod
    def _head_row(head: dict) -> dict:
        return {
            "scope_key": head.get("scope_key"),
            **{col: head.get(col) for col in IDENTITY_COLUMNS},
            "entry_id": head.get("entry_id"),
            "entry_version_id": head.get("entry_version_id"),
            "state": head.get("state") or "active",
            "text": head.get("text") or "",
            "searchable_text": head.get("searchable_text") or head.get("text") or "",
            "kind": head.get("kind"),
            "categories": json.dumps(head.get("categories") or [], ensure_ascii=False),
            "scope_revision": int(head.get("scope_revision", 0)),
            "updated_at": _now(),
        }

    def _get_checkpoint(self, scope_key: str) -> int:
        with self.engine.connect() as conn:
            row = conn.execute(
                text(
                    f"SELECT published_revision FROM {self.checkpoint_table} WHERE scope_key = :scope_key"
                ),
                {"scope_key": scope_key},
            ).fetchone()
        return int(row[0]) if row else 0

    def _advance_checkpoint(self, scope_key: str, revision: int) -> None:
        upsert = (
            f"INSERT INTO {self.checkpoint_table} (scope_key, published_revision, as_of_at, last_verified_at) "
            f"VALUES (:scope_key, :revision, :now, :now) "
            f"ON DUPLICATE KEY UPDATE published_revision = :revision, as_of_at = :now, last_verified_at = :now"
            if self.dialect == "mysql"
            else f"INSERT INTO {self.checkpoint_table} (scope_key, published_revision, as_of_at, last_verified_at) "
            f"VALUES (:scope_key, :revision, :now, :now) "
            f"ON CONFLICT (scope_key) DO UPDATE SET published_revision = :revision, as_of_at = :now, last_verified_at = :now"
        )
        with self.engine.begin() as conn:
            conn.execute(text(upsert), {"scope_key": scope_key, "revision": revision, "now": _now()})

    # -- FTS fallback search (design §6.3) ------------------------------------------

    def fts_search(
        self, query: str, *, identity_filters: Dict[str, str], limit: int
    ) -> Tuple[List[Dict[str, Any]], int]:
        """Keyword-ish FTS over the recall copy. Returns (hits, as_of_revision)
        where as_of_revision is the MIN checkpoint across matched scopes — the
        conservative freshness floor of the snapshot."""
        where = ["state = 'active'"]
        params: Dict[str, Any] = {"limit": limit}
        for column in IDENTITY_COLUMNS:
            if identity_filters.get(column):
                where.append(f"{column} = :f_{column}")
                params[f"f_{column}"] = identity_filters[column]

        # searchable_text stores the lemmatized-token projection (mirrors the
        # ES head field), so the FTS query must be tokenized the same way —
        # this also makes stock MySQL FULLTEXT (no ngram parser) work for CJK.
        from mem0.utils.lemmatization import lemmatize_for_bm25

        tokenized_query = lemmatize_for_bm25(query) or query

        if self.dialect == "mysql":
            where.append("MATCH(searchable_text) AGAINST (:query IN NATURAL LANGUAGE MODE)")
            select = (
                f"SELECT scope_key, tenant_id, user_id, agent_id, run_id, session_id, entry_id, "
                f"entry_version_id, kind, text, categories, scope_revision, "
                f"MATCH(searchable_text) AGAINST (:query IN NATURAL LANGUAGE MODE) AS score "
                f"FROM {self.head_table} WHERE "
            )
            order = " ORDER BY score DESC LIMIT :limit"
        elif self.dialect == "postgresql":
            where.append("to_tsvector('simple', searchable_text) @@ plainto_tsquery('simple', :query)")
            select = (
                f"SELECT scope_key, tenant_id, user_id, agent_id, run_id, session_id, entry_id, "
                f"entry_version_id, kind, text, categories, scope_revision, "
                f"ts_rank(to_tsvector('simple', searchable_text), plainto_tsquery('simple', :query)) AS score "
                f"FROM {self.head_table} WHERE "
            )
            order = " ORDER BY score DESC LIMIT :limit"
        else:  # sqlite and other dialects: portable token-substring fallback (unit tests)
            tokens = [t for t in tokenized_query.split() if t][:8] or [query]
            like_clauses = []
            for i, token in enumerate(tokens):
                where.append(f"searchable_text LIKE :like_{i}")
                params[f"like_{i}"] = f"%{token}%"
                like_clauses.append(where.pop())
            where.append("(" + " OR ".join(like_clauses) + ")")
            select = (
                f"SELECT scope_key, tenant_id, user_id, agent_id, run_id, session_id, entry_id, "
                f"entry_version_id, kind, text, categories, scope_revision, "
                f"1.0 AS score FROM {self.head_table} WHERE "
            )
            order = " ORDER BY entry_id LIMIT :limit"
        params["query"] = tokenized_query

        with self.engine.connect() as conn:
            rows = conn.execute(
                text(select + " AND ".join(where) + order), params
            ).fetchall()
            if rows:
                scope_keys = [row[0] for row in rows]
                placeholders = ", ".join(f":s{i}" for i in range(len(scope_keys)))
                ck_params = {f"s{i}": key for i, key in enumerate(scope_keys)}
                ck_rows = conn.execute(
                    text(
                        f"SELECT MIN(published_revision) FROM {self.checkpoint_table} "
                        f"WHERE scope_key IN ({placeholders})"
                    ),
                    ck_params,
                ).fetchone()
                as_of = int(ck_rows[0]) if ck_rows and ck_rows[0] is not None else 0
            else:
                as_of = 0

        hits = []
        columns = [
            "scope_key",
            "tenant_id",
            "user_id",
            "agent_id",
            "run_id",
            "session_id",
            "entry_id",
            "entry_version_id",
            "kind",
            "text",
            "categories",
            "scope_revision",
            "score",
        ]
        for row in rows:
            record = dict(zip(columns, row))
            try:
                categories = json.loads(record.get("categories") or "[]")
            except (json.JSONDecodeError, TypeError):
                categories = []
            hit: Dict[str, Any] = {
                "entry_id": record["entry_id"],
                "entry_version_id": record["entry_version_id"],
                "kind": record["kind"],
                "text": record["text"],
                "score": float(record.get("score") or 0.0),
                "matched_by": ["fts_sidecar"],
                "categories": categories,
                "scope_key": record["scope_key"],
                "scope_revision": record.get("scope_revision"),
            }
            for column in IDENTITY_COLUMNS:
                if record.get(column):
                    hit[column] = record[column]
            hits.append(hit)
        return hits, as_of
