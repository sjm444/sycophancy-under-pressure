"""Pull the A-D answer out of a model's free-text reply.

Models are told to end with ``FINAL_ANSWER: X``, but they sometimes repeat
the instruction earlier, wrap the letter in markdown or parentheses, or just
write "The correct answer is B." Parsing lives here so those cases can be
tested, and so each result records which path matched (primary, fallback,
or failed).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

ParseMethod = Literal["primary", "fallback", "failed"]

# Models sometimes repeat the prompt instruction ("I will end with
# FINAL_ANSWER: A or B") before giving their actual answer at the end.
# Using findall() and taking the *last* match avoids capturing those earlier
# mentions. The negative lookahead (?!\w) prevents matching the first letter
# of a word (e.g., "FINAL_ANSWER: Apple" will not match "A").
PRIMARY_PATTERN = re.compile(
    r"FINAL_ANSWER:\s*\**\(?\**([A-D])\**\)?\**(?!\w)",
    re.IGNORECASE,
)

# Fallback handles models that ignore the exact FINAL_ANSWER tag and
# write natural prose such as "Therefore, the correct answer is **C**."
FALLBACK_PATTERN = re.compile(
    r"(?:(?:the\s+)?(?:final\s+|correct\s+|best\s+)?answer\s*(?:is|:)\s*"
    r"(?:option\s+|choice\s+)?)\**\(?\**([A-D])\**\)?\**(?!\w)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ParseResult:
    """Structured result from extracting a multiple-choice answer letter.

    Attributes:
        answer: Extracted uppercase letter (``"A"``-``"D"``), or ``None`` if
            no answer could be extracted.
        method: Which parser rule succeeded: ``"primary"``, ``"fallback"``,
            or ``"failed"``.
    """

    answer: str | None
    method: ParseMethod


def parse_answer(text: str | None) -> ParseResult:
    """Extract the multiple-choice answer letter (A-D) from a model response.

    Tries ``PRIMARY_PATTERN`` first (taking the last match), then falls back
    to ``FALLBACK_PATTERN`` (also taking the last match). Returns a failed
    ``ParseResult`` if neither matches.

    Args:
        text: Raw assistant response text. ``None`` or empty strings return
            a failed parse.

    Returns:
        A ``ParseResult`` with the uppercase letter (or ``None``) and the
        parsing method used.

    Example:
        >>> parse_answer("Let's think. FINAL_ANSWER: **B**")
        ParseResult(answer='B', method='primary')
        >>> parse_answer("So the correct answer is (C).")
        ParseResult(answer='C', method='fallback')
        >>> parse_answer("I am not sure about this one.")
        ParseResult(answer=None, method='failed')
    """
    if not text:
        return ParseResult(answer=None, method="failed")

    primary_matches = PRIMARY_PATTERN.findall(text)
    if primary_matches:
        return ParseResult(answer=primary_matches[-1].upper(), method="primary")

    fallback_matches = FALLBACK_PATTERN.findall(text)
    if fallback_matches:
        return ParseResult(answer=fallback_matches[-1].upper(), method="fallback")

    return ParseResult(answer=None, method="failed")
