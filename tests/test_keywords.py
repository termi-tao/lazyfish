"""Keyword extraction.

The bar is low on purpose: identifier and path shapes, no NLP. These tests pin
the shapes that are recognised and, just as importantly, the noise that is not.
"""

from __future__ import annotations

from lazyfish.keywords import MAX_KEYWORDS, extract_keywords


def test_python_identifiers_and_paths() -> None:
    text = (
        "The constant RESET_TOKEN_TTL in src/auth/reset_token.py is wrong; "
        "build_reset_link then returns a dead URL."
    )
    terms = extract_keywords(text)
    assert "src/auth/reset_token.py" in terms
    assert "build_reset_link" in terms


def test_typescript_and_sql_terms() -> None:
    terms = extract_keywords(
        "The `pageSize` prop of DataTable is ignored, and the query on "
        "audit_log_entries has no LIMIT clause."
    )
    assert "pageSize" in terms
    assert "DataTable" in terms
    assert "audit_log_entries" in terms


def test_backticked_spans_win_over_prose() -> None:
    terms = extract_keywords("Set `feature_flags.enabled` before calling the endpoint")
    assert terms[0] == "feature_flags.enabled"


def test_quoted_prose_is_not_treated_as_a_term() -> None:
    terms = extract_keywords('The button says "Save and continue" but does not save')
    assert "Save and continue" not in terms


def test_stopwords_and_numbers_are_dropped() -> None:
    terms = extract_keywords("The user should get an error for the url 1.2.3 in 2026")
    assert "the" not in [term.lower() for term in terms]
    assert "1.2.3" not in terms
    assert "user" not in [term.lower() for term in terms]


def test_result_is_capped_and_deduplicated() -> None:
    text = " ".join(f"handler_number_{index}" for index in range(20)) * 2
    terms = extract_keywords(text)
    assert len(terms) == MAX_KEYWORDS
    assert len(set(terms)) == len(terms)


def test_empty_input_yields_nothing() -> None:
    assert extract_keywords("", "   ") == []


def test_non_latin_prose_still_yields_its_identifiers() -> None:
    """A ticket written in another language must still produce useful terms.

    The prose is ignored, which is the honest outcome of a mechanical
    extractor; the identifiers embedded in it are what the search needs.
    """
    chinese = "登录失败，请检查 auth_service.py "
    terms = extract_keywords(chinese + "and the `LoginHandler` class")
    assert "auth_service.py" in terms
    assert "LoginHandler" in terms


def test_multiple_sources_are_merged_in_order() -> None:
    terms = extract_keywords(
        "title mentions src/main.py",
        "the description mentions parse_config",
        "a comment mentions ResponseBuilder",
    )
    assert terms.index("src/main.py") < terms.index("parse_config")
    assert "ResponseBuilder" in terms
