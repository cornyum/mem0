import json
import logging
import threading
from copy import deepcopy
from typing import Any, Callable, Dict

logger = logging.getLogger()

_state_lock = threading.RLock()
_current_config: Dict[str, Any] = {}
_app_service = None  # MemoryApplicationService (v3 storage modes)
_memory_instance = None  # LegacyMemoryAdapter facade over the service
_session_factory: Callable | None = None


def set_session_factory(factory: Callable) -> None:
    global _session_factory
    _session_factory = factory


class _OverridesLoadFailed(Exception):
    """Distinguishes 'no stored overrides' from 'storage read failed'."""


def _load_overrides() -> Dict[str, Any]:
    try:
        if _session_factory is None:
            return {}
        from models import Settings

        session = _session_factory()
        try:
            row = session.get(Settings, "config_overrides")
            if row is None:
                return {}
            return json.loads(row.value)
        finally:
            session.close()
    except _OverridesLoadFailed:
        raise
    except Exception as exc:
        raise _OverridesLoadFailed(str(exc)) from exc


def _save_overrides(overrides: Dict[str, Any]) -> None:
    try:
        if _session_factory is None:
            return
        from models import Settings

        session = _session_factory()
        try:
            serialized = json.dumps(overrides)
            dialect_name = session.bind.dialect.name if session.bind is not None else ""
            if dialect_name == "mysql":
                from sqlalchemy.dialects.mysql import insert

                stmt = (
                    insert(Settings)
                    .values(key="config_overrides", value=serialized)
                    .on_duplicate_key_update(value=serialized)
                )
            else:
                from sqlalchemy.dialects.postgresql import insert

                stmt = (
                    insert(Settings)
                    .values(key="config_overrides", value=serialized)
                    .on_conflict_do_update(
                        index_elements=[Settings.key],
                        set_={"value": serialized},
                    )
                )
            session.execute(stmt)
            session.commit()
        finally:
            session.close()
    except Exception:
        logging.warning("Failed to persist config overrides to database", exc_info=True)


def _merge_config(base: Dict[str, Any], updates: Dict[str, Any]) -> Dict[str, Any]:
    merged = deepcopy(base)
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_config(merged[key], value)
        else:
            merged[key] = value
    return merged


CATEGORIES_SETTINGS_KEY = "memory_categories"


def apply_category_instructions(config: Dict[str, Any]) -> Dict[str, Any]:
    """Compile the deployment's category taxonomy into extraction instructions.

    Reads the category definitions from Settings (key=memory_categories) and, when
    a non-empty taxonomy exists, appends the compiled instruction block to the
    config's ``custom_instructions``. Returns a new dict; the input config is never
    mutated, so repeated instance rebuilds never accumulate duplicated blocks.
    """
    try:
        if _session_factory is None:
            return config
        from models import Settings

        session = _session_factory()
        try:
            row = session.get(Settings, CATEGORIES_SETTINGS_KEY)
            if row is None or not row.value:
                return config
            definitions = json.loads(row.value).get("categories") or []
        finally:
            session.close()
    except Exception:
        return config

    definitions = [d for d in definitions if isinstance(d, dict) and str(d.get("name") or "").strip()]
    if not definitions:
        return config

    lines = [
        "### Memory Categories",
        "This deployment defines an official category taxonomy. For EVERY memory object in your output, "
        'add a "categories" array containing zero or more names chosen ONLY from the list below. '
        "Use the descriptions to decide.",
    ]
    for definition in definitions:
        name = str(definition.get("name")).strip()
        description = str(definition.get("description") or "").strip() or "（无描述）"
        lines.append(f"- {name}: {description}")
    example_name = str(definitions[0].get("name")).strip()
    lines.append(f'Example output object: {{"id": "0", "text": "...", "categories": ["{example_name}"]}}')
    lines.append("If no category fits, use an empty array. Never invent names outside the list.")
    block = "\n".join(lines)

    next_config = deepcopy(config)
    existing = next_config.get("custom_instructions")
    next_config["custom_instructions"] = f"{existing}\n\n{block}" if existing else block
    return next_config


