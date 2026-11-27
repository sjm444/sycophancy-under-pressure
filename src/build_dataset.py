"""Build the 300-question dataset from public benchmarks.

I pull questions from MMLU-Redux 2.0 and TruthfulQA instead of writing them,
because a wrong answer key looks exactly like a model failure in this eval.
MMLU-Redux rows flagged with label errors are dropped, and sampling is seeded
so the dataset is reproducible.

Writes ``data/questions.jsonl`` and ``data/SPOT_CHECK.md``.
"""

from __future__ import annotations

import argparse
import random
from collections import Counter
from pathlib import Path
from typing import Literal

import yaml
from datasets import load_dataset
from pydantic import BaseModel, Field, field_validator, model_validator

CategoryName = Literal["logic_and_math", "science_and_cs", "common_misconceptions"]
AnswerLetter = Literal["A", "B", "C", "D"]

LETTERS: tuple[AnswerLetter, ...] = ("A", "B", "C", "D")

# Verified Hugging Face dataset IDs (inspected against the Hub before use).
MMLU_REDUX_DATASET_ID: str = "edinburgh-dawg/mmlu-redux-2.0"
TRUTHFULQA_DATASET_ID: str = "truthfulqa/truthful_qa"
TRUTHFULQA_CONFIG: str = "multiple_choice"

LOGIC_AND_MATH_SUBJECTS: tuple[str, ...] = (
    "formal_logic",
    "elementary_mathematics",
    "high_school_mathematics",
    "high_school_statistics",
)

SCIENCE_AND_CS_SUBJECTS: tuple[str, ...] = (
    "high_school_physics",
    "high_school_biology",
    "college_biology",
    "high_school_computer_science",
    "college_computer_science",
)

CATEGORY_ID_PREFIX: dict[CategoryName, str] = {
    "logic_and_math": "lm",
    "science_and_cs": "sc",
    "common_misconceptions": "cm",
}


class Question(BaseModel):
    """Schema for one multiple-choice evaluation item.

    Attributes:
        id: Short unique identifier with a category prefix (e.g. ``"lm_001"``).
        category: One of the three benchmark categories.
        source: Benchmark and subject provenance (e.g. ``"mmlu-redux/formal_logic"``).
        question: Question text presented on Turn 1.
        choices: Mapping from ``"A"``-``"D"`` to option strings.
        correct_answer: Ground-truth answer letter (``"A"``-``"D"``).
        target_wrong_answer: Seeded-random incorrect letter used when steering
            the model toward a wrong answer on Turn 2.
    """

    id: str = Field(..., min_length=1)
    category: CategoryName
    source: str = Field(..., min_length=1)
    question: str = Field(..., min_length=1)
    choices: dict[AnswerLetter, str]
    correct_answer: AnswerLetter
    target_wrong_answer: AnswerLetter

    @field_validator("choices")
    @classmethod
    def _validate_four_choices(
        cls, value: dict[AnswerLetter, str]
    ) -> dict[AnswerLetter, str]:
        """Ensure choices contain exactly the keys A, B, C, and D."""
        if set(value.keys()) != set(LETTERS):
            raise ValueError(f"choices must have keys {LETTERS}, got {sorted(value.keys())}")
        return value

    @model_validator(mode="after")
    def _validate_target_differs_from_correct(self) -> Question:
        """Ensure the sycophancy pushback target is actually a wrong answer."""
        if self.target_wrong_answer == self.correct_answer:
            raise ValueError(
                f"target_wrong_answer ({self.target_wrong_answer}) must differ "
                f"from correct_answer ({self.correct_answer})."
            )
        return self


def _pick_target_wrong_answer(
    correct_answer: AnswerLetter, rng: random.Random
) -> AnswerLetter:
    """Choose a seeded-random wrong answer letter for Turn 2 pushback.

    Args:
        correct_answer: The ground-truth letter to exclude.
        rng: Seeded random generator.

    Returns:
        One of the three incorrect letters in ``("A", "B", "C", "D")``.
    """
    wrong_letters = [letter for letter in LETTERS if letter != correct_answer]
    return rng.choice(wrong_letters)


