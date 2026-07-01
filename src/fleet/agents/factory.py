"""
================================================================================
 FILE: agents/factory.py  —  turn a Skill into a real ADK agent (graph node)
================================================================================

WHAT THIS FILE DOES
-------------------
Bridges the declarative world (SKILL.md) and the ADK graph world. Given a Skill,
it picks the concrete model for the skill's group ("fast"/"deep") and builds an
ADK Agent whose:
  - instruction  = the SKILL.md body,
  - tools        = real ADK FunctionTools wrapping the declared tool functions,
  - model        = the concrete Gemini/OpenAI model for the group.

WHY THIS MATTERS — "add a skill, get a graph node"
--------------------------------------------------
The ADK example loops over skills and calls build_adk_agent() for each, then wires
them into a Workflow. So adding a SKILL.md literally adds a node to the graph.

ADK CONTEXT (interview quote, and where it lives)
-------------------------------------------------
"ADK's architecture is event-driven: agents, tools and callbacks communicate via
Event objects coordinated by a central Runner." -> the Runner is created in the
examples (Runner(agent=workflow, ...)); each Agent we build here becomes a node
whose internal LLM/tool calls ADK emits as child events/spans.

NOTE ON ADK VERSIONS
--------------------
A graph node must be a single-turn Agent — do NOT set mode="task" (that's for
sub-agents under a coordinator). ADK 2.x changed this between beta and GA; we
follow the GA behavior (plain Agent in the edges).
"""
from __future__ import annotations

from dataclasses import dataclass

from ..config import Settings, get_settings
from ..skills.loader import Skill
from ..tools.registry import ToolRegistry


@dataclass
class AgentBlueprint:
    """Plain, testable description of what an agent WOULD be (no ADK needed)."""
    skill_name: str
    model: str          # concrete model id chosen for the skill's group
    tools: list[str]
    instruction: str


def _concrete_model(skill: Skill, s: Settings) -> str:
    """Map the skill's logical group to a concrete model id, preferring Gemini."""
    if skill.model == "deep":
        return s.gemini_deep_model if s.google_api_key else s.openai_deep_model
    return s.gemini_fast_model if s.google_api_key else s.openai_fast_model


def plan_agent(skill: Skill, settings: Settings | None = None) -> AgentBlueprint:
    """Decide model + tools for a skill WITHOUT building ADK objects (unit-testable)."""
    s = settings or get_settings()
    return AgentBlueprint(
        skill_name=skill.name,
        model=_concrete_model(skill, s),
        tools=list(skill.tools),
        instruction=skill.instruction,
    )


def build_adk_agent(skill: Skill, registry: ToolRegistry, settings: Settings | None = None):
    """Build a real ADK Agent for a skill. Requires google-adk at call time."""
    from google.adk import Agent          # lazy: package imports fine without ADK
    from google.adk.tools import FunctionTool

    s = settings or get_settings()
    model = _concrete_model(skill, s)
    # ADK handles Gemini NATIVELY: it wants "gemini-2.5-flash", NOT "gemini/gemini-2.5-flash".
    # The "gemini/" prefix is the LiteLLM convention (used by the platform path), not ADK.
    if model.startswith("gemini/"):
        model = model.split("/", 1)[1]
    adk_tools = [FunctionTool(registry.get(name)) for name in skill.tools]
    return Agent(
        name=skill.name,
        model=model,                       # e.g. "gemini/gemini-2.5-flash"
        description=skill.description,
        instruction=skill.instruction,
        tools=adk_tools,
    )
