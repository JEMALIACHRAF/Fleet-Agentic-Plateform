# Production image: ADK FastAPI server (serving/main.py) + Langfuse + Prometheus.
FROM python:3.12-slim

WORKDIR /app

# System deps kept minimal. uvicorn serves the ADK app.
COPY pyproject.toml requirements.txt ./
COPY src ./src
RUN pip install --no-cache-dir -e . \
 && pip install --no-cache-dir \
      google-adk>=2.0 \
      litellm>=1.40 \
      openinference-instrumentation-google-adk>=0.1.10 \
      uvicorn[standard]>=0.30

# App code (agents discovered from serving/agents/)
COPY serving ./serving
COPY data ./data

ENV PORT=8080
EXPOSE 8080

# get_fast_api_app discovers serving/agents/*; /metrics exposes Prometheus.
CMD ["uvicorn", "serving.main:app", "--host", "0.0.0.0", "--port", "8080"]
