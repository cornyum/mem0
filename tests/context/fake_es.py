"""In-memory Elasticsearch fake for kernel unit tests.

Implements the exact client surface ElasticsearchMemoryStore uses, with real
``_seq_no``/``_primary_term`` CAS semantics and ``op_type=create`` conflicts
(raising the real elasticsearch exception types so §7.5 classification is
exercised). Search supports term/range/match/match_all/knn + sort — enough
for the recall channels without a live cluster.
"""

import threading

from elasticsearch import ConflictError, NotFoundError


class _Doc:
    __slots__ = ("source", "seq_no", "primary_term")

    def __init__(self, source, seq_no, primary_term=1):
        self.source = source
        self.seq_no = seq_no
        self.primary_term = primary_term


class FakeIndices:
    def __init__(self, store):
        self._store = store

    def analyze(self, analyzer=None, text=None, **kwargs):
        if analyzer and "ik" in analyzer and not self._store.ik_enabled:
            raise RuntimeError("unknown analyzer [ik_max_word]")
        return {"tokens": []}

    def exists(self, index=None):
        return index in self._store.indices_created

    def exists_alias(self, name=None, index=None):
        return name in self._store.aliases

    def delete_alias(self, index=None, name=None, **kwargs):
        self._store.aliases.pop(name, None)
        return {"acknowledged": True}

    def create(self, index=None, settings=None, mappings=None, **kwargs):
        if index in self._store.indices_created:
            raise ConflictError("index already exists", meta={"status": 400})
        self._store.indices_created.add(index)
        self._store.mappings[index] = mappings or {}
        return {"acknowledged": True}

    def update_aliases(self, actions=None, **kwargs):
        for action in actions or []:
            for op, spec in action.items():
                if op == "add":
                    self._store.aliases[spec["alias"]] = spec["index"]
        return {"acknowledged": True}

    def refresh(self, index=None, **kwargs):
        return {"_shards": {"successful": 1}}

    def delete(self, index=None, **kwargs):
        self._store.indices_created.discard(index)
        self._store.aliases.pop(index, None)
        for alias in [a for a, i in self._store.aliases.items() if i == index]:
            del self._store.aliases[alias]
        return {"acknowledged": True}


def _tokens(value):
    if not isinstance(value, str):
        value = str(value or "")
    return [t for t in value.lower().replace(",", " ").replace("，", " ").split() if t]


