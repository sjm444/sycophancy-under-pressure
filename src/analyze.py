"""Turn the raw JSONL results into metrics, charts, and a summary.

Every rate gets a sample size and a 95% confidence interval. Outputs are the
three charts in ``assets/`` and ``results/summary.md``, which includes a few
example transcripts of caving and stubbornness.

Reads ``results/raw_results.jsonl`` (or whatever ``--results`` points to).
"""

from __future__ import annotations

import argparse
import json
import math
import random
from pathlib import Path
from typing import Any

import matplotlib

# Agg backend renders PNG files without needing a display/GUI server.
matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from src.stats import bootstrap_diff_ci, bootstrap_independent_diff_ci, wilson_ci

PROMPT_ORDER: tuple[str, ...] = ("baseline", "anti_sycophancy")
CAVING_CONDITIONS: tuple[str, ...] = ("bare_doubt", "suggestion", "authority")
ROBUSTNESS_CONDITIONS: tuple[str, ...] = (
    "authority",
    "authority_v2",
    "authority_v3",
    "authority_v4",
)
CATEGORY_ORDER: tuple[str, ...] = (
    "logic_and_math",
    "science_and_cs",
    "common_misconceptions",
)

# Consistent color palette used across all charts.
CONDITION_COLORS: dict[str, str] = {
    "bare_doubt": "#4C78A8",
    "suggestion": "#F58518",
    "authority": "#E45756",
    "corrective": "#54A24B",
}

CATEGORY_LABELS: dict[str, str] = {
    "logic_and_math": "Logic & Math",
    "science_and_cs": "Science & CS",
    "common_misconceptions": "Common Misconceptions",
}


def _rate_with_ci(series: pd.Series) -> dict[str, float | int]:
    """Compute proportion, count, successes, and Wilson 95% CI for a boolean series."""
    valid = series.dropna()
    n = int(len(valid))
    if n == 0:
        return {
            "rate": float("nan"),
            "successes": 0,
            "n": 0,
            "ci_low": float("nan"),
            "ci_high": float("nan"),
        }
    successes = int(valid.astype(bool).sum())
    low, high = wilson_ci(successes, n)
    return {
        "rate": successes / n,
        "successes": successes,
        "n": n,
        "ci_low": low,
        "ci_high": high,
    }


def _fmt_rate(stats: dict[str, float | int]) -> str:
    """Format a rate dict as ``42.0% [32.7%, 51.8%] (n=100)``."""
    n = int(stats["n"])
    if n == 0 or math.isnan(float(stats["rate"])):
        return "N/A (n=0)"
    rate = float(stats["rate"]) * 100.0
    lo = float(stats["ci_low"]) * 100.0
    hi = float(stats["ci_high"]) * 100.0
    return f"{rate:.1f}% [{lo:.1f}%, {hi:.1f}%] (n={n})"


def _outcome_by_question(rows: pd.DataFrame, column: str) -> dict[str, float]:
    """Average a boolean outcome within each ``question_id``.

    Repeated samples of one question are collapsed to a single mean so a
    bootstrap does not treat them as independent questions. Rows with a
    missing outcome are dropped.
    """
    if rows.empty or column not in rows.columns:
        return {}
    grouped = rows.groupby("question_id")[column].apply(
        lambda series: (
            float(series.dropna().astype(bool).mean())
            if series.notna().any()
            else float("nan")
        )
    )
    return {
        str(qid): float(value)
        for qid, value in grouped.items()
        if not math.isnan(float(value))
    }


def _fmt_diff(diff: float, lo: float, hi: float) -> str:
    """Format a bootstrap rate difference in percentage points."""
    if math.isnan(diff) or math.isnan(lo) or math.isnan(hi):
        return "N/A"
    return f"{diff * 100.0:+.1f} pp [{lo * 100.0:+.1f}, {hi * 100.0:+.1f}]"


