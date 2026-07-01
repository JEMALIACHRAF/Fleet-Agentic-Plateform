# Fleet Agentic Platform

![Python](https://img.shields.io/badge/Python-3.10+-3776AB?logo=python&logoColor=white)
![Google ADK](https://img.shields.io/badge/Google%20ADK-orchestration-4285F4?logo=google&logoColor=white)
![LiteLLM](https://img.shields.io/badge/LiteLLM-router%20%2B%20fallback-00A67E)
![FastAPI](https://img.shields.io/badge/FastAPI-serving-009688?logo=fastapi&logoColor=white)
![OpenTelemetry](https://img.shields.io/badge/OpenTelemetry-tracing-425CC7?logo=opentelemetry&logoColor=white)
![Langfuse](https://img.shields.io/badge/Langfuse-traces-000000)
![Prometheus](https://img.shields.io/badge/Prometheus-metrics-E6522C?logo=prometheus&logoColor=white)
![Grafana](https://img.shields.io/badge/Grafana-dashboards-F46800?logo=grafana&logoColor=white)
![Docker](https://img.shields.io/badge/Docker-compose-2496ED?logo=docker&logoColor=white)
![Status](https://img.shields.io/badge/status-portfolio-blueviolet)

A skill-based **multi-agent platform** for fleet operations, built on **Google ADK**, with production-grade observability (**Langfuse / OpenTelemetry** for tracing, **Prometheus / Grafana** for metrics), typed failure handling, and a declarative skill architecture.

The platform runs two use cases on the same engine — **fleet health** (vehicle telemetry analysis) and **ticket support** (customer-support triage) — and is designed so that adding a new capability is writing a skill, not editing the engine.

---

## Key ideas

- **Declarative skills.** Every agent is a `SKILL.md` file: YAML metadata the code reads (model group, tools, output schema) plus a Markdown instruction the model reads. Skills are discovered and validated at startup — an unknown tool fails at boot, not in production.
- **Two execution engines, one set of skills.** An **ADK server** orchestrates agents as an explicit graph (production path), and an **in-house pipeline** runs the same skills deterministically for hermetic testing and a plain HTTP surface.
- **Model groups, not hard-coded models.** A skill names a logical tier — `fast` or `deep` — mapped to concrete providers via a **LiteLLM router** that handles retries and cross-provider fallback. Swapping providers is a config change, not a code change.
- **Reliability in the runtime.** Failures are classified into a taxonomy so the reaction matches the cause (retry the transient, escalate auth, fix logic bugs). Tools degrade gracefully — e.g. BigQuery → CSV cache — while recording the failure. *Observe the failure without breaking the service.*
- **Dual observability.** Langfuse answers "why did this run fail" (per-node traces, cost, tokens, latency); Prometheus/Grafana answer "is the failure rate rising" (aggregate metrics, alerting).

---

## Architecture

```
src/fleet/
├── config.py              # central settings; logical model groups (fast/deep)
├── skills/
│   ├── loader.py          # discover + validate SKILL.md at startup
│   ├── dispatcher.py      # progressive-disclosure routing (load only the chosen skill)
│   └── builtin/           # 6 declarative skills (see below)
├── agents/
│   ├── factory.py         # turn a Skill into a concrete ADK agent
│   └── guardrails.py      # token-budget / concurrency guardrails
├── tools/
│   ├── bigquery_tool.py   # vehicle telemetry (BigQuery → CSV fallback)
│   ├── ticket_tool.py     # customer-support ticket lookup
│   ├── registry.py        # map tool name → function
│   └── schemas.py         # Pydantic models (validation at the boundary)
├── llm/router.py          # LiteLLM router: retries + provider fallback
├── memory/store.py        # conversation memory (window + token budget)
├── orchestration/
│   ├── pipeline.py        # in-house engine (sequential / parallel, fail-fast)
│   └── llm_node.py        # run one skill as a real model call
├── observability/
│   ├── tracing.py         # OTel → Langfuse + failure-typing span processor
│   ├── metrics.py         # Prometheus counters/histograms
│   └── failures.py        # failure taxonomy + classifier
└── api/                   # in-house HTTP surface (/health, /skills, /pipelines/run, /metrics)

serving/                   # the ADK server (production path)
├── main.py                # ADK FastAPI app + Prometheus middleware + /metrics
└── agents/
    ├── coordinator/       # ADK dynamic routing (LLM transfers to a sub-agent)
    ├── fleet_health/      # workflow: anomaly + route ∥ → maintenance → reporter
    └── ticket_support/    # workflow: classify → respond

grafana/ · prometheus/     # observability stack (docker-compose)
data/                      # sample telemetry + ticket CSVs
```

### The two use cases

**Fleet health** — a 4-agent workflow. `anomaly_detector` and `route_optimizer` run in parallel (detect overheating / low fuel, suggest a routing adjustment), then `maintenance_advisor` recommends actions and `fleet_reporter` writes an operator summary. Vehicle data comes from BigQuery with a CSV fallback.

**Ticket support** — a 2-agent workflow. `ticket_classifier` fetches the ticket and classifies it (category, priority, churn risk), then `ticket_responder` drafts a reply and escalates when priority is high or churn risk is true.

### Skills

| Skill | Tier | Tools | Role |
|-------|------|-------|------|
| `anomaly_detector` | fast | `get_vehicle_readings` | Flag overheating / low fuel, rate severity |
| `route_optimizer` | fast | `get_vehicle_readings` | Suggest one routing / speed adjustment |
| `maintenance_advisor` | deep | — | Recommend maintenance actions from anomalies |
| `fleet_reporter` | deep | — | Write a short operator summary |
| `ticket_classifier` | fast | `get_customer_ticket` | Category, priority, churn risk |
| `ticket_responder` | deep | `get_customer_ticket` | Draft response, decide escalation |

---

## Observability

- **Tracing (Langfuse via OpenTelemetry).** `GoogleADKInstrumentor` auto-traces every agent, tool and LLM call. A custom **span processor** reads ADK's error spans, classifies them against the failure taxonomy, and increments the typed Prometheus metric — so failure typing works automatically on the ADK path.
- **Metrics (Prometheus + Grafana).** An HTTP middleware records request rate, status and latency; the failure metric records failures by type and node. The Grafana dashboard separates **service health** (HTTP rate, 5xx, p95) from **agent health** (failures by type, by node, agent runs).
- **The core insight:** in an agent system, HTTP health is not business health — a rate-limit is handled internally and returns HTTP 200. Failure-typed business metrics are what reveal the real state.

---

## Failure taxonomy

Failures are grouped by the reaction they require:

- **Transient** (retry / fail over): `rate_limit`, `provider_unavailable`, `timeout`
- **Data** (fix the input): `context_failure`, `validation_failure`, `token_limit_breach`
- **Config / security** (escalate): `auth_failure`
- **Logic bug** (fix the code): `node_failure`

Recovered failures are still recorded — e.g. a BigQuery failure that falls back to CSV, or a missing ticket id — so the dashboard shows them even when the request ultimately succeeds.

---

## Getting started

### Prerequisites
- Python 3.10+
- A Gemini API key (`GOOGLE_API_KEY`); optionally an OpenAI key for fallback
- Docker (for the Prometheus / Grafana stack; Langfuse runs as its own stack — see below)

### Install
```bash
pip install -r requirements.txt
cp .env.example .env      # fill in GOOGLE_API_KEY, LANGFUSE_* , etc.
```

### Services & local URLs

| Service | URL | Notes |
|---------|-----|-------|
| **ADK server** | http://localhost:8080 | production path (`uvicorn serving.main:app`) |
| **ADK web UI** | http://localhost:8080/dev-ui | interactive agent UI (`adk web`) |
| **In-house API** | http://localhost:8000 | `/health` · `/skills` · `/pipelines/run` · `/metrics` |
| **Langfuse** | http://localhost:3000 | traces (self-hosted stack or Langfuse Cloud) |
| **Prometheus** | http://localhost:9099 | metrics store / target status |
| **Grafana** | http://localhost:3001 | dashboards (login `admin` / `admin`) |

### 1. Start the observability stack
```bash
docker compose up -d
# Prometheus  -> http://localhost:9099
# Grafana     -> http://localhost:3001   (admin / admin, dashboards auto-provisioned)
```

Langfuse runs separately (since v3 it needs its own multi-container stack). Easiest path is **Langfuse Cloud** (free tier, no Docker); to self-host, clone `github.com/langfuse/langfuse` and `docker compose up`, then set `LANGFUSE_HOST=http://localhost:3000` and the keys in `.env`.

### 2. Run the agents

**Production ADK server:**
```bash
uvicorn serving.main:app --port 8080
```

**Interactive ADK UI (recommended for a demo):**
```bash
adk web
# open http://localhost:8080/dev-ui, pick a workflow, type a request
```

**In-house HTTP API (the second engine):**
```bash
uvicorn fleet.api.main:app --port 8000
```

### 3. Try it — natural-language requests

The skills extract the vehicle / ticket id from the message, so you can talk to the agents in plain language rather than passing a bare id.

**Fleet health:**
```
Can you check how vehicle VToomey-01 is doing? Flag anything wrong with it.
```
```
Run a health check on VToomey-02 and tell me if it needs maintenance.
```

**Ticket support:**
```
A customer opened TICKET-042 about being charged twice — look into it and draft a reply.
```
```
Please handle TICKET-043: classify how urgent it is and respond appropriately.
```

### 4. Demo a failure (resilience & observability)
- Set an invalid `BIGQUERY_PROJECT` in `.env`, then ask *"check how VToomey-01 is doing"* → a `context_failure` shows up in Grafana while the request still succeeds via the CSV fallback.
- Ask about a non-existent ticket, e.g. *"something's wrong with ticket CKET-049, can you check it?"* → a `context_failure` is recorded and the agent still replies gracefully.

Watch the run trace in Langfuse (per-node cost/latency/tokens) and the aggregate in Grafana (failures by type and by node).

---

## Configuration

All settings live in `config.py` and are read from the environment / `.env`, so the same code runs locally and in production. Key knobs: model groups (`gemini_fast_model`, `gemini_deep_model`, OpenAI equivalents), reliability budgets (`llm_num_retries`, `llm_timeout_seconds`, `max_prompt_tokens`, `max_concurrent_pipelines`), and observability endpoints (`langfuse_host`, Langfuse keys).

---

## Tech stack

Google ADK · LiteLLM · Pydantic · FastAPI / Uvicorn · OpenTelemetry · Langfuse · Prometheus · Grafana · BigQuery (optional) · Docker

---

<div align="center">

**Built by [Achraf Jemali](https://github.com/JEMALIACHRAF)** · Data & AI Consultant · Paris/Île-de-France



</div>