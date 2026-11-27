"""Run the two-turn sycophancy eval.

For each question: ask Turn 1, parse the answer, then branch into every Turn 2
pushback that applies, all from the same Turn 1 transcript. Each result is
appended to the JSONL right away, so an interrupted run can pick up where it
left off with ``--resume``.
"""

from __future__ import annotations

import argparse
import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import yaml
from tqdm import tqdm

from src.build_dataset import Question
from src.parsing import parse_answer
from src.prompts import (
    SYSTEM_PROMPTS,
    format_turn1_message,
    format_turn2_message,
    get_applicable_conditions,
)
from src.providers import chat

ResumeKey = tuple[str, str, str, int, str]
Turn1Key = tuple[str, str, str, int]


def load_questions(dataset_path: Path, limit: int | None = None) -> list[Question]:
    """Load validated ``Question`` records from JSONL, optionally limited to ``N``.

    When ``limit`` is smaller than the full dataset, questions are selected in
    round-robin order across categories so small smoke tests still cover all
    three categories.

    Args:
        dataset_path: Path to ``data/questions.jsonl``.
        limit: Optional maximum number of questions to return.

    Returns:
        List of ``Question`` instances.
    """
    questions = [
        Question.model_validate(json.loads(line))
        for line in dataset_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if limit is None or limit >= len(questions):
        return questions

    # `questions.jsonl` is grouped by category (100 logic_and_math,
    # 100 science_and_cs, 100 common_misconceptions). Round-robin interleaving
    # means `--limit 10` gets all three categories instead of only one.
    buckets: dict[str, list[Question]] = {}
    for q in questions:
        buckets.setdefault(q.category, []).append(q)

    selected: list[Question] = []
    cat_order = list(buckets.keys())
    idx = 0
    while len(selected) < limit:
        added_any = False
        for cat in cat_order:
            if idx < len(buckets[cat]) and len(selected) < limit:
                selected.append(buckets[cat][idx])
                added_any = True
        if not added_any:
            break
        idx += 1
    return selected


def load_resume_state(
    out_path: Path,
) -> tuple[set[ResumeKey], dict[Turn1Key, dict[str, Any]]]:
    """Read an existing results JSONL to support ``--resume``.

    Args:
        out_path: Path to an existing ``raw_results.jsonl`` file.

    Returns:
        A tuple ``(completed_keys, turn1_cache)`` where:
          - ``completed_keys`` is the set of ``(model_name, system_prompt_name,
            question_id, sample_idx, condition)`` tuples already recorded.
          - ``turn1_cache`` maps ``(model_name, system_prompt_name, question_id,
            sample_idx)`` to the saved Turn 1 response and parse fields so
            Turn 1 does not need to be re-queried.
    """
    completed: set[ResumeKey] = set()
    turn1_cache: dict[Turn1Key, dict[str, Any]] = {}

    if not out_path.exists():
        return completed, turn1_cache

    for line in out_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        # Skip records that failed with an API error so `--resume` retries
        # them instead of treating failed calls as finished. Skipping here also
        # keeps an errored Turn 1 out of `turn1_cache`.
        if rec.get("error"):
            continue
        r_key: ResumeKey = (
            str(rec["model_name"]),
            str(rec["system_prompt_name"]),
            str(rec["question_id"]),
            int(rec["sample_idx"]),
            str(rec["condition"]),
        )
        completed.add(r_key)

        t1_key: Turn1Key = (
            str(rec["model_name"]),
            str(rec["system_prompt_name"]),
            str(rec["question_id"]),
            int(rec["sample_idx"]),
        )
        if t1_key not in turn1_cache and rec.get("t1_response") is not None:
            turn1_cache[t1_key] = {
                "t1_response": rec["t1_response"],
                "t1_answer": rec["t1_answer"],
                "t1_parse_method": rec["t1_parse_method"],
                "t1_correct": rec["t1_correct"],
            }

    return completed, turn1_cache


def _append_record(
    out_path: Path,
    record: Mapping[str, Any],
    write_lock: threading.Lock | None = None,
) -> None:
    """Append one JSONL record to ``out_path`` and flush immediately.

    Flushing after every API call means almost nothing is lost if a
    long run is interrupted by a network outage, rate limit, or Ctrl-C.
    Guarding the write with ``write_lock`` prevents concurrent worker threads
    from interleaving bytes on the same line.
    """
    line = json.dumps(record, ensure_ascii=False) + "\n"
    if write_lock is not None:
        with write_lock:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with out_path.open("a", encoding="utf-8", newline="\n") as fh:
                fh.write(line)
                fh.flush()
    else:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(line)
            fh.flush()


def _resolve_target_answer(question: Question, condition: str) -> str | None:
    """Return the answer letter the pushback steers toward, or ``None``."""
    if condition == "suggestion" or condition.startswith("authority"):
        return question.target_wrong_answer
    if condition == "corrective":
        return question.correct_answer
    return None


def estimate_api_calls(
    models: list[dict[str, Any]],
    questions: list[Question],
    samples_per_question: int,
    completed: set[ResumeKey],
    turn1_cache: dict[Turn1Key, dict[str, Any]],
    robust_ids: set[str] | None = None,
) -> dict[str, int]:
    """Compute exact Turn 1 and min/max Turn 2 API call counts for ``--dry-run``.

    Args:
        models: Selected model configs from ``config.yaml``.
        questions: Loaded questions (after ``--limit``).
        samples_per_question: Number of samples per question.
        completed: Already-completed resume keys (empty unless ``--resume``).
        turn1_cache: Already-cached Turn 1 states (empty unless ``--resume``).

    Returns:
        Dictionary with ``t1_calls``, ``t2_min_calls``, ``t2_max_calls``,
        ``total_min_calls``, and ``total_max_calls``.
    """
    t1_calls = 0
    t2_min = 0
    t2_max = 0
    robust_ids = robust_ids or set()

    for model_cfg in models:
        m_name = str(model_cfg["name"])
        for sp_name in SYSTEM_PROMPTS:
            for q in questions:
                for sample_idx in range(samples_per_question):
                    t1_key: Turn1Key = (m_name, sp_name, q.id, sample_idx)
                    if t1_key in turn1_cache:
                        t1_correct = turn1_cache[t1_key]["t1_correct"]
                        if t1_correct is None:
                            continue
                        conds = get_applicable_conditions(
                            bool(t1_correct), robustness=q.id in robust_ids
                        )
                        remaining = sum(
                            1
                            for c in conds
                            if (m_name, sp_name, q.id, sample_idx, c) not in completed
                        )
                        t2_min += remaining
                        t2_max += remaining
                    else:
                        t1_calls += 1
                        # 2 Turn 2 branches if Turn 1 is wrong, 3 if Turn 1 is right.
                        # Robustness adds 3 authority wordings only when Turn 1 is right.
                        t2_min += 2
                        t2_max += 3 + (3 if q.id in robust_ids else 0)

    return {
        "t1_calls": t1_calls,
        "t2_min_calls": t2_min,
        "t2_max_calls": t2_max,
        "total_min_calls": t1_calls + t2_min,
        "total_max_calls": t1_calls + t2_max,
    }


def run_evaluation(
    config: Mapping[str, Any],
    dataset_path: Path,
    out_path: Path,
    *,
    model_names: list[str] | None = None,
    limit: int | None = None,
    resume: bool = False,
    dry_run: bool = False,
    show_progress: bool = True,
    robustness: bool = False,
    robustness_questions: int = 100,
    workers: int = 1,
) -> dict[str, int]:
    """Run the two-turn sycophancy evaluation and write records to JSONL.

    Args:
        config: Parsed ``config.yaml`` dictionary.
        dataset_path: Path to ``data/questions.jsonl``.
        out_path: Destination ``raw_results.jsonl`` path.
        model_names: Optional subset of model ``name`` values to run.
        limit: Optional maximum number of questions to evaluate.
        resume: If True, skip already-logged keys in ``out_path`` and reuse
            cached Turn 1 replies. If False, overwrite ``out_path``.
        dry_run: If True, print estimated API call counts and return without
            calling any provider.
        show_progress: Whether to display a ``tqdm`` progress bar.

    Returns:
        Summary dictionary of call counts (for ``dry_run``) or written record
        count (for actual runs).
    """
    all_models: list[dict[str, Any]] = list(config["models"])
    if model_names:
        requested = set(model_names)
        models = [m for m in all_models if m["name"] in requested]
        missing = requested - {m["name"] for m in models}
        if missing:
            raise ValueError(
                f"Unknown model name(s): {sorted(missing)}. "
                f"Available in config: {[m['name'] for m in all_models]}."
            )
    else:
        models = all_models

    questions = load_questions(dataset_path, limit=limit)
    # questions.jsonl is grouped by category, so the literal first 100
    # rows are all logic_and_math. Round-robin selection keeps the robustness
    # slice spread across the three categories.
    robust_ids = (
        {q.id for q in load_questions(dataset_path, limit=robustness_questions)}
        if robustness
        else set()
    )
    samples_per_question = int(config.get("samples_per_question", 1))
    temperature = float(config.get("temperature", 0.0))
    max_tokens = int(config.get("max_tokens", 1024))

    if resume:
        completed, turn1_cache = load_resume_state(out_path)
    else:
        completed, turn1_cache = set(), {}

    if dry_run:
        est = estimate_api_calls(
            models,
            questions,
            samples_per_question,
            completed,
            turn1_cache,
            robust_ids,
        )
        print("Dry-run API call estimate:")
        print(f"  Models:               {[m['name'] for m in models]}")
        print(f"  System prompts:       {list(SYSTEM_PROMPTS.keys())}")
        print(f"  Questions:            {len(questions)}")
        if robustness:
            in_run = sum(1 for q in questions if q.id in robust_ids)
            print(f"  Robustness questions: {in_run} (3 extra authority wordings)")
        print(f"  Samples per question: {samples_per_question}")
        print(f"  Turn 1 calls:         {est['t1_calls']}")
        print(
            f"  Turn 2 calls:         {est['t2_min_calls']}–{est['t2_max_calls']} "
            "(depends on Turn 1 accuracy)"
        )
        print(
            f"  Total API calls:      {est['total_min_calls']}–{est['total_max_calls']}"
        )
        return est

    if workers < 1:
        raise ValueError(f"workers must be >= 1, got {workers}")

    if not resume and out_path.exists():
        out_path.unlink()

    run_id = uuid.uuid4().hex[:12]
    work_items: list[tuple[dict[str, Any], str, str, Question, int]] = [
        (model_cfg, sp_name, sp_text, question, sample_idx)
        for model_cfg in models
        for sp_name, sp_text in SYSTEM_PROMPTS.items()
        for question in questions
        for sample_idx in range(samples_per_question)
    ]

    records_written = 0
    # One lock protects both JSONL appends (so two threads never write
    # interleaved bytes onto the same line) and in-memory shared state
    # (`completed`, `turn1_cache`, `records_written`).
    write_lock = threading.Lock()
    # Caps total simultaneous API requests at `workers`, even when a trial
    # runs its Turn 2 branches concurrently. Threads release the semaphore
    # before waiting on Turn 2 futures, so nested submission cannot deadlock.
    api_semaphore = threading.Semaphore(workers)

    progress = tqdm(
        total=len(work_items),
        desc="Evaluating",
        disable=not show_progress,
    )

    def _call_chat(
        model_cfg: Mapping[str, Any],
        sp_text: str,
        messages: list[dict[str, str]],
    ) -> str:
        with api_semaphore:
            return chat(
                model_cfg,
                sp_text,
                messages,
                temperature,
                max_tokens=max_tokens,
            )

    def _run_one_unit(
        item: tuple[dict[str, Any], str, str, Question, int],
    ) -> int:
        model_cfg, sp_name, sp_text, question, sample_idx = item
        model_name = str(model_cfg["name"])
        model_id = str(model_cfg["model_id"])
        provider = str(model_cfg["provider"])
        t1_user_msg = format_turn1_message(question.question, question.choices)
        t1_key: Turn1Key = (model_name, sp_name, question.id, sample_idx)
        written_here = 0

        # Step 1: Obtain Turn 1 response (from cache on resume, or API)
        t1_error: str | None = None
        with write_lock:
            cached = turn1_cache.get(t1_key)

        if cached is not None:
            t1_response: str | None = cached["t1_response"]
            t1_answer: str | None = cached["t1_answer"]
            t1_parse_method: str = cached["t1_parse_method"]
            t1_correct: bool | None = cached["t1_correct"]
        else:
            try:
                t1_response = _call_chat(
                    model_cfg,
                    sp_text,
                    [{"role": "user", "content": t1_user_msg}],
                )
                t1_parsed = parse_answer(t1_response)
                t1_answer = t1_parsed.answer
                t1_parse_method = t1_parsed.method
                t1_correct = (
                    (t1_answer == question.correct_answer)
                    if t1_answer is not None
                    else None
                )
            except Exception as exc:  # noqa: BLE001
                t1_response = None
                t1_answer = None
                t1_parse_method = "failed"
                t1_correct = None
                t1_error = f"{type(exc).__name__}: {exc}"

            # Only cache Turn 1 when the API call succeeded (no error).
            if t1_error is None:
                with write_lock:
                    turn1_cache[t1_key] = {
                        "t1_response": t1_response,
                        "t1_answer": t1_answer,
                        "t1_parse_method": t1_parse_method,
                        "t1_correct": t1_correct,
                    }

        # If Turn 1 failed to parse or errored, log once with condition="none"
        if t1_answer is None or t1_error is not None:
            none_key: ResumeKey = (
                model_name,
                sp_name,
                question.id,
                sample_idx,
                "none",
            )
            with write_lock:
                already_done = none_key in completed
            if not already_done:
                record = {
                    "run_id": run_id,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "model_name": model_name,
                    "model_id": model_id,
                    "provider": provider,
                    "system_prompt_name": sp_name,
                    "question_id": question.id,
                    "category": question.category,
                    "sample_idx": sample_idx,
                    "temperature": temperature,
                    "condition": "none",
                    "target_answer": None,
                    "correct_answer": question.correct_answer,
                    "t1_response": t1_response,
                    "t1_answer": t1_answer,
                    "t1_parse_method": t1_parse_method,
                    "t1_correct": t1_correct,
                    "t2_user_message": None,
                    "t2_response": None,
                    "t2_answer": None,
                    "t2_parse_method": None,
                    "t2_correct": None,
                    "changed_answer": None,
                    "adopted_target": None,
                    "flipped_to_wrong": None,
                    "flipped_to_right": None,
                    "error": t1_error,
                }
                _append_record(out_path, record, write_lock=write_lock)
                if t1_error is None:
                    with write_lock:
                        completed.add(none_key)
                written_here += 1
            return written_here

        # Step 2: Branch into each applicable Turn 2 condition from the same Turn 1 state
        assert t1_correct is not None and t1_response is not None
        conditions = get_applicable_conditions(
            t1_correct, robustness=question.id in robust_ids
        )
        with write_lock:
            pending_conditions = [
                cond
                for cond in conditions
                if (model_name, sp_name, question.id, sample_idx, cond)
                not in completed
            ]

        def _run_t2_branch(condition: str) -> int:
            r_key: ResumeKey = (
                model_name,
                sp_name,
                question.id,
                sample_idx,
                condition,
            )
            target_answer = _resolve_target_answer(question, condition)
            t2_user_msg = format_turn2_message(condition, target_answer)
            branch_messages = [
                {"role": "user", "content": t1_user_msg},
                {"role": "assistant", "content": t1_response},
                {"role": "user", "content": t2_user_msg},
            ]

            t2_error: str | None = None
            try:
                t2_response: str | None = _call_chat(
                    model_cfg,
                    sp_text,
                    branch_messages,
                )
                t2_parsed = parse_answer(t2_response)
                t2_answer: str | None = t2_parsed.answer
                t2_parse_method: str | None = t2_parsed.method
            except Exception as exc:  # noqa: BLE001
                t2_response = None
                t2_answer = None
                t2_parse_method = "failed"
                t2_error = f"{type(exc).__name__}: {exc}"

            t2_correct: bool | None = (
                (t2_answer == question.correct_answer)
                if t2_answer is not None
                else None
            )
            changed_answer: bool | None = (
                (t2_answer != t1_answer)
                if t2_answer is not None
                else None
            )
            adopted_target: bool | None = (
                (t2_answer == target_answer)
                if (t2_answer is not None and target_answer is not None)
                else None
            )
            flipped_to_wrong: bool | None = (
                (t1_correct and (t2_correct is False))
                if t2_correct is not None
                else None
            )
            flipped_to_right: bool | None = (
                ((not t1_correct) and bool(t2_correct))
                if t2_correct is not None
                else None
            )

            record = {
                "run_id": run_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "model_name": model_name,
                "model_id": model_id,
                "provider": provider,
                "system_prompt_name": sp_name,
                "question_id": question.id,
                "category": question.category,
                "sample_idx": sample_idx,
                "temperature": temperature,
                "condition": condition,
                "target_answer": target_answer,
                "correct_answer": question.correct_answer,
                "t1_response": t1_response,
                "t1_answer": t1_answer,
                "t1_parse_method": t1_parse_method,
                "t1_correct": t1_correct,
                "t2_user_message": t2_user_msg,
                "t2_response": t2_response,
                "t2_answer": t2_answer,
                "t2_parse_method": t2_parse_method,
                "t2_correct": t2_correct,
                "changed_answer": changed_answer,
                "adopted_target": adopted_target,
                "flipped_to_wrong": flipped_to_wrong,
                "flipped_to_right": flipped_to_right,
                "error": t2_error,
            }
            _append_record(out_path, record, write_lock=write_lock)
            if t2_error is None:
                with write_lock:
                    completed.add(r_key)
            return 1

        if workers > 1 and len(pending_conditions) > 1:
            with ThreadPoolExecutor(max_workers=len(pending_conditions)) as t2_pool:
                t2_futures = [
                    t2_pool.submit(_run_t2_branch, cond)
                    for cond in pending_conditions
                ]
                for fut in as_completed(t2_futures):
                    written_here += fut.result()
        else:
            for cond in pending_conditions:
                written_here += _run_t2_branch(cond)

        return written_here

    try:
        if workers == 1:
            for item in work_items:
                records_written += _run_one_unit(item)
                progress.update(1)
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [pool.submit(_run_one_unit, item) for item in work_items]
                for fut in as_completed(futures):
                    records_written += fut.result()
                    progress.update(1)
    finally:
        progress.close()

    return {"records_written": records_written}


def _parse_model_list(raw: list[str] | None) -> list[str] | None:
    """Accept either ``--models a b`` or ``--models a,b``."""
    if not raw:
        return None
    names: list[str] = []
    for item in raw:
        for part in item.split(","):
            cleaned = part.strip()
            if cleaned:
                names.append(cleaned)
    return names or None


def main() -> None:
    """CLI entry point for running the sycophancy evaluation harness."""
    parser = argparse.ArgumentParser(
        description="Run the Sycophancy Under Pressure evaluation harness."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config.yaml"),
        help="Path to config.yaml (default: config.yaml).",
    )
    parser.add_argument(
        "--dataset",
        type=Path,
        default=Path("data/questions.jsonl"),
        help="Path to questions JSONL (default: data/questions.jsonl).",
    )
    parser.add_argument(
        "--models",
        nargs="+",
        default=None,
        help="Optional subset of model names from config.yaml.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional limit on the number of questions to evaluate.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume from an existing --out file without re-running completed calls.",
    )
    parser.add_argument(
        "--robustness",
        action="store_true",
        help=(
            "Also run authority_v2, authority_v3, and authority_v4 on a "
            "100-question spread when Turn 1 was correct."
        ),
    )
    parser.add_argument(
        "--robustness-questions",
        type=int,
        default=100,
        help="How many questions receive the alternate authority wordings (default: 100).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print estimated API call counts and exit without making any calls.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Number of parallel worker threads (default: 1, sequential).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("results/raw_results.jsonl"),
        help="Output JSONL path (default: results/raw_results.jsonl).",
    )
    args = parser.parse_args()

    cfg = yaml.safe_load(args.config.read_text(encoding="utf-8"))
    run_evaluation(
        config=cfg,
        dataset_path=args.dataset,
        out_path=args.out,
        model_names=_parse_model_list(args.models),
        limit=args.limit,
        resume=args.resume,
        dry_run=args.dry_run,
        robustness=args.robustness,
        robustness_questions=args.robustness_questions,
        workers=args.workers,
    )


if __name__ == "__main__":
    main()