def _forbid_storage_topology_changes(updates: Dict[str, Any]) -> None:
    """ADR-7: storage mode and provider are fixed at startup — a /configure
    attempt to change them is rejected instead of half-applied."""
    import context_runtime

    vector_store = updates.get("vector_store") or {}
    if isinstance(vector_store, dict):
        provider = vector_store.get("provider")
        if provider and provider.strip().lower() != "elasticsearch":
            raise ValueError(
                f"VECTOR_STORE_PROVIDER is fixed to elasticsearch in {context_runtime.STORAGE_MODE} mode; "
                "restart with MEMORY_STORAGE_MODE/VECTOR_STORE_PROVIDER changes instead."
            )


def _build_memory(config: Dict[str, Any]):
    """Single construction point (design §8.2 discipline): builds the
    MemoryApplicationService over ES for both v3 storage modes and wraps it in
    the LegacyMemoryAdapter so REST/MCP/Legacy share one domain entry point."""
    import context_runtime
    from legacy_adapter import LegacyMemoryAdapter
    from mem0.context.observability import ObservableEmbedder, ObservableLLM
    from mem0.context.vdb import MemoryApplicationService

    es_config = (config.get("vector_store") or {}).get("config") or {}
    service = MemoryApplicationService.from_config(
        apply_category_instructions(config),
        es_host=es_config.get("host", "elasticsearch"),
        es_port=int(es_config.get("port", 9200)),
        es_user=es_config.get("user"),
        es_password=es_config.get("password"),
        es_use_ssl=bool(es_config.get("use_ssl", False)),
        es_verify_certs=bool(es_config.get("verify_certs", False)),
        es_prefix=es_config.get("collection_name", "agentar_mem0"),
        storage_mode=context_runtime.STORAGE_MODE,
        hybrid_sidecar=context_runtime.get_hybrid_sidecar(),
        obs=context_runtime.get_observability(),
    )
    obs = context_runtime.get_observability()
    if service.llm is not None:
        service.llm = ObservableLLM(service.llm, obs)
    if service.embedder is not None:
        service.embedder = ObservableEmbedder(service.embedder, obs)
        service.writer.embedder = service.embedder
        service.recaller.embedder = service.embedder
        service.embedder_reconciler.embedder = service.embedder
    context_runtime.register_v3_readiness(service)
    return service, LegacyMemoryAdapter(service, config=config)


def initialize_state(default_config: Dict[str, Any]) -> None:
    global _current_config, _app_service, _memory_instance
    with _state_lock:
        _current_config = deepcopy(default_config)
        overrides = _load_overrides()
        if overrides:
            try:

                _forbid_storage_topology_changes(overrides)
            except ValueError as exc:
                logger.warning("Dropping incompatible stored overrides: %s", str(exc))
                overrides = {}
            _current_config = _merge_config(_current_config, overrides)
        _app_service, _memory_instance = _build_memory(_current_config)


def update_config(updates: Dict[str, Any]) -> Dict[str, Any]:
    global _current_config, _app_service, _memory_instance
    with _state_lock:
        _forbid_storage_topology_changes(updates)
        next_config = _merge_config(_current_config, updates)
        # build FIRST: a failed construction must not leave a phantom config
        # paired with the stale service (server review #4)
        app_service, memory_instance = _build_memory(next_config)
        _current_config = next_config
        _app_service = app_service
        _memory_instance = memory_instance
        if updates:
            # Skip the read-modify-write for refresh-only calls (empty updates):
            # with multiple uvicorn workers there is no cross-process lock, so a
            # redundant rewrite could roll back another worker's concurrent
            # POST /configure persist.
            try:
                overrides = _load_overrides()
            except _OverridesLoadFailed:
                # a transient read failure must not wipe what is stored
                # (server review #19); the in-memory build stays authoritative
                logging.warning("config_overrides load failed; persist skipped", exc_info=True)
                return deepcopy(_current_config)
            overrides = _merge_config(overrides, updates)
            _save_overrides(overrides)
        return deepcopy(_current_config)


def get_current_config() -> Dict[str, Any]:
    with _state_lock:
        return deepcopy(_current_config)


def get_app_service():
    """The MemoryApplicationService — the only object REST /v1 and MCP call."""
    with _state_lock:
        if _app_service is None:
            raise RuntimeError("Mem0 runtime has not been initialized.")
        return _app_service


def get_memory_instance():
    """Legacy-compatible facade (LegacyMemoryAdapter) for /memories routes."""
    with _state_lock:
        if _memory_instance is None:
            raise RuntimeError("Mem0 runtime has not been initialized.")
        return _memory_instance
