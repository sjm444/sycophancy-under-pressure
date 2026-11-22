"""Statistical uncertainty helpers (Wilson intervals and question-level bootstrap).

Why this file exists:
    Reporting raw percentages like "40% caving" without sample sizes or
    uncertainty bounds is misleading — 4/10 and 400/1000 have very different
    reliability. This module provides two lightweight, dependency-free
    statistical tools tailored for multiple-choice LLM evaluations:
      1. ``wilson_ci`` for single proportions (e.g., caving rate on 100 items).
      2. ``bootstrap_diff_ci`` for comparing two system prompts on the same
         set of questions.
      3. ``bootstrap_independent_diff_ci`` for a difference of two rates
         measured on different questions (the discernment gap).

Pipeline position:
    Imported by ``src/analyze.py`` to compute confidence intervals for every
    table and chart error bar.
"""

from __future__ import annotations

import math
from typing import Mapping

import numpy as np


def wilson_ci(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Compute the Wilson score confidence interval for a binomial proportion.

    What it computes:
        Given ``successes`` events out of ``n`` independent trials, returns
        ``(low, high)`` bounds (in ``[0.0, 1.0]``) for the true underlying rate.
        With ``z=1.96``, this is a 95% confidence interval.

    Why it is used here:
        The textbook normal approximation (``p ± 1.96 * sqrt(p(1-p)/n)``) breaks
        down when ``n`` is small or when ``p`` is near 0% or 100% — for
        example, at 0/10 it claims a zero-width interval ``[0%, 0%]`` and can
        even produce negative lower bounds. The Wilson score interval stays
        inside ``[0, 1]`` and remains well-calibrated even at 0/10 or 10/10.

    How to read the output:
        A result of ``(0.24, 0.76)`` means the data are consistent with a true
        rate anywhere between 24% and 76%. Wider intervals mean higher
        uncertainty (usually because ``n`` is small).

    Args:
        successes: Number of positive outcomes (``0 <= successes <= n``).
        n: Total number of trials (``n >= 0``).
        z: Critical value from the standard normal distribution
            (default ``1.96`` for a 95% CI).

    Returns:
        ``(low, high)`` tuple of floats in ``[0.0, 1.0]``, or ``(nan, nan)``
        if ``n == 0``.

    Raises:
        ValueError: If ``n < 0`` or ``successes`` is outside ``[0, n]``.

    Example:
        >>> low, high = wilson_ci(5, 10)
        >>> round(low, 3), round(high, 3)
        (0.237, 0.763)
    """
    if n < 0:
        raise ValueError(f"n must be non-negative, got {n}")
    if successes < 0 or successes > n:
        raise ValueError(f"successes ({successes}) must be between 0 and n ({n})")
    if n == 0:
        return (float("nan"), float("nan"))

    p = successes / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (p + z2 / (2.0 * n)) / denom
    half_width = (z / denom) * math.sqrt((p * (1.0 - p)) / n + z2 / (4.0 * n * n))

    low = max(0.0, center - half_width)
    high = min(1.0, center + half_width)
    return (low, high)


def bootstrap_diff_ci(
    baseline_by_qid: Mapping[str, float],
    treatment_by_qid: Mapping[str, float],
    seed: int = 42,
    n_reps: int = 2000,
    alpha: float = 0.05,
) -> tuple[float, float, float]:
    """Compute a percentile bootstrap CI for ``treatment - baseline`` over questions.

    What it computes:
        Estimates the difference in rate (``mean(treatment) - mean(baseline)``)
        and its ``(1 - alpha)`` confidence interval (95% by default) by
        resampling **question IDs** with replacement ``n_reps`` times.

    Why it is used here:
        We evaluate both ``baseline`` and ``anti_sycophancy`` on the *same*
        questions. Resampling question IDs (rather than treating the two runs
        as independent samples) preserves that pairing — if a question is hard
        under both prompts, it is drawn together in each bootstrap replicate.
        It also handles ``samples_per_question > 1`` cleanly when each value is
        the question's average outcome across samples, avoiding over-counting
        repeated samples of the same question as independent data points.

    How to read the output:
        Returns ``(diff, ci_low, ci_high)`` where ``diff = treatment - baseline``.
        For caving rates, a *negative* diff (e.g., ``-0.35``, CI ``[-0.48, -0.22]``)
        means ``anti_sycophancy`` reduced caving by 35 percentage points. If
        the interval excludes 0, the difference is statistically distinguishable
        from zero at the ``1 - alpha`` level.

    Args:
        baseline_by_qid: Mapping from ``question_id`` to numeric outcome
            (typically ``0.0`` or ``1.0``, or a per-question mean) for the
            ``baseline`` system prompt.
        treatment_by_qid: Same mapping for the ``anti_sycophancy`` prompt.
        seed: Random seed for reproducible resampling (default ``42``).
        n_reps: Number of bootstrap replicates (default ``2000``).
        alpha: Significance level (default ``0.05`` for a 95% CI).

    Returns:
        ``(point_diff, ci_low, ci_high)``. Returns ``(nan, nan, nan)`` if
        either input mapping is empty.

    Example:
        >>> base = {f"q{i}": 1.0 for i in range(10)}
        >>> anti = {f"q{i}": 0.0 for i in range(10)}
        >>> diff, lo, hi = bootstrap_diff_ci(base, anti, seed=42, n_reps=500)
        >>> (diff, lo, hi)
        (-1.0, -1.0, -1.0)
    """
    if not baseline_by_qid or not treatment_by_qid:
        return (float("nan"), float("nan"), float("nan"))

    # Sort IDs so the resampling order is completely deterministic.
    all_qids = sorted(set(baseline_by_qid.keys()) | set(treatment_by_qid.keys()))
    n_qids = len(all_qids)

    base_vals = np.array(
        [baseline_by_qid.get(qid, np.nan) for qid in all_qids], dtype=float
    )
    treat_vals = np.array(
        [treatment_by_qid.get(qid, np.nan) for qid in all_qids], dtype=float
    )

    point_diff = float(np.nanmean(treat_vals) - np.nanmean(base_vals))

    rng = np.random.default_rng(seed)
    idx = rng.integers(0, n_qids, size=(n_reps, n_qids))

    sampled_base = base_vals[idx]
    sampled_treat = treat_vals[idx]

    # WHY: Because Turn 1 accuracy can differ slightly between prompts, a
    # question might enter `authority` under baseline but not under
    # anti_sycophancy. Using nanmean handles unpaired entries cleanly while
    # preserving pairing for every shared question.
    valid_base = ~np.isnan(sampled_base)
    valid_treat = ~np.isnan(sampled_treat)
    valid_reps = valid_base.any(axis=1) & valid_treat.any(axis=1)

    if not np.any(valid_reps):
        return (point_diff, float("nan"), float("nan"))

    base_means = np.nanmean(sampled_base[valid_reps], axis=1)
    treat_means = np.nanmean(sampled_treat[valid_reps], axis=1)
    diffs = treat_means - base_means

    ci_low = float(np.percentile(diffs, 100.0 * (alpha / 2.0)))
    ci_high = float(np.percentile(diffs, 100.0 * (1.0 - alpha / 2.0)))
    return (point_diff, ci_low, ci_high)


def bootstrap_independent_diff_ci(
    left_by_qid: Mapping[str, float],
    right_by_qid: Mapping[str, float],
    seed: int = 42,
    n_reps: int = 2000,
    alpha: float = 0.05,
) -> tuple[float, float, float]:
    """Compute a percentile bootstrap CI for ``mean(left) - mean(right)``.

    What it computes:
        The point estimate is the difference of the two group means. The
        ``(1 - alpha)`` interval (95% by default) comes from resampling each
        group separately, with replacement, at its own size, ``n_reps`` times.

    Why it is not ``bootstrap_diff_ci``:
        ``bootstrap_diff_ci`` resamples one shared list of question IDs so a
        question stays paired across two system prompts. The discernment gap
        subtracts rates from different questions: ``corrective`` trials are
        Turn 1 incorrect, and ``suggestion`` trials are Turn 1 correct. A
        question cannot be in both groups, so each group is resampled on its
        own.

    How to read the output:
        Returns ``(diff, ci_low, ci_high)`` where
        ``diff = mean(left) - mean(right)``. For the discernment gap, ``left``
        is the corrective switch-to-correct outcome and ``right`` is the
        suggestion adopt-wrong-target outcome. A positive diff means the model
        accepted a named correct answer more often than it adopted a named
        wrong answer. If the interval excludes 0, the two rates differ at the
        ``1 - alpha`` level.

    Args:
        left_by_qid: Mapping from ``question_id`` to a numeric outcome
            (typically ``0.0`` or ``1.0``, or a per-question mean).
        right_by_qid: Same mapping for the rate being subtracted.
        seed: Random seed for reproducible resampling (default ``42``).
        n_reps: Number of bootstrap replicates (default ``2000``).
        alpha: Significance level (default ``0.05`` for a 95% CI).

    Returns:
        ``(point_diff, ci_low, ci_high)``. Returns ``(nan, nan, nan)`` if
        either mapping is empty or contains only NaN values.
    """
    if not left_by_qid or not right_by_qid:
        return (float("nan"), float("nan"), float("nan"))

    # Sort so the values line up the same way on every call with this seed.
    left_vals = np.array(
        [left_by_qid[qid] for qid in sorted(left_by_qid)], dtype=float
    )
    right_vals = np.array(
        [right_by_qid[qid] for qid in sorted(right_by_qid)], dtype=float
    )
    left_vals = left_vals[~np.isnan(left_vals)]
    right_vals = right_vals[~np.isnan(right_vals)]
    if left_vals.size == 0 or right_vals.size == 0:
        return (float("nan"), float("nan"), float("nan"))

    point_diff = float(left_vals.mean() - right_vals.mean())

    rng = np.random.default_rng(seed)
    left_idx = rng.integers(0, left_vals.size, size=(n_reps, left_vals.size))
    right_idx = rng.integers(0, right_vals.size, size=(n_reps, right_vals.size))
    diffs = left_vals[left_idx].mean(axis=1) - right_vals[right_idx].mean(axis=1)

    ci_low = float(np.percentile(diffs, 100.0 * (alpha / 2.0)))
    ci_high = float(np.percentile(diffs, 100.0 * (1.0 - alpha / 2.0)))
    return (point_diff, ci_low, ci_high)
