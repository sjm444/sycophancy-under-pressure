"""Unit tests for ``src/parsing.py``.

Covers primary regex extraction, last-match precedence, case/markdown/paren
variations, natural-language fallback parsing, and unparseable responses.
"""

from __future__ import annotations

from src.parsing import ParseResult, parse_answer


def test_primary_simple() -> None:
    """Basic FINAL_ANSWER: X line is extracted via primary method."""
    res = parse_answer("Because 2 + 2 = 4.\nFINAL_ANSWER: B")
    assert res == ParseResult(answer="B", method="primary")


def test_primary_takes_last_match() -> None:
    """When FINAL_ANSWER appears multiple times, the last occurrence wins."""
    text = (
        "Initially I thought FINAL_ANSWER: A, wait let me double check. "
        "Actually the reasoning shows it must be D.\n"
        "FINAL_ANSWER: D"
    )
    res = parse_answer(text)
    assert res == ParseResult(answer="D", method="primary")


def test_primary_lowercase() -> None:
    """Matching is case-insensitive and normalizes the answer to uppercase."""
    res = parse_answer("Reasoning here.\nfinal_answer: c")
    assert res == ParseResult(answer="C", method="primary")


def test_primary_markdown_bold() -> None:
    """Markdown bold around the answer letter is stripped cleanly."""
    res = parse_answer("Reasoning here.\nFINAL_ANSWER: **A**")
    assert res == ParseResult(answer="A", method="primary")


def test_primary_parentheses_and_bold() -> None:
    """Parentheses (with or without bold) around the letter are handled."""
    assert parse_answer("FINAL_ANSWER: (B)") == ParseResult(answer="B", method="primary")
    assert parse_answer("FINAL_ANSWER: **(D)**") == ParseResult(answer="D", method="primary")


def test_fallback_phrasing() -> None:
    """Natural-language 'answer is X' phrasing triggers fallback parsing."""
    text = "After checking the options, the correct answer is C."
    res = parse_answer(text)
    assert res == ParseResult(answer="C", method="fallback")


def test_fallback_takes_last_match() -> None:
    """Fallback also takes the last 'answer is X' match in the response."""
    text = (
        "You might think the answer is A, "
        "but upon closer inspection the final answer is **B**."
    )
    res = parse_answer(text)
    assert res == ParseResult(answer="B", method="fallback")


def test_no_answer_fails() -> None:
    """Responses with no identifiable A-D answer return failed."""
    assert parse_answer("I really cannot decide between these.") == ParseResult(
        answer=None, method="failed"
    )
    assert parse_answer("") == ParseResult(answer=None, method="failed")
    assert parse_answer(None) == ParseResult(answer=None, method="failed")


def test_does_not_match_word_prefix() -> None:
    """A word starting with A-D (like 'Apple') is not falsely parsed as A."""
    assert parse_answer("FINAL_ANSWER: Apple") == ParseResult(answer=None, method="failed")
    assert parse_answer("The answer is definitely clear.") == ParseResult(
        answer=None, method="failed"
    )
