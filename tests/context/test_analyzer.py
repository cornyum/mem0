"""Analyzer v1: cross-backend CJK tokenization (design §6.3)."""

import re

import pytest

from mem0.context.analyzer import ANALYZER_VERSION, analyze


def test_latin_lowercased_and_kept():
    assert analyze("Hello World FOO") == "hello world foo"


def test_cjk_unigram_and_bigram():
    tokens = analyze("中文").split()
    assert tokens == ["u4e2d", "u6587", "b4e2d6587"]


def test_mixed_run_splits_segments():
    tokens = analyze("AI中文检索").split()
    assert "ai" in tokens
    assert "u4e2d" in tokens
    assert "b4e2d6587" in tokens
    # No token mixes latin and CJK
    assert all(re.fullmatch(r"[0-9a-z]+", t) for t in tokens)


def test_tokens_are_charset_safe():
    # The contract: whitespace-separated, strictly [0-9a-z] per token — so
    # ES standard, MySQL FULLTEXT, PG simple and SQLite unicode61 all keep
    # them whole (design ADR-6).
    for text in ["用户喜欢深色模式", "Mixed 混合 text-123", "日本語テスト", "emoji 😀 ok"]:
        for token in analyze(text).split():
            assert re.fullmatch(r"[0-9a-z]+", token), (text, token)


def test_punctuation_is_separator():
    assert analyze("hello, world! 你好。") == "hello world u4f60 u597d b4f60597d"


def test_query_and_index_paths_share_normalization():
    assert analyze("Dark模式") == analyze("dark 模式")


def test_duplicate_tokens_deduplicated_in_order():
    tokens = analyze("abc abc 中文中文").split()
    assert tokens.count("abc") == 1
    assert tokens.count("u4e2d") == 1


def test_non_string_raises():
    with pytest.raises(TypeError):
        analyze(123)


def test_version_pinned():
    # Changing the grammar is a deployment contract change (rebuild indexes).
    assert ANALYZER_VERSION == "agentar.analyzer.v1"