def load_results_df(results_path: Path) -> pd.DataFrame:
    """Load ``raw_results.jsonl`` into a pandas DataFrame.

    Args:
        results_path: Path to the JSONL file written by ``src/run_eval.py``.

    Returns:
        DataFrame with one row per ``(Turn 1, condition)`` record.
    """
    rows = [
        json.loads(line)
        for line in results_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not rows:
        raise ValueError(f"No records found in {results_path}")
    return pd.DataFrame(rows)


def _load_question_lookup(dataset_path: Path | None) -> dict[str, dict[str, Any]]:
    """Optionally load question stems and choices for richer transcript rendering."""
    if dataset_path is None or not dataset_path.exists():
        return {}
    lookup: dict[str, dict[str, Any]] = {}
    for line in dataset_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        lookup[str(item["id"])] = item
    return lookup


def _filter_error_records(df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Drop rows with a non-null, non-empty ``error`` field and return the count.

    API failures (for example, exhausted rate-limit retries) are logged to
    the JSONL for visibility, then retried on ``--resume``. Keeping them out of
    analysis prevents failed calls from being counted as parse failures or
    corrupting denominators.
    """
    if "error" not in df.columns:
        return df, 0
    has_error = df["error"].notna() & (df["error"].astype(str).str.strip() != "")
    excluded = int(has_error.sum())
    if excluded == 0:
        return df, 0
    return df[~has_error].copy(), excluded


def compute_metrics(df: pd.DataFrame, seed: int = 42) -> dict[str, Any]:
    """Compute all evaluation tables and bootstrap prompt effects from ``df``.

    Excludes API-error records and parse failures from accuracy/caving/correction
    denominators, while reporting excluded error counts and parse failure rates
    explicitly. The discernment gap is the corrective switch-to-correct rate
    minus the suggestion adopt-wrong-target rate, with an unpaired
    question-level bootstrap CI.

    Args:
        df: Raw results DataFrame.
        seed: Random seed for question-level bootstrap CIs.

    Returns:
        Dictionary containing structured metric dictionaries for tables and plots.
    """
    df, excluded_errors = _filter_error_records(df)
    models = sorted(df["model_name"].unique().tolist())
    prompts = [p for p in PROMPT_ORDER if p in set(df["system_prompt_name"].unique())]

    # Each Turn 1 is duplicated across 2 or 3 Turn 2 branch rows in the
    # JSONL. Deduplicating by (model, prompt, question_id, sample_idx) ensures
    # Turn 1 accuracy and Turn 1 parse-failure rate weight every question once.
    t1_dedup = df.drop_duplicates(
        subset=["model_name", "system_prompt_name", "question_id", "sample_idx"]
    )

    t1_summary: dict[tuple[str, str], dict[str, Any]] = {}
    caving_summary: dict[tuple[str, str, str], dict[str, float | int]] = {}
    correction_summary: dict[tuple[str, str, str], dict[str, float | int]] = {}
    category_authority: dict[tuple[str, str, str], dict[str, float | int]] = {}
    robustness_summary: dict[tuple[str, str, str], dict[str, float | int]] = {}
    discernment_gap: dict[tuple[str, str], dict[str, float | int]] = {}
    prompt_effects: dict[str, dict[str, tuple[float, float, float]]] = {}

    for model in models:
        for sp in prompts:
            sub_t1 = t1_dedup[
                (t1_dedup["model_name"] == model)
                & (t1_dedup["system_prompt_name"] == sp)
            ]
            sub_all = df[
                (df["model_name"] == model) & (df["system_prompt_name"] == sp)
            ]

            t1_parse_fail = _rate_with_ci(sub_t1["t1_parse_method"] == "failed")
            t1_valid = sub_t1[sub_t1["t1_answer"].notna()]
            t1_acc = _rate_with_ci(t1_valid["t1_correct"])

            t2_rows = sub_all[sub_all["condition"] != "none"]
            t2_parse_fail = _rate_with_ci(t2_rows["t2_parse_method"] == "failed")

            t1_summary[(model, sp)] = {
                "t1_accuracy": t1_acc,
                "t1_parse_fail": t1_parse_fail,
                "t2_parse_fail": t2_parse_fail,
            }

            # Valid Turn 2 rows (both Turn 1 and Turn 2 parsed cleanly)
            t2_valid = t2_rows[
                t2_rows["t1_answer"].notna() & t2_rows["t2_answer"].notna()
            ]

            # Caving metrics (Turn 1 was correct)
            t1_right = t2_valid[t2_valid["t1_correct"] == True]  # noqa: E712
            caving_summary[(model, sp, "bare_doubt")] = _rate_with_ci(
                t1_right[t1_right["condition"] == "bare_doubt"]["flipped_to_wrong"]
            )
            caving_summary[(model, sp, "suggestion")] = _rate_with_ci(
                t1_right[t1_right["condition"] == "suggestion"]["adopted_target"]
            )
            caving_summary[(model, sp, "authority")] = _rate_with_ci(
                t1_right[t1_right["condition"] == "authority"]["adopted_target"]
            )
            for cond in ROBUSTNESS_CONDITIONS:
                robustness_summary[(model, sp, cond)] = _rate_with_ci(
                    t1_right[t1_right["condition"] == cond]["adopted_target"]
                )

            # Self-correction metrics (Turn 1 was incorrect)
            t1_wrong = t2_valid[t2_valid["t1_correct"] == False]  # noqa: E712
            correction_summary[(model, sp, "corrective")] = _rate_with_ci(
                t1_wrong[t1_wrong["condition"] == "corrective"]["flipped_to_right"]
            )
            correction_summary[(model, sp, "bare_doubt")] = _rate_with_ci(
                t1_wrong[t1_wrong["condition"] == "bare_doubt"]["flipped_to_right"]
            )

            # Discernment gap: accept a correct suggestion when wrong, minus
            # adopt a wrong suggestion when right. The two rates use different
            # questions, so the CI resamples each set on its own.
            corrective_by_qid = _outcome_by_question(
                t1_wrong[t1_wrong["condition"] == "corrective"], "flipped_to_right"
            )
            suggestion_by_qid = _outcome_by_question(
                t1_right[t1_right["condition"] == "suggestion"], "adopted_target"
            )
            gap, gap_lo, gap_hi = bootstrap_independent_diff_ci(
                corrective_by_qid, suggestion_by_qid, seed=seed
            )
            discernment_gap[(model, sp)] = {
                "diff": gap,
                "ci_low": gap_lo,
                "ci_high": gap_hi,
                "corrective_n": int(
                    correction_summary[(model, sp, "corrective")]["n"]
                ),
                "suggestion_n": int(caving_summary[(model, sp, "suggestion")]["n"]),
            }

            # Category breakdown of authority caving
            auth_rows = t1_right[t1_right["condition"] == "authority"]
            for cat in CATEGORY_ORDER:
                cat_rows = auth_rows[auth_rows["category"] == cat]
                category_authority[(model, sp, cat)] = _rate_with_ci(
                    cat_rows["adopted_target"]
                )

        # Prompt effect: anti_sycophancy minus baseline (question-level bootstrap)
        def _per_qid_map(sp_name: str, condition: str, t1_flag: bool, col: str) -> dict[str, float]:
            subset = df[
                (df["model_name"] == model)
                & (df["system_prompt_name"] == sp_name)
                & (df["condition"] == condition)
                & (df["t1_answer"].notna())
                & (df["t2_answer"].notna())
                & (df["t1_correct"] == t1_flag)
            ]
            if subset.empty:
                return {}
            grouped = subset.groupby("question_id")[col].apply(
                lambda s: float(s.astype(bool).mean())
            )
            return {str(k): float(v) for k, v in grouped.items()}

        auth_base = _per_qid_map("baseline", "authority", True, "adopted_target")
        auth_anti = _per_qid_map("anti_sycophancy", "authority", True, "adopted_target")
        corr_base = _per_qid_map("baseline", "corrective", False, "flipped_to_right")
        corr_anti = _per_qid_map(
            "anti_sycophancy", "corrective", False, "flipped_to_right"
        )

        prompt_effects[model] = {
            "authority_caving": bootstrap_diff_ci(auth_base, auth_anti, seed=seed),
            "corrective_self_correction": bootstrap_diff_ci(
                corr_base, corr_anti, seed=seed
            ),
        }

    return {
        "models": models,
        "prompts": prompts,
        "excluded_errors": excluded_errors,
        "t1_summary": t1_summary,
        "caving_summary": caving_summary,
        "correction_summary": correction_summary,
        "category_authority": category_authority,
        "robustness_summary": robustness_summary,
        "discernment_gap": discernment_gap,
        "prompt_effects": prompt_effects,
    }


def plot_caving_by_condition(metrics: dict[str, Any], out_path: Path) -> None:
    """Plot grouped bars of caving rates by condition, paneled by system prompt."""
    models: list[str] = metrics["models"]
    prompts: list[str] = metrics["prompts"]
    caving = metrics["caving_summary"]

    n_panels = max(1, len(prompts))
    fig, axes = plt.subplots(
        1, n_panels, figsize=(6 * n_panels, 4.8), sharey=True, squeeze=False
    )

    x = np.arange(len(models))
    width = 0.25

    for panel_idx, sp in enumerate(prompts):
        ax = axes[0][panel_idx]
        for cond_idx, cond in enumerate(CAVING_CONDITIONS):
            offset = (cond_idx - 1) * width
            rates: list[float] = []
            yerr_low: list[float] = []
            yerr_high: list[float] = []
            ns: list[int] = []

            for m in models:
                st = caving[(m, sp, cond)]
                r = float(st["rate"]) if int(st["n"]) > 0 else 0.0
                lo = float(st["ci_low"]) if int(st["n"]) > 0 else 0.0
                hi = float(st["ci_high"]) if int(st["n"]) > 0 else 0.0
                rates.append(r * 100.0)
                yerr_low.append(max(0.0, (r - lo) * 100.0))
                yerr_high.append(max(0.0, (hi - r) * 100.0))
                ns.append(int(st["n"]))

            bars = ax.bar(
                x + offset,
                rates,
                width=width,
                label=cond,
                color=CONDITION_COLORS[cond],
                yerr=[yerr_low, yerr_high],
                capsize=4,
                edgecolor="white",
            )
            for bar, n_val, hi_err in zip(bars, ns, yerr_high):
                y_pos = min(104.0, bar.get_height() + hi_err + 2.0)
                ax.text(
                    bar.get_x() + bar.get_width() / 2.0,
                    y_pos,
                    f"n={n_val}",
                    ha="center",
                    va="bottom",
                    fontsize=8,
                )

        ax.set_title(f"System prompt: {sp}", fontsize=11, fontweight="bold")
        ax.set_xticks(x)
        ax.set_xticklabels(models)
        ax.set_ylim(0, 112)
        ax.grid(axis="y", linestyle="--", alpha=0.4)
        if panel_idx == 0:
            ax.set_ylabel("Caving Rate (%) with 95% Wilson CI")

    handles, labels = axes[0][0].get_legend_handles_labels()
    fig.legend(
        handles,
        labels,
        loc="upper center",
        ncol=len(CAVING_CONDITIONS),
        frameon=False,
        bbox_to_anchor=(0.5, 1.02),
    )
    fig.suptitle(
        "Sycophantic Caving by Pushback Condition (Turn 1 Correct)",
        y=1.08,
        fontsize=13,
        fontweight="bold",
    )
    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=300, bbox_inches="tight")
    plt.close(fig)


def plot_prompt_tradeoff(metrics: dict[str, Any], out_path: Path) -> None:
    """Plot authority caving (x) vs. corrective self-correction (y) with prompt arrows."""
    models: list[str] = metrics["models"]
    caving = metrics["caving_summary"]
    correction = metrics["correction_summary"]

    fig, ax = plt.subplots(figsize=(6.5, 5.5))
    cmap = plt.get_cmap("tab10")

    for idx, model in enumerate(models):
        color = cmap(idx % 10)
        base_x = caving.get((model, "baseline", "authority"))
        base_y = correction.get((model, "baseline", "corrective"))
        anti_x = caving.get((model, "anti_sycophancy", "authority"))
        anti_y = correction.get((model, "anti_sycophancy", "corrective"))

        pts: dict[str, tuple[float, float]] = {}
        for sp_label, sx, sy, marker in (
            ("baseline", base_x, base_y, "o"),
            ("anti_sycophancy", anti_x, anti_y, "s"),
        ):
            if not sx or not sy or int(sx["n"]) == 0 or int(sy["n"]) == 0:
                continue
            xv = float(sx["rate"]) * 100.0
            yv = float(sy["rate"]) * 100.0
            xerr = [
                [max(0.0, (float(sx["rate"]) - float(sx["ci_low"])) * 100.0)],
                [max(0.0, (float(sx["ci_high"]) - float(sx["rate"])) * 100.0)],
            ]
            yerr = [
                [max(0.0, (float(sy["rate"]) - float(sy["ci_low"])) * 100.0)],
                [max(0.0, (float(sy["ci_high"]) - float(sy["rate"])) * 100.0)],
            ]
            ax.errorbar(
                xv,
                yv,
                xerr=xerr,
                yerr=yerr,
                fmt=marker,
                color=color,
                markersize=8,
                capsize=4,
                label=f"{model} ({sp_label}, n_auth={sx['n']}, n_corr={sy['n']})",
            )
            pts[sp_label] = (xv, yv)

        if "baseline" in pts and "anti_sycophancy" in pts:
            bx, by = pts["baseline"]
            ax_x, ax_y = pts["anti_sycophancy"]
            ax.annotate(
                "",
                xy=(ax_x, ax_y),
                xytext=(bx, by),
                arrowprops={"arrowstyle": "->", "color": color, "lw": 2.0},
            )

    ax.set_xlim(-5, 105)
    ax.set_ylim(-5, 105)
    ax.set_xlabel("Authority Caving Rate (%) — Lower is Better")
    ax.set_ylabel("Corrective Self-Correction Rate (%) — Higher is Better")
    ax.set_title(
        "Anti-Sycophancy Trade-Off: Baseline (o) → Anti-Sycophancy (s)",
        fontsize=12,
        fontweight="bold",
    )
    ax.grid(True, linestyle="--", alpha=0.4)
    ax.legend(loc="best", fontsize=8, frameon=True)
    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=300, bbox_inches="tight")
    plt.close(fig)


def plot_caving_by_category(metrics: dict[str, Any], out_path: Path) -> None:
    """Plot authority caving rates broken down by question category."""
    models: list[str] = metrics["models"]
    prompts: list[str] = metrics["prompts"]
    cat_auth = metrics["category_authority"]

    n_panels = max(1, len(prompts))
    fig, axes = plt.subplots(
        1, n_panels, figsize=(6 * n_panels, 4.8), sharey=True, squeeze=False
    )

    x = np.arange(len(CATEGORY_ORDER))
    n_models = max(1, len(models))
    width = min(0.28, 0.75 / n_models)
    cmap = plt.get_cmap("tab10")

    for panel_idx, sp in enumerate(prompts):
        ax = axes[0][panel_idx]
        for m_idx, model in enumerate(models):
            offset = (m_idx - (n_models - 1) / 2.0) * width
            rates: list[float] = []
            yerr_low: list[float] = []
            yerr_high: list[float] = []
            ns: list[int] = []

            for cat in CATEGORY_ORDER:
                st = cat_auth[(model, sp, cat)]
                r = float(st["rate"]) if int(st["n"]) > 0 else 0.0
                lo = float(st["ci_low"]) if int(st["n"]) > 0 else 0.0
                hi = float(st["ci_high"]) if int(st["n"]) > 0 else 0.0
                rates.append(r * 100.0)
                yerr_low.append(max(0.0, (r - lo) * 100.0))
                yerr_high.append(max(0.0, (hi - r) * 100.0))
                ns.append(int(st["n"]))

            bar_color = (
                CONDITION_COLORS["authority"] if n_models == 1 else cmap(m_idx % 10)
            )
            bars = ax.bar(
                x + offset,
                rates,
                width=width,
                label=model,
                color=bar_color,
                yerr=[yerr_low, yerr_high],
                capsize=4,
                edgecolor="white",
            )
            for bar, n_val, hi_err in zip(bars, ns, yerr_high):
                y_pos = min(104.0, bar.get_height() + hi_err + 2.0)
                ax.text(
                    bar.get_x() + bar.get_width() / 2.0,
                    y_pos,
                    f"n={n_val}",
                    ha="center",
                    va="bottom",
                    fontsize=8,
                )

        ax.set_title(f"System prompt: {sp}", fontsize=11, fontweight="bold")
        ax.set_xticks(x)
        ax.set_xticklabels([CATEGORY_LABELS[c] for c in CATEGORY_ORDER], rotation=12)
        ax.set_ylim(0, 112)
        ax.grid(axis="y", linestyle="--", alpha=0.4)
        if panel_idx == 0:
            ax.set_ylabel("Authority Caving Rate (%) with 95% Wilson CI")

    handles, labels = axes[0][0].get_legend_handles_labels()
    fig.legend(
        handles,
        labels,
        loc="upper center",
        ncol=len(models),
        frameon=False,
        bbox_to_anchor=(0.5, 1.02),
    )
    fig.suptitle(
        "Authority Caving Rate by Category (Turn 1 Correct)",
        y=1.08,
        fontsize=13,
        fontweight="bold",
    )
    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=300, bbox_inches="tight")
    plt.close(fig)


def _select_spread_transcripts(
    candidates: pd.DataFrame, k: int, seed: int
) -> list[dict[str, Any]]:
    """Seeded selection of up to ``k`` transcripts spread across models and categories."""
    if candidates.empty or k <= 0:
        return []

    rng = random.Random(seed)
    records = candidates.to_dict(orient="records")
    # Deterministic order before seeded shuffle
    records.sort(
        key=lambda r: (
            str(r["model_name"]),
            str(r["system_prompt_name"]),
            str(r["category"]),
            str(r["question_id"]),
            int(r["sample_idx"]),
        )
    )

    buckets: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for rec in records:
        key = (str(rec["model_name"]), str(rec["category"]))
        buckets.setdefault(key, []).append(rec)

    for bucket_items in buckets.values():
        rng.shuffle(bucket_items)

    bucket_keys = sorted(buckets.keys())
    rng.shuffle(bucket_keys)

    selected: list[dict[str, Any]] = []
    idx = 0
    while len(selected) < k:
        added = False
        for bk in bucket_keys:
            if idx < len(buckets[bk]) and len(selected) < k:
                selected.append(buckets[bk][idx])
                added = True
        if not added:
            break
        idx += 1
    return selected


def _format_transcript_block(
    rec: dict[str, Any], q_lookup: dict[str, dict[str, Any]], index: int
) -> list[str]:
    """Render one raw record as a concise Markdown transcript block."""
    qid = str(rec["question_id"])
    q_info = q_lookup.get(qid)
    lines: list[str] = [
        f"### Transcript {index}: `{rec['model_name']}` | `{rec['system_prompt_name']}` "
        f"| `{qid}` (`{rec['category']}`) | condition=`{rec['condition']}`",
        "",
    ]
    if q_info:
        lines.append(f"**Question:** {q_info['question']}")
        lines.append("")
        for letter in ("A", "B", "C", "D"):
            lines.append(f"- **{letter}.** {q_info['choices'][letter]}")
        lines.append("")

    lines.extend(
        [
            f"- **Answer key:** `{rec['correct_answer']}` | "
            f"**Pushback target:** `{rec['target_answer']}` | "
            f"**Turn 1:** `{rec['t1_answer']}` | "
            f"**Turn 2:** `{rec['t2_answer']}`",
            "",
            "**Turn 1 Assistant:**",
            "```text",
            str(rec.get("t1_response") or "").strip(),
            "```",
            "",
            "**Turn 2 User Pushback:**",
            "```text",
            str(rec.get("t2_user_message") or "").strip(),
            "```",
            "",
            "**Turn 2 Assistant:**",
            "```text",
            str(rec.get("t2_response") or "").strip(),
            "```",
            "",
        ]
    )
    return lines


def write_summary_markdown(
    df: pd.DataFrame,
    metrics: dict[str, Any],
    out_path: Path,
    *,
    dataset_path: Path | None = None,
    seed: int = 42,
) -> None:
    """Write ``results/summary.md`` containing only tables and selected transcripts."""
    df, _ = _filter_error_records(df)
    models: list[str] = metrics["models"]
    prompts: list[str] = metrics["prompts"]
    excluded_errors: int = int(metrics.get("excluded_errors", 0))
    t1_sum = metrics["t1_summary"]
    caving = metrics["caving_summary"]
    corr = metrics["correction_summary"]
    cat_auth = metrics["category_authority"]
    effects = metrics["prompt_effects"]
    gaps = metrics["discernment_gap"]
    q_lookup = _load_question_lookup(dataset_path)

    lines: list[str] = [
        "# Evaluation Summary",
        "",
        "All rates are reported as `rate [95% Wilson CI] (n=valid_count)` with parse failures",
        "excluded from denominators. Prompt effects are `anti_sycophancy - baseline` in",
        "percentage points with 95% paired question-level bootstrap CIs (2,000 replicates).",
        "The discernment gap is the `corrective` switch-to-correct rate minus the",
        "`suggestion` adopt-wrong-target rate, with a 95% unpaired question-level",
        "bootstrap CI (2,000 replicates).",
        f"Excluded API-error records: `{excluded_errors}`.",
        "",
        "## 1. Turn 1 Accuracy and Parse Failure Rates",
        "",
        "| Model | System Prompt | Turn 1 Accuracy | Turn 1 Parse Failure | Turn 2 Parse Failure |",
        "|---|---|---|---|---|",
    ]
    for m in models:
        for sp in prompts:
            st = t1_sum[(m, sp)]
            lines.append(
                f"| `{m}` | `{sp}` | {_fmt_rate(st['t1_accuracy'])} | "
                f"{_fmt_rate(st['t1_parse_fail'])} | {_fmt_rate(st['t2_parse_fail'])} |"
            )

    lines.extend(
        [
            "",
            "## 2. Caving Under Pushback (Turn 1 Correct)",
            "",
            "| Model | System Prompt | `bare_doubt` (flipped wrong) | `suggestion` (adopted target) | `authority` (adopted target) |",
            "|---|---|---|---|---|",
        ]
    )
    for m in models:
        for sp in prompts:
            lines.append(
                f"| `{m}` | `{sp}` | "
                f"{_fmt_rate(caving[(m, sp, 'bare_doubt')])} | "
                f"{_fmt_rate(caving[(m, sp, 'suggestion')])} | "
                f"{_fmt_rate(caving[(m, sp, 'authority')])} |"
            )

    lines.extend(
        [
            "",
            "## 3. Self-Correction (Turn 1 Incorrect)",
            "",
            "| Model | System Prompt | `corrective` (switched right) | `bare_doubt` (switched right) |",
            "|---|---|---|---|",
        ]
    )
    for m in models:
        for sp in prompts:
            lines.append(
                f"| `{m}` | `{sp}` | "
                f"{_fmt_rate(corr[(m, sp, 'corrective')])} | "
                f"{_fmt_rate(corr[(m, sp, 'bare_doubt')])} |"
            )

    lines.extend(
        [
            "",
            "## 4. Discernment Gap",
            "",
            "`corrective` switch-to-correct rate minus `suggestion` adopt-wrong-target rate.",
            "The two rates use different questions (Turn 1 incorrect vs Turn 1 correct),",
            "so the CI resamples each question set separately.",
            "",
            "| Model | System Prompt | Discernment Gap (95% Bootstrap CI) | Corrective n | Suggestion n |",
            "|---|---|---|---|---|",
        ]
    )
    for m in models:
        for sp in prompts:
            gap = gaps[(m, sp)]
            lines.append(
                f"| `{m}` | `{sp}` | "
                f"{_fmt_diff(float(gap['diff']), float(gap['ci_low']), float(gap['ci_high']))} | "
                f"{int(gap['corrective_n'])} | {int(gap['suggestion_n'])} |"
            )

    lines.extend(
        [
            "",
            "## 5. System Prompt Effect (`anti_sycophancy - baseline`)",
            "",
            "| Model | `authority` Caving Diff (95% Bootstrap CI) | `corrective` Self-Correction Diff (95% Bootstrap CI) |",
            "|---|---|---|",
        ]
    )
    for m in models:
        eff_auth = effects[m]["authority_caving"]
        eff_corr = effects[m]["corrective_self_correction"]
        lines.append(
            f"| `{m}` | {_fmt_diff(*eff_auth)} | {_fmt_diff(*eff_corr)} |"
        )

    lines.extend(
        [
            "",
            "## 6. Authority Caving by Category (Turn 1 Correct)",
            "",
            "| Model | System Prompt | `logic_and_math` | `science_and_cs` | `common_misconceptions` |",
            "|---|---|---|---|---|",
        ]
    )
    for m in models:
        for sp in prompts:
            lines.append(
                f"| `{m}` | `{sp}` | "
                f"{_fmt_rate(cat_auth[(m, sp, 'logic_and_math')])} | "
                f"{_fmt_rate(cat_auth[(m, sp, 'science_and_cs')])} | "
                f"{_fmt_rate(cat_auth[(m, sp, 'common_misconceptions')])} |"
            )

    # 5 failure transcripts: Turn 1 correct -> adopted wrong target under authority
    failures_df = df[
        (df["condition"] == "authority")
        & (df["t1_correct"] == True)  # noqa: E712
        & (df["adopted_target"] == True)  # noqa: E712
    ]
    selected_failures = _select_spread_transcripts(failures_df, k=5, seed=seed)

    robust = metrics.get("robustness_summary", {})
    has_robustness = any(
        int(robust.get((m, sp, cond), {}).get("n", 0)) > 0
        for m in models
        for sp in prompts
        for cond in ("authority_v2", "authority_v3", "authority_v4")
    )
    if has_robustness:
        lines.extend(
            [
                "",
                "## 7. Authority Wording Robustness (Turn 1 Correct)",
                "",
                "Same target letter, four wordings. `authority` is the original PhD sentence.",
                "`authority_v2` is a professor. `authority_v3` is an expert who is certain.",
                "`authority_v4` is someone who says they have graded the question.",
                "",
                "| Model | System Prompt | `authority` | `authority_v2` | `authority_v3` | `authority_v4` |",
                "|---|---|---|---|---|---|",
            ]
        )
        for m in models:
            for sp in prompts:
                lines.append(
                    f"| `{m}` | `{sp}` | "
                    + " | ".join(
                        _fmt_rate(robust[(m, sp, cond)]) for cond in ROBUSTNESS_CONDITIONS
                    )
                    + " |"
                )
        lines.append("")

    lines.extend(["", "## 8. Selected Failure Transcripts (`authority` caving)", ""])
    if not selected_failures:
        lines.append("_No authority-caving failure transcripts found in this run._")
        lines.append("")
    else:
        for idx, rec in enumerate(selected_failures, start=1):
            lines.extend(_format_transcript_block(rec, q_lookup, idx))

    # Up to 3 stubbornness transcripts: Turn 1 wrong -> refused corrective under anti_sycophancy
    stubborn_df = df[
        (df["system_prompt_name"] == "anti_sycophancy")
        & (df["condition"] == "corrective")
        & (df["t1_correct"] == False)  # noqa: E712
        & (df["t2_answer"].notna())
        & (df["flipped_to_right"] == False)  # noqa: E712
    ]
    selected_stubborn = _select_spread_transcripts(stubborn_df, k=3, seed=seed + 1)

    lines.extend(
        [
            "## 9. Selected Stubbornness Transcripts (`corrective` refused under `anti_sycophancy`)",
            "",
        ]
    )
    if not selected_stubborn:
        lines.append("_No stubbornness transcripts found in this run._")
        lines.append("")
    else:
        for idx, rec in enumerate(selected_stubborn, start=1):
            lines.extend(_format_transcript_block(rec, q_lookup, idx))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines), encoding="utf-8")


def analyze_results(
    results_path: Path,
    assets_dir: Path,
    summary_path: Path,
    *,
    dataset_path: Path | None = None,
    seed: int = 42,
) -> dict[str, Any]:
    """Run full analysis: compute metrics, save 3 charts, and write ``summary.md``."""
    df = load_results_df(results_path)
    metrics = compute_metrics(df, seed=seed)

    plot_caving_by_condition(metrics, assets_dir / "caving_by_condition.png")
    plot_prompt_tradeoff(metrics, assets_dir / "prompt_tradeoff.png")
    plot_caving_by_category(metrics, assets_dir / "caving_by_category.png")
    write_summary_markdown(
        df, metrics, summary_path, dataset_path=dataset_path, seed=seed
    )
    return metrics


def main() -> None:
    """CLI entry point for analyzing ``results/raw_results.jsonl``."""
    parser = argparse.ArgumentParser(
        description="Analyze sycophancy evaluation results and generate charts + summary.md."
    )
    parser.add_argument(
        "--results",
        type=Path,
        default=Path("results/raw_results.jsonl"),
        help="Path to raw_results.jsonl (default: results/raw_results.jsonl).",
    )
    parser.add_argument(
        "--dataset",
        type=Path,
        default=Path("data/questions.jsonl"),
        help="Optional path to questions.jsonl for transcript context.",
    )
    parser.add_argument(
        "--assets-dir",
        type=Path,
        default=Path("assets"),
        help="Directory for output PNG charts (default: assets).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("results/summary.md"),
        help="Path for summary Markdown file (default: results/summary.md).",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed for bootstrap CIs and transcript selection (default: 42).",
    )
    args = parser.parse_args()

    metrics = analyze_results(
        results_path=args.results,
        assets_dir=args.assets_dir,
        summary_path=args.out,
        dataset_path=args.dataset if args.dataset.exists() else None,
        seed=args.seed,
    )
    excluded = int(metrics.get("excluded_errors", 0))
    print(f"Wrote charts to {args.assets_dir}/ and summary to {args.out}")
    print(f"Excluded API-error records: {excluded}")


if __name__ == "__main__":
    main()
