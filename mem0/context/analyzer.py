"""Analyzer v1: cross-backend CJK tokenization baseline (design §6.3).

Produces a whitespace-separated token stream whose tokens are strictly
``[0-9a-z]`` so that *every* backend tokenizer (Elasticsearch standard,
MySQL FULLTEXT, PostgreSQL ``simple``, SQLite unicode61) keeps them whole:

- Latin runs are lowercased and kept verbatim.
- Each CJK codepoint becomes a unigram ``uXXXX`` (hex codepoint).
- Each adjacent CJK pair becomes a bigram ``bXXXXYYYY``.
- Everything else is a separator.

On the ES tier the ik plugin (``text_zh`` field) is the primary Chinese
channel; this analyzer remains the baseline for every other medium and for
the MySQL FTS sidecar, and it feeds ``text_lemmatized``/``searchable_text``.

Pure function, zero dependencies. Changing the token grammar is a deployment
contract change: indexes must be rebuilt (design ADR-6).
"""

import re
import unicodedata

ANALYZER_VERSION = "agentar.analyzer.v1"

_CJK_RANGES = (
    (0x4E00, 0x9FFF),  # CJK Unified Ideographs
    (0x3400, 0x4DBF),  # Extension A
    (0xF900, 0xFAFF),  # Compatibility Ideographs
    (0x3040, 0x30FF),  # Hiragana + Katakana
)
_TOKEN_RE = re.compile(r"[0-9A-Za-z\u3400-\u4DBF\u4E00-\u9FFF\uF900-\uFAFF\u3040-\u30FF]+")


def _is_cjk(codepoint: int) -> bool:
    return any(start <= codepoint <= end for start, end in _CJK_RANGES)


def analyze(text: str) -> str:
    """Normalize ``text`` into a whitespace-separated token stream. Tokens
    contain only ``[0-9a-z]`` — no punctuation — so no backend tokenizer can
    split them mid-token."""
    if not isinstance(text, str):
        raise TypeError(f"analyze() expects str, got {type(text).__name__}")
    normalized = unicodedata.normalize("NFC", text)
    tokens: list[str] = []
    latin_run: list[str] = []
    cjk_run: list[str] = []

    def _flush_latin():
        if latin_run:
            tokens.append("".join(latin_run).lower())
            latin_run.clear()

    def _flush_cjk():
        if cjk_run:
            tokens.extend(_cjk_tokens("".join(cjk_run)))
            cjk_run.clear()

    for run in _TOKEN_RE.findall(normalized):
        for ch in run:
            if _is_cjk(ord(ch)):
                _flush_latin()
                cjk_run.append(ch)
            else:
                _flush_cjk()
                latin_run.append(ch)
        _flush_latin()
        _flush_cjk()
    # Deduplicate while preserving order to bound index bloat from bigrams.
    seen: set[str] = set()
    ordered: list[str] = []
    for token in tokens:
        if token not in seen:
            seen.add(token)
            ordered.append(token)
    return " ".join(ordered)


def _cjk_tokens(segment: str) -> list[str]:
    tokens = [f"u{ord(ch):04x}" for ch in segment]
    tokens.extend(f"b{ord(a):04x}{ord(b):04x}" for a, b in zip(segment, segment[1:]))
    return tokens


def has_cjk(text: str) -> bool:
    """True when the text contains any CJK codepoint — the signal for
    lemmatize_for_bm25 to take the analyzer path instead of spaCy."""
    if not isinstance(text, str):
        return False
    return any(_is_cjk(ord(ch)) for ch in text)
