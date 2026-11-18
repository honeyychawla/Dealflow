"""Client for OpenRouter's Decisions API, which serves TypeSafe's Jev.

Jev is not a chat model. You send a `state` (the thing to judge) and a map of
typed `questions`, and get back typed answers with probabilities:

    POST https://openrouter.ai/api/alpha/decisions
    {"model": "typesafe/jev-1.14",
     "state": {...},
     "questions": {
        "category":  {"type": "choice", "criteria": {"label": "when it applies", ...}},
        "is_spam":   {"type": "noul",   "criteria": {"true": "...", "false": "..."}},
        "urgency":   {"type": "score",  "criteria": ["lowest", "...", "highest"]}}}

    -> {"answers": {
          "category": {"type": "choice", "choice": "label", "probabilities": {...}, "confidence": 0.75},
          "is_spam":  {"type": "noul", "noul": 0.96},
          "urgency":  {"type": "score", "score": 1.99, "probabilities": {"0": 0, "1": 0.01, "2": 0.99},
                       "legend": {...}, "confidence": 0.99}},
        "usage": {"input_tokens": 476, "output_tokens": 70, "cost": 0.00002}}

The endpoint is marked alpha by OpenRouter, so everything shape-specific lives
in this one file.
"""

from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.request
from typing import Any

from ..net import explain_tls_failure, ssl_context

DECISIONS_PATH = "/api/alpha/decisions"
RETRYABLE = {408, 429, 500, 502, 503, 524, 529}

# Above ~32,000 bytes of text Jev switches to a slower, less accurate
# long-context mode, so callers keep `state` under this budget.
STATE_BYTE_BUDGET = 28_000


class JevError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def decide(
    api_key: str,
    model: str,
    state: Any,
    questions: dict[str, dict[str, Any]],
    base_url: str = "https://openrouter.ai",
    timeout: float = 60.0,
    attempts: int = 4,
) -> dict[str, Any]:
    """One Decisions request. Retries rate limits and upstream hiccups with backoff."""
    if not api_key:
        raise JevError("OPENROUTER_API_KEY is not set")
    body = json.dumps({"model": model, "state": state, "questions": questions}).encode("utf-8")
    request = urllib.request.Request(
        base_url.rstrip("/") + DECISIONS_PATH,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "X-Title": "Dealflow Triage",
        },
    )
    last_error: JevError | None = None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=timeout, context=ssl_context()) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if "answers" not in payload:
                raise JevError(f"unexpected response shape: {str(payload)[:300]}")
            return payload
        except urllib.error.HTTPError as error:
            delay = _retry_after(error) or (1.5 * (2**attempt) + random.random())
            detail = _error_message(error)
            last_error = JevError(f"HTTP {error.code}: {detail}", status=error.code)
            if error.code not in RETRYABLE:
                raise last_error from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            tls = explain_tls_failure(error)
            if tls:  # retrying cannot fix a certificate problem
                raise JevError(tls) from None
            last_error = JevError(f"network error: {error}")
            delay = 1.5 * (2**attempt) + random.random()
        except json.JSONDecodeError as error:
            raise JevError(f"response was not JSON: {error}") from None
        if attempt < attempts - 1:
            time.sleep(min(delay, 30))
    assert last_error is not None
    raise last_error


def _error_message(error: urllib.error.HTTPError) -> str:
    try:
        payload = json.loads(error.read().decode("utf-8"))
        return str(payload.get("error", {}).get("message") or payload)[:400]
    except Exception:  # noqa: BLE001 - best-effort error text
        return error.reason if isinstance(error.reason, str) else str(error.reason)
    finally:
        error.close()


def _retry_after(error: urllib.error.HTTPError) -> float | None:
    value = error.headers.get("Retry-After") if error.headers else None
    try:
        return float(value) if value else None
    except ValueError:
        return None


def self_test(api_key: str, model: str, base_url: str = "https://openrouter.ai") -> dict[str, Any]:
    """Ask one tiny yes/no question to prove the key, the credit and the model id work."""
    response = decide(
        api_key, model,
        {"message": "Hi, we would like to sponsor your next video. Our budget is $5,000."},
        {"is_paid_offer": {"type": "noul", "criteria": {"true": "A paid offer is made.", "false": "No paid offer."}}},
        base_url=base_url, attempts=1, timeout=30,
    )
    return {
        "model": str(response.get("model") or model),
        "probability": noul_probability(response["answers"].get("is_paid_offer")),
        "cost": (response.get("usage") or {}).get("cost"),
    }


# -- answer normalisation -----------------------------------------------------


def noul_probability(answer: dict[str, Any] | None) -> float | None:
    """Probability (0..1) that a yes/no question is true. None when refused or missing."""
    if not answer or answer.get("type") != "noul":
        return None
    value = answer.get("noul")
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return max(0.0, min(1.0, float(value)))
    return None


def score_fraction(answer: dict[str, Any] | None, levels: int) -> float | None:
    """Position on an ordered scale as 0..1 (0 = first criterion, 1 = last).

    Scales are 0-indexed: with 3 criteria the raw score runs 0..2. Prefer the
    per-level probabilities when present and fall back to the raw score.
    """
    if not answer or answer.get("type") != "score" or levels < 2:
        return None
    top = levels - 1
    probabilities = answer.get("probabilities")
    if isinstance(probabilities, dict) and probabilities:
        try:
            pairs = [(int(key), float(value)) for key, value in probabilities.items()]
            total = sum(weight for _, weight in pairs)
            if total > 0:
                expected = sum(index * weight for index, weight in pairs) / total
                return max(0.0, min(1.0, expected / top))
        except (TypeError, ValueError):
            pass
    raw = answer.get("score")
    if isinstance(raw, (int, float)):
        return max(0.0, min(1.0, float(raw) / top))
    return None


def choice_distribution(answer: dict[str, Any] | None) -> tuple[str, dict[str, float], float | None]:
    """(chosen label, label -> probability, confidence). Empty label when refused."""
    if not answer or answer.get("type") != "choice":
        return "", {}, None
    probabilities = {
        str(label): float(value)
        for label, value in (answer.get("probabilities") or {}).items()
        if isinstance(value, (int, float))
    }
    chosen = str(answer.get("choice") or "")
    if not chosen and probabilities:
        chosen = max(probabilities, key=probabilities.get)
    confidence = answer.get("confidence")
    if not isinstance(confidence, (int, float)):
        confidence = probabilities.get(chosen)
    return chosen, probabilities, (float(confidence) if confidence is not None else None)
