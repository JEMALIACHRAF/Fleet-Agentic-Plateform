"""
================================================================================
 FILE: serving/agents/coordinator/agent.py  —  LLM router (dynamic routing)
================================================================================

WHAT IT DOES
------------
A coordinator agent. ADK gives the coordinator's LLM the NAME + DESCRIPTION of each
sub-agent, and the LLM decides which one to TRANSFER to (transfer_to_agent), based
on the user's natural-language request. This is ADK's native dynamic routing.

So: type "a customer was double charged" -> it transfers to ticket_classifier.
    Type "is vehicle VToomey-01 overheating?" -> it transfers to anomaly_detector.

WHY THIS IS THE ROUTING (not progressive disclosure)
----------------------------------------------------
The coordinator routes by reading DESCRIPTIONS (cheap). It does NOT lazy-load
instructions from disk (the sub-agents are already built). Routing = this file;
token-saving progressive disclosure = the separate dispatcher.

ADDING A USE CASE
-----------------
Add a skill -> it becomes a sub-agent here automatically (the loop below builds one
agent per skill). No new agent.py per use case: the description does the routing.
"""
from fleet.observability.tracing import setup_tracing
setup_tracing()                      # OTel -> Langfuse, before agents are built

from google.adk import Agent         # Agent == LlmAgent in ADK

from fleet.agents.factory import build_adk_agent
from fleet.config import get_settings
from fleet.skills.loader import load_all_skills
from fleet.tools.registry import default_registry

_s = get_settings()
_reg = default_registry(_s)
_skills = load_all_skills(_s, _reg)

# Build ONE routable agent per skill. Each carries skill.description -> the LLM
# coordinator reads these descriptions to decide where to route.
_sub_agents = [build_adk_agent(sk, _reg, _s) for sk in _skills.values()]

# Concrete fast model for the router itself (ADK wants the bare name, no "gemini/").
_model = _s.gemini_fast_model if _s.google_api_key else _s.openai_fast_model
if _model.startswith("gemini/"):
    _model = _model.split("/", 1)[1]

root_agent = Agent(
    name="fleet_coordinator",
    model=_model,
    description="Routes a request to the specialist skill whose description matches it.",
    instruction=(
        "You are a router for a fleet-management platform. Read the user's request "
        "and TRANSFER to the single sub-agent whose description best matches the task. "
        "Do not answer the request yourself — always delegate. If a vehicle id like "
        "VToomey-01 is present, it's a fleet task; if a ticket id like TICKET-042 or a "
        "billing/cancellation complaint is present, it's a support task."
    ),
    sub_agents=_sub_agents,
)