"""
================================================================================
 FILE: orchestration/llm_node.py  —  run ONE skill as a REAL LLM call
================================================================================

Engine-agnostic: called by BOTH the hand-rolled pipeline AND the LangGraph engine.
Turns a Skill into one real model call: prefetch its tools' data, build the prompt,
call the LiteLLM router (retry + fallback), parse the JSON. Nothing here depends on
which orchestrator invokes it — that's why swapping pipeline.py for LangGraph leaves
this file untouched.
"""
from __future__ import annotations

import json

from ..llm.router import LLMRouter
from ..skills.loader import Skill
from ..tools.registry import ToolRegistry


def _prefetch_tools(skill: Skill, registry: ToolRegistry, entity_id: str) -> dict:
    data: dict = {}
    for tool_name in skill.tools:
        fn = registry.get(tool_name)
        result = fn(entity_id)
        data[tool_name] = result.model_dump(mode="json") if hasattr(result, "model_dump") else result
    return data


def build_prompt(skill: Skill, tool_data: dict, state: dict) -> str:
    parts = [skill.instruction]
    if tool_data:
        parts.append("TOOL DATA:\n" + json.dumps(tool_data, default=str))
    upstream = {k: v for k, v in state.items() if k.endswith("_output")}
    if upstream:
        parts.append("UPSTREAM RESULTS:\n" + json.dumps(upstream, default=str))
    entity = state.get("entity_id") or state.get("vehicle_id")
    parts.append(f"ENTITY: {entity}\nReturn ONLY the JSON described above.")
    return "\n\n".join(parts)


def run_skill(skill: Skill, registry: ToolRegistry, router: LLMRouter,
              state: dict, *, mock: str | None = None) -> dict:
    entity_id = state.get("entity_id") or state["vehicle_id"]
    tool_data = _prefetch_tools(skill, registry, entity_id)
    prompt = build_prompt(skill, tool_data, state)
    result = router.complete(skill.model, prompt, mock=mock)
    return _parse_json(result.text, entity_id)


def _parse_json(text: str, vehicle_id: str) -> dict:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```", 2)[1]
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
    cleaned = cleaned.strip()
    try:
        return json.loads(cleaned)
    except Exception:
        return {"vehicle_id": vehicle_id, "_raw": text, "_unparsed": True}