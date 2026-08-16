"""MemoryApplicationService: the single domain entry point (design §2/§5.1).

REST /v1, MCP tools and the Legacy adapter all funnel through these methods —
no transport layer touches ES or SQL directly. The service composes the
WriteCoordinator (publish protocol), RecallCoordinator (channel fusion) and
the reconcilers over one ElasticsearchMemoryStore, plus the optional HYBRID
SQL sidecar.
"""

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from pydantic import ValidationError as PydanticValidationError

from mem0.configs.base import MemoryConfig
from mem0.context.errors import (
    CapabilityNotSupportedError,
    ContextError,
    ContextValidationError,
    EntryNotFoundError,
    EvidenceExpiredError,
)
from mem0.context.hashing import MAX_TEXT_BYTES, entry_content_hash
from mem0.context.models import (
    ChangeRecordBody,
    EntryVersionBody,
    MemoryCitation,
    RememberResult,
)
from mem0.context.observability import Observability, shared_observability
from mem0.context.scope import SCOPE_FIELDS, ScopeIdentity
from mem0.context.vdb.es_store import ElasticsearchMemoryStore, build_es_client
from mem0.context.vdb.recall import RecallCoordinator
from mem0.context.vdb.reconcile import EmbeddingReconciler, RecoveryReconciler
from mem0.context.vdb.write import WriteCoordinator

logger = logging.getLogger(__name__)

MAX_QUERY_BYTES = 32 * 1024


def translate_es_errors(fn):
    """Write-path guard: an unclassified ES runtime failure surfaces as 503
    primary_unavailable (design §7.4) instead of an opaque 500. Domain errors
    (ContextError subclasses) pass through untouched."""
    import functools

    from mem0.context.vdb.errors import PrimaryUnavailableError, classify_elasticsearch_error

    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        try:
            return fn(self, *args, **kwargs)
        except ContextError:
            raise
        except Exception as exc:
            cls = classify_elasticsearch_error(exc)
            raise PrimaryUnavailableError(
                f"Elasticsearch write path failed ({cls}): {exc}", error_class=cls
            ) from exc

    return wrapper


def _normalize_text(text: str) -> str:
    import unicodedata

    if not isinstance(text, str):
        raise ContextValidationError("text must be a string")
    normalized = unicodedata.normalize("NFC", text).strip()
    if not normalized:
        raise ContextValidationError("text must not be empty")
    if len(normalized.encode("utf-8")) > MAX_TEXT_BYTES:
        raise ContextValidationError(f"text exceeds {MAX_TEXT_BYTES} bytes")
    return normalized


def _messages_to_transcript(messages: List[Dict[str, Any]]) -> str:
    lines = []
    for message in messages or []:
        role = str(message.get("role") or "user")
        content = str(message.get("content") or "").strip()
        if content:
            lines.append(f"{role}: {content}")
    return "\n".join(lines)


def parse_extraction_facts(raw: str) -> List[str]:
    """Parse the extraction LLM's JSON facts payload tolerantly."""
    if not raw:
        return []
    text = raw.strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}|\[.*\]", text, re.DOTALL)
        if not match:
            return []
        try:
            payload = json.loads(match.group(0))
        except json.JSONDecodeError:
            return []
    if isinstance(payload, dict):
        facts = payload.get("facts") or []
    elif isinstance(payload, list):
        facts = payload
    else:
        return []
    out = []
    for fact in facts:
        if isinstance(fact, str) and fact.strip():
            out.append(fact.strip())
        elif isinstance(fact, dict):
            text_value = fact.get("text") or fact.get("memory")
            if isinstance(text_value, str) and text_value.strip():
                out.append(text_value.strip())
    return out


