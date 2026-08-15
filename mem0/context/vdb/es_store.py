"""ElasticsearchMemoryStore: the ES authority document family (design §4).

Five logical record families live in five indices addressed through aliases
(appendix A): ``scope`` (CAS + publish watermark), ``head`` (recall surface),
``version`` (immutable content), ``event`` (changes cursor), ``dedup``
(concurrent same-content claims). Only the scope document carries concurrency
control; everything else is deterministic-``_id`` rebuildable data (ADR-2/3).

All exact scope-bound reads/writes route by ``scope_key`` (design §4.2);
subset recall filters on the identity columns without routing.

The store raises the underlying elasticsearch exceptions; callers classify
them via :func:`classify_elasticsearch_error` (§7.5) — except CAS/dedup 409s,
which surface as :class:`EsCasConflict` / :class:`EsDocExists` so the write
protocol can treat them as domain events, not transport failures.
"""

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from mem0.context.scope import SCOPE_FIELDS
from mem0.context.vdb.errors import (
    ES_BAD_REQUEST,
    ES_CONFLICT,
    ES_THROTTLED,
    ES_TIMEOUT,
    ES_UNAVAILABLE,
    PrimaryUnavailableError,
    classify_elasticsearch_error,
)

logger = logging.getLogger(__name__)

FAMILIES = ("scope", "head", "version", "event", "dedup")

# Deterministic _id scheme (design §4.1)
def scope_doc_id(scope_key: str) -> str:
    return f"s:{scope_key}"


def head_doc_id(scope_key: str, entry_id: str) -> str:
    return f"h:{scope_key}:{entry_id}"


def version_doc_id(scope_key: str, entry_id: str, version: int) -> str:
    return f"v:{scope_key}:{entry_id}:{version}"


def event_doc_id(scope_key: str, revision: int) -> str:
    return f"e:{scope_key}:{revision}"


def dedup_doc_id(scope_key: str, dedup_key: str) -> str:
    return f"d:{scope_key}:{dedup_key}"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class EsCasConflict(Exception):
    """The scope conditional update lost the CAS race (expected domain 409)."""


class EsDocExists(Exception):
    """op_type=create hit an existing _id (expected dedup/scope 409)."""

    def __init__(self, existing: Optional[dict] = None):
        super().__init__("document already exists")
        self.existing = existing


@dataclass
class ScopeDoc:
    scope_key: str
    artifact_id: str
    published_revision: int
    last_event_type: Optional[str] = None
    last_entry_id: Optional[str] = None
    last_entry_version_id: Optional[str] = None
    seq_no: int = 0
    primary_term: int = 1
    fields: Dict[str, str] = field(default_factory=dict)
    updated_at: Optional[str] = None

    def identity(self) -> Dict[str, str]:
        return dict(self.fields)


