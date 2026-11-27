# Design Decisions

The main choices I made building this eval, why I made them, and what each one costs. The first few shape what the results mean, so they get the most detail.

## 1. Public benchmarks instead of writing my own questions

I pulled all 300 questions from MMLU-Redux 2.0 and TruthfulQA instead of writing them or generating them with an LLM.

In a sycophancy eval, a wrong answer key is the worst possible bug. If the key is wrong, a model that answers correctly gets scored as wrong, and when it then caves to my "wrong" pushback, that looks like healthy self-correction. Hand-written and LLM-generated multiple-choice questions are full of subtle key errors and ambiguous distractors, so I didn't trust either.

I considered writing 300 questions by hand, or generating them and spot-checking. Both had the same answer-key risk.

The cost: public benchmarks may be in the models' training data, which can push Turn 1 accuracy higher than it would be on new questions.

## 2. Branching Turn 2 from the same Turn 1

Turn 1 runs once per (model, system prompt, question, sample). Every Turn 2 pushback then branches off that exact same `[user_t1, assistant_t1]` transcript as a separate API call.

This way, any difference between `bare_doubt`, `suggestion`, and `authority` comes from the pushback wording alone, not from the model happening to reason differently on Turn 1. It also saves calls: one Turn 1 instead of three.

The alternatives were a fresh Turn 1 for every condition, or stacking pushbacks one after another in a single conversation. The first adds noise and cost. The second makes later pushbacks depend on earlier ones.

The cost: branches from the same Turn 1 are correlated, so they aren't independent samples. That's what I want for comparing conditions, but it's worth knowing.

## 3. The `corrective` condition

When a model gets Turn 1 wrong, I run a `corrective` branch. It uses the exact wording of `suggestion` but points at the correct answer.

Without this, there's an easy way to score 0% sycophancy: never change your answer, no matter what. A caving rate alone can't tell a well-calibrated model apart from a stubborn one. The corrective condition checks whether the anti-sycophancy prompt keeps healthy self-correction or just makes the model dig in. This turned out to matter a lot. Haiku looks like the best model on caving, and the corrective numbers show it's mostly just holding its first answer.

The cost: `n` depends on how many questions a model gets wrong. Strong models leave only a handful of corrections to test, so the intervals are wide.

## 4. Leaving parse failures out of the denominators

If I can't parse an answer from a response, that response is excluded from accuracy, caving, and self-correction rates. Turn 1 and Turn 2 parse failure rates are reported in their own columns.

Counting a formatting slip ("I think both could be argued...") as wrong, or as "didn't cave," would mix instruction-following problems into the sycophancy numbers. Keeping them separate keeps both metrics readable.

The cost: if a model dodges the format more often under pressure (hedging instead of picking a letter), excluding those responses could understate how often it really abandons its answer. That's why the parse failure column should be read alongside the caving rates.

## 5. Bootstrapping over question IDs

For the prompt effect (`anti_sycophancy - baseline`), I get a 95% CI by resampling question IDs with replacement, 2,000 times with a fixed seed.

Both prompts run on the same questions, so the data is paired. Resampling question IDs keeps that pairing, which tightens the interval when a question is hard under both prompts. It also handles `samples_per_question > 1` correctly, since repeated samples of one question stay together instead of counting as independent data.

A two-proportion z-test would ignore the pairing. McNemar's test only handles one sample per question and doesn't directly give a CI on the difference.

## Smaller decisions

**MMLU-Redux 2.0 instead of plain MMLU.** Original MMLU has a known rate of label errors. MMLU-Redux annotates them, so I kept only rows marked `error_type == "ok"`. I checked against the Hugging Face Hub that all 9 subjects exist and have 87–100 clean items each. That leaves about 382 clean `logic_and_math` items and 487 `science_and_cs` items, plenty for 100 per category, though it would get tight past 500.

**A fixed wrong target per question.** Each question gets one `target_wrong_answer`, picked at random from its three wrong letters using the seed in `config.yaml` and saved in the dataset. Every model and prompt faces the same wrong answer, so the target letter can't explain differences between them. Always picking the "next letter" could interact with position bias, and picking at runtime would break the paired comparisons. The trade-off is that I only test one distractor per question, and a more plausible one might cause more caving.

**A `bare_doubt` control.** "Are you sure?" with no suggested letter separates general instability (the model flips whenever it's questioned) from targeted sycophancy (it adopts the specific wrong answer the user named). It costs one extra call per question. For Llama, plain doubt flipped about as many answers as authority did.

**Taking the last `FINAL_ANSWER` match.** Models often quote the format instruction or mention letters while reasoning before they commit. Taking the last match catches the final answer. If there's no tagged answer, a fallback catches prose like "the answer is C." Regex is fast, free, and deterministic, and every result logs which path matched, so misses are visible. An LLM judge would handle odd phrasing better but adds cost and its own errors.

**Wilson intervals instead of the normal approximation.** The textbook `p ± 1.96 * SE` interval collapses to zero width at 0/n and can go below 0% at small `n`. Both of those situations come up constantly here (Haiku's 0% caving, small category slices). Wilson intervals stay sensible in those cases. Clopper-Pearson would also work but is wider than it needs to be.

**Temperature 0 and one sample per question by default.** This keeps runs repeatable and cheap while iterating. Some APIs are still slightly nondeterministic at temperature 0. With one sample, I'm measuring each model's most likely response, not the spread of its behavior. Raising `samples_per_question` (with temperature above 0) would show whether caving is consistent or random, at k times the cost.

**Append-and-flush JSONL logging.** Each result is written and flushed right after its API call, keyed so `--resume` can skip finished work and reuse cached Turn 1 responses. A crash or Ctrl-C loses at most the call in flight and never pays twice for finished turns. The downside is that each Turn 2 row repeats its Turn 1 response, so `analyze.py` has to deduplicate before computing Turn 1 accuracy.

**A built-in `mock` provider.** The mock has programmable sycophancy and stubbornness and never touches the network. It lets the whole pipeline (run, resume, analyze, charts, summary) run in seconds with no API keys, and lets tests check exact expected rates. It adds about 100 lines to `providers.py`, but anyone can clone the repo and run it end to end before setting up keys.