class MemoryApplicationService:
    def __init__(
        self,
        store: ElasticsearchMemoryStore,
        *,
        llm=None,
        embedder=None,
        reranker=None,
        obs: Optional[Observability] = None,
        storage_mode: str = "ONLY_VDB",
        hybrid_sidecar=None,
        custom_instructions: Optional[str] = None,
    ):
        self.store = store
        self.llm = llm
        self.embedder = embedder
        self.reranker = reranker
        self.storage_mode = storage_mode
        self.custom_instructions = (custom_instructions or "").strip() or None
        self.hybrid_sidecar = hybrid_sidecar
        self._obs = obs if obs is not None else shared_observability()

        self.writer = WriteCoordinator(store, embedder=embedder)
        fts_fallback = hybrid_sidecar.fts_search if hybrid_sidecar is not None else None
        self.recaller = RecallCoordinator(store, embedder=embedder, reranker=reranker, fts_fallback=fts_fallback)
        self.recovery = RecoveryReconciler(store)
        self.embedder_reconciler = EmbeddingReconciler(store, embedder)

    # -- construction -------------------------------------------------------------

    @classmethod
    def from_config(
        cls,
        config_dict: Dict[str, Any],
        *,
        es_host: str = "elasticsearch",
        es_port: int = 9200,
        es_user: Optional[str] = None,
        es_password: Optional[str] = None,
        es_use_ssl: bool = False,
        es_verify_certs: bool = False,
        es_prefix: str = "agentar_mem0",
        storage_mode: str = "ONLY_VDB",
        obs: Optional[Observability] = None,
        hybrid_sidecar=None,
        es_client=None,
    ) -> "MemoryApplicationService":
        try:
            config = MemoryConfig(**config_dict)
        except PydanticValidationError as exc:
            raise ContextValidationError(f"Invalid memory config: {exc}") from exc

        from mem0.utils.factory import EmbedderFactory, LlmFactory, RerankerFactory

        embedder = None
        if config.embedder and config.embedder.provider != "null":
            embedder = EmbedderFactory.create(
                config.embedder.provider,
                config.embedder.config,
                config.vector_store.config,
            )
        llm = None
        if config.llm and config.llm.provider != "null":
            llm = LlmFactory.create(config.llm.provider, config.llm.config)
        reranker = None
        if config.reranker:
            try:
                reranker = RerankerFactory.create(config.reranker.provider, config.reranker.config)
            except Exception as exc:
                logger.warning("Reranker construction failed; rerank disabled (%s)", exc)

        dims = 1024
        if embedder is not None:
            try:
                dims = len(embedder.embed("probe", "memory"))
            except Exception:
                dims = int(getattr(config.embedder.config, "embedding_dims", 0) or 0) or 1024
        elif config.vector_store and getattr(config.vector_store.config, "embedding_model_dims", None):
            dims = int(config.vector_store.config.embedding_model_dims)

        client = es_client or build_es_client(
            host=es_host,
            port=es_port,
            user=es_user,
            password=es_password,
            use_ssl=es_use_ssl,
            verify_certs=es_verify_certs,
        )
        store = ElasticsearchMemoryStore(client, prefix=es_prefix, dims=dims)
        return cls(
            store,
            llm=llm,
            embedder=embedder,
            reranker=reranker,
            obs=obs,
            storage_mode=storage_mode,
            hybrid_sidecar=hybrid_sidecar,
            custom_instructions=config.custom_instructions,
        )

    # -- capabilities (design §10) --------------------------------------------------

    def capabilities(self) -> Dict[str, Any]:
        keyword_probe = True
        try:
            self.store.keyword_search("probe", "probe", identity_filters=None, limit=1)
        except Exception:
            keyword_probe = False
        semantic_probe = False
        embedding_profile = None
        if self.embedder is not None:
            try:
                self.embedder.embed("capability probe", "search")
                semantic_probe = True
                embedding_profile = getattr(self.embedder.config, "model", None) if hasattr(
                    self.embedder, "config"
                ) else None
            except Exception:
                semantic_probe = False
        return {
            "storage": {
                "mode": self.storage_mode.lower(),
                "primary_provider": "elasticsearch",
                "sql_fallback_enabled": self.hybrid_sidecar is not None
                and self.hybrid_sidecar.healthy(),
            },
            "memory": {
                "extraction": self.llm is not None,
                "semantic_search": semantic_probe,
                "keyword_search": keyword_probe,
                "embedding_profile": embedding_profile,
                "lifecycle": True,
            },
        }

    # -- write path (design §5.1) -----------------------------------------------------

    @translate_es_errors
    def remember(
        self,
        text: Optional[str] = None,
        *,
        mode: str = "auto",
        messages: Optional[List[Dict[str, Any]]] = None,
        kind: str = "fact",
        categories: Optional[List[str]] = None,
        source_refs: Optional[List[str]] = None,
        artifact_refs: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        expires_at: Optional[str] = None,
        prompt: Optional[str] = None,
        expected_revision: Optional[int] = None,
        **ids: Optional[str],
    ) -> Dict[str, Any]:
        """Unified write. append → one zero-LLM candidate; extract → LLM over
        ``messages`` producing 0..N candidates, each published individually;
        auto routes text→append and messages→extract (design §5.1)."""
        if mode not in ("auto", "append", "extract"):
            raise ContextValidationError(f"mode must be auto|append|extract, got {mode!r}")

        scope = ScopeIdentity(**ids)
        if mode == "extract" or (mode == "auto" and text is None):
            return self._remember_extract(
                scope,
                messages=messages,
                kind=kind,
                categories=categories or [],
                source_refs=source_refs or [],
                artifact_refs=artifact_refs or [],
                metadata=metadata,
                expected_revision=expected_revision,
                prompt_override=prompt,
            )
        if text is None:
            raise ContextValidationError("text is required for mode=append/auto")

        outcome = self.writer.remember(
            scope,
            kind=kind,
            text=_normalize_text(text),
            categories=[c for c in (categories or []) if c],
            source_refs=source_refs or [],
            artifact_refs=artifact_refs or [],
            metadata=metadata,
            expires_at=expires_at,
            expected_revision=expected_revision,
        )
        return {"results": [self._to_remember_result(outcome).model_dump(mode="json")]}

    def _remember_extract(
        self,
        scope: ScopeIdentity,
        *,
        messages: Optional[List[Dict[str, Any]]],
        kind: str,
        categories: List[str],
        source_refs: List[str],
        artifact_refs: List[str],
        metadata: Optional[Dict[str, Any]],
        expected_revision: Optional[int],
        prompt_override: Optional[str] = None,
    ) -> Dict[str, Any]:
        if self.llm is None:
            raise CapabilityNotSupportedError("extract")
        if not messages:
            raise ContextValidationError("messages are required for mode=extract")
        transcript = _messages_to_transcript(messages)
        if not transcript.strip():
            raise ContextValidationError("messages contain no textual content")

        from mem0.memory.utils import get_fact_retrieval_messages

        system_prompt, user_prompt = get_fact_retrieval_messages(transcript)
        # deployment taxonomy/custom instructions and the per-call legacy
        # ``prompt`` override must actually reach the extraction LLM
        extra = self.custom_instructions or ""
        if prompt_override:
            extra = f"{extra}\n\n{prompt_override}" if extra else prompt_override
        if extra:
            system_prompt = f"{system_prompt}\n\n{extra}"
        response = self.llm.generate_response(
            [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
            response_format={"type": "json_object"},
        )
        facts = parse_extraction_facts(response)

        results = []
        for fact in facts:
            outcome = self.writer.remember(
                scope,
                kind=kind,
                text=_normalize_text(fact),
                categories=categories,
                source_refs=source_refs,
                artifact_refs=artifact_refs,
                metadata=metadata,
                expected_revision=expected_revision if not results else None,
            )
            results.append(self._to_remember_result(outcome).model_dump(mode="json"))
        return {"results": results}

    @translate_es_errors
    def revise(
        self,
        entry_id: str,
        *,
        text: Optional[str] = None,
        kind: Optional[str] = None,
        categories: Optional[List[str]] = None,
        source_refs: Optional[List[str]] = None,
        artifact_refs: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        expires_at: Optional[str] = None,
        clear_expires_at: bool = False,
        expected_revision: Optional[int] = None,
        **ids: Optional[str],
    ) -> RememberResult:
        scope = ScopeIdentity(**ids)
        outcome = self.writer.revise(
            scope,
            entry_id,
            kind=kind,
            text=_normalize_text(text) if text is not None else None,
            categories=categories,
            source_refs=source_refs,
            artifact_refs=artifact_refs,
            metadata=metadata,
            expires_at=expires_at,
            clear_expires_at=clear_expires_at,
            expected_revision=expected_revision,
        )
        return self._to_remember_result(outcome)

    @translate_es_errors
    def retire(
        self,
        entry_id: str,
        *,
        reason: Optional[str] = None,
        expected_revision: Optional[int] = None,
        **ids: Optional[str],
    ) -> RememberResult:
        scope = ScopeIdentity(**ids)
        outcome = self.writer.retire(scope, entry_id, reason=reason, expected_revision=expected_revision)
        return self._to_remember_result(outcome)

    @translate_es_errors
    def reactivate(
        self,
        entry_id: str,
        *,
        reason: Optional[str] = None,
        expected_revision: Optional[int] = None,
        **ids: Optional[str],
    ) -> RememberResult:
        scope = ScopeIdentity(**ids)
        outcome = self.writer.reactivate(scope, entry_id, reason=reason, expected_revision=expected_revision)
        return self._to_remember_result(outcome)

    @translate_es_errors
    def purge(
        self,
        entry_id: str,
        *,
        reason: Optional[str] = None,
        expected_revision: Optional[int] = None,
        **ids: Optional[str],
    ) -> RememberResult:
        scope = ScopeIdentity(**ids)
        outcome = self.writer.purge(scope, entry_id, reason=reason, expected_revision=expected_revision)
        return self._to_remember_result(outcome)

    # -- read path (design §6/§7.1) ------------------------------------------------

    def recall(
        self,
        query: str,
        *,
        limit: int = 10,
        mode: str = "auto",
        threshold: Optional[float] = None,
        rerank: bool = False,
        **ids: Optional[str],
    ) -> Dict[str, Any]:
        scope = ScopeIdentity(**ids)
        return self.recaller.recall(
            query,
            identity_filters=scope.fields,
            limit=limit,
            mode=mode,
            threshold=threshold,
            rerank=rerank,
        )

    def expand(self, citation: MemoryCitation, **ids: Optional[str]) -> EntryVersionBody:
        """Version-exact read; the citation alone locates the version and the
        stored hash is re-verified (design §7.1)."""
        version_doc = self.store.find_version(citation.entry_version_id)
        if version_doc is None:
            raise EntryNotFoundError(f"Version {citation.entry_version_id} not found")
        scope_doc = self.store.get_scope(version_doc["scope_key"])
        if scope_doc is None or scope_doc.artifact_id != citation.artifact_id:
            raise EntryNotFoundError(
                f"Citation artifact {citation.artifact_id} does not resolve in this scope"
            )
        recomputed = entry_content_hash(
            kind=version_doc["kind"],
            text=version_doc["text"],
            source_refs=version_doc.get("source_refs") or [],
            artifact_refs=version_doc.get("artifact_refs") or [],
            categories=version_doc.get("categories") or [],
        )
        if recomputed != version_doc.get("content_hash"):
            raise EvidenceExpiredError(
                f"Citation {citation.entry_version_id} failed hash verification"
            )
        return self._version_body(version_doc)

    def changes(
        self,
        *,
        since_revision: int = 0,
        limit: int = 200,
        cursor: Optional[int] = None,
        **ids: Optional[str],
    ) -> List[ChangeRecordBody]:
        scope = ScopeIdentity(**ids)
        start = max(since_revision, cursor or 0)
        events = self.store.list_events(scope.scope_key, since_revision=start, limit=limit)
        records = []
        for index, event in enumerate(events):
            next_cursor = None
            if len(events) == limit and index == len(events) - 1:
                next_cursor = int(event["scope_revision"])
            records.append(
                ChangeRecordBody(
                    entry_id=event["entry_id"],
                    entry_version_id=event.get("entry_version_id") or "",
                    version=int(event.get("version") or 0),
                    kind=event.get("kind") or "",
                    entry_content_hash=event.get("content_hash") or "",
                    created_in_revision=int(event["scope_revision"]),
                    provenance=event.get("provenance") or "api",
                    created_at=self._parse_ts(event.get("created_at")),
                    next_cursor=next_cursor,
                )
            )
        return records

    def get(self, entry_id: str, **ids: Optional[str]) -> Dict[str, Any]:
        """Point read of the current head by entry_id (design §7.6.1)."""
        try:
            scope = ScopeIdentity(**ids)
            head = self.store.get_head(scope.scope_key, entry_id)
        except ContextValidationError:
            head = None
            heads = self.store.find_heads(entry_id)
            head = heads[0] if heads else None
        if head is None:
            raise EntryNotFoundError(f"Entry {entry_id} not found")
        return self._head_body(head)

    def list_heads(
        self,
        *,
        limit: int = 1000,
        active_only: bool = True,
        **ids: Optional[str],
    ) -> List[Dict[str, Any]]:
        identity = {k: v for k, v in ids.items() if v}
        return [self._head_body(h) for h in self.store.list_heads(identity, limit=limit, active_only=active_only)]

    def prepare_context(
        self,
        query: str,
        *,
        budget_bytes: int = 8000,
        mode: str = "auto",
        **ids: Optional[str],
    ) -> Dict[str, Any]:
        """PreparedContext v1 over the ES recall surface (design §6.4/§7.6.1)."""
        from mem0.context.prepared import (
            MAX_MEMORY_ITEMS,
            PreparedItem,
            build_prepared_context,
        )

        scope = ScopeIdentity(**ids)
        recall = self.recall(query, limit=MAX_MEMORY_ITEMS, mode=mode, **ids)
        items = []
        for result in recall["results"]:
            citation = None
            scope_doc = self.store.get_scope(result.get("scope_key") or scope.scope_key)
            if scope_doc is not None and result.get("entry_id"):
                citation = {
                    "artifact_id": scope_doc.artifact_id,
                    "entry_id": result["entry_id"],
                    "entry_version_id": result.get("entry_version_id"),
                }
            items.append(PreparedItem(type="memory", text=result["text"], citation=citation))
        prepared = build_prepared_context(items, budget_bytes=budget_bytes)
        return {
            "schema": prepared.schema,
            "rendered": prepared.rendered,
            "item_count": prepared.item_count,
            "dropped": prepared.dropped,
            "budget_bytes": prepared.budget_bytes,
            "rendered_bytes": prepared.rendered_bytes,
            "search_mode": recall["search_mode"],
        }

    # -- reconciliation (design §5.4 / §6.4) ----------------------------------------

    def reconcile(self) -> Dict[str, int]:
        recovery_counts = self.recovery.reconcile()
        embedding_counts = self.embedder_reconciler.reconcile()
        out = dict(recovery_counts)
        out.update({f"embedding_{k}": v for k, v in embedding_counts.items()})
        return out

    def rebuild_heads(self, *, limit: int = 1000) -> Dict[str, int]:
        """Admin rebuild: reconstruct every head from its newest version."""
        rebuilt = 0
        events_missing = 0
        scopes = self.store.scan_all_scopes(limit=limit)
        for scope in scopes:
            for event in self.store.list_events(scope.scope_key, since_revision=0, limit=1000):
                if event.get("event_type") in ("created", "revised", "reactivated"):
                    version_doc = self.store.find_version(event.get("entry_version_id"))
                    if version_doc is None:
                        continue
                    existing = self.store.get_head(scope.scope_key, event["entry_id"])
                    if existing is None or int(existing.get("scope_revision", 0)) < int(
                        event["scope_revision"]
                    ):
                        head = self.recovery._head_from_version(scope, version_doc, event)
                        self.store.put_head(head)
                        rebuilt += 1
                if self.store.get_event(scope.scope_key, int(event["scope_revision"])) is None:
                    events_missing += 1
        return {"scopes": len(scopes), "heads_rebuilt": rebuilt, "events_rebuilt": events_missing}

    def reset_all(self) -> Dict[str, str]:
        """Admin reset: drop and recreate the authority indices."""
        self.store.delete_all()
        return {"status": "reset"}

    # -- internals ---------------------------------------------------------------------

    def _to_remember_result(self, outcome) -> RememberResult:
        entry = None
        if outcome.entry_id and outcome.entry_version_id and outcome.event_type in (
            "created",
            "revised",
        ):
            version_doc = self.store.get_version(
                outcome.scope_key, outcome.entry_id, int(outcome.version or 1)
            )
            if version_doc is not None:
                entry = self._version_body(version_doc)
        return RememberResult(
            outcome=outcome.outcome,
            artifact_id=outcome.artifact_id,
            revision=outcome.revision,
            pending_embed=outcome.pending_embed,
            entry=entry,
        )

    def _version_body(self, version_doc: dict) -> EntryVersionBody:
        return EntryVersionBody(
            entry_id=version_doc["entry_id"],
            entry_version_id=version_doc["entry_version_id"],
            version=int(version_doc.get("version", 1)),
            kind=version_doc.get("kind") or "fact",
            text=version_doc.get("text") or "",
            categories=version_doc.get("categories") or [],
            source_refs=version_doc.get("source_refs") or [],
            artifact_refs=version_doc.get("artifact_refs") or [],
            entry_content_hash=version_doc.get("content_hash") or "",
            created_in_revision=int(version_doc.get("scope_revision", 0)),
            provenance=version_doc.get("provenance") or "api",
            created_at=self._parse_ts(version_doc.get("created_at")),
        )

    def _head_body(self, head: dict) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "entry_id": head.get("entry_id"),
            "entry_version_id": head.get("entry_version_id"),
            "version": head.get("version"),
            "kind": head.get("kind"),
            "state": head.get("state"),
            "text": head.get("text"),
            "content_hash": head.get("content_hash"),
            "expires_at": head.get("expires_at"),
            "categories": head.get("categories") or [],
            "source_refs": head.get("source_refs") or [],
            "artifact_refs": head.get("artifact_refs") or [],
            "scope_key": head.get("scope_key"),
            "scope_revision": head.get("scope_revision"),
            "embedding_status": head.get("embedding_status"),
            "created_at": head.get("created_at"),
            "updated_at": head.get("updated_at"),
        }
        for field in SCOPE_FIELDS:
            if head.get(field):
                body[field] = head[field]
        if isinstance(head.get("metadata"), dict):
            body["metadata"] = head["metadata"]
        if head.get("expires_at"):
            body["expires_at"] = head["expires_at"]
        return body

    @staticmethod
    def _parse_ts(value: Optional[str]) -> datetime:
        if not value:
            return datetime.now(timezone.utc)
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return datetime.now(timezone.utc)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
