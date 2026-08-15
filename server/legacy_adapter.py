"""LegacyAdapter (design §7.2): protocol translation for the no-prefix
compatibility surface — POST/GET/PUT/DELETE /memories and POST /search.

No storage semantics live here: every call maps onto the
MemoryApplicationService (remember/revise/retire/recall/get) so the compat
API and /v1/memory/* can never drift (design §7.6.3). ``memory_id`` in the
legacy surface IS the ``entry_id``; head/version documents also mirror it in
``legacy_ids`` for future migration tooling.
"""

import logging
from typing import Any, Dict, List, Optional

from mem0.context.scope import SCOPE_FIELDS

logger = logging.getLogger(__name__)

PROMOTED_KEYS = ("user_id", "agent_id", "run_id", "tenant_id", "session_id")


def _legacy_row(head: Dict[str, Any], score: Optional[float] = None) -> Dict[str, Any]:
    """Legacy memory row shape (serialize.py contract)."""
    metadata = dict(head.get("metadata") or {})
    row = {
        "id": head.get("entry_id"),
        "memory": head.get("text"),
        "hash": head.get("content_hash"),
        "created_at": head.get("created_at"),
        "updated_at": head.get("updated_at"),
        "expiration_date": (head.get("expires_at") or "").split("T")[0] or None,
        "metadata": metadata,
        "categories": head.get("categories") or [],
        "state": head.get("state"),
        "entry_version_id": head.get("entry_version_id"),
    }
    for key in PROMOTED_KEYS:
        if head.get(key):
            row[key] = head[key]
    if score is not None:
        row["score"] = score
    return row


class _Row:
    """OutputData-like row for admin listing paths (entities/export)."""

    __slots__ = ("id", "payload", "score")

    def __init__(self, row_id: str, payload: Dict[str, Any]):
        self.id = row_id
        self.payload = payload
        self.score = 1.0


class _VectorStoreShim:
    """Minimal ``vector_store`` surface used by admin listing routes: the ES
    head index answers ``list`` with legacy-shaped payload rows."""

    def __init__(self, adapter: "LegacyMemoryAdapter"):
        self._adapter = adapter

    def list(self, top_k: Optional[int] = None, filters: Optional[Dict] = None) -> List[List[_Row]]:
        heads = self._adapter.service.list_heads(limit=top_k or 1000, active_only=False)
        rows = []
        for head in heads:
            if filters:
                if any(head.get(k) != v for k, v in filters.items() if v):
                    continue
            payload = dict(head.get("metadata") or {})
            payload["data"] = head.get("text")
            payload["hash"] = head.get("content_hash")
            payload["created_at"] = head.get("created_at")
            payload["updated_at"] = head.get("updated_at")
            for key in PROMOTED_KEYS:
                if head.get(key):
                    payload[key] = head[key]
            rows.append(_Row(head.get("entry_id"), payload))
        return [rows]


