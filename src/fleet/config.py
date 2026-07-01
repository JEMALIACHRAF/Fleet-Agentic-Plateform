"""
================================================================================
 FILE: config.py  —  central configuration + LiteLLM model groups
================================================================================

WHAT THIS FILE DOES
-------------------
One place for every setting (API keys, model groups, reliability budgets,
observability endpoints). Everything is read from environment / .env so the SAME
code runs locally and in production without changes.

KEY DESIGN CHOICE — WHY MODEL *GROUPS* INSTEAD OF HARD-CODED MODELS
------------------------------------------------------------------
A skill never names a concrete model. It names a logical GROUP: "fast" or "deep".
  - "fast"  = cheap, low-latency tier  (Gemini Flash, fallback OpenAI mini)
  - "deep"  = high-quality tier        (Gemini Pro,   fallback GPT-4o)
The mapping group -> real deployments lives here in build_model_list() and is
consumed by the LiteLLM Router (llm/router.py). Enterprise pattern: the
cost/quality TRADEOFF is which group a skill picks; swapping providers is a
config change, not a code change.

INTERVIEW ANCHOR
----------------
Q: "How do you choose which model an agent uses?"
A: "Each skill declares a logical group (fast/deep). The group maps to real
   deployments here. LiteLLM Router picks one, retries, and falls back across
   providers. We don't hand-roll model selection - we delegate it to LiteLLM."
"""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # extra="ignore" so unrelated env vars never crash startup.
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore", populate_by_name=True)

    # --- Credentials. At least one is required to run for real. ---------------
    google_api_key: str | None = Field(default=None, alias="GOOGLE_API_KEY")
    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")

    # --- Model groups: which concrete model backs each logical tier. ----------
    gemini_fast_model: str = "gemini/gemini-2.5-flash"
    gemini_deep_model: str = "gemini/gemini-2.5-flash"   # -> gemini-2.5-pro when billing is on
    openai_fast_model: str = "openai/gpt-4o-mini"
    openai_deep_model: str = "openai/gpt-4o"

    # --- Reliability budgets (read by LiteLLM Router + guardrails). -----------
    llm_num_retries: int = 2            # retry transient errors (429/503) before giving up
    llm_timeout_seconds: float = 30.0   # per-LLM-call wall clock
    max_prompt_tokens: int = 120_000    # token-budget guardrail
    max_concurrent_pipelines: int = 8   # rate-limit guardrail
    tool_timeout_seconds: float = 8.0   # per-tool wall clock

    # --- Observability --------------------------------------------------------
    langfuse_host: str = "http://localhost:3000"
    langfuse_public_key: str | None = Field(default=None, alias="LANGFUSE_PUBLIC_KEY")
    langfuse_secret_key: str | None = Field(default=None, alias="LANGFUSE_SECRET_KEY")
    otel_service_name: str = "fleet-agent-platform"
    tracing_enabled: bool = True

    # --- Data layer -----------------------------------------------------------
    bigquery_project: str | None = None
    bigquery_dataset: str = "demo"
    bigquery_table: str = "vehicle_tracker"
    csv_fallback_path: str = "data/vehicle_tracker_sample.csv"

    skills_dir: str = "src/fleet/skills/builtin"

    def build_model_list(self) -> list[dict]:
        """group -> real deployment(s). Several entries with the same model_name
        give the Router load-balancing AND cross-provider fallback for free.
        A missing key simply drops that provider's deployments."""
        ml: list[dict] = []
        if self.google_api_key:
            ml += [
                {"model_name": "fast", "litellm_params": {"model": self.gemini_fast_model, "api_key": self.google_api_key}},
                {"model_name": "deep", "litellm_params": {"model": self.gemini_deep_model, "api_key": self.google_api_key}},
            ]
        if self.openai_api_key:
            ml += [
                {"model_name": "fast", "litellm_params": {"model": self.openai_fast_model, "api_key": self.openai_api_key}},
                {"model_name": "deep", "litellm_params": {"model": self.openai_deep_model, "api_key": self.openai_api_key}},
            ]
        return ml


@lru_cache
def get_settings() -> Settings:
    """Cached singleton: parse env once."""
    return Settings()
