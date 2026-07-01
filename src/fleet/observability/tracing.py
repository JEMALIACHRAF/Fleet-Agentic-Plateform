"""Tracing setup.

Wires OpenTelemetry to Langfuse via OTLP/HTTP. If Langfuse keys are absent we
fall back to an in-memory exporter (tests) or console exporter (local), so the
*same tracing code paths execute* regardless of environment.

Also calls GoogleADKInstrumentor so every ADK agent/tool/LLM span is captured
automatically; on top of that, a span processor types ADK's error spans into our
Prometheus failure metric (see failures.py).
"""
from __future__ import annotations

import base64
import os
from dataclasses import dataclass

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
)
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from ..config import Settings, get_settings

_TRACER_NAME = "fleet.platform"
_ACTIVE_PROVIDER: TracerProvider | None = None

# We count a failure ONCE, at the span that represents a real unit of work:
#   - "call_llm"      : a model call
#   - "execute_tool"  : a tool call
# Container spans like "agent_run [...]" and "invocation [...]" re-record the SAME
# exception as they propagate it up the tree, so counting them would double/triple
# count one failure. We only count the work-unit spans (inclusion list = stable even
# when the framework adds new container span types).
_COUNTABLE_SPAN_PREFIXES = ("call_llm", "execute_tool")


class _FailureTypingSpanProcessor(SpanProcessor):
    """Bridges ADK's error spans to our failure taxonomy + Prometheus.

    On every finished ERROR span, read the recorded exception, classify it with
    classify_failure(), and increment fleet_node_failures_total{type=...}.

    Anti-double-count: count ONLY work-unit spans (call_llm / execute_tool). ADK
    re-records the same exception on the parent container spans (agent_run,
    invocation) as it propagates, so those must be skipped or one failure would be
    counted at every level of the tree.
    """

    def on_start(self, span, parent_context=None):
        return None

    def on_end(self, span):
        try:
            from opentelemetry.trace import StatusCode

            # (a) only ERROR spans are failures
            if span.status is None or span.status.status_code != StatusCode.ERROR:
                return

            # (b) count ONLY real work-unit spans; skip propagating container spans
            #     (agent_run [...], invocation [...]) -> no double/triple counting.
            name = span.name or "unknown"
            if not name.startswith(_COUNTABLE_SPAN_PREFIXES):
                return

            # (c) read the recorded exception (type + message)
            exc_type, exc_msg = "", (span.status.description or "")
            for ev in (span.events or []):
                if ev.name == "exception":
                    a = dict(ev.attributes or {})
                    exc_type = a.get("exception.type", exc_type)
                    exc_msg = a.get("exception.message", exc_msg)
                    break

            # (d) classify + increment exactly once, at the origin work-unit span
            from .failures import classify_failure
            from . import metrics as M

            ftype = classify_failure(exc_type, exc_msg)
            graph = dict(span.attributes or {}).get("fleet.graph", "adk")
            M.NODE_FAILURES.labels(node=name, graph=graph, type=ftype).inc()
        except Exception:
            return  # observability must never break the app

    def shutdown(self):
        return None

    def force_flush(self, timeout_millis: int = 30000):
        return True


@dataclass
class TracingHandles:
    provider: TracerProvider
    in_memory: InMemorySpanExporter | None
    mode: str


def _langfuse_otlp_env(s: Settings) -> bool:
    """Configure OTLP env vars for Langfuse. Returns True if keys were present."""
    if not (s.langfuse_public_key and s.langfuse_secret_key):
        return False
    auth = base64.b64encode(
        f"{s.langfuse_public_key}:{s.langfuse_secret_key}".encode()
    ).decode()
    os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = s.langfuse_host.rstrip("/") + "/api/public/otel"
    os.environ["OTEL_EXPORTER_OTLP_HEADERS"] = f"Authorization=Basic {auth}"
    return True


def setup_tracing(settings: Settings | None = None, force_memory: bool = False) -> TracingHandles:
    """Idempotent-ish tracer setup. `force_memory=True` is used by tests."""
    s = settings or get_settings()
    resource = Resource.create({"service.name": s.otel_service_name})
    provider = TracerProvider(resource=resource)
    in_memory: InMemorySpanExporter | None = None

    if not s.tracing_enabled:
        mode = "off"
    elif force_memory:
        in_memory = InMemorySpanExporter()
        provider.add_span_processor(SimpleSpanProcessor(in_memory))
        mode = "memory"
    elif _langfuse_otlp_env(s):
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        mode = "langfuse"
    else:
        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
        mode = "console"

    trace.set_tracer_provider(provider)
    if mode != "off":
        provider.add_span_processor(_FailureTypingSpanProcessor())
    global _ACTIVE_PROVIDER
    _ACTIVE_PROVIDER = provider
    _maybe_instrument_adk(provider)
    return TracingHandles(provider=provider, in_memory=in_memory, mode=mode)


def _maybe_instrument_adk(provider: TracerProvider) -> None:
    """Attach OpenInference ADK auto-instrumentation if available (lazy/optional)."""
    try:
        from openinference.instrumentation.google_adk import GoogleADKInstrumentor
        GoogleADKInstrumentor().instrument(tracer_provider=provider)
    except Exception:
        pass


def get_tracer():
    if _ACTIVE_PROVIDER is not None:
        return _ACTIVE_PROVIDER.get_tracer(_TRACER_NAME)
    return trace.get_tracer(_TRACER_NAME)