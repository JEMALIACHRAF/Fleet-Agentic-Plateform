"""
================================================================================
 FILE: serving/main.py  —  PRODUCTION server (ADK + Langfuse + Prometheus)
================================================================================

WHAT THIS IS
------------
The single production entry point. It uses ADK's own FastAPI server, which runs
the agents discovered in serving/agents/ and traces them to Langfuse via OTel.

We bolt on THREE things ADK's server doesn't give us:
  1. a Prometheus middleware that times every HTTP request,
  2. a GET /metrics endpoint Prometheus can scrape,
  3. a failure-typing span processor attached to ADK's OWN TracerProvider, so our
     8-category failure taxonomy (rate_limit, auth, validation...) feeds Prometheus
     even though ADK owns the spans.

=> ONE server, BOTH observability signals:
   - Langfuse  (traces)  : automatic, from ADK's native OTel spans.
   - Prometheus (metrics): HTTP middleware + typed failures + /metrics.

WHY WE ATTACH TO ADK'S PROVIDER (the OTel gotcha)
-------------------------------------------------
ADK installs its OWN global TracerProvider at import time, and OTel forbids
replacing it ("Overriding of current TracerProvider is not allowed"). So instead
of imposing our provider, we grab the ACTIVE provider (ADK's) and add our span
processor onto it — that's how we cohabit with a library that already owns OTel.

RUN
---
  uvicorn serving.main:app --port 8080
Then ADK endpoints are live (POST /run, /apps/.../sessions, etc.) and GET /metrics
exposes Prometheus. Point Prometheus at host:8080/metrics.

NOTE
----
ADK's get_fast_api_app signature varies a little by version. If an argument is
rejected on your ADK version, remove it (web=, trace_to_cloud=) — the agents_dir
argument is the stable one.
"""
from __future__ import annotations

import os
import time

# Configure OTel -> Langfuse before ADK imports build anything.
from fleet.observability.tracing import setup_tracing
setup_tracing()

from fleet.observability import metrics as M

AGENTS_DIR = os.path.join(os.path.dirname(__file__), "agents")


def _build_app():
    """Build the ADK FastAPI app (runs the agents in serving/agents/)."""
    from google.adk.cli.fast_api import get_fast_api_app
    try:
        return get_fast_api_app(agents_dir=AGENTS_DIR, web=True, trace_to_cloud=False)
    except TypeError:
        # older/newer ADK: fall back to the stable single-arg form
        return get_fast_api_app(agents_dir=AGENTS_DIR)


app = _build_app()


def _attach_failure_processor():
    """ADK has already installed its own global TracerProvider. We grab the ACTIVE
    provider and attach OUR failure-typing span processor to it, so error spans
    emitted by ADK get classified and counted in Prometheus.

    (Trying to set our own provider fails with 'Overriding of current
    TracerProvider is not allowed' — ADK wins the race at import time.)"""
    from opentelemetry import trace
    from fleet.observability.tracing import _FailureTypingSpanProcessor
    provider = trace.get_tracer_provider()
    if hasattr(provider, "add_span_processor"):
        provider.add_span_processor(_FailureTypingSpanProcessor())
        print("[FAILPROC] attached to active TracerProvider", flush=True)
    else:
        print(f"[FAILPROC] active provider {type(provider).__name__} "
              f"has no add_span_processor", flush=True)


_attach_failure_processor()


@app.middleware("http")         #Middleware means: execute code before and after EVERY HTTP request.
async def prometheus_middleware(request, call_next):
    """Record request rate + latency for every call, then expose via /metrics."""
    start = time.perf_counter()
    response = await call_next(request)
    M.record_http(request.method, request.url.path, response.status_code,
                  time.perf_counter() - start)
    return response


@app.get("/metrics")
def metrics():
    from fastapi.responses import Response
    return Response(M.render(), media_type=M.CONTENT_TYPE)