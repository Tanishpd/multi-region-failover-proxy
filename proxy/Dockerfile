# syntax=docker/dockerfile:1
FROM python:3.12-slim-bookworm AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN useradd --system --uid 10001 --create-home appuser
USER appuser

EXPOSE 8000

# /healthz is process liveness only and stays 200 while every region is dead —
# so this healthcheck reports the proxy, never the regions. python:*-slim has
# no curl, hence urllib.
HEALTHCHECK --interval=3s --timeout=2s --start-period=3s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys;\
sys.exit(0) if urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=1.5).status==200 else sys.exit(1)"]

# ONE worker, pinned. prometheus-client without PROMETHEUS_MULTIPROC_DIR reports
# only one worker's counters, so multiple workers would silently under-report
# every number in the chaos report. It is also what lets the circuit breaker
# live in process memory instead of needing distributed state. (ADR-008)
CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "1", \
     "--no-access-log", \
     "--timeout-graceful-shutdown", "2"]
