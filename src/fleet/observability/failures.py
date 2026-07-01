"""
================================================================================
 FILE: observability/failures.py  —  failure-aware, per-node tracing
================================================================================

WHAT THIS FILE DOES (and why it's THE file for the interview)
-------------------------------------------------------------
The case study is essentially: "when a node fails, can you point to the exact
node, what it received, and WHY it failed?" This file makes that first-class.

Two things live here:
  1. `traced_node(...)` : a context manager wrapping ONE node of the graph. It
     opens an OpenTelemetry span (-> Langfuse) AND records Prometheus node
     metrics from the SAME place, then tags the span with a structured
     `fleet.failure.type` on error.
  2. A FAILURE TAXONOMY + `classify_exception()` : turns a raw exception into a
     named category so you can filter Langfuse/Grafana by failure type and react
     differently per type (a rate_limit is retried; a node_failure is not).

WHY TWO SIGNALS FROM ONE PLACE
------------------------------
Traces (Langfuse) answer "why did THIS run's node fail?" (diagnosis).
Metrics (Prometheus) answer "is the failure RATE rising?" (monitoring).
Both are emitted inside traced_node so they can never drift apart.

WHY OpenTelemetry (not the Langfuse SDK)?
-----------------------------------------
ADK emits OTel spans NATIVELY, and the GoogleADKInstrumentor (see tracing.py)
captures them automatically. By staying on OTel we (a) get ADK's spans for free
and (b) stay portable — the same code can export to Jaeger/Datadog by changing
one endpoint. Langfuse just happens to consume OTel traces.

INTERVIEW ANCHORS
-----------------
- "Trace which node failed" -> traced_node sets fleet.node.name + fleet.failure.type.
- "What failure types?" -> the FailureType class below (8 categories).
- "rate_limit vs node_failure" -> classify_exception maps them; you retry the
  first, not the second.
"""
from __future__ import annotations

import json
import time
from contextlib import contextmanager
from dataclasses import dataclass

from opentelemetry.trace import Status, StatusCode

from . import metrics
from .tracing import get_tracer

FAILURE_ATTR = "fleet.failure.type"   # the span attribute every dashboard filters on


class FailureType:
    """The failure taxonomy. Each value reacts differently in production:
      - transient (retry/fallback):  RATE_LIMIT, PROVIDER_UNAVAILABLE, TIMEOUT
      - data problem (fix input):    CONTEXT, VALIDATION, TOKEN
      - config/security (page a human): AUTH
      - logic bug (don't retry):     NODE
    """
    NODE = "node_failure"                  # the node's own logic raised / produced garbage
    CONTEXT = "context_failure"            # a tool/retrieval returned empty or malformed data
    TOKEN = "token_limit_breach"           # prompt exceeded the model context window
    RATE_LIMIT = "rate_limit"              # 429 — provider quota/RPM exceeded (RETRYABLE)
    PROVIDER_UNAVAILABLE = "provider_unavailable"  # 503 — model overloaded (RETRYABLE)
    TIMEOUT = "timeout"                    # the call took too long (RETRYABLE)
    VALIDATION = "validation_failure"      # LLM output failed its output schema
    AUTH = "auth_failure"                  # bad/missing API key, 401/403 (NOT retryable)


# --- exceptions we raise ourselves -------------------------------------------
class NodeError(RuntimeError): ...                 # -> NODE
class ContextRetrievalError(RuntimeError): ...     # -> CONTEXT
class TokenLimitError(RuntimeError): ...           # -> TOKEN
class OutputValidationError(RuntimeError): ...     # -> VALIDATION


def classify_failure(name: str, message: str) -> str:
    """Classify a failure from its exception class NAME + MESSAGE (strings only).

    Used both by classify_exception (below) AND by the ADK span processor in
    tracing.py, which only has the recorded exception's type/message strings — not
    a live exception object. One source of truth for the taxonomy."""
    name = (name or "").lower()
    msg = (message or "").lower()
    if "ratelimit" in name or "429" in msg or "resource_exhausted" in msg:
        return FailureType.RATE_LIMIT
    if "timeout" in name or "timeout" in msg:
        return FailureType.TIMEOUT
    if "503" in msg or "unavailable" in name or "overloaded" in msg:
        return FailureType.PROVIDER_UNAVAILABLE
    if "auth" in name or "401" in msg or "403" in msg or "api key" in msg or "permission" in msg:
        return FailureType.AUTH
    if "contextretrieval" in name:  return FailureType.CONTEXT
    if "tokenlimit" in name:        return FailureType.TOKEN
    if "outputvalidation" in name:  return FailureType.VALIDATION
    return FailureType.NODE


def classify_exception(exc: Exception) -> str:
    """Map ANY exception to a FailureType. We match on class NAME (string) so we
    never need to import litellm/openai here — keeps this file dependency-light
    and robust to provider SDK changes."""
    name = type(exc).__name__.lower()
    msg = str(exc).lower()
    # our own typed exceptions first
    if isinstance(exc, ContextRetrievalError): return FailureType.CONTEXT
    if isinstance(exc, TokenLimitError):       return FailureType.TOKEN
    if isinstance(exc, OutputValidationError): return FailureType.VALIDATION
    if isinstance(exc, NodeError):             return FailureType.NODE
    # provider SDK exceptions (litellm/openai/google) -> shared string classifier
    return classify_failure(name, msg)


def _safe_json(obj, limit: int = 4000) -> str:
    try:
        s = json.dumps(obj, default=str)
    except Exception:
        s = str(obj)
    return s[:limit]




def check_token_budget(prompt: str, *, limit_tokens: int, counter=None) -> int:
    """Raise TokenLimitError (-> TOKEN_LIMIT_BREACH span) if the prompt is too big.
    Returns the token estimate on success."""
    from ..memory.store import approx_tokens
    counter = counter or approx_tokens
    n = counter(prompt)
    if n > limit_tokens:
        raise TokenLimitError(f"prompt ~{n} tokens exceeds limit {limit_tokens}")
    return n


@dataclass
class _NodeSpan:
    span: object
    def set_output(self, output) -> None:
        self.span.set_attribute("fleet.node.output", _safe_json(output))
    def annotate(self, key: str, value) -> None:
        self.span.set_attribute(key, _safe_json(value))


@contextmanager
def traced_node(node_name: str, *, state_in: dict | None = None, agent_graph: str = ""):
    """Trace ONE node. Emits a Langfuse span + Prometheus metrics from one place,
    and tags any exception with its FailureType via classify_exception()."""
    tracer = get_tracer()
    graph = agent_graph or "none"
    start = time.perf_counter()
    status = "ok"
    failure_type: str | None = None
    with tracer.start_as_current_span(f"node:{node_name}") as span:
        span.set_attribute("fleet.node.name", node_name)
        if agent_graph:
            span.set_attribute("fleet.graph", agent_graph)
        if state_in is not None:
            span.set_attribute("fleet.node.state_in", _safe_json(state_in))
        handle = _NodeSpan(span)
        try:
            yield handle
        except Exception as exc:
            status = "fail"
            failure_type = classify_exception(exc)     # ONE place decides the category
            span.set_attribute(FAILURE_ATTR, failure_type)
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            span.record_exception(exc)
            raise
        finally:
            duration = time.perf_counter() - start
            span.set_attribute("fleet.node.duration_s", round(duration, 6))
            metrics.record_node(node_name, graph, status, duration, failure_type)