class ElasticsearchMemoryStore:
    """Read/write access to the five ES authority document families."""

    def __init__(
        self,
        client,
        *,
        prefix: str = "agentar_mem0",
        dims: int = 1024,
        ensure_indices: bool = True,
    ):
        self.client = client
        self.prefix = prefix
        self.dims = dims
        self._ik = False
        if ensure_indices:
            self.ensure_indices()

    # -- index bootstrap ------------------------------------------------------

    def alias(self, family: str) -> str:
        return f"{self.prefix}_{family}"

    def _index_name(self, family: str) -> str:
        return f"{self.prefix}_{family}_v1"

    def detect_ik(self) -> bool:
        try:
            self.client.indices.analyze(analyzer="ik_max_word", text="插件探测")
            return True
        except Exception:
            return False

    def ensure_indices(self) -> None:
        """Create the five backing indices + aliases (idempotent, appendix A)."""
        self._ik = self.detect_ik()
        if not self._ik:
            logger.info("analysis-ik unavailable; head.text_zh omitted from mappings")
        for family in FAMILIES:
            index_name = self._index_name(family)
            alias = self.alias(family)
            if not self.client.indices.exists_alias(name=alias, index=index_name):
                mappings = self._mappings(family)
                if not self.client.indices.exists(index=index_name):
                    self.client.indices.create(
                        index=index_name,
                        settings={
                            "index": {
                                "number_of_shards": 1,
                                "number_of_replicas": 0,
                                "refresh_interval": "1s",
                            }
                        },
                        mappings=mappings,
                    )
                    logger.info("Created ES index %s", index_name)
                self.client.indices.update_aliases(
                    actions=[{"add": {"index": index_name, "alias": alias}}]
                )

    def _identity_props(self) -> Dict[str, Any]:
        return {name: {"type": "keyword"} for name in SCOPE_FIELDS}

    def _mappings(self, family: str) -> Dict[str, Any]:
        props: Dict[str, Any] = {}
        if family == "scope":
            props = {
                **self._identity_props(),
                "scope_key": {"type": "keyword"},
                "artifact_id": {"type": "keyword"},
                "published_revision": {"type": "long"},
                "last_event_type": {"type": "keyword"},
                "last_entry_id": {"type": "keyword"},
                "last_entry_version_id": {"type": "keyword"},
                "created_at": {"type": "date"},
                "updated_at": {"type": "date"},
            }
        elif family == "head":
            props = {
                **self._identity_props(),
                "scope_key": {"type": "keyword"},
                "entry_id": {"type": "keyword"},
                "entry_version_id": {"type": "keyword"},
                "version": {"type": "long"},
                "kind": {"type": "keyword"},
                "state": {"type": "keyword"},
                "content_hash": {"type": "keyword"},
                "scope_revision": {"type": "long"},
                "text": {"type": "text"},
                "searchable_text": {"type": "text", "analyzer": "standard"},
                "categories": {"type": "keyword"},
                "source_refs": {"type": "keyword"},
                "artifact_refs": {"type": "keyword"},
                "embedding_status": {"type": "keyword"},
                "vector": {
                    "type": "dense_vector",
                    "dims": self.dims,
                    "index": True,
                    "similarity": "cosine",
                },
                "metadata": {"type": "flattened"},
                "legacy_ids": {"type": "keyword"},
                "expires_at": {"type": "date"},
                "created_at": {"type": "date"},
                "updated_at": {"type": "date"},
            }
            if self._ik:
                props["text_zh"] = {
                    "type": "text",
                    "analyzer": "ik_max_word",
                    "search_analyzer": "ik_smart",
                }
        elif family == "version":
            props = {
                **self._identity_props(),
                "scope_key": {"type": "keyword"},
                "entry_id": {"type": "keyword"},
                "entry_version_id": {"type": "keyword"},
                "version": {"type": "long"},
                "kind": {"type": "keyword"},
                "content_hash": {"type": "keyword"},
                "text": {"type": "text"},
                "source_refs": {"type": "keyword"},
                "artifact_refs": {"type": "keyword"},
                "categories": {"type": "keyword"},
                "scope_revision": {"type": "long"},
                "provenance": {"type": "keyword"},
                "legacy_ids": {"type": "keyword"},
                "created_at": {"type": "date"},
            }
        elif family == "event":
            props = {
                "scope_key": {"type": "keyword"},
                "scope_revision": {"type": "long"},
                "event_type": {"type": "keyword"},
                "entry_id": {"type": "keyword"},
                "entry_version_id": {"type": "keyword"},
                "version": {"type": "long"},
                "kind": {"type": "keyword"},
                "content_hash": {"type": "keyword"},
                "state_after": {"type": "keyword"},
                "provenance": {"type": "keyword"},
                "reason": {"type": "keyword"},
                "created_at": {"type": "date"},
            }
        elif family == "dedup":
            props = {
                "scope_key": {"type": "keyword"},
                "dedup_key": {"type": "keyword"},
                "status": {"type": "keyword"},
                "entry_id": {"type": "keyword"},
                "entry_version_id": {"type": "keyword"},
                "scope_revision": {"type": "long"},
                "created_at": {"type": "date"},
                "updated_at": {"type": "date"},
            }
        return {"dynamic": "strict", "properties": props}

    # -- scope ----------------------------------------------------------------

    def create_scope(self, scope_key: str, identity: Dict[str, str]) -> Optional[ScopeDoc]:
        """Create the scope control doc (published_revision=0). Returns None
        when it already exists (expected on every write after the first)."""
        doc = {
            "scope_key": scope_key,
            **identity,
            "artifact_id": uuid.uuid4().hex,
            "published_revision": 0,
            "created_at": utcnow(),
            "updated_at": utcnow(),
        }
        try:
            self.client.index(
                index=self.alias("scope"),
                id=scope_doc_id(scope_key),
                document=doc,
                op_type="create",
                routing=scope_key,
            )
        except EsDocExists:
            return None
        except Exception as exc:
            if classify_elasticsearch_error(exc) == ES_CONFLICT:
                return None
            raise
        return self.get_scope(scope_key)

    def get_scope(self, scope_key: str) -> Optional[ScopeDoc]:
        try:
            resp = self.client.get(
                index=self.alias("scope"), id=scope_doc_id(scope_key), routing=scope_key
            )
        except Exception:
            return None
        src = resp["_source"]
        return ScopeDoc(
            scope_key=scope_key,
            artifact_id=src["artifact_id"],
            published_revision=int(src.get("published_revision", 0)),
            last_event_type=src.get("last_event_type"),
            last_entry_id=src.get("last_entry_id"),
            last_entry_version_id=src.get("last_entry_version_id"),
            seq_no=int(resp["_seq_no"]),
            primary_term=int(resp["_primary_term"]),
            fields={f: src[f] for f in SCOPE_FIELDS if src.get(f)},
            updated_at=src.get("updated_at"),
        )

    def cas_publish(
        self,
        scope_key: str,
        *,
        if_seq_no: int,
        if_primary_term: int,
        published_revision: int,
        last_event_type: str,
        last_entry_id: str,
        last_entry_version_id: str,
    ) -> ScopeDoc:
        """The single linearization point of every write (design §5.2 step 4)."""
        doc = {
            "published_revision": published_revision,
            "last_event_type": last_event_type,
            "last_entry_id": last_entry_id,
            "last_entry_version_id": last_entry_version_id,
            "updated_at": utcnow(),
        }
        try:
            self.client.update(
                index=self.alias("scope"),
                id=scope_doc_id(scope_key),
                if_seq_no=if_seq_no,
                if_primary_term=if_primary_term,
                doc=doc,
                routing=scope_key,
            )
        except Exception as exc:
            if classify_elasticsearch_error(exc) == ES_CONFLICT:
                raise EsCasConflict(scope_key) from exc
            raise
        return self.get_scope(scope_key)

    def scan_recent_scopes(self, *, updated_after: str, limit: int = 200) -> List[ScopeDoc]:
        """Scopes touched after a timestamp — RecoveryReconciler input (§5.4)."""
        body = {
            "size": limit,
            "seq_no_primary_term": True,
            "query": {"range": {"updated_at": {"gt": updated_after}}},
            "sort": [{"updated_at": {"order": "asc"}}],
        }
        resp = self._search("scope", body)
        out = []
        for hit in resp["hits"]["hits"]:
            src = hit["_source"]
            out.append(
                ScopeDoc(
                    scope_key=src["scope_key"],
                    artifact_id=src["artifact_id"],
                    published_revision=int(src.get("published_revision", 0)),
                    last_event_type=src.get("last_event_type"),
                    last_entry_id=src.get("last_entry_id"),
                    last_entry_version_id=src.get("last_entry_version_id"),
                    seq_no=int(hit["_seq_no"]),
                    primary_term=int(hit["_primary_term"]),
                    fields={f: src[f] for f in SCOPE_FIELDS if src.get(f)},
                    updated_at=src.get("updated_at"),
                )
            )
        return out

    def scan_all_scopes(self, *, limit: int = 1000) -> List[ScopeDoc]:
        """Every scope document — FallbackReconciler's sync unit list."""
        resp = self._search(
            "scope", {"size": limit, "seq_no_primary_term": True, "query": {"match_all": {}}}
        )
        out = []
        for hit in resp["hits"]["hits"]:
            src = hit["_source"]
            out.append(
                ScopeDoc(
                    scope_key=src["scope_key"],
                    artifact_id=src["artifact_id"],
                    published_revision=int(src.get("published_revision", 0)),
                    last_event_type=src.get("last_event_type"),
                    last_entry_id=src.get("last_entry_id"),
                    last_entry_version_id=src.get("last_entry_version_id"),
                    seq_no=int(hit["_seq_no"]),
                    primary_term=int(hit["_primary_term"]),
                    fields={f: src[f] for f in SCOPE_FIELDS if src.get(f)},
                    updated_at=src.get("updated_at"),
                )
            )
        return out

    # -- version ---------------------------------------------------------------

    def put_version(self, doc: Dict[str, Any], *, refresh: bool = False) -> None:
        self.client.index(
            index=self.alias("version"),
            id=version_doc_id(doc["scope_key"], doc["entry_id"], doc["version"]),
            document=doc,
            routing=doc["scope_key"],
            refresh="wait_for" if refresh else False,
        )

    def get_version(self, scope_key: str, entry_id: str, version: int) -> Optional[dict]:
        return self._get_doc("version", version_doc_id(scope_key, entry_id, version), scope_key)

    def find_version(self, entry_version_id: str) -> Optional[dict]:
        """expand() locates a version by entry_version_id alone (design §7.1)."""
        resp = self._search(
            "version",
            {"size": 1, "query": {"term": {"entry_version_id": entry_version_id}}},
        )
        hits = resp["hits"]["hits"]
        return hits[0]["_source"] if hits else None

    def list_versions(self, scope_key: str, entry_id: str, *, limit: int = 100) -> List[dict]:
        resp = self._search(
            "version",
            {
                "size": limit,
                "query": {
                    "bool": {
                        "filter": [
                            {"term": {"scope_key": scope_key}},
                            {"term": {"entry_id": entry_id}},
                        ]
                    }
                },
                "sort": [{"version": {"order": "asc"}}],
            },
        )
        return [hit["_source"] for hit in resp["hits"]["hits"]]

    def delete_entry_versions(self, scope_key: str, entry_id: str) -> int:
        return self._delete_by_query(
            "version",
            {
                "bool": {
                    "filter": [
                        {"term": {"scope_key": scope_key}},
                        {"term": {"entry_id": entry_id}},
                    ]
                }
            },
        )

    # -- head ------------------------------------------------------------------

    def put_head(self, doc: Dict[str, Any], *, refresh: bool = False) -> None:
        """Full-document upsert: replaces text/vector state atomically so no
        stale text_zh/token column survives a revise (design 阶段一 item 5)."""
        self.client.index(
            index=self.alias("head"),
            id=head_doc_id(doc["scope_key"], doc["entry_id"]),
            document=doc,
            routing=doc["scope_key"],
            refresh="wait_for" if refresh else False,
        )

    def get_head(self, scope_key: str, entry_id: str) -> Optional[dict]:
        return self._get_doc("head", head_doc_id(scope_key, entry_id), scope_key)

    def find_heads(self, entry_id: str, *, limit: int = 10) -> List[dict]:
        """get() without a scope: resolve entry_id across scopes."""
        resp = self._search("head", {"size": limit, "query": {"term": {"entry_id": entry_id}}})
        return [hit["_source"] for hit in resp["hits"]["hits"]]

    def list_heads(
        self,
        identity_filters: Optional[Dict[str, str]] = None,
        *,
        limit: int = 1000,
        active_only: bool = True,
    ) -> List[dict]:
        filters = self._identity_terms(identity_filters)
        if active_only:
            filters.append({"term": {"state": "active"}})
        body: Dict[str, Any] = {"size": limit, "query": {"bool": {"filter": filters}}}
        resp = self._search("head", body)
        return [hit["_source"] for hit in resp["hits"]["hits"]]

    def delete_head(self, scope_key: str, entry_id: str) -> None:
        try:
            self.client.delete(
                index=self.alias("head"), id=head_doc_id(scope_key, entry_id), routing=scope_key
            )
        except Exception as exc:
            if classify_elasticsearch_error(exc) == "NOT_FOUND":
                return
            raise

    def scan_heads_by_embedding_status(self, status: str, *, limit: int = 200) -> List[dict]:
        resp = self._search(
            "head",
            {
                "size": limit,
                "query": {
                    "bool": {"filter": [{"term": {"state": "active"}}, {"term": {"embedding_status": status}}]}
                },
            },
        )
        return [hit["_source"] for hit in resp["hits"]["hits"]]

    def set_head_vector(self, scope_key: str, entry_id: str, vector: List[float]) -> None:
        """EmbeddingReconciler completion write (vector + status=ready)."""
        self.client.update(
            index=self.alias("head"),
            id=head_doc_id(scope_key, entry_id),
            doc={"vector": vector, "embedding_status": "ready", "updated_at": utcnow()},
            routing=scope_key,
        )

    def scan_orphan_versions(self, *, limit: int = 500) -> List[dict]:
        """Versions whose scope_revision exceeds the scope watermark (§5.4.4)."""
        scopes = {}
        resp = self._search(
            "version", {"size": limit, "query": {"match_all": {}}, "sort": [{"scope_revision": {"order": "desc"}}]}
        )
        orphans = []
        for hit in resp["hits"]["hits"]:
            src = hit["_source"]
            scope_key = src["scope_key"]
            if scope_key not in scopes:
                scope = self.get_scope(scope_key)
                scopes[scope_key] = scope.published_revision if scope else -1
            if int(src.get("scope_revision", 0)) > scopes[scope_key]:
                orphans.append(src)
        return orphans

    # -- event -----------------------------------------------------------------

    def put_event(self, doc: Dict[str, Any], *, refresh: bool = False) -> None:
        self.client.index(
            index=self.alias("event"),
            id=event_doc_id(doc["scope_key"], doc["scope_revision"]),
            document=doc,
            routing=doc["scope_key"],
            refresh="wait_for" if refresh else False,
        )

    def get_event(self, scope_key: str, revision: int) -> Optional[dict]:
        return self._get_doc("event", event_doc_id(scope_key, revision), scope_key)

    def list_events(self, scope_key: str, *, since_revision: int = 0, limit: int = 200) -> List[dict]:
        """Ascending revision window strictly above since_revision (§5.1)."""
        resp = self._search(
            "event",
            {
                "size": limit,
                "query": {
                    "bool": {
                        "filter": [
                            {"term": {"scope_key": scope_key}},
                            {"range": {"scope_revision": {"gt": since_revision}}},
                        ]
                    }
                },
                "sort": [{"scope_revision": {"order": "asc"}}],
            },
        )
        return [hit["_source"] for hit in resp["hits"]["hits"]]

    # -- dedup -----------------------------------------------------------------

    def try_claim_dedup(
        self,
        scope_key: str,
        dedup_key: str,
        *,
        entry_id: str,
        entry_version_id: str,
        scope_revision: int,
    ) -> Optional[dict]:
        """op_type=create claim. Returns the existing doc when the key is taken,
        None when this call won the claim (design §5.2 step 1)."""
        doc = {
            "scope_key": scope_key,
            "dedup_key": dedup_key,
            "status": "prepared",
            "entry_id": entry_id,
            "entry_version_id": entry_version_id,
            "scope_revision": scope_revision,
            "created_at": utcnow(),
            "updated_at": utcnow(),
        }
        try:
            self.client.index(
                index=self.alias("dedup"),
                id=dedup_doc_id(scope_key, dedup_key),
                document=doc,
                op_type="create",
                routing=scope_key,
            )
            return None
        except Exception as exc:
            if classify_elasticsearch_error(exc) == ES_CONFLICT:
                return self.get_dedup(scope_key, dedup_key)
            raise

    def get_dedup(self, scope_key: str, dedup_key: str) -> Optional[dict]:
        return self._get_doc("dedup", dedup_doc_id(scope_key, dedup_key), scope_key)

    def set_dedup_status(
        self,
        scope_key: str,
        dedup_key: str,
        status: str,
        *,
        entry_id: Optional[str] = None,
        entry_version_id: Optional[str] = None,
        scope_revision: Optional[int] = None,
        refresh: bool = False,
    ) -> None:
        doc: Dict[str, Any] = {
            "scope_key": scope_key,
            "dedup_key": dedup_key,
            "status": status,
            "updated_at": utcnow(),
        }
        if entry_id is not None:
            doc["entry_id"] = entry_id
        if entry_version_id is not None:
            doc["entry_version_id"] = entry_version_id
        if scope_revision is not None:
            doc["scope_revision"] = scope_revision
        self.client.index(
            index=self.alias("dedup"),
            id=dedup_doc_id(scope_key, dedup_key),
            document=doc,
            routing=scope_key,
            refresh="wait_for" if refresh else False,
        )

    def scan_prepared_dedups(self, *, older_than_iso: str, limit: int = 200) -> List[dict]:
        resp = self._search(
            "dedup",
            {
                "size": limit,
                "query": {
                    "bool": {
                        "filter": [
                            {"term": {"status": "prepared"}},
                            {"range": {"updated_at": {"lt": older_than_iso}}},
                        ]
                    }
                },
            },
        )
        return [hit["_source"] for hit in resp["hits"]["hits"]]

    # -- recall channels (design §6.2) ------------------------------------------

    def knn_search(
        self,
        vector: List[float],
        *,
        identity_filters: Optional[Dict[str, str]] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        filters = self._identity_terms(identity_filters)
        filters.append({"term": {"state": "active"}})
        filters.append({"term": {"embedding_status": "ready"}})
        body = {
            "size": limit,
            "knn": {
                "field": "vector",
                "query_vector": vector,
                "k": limit,
                "num_candidates": max(limit, 50),
                "filter": {"bool": {"filter": filters, "must_not": [{"range": {"expires_at": {"lt": "now"}}}]}},
            },
        }
        resp = self._search("head", body)
        return [
            {"doc": hit["_source"], "score": hit["_score"], "id": hit["_id"]}
            for hit in resp["hits"]["hits"]
        ]

    def keyword_search(
        self,
        query_original: str,
        query_lemmatized: str,
        *,
        identity_filters: Optional[Dict[str, str]] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """BM25 channel: text_zh takes the ORIGINAL query (ik analyzers handle
        segmentation), searchable_text takes lemmatized tokens (design §6.1)."""
        should = []
        if self._ik:
            should.append({"match": {"text_zh": {"query": query_original}}})
        should.append({"match": {"text": {"query": query_original}}})
        if query_lemmatized and query_lemmatized != query_original:
            should.append({"match": {"searchable_text": {"query": query_lemmatized}}})
        else:
            should.append({"match": {"searchable_text": {"query": query_original}}})
        filters = self._identity_terms(identity_filters)
        filters.append({"term": {"state": "active"}})
        body = {
            "size": limit,
            "query": {
                "bool": {
                    "should": should,
                    "minimum_should_match": 1,
                    "filter": filters,
                    "must_not": [{"range": {"expires_at": {"lt": "now"}}}],
                }
            },
        }
        resp = self._search("head", body)
        return [
            {"doc": hit["_source"], "score": hit["_score"], "id": hit["_id"]}
            for hit in resp["hits"]["hits"]
        ]

    # -- maintenance -----------------------------------------------------------

    def delete_all(self) -> None:
        """Drop and recreate the five indices (admin reset / e2e from-zero)."""
        for family in FAMILIES:
            try:
                self.client.indices.delete(index=self._index_name(family))
            except Exception:
                pass
            try:
                self.client.indices.delete(index=self.alias(family))
            except Exception:
                pass
        self.ensure_indices()

    # -- internals ---------------------------------------------------------------

    def _search(self, family: str, body: Dict[str, Any]) -> Dict[str, Any]:
        try:
            return self.client.search(index=self.alias(family), **self._search_kwargs(body))
        except Exception as exc:
            cls = classify_elasticsearch_error(exc)
            if cls in (ES_UNAVAILABLE, ES_TIMEOUT, ES_THROTTLED, ES_BAD_REQUEST, ES_CONFLICT, "UNKNOWN"):
                raise PrimaryUnavailableError(
                    f"Elasticsearch {family} search failed ({cls})", error_class=cls
                ) from exc
            raise

    @staticmethod
    def _search_kwargs(body: Dict[str, Any]) -> Dict[str, Any]:
        """elasticsearch-py 8.x accepts the body fields as kwargs; keep both
        shapes working (8.17 prefers kwargs)."""
        kwargs: Dict[str, Any] = {}
        for key in ("size", "query", "knn", "sort", "seq_no_primary_term"):
            if key in body:
                kwargs[key] = body[key]
        return kwargs or {"body": body}

    def _get_doc(self, family: str, doc_id: str, routing: str) -> Optional[dict]:
        try:
            resp = self.client.get(index=self.alias(family), id=doc_id, routing=routing)
        except Exception:
            return None
        return resp["_source"]

    def _delete_by_query(self, family: str, query: Dict[str, Any]) -> int:
        try:
            resp = self.client.delete_by_query(index=self.alias(family), query=query)
            return int(resp.get("deleted", 0))
        except Exception as exc:
            if classify_elasticsearch_error(exc) == "NOT_FOUND":
                return 0
            raise

    @staticmethod
    def _identity_terms(identity_filters: Optional[Dict[str, str]]) -> List[Dict[str, Any]]:
        if not identity_filters:
            return []
        return [{"term": {k: v}} for k, v in identity_filters.items() if v]


def build_es_client(
    *,
    host: str,
    port: int,
    user: Optional[str] = None,
    password: Optional[str] = None,
    use_ssl: bool = False,
    verify_certs: bool = False,
    ca_certs: Optional[str] = None,
) -> Any:
    """Transport with the design §7.5 retry policy baked in."""
    from elasticsearch import Elasticsearch

    scheme = "https" if use_ssl else "http"
    return Elasticsearch(
        hosts=[f"{scheme}://{host}:{port}"],
        basic_auth=(user, password) if (user and password) else None,
        verify_certs=verify_certs,
        ca_certs=ca_certs,
        max_retries=3,
        retry_on_status=(429, 502, 503, 504),
        retry_on_timeout=True,
        request_timeout=30,
    )
