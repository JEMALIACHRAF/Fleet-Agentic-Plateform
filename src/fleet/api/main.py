"""
================================================================================
 FILE: api/main.py  —  FastAPI HTTP layer over the platform
================================================================================

WHAT THIS FILE DOES
-------------------
Thin HTTP surface. The endpoints prove the platform's claims:
  GET  /health         -> liveness + which tracing backend is active
  GET  /skills         -> the discovered skills (proves "add a skill, no code")
  POST /pipelines/run  -> run a sequential/parallel pipeline over a vehicle (REAL LLM)
  GET  /metrics        -> Prometheus exposition (the SECOND observability signal)

WHY METRICS ARE EXPOSED HERE (and traces are not)
-------------------------------------------------
Prometheus is PULL-based: it scrapes GET /metrics every few seconds. That's why the
metrics endpoint lives on the API. Traces are PUSH-based (the app exports spans to
Langfuse over OTLP) and so don't need an endpoint. Two signals, two transport
models, by design.

INTERVIEW ANCHOR
----------------
Q: "Where do Grafana's numbers come from?"  ->  "Prometheus scrapes GET /metrics on
   this API. The per-node counters/histograms are emitted inside traced_node; this
   endpoint just renders them."
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException  #FastAPI : permet de créer l'API.HTTPException : permet de retourner des erreurs HTTP (400, 503, etc.).
from pydantic import BaseModel   #BaseModel : permet de définir le schéma des requêtes entrantes.

from ..agents.guardrails import ConcurrencyGuard  #ConcurrencyGuard → limite le nombre de pipelines exécutés simultanément.
from ..config import get_settings
from ..memory.store import MemoryStore # 
from ..observability import metrics as M
from ..observability.tracing import setup_tracing
from ..orchestration.pipeline import Mode, build_pipeline
from ..skills.loader import load_all_skills
from ..tools.registry import default_registry


class RunRequest(BaseModel):
    pipeline: str = "fleet_health"
    vehicle_id: str
    session_id: str = "default"
    skills: list[str] | None = None
    mode: Mode = Mode.SEQUENTIAL


_settings = get_settings()
_memory = MemoryStore()
_concurrency = ConcurrencyGuard(_settings.max_concurrent_pipelines)
_state: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    _state["tracing"] = setup_tracing(_settings)   # init OTel -> Langfuse on startup
    yield


app = FastAPI(title="Fleet Agent Platform", version="0.2.0", lifespan=lifespan)


@app.get("/health")
def health():
    tr = _state.get("tracing")
    return {"status": "ok", "tracing_mode": getattr(tr, "mode", "uninitialized")}


@app.get("/skills")
def skills():
    """List discovered skills — the live proof of the 'a skill is a file' claim."""
    registry = default_registry(_settings)
    loaded = load_all_skills(_settings, registry)
    return {
        "count": len(loaded),
        "skills": [
            {"name": s.name, "display_name": s.display_name, "model": s.model,
             "tools": s.tools, "schema": s.output_schema}
            for s in loaded.values()
        ],
    }


@app.post("/pipelines/run")
def run_pipeline(req: RunRequest):
    """Run a pipeline. Each node is a REAL LLM call (LiteLLM router, retry+fallback)."""
    skill_names = req.skills or ["anomaly_detector", "maintenance_advisor", "fleet_reporter"]
    try:
        pipe = build_pipeline(req.pipeline, skill_names, req.mode,
                              settings=_settings, memory=_memory, concurrency=_concurrency)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:                       # no API keys configured
        raise HTTPException(status_code=503, detail=str(exc))

    M.ACTIVE.set(_concurrency.active)
    with M.PIPELINE_DURATION.labels(pipeline=req.pipeline).time():
        result = pipe.run(req.vehicle_id, req.session_id)
    M.PIPELINE_RUNS.labels(pipeline=req.pipeline, status="ok" if result.ok else "fail").inc()
    for n in result.nodes:
        if not n.ok and n.failure_type == "rate_limited":
            M.PIPELINE_REJECTED.labels(reason="rate_limited").inc()

    return {
        "pipeline": result.pipeline, "ok": result.ok, "session_id": result.session_id,
        "nodes": [{"node": n.node, "ok": n.ok, "failure_type": n.failure_type,
                   "error": n.error, "output": n.output} for n in result.nodes],
        "final_state": {k: v for k, v in result.state.items() if k.endswith("_output")},
    }




class DispatchRequest(BaseModel):
    task: str                       # natural-language task; its match to skill descriptions selects skills
    entity_id: str                  # vehicle id or ticket id
    session_id: str = "default"


@app.post("/dispatch")
def dispatch(req: DispatchRequest):
    """Anthropic-style dynamic activation: pick skills by description, then run them."""
    from ..skills.dispatcher import SkillDispatcher
    try:
        d = SkillDispatcher(_settings)
        res = d.dispatch(req.task, req.entity_id, req.session_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    if not res.selected:
        return {"task": req.task, "selected": [], "detail": "no skill matched the task"}
    r = res.pipeline_result
    return {"task": req.task, "selected": res.selected, "ok": r.ok,
            "nodes": [{"node": n.node, "ok": n.ok, "output": n.output} for n in r.nodes]}

@app.get("/metrics")
def prometheus_metrics():
    from fastapi.responses import Response
    return Response(M.render(), media_type=M.CONTENT_TYPE)
