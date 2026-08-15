"""ScopeIdentity: validation, canonical encoding, write-time key derivation."""

import pytest

from mem0.context.errors import ContextValidationError
from mem0.context.scope import ScopeIdentity


def test_at_least_one_id_required():
    with pytest.raises(ContextValidationError):
        ScopeIdentity()


def test_none_and_blank_ids_are_omitted():
    scope = ScopeIdentity(tenant_id="t1", user_id=None, agent_id="  ")
    assert scope.fields == {"tenant_id": "t1"}


def test_key_is_deterministic_and_field_order_insensitive():
    a = ScopeIdentity(tenant_id="t1", user_id="u1")
    b = ScopeIdentity(user_id="u1", tenant_id="t1")
    assert a.scope_key == b.scope_key


def test_different_id_sets_produce_different_keys():
    a = ScopeIdentity(tenant_id="t1", user_id="u1")
    b = ScopeIdentity(tenant_id="t1", user_id="u1", session_id="s1")
    assert a.scope_key != b.scope_key


def test_superset_scope_is_a_distinct_write_scope():
    # Dedup boundary is the exact id set present at write time (design §3.3):
    # remembering the same fact under a narrower scope is a separate entry,
    # mirroring mem0's filter semantics.
    narrow = ScopeIdentity(tenant_id="t1", user_id="u1")
    wide = ScopeIdentity(tenant_id="t1", user_id="u1", run_id="r1")
    assert narrow.scope_key != wide.scope_key


def test_ids_are_nfc_normalized_and_stripped():
    scope = ScopeIdentity(user_id="  café   ")
    assert scope.fields["user_id"] == "café"


def test_oversized_id_rejected():
    with pytest.raises(ContextValidationError):
        ScopeIdentity(user_id="x" * 257)


def test_control_characters_rejected():
    with pytest.raises(ContextValidationError):
        ScopeIdentity(user_id="bad\x01id")


def test_non_string_id_rejected():
    with pytest.raises(ContextValidationError):
        ScopeIdentity(user_id=123)


def test_scope_key_is_sha256_hex():
    scope = ScopeIdentity(user_id="u1")
    assert len(scope.scope_key) == 64
    int(scope.scope_key, 16)  # parses as hex