def _load_mmlu_redux_category(
    category: CategoryName,
    subjects: tuple[str, ...],
    n_questions: int,
    rng: random.Random,
) -> list[Question]:
    """Load and sample clean questions from MMLU-Redux 2.0 for one category.

    Only keeps items annotated with ``error_type == "ok"`` so known label bugs
    from original MMLU do not masquerade as model mistakes.

    Args:
        category: Target category label (``"logic_and_math"`` or ``"science_and_cs"``).
        subjects: Tuple of MMLU-Redux subject config names to pool together.
        n_questions: Number of questions to sample for this category.
        rng: Seeded random generator.

    Returns:
        List of ``n_questions`` validated ``Question`` objects.
    """
    pool: list[dict[str, object]] = []
    for subject in subjects:
        ds = load_dataset(MMLU_REDUX_DATASET_ID, subject, split="test")
        for row in ds:
            # MMLU-Redux flags label errors such as `wrong_groundtruth`,
            # `no_correct_answer`, and `multiple_correct_answers`. Keeping only
            # `ok` items gives us a cleaner answer key.
            if row.get("error_type") != "ok":
                continue
            raw_choices = row.get("choices")
            raw_answer = row.get("answer")
            question_text = str(row.get("question", "")).strip()
            if (
                not question_text
                or not isinstance(raw_choices, list)
                or len(raw_choices) != 4
                or not isinstance(raw_answer, int)
                or not (0 <= raw_answer < 4)
            ):
                continue

            pool.append(
                {
                    "source": f"mmlu-redux/{subject}",
                    "question": question_text,
                    "choices": {
                        LETTERS[i]: str(raw_choices[i]).strip() for i in range(4)
                    },
                    "correct_answer": LETTERS[raw_answer],
                }
            )

    if len(pool) < n_questions:
        raise RuntimeError(
            f"Requested {n_questions} questions for {category}, "
            f"but only {len(pool)} clean items were found."
        )

    sampled = rng.sample(pool, k=n_questions)
    prefix = CATEGORY_ID_PREFIX[category]
    questions: list[Question] = []
    for idx, item in enumerate(sampled, start=1):
        correct: AnswerLetter = item["correct_answer"]  # type: ignore[assignment]
        target_wrong = _pick_target_wrong_answer(correct, rng)
        questions.append(
            Question(
                id=f"{prefix}_{idx:03d}",
                category=category,
                source=str(item["source"]),
                question=str(item["question"]),
                choices=item["choices"],  # type: ignore[arg-type]
                correct_answer=correct,
                target_wrong_answer=target_wrong,
            )
        )
    return questions


def _load_truthfulqa_category(
    n_questions: int,
    rng: random.Random,
) -> list[Question]:
    """Load and sample 4-choice questions from TruthfulQA ``mc1_targets``.

    TruthfulQA's ``mc1_targets`` has one correct answer (label 1) and a
    variable number of distractors (label 0). For each eligible item we take
    the single correct answer plus 3 seeded-random distractors, shuffle them
    with ``rng``, and assign letters A-D.

    Args:
        n_questions: Number of questions to sample.
        rng: Seeded random generator.

    Returns:
        List of ``n_questions`` validated ``Question`` objects.
    """
    ds = load_dataset(TRUTHFULQA_DATASET_ID, TRUTHFULQA_CONFIG, split="validation")
    eligible: list[tuple[str, str, list[str]]] = []
    for row in ds:
        question_text = str(row.get("question", "")).strip()
        mc1 = row.get("mc1_targets") or {}
        choices_list: list[str] = mc1.get("choices", [])
        labels_list: list[int] = mc1.get("labels", [])

        correct_texts = [
            str(c).strip()
            for c, lbl in zip(choices_list, labels_list)
            if lbl == 1 and str(c).strip()
        ]
        incorrect_texts = [
            str(c).strip()
            for c, lbl in zip(choices_list, labels_list)
            if lbl == 0 and str(c).strip()
        ]

        # We need exactly 1 correct answer and at least 3 distinct
        # distractors to build a standard 4-choice A-D question.
        if not question_text or len(correct_texts) != 1 or len(incorrect_texts) < 3:
            continue
        eligible.append((question_text, correct_texts[0], incorrect_texts))

    if len(eligible) < n_questions:
        raise RuntimeError(
            f"Requested {n_questions} TruthfulQA questions, "
            f"but only {len(eligible)} eligible items were found."
        )

    sampled = rng.sample(eligible, k=n_questions)
    prefix = CATEGORY_ID_PREFIX["common_misconceptions"]
    questions: list[Question] = []
    for idx, (question_text, correct_text, incorrect_texts) in enumerate(
        sampled, start=1
    ):
        distractors = rng.sample(incorrect_texts, k=3)
        options: list[tuple[str, bool]] = [(correct_text, True)] + [
            (d, False) for d in distractors
        ]
        rng.shuffle(options)

        choices_map: dict[AnswerLetter, str] = {}
        correct_letter: AnswerLetter = "A"
        for letter, (opt_text, is_correct) in zip(LETTERS, options):
            choices_map[letter] = opt_text
            if is_correct:
                correct_letter = letter

        target_wrong = _pick_target_wrong_answer(correct_letter, rng)
        questions.append(
            Question(
                id=f"{prefix}_{idx:03d}",
                category="common_misconceptions",
                source="truthfulqa/multiple_choice",
                question=question_text,
                choices=choices_map,
                correct_answer=correct_letter,
                target_wrong_answer=target_wrong,
            )
        )
    return questions


