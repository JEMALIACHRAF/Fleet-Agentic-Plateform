"""
PRODUCTION ADK AGENT — fleet health graph (discovered by ADK's get_fast_api_app).

ADK's server (serving/main.py) scans serving/agents/ and turns each folder that
exposes a `root_agent` into an HTTP app. This file IS the production fleet agent:
a real Workflow graph built from the SKILL.md files, calling Gemini, traced to
Langfuse via OpenTelemetry.
"""
from fleet.observability.tracing import setup_tracing
setup_tracing()  # OTel -> Langfuse + GoogleADKInstrumentor, BEFORE building agents

from google.adk import Workflow
from google.adk.workflow import JoinNode

from fleet.agents.factory import build_adk_agent
from fleet.config import get_settings
from fleet.skills.loader import load_all_skills
from fleet.tools.registry import default_registry

_s = get_settings()
_reg = default_registry(_s)
_skills = load_all_skills(_s, _reg)

# Build one real ADK agent per fleet skill (single-turn nodes, no mode="task").
anomaly = build_adk_agent(_skills["anomaly_detector"], _reg, _s)
route = build_adk_agent(_skills["route_optimizer"], _reg, _s)
maintenance = build_adk_agent(_skills["maintenance_advisor"], _reg, _s)
reporter = build_adk_agent(_skills["fleet_reporter"], _reg, _s)
join = JoinNode(name="join")

root_agent = Workflow(
    name="fleet_health",
    edges=[
        ("START", anomaly, join),       # branch 1 (parallel)
        ("START", route, join),          # branch 2 (parallel)
        (join, maintenance, reporter),   # fan-in -> chain
    ],
)
