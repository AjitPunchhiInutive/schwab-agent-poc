FROM python:3.11-slim AS base

# Security: run as non-root
RUN groupadd -r agent && useradd -r -g agent -s /sbin/nologin agent

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .
RUN chown -R agent:agent /app

USER agent

EXPOSE 8080

# Health check for Cloud Run readiness probes
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/healthz')" || exit 1

# Start ADK API server with Cloud Trace and OpenTelemetry enabled
CMD ["python", "-m", "google.adk.cli", "api_server", \
     "--port=8080", \
     "--trace_to_cloud", \
     "--otel_to_cloud", \
     "--agent=agent"]