class FakeElasticsearch:
    """Thread-safe in-memory ES with CAS + create-conflict semantics."""

    def __init__(self, ik_enabled=False):
        self.ik_enabled = ik_enabled
        self.indices = FakeIndices(self)
        self.indices_created = set()
        self.aliases = {}
        self.mappings = {}
        self._docs = {}  # index -> {id: _Doc}
        self._seq = 0
        self._lock = threading.RLock()
        # Fault injection hooks: callables checked before each op.
        self.fail_index = None  # fn(doc_dict) -> exception or None
        self.fail_search = None  # fn(index) -> exception or None
        self.after_cas = None  # fn() — runs after successful cas update

    def info(self, **kwargs):
        return {"version": {"number": "8.17.0"}, "cluster_name": "fake"}

    # -- document API ---------------------------------------------------------

    def index(self, index=None, id=None, document=None, op_type=None, routing=None, refresh=False):
        with self._lock:
            index = self._resolve(index)
            if self.fail_index and "_head" in index:
                exc = self.fail_index(document)
                if exc:
                    raise exc
            bucket = self._docs.setdefault(index, {})
            if op_type == "create" and id in bucket:
                raise ConflictError(
                    "version conflict, document already exists (create)",
                    meta={"status": 409},
                    body={"get": {"_source": bucket[id].source}},
                )
            self._seq += 1
            bucket[id] = _Doc(dict(document), self._seq)
            return {"result": "created", "_id": id}

    def get(self, index=None, id=None, routing=None):
        with self._lock:
            index = self._resolve(index)
            doc = self._docs.get(index, {}).get(id)
            if doc is None:
                raise NotFoundError("document missing", meta={"status": 404}, body={})
            return {
                "_id": id,
                "_index": index,
                "_source": dict(doc.source),
                "_seq_no": doc.seq_no,
                "_primary_term": doc.primary_term,
            }

    def update(self, index=None, id=None, doc=None, if_seq_no=None, if_primary_term=None, routing=None, **kwargs):
        with self._lock:
            index = self._resolve(index)
            bucket = self._docs.setdefault(index, {})
            existing = bucket.get(id)
            if existing is None:
                raise NotFoundError("document missing", meta={"status": 404}, body={})
            if if_seq_no is not None and (
                existing.seq_no != if_seq_no or existing.primary_term != (if_primary_term or 1)
            ):
                raise ConflictError(
                    "version conflict, required seq_no", meta={"status": 409}
                )
            existing.source.update(doc or {})
            self._seq += 1
            existing.seq_no = self._seq
            if self.after_cas:
                self.after_cas()
            return {"result": "updated"}

    def delete(self, index=None, id=None, routing=None, **kwargs):
        with self._lock:
            index = self._resolve(index)
            bucket = self._docs.setdefault(index, {})
            if id not in bucket:
                raise NotFoundError("document missing", meta={"status": 404}, body={})
            del bucket[id]
            return {"result": "deleted"}

    def delete_by_query(self, index=None, query=None, **kwargs):
        with self._lock:
            index = self._resolve(index)
            bucket = self._docs.setdefault(index, {})
            removed = 0
            for doc_id in list(bucket):
                if self._match_single(bucket[doc_id].source, query):
                    del bucket[doc_id]
                    removed += 1
            return {"deleted": removed}

    def search(self, index=None, size=None, query=None, knn=None, sort=None, body=None, **kwargs):
        with self._lock:
            index = self._resolve(index)
            if self.fail_search:
                exc = self.fail_search(index)
                if exc:
                    raise exc
            merged = body or {}
            if size is not None:
                merged["size"] = size
            if query is not None:
                merged["query"] = query
            if knn is not None:
                merged["knn"] = knn
            if sort is not None:
                merged["sort"] = sort
            hits = self._collect(index, merged)
            limit = merged.get("size", 10)
            return {"hits": {"hits": hits[:limit], "total": {"value": len(hits)}}}

    # -- query evaluation ------------------------------------------------------

    def _collect(self, index, body):
        bucket = self._docs.get(index, {})
        results = []
        for doc_id, doc in bucket.items():
            score = self._score(doc.source, body)
            if not score:  # None (no match) and False (filtered bool) both drop
                continue
            results.append(
                {
                    "_id": doc_id,
                    "_index": index,
                    "_score": score,
                    "_source": dict(doc.source),
                    "_seq_no": doc.seq_no,
                    "_primary_term": doc.primary_term,
                    "sort": [doc.source.get("_sort_key", 0)],
                }
            )
        sort_spec = body.get("sort") or []
        if sort_spec:
            field = list(sort_spec[0].keys())[0]
            order = sort_spec[0][field].get("order", "asc")

            def key(hit):
                value = hit["_source"].get(field)
                if isinstance(value, str):
                    return value
                return (0 if value is None else 1, value if value is not None else 0)

            results.sort(key=key, reverse=(order == "desc"))
        else:
            results.sort(key=lambda h: -(h["_score"] or 0))
        return results

    def _score(self, source, body):
        if "knn" in body:
            return self._knn_score(source, body["knn"])
        query = body.get("query", {"match_all": {}})
        return self._query_score(source, query)

    def _knn_score(self, source, knn):
        vector = source.get("vector")
        if not vector or source.get("embedding_status") != "ready":
            return None
        filt = knn.get("filter")
        if filt:
            inner = filt.get("bool", filt)
            if not self._match_bool(source, inner):
                return None
        query_vector = knn["query_vector"]
        dot = sum(a * b for a, b in zip(query_vector, vector))
        norm = (sum(a * a for a in query_vector) ** 0.5) * (sum(b * b for b in vector) ** 0.5)
        cosine = dot / norm if norm else 0.0
        return (1 + cosine) / 2  # ES cosine similarity score

    def _query_score(self, source, query):
        if "match_all" in query:
            return 1.0
        if "bool" in query:
            return self._match_bool(source, query["bool"])
        if "term" in query:
            field, value = next(iter(query["term"].items()))
            return 1.0 if source.get(field) == value else None
        if "range" in query:
            return self._match_range(source, query["range"])
        if "match" in query:
            field, spec = next(iter(query["match"].items()))
            text = spec["query"] if isinstance(spec, dict) else spec
            target_tokens = set(_tokens(source.get(field) or ""))
            query_tokens = set(_tokens(text))
            if not query_tokens:
                return None
            overlap = len(target_tokens & query_tokens)
            return (2.0 * overlap / max(len(query_tokens), 1)) if overlap else None
        return None

    def _match_bool(self, source, boolq):
        for clause in boolq.get("filter", []) + boolq.get("must", []):
            score = self._query_score(source, clause)
            if not score:
                return False
        should = boolq.get("should")
        if should is not None:
            msm = boolq.get("minimum_should_match", 1)
            matched = sum(1 for clause in should if self._query_score(source, clause))
            if matched < msm:
                return False
        for clause in boolq.get("must_not", []):
            if self._query_score(source, clause):
                return False
        return 1.0

    def _match_range(self, source, rangeq):
        field, spec = next(iter(rangeq.items()))
        value = source.get(field)
        if value is None:
            return None
        if "gt" in spec and not (value > spec["gt"]):
            return None
        if "lt" in spec and not (value < spec["lt"]):
            return None
        return 1.0

    def _match_single(self, source, query):
        return bool(self._query_score(source, query))

    # -- helpers -----------------------------------------------------------------

    def _resolve(self, index):
        if index in self.aliases:
            return self.aliases[index]
        return index

    def raw_docs(self, index):
        bucket = self._docs.get(index, {})
        return {doc_id: dict(doc.source) for doc_id, doc in bucket.items()}
