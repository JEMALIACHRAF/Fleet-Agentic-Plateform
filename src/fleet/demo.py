"""
================================================================================
 FILE: demo.py  —  one-command tour of the platform (python -m fleet.demo)
================================================================================

Exercises the core layers end-to-end. Uses the LiteLLM router with `mock=` when no
API keys are set, so it runs offline but on the SAME code path as production (there
is no fake provider). Set GOOGLE_API_KEY to see real Gemini calls (drop the mock).
"""
from __future__ import annotations

import json

from .agents.guardrails import ConcurrencyGuard
from .config import get_settings
from .llm.router import build_router
from .memory.store import MemoryStore
from .observability.tracing import setup_tracing
from .orchestration.pipeline import Mode, build_pipeline

MOCK = json.dumps({"vehicle_id": "VToomey-01",
                   "anomalies": [{"ts": "08:10", "type": "overheating", "detail": "118C"}],
                   "severity": "high"})


def main():
    s = get_settings()
    print(f"tracing mode: {setup_tracing(s).mode}")

    router = build_router(s)
    mock = None if router.live else MOCK
    if not router.live:
        print("(no API keys -> production code path with mock_response)")

    mem = MemoryStore()
    pipe = build_pipeline("fleet_health",
                          ["anomaly_detector", "maintenance_advisor", "fleet_reporter"],
                          Mode.SEQUENTIAL, settings=s, memory=mem, router=router, mock=mock)
    print("\n== Sequential ==")
    for n in pipe.run("VToomey-01", "demo-seq").nodes:
        print(f"  {'ok ' if n.ok else 'FAIL'} {n.node}")

    print("\n== Guardrail (admission control) ==")
    blocked = build_pipeline("x", ["anomaly_detector"], Mode.SEQUENTIAL, settings=s,
                             concurrency=ConcurrencyGuard(0), router=router, mock=mock)
    rb = blocked.run("VToomey-01", "demo-block")
    print(f"  ok={rb.ok} -> {rb.nodes[0].failure_type}")
    print("\nDone.")


if __name__ == "__main__":
    main()
