"""
================================================================================
 FILE: orchestration/langgraph_pipeline.py  —  LangGraph engine (replaces pipeline.py)
================================================================================

Drop-in replacement for the in-house Pipeline, built on LangGraph's StateGraph.
Same inputs (skill names + entity_id), same shared bricks (loader, tools, router,
schemas). Only the ORCHESTRATION engine changes.

MAPPING vs the hand-rolled pipeline:
  Pipeline.run()              -> graph.invoke(state)
  state dict + <node>_output  -> TypedDict state, reducer merges outputs
  sequential fail-fast        -> linear edges START->n1->n2->...->END
  parallel + dict(state) copy -> fan-out edges (LangGraph isolates branch state,
                                 merges via the Annotated reducers)
  _safe_node (failure->data)  -> each node traps its own exception -> NodeResult
  classify_exception          -> reused unchanged

TRACING / METRICS:
  LangGraph auto-traces to Langfuse via the LangChain CallbackHandler (passed at
  invoke time), so traced_node is NOT needed for traces. We still record the
  Prometheus per-node failure metric explicitly — no framework emits that for us.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Annotated, TypedDict

from langgraph.graph import StateGraph, START, END

from ..config import Settings, get_settings
from ..llm.router import LLMRouter, build_router
from ..observability import metrics as M
from ..observability.failures import classify_exception
from ..skills.loader import Skill, load_all_skills
from ..tools.registry import ToolRegistry, default_registry
from .llm_node import run_skill


class Mode(str, Enum):
    SEQUENTIAL = "sequential"
    PARALLEL = "parallel"


@dataclass
class NodeResult:
    node: str
    ok: bool
    output: dict | None = None
    error: str | None = None
    failure_type: str | None = None


def _merge_dicts(a: dict, b: dict) -> dict:
    out = dict(a)
    out.update(b)
    return out


def _append(a: list, b: list) -> list:
    return (a or []) + (b or [])


class GraphState(TypedDict, total=False):
    """Shared state. The Annotated reducers tell LangGraph how to MERGE state
    returning from parallel branches — the equivalent of dict(state) copy + merge
    in the hand-rolled parallel path."""
    entity_id: str
    vehicle_id: str
    session_id: str
    outputs: Annotated[dict, _merge_dicts]      # {"<skill>_output": {...}}
    results: Annotated[list, _append]           # list[NodeResult dicts]


def _make_node(skill: Skill, registry: ToolRegistry, router: LLMRouter, mock: str | None):
    """Wrap one skill as a LangGraph node: state -> partial state update.
    Mirrors Pipeline._safe_node — a failure becomes DATA, never crashes the graph."""
    def node(state: GraphState) -> dict:
        flat = dict(state)
        flat.update(state.get("outputs", {}))       # expose upstream <node>_output
        try:
            output = run_skill(skill, registry, router, flat, mock=mock)   # REAL model call
            nr = NodeResult(node=skill.name, ok=True, output=output)
            return {"outputs": {f"{skill.name}_output": output},
                    "results": [nr.__dict__]}
        except Exception as exc:
            ftype = classify_exception(exc)
            M.NODE_FAILURES.labels(node=skill.name, graph="langgraph", type=ftype).inc()
            nr = NodeResult(skill.name, ok=False, error=str(exc), failure_type=ftype)
            return {"results": [nr.__dict__]}
    return node


def build_graph(name: str, skill_names: list[str], mode: Mode = Mode.SEQUENTIAL, *,
                settings: Settings | None = None, router: LLMRouter | None = None,
                mock: str | None = None):
    """Assemble a compiled LangGraph from skill names. Same spirit as build_pipeline."""
    s = settings or get_settings()
    registry = default_registry(s)
    all_skills = load_all_skills(s, registry)
    missing = [n for n in skill_names if n not in all_skills]
    if missing:
        raise ValueError(f"unknown skills: {missing}")
    skills = [all_skills[n] for n in skill_names]
    router = router or build_router(s)

    g = StateGraph(GraphState)
    names = [sk.name for sk in skills]
    for sk in skills:
        g.add_node(sk.name, _make_node(sk, registry, router, mock))

    if mode == Mode.PARALLEL:
        for n in names:                       # fan-out: all run concurrently
            g.add_edge(START, n)
            g.add_edge(n, END)
    else:
        g.add_edge(START, names[0])           # sequential chain
        for a, b in zip(names, names[1:]):
            g.add_edge(a, b)
        g.add_edge(names[-1], END)

    return g.compile()


def run_graph(name: str, skill_names: list[str], entity_id: str, session_id: str,
              mode: Mode = Mode.SEQUENTIAL, *, settings: Settings | None = None,
              router: LLMRouter | None = None, mock: str | None = None,
              langfuse_handler=None) -> dict:
    """Invoke the compiled graph. Pass langfuse_handler in config for auto-tracing."""
    graph = build_graph(name, skill_names, mode, settings=settings, router=router, mock=mock)
    init: GraphState = {"entity_id": entity_id, "vehicle_id": entity_id,
                        "session_id": session_id, "outputs": {}, "results": []}
    config = {"callbacks": [langfuse_handler]} if langfuse_handler else {}
    return graph.invoke(init, config=config)