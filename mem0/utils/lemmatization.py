"""
BM25 lemmatization for consistent keyword matching.

Uses spaCy's lemmatizer for better handling of:
- Verb forms: attending/attends/attended -> attend
- Comparatives/superlatives: older/oldest -> old
- Plurals: memories -> memory
- Avoids over-stemming: organization != organize

Also includes original -ing forms alongside lemmas to handle cases
where spaCy's context-dependent lemmatization produces inconsistent
results (e.g., "meeting" as noun vs verb -> different lemmas).

Text containing CJK codepoints takes the Analyzer v1 path instead
(design §6.3): spaCy's English models do not segment Chinese, so the
"lemma" of a CJK run is the run itself and keyword search silently
degrades to whole-sentence matching. Analyzer v1 emits unigram+bigram
tokens ([0-9a-z] only) that every backend tokenizer keeps whole.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def lemmatize_for_bm25(text: str) -> str:
    """Lemmatize text for BM25 matching.

    CJK-containing text is analyzer-tokenized (write and query sides share
    the same normalization); pure Latin text keeps the spaCy lemma path and
    falls back to the original text if spaCy is unavailable.
    """
    from mem0.context.analyzer import has_cjk

    if has_cjk(text):
        from mem0.context.analyzer import analyze

        return analyze(text)

    from mem0.utils.spacy_models import get_nlp_lemma

    nlp = get_nlp_lemma()
    if nlp is None:
        return text

    doc = nlp(text.lower())
    tokens = []

    for token in doc:
        if token.is_punct or token.is_stop:
            continue

        lemma = token.lemma_
        if lemma.isalnum():
            tokens.append(lemma)

        # Also add original if it ends in -ing and differs from lemma.
        # This handles noun/verb ambiguity (meeting/meet, attending/attend).
        if token.text.endswith("ing") and token.text != lemma and token.text.isalnum():
            tokens.append(token.text)

    return " ".join(tokens)
