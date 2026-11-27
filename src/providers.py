"""One ``chat()`` function over Anthropic, OpenAI, OpenRouter, Ollama, and a mock.

Each SDK has its own parameters and error types, so this keeps the eval loop
from caring which provider it's talking to. It also handles retries with
backoff, and the ``mock`` provider lets the whole pipeline run in tests with
no network calls.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

import anthropic
import openai
from dotenv import load_dotenv

# Load .env once on import if present (keys are read from os.environ and never logged).
load_dotenv()

MAX_RETRIES: int = 6
BASE_BACKOFF_SECONDS: float = 1.0
MAX_BACKOFF_SECONDS: float = 30.0

OPENROUTER_BASE_URL: str = "https://openrouter.ai/api/v1"
DEFAULT_OLLAMA_BASE_URL: str = "http://localhost:11434/v1"

# Lazily cached map of question stem -> correct_answer letter for the mock provider.
_MOCK_DATASET_CACHE: dict[str, str] | None = None

_TURN1_QUESTION_RE = re.compile(r"^Question:\s*(.+?)\n\nA\.", re.DOTALL)
_SUGGESTED_TARGET_RE = re.compile(
    r"(?:actually|definitely)\s+([A-D])\b", re.IGNORECASE
)
_PREV_ANSWER_RE = re.compile(r"FINAL_ANSWER:\s*([A-D])\b", re.IGNORECASE)


def _is_retryable_error(exc: Exception) -> bool:
    """Return True if an API exception is transient and safe to retry.

    Retries rate limits (429), timeouts, connection errors, and 5xx server
    errors. Never retries authentication errors (401/403) or bad requests (400).

    Args:
        exc: Exception raised by an SDK call.

    Returns:
        True if the call should be retried with backoff, False otherwise.
    """
    # Explicit non-retryable client errors
    non_retryable = (
        anthropic.AuthenticationError,
        anthropic.PermissionDeniedError,
        anthropic.BadRequestError,
        openai.AuthenticationError,
        openai.PermissionDeniedError,
        openai.BadRequestError,
    )
    if isinstance(exc, non_retryable):
        return False

    retryable = (
        anthropic.RateLimitError,
        anthropic.APITimeoutError,
        anthropic.APIConnectionError,
        anthropic.InternalServerError,
        openai.RateLimitError,
        openai.APITimeoutError,
        openai.APIConnectionError,
        openai.InternalServerError,
    )
    if isinstance(exc, retryable):
        return True

    # Catch generic APIStatusError with 5xx status codes (e.g. 502/503/529)
    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int) and (status_code == 429 or status_code >= 500):
        return True

    return False


def _call_with_retry(fn: Any) -> str:
    """Execute a zero-argument callable with exponential backoff and jitter.

    Args:
        fn: Callable returning the assistant response text.

    Returns:
        The string returned by ``fn()``.

    Raises:
        Exception: Re-raises the last exception after ``MAX_RETRIES`` attempts
            or immediately if the error is non-retryable.
    """
    last_exc: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            return str(fn())
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if not _is_retryable_error(exc) or attempt == MAX_RETRIES - 1:
                raise
            # Exponential backoff with random jitter prevents thundering
            # herds when recovering from rate limits or temporary 5xx spikes.
            delay = min(MAX_BACKOFF_SECONDS, BASE_BACKOFF_SECONDS * (2**attempt))
            jitter = random.uniform(0.0, delay * 0.5)
            time.sleep(delay + jitter)

    assert last_exc is not None
    raise last_exc


def _load_default_mock_answer_key() -> dict[str, str]:
    """Load question -> correct_answer lookup from ``data/questions.jsonl`` if present."""
    global _MOCK_DATASET_CACHE
    if _MOCK_DATASET_CACHE is not None:
        return _MOCK_DATASET_CACHE

    cache: dict[str, str] = {}
    dataset_path = Path("data/questions.jsonl")
    if dataset_path.exists():
        for line in dataset_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            q_text = str(item.get("question", "")).strip()
            ans = str(item.get("correct_answer", "")).strip().upper()
            if q_text and ans in ("A", "B", "C", "D"):
                cache[q_text] = ans
    _MOCK_DATASET_CACHE = cache
    return cache


def _extract_question_stem(turn1_user_msg: str) -> str:
    """Extract the raw question stem from a formatted Turn 1 prompt."""
    match = _TURN1_QUESTION_RE.search(turn1_user_msg)
    if match:
        return match.group(1).strip()
    return turn1_user_msg.strip()


def _mock_chat(
    model_cfg: Mapping[str, Any],
    system_prompt: str,
    messages: Sequence[Mapping[str, str]],
) -> str:
    """Deterministic, zero-network mock LLM for testing the full pipeline.

    Default behavior (``model_id: mock-sycophant``):
      - Turn 1: answers correctly on 70% of questions (by SHA-256 hash of the
        question stem) and picks a deterministic wrong letter on the remaining
        30%, so both caving and self-correction branches are exercised.
      - Turn 2 under ``baseline`` system prompt:
          * ``bare_doubt``: keeps its Turn 1 answer.
          * ``suggestion``: adopts the suggested target on 50% of items.
          * ``authority``: adopts the suggested target on 100% of items.
          * ``corrective``: adopts the suggested correct answer on 100% of items.
      - Turn 2 under ``anti_sycophancy`` system prompt:
          * ``bare_doubt``: keeps its Turn 1 answer.
          * ``suggestion``: never caves (0%).
          * ``authority``: caves on 25% of items (substantially less than baseline).
          * ``corrective``: refuses on 50% of wrong items (demonstrates the
            stubbornness trade-off).

    Tests can override any of these rates via ``model_cfg["mock_rules"]`` and
    supply an explicit ``model_cfg["mock_answer_key"]`` mapping question text
    to the correct letter.
    """
    rules: Mapping[str, Any] = model_cfg.get("mock_rules", {})
    answer_key: Mapping[str, str] = (
        model_cfg.get("mock_answer_key") or _load_default_mock_answer_key()
    )

    turn1_msg = messages[0]["content"]
    stem = _extract_question_stem(turn1_msg)
    hash_val = hashlib.sha256(stem.encode("utf-8")).digest()[0]

    correct_letter = answer_key.get(stem, "A")
    wrong_letters = [lt for lt in ("A", "B", "C", "D") if lt != correct_letter]
    default_wrong = wrong_letters[hash_val % len(wrong_letters)]

    is_anti_sycophancy = "social pressure" in system_prompt.lower()

    # Turn 1: single user message
    if len(messages) == 1:
        if "t1_always_correct" in rules:
            is_correct = bool(rules["t1_always_correct"])
        elif "t1_wrong_questions" in rules:
            is_correct = stem not in rules["t1_wrong_questions"]
        else:
            is_correct = (hash_val % 10) < 7

        chosen = correct_letter if is_correct else default_wrong
        return (
            f"[mock] Considering the question carefully, option {chosen} fits best.\n"
            f"FINAL_ANSWER: {chosen}"
        )

    # Turn 2: [user_t1, assistant_t1, user_t2]
    t1_reply = messages[1]["content"]
    t2_pushback = messages[2]["content"]
    prev_match = _PREV_ANSWER_RE.search(t1_reply)
    prev_answer = prev_match.group(1).upper() if prev_match else correct_letter

    target_match = _SUGGESTED_TARGET_RE.search(t2_pushback)
    suggested_target = target_match.group(1).upper() if target_match else None

    # Robustness wordings do not say "PhD", but they are still authority
    # pushes. The mock should treat them like the original authority condition.
    pushback_lower = t2_pushback.lower()
    is_authority = any(
        marker in pushback_lower
        for marker in ("phd", "professor", "i'm an expert", "graded this exact")
    )
    is_bare_doubt = suggested_target is None
    t1_was_correct = prev_answer == correct_letter

    if is_bare_doubt:
        cave_rule = rules.get(
            "anti_bare_doubt_cave" if is_anti_sycophancy else "baseline_bare_doubt_cave",
            False,
        )
        if cave_rule:
            new_answer = default_wrong if t1_was_correct else correct_letter
            return (
                f"[mock] On second thought, I will switch to {new_answer}.\n"
                f"FINAL_ANSWER: {new_answer}"
            )
        return (
            f"[mock] I rechecked my reasoning and still stand by {prev_answer}.\n"
            f"FINAL_ANSWER: {prev_answer}"
        )

    assert suggested_target is not None
    if t1_was_correct:
        if is_authority:
            default_cave = (hash_val % 4 == 0) if is_anti_sycophancy else True
            should_cave = rules.get(
                "anti_authority_cave" if is_anti_sycophancy else "baseline_authority_cave",
                default_cave,
            )
        else:
            default_cave = False if is_anti_sycophancy else (hash_val % 2 == 0)
            should_cave = rules.get(
                "anti_suggestion_cave" if is_anti_sycophancy else "baseline_suggestion_cave",
                default_cave,
            )
        if should_cave:
            return (
                f"[mock] You raise a strong point; I apologize and revise my answer "
                f"to {suggested_target}.\nFINAL_ANSWER: {suggested_target}"
            )
        return (
            f"[mock] I appreciate your perspective, but my reasoning still supports "
            f"{prev_answer}.\nFINAL_ANSWER: {prev_answer}"
        )

    # Turn 1 was incorrect -> corrective pushback toward correct_answer
    default_correct = (hash_val % 2 == 1) if is_anti_sycophancy else True
    should_correct = rules.get(
        "anti_corrective_accept" if is_anti_sycophancy else "baseline_corrective_accept",
        default_correct,
    )
    if should_correct:
        return (
            f"[mock] Good catch — rechecking shows {suggested_target} is indeed right.\n"
            f"FINAL_ANSWER: {suggested_target}"
        )
    return (
        f"[mock] I am not persuaded without a concrete proof; keeping {prev_answer}.\n"
        f"FINAL_ANSWER: {prev_answer}"
    )


def _chat_anthropic(
    model_cfg: Mapping[str, Any],
    system_prompt: str,
    messages: Sequence[Mapping[str, str]],
    temperature: float,
    max_tokens: int,
) -> str:
    """Send a chat completion request via the official Anthropic SDK."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set in the environment or .env file."
        )
    client = anthropic.Anthropic(api_key=api_key)

    kwargs: dict[str, Any] = {
        "model": str(model_cfg["model_id"]),
        "system": system_prompt,
        "messages": [
            {"role": m["role"], "content": m["content"]} for m in messages
        ],
        "max_tokens": max_tokens,
    }
    if model_cfg.get("supports_temperature", True):
        kwargs["temperature"] = temperature
    # Haiku 5.5 thinks by default, and those tokens count toward max_tokens.
    # Opt in per model. The default effort (medium) accepts this value.
    if model_cfg.get("thinking") == "disabled":
        kwargs["thinking"] = {"type": "disabled"}

    def _invoke() -> str:
        response = client.messages.create(**kwargs)
        text_blocks = [
            block.text for block in response.content if getattr(block, "type", None) == "text"
        ]
        return "\n".join(text_blocks)

    return _call_with_retry(_invoke)


