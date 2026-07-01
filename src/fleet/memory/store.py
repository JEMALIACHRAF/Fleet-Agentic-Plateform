"""Intelligent memory management.

ADK gives you InMemorySessionService (per-session state) and a MemoryService
(cross-session recall). Those are storage. The *intelligence* — deciding what to
keep in the prompt when context is finite — is what this module adds, and it's
the thing that actually prevents the "token limit breach" failure in production.

Design:
  - WorkingMemory holds an ordered list of turns + a rolling summary + pinned facts.
  - Before each LLM call, `render(budget_tokens)` returns a context string that
    fits the budget by (1) always keeping pinned facts, (2) keeping the rolling
    summary, (3) keeping the most recent turns, and (4) summarizing/evicting the
    oldest turns when over budget.
  - Token counting is pluggable; default is a cheap char/4 heuristic so it runs
    without tiktoken. Swap in a real tokenizer in production.

All pure Python, fully unit-testable, no LLM required (summarizer is injectable).
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol


def approx_tokens(text: str) -> int:
    """Cheap, dependency-free token estimate. Good enough for budgeting."""
    return max(1, len(text) // 4)


class Summarizer(Protocol):
    def __call__(self, text: str) -> str: ...


def _truncating_summarizer(text: str, target_chars: int = 240) -> str:
    """Fallback summarizer with no LLM: keep head, mark elision. Deterministic."""
    text = " ".join(text.split())
    if len(text) <= target_chars:
        return text
    return text[:target_chars].rsplit(" ", 1)[0] + " […]"


@dataclass
class Turn:
    role: str          # "user" | "assistant" | "tool"
    content: str
    tokens: int = 0

    def __post_init__(self):
        if not self.tokens:
            self.tokens = approx_tokens(self.content)


@dataclass
class WorkingMemory:
    """Per-session working memory with budget-aware rendering."""
    token_counter: Callable[[str], int] = approx_tokens
    summarizer: Summarizer = field(default=_truncating_summarizer)
    pinned_facts: list[str] = field(default_factory=list)
    rolling_summary: str = ""
    turns: list[Turn] = field(default_factory=list)
    # Keep at least this many recent turns verbatim, never summarized.
    keep_recent: int = 4

    # --- write API ------------------------------------------------------------
    def add_turn(self, role: str, content: str) -> None:
        self.turns.append(Turn(role, content, self.token_counter(content)))

    def pin_fact(self, fact: str) -> None:
        """Pin a durable fact (e.g. 'fleet has 42 vehicles', user prefs). Never evicted."""
        if fact not in self.pinned_facts:
            self.pinned_facts.append(fact)

    # --- maintenance ----------------------------------------------------------
    def _summarize_evicted(self, evicted: list[Turn]) -> None:
        if not evicted:
            return
        joined = "\n".join(f"{t.role}: {t.content}" for t in evicted)
        merged = (self.rolling_summary + "\n" + joined).strip()
        self.rolling_summary = self.summarizer(merged)

    def compact(self, budget_tokens: int) -> None:
        """Ensure rendered context fits budget by evicting+summarizing oldest turns."""
        # Always-present overhead: pinned facts + rolling summary.
        fixed = self._fixed_tokens()
        # Walk from oldest, summarizing into rolling_summary, until we fit or hit keep_recent.
        while self._turns_tokens() + fixed > budget_tokens and len(self.turns) > self.keep_recent:
            evicted = [self.turns.pop(0)]
            self._summarize_evicted(evicted)
            fixed = self._fixed_tokens()

    def _fixed_tokens(self) -> int:
        facts = self.token_counter("\n".join(self.pinned_facts)) if self.pinned_facts else 0
        summ = self.token_counter(self.rolling_summary) if self.rolling_summary else 0
        return facts + summ

    def _turns_tokens(self) -> int:
        return sum(t.tokens for t in self.turns)

    # --- read API -------------------------------------------------------------
    def render(self, budget_tokens: int) -> str:
        """Return a context string guaranteed to fit (best-effort) in budget_tokens."""
        self.compact(budget_tokens)
        blocks: list[str] = []
        if self.pinned_facts:
            blocks.append("FACTS:\n" + "\n".join(f"- {f}" for f in self.pinned_facts))
        if self.rolling_summary:
            blocks.append("SUMMARY OF EARLIER CONTEXT:\n" + self.rolling_summary)
        if self.turns:
            convo = "\n".join(f"{t.role}: {t.content}" for t in self.turns)
            blocks.append("RECENT TURNS:\n" + convo)
        return "\n\n".join(blocks)

    def estimated_tokens(self) -> int:
        return self.token_counter(self.render(10**9))  # render-all then count


class MemoryStore:
    """Holds WorkingMemory per session id. Mirrors ADK's session-keyed model."""

    def __init__(self, summarizer: Summarizer | None = None, keep_recent: int = 4):
        self._sessions: dict[str, WorkingMemory] = {}
        self._summarizer = summarizer or _truncating_summarizer
        self._keep_recent = keep_recent

    def get(self, session_id: str) -> WorkingMemory:
        if session_id not in self._sessions:
            self._sessions[session_id] = WorkingMemory(
                summarizer=self._summarizer, keep_recent=self._keep_recent
            )
        return self._sessions[session_id]

    def drop(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)
