"""Guardrails.

Two layers:
  1. Pure, testable guard *logic* (no ADK) — these are what unit tests hit.
  2. Thin ADK callback adapters that call the pure logic from
     BeforeAgentCallback / AfterAgentCallback hooks.

Guards implemented:
  - token budget   (before): abort a node if its assembled prompt would breach
                              the context window -> raises TokenLimitError (traced
                              as TOKEN_LIMIT_BREACH).
  - rate limit     (before): reject a run if too many pipelines are active.
  - output schema  (after):  validate the agent's output against a Pydantic model
                              -> NODE_FAILURE if invalid.
"""
from __future__ import annotations

import json
import threading

from ..observability.failures import (
    NodeError,
    TokenLimitError,
    check_token_budget,
)


# --- pure guard logic --------------------------------------------------------

class ConcurrencyGuard:
    """Thread-safe active-pipeline counter for the rate-limit guardrail."""

    def __init__(self, max_active: int):
        self._max = max_active
        self._active = 0
        self._lock = threading.Lock()

    @property
    def active(self) -> int:
        return self._active

    def try_acquire(self) -> bool:
        with self._lock:
            if self._active >= self._max:
                return False
            self._active += 1
            return True

    def release(self) -> None:
        with self._lock:
            self._active = max(0, self._active - 1)


def enforce_token_budget(prompt: str, limit_tokens: int) -> int:
    """Raise TokenLimitError if the prompt is too large. Returns token estimate."""
    return check_token_budget(prompt, limit_tokens=limit_tokens)


def validate_output_schema(output_text: str, schema_model) -> object:
    """Validate JSON text against a Pydantic model. Raises NodeError on failure."""
    try:
        payload = json.loads(output_text) if isinstance(output_text, str) else output_text
        return schema_model.model_validate(payload)
    except Exception as exc:
        raise NodeError(f"output failed schema {schema_model.__name__}: {exc}") from exc


# --- ADK callback adapters ---------------------------------------------------
# Signatures follow ADK's BeforeAgentCallback/AfterAgentCallback(callback_context).
# Imported lazily by the factory; kept here so all guard code lives together.

def make_before_callback(limit_tokens: int, concurrency: ConcurrencyGuard | None = None):
    def _before(callback_context):  # CallbackContext
        state = getattr(callback_context, "state", {}) or {}
        # Best-effort prompt size estimate from state we control.
        assembled = state.get("rendered_context", "")
        if assembled:
            enforce_token_budget(assembled, limit_tokens)
        return None  # None = proceed; returning Content would short-circuit the agent
    return _before


def make_after_callback(schema_model):
    def _after(callback_context):  # CallbackContext
        state = getattr(callback_context, "state", {}) or {}
        out = state.get("last_output")
        if out is not None and schema_model is not None:
            validate_output_schema(out, schema_model)
        return None
    return _after
