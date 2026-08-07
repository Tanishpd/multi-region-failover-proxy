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
# The graceful-shutdown window comes from MRFP_GRACEFUL_SHUTDOWN_S, because it
# was hardcoded to 2 while `graceful_shutdown_s` existed as a setting, was set
# to 30 by the prod profile, and was plumbed through compose — and nothing
# consumed it. Measured: with a 6-second request in flight, `docker stop`
# returned in 2.36s, cutting it off, on a profile whose `stop_grace_period` is
# 45s precisely so that cannot happen. Every rolling deploy dropped every
# request slower than 2 seconds — which, at a 30s read timeout for real
# inference, is most of them.
#
# `sh -c` with `exec` on purpose: exec form cannot expand a variable, and plain
# shell form would leave the shell as PID 1 so uvicorn never receives SIGTERM —
# which would turn every graceful shutdown into a 45-second SIGKILL wait.
CMD ["sh", "-c", "exec uvicorn app.main:app \
     --host 0.0.0.0 --port 8000 \
     --workers 1 --no-access-log \
     --timeout-graceful-shutdown ${MRFP_GRACEFUL_SHUTDOWN_S:-5}"]
