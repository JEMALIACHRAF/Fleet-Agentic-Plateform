"""
PRODUCTION ADK AGENT — customer-ticket handling (the second use case).

Proves "add a use case = a file": same factory, same tracing, new skills.
classify -> respond. Discovered automatically by serving/main.py.
"""
from fleet.observability.tracing import setup_tracing
setup_tracing()

from google.adk import Workflow

from fleet.agents.factory import build_adk_agent
from fleet.config import get_settings
from fleet.skills.loader import load_all_skills
from fleet.tools.registry import default_registry

_s = get_settings()
_reg = default_registry(_s)
_skills = load_all_skills(_s, _reg)

classifier = build_adk_agent(_skills["ticket_classifier"], _reg, _s)
responder = build_adk_agent(_skills["ticket_responder"], _reg, _s)

root_agent = Workflow(
    name="ticket_support",
    edges=[("START", classifier, responder)],   # classify -> respond
)
