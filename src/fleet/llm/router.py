"""
================================================================================
 FILE: llm/router.py  —  the model router (delegated to LiteLLM)
================================================================================

WHY THIS FILE EXISTS / DESIGN CHOICE
------------------------------------
Enterprises DON'T hand-roll model routing. Retry, fallback, rate-limit handling
and multi-provider load-balancing are solved problems. We delegate them to
LiteLLM's Router. This single thin wrapper replaces an entire custom
providers/ package (base + gemini + openai + mock + a scoring router).

WHAT LiteLLM Router GIVES US (for free, battle-tested):
  - num_retries        : retry transient errors (429 rate-limit, 503 unavailable)
  - fallbacks          : if the "fast" group fails, automatically try "deep"
                         (and across providers: Gemini down -> OpenAI)
  - load-balancing     : spread calls across multiple deployments of a group
  - one call site      : router.complete("fast", messages) — caller never knows
                         which provider answered.




"""
from __future__ import annotations

from dataclasses import dataclass

from ..config import Settings, get_settings


@dataclass
class LLMResult:
    """Normalized result so callers never touch provider-specific shapes."""
    text: str
    model: str            # the concrete model that actually answered
    input_tokens: int
    output_tokens: int


class LLMRouter:
    """Thin wrapper around litellm.Router. Build once, reuse everywhere."""

    def __init__(self, settings: Settings | None = None):
        self.s = settings or get_settings()
        model_list = self.s.build_model_list()
        # `live` = real providers configured. With no keys we still build a router
        # (with a placeholder deployment) so mock_response calls work in tests/CI;
        # a REAL call without keys raises a clear error in complete().
        self.live = bool(model_list)
        if not model_list:
            model_list = [{"model_name": g, "litellm_params":
                           {"model": "gemini/gemini-2.5-flash", "api_key": "PLACEHOLDER"}}
                          for g in ("fast", "deep")]
        from litellm import Router  # lazy import: package imports fine without litellm
        self._router = Router(
            model_list=model_list,
            # If the chosen group fails entirely, try the other group (provider switch).
            fallbacks=[{"fast": ["deep"]}, {"deep": ["fast"]}],
            num_retries=self.s.llm_num_retries,
            timeout=self.s.llm_timeout_seconds,
        )

    def complete(self, model_group: str, prompt: str, *, mock: str | None = None) -> LLMResult:
        """Run one completion against a logical group ("fast"/"deep").

        `mock` (test only) short-circuits the network but keeps the same code path.
        """
        if mock is None and not self.live:
            raise RuntimeError(
                "No LLM providers configured. Set GOOGLE_API_KEY and/or OPENAI_API_KEY "
                "(or pass mock= for tests)."
            )
        kwargs = {}
        if mock is not None:
            kwargs["mock_response"] = mock
        resp = self._router.completion(
            model=model_group,
            messages=[{"role": "user", "content": prompt}],
            **kwargs,
        )
        usage = getattr(resp, "usage", None)
        return LLMResult(
            text=resp.choices[0].message.content or "",
            model=getattr(resp, "model", model_group),
            input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            output_tokens=getattr(usage, "completion_tokens", 0) or 0,
        )


def build_router(settings: Settings | None = None) -> LLMRouter:
    return LLMRouter(settings)
