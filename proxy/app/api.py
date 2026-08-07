"""Routes. docs/CONTRACT.md section 3 owns the wire contract."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import secrets

from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from app import metrics
from app.logging_setup import logx
from app.schemas import ProxyRequest, ProxyResult, problem


def _wants_stream(body: bytes, enabled: bool) -> bool:
    """Peek at the parsed body for `stream: true`.

    Cheap and forgiving: a malformed body is not this function's problem, the
    upstream will reject it with a 4xx that passes straight through.
    """
    if not enabled:
        return False
    import json as _json

    try:
        return bool(_json.loads(body).get("stream"))
    except Exception:
        return False


async def _infer_streaming(request: Request, req: ProxyRequest) -> Response:
    """Streaming path. Failover is possible until the first byte, then never.

    The lease already holds the upstream's first chunk, so everything up to
    here was replayable and any region failure was invisible to the client.
    """
    from app import streaming

    state = request.app.state
    lease, attempts = await state.router.open_stream(req)

    if lease is None:
        result = ProxyResult.all_failed(attempts, 0.0)
        return result.to_response(primary_region=state.primary_region)

    base = {
        "X-Served-By-Region": lease.region,
        "X-Failover-Path": ",".join(a.render() for a in attempts) or "-",
        "X-Attempts": str(len(attempts)),
        "X-Degraded": "0" if lease.region == state.primary_region else "1",
    }
    return StreamingResponse(
        streaming.pump(
            lease,
            request_id=req.request_id,
            inter_chunk_timeout_s=state.settings.stream_inter_chunk_timeout_s,
        ),
        media_type=lease.headers.get("content-type", "text/event-stream"),
        headers=streaming.response_headers(lease, base),
    )


async def infer(request: Request) -> Response:
    """POST /v1/infer — the only data-plane route."""
    state = request.app.state

    tls = require_tls(request)
    if tls is not None:
        return tls

    # Admission gate. Buffered bodies times unbounded concurrency is an
    # unbounded memory commitment in a 256M container, so shed early and
    # cheaply rather than OOM under total-blackout. (ADR-006)
    if state.admission.locked():
        metrics.ADMISSION_REJECTED.inc()
        logx("admission_rejected", level=logging.WARNING)
        return problem(
            503,
            "overloaded",
            "Too many in-flight requests",
            f"More than {state.settings.max_inflight} requests are already in flight.",
            request_id=getattr(request.state, "request_id", "-"),
        )

    async with state.admission:
        metrics.ADMISSION_INFLIGHT.inc()
        try:
            req = await ProxyRequest.from_starlette(
                request,
                max_body=state.settings.max_body_bytes,
                read_timeout_s=state.settings.body_read_timeout_s,
                replay_maybe_sent=state.settings.replay_maybe_sent,
            )
        except ValueError as e:
            # Covers three cases, all client-caused: Content-Length over the
            # cap, a body that outgrew it mid-stream, and a read that stalled.
            detail = str(e)
            status = 408 if "not received within" in detail else 413
            title = "Request timeout" if status == 408 else "Request body too large"
            logx("request_rejected", level=logging.WARNING, status=status, detail=detail)
            return problem(status, "body-rejected", title, detail)

        # Claim the idempotency key BEFORE any upstream call, so a duplicate
        # that arrives while the first is still running is refused rather than
        # started. Released if we never reach an upstream, or the client's own
        # correct retry would be blocked for the full TTL.
        principal = getattr(request.state, "principal", None)
        claim = await state.idempotency.claim(
            principal.id if principal else getattr(request.state, "client_ip", "-"),
            req.idempotency_key,
        )
        if not claim.may_proceed:
            return problem(
                409,
                "idempotency-conflict",
                "Duplicate request",
                f"Idempotency-Key {req.idempotency_key!r} is {claim.state.value}. The original "
                "request may still be running; this one was not started.",
                request_id=req.request_id,
            )

        if _wants_stream(req.body, state.settings.streaming_enabled):
            try:
                return await _infer_streaming(request, req)
            finally:
                # The slot is released as soon as the response STARTS. A stream
                # can run for minutes; holding a buffered-path admission slot
                # for its whole life would let a handful of long generations
                # shut out every short request.
                metrics.ADMISSION_INFLIGHT.dec()

        try:
            result: ProxyResult = await state.router.route(req)
        except BaseException:
            await state.idempotency.release(claim)
            raise
        finally:
            metrics.ADMISSION_INFLIGHT.dec()

        if result.status < 500:
            await state.idempotency.finish(claim)
        else:
            # Nothing was delivered, so the caller's retry must be allowed.
            await state.idempotency.release(claim)

        if len(result.body) > state.settings.max_response_bytes:
            # The request side has a cap; the response side needs the same one.
            # A buffered response is held in memory for the whole request, so an
            # upstream returning something enormous is the same OOM the request
            # cap exists to prevent, arriving from the other direction.
            logx(
                "response_too_large",
                level=logging.ERROR,
                region=result.region,
                bytes=len(result.body),
                cap=state.settings.max_response_bytes,
            )
            return problem(
                502,
                "upstream-response-too-large",
                "Upstream response too large",
                f"{result.region} returned {len(result.body)} bytes, over the "
                f"{state.settings.max_response_bytes} byte cap.",
            )
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


async def drain(request: Request) -> Response:
    """Begin draining: fail readiness while continuing to serve in-flight work.

    This is the missing half of a safe deploy. Without it, SIGTERM is the first
    thing that tells the load balancer anything, and every request in flight
    when the container stops is a 502 the client sees.

    The sequence a preStop hook should run:
      1. POST /admin/drain   -> /readyz starts failing
      2. sleep >= 2 x the LB's readiness interval  -> it stops sending new work
      3. SIGTERM             -> uvicorn finishes what it is holding

    /healthz deliberately keeps returning 200 throughout: liveness must not
    fail during a drain, or the orchestrator kills the pod mid-request.
    """
    denied = _admin_denied(request)
    if denied is not None:
        return denied

    request.app.state.draining = True
    logx("draining", level=logging.WARNING)
    return JSONResponse({"status": "draining"})


async def readyz(request: Request) -> JSONResponse:
    """503 unless at least one region is currently believed usable.

    "Believed" is doing real work: this reflects only what actual requests have
    learned, because there is no health poller. A region nothing has tried yet
    counts as usable — with passive detection, refusing traffic on absence of
    evidence would be a self-fulfilling outage.
    """
    state = request.app.state
    if getattr(state, "draining", False):
        return JSONResponse({"status": "draining", "usable": []}, status_code=503)

    router = state.router
    usable = router.usable_regions()
    return JSONResponse(
        {
            "status": "ready" if usable else "no-regions",
            "usable": usable,
            "regions": {rid: {"ok": h.ok, "last": h.code} for rid, h in router.health.items()},
            # Reported, deliberately NOT part of the verdict. A degraded limiter
            # still serves every request correctly, just unlimited, so failing
            # readiness would turn a degradation into a self-inflicted outage.
            # The deploy-time case is caught at boot instead, and the runtime
            # case pages via rate_limit_backend_up. (ADR-029)
            "rate_limit": {
                "boot": getattr(state, "redis_boot_state", "unknown"),
                "backend": "redis" if metrics.rate_limit_backend_up() else "fallback",
            },
        },
        status_code=200 if usable else 503,
    )


async def admin_regions(request: Request) -> Response:
    denied = _admin_denied(request)
    if denied is not None:
        return denied

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


def _admin_denied(request: Request) -> Response | None:
    """Guard for /admin/* and /debug/*. Returns a response to send, or None.

    Two states, no third:
      * MRFP_ADMIN_TOKEN unset  -> the routes are DISABLED (404). A control
        plane that can reset any client's rate-limit bucket must not be open
        merely because nobody configured it.
      * set -> require `Authorization: Bearer <token>`, compared in constant
        time so the comparison itself is not an oracle.

    Previously these routes were entirely unauthenticated, and the rate limiter
    exempts /admin/* — so `DELETE /admin/ratelimit?id=<self>` between batches
    took a measured 20/40 up to 100/100 through a 20/s limit.
    """
    token = request.app.state.settings.admin_token
    if not token:
        return problem(
            404,
            "not-found",
            "Not found",
            "The control plane is disabled. Set MRFP_ADMIN_TOKEN to enable it.",
        )

    presented = request.headers.get("authorization", "")
    scheme, _, value = presented.partition(" ")
    if scheme.lower() != "bearer" or not secrets.compare_digest(value, token):
        logx(
            "admin_denied",
            level=logging.WARNING,
            path=request.url.path,
            peer=getattr(request.state, "peer", "-"),
        )
        return problem(
            401,
            "unauthorized",
            "Unauthorized",
            "This endpoint requires Authorization: Bearer <MRFP_ADMIN_TOKEN>.",
        )
    return None


def require_tls(request: Request) -> Response | None:
    """Refuse cleartext when TLS is expected.

    The proxy does not terminate TLS — an ingress does — so the only thing it
    can know is what that ingress tells it via X-Forwarded-Proto, and that
    header is only believable from a trusted peer (the same rule as
    X-Forwarded-For).

    426 rather than a redirect: a 308 makes many clients re-issue without the
    body, which for a POST-only API silently corrupts the request. 426 is
    unambiguous and unrecoverable-by-accident.
    """
    settings = request.app.state.settings
    if not settings.require_tls:
        return None

    peer = getattr(request.state, "peer", "")
    trusted = request.app.state.trusted_networks
    import ipaddress

    try:
        peer_trusted = any(ipaddress.ip_address(peer) in net for net in trusted)
    except ValueError:
        peer_trusted = False

    proto = request.headers.get("x-forwarded-proto", "").lower() if peer_trusted else ""
    if proto == "https" or request.url.scheme == "https":
        return None

    logx("cleartext_refused", level=logging.WARNING, peer=peer, proto=proto or "none")
    return problem(
        426,
        "tls-required",
        "TLS required",
        "This endpoint is served over HTTPS only. The proxy saw a cleartext request "
        "and no trusted X-Forwarded-Proto: https.",
    )


def _valid_identity(raw: str) -> bool:
    """Bucket identities are IPs. Anything else is someone probing.

    `?id=` was unvalidated, so an attacker could allocate an unbounded number of
    distinct keys in the in-process fallback bucket, which has no eviction.
    """
    try:
        ipaddress.ip_address(raw)
    except ValueError:
        return False
    return True


async def metrics_endpoint(request: Request) -> Response:
    """Prometheus exposition. Gauges are refreshed here rather than on a timer.

    A gauge written only on state change goes stale whenever nothing happens,
    which is exactly the situation during an outage nobody is driving traffic
    through. Recomputing at scrape time keeps it honest.
    """
    # Scrapers frequently cannot present a bearer token, and this route mutates
    # nothing — so it has its own switch rather than sharing the admin guard.
    if not request.app.state.settings.metrics_public:
        denied = _admin_denied(request)
        if denied is not None:
            return denied

    router = request.app.state.router
    metrics.observe_pools(request.app.state.clients)
    for region_id, health in router.health.items():
        metrics.observe_health(region_id, health.ok)
    if router.breaker is not None:
        for region_id in router.health:
            metrics.observe_breaker(region_id, router.breaker.state_name(region_id))

    return Response(content=metrics.render(), media_type=metrics.CONTENT_TYPE)


async def admin_ratelimit(request: Request) -> JSONResponse:
    """Inspect or reset a bucket. GET peeks without consuming; DELETE clears it.

    `peek` matters: inspecting a bucket must not change it, or the act of
    debugging a rate limit becomes part of the rate limit.
    """
    denied = _admin_denied(request)
    if denied is not None:
        return denied

    state = request.app.state
    identity = request.query_params.get("id") or getattr(request.state, "client_ip", "unknown")
    if not _valid_identity(identity):
        return problem(
            400, "bad-identity", "Bad identity", f"{identity!r} is not a valid IP address."
        )

    if request.method == "DELETE":
        cleared = await state.limiter.reset(identity)
        logx("admin_ratelimit_reset", level=logging.WARNING, identity=identity)
        return JSONResponse({"identity": identity, "cleared": cleared})

    decision = await state.limiter.peek(identity)
    return JSONResponse(
        {
            "identity": identity,
            "bucket_key": decision.bucket_key,
            "limit": decision.limit,
            "remaining": decision.remaining,
            "reset_after_s": decision.reset_after_s,
            "mode": decision.mode,
        }
    )


async def whoami(request: Request) -> Response:
    """What the proxy thinks you are — the first stop when every client shares
    one rate-limit bucket.

    Behind the admin guard, and no longer echoes `trusted_proxies`: telling an
    anonymous caller which source ranges are believed hands them the recipe for
    spoofing an identity.
    """
    denied = _admin_denied(request)
    if denied is not None:
        return denied

    return JSONResponse(
        {
            "peer": getattr(request.state, "peer", "-"),
            "resolved_client_ip": getattr(request.state, "client_ip", "-"),
            "x_forwarded_for": request.headers.get("x-forwarded-for"),
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
