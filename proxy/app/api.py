"""Routes. docs/CONTRACT.md section 3 owns the wire contract."""

from __future__ import annotations

import asyncio
import logging

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.logging_setup import logx
from app.schemas import ProxyRequest, ProxyResult, problem


async def infer(request: Request) -> Response:
    """POST /v1/infer — the only data-plane route."""
    state = request.app.state

    # Admission gate. Buffered bodies times unbounded concurrency is an
    # unbounded memory commitment in a 256M container, so shed early and
    # cheaply rather than OOM under total-blackout. (ADR-006)
    if state.admission.locked():
        logx("admission_rejected", level=logging.WARNING)
        return problem(
            503,
            "overloaded",
            "Too many in-flight requests",
            f"More than {state.settings.max_inflight} requests are already in flight.",
            request_id=getattr(request.state, "request_id", "-"),
        )

    async with state.admission:
        try:
            req = await ProxyRequest.from_starlette(request, max_body=state.settings.max_body_bytes)
        except ValueError as e:
            return problem(413, "body-too-large", "Request body too large", str(e))

        result: ProxyResult = await state.router.route(req)
        return result.to_response(primary_region=state.primary_region)


async def healthz(request: Request) -> JSONResponse:
    """Process liveness ONLY — never region health.

    This must return 200 while every region is dead. If it reflected region
    status, `docker compose ps` would report the one surviving component as
    broken at exactly the moment a reviewer is watching the demo, and an
    orchestrator would restart the proxy for an outage it is correctly
    surviving. Region status lives at /readyz and /admin/regions.
    """
    return JSONResponse({"status": "ok"})


async def readyz(request: Request) -> JSONResponse:
    """503 unless at least one region is currently believed usable.

    "Believed" is doing real work: this reflects only what actual requests have
    learned, because there is no health poller. A region nothing has tried yet
    counts as usable — with passive detection, refusing traffic on absence of
    evidence would be a self-fulfilling outage.
    """
    router = request.app.state.router
    usable = router.usable_regions()
    return JSONResponse(
        {
            "status": "ready" if usable else "no-regions",
            "usable": usable,
            "regions": {rid: {"ok": h.ok, "last": h.code} for rid, h in router.health.items()},
        },
        status_code=200 if usable else 503,
    )


async def admin_regions(request: Request) -> JSONResponse:
    state = request.app.state
    breaker = state.router.breaker
    health = state.router.health
    return JSONResponse(
        {
            "mode": state.settings.mode,
            "primary": state.primary_region,
            "regions": [
                {
                    "id": s.id,
                    "zone": s.zone,
                    "priority": s.priority,
                    "base_url": s.base_url,
                    "last_outcome": health[s.id].code,
                    "believed_ok": health[s.id].ok,
                    "breaker": (breaker.describe(s.id) if breaker else "n/a"),
                }
                for s in state.registry.ordered()
            ],
        }
    )


async def whoami(request: Request) -> JSONResponse:
    """What the proxy thinks you are — the first stop when every client shares
    one rate-limit bucket."""
    return JSONResponse(
        {
            "peer": getattr(request.state, "peer", "-"),
            "resolved_client_ip": getattr(request.state, "client_ip", "-"),
            "x_forwarded_for": request.headers.get("x-forwarded-for"),
            "trusted_proxies": request.app.state.settings.trusted_proxies,
            "note": (
                "resolved_client_ip is the rate-limit bucket identity. If this is a "
                "docker bridge address, every host client shares one bucket."
            ),
        }
    )


async def not_found(request: Request, exc: Exception) -> Response:
    return problem(404, "not-found", "Not found", f"No route for {request.url.path}")


async def server_error(request: Request, exc: Exception) -> Response:
    """Anything unhandled is OUR bug, and must not masquerade as an outage."""
    logx("internal_error", level=logging.ERROR, error_class=type(exc).__qualname__, error=str(exc))
    return problem(500, "internal", "Internal proxy error", "This is a defect in the proxy.")


async def _drain(task: asyncio.Task) -> None:  # pragma: no cover - helper
    with __import__("contextlib").suppress(asyncio.CancelledError):
        await task
