"""Prometheus metrics — the second observability signal alongside Langfuse traces.

Design: traces and metrics are emitted from the SAME instrumentation point
(`observability.failures.traced_node`), so for every node you get BOTH a Langfuse
span (for fine-grained debugging of one run) AND Prometheus counters/histograms
(for aggregate monitoring across all runs). They never drift apart because they're
recorded together.

  span   -> OpenTelemetry -> Langfuse   (why did THIS run's node fail?)
  metric -> prometheus_client -> Prometheus -> Grafana   (is the node failure RATE rising?)

Degrades to no-op if prometheus_client isn't installed, so the platform still runs.
"""
from __future__ import annotations

_ENABLED = True
try:
    from prometheus_client import (
        CONTENT_TYPE_LATEST,
        Counter,
        Gauge,
        Histogram,
        generate_latest,
    )
except Exception:  # prometheus_client not installed
    _ENABLED = False


if _ENABLED:
    CONTENT_TYPE = CONTENT_TYPE_LATEST

    # --- pipeline level (whole graph) ---
    PIPELINE_RUNS = Counter("fleet_pipeline_runs_total", "Pipeline runs", ["pipeline", "status"])
    PIPELINE_DURATION = Histogram("fleet_pipeline_duration_seconds", "Pipeline duration", ["pipeline"])
    PIPELINE_REJECTED = Counter("fleet_pipeline_rejected_total", "Pipelines rejected at admission", ["reason"])
    ACTIVE = Gauge("fleet_active_pipelines", "Currently active pipelines")

    # --- node level (per agent / per tool) — the traceability bit ---
    NODE_RUNS = Counter("fleet_node_runs_total", "Node executions", ["node", "graph", "status"])
    NODE_DURATION = Histogram("fleet_node_duration_seconds", "Node duration", ["node", "graph"])
    NODE_FAILURES = Counter("fleet_node_failures_total", "Node failures by type", ["node", "graph", "type"])

    # --- HTTP level (the ADK server middleware feeds these) ---
    # The ADK server runs the agents and traces to Langfuse natively, but it does NOT
    # emit Prometheus metrics. So a middleware on the ADK app records request rate and
    # latency here, and /metrics exposes them. That's how we get BOTH signals from one server.
    HTTP_REQUESTS = Counter("fleet_http_requests_total", "HTTP requests", ["method", "path", "status"])
    HTTP_LATENCY = Histogram("fleet_http_request_seconds", "HTTP request latency", ["method", "path"])

    def record_http(method: str, path: str, status: int, duration_s: float) -> None:
        HTTP_REQUESTS.labels(method=method, path=path, status=str(status)).inc()
        HTTP_LATENCY.labels(method=method, path=path).observe(duration_s)

    def record_node(node: str, graph: str, status: str, duration_s: float,
                    failure_type: str | None = None) -> None:
        NODE_RUNS.labels(node=node, graph=graph, status=status).inc()
        NODE_DURATION.labels(node=node, graph=graph).observe(duration_s)
        if failure_type:
            NODE_FAILURES.labels(node=node, graph=graph, type=failure_type).inc()

    def render() -> bytes:
        return generate_latest()

else:
    CONTENT_TYPE = "text/plain"

    class _Noop:
        def labels(self, *a, **k): return self
        def inc(self, *a, **k): return None
        def observe(self, *a, **k): return None
        def set(self, *a, **k): return None
        def time(self):
            class _Ctx:
                def __enter__(self_): return None
                def __exit__(self_, *a): return False
            return _Ctx()

    PIPELINE_RUNS = PIPELINE_DURATION = PIPELINE_REJECTED = ACTIVE = _Noop()
    NODE_RUNS = NODE_DURATION = NODE_FAILURES = _Noop()
    HTTP_REQUESTS = HTTP_LATENCY = _Noop()

    def record_http(*a, **k) -> None:
        return None

    def record_node(*a, **k) -> None:
        return None

    def render() -> bytes:
        return b"# prometheus_client not installed\n"
