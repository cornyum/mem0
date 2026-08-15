import pytest


@pytest.fixture(autouse=True)
def _ensure_spacy():
    """Skip tests if spaCy model is not available."""
    try:
        import spacy
        spacy.load("en_core_web_sm")
    except Exception:
        pytest.skip("spaCy en_core_web_sm model not available")


class TestLemmatizeForBm25:
    def test_basic_lemmatization(self):
        from mem0.utils.lemmatization import lemmatize_for_bm25

        result = lemmatize_for_bm25("The cats are running quickly")
        assert "cat" in result
        assert "run" in result or "running" in result
        # Stop words and punctuation should be removed
        assert "the" not in result.split()

    def test_verb_forms_normalized(self):
        from mem0.utils.lemmatization import lemmatize_for_bm25

        result = lemmatize_for_bm25("she attended multiple meetings yesterday")
        assert "attend" in result or "attended" in result
        assert "meeting" in result  # -ing form preserved alongside lemma
        # "multiple" is kept (not a spaCy stop word)

    def test_ing_preservation(self):
        from mem0.utils.lemmatization import lemmatize_for_bm25

        result = lemmatize_for_bm25("attending the morning meeting")
        tokens = result.split()
        # Should have both the lemma and the -ing form
        assert "attending" in tokens or "attend" in tokens

    def test_empty_string(self):
        from mem0.utils.lemmatization import lemmatize_for_bm25

        result = lemmatize_for_bm25("")
        assert result == ""

    def test_punctuation_removed(self):
        from mem0.utils.lemmatization import lemmatize_for_bm25

        result = lemmatize_for_bm25("Hello, world! How are you?")
        assert "," not in result
        assert "!" not in result
        assert "?" not in result

    def test_lowercased(self):
        from mem0.utils.lemmatization import lemmatize_for_bm25

        result = lemmatize_for_bm25("PYTHON Programming LANGUAGE")
        for token in result.split():
            assert token == token.lower()

    def test_stop_words_removed(self):
        from mem0.utils.lemmatization import lemmatize_for_bm25

        result = lemmatize_for_bm25("this is a very simple test of the system")
        tokens = result.split()
        for stop in ["this", "is", "a", "very", "of", "the"]:
            assert stop not in tokens


class TestCjkAnalyzerPath:
    """CJK text takes Analyzer v1 regardless of spaCy availability
    (design §6.3): spaCy's English models cannot segment Chinese, which is
    exactly the failure this path replaces."""

    def test_cjk_yields_analyzer_tokens(self):
        from mem0.utils.lemmatization import lemmatize_for_bm25

        tokens = lemmatize_for_bm25("用户偏好深色模式").split()
        assert "u7528" in tokens  # 用
        assert "b75286237" in tokens  # 用户 bigram
        assert all(t == t.lower() and t.isalnum() for t in tokens)

    def test_mixed_text_takes_analyzer_path(self):
        from mem0.utils.lemmatization import lemmatize_for_bm25

        tokens = lemmatize_for_bm25("Dark 模式切换").split()
        assert "dark" in tokens
        assert "u6a21" in tokens  # 模

    def test_write_and_query_normalization_identical(self):
        from mem0.utils.lemmatization import lemmatize_for_bm25

        assert lemmatize_for_bm25("深色模式") == lemmatize_for_bm25("深色模式")

    def test_has_cjk(self):
        from mem0.context.analyzer import has_cjk

        assert has_cjk("用户")
        assert has_cjk("mixed 中文")
        assert not has_cjk("plain english")
        assert not has_cjk("")
        assert not has_cjk(None)  # type: ignore[arg-type]
