import json
import logging
import threading
from copy import deepcopy
from typing import Any, Callable, Dict

from mem0 import Memory
from mem0.context.power_memory import PowerMemory

_state_lock = threading.RLock()
_current_config: Dict[str, Any] = {}
_memory_instance: PowerMemory | None = None
_session_factory: Callable | None = None


def set_session_factory(factory: Callable) -> None:
    global _session_factory
    _session_factory = factory


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
    except Exception:
        return {}


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
    Returns the config unchanged when no session factory is wired, storage fails,
    or no taxonomy is defined.
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


def _build_memory(config: Dict[str, Any]) -> PowerMemory:
    """Single construction point for the memory instance (design §8.2):
    initialize_state and update_config share it, so context wiring (ctx
    store, readiness probes, observability wrappers) is applied on every
    hot rebuild — an update_config can never leave a stale or unwrapped
    instance behind."""
    import context_runtime
    from mem0.context.observability import wrap_memory_for_observation

    memory = PowerMemory.from_config(
        apply_category_instructions(config),
        ctx_store=context_runtime.get_context_store(),
        obs=context_runtime.get_observability(),
    )
    wrap_memory_for_observation(memory, context_runtime.get_observability())
    context_runtime.set_memory_instance(memory)
    return memory


def initialize_state(default_config: Dict[str, Any]) -> None:
    global _current_config, _memory_instance
    with _state_lock:
        _current_config = deepcopy(default_config)
        overrides = _load_overrides()
        if overrides:
            _current_config = _merge_config(_current_config, overrides)
        _memory_instance = _build_memory(_current_config)


def update_config(updates: Dict[str, Any]) -> Dict[str, Any]:
    global _current_config, _memory_instance
    with _state_lock:
        next_config = _merge_config(_current_config, updates)
        _current_config = next_config
        _memory_instance = _build_memory(next_config)
        if updates:
            # Skip the read-modify-write for refresh-only calls (empty updates):
            # with multiple uvicorn workers there is no cross-process lock, so a
            # redundant rewrite could roll back another worker's concurrent
            # POST /configure persist.
            overrides = _load_overrides()
            overrides = _merge_config(overrides, updates)
            _save_overrides(overrides)
        return deepcopy(_current_config)


def get_current_config() -> Dict[str, Any]:
    with _state_lock:
        return deepcopy(_current_config)


def get_memory_instance() -> Memory:
    with _state_lock:
        if _memory_instance is None:
            raise RuntimeError("Mem0 runtime has not been initialized.")
        return _memory_instance