def _chat_openai_compatible(
    model_cfg: Mapping[str, Any],
    system_prompt: str,
    messages: Sequence[Mapping[str, str]],
    temperature: float,
    max_tokens: int,
    *,
    api_key: str,
    base_url: str | None = None,
    token_param: str = "max_tokens",
) -> str:
    """Send a chat completion request via the OpenAI SDK (OpenAI, OpenRouter, Ollama)."""
    client_kwargs: dict[str, Any] = {"api_key": api_key}
    if base_url:
        client_kwargs["base_url"] = base_url
    client = openai.OpenAI(**client_kwargs)

    full_messages = [{"role": "system", "content": system_prompt}] + [
        {"role": m["role"], "content": m["content"]} for m in messages
    ]
    # Current OpenAI models reject `max_tokens` and require
    # `max_completion_tokens`. OpenRouter and Ollama still use `max_tokens`.
    kwargs: dict[str, Any] = {
        "model": str(model_cfg["model_id"]),
        "messages": full_messages,
        token_param: max_tokens,
    }
    # Some reasoning models reject the `temperature` parameter entirely.
    if model_cfg.get("supports_temperature", True):
        kwargs["temperature"] = temperature

    def _invoke() -> str:
        response = client.chat.completions.create(**kwargs)
        content = response.choices[0].message.content
        return content or ""

    return _call_with_retry(_invoke)


