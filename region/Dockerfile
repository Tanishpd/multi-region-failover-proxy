# syntax=docker/dockerfile:1
#
# The mock region. One image, run three times with different environment.
#
# python:3.12-slim-bookworm, native arm64. Never `platform: linux/amd64` —
# emulation jitter would make the proxy's 200 ms threshold flap. (ADR-015)
FROM python:3.12-slim-bookworm AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Requirements first so the dependency layer survives app-code edits.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN useradd --system --uid 10001 --create-home appuser
USER appuser

EXPOSE 8080

# python:*-slim ships no curl, so the healthcheck uses urllib. A curl-based
# check would mark every service unhealthy and `up --wait` would hang forever.
HEALTHCHECK --interval=3s --timeout=2s --start-period=3s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys;\
sys.exit(0) if urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=1.5).status==200 else sys.exit(1)"]

# --timeout-graceful-shutdown 0 is load-bearing for the whole project.
#
# Without it, uvicorn's shutdown waits for in-flight connections to close —
# including the proxy's warm keep-alive sockets. `docker stop` then blocks for
# the full grace period WHILE THE REGION KEEPS SERVING 200s, so the chaos demo
# shows no failover and the router looks broken. (ADR-011)
CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", "--port", "8080", \
     "--no-access-log", \
     "--timeout-graceful-shutdown", "0"]
