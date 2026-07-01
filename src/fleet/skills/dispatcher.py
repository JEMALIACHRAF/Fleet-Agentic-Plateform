"""
================================================================================
 FILE: skills/dispatcher.py  —  dynamic skill selection (Anthropic-style)
================================================================================

WHAT THIS DOES (the "progressive disclosure" pattern)
-----------------------------------------------------
Anthropic's Agent Skills don't load every skill's full instructions up front. At
startup only each skill's NAME + DESCRIPTION sit in context (~30-50 tokens each).
When a task arrives, the agent matches it against those descriptions, then reads
the FULL SKILL.md only for the chosen skill(s).

This dispatcher mirrors that:
  1. Load only the manifests (name + description) — cheap.
  2. Ask the `fast` model which skill(s) the task matches (selection).
  3. Load the FULL skill(s) for the winners and run them as a pipeline.

WHY THIS MATTERS (interview)
----------------------------
- Scales to many skills without bloating context (the context window is a budget).
- Adding a use case = a new SKILL.md whose DESCRIPTION is its trigger condition. No
  router code change: the description IS the routing logic.
- Honest gap vs. mine before this: I used to load ALL skills eagerly; this adds the
  on-demand activation Anthropic uses.

INTERVIEW ANCHOR
----------------
Q: "How does the system pick the right skill?"
A: "Each skill's description is its trigger. At startup I load only name+description.
   A cheap model matches the task to descriptions, then I load the full instructions
   only for the chosen skills — progressive disclosure, the Anthropic pattern."
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from ..config import Settings, get_settings
from ..llm.router import LLMRouter, build_router
from ..memory.store import MemoryStore
from .loader import SkillManifest, load_skill_manifests


@dataclass
class DispatchResult:
    task: str
    selected: list[str]          # skill names the dispatcher chose
    pipeline_result: object      # PipelineResult from running them


class SkillDispatcher:
    """Selects skills by description, then runs them. Holds only manifests in memory."""

    def __init__(self, settings: Settings | None = None, router: LLMRouter | None = None):
        self.s = settings or get_settings()
        self.router = router or build_router(self.s)
        self.manifests: list[SkillManifest] = load_skill_manifests(self.s)  # cheap, name+desc only

    def _selection_prompt(self, task: str) -> str:
        catalog = "\n".join(f"- {m.name}: {m.description}" for m in self.manifests)
        return (
            "You are a skill router. Given a TASK and a CATALOG of skills (name: "
            "description), return the ordered list of skill names whose description "
            "matches the task. Return ONLY a JSON array of names, e.g. [\"a\",\"b\"].\n\n"
            f"CATALOG:\n{catalog}\n\nTASK: {task}"
        )

    def select(self, task: str, *, mock: str | None = None) -> list[str]:
        """Ask the fast model which skills match the task (progressive disclosure step 2)."""
        out = self.router.complete("fast", self._selection_prompt(task), mock=mock).text
        names = _parse_name_list(out)
        valid = {m.name for m in self.manifests}
        return [n for n in names if n in valid]      # drop anything hallucinated

    def dispatch(self, task: str, entity_id: str, session_id: str = "default", *,
                 mode=None, select_mock: str | None = None,
                 run_mock: str | None = None) -> DispatchResult:
        """End-to-end: select skills for the task, then load+run only those."""
        from ..orchestration.pipeline import Mode, build_pipeline  # lazy: avoids circular import
        mode = mode or Mode.SEQUENTIAL
        selected = self.select(task, mock=select_mock)
        if not selected:
            return DispatchResult(task, [], None)
        pipe = build_pipeline("dispatch", selected, mode, settings=self.s,
                              memory=MemoryStore(), router=self.router, mock=run_mock)
        result = pipe.run(entity_id, session_id)     # full skills loaded here, not before
        return DispatchResult(task, selected, result)


def _parse_name_list(text: str) -> list[str]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```", 2)[1].removeprefix("json").strip()
    try:
        data = json.loads(cleaned)
        return [str(x) for x in data] if isinstance(data, list) else []
    except Exception:
        return []