class LegacyMemoryAdapter:
    """The Memory-compatible facade over MemoryApplicationService."""

    def __init__(self, service, config=None):
        self.service = service
        self.config = config
        self.llm = service.llm
        self.reranker = service.reranker
        self.vector_store = _VectorStoreShim(self)

    # -- write ----------------------------------------------------------------

    def add(
        self,
        messages: List[Dict[str, Any]],
        *,
        infer: Optional[bool] = None,
        metadata: Optional[Dict[str, Any]] = None,
        expiration_date: Optional[str] = None,
        prompt: Optional[str] = None,
        memory_type: Optional[str] = None,
        **ids: Optional[str],
    ) -> Dict[str, Any]:
        """Legacy create: infer=false → remember(append) of the raw last user
        message; infer=true → remember(extract) over the messages (§7.2)."""
        use_extract = True if infer is None else infer
        payload_metadata = dict(metadata or {})
        if memory_type:
            payload_metadata.setdefault("memory_type", memory_type)
        if prompt:
            payload_metadata.setdefault("custom_prompt", True)

        if use_extract:
            response = self.service.remember(
                messages=messages,
                mode="extract",
                metadata=payload_metadata or None,
                expires_at=self._expires_at(expiration_date),
                **ids,
            )
            results = [
                {"id": r["entry"]["entry_id"], "memory": r["entry"]["text"], "event": "ADD"}
                for r in response["results"]
                if r.get("entry")
            ]
        else:
            text = self._last_user_text(messages)
            response = self.service.remember(
                text,
                mode="append",
                metadata=payload_metadata or None,
                expires_at=self._expires_at(expiration_date),
                **ids,
            )
            first = response["results"][0]
            results = []
            if first["outcome"] != "noop" and first.get("entry"):
                results.append(
                    {"id": first["entry"]["entry_id"], "memory": first["entry"]["text"], "event": "ADD"}
                )
        return {"results": results, "relations": []}

    def update(
        self,
        *,
        memory_id: str,
        data: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        expiration_date: Optional[str] = None,
        **ids: Optional[str],
    ) -> Dict[str, Any]:
        head = self._find_head(memory_id, ids)
        result = self.service.revise(
            memory_id,
            text=data,
            metadata=metadata,
            expires_at=self._expires_at(expiration_date),
            **self._scope_kwargs(head),
        )
        new_text = data if data is not None else head.get("text")
        return {
            "id": memory_id,
            "memory": new_text,
            "previous_memory": head.get("text"),
            "event": "UPDATE",
            "revision": result.revision,
        }

    def delete(self, *, memory_id: str, purge: bool = False, **ids: Optional[str]) -> Dict[str, Any]:
        """DELETE /memories/{id}: retire by default; purge=true physically
        deletes (design §7.2)."""
        head = self._find_head(memory_id, ids)
        if purge:
            self.service.purge(memory_id, reason="legacy purge", **self._scope_kwargs(head))
        else:
            self.service.retire(memory_id, reason="legacy delete", **self._scope_kwargs(head))
        return {"id": memory_id, "event": "DELETE"}

    def delete_all(self, **ids: Optional[str]) -> Dict[str, Any]:
        provided = {k: v for k, v in ids.items() if v}
        if not provided:
            raise ValueError("At least one identifier is required.")
        heads = self.service.list_heads(limit=5000, active_only=False, **provided)
        deleted = 0
        for head in heads:
            try:
                self.service.purge(head["entry_id"], reason="legacy delete_all", **provided)
                deleted += 1
            except Exception:
                logger.warning("delete_all: entry %s failed", head.get("entry_id"), exc_info=True)
        return {"deleted": deleted}

    # -- read -------------------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        filters: Optional[Dict[str, Any]] = None,
        top_k: int = 20,
        threshold: float = 0.1,
        rerank: bool = False,
        mode: str = "auto",
        explain: bool = False,
        show_expired: bool = False,
        **kwargs,
    ) -> Dict[str, Any]:
        filters = dict(filters or {})
        identity = {k: filters.pop(k) for k in list(filters) if k in SCOPE_FIELDS and filters[k]}
        recall = self.service.recall(
            query,
            limit=max(1, min(top_k, 50)),
            mode=mode,
            threshold=threshold if threshold is not None else None,
            rerank=rerank,
            **identity,
        )
        results = []
        for hit in recall["results"]:
            row = _legacy_row(hit, score=hit.get("score"))
            row["matched_by"] = hit.get("matched_by") or []
            if filters:
                meta = dict(row.get("metadata") or {})
                meta.update({k: v for k, v in filters.items()})
                row["metadata"] = meta
            results.append(row)
        return {
            "results": results,
            "search_mode": recall["search_mode"],
            "storage_source": recall.get("storage_source"),
        }

    def get(self, memory_id: str) -> Dict[str, Any]:
        heads = self.service.store.find_heads(memory_id, limit=5)
        if not heads:
            raise ValueError(f"Memory with id {memory_id} not found")
        return _legacy_row(heads[0])

    def get_all(
        self,
        *,
        filters: Optional[Dict[str, Any]] = None,
        top_k: Optional[int] = None,
        show_expired: bool = False,
        api: Optional[str] = None,
        **ids: Optional[str],
    ) -> Dict[str, Any]:
        provided = {k: v for k, v in ids.items() if v}
        if filters:
            provided.update({k: v for k, v in filters.items() if k in SCOPE_FIELDS and v})
        heads = self.service.list_heads(limit=top_k or 1000, active_only=True, **provided)
        if not show_expired:
            heads = [h for h in heads if not h.get("expires_at")]
        return {"results": [_legacy_row(h) for h in heads]}

    def history(self, *, memory_id: str) -> List[Dict[str, Any]]:
        heads = self.service.store.find_heads(memory_id, limit=1)
        if not heads:
            return []
        head = heads[0]
        versions = self.service.store.list_versions(head["scope_key"], memory_id, limit=100)
        history = []
        for index, version in enumerate(versions):
            history.append(
                {
                    "id": memory_id,
                    "memory": version["text"],
                    "event": "ADD" if index == 0 else "UPDATE",
                    "entry_version_id": version["entry_version_id"],
                    "creation_datetime": version.get("created_at"),
                }
            )
        if head.get("state") == "inactive":
            history.append(
                {"id": memory_id, "memory": head.get("text"), "event": "DELETE", "creation_datetime": head.get("updated_at")}
            )
        return history

    def reset(self) -> None:
        self.service.reset_all()

    # -- helpers ------------------------------------------------------------------

    @staticmethod
    def _last_user_text(messages: List[Dict[str, Any]]) -> str:
        for message in reversed(messages or []):
            if (message.get("role") or "user") == "user" and (message.get("content") or "").strip():
                return str(message["content"]).strip()
        for message in messages or []:
            if (message.get("content") or "").strip():
                return str(message["content"]).strip()
        raise ValueError("messages contain no text")

    @staticmethod
    def _expires_at(expiration_date: Optional[str]) -> Optional[str]:
        if not expiration_date:
            return None
        return f"{expiration_date}T23:59:59+00:00"

    def _find_head(self, memory_id: str, ids: Dict[str, Any]) -> Dict[str, Any]:
        provided = {k: v for k, v in ids.items() if v}
        if provided:
            from mem0.context.scope import ScopeIdentity

            head = self.service.store.get_head(ScopeIdentity(**provided).scope_key, memory_id)
            if head is not None:
                return head
        heads = self.service.store.find_heads(memory_id, limit=1)
        if not heads:
            raise ValueError(f"Memory with id {memory_id} not found")
        return heads[0]

    @staticmethod
    def _scope_kwargs(head: Dict[str, Any]) -> Dict[str, str]:
        return {k: head[k] for k in SCOPE_FIELDS if head.get(k)}
