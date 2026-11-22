"""Validation tests for the generated ``data/questions.jsonl`` dataset.

Skips cleanly with an informative message if the dataset has not been built yet.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest
import yaml

from src.build_dataset import LETTERS, Question

DATASET_PATH = Path("data/questions.jsonl")
CONFIG_PATH = Path("config.yaml")


@pytest.mark.skipif(
    not DATASET_PATH.exists(),
    reason="data/questions.jsonl not found; run `python -m src.build_dataset` first.",
)
def test_dataset_schema_and_invariants() -> None:
    """Verify schema validity, choice counts, unique IDs, and category balance."""
    lines = [
        line
        for line in DATASET_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(lines) > 0, "data/questions.jsonl is empty."

    questions = [Question.model_validate(json.loads(line)) for line in lines]

    ids = [q.id for q in questions]
    assert len(ids) == len(set(ids)), "Duplicate question IDs found."

    for q in questions:
        assert set(q.choices.keys()) == set(LETTERS)
        assert all(text.strip() for text in q.choices.values())
        assert q.correct_answer in LETTERS
        assert q.target_wrong_answer in LETTERS
        assert q.target_wrong_answer != q.correct_answer

    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    expected_per_cat = int(cfg["questions_per_category"])
    cat_counts = Counter(q.category for q in questions)
    assert set(cat_counts.keys()) == {
        "logic_and_math",
        "science_and_cs",
        "common_misconceptions",
    }
    for cat, count in cat_counts.items():
        assert count == expected_per_cat, (
            f"Category {cat} has {count} questions, expected {expected_per_cat}."
        )
