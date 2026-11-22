# Sycophancy Under Pressure

A small, reproducible AI safety evaluation measuring whether Large Language Models (LLMs) abandon correct answers under social pressure, whether authority framing amplifies caving, and whether an anti-sycophancy system prompt reduces caving without making models stubbornly reject valid corrections.

---

## Research Question & Hypotheses

**Research question:** Do LLMs abandon correct answers under social pressure? Does authority framing make it worse than plain disagreement? Does an "anti-sycophancy" system prompt reduce caving without making the model stubborn when it is actually wrong?

- **H1:** Models cave more to `authority` framing ("I have a PhD...") than to `bare_doubt` ("Are you sure?").
- **H2:** The `anti_sycophancy` system prompt reduces `authority` caving compared to the `baseline` system prompt.
- **H3:** The `anti_sycophancy` system prompt also reduces `corrective` self-correction when the model is wrong on Turn 1 (a caving-vs.-stubbornness trade-off).

---

## Related Work

- Sharma, M., Tong, M., Korbak, T., Duvenaud, D., Askell, A., Bowman, S. R., Cheng, N., Durmus, E., Hatfield-Dodds, Z., Johnston, S. R., Kravec, S., Maxwell, T., McCandlish, S., Ndousse, K., Rausch, O., Schiefer, N., Yan, D., Zhang, M., & Perez, E. (2023). *Towards Understanding Sycophancy in Language Models*. Anthropic. [arXiv:2310.13548](https://arxiv.org/abs/2310.13548)
- **TODO:** Add additional related work (e.g., follow-up evaluations on authority bias, multi-turn sycophancy, and prompt-based vs. training-based mitigations).

---

## Methodology Summary

- **Dataset (300 items, seeded):** 100 `logic_and_math` and 100 `science_and_cs` questions filtered to `error_type == "ok"` from `edinburgh-dawg/mmlu-redux-2.0`, plus 100 `common_misconceptions` 4-choice questions built from `truthfulqa/truthful_qa` (`multiple_choice`, `mc1_targets`).
- **Two-turn branching protocol:** Every `(model, system_prompt, question, sample)` runs Turn 1 once, then branches independently into Turn 2 pushbacks:
  - If Turn 1 was **correct:** `bare_doubt`, `suggestion`, `authority`.
  - If Turn 1 was **incorrect:** `bare_doubt`, `corrective`.
- **Statistical reporting:** All single rates include `n` and 95% Wilson score CIs; prompt effects (`anti_sycophancy - baseline`) use 95% paired question-level bootstrap CIs (2,000 replicates). Parse failures are excluded from denominators and reported separately.

For full details, see:
- [`docs/HOW_IT_WORKS.md`](docs/HOW_IT_WORKS.md) — pipeline walkthrough, end-to-end example, glossary, and chart reading guide.
- [`docs/DESIGN_DECISIONS.md`](docs/DESIGN_DECISIONS.md) — rationale and trade-offs for every design choice.

---

## Setup & Reproducibility

### 1. Create a virtual environment and install dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and add API keys only when you are ready to run real models:

```bash
cp .env.example .env
```

### 2. Build the dataset (deterministic)

```bash
python -m src.build_dataset
```

This writes `data/questions.jsonl` (300 items) and `data/SPOT_CHECK.md` (15 random items for manual answer-key review).

### 3. Run the test suite (no network required)

```bash
python -m pytest -v
```

### 4. End-to-end mock smoke test (10 questions, no API keys)

```bash
python -m src.run_eval --models mock --limit 10 --out results/raw_results.jsonl
python -m src.analyze --results results/raw_results.jsonl --out results/summary.md
```

### 5. Estimate API call counts before spending money (`--dry-run`)

Fill in your desired `model_id` values in `config.yaml`, then run:

```bash
python -m src.run_eval --models claude open_weight --dry-run
```

> **Cost note:** Every `(model × system_prompt × question × sample)` makes **1 Turn 1 call** plus **2–3 Turn 2 calls** (3 if Turn 1 is correct, 2 if Turn 1 is wrong). Always run `--dry-run` first to see the exact Turn 1 count and min/max total calls before launching a paid run. On the full 300-question dataset with 2 system prompts and `samples_per_question: 1`, each model makes **1,800–2,400 API calls**.

### 6. Small real sanity check (`--limit 5`)

```bash
python -m src.run_eval --models claude --limit 5 --out results/sanity_check.jsonl
python -m src.analyze --results results/sanity_check.jsonl --out results/sanity_summary.md
```

### 7. Full run (with `--resume` support)

```bash
python -m src.run_eval --models claude open_weight --out results/raw_results.jsonl
# If interrupted, pick up right where you left off:
python -m src.run_eval --models claude open_weight --resume --out results/raw_results.jsonl
```

### 8. Analyze results

```bash
python -m src.analyze --results results/raw_results.jsonl --out results/summary.md
```

---

## Results

> **TODO:** Replace the placeholders below with your real-model results after running `src/run_eval.py` and `src/analyze.py`.

### Summary Table

_TODO: Paste the summary tables from `results/summary.md` here._

### Charts

![Caving by Pushback Condition](assets/caving_by_condition.png)

![Anti-Sycophancy Prompt Trade-Off](assets/prompt_tradeoff.png)

![Authority Caving by Category](assets/caving_by_category.png)

---

## Failure Case Analysis

**TODO (for you to write):** Inspect the selected failure and stubbornness transcripts in `results/summary.md` and summarize recurring patterns (e.g., deferring to credentials vs. fabricating post-hoc rationalizations for the wrong option).

---

## Limitations

**TODO (for you to write):** Expand on the following limitations when interpreting your results:

- **Benchmark contamination (TruthfulQA / MMLU):** Models may have seen these questions during pretraining, inflating Turn 1 accuracy compared to novel questions.
- **Remaining label noise:** MMLU-Redux 2.0 removes flagged errors, but some subtle ambiguities or disputed items may still remain.
- **Single fixed wording per pushback:** Each condition uses one exact phrasing; models may be sensitive to small wording changes or tone variations.
- **Sample size:** 100 items per category (and fewer for Turn 1 incorrect `corrective` items on strong models) produces non-trivial confidence intervals.
- **English only:** Findings may not generalize to other languages or cultural authority norms.
- **Multiple choice vs. open-ended answers:** Forced-choice `A`–`D` questions make scoring clean and automated, but real-world sycophancy often appears in open-ended advice, code review, or nuanced disagreements.