def chat(
    model_cfg: Mapping[str, Any],
    system_prompt: str,
    messages: Sequence[Mapping[str, str]],
    temperature: float,
    max_tokens: int | None = None,
) -> str:
    """Send a multi-turn chat request to the configured provider and return text.

    Args:
        model_cfg: Model entry from ``config.yaml`` containing at least
            ``"provider"`` and ``"model_id"``.
        system_prompt: System-level instruction string.
        messages: Sequence of ``{"role": "user" | "assistant", "content": str}``
            conversation turns.
        temperature: Sampling temperature (ignored if
            ``model_cfg["supports_temperature"]`` is ``False``).
        max_tokens: Maximum tokens to generate. Defaults to
            ``model_cfg.get("max_tokens", 1024)``.

    Returns:
        The assistant's raw response text.

    Raises:
        ValueError: If ``provider`` is unknown.
        RuntimeError: If a required API key environment variable is missing.
    """
    provider = str(model_cfg["provider"]).lower()
    resolved_max_tokens = (
        max_tokens if max_tokens is not None else int(model_cfg.get("max_tokens", 1024))
    )

    if provider == "mock":
        return _mock_chat(model_cfg, system_prompt, messages)

    if provider == "anthropic":
        return _chat_anthropic(
            model_cfg, system_prompt, messages, temperature, resolved_max_tokens
        )

    if provider == "openai":
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError(
                "OPENAI_API_KEY is not set in the environment or .env file."
            )
        return _chat_openai_compatible(
            model_cfg,
            system_prompt,
            messages,
            temperature,
            resolved_max_tokens,
            api_key=api_key,
            token_param="max_completion_tokens",
        )

    if provider == "openrouter":
        api_key = os.environ.get("OPENROUTER_API_KEY")
        if not api_key:
            raise RuntimeError(
                "OPENROUTER_API_KEY is not set in the environment or .env file."
            )
        return _chat_openai_compatible(
            model_cfg,
            system_prompt,
            messages,
            temperature,
            resolved_max_tokens,
            api_key=api_key,
            base_url=OPENROUTER_BASE_URL,
        )

    if provider == "ollama":
        base_url = os.environ.get("OLLAMA_BASE_URL", DEFAULT_OLLAMA_BASE_URL)
        # Ollama's OpenAI-compatible server ignores the API key, but the
        # OpenAI Python SDK requires a non-empty string.
        return _chat_openai_compatible(
            model_cfg,
            system_prompt,
            messages,
            temperature,
            resolved_max_tokens,
            api_key="ollama",
            base_url=base_url,
        )

    raise ValueError(
        f"Unsupported provider {provider!r}. "
        "Expected one of: 'anthropic', 'openai', 'openrouter', 'ollama', 'mock'."
    )
