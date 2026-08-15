"""Content hashing: stability, order-insensitivity, domain separation."""

from mem0.context.hashing import entry_content_hash


def test_hash_stable():
    a = entry_content_hash(kind="fact", text="用户偏好深色模式")
    b = entry_content_hash(kind="fact", text="用户偏好深色模式")
    assert a == b


def test_kind_changes_hash():
    a = entry_content_hash(kind="fact", text="same")
    b = entry_content_hash(kind="preference", text="same")
    assert a != b


def test_categories_change_hash():
    a = entry_content_hash(kind="fact", text="same", categories=("a",))
    b = entry_content_hash(kind="fact", text="same", categories=("b",))
    assert a != b


def test_ref_order_does_not_change_hash():
    a = entry_content_hash(kind="fact", text="same", source_refs=("s1", "s2"))
    b = entry_content_hash(kind="fact", text="same", source_refs=("s2", "s1"))
    assert a == b


def test_domain_separation_from_plain_sha256():
    import hashlib

    raw = hashlib.sha256("fact\0same".encode()).hexdigest()
    assert entry_content_hash(kind="fact", text="same") != raw
