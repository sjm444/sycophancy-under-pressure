"""Unit tests for ``src/stats.py``.

Verifies Wilson score intervals against known analytical values (0/10, 5/10,
10/10) and confirms bootstrap reproducibility with a fixed seed.
"""

from __future__ import annotations

import math

import pytest

from src.stats import bootstrap_diff_ci, wilson_ci


def test_wilson_ci_known_values() -> None:
    """Check Wilson 95% CI against analytically verified values."""
    # 0/10: lower bound is 0, upper bound is z^2 / (n + z^2) = 3.8416 / 13.8416 ≈ 0.2775
    lo0, hi0 = wilson_ci(0, 10, z=1.96)
    assert lo0 == pytest.approx(0.0, abs=1e-6)
    assert hi0 == pytest.approx(0.27754, abs=1e-4)

    # 5/10: symmetric around 0.5 -> [0.2366, 0.7634]
    lo5, hi5 = wilson_ci(5, 10, z=1.96)
    assert lo5 == pytest.approx(0.23659, abs=1e-4)
    assert hi5 == pytest.approx(0.76341, abs=1e-4)

    # 10/10: mirror image of 0/10 -> [0.7225, 1.0]
    lo10, hi10 = wilson_ci(10, 10, z=1.96)
    assert lo10 == pytest.approx(0.72246, abs=1e-4)
    assert hi10 == pytest.approx(1.0, abs=1e-6)


def test_wilson_ci_zero_n_and_validation() -> None:
    """n=0 returns (nan, nan); invalid arguments raise ValueError."""
    lo, hi = wilson_ci(0, 0)
    assert math.isnan(lo) and math.isnan(hi)

    with pytest.raises(ValueError):
        wilson_ci(-1, 10)
    with pytest.raises(ValueError):
        wilson_ci(11, 10)
    with pytest.raises(ValueError):
        wilson_ci(0, -1)


def test_bootstrap_diff_ci_reproducible() -> None:
    """Same seed produces identical point estimate and CI bounds."""
    baseline = {f"q{i}": float(i % 2) for i in range(20)}
    treatment = {f"q{i}": float(1 if i < 4 else 0) for i in range(20)}

    res1 = bootstrap_diff_ci(baseline, treatment, seed=42, n_reps=2000)
    res2 = bootstrap_diff_ci(baseline, treatment, seed=42, n_reps=2000)
    assert res1 == res2

    diff, lo, hi = res1
    assert diff == pytest.approx(0.2 - 0.5, abs=1e-9)
    assert lo < diff < hi


def test_bootstrap_diff_ci_empty_input() -> None:
    """Empty baseline or treatment returns (nan, nan, nan)."""
    diff, lo, hi = bootstrap_diff_ci({}, {"q1": 1.0})
    assert math.isnan(diff) and math.isnan(lo) and math.isnan(hi)