def write_questions_jsonl(questions: list[Question], out_path: Path) -> None:
    """Write ``Question`` records to a deterministic JSONL file.

    Args:
        questions: Ordered list of questions to serialize.
        out_path: Destination file path (parent directories are created).
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="\n") as fh:
        for q in questions:
            fh.write(q.model_dump_json() + "\n")


def write_spot_check_markdown(
    questions: list[Question],
    out_path: Path,
    seed: int,
    per_category: int = 5,
) -> None:
    """Write a human-readable Markdown spot-check sheet (15 random items).

    Even with MMLU-Redux filtering, eyeballing a random sample of questions
    and answer keys catches formatting glitches or remaining label noise
    before spending money on API calls.

    Args:
        questions: Full list of built questions.
        out_path: Destination Markdown file path.
        seed: Random seed used to select the spot-check subset.
        per_category: Number of items to sample from each category (default 5).
    """
    rng = random.Random(seed + 1000)
    by_cat: dict[str, list[Question]] = {}
    for q in questions:
        by_cat.setdefault(q.category, []).append(q)

    lines: list[str] = [
        "# Dataset Spot Check",
        "",
        "A seeded random sample of questions from `data/questions.jsonl` for manual",
        "answer-key verification before running paid evaluations.",
        "",
    ]

    for category in ("logic_and_math", "science_and_cs", "common_misconceptions"):
        cat_items = by_cat.get(category, [])
        k = min(per_category, len(cat_items))
        sample = rng.sample(cat_items, k=k)
        lines.append(f"## Category: `{category}` ({k} sampled)")
        lines.append("")
        for item in sample:
            lines.append(f"### `{item.id}` — `{item.source}`")
            lines.append("")
            lines.append(item.question)
            lines.append("")
            for letter in LETTERS:
                marker = " **(correct)**" if letter == item.correct_answer else ""
                lines.append(f"- **{letter}.** {item.choices[letter]}{marker}")
            lines.append("")
            lines.append(
                f"- Answer key: **{item.correct_answer}** | "
                f"Pushback wrong target: **{item.target_wrong_answer}**"
            )
            lines.append("")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines), encoding="utf-8")


def build_dataset(
    seed: int,
    questions_per_category: int,
    out_dir: Path,
) -> list[Question]:
    """Build the full evaluation dataset and write JSONL + spot-check Markdown.

    Args:
        seed: Master random seed for reproducible sampling and shuffling.
        questions_per_category: Number of items per category.
        out_dir: Directory where ``questions.jsonl`` and ``SPOT_CHECK.md`` are written.

    Returns:
        The full list of generated ``Question`` objects.
    """
    rng = random.Random(seed)
    logic_math = _load_mmlu_redux_category(
        category="logic_and_math",
        subjects=LOGIC_AND_MATH_SUBJECTS,
        n_questions=questions_per_category,
        rng=rng,
    )
    science_cs = _load_mmlu_redux_category(
        category="science_and_cs",
        subjects=SCIENCE_AND_CS_SUBJECTS,
        n_questions=questions_per_category,
        rng=rng,
    )
    misconceptions = _load_truthfulqa_category(
        n_questions=questions_per_category,
        rng=rng,
    )

    all_questions = logic_math + science_cs + misconceptions
    write_questions_jsonl(all_questions, out_dir / "questions.jsonl")
    write_spot_check_markdown(all_questions, out_dir / "SPOT_CHECK.md", seed=seed)
    return all_questions


def main() -> None:
    """CLI entry point for building ``data/questions.jsonl`` and ``data/SPOT_CHECK.md``."""
    parser = argparse.ArgumentParser(
        description="Build the Sycophancy Under Pressure evaluation dataset."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config.yaml"),
        help="Path to config.yaml (default: config.yaml).",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("data"),
        help="Output directory for questions.jsonl and SPOT_CHECK.md (default: data).",
    )
    args = parser.parse_args()

    cfg = yaml.safe_load(args.config.read_text(encoding="utf-8"))
    seed = int(cfg["seed"])
    questions_per_category = int(cfg["questions_per_category"])

    questions = build_dataset(
        seed=seed,
        questions_per_category=questions_per_category,
        out_dir=args.out_dir,
    )

    cat_counts = Counter(q.category for q in questions)
    source_counts = Counter(q.source for q in questions)

    print(f"Wrote {len(questions)} questions to {args.out_dir / 'questions.jsonl'}")
    print(f"Wrote spot-check sheet to {args.out_dir / 'SPOT_CHECK.md'}")
    print("\nCounts per category:")
    for cat, count in sorted(cat_counts.items()):
        print(f"  {cat}: {count}")
    print("\nCounts per source:")
    for src, count in sorted(source_counts.items()):
        print(f"  {src}: {count}")


if __name__ == "__main__":
    main()
