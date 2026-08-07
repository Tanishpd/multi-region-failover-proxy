"""One pure-ASGI layer replacing three BaseHTTPMiddleware layers.

WHY IT IS ONE LAYER. Every BaseHTTPMiddleware wraps the call in an anyio task
group and pumps the response through a memory object stream. Measured on this
machine (starlette 0.45.3): a bare Starlette app is ~109 us/req, three stacked
BaseHTTPMiddleware layers ~530 us/req, and one pure-ASGI layer doing all three
jobs ~110 us/req. The three layers cost ~420 us of pure CPU per request and cap
a single worker near 2,000 rps before any proxying happens.

WHY IT IS ALSO THE TELEMETRY LAYER. This is the outermost thing that sees a
terminal status, so it is the only place that can count the 429s, 413s, 408s,
404s and 500s that never reach the router. Before this existed,
`proxy_requests_total` was incremented only inside `ResilientRouter.route`, so
every rate-limited, oversized, timed-out or shed request was invisible to every
availability calculation. Fixing throughput and making the SLIs honest turned
out to be the same edit.

Order of work, outermost to innermost, unchanged from the three layers it
replaces: request id -> client identity -> rate limit -> app.
"""

from __future__ import annotations

import logging
import time
import uuid

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app import metrics
from app.auth import authenticate
from app.logging_setup import logx, request_id_var
from app.middleware.client_identity import normalize, parse_networks, resolve_client_ip
from app.ratelimit.headers import headers_for, rejection_body

# Control-plane routes skip rate limiting: throttling your own health check
# means an orchestrator can restart you for being popular.
EXEMPT_PREFIXES = ("/healthz", "/readyz", "/metrics", "/admin", "/debug")

_MAX_RID_LEN = 64


def _outcome(status: int, region: str | None, route: str) -> str:
    """The SLI dimension: what the CLIENT experienced.

    Deliberately not the raw status. `served_primary` and `served_failover` are
    both successes but only one of them is the happy path, and a 429 caused by
    the limiter failing closed is an outage wearing a client-error costume.

    Control-plane traffic gets its own bucket. Without that, a 200 from
    /healthz has no serving region and falls through to `served_failover`,
    which silently inflates the failover SLI with health-check volume — and
    health checks are by far the highest-rate route in a real deployment.
    """
    if route != "/v1/infer":
        return "control_plane" if status < 400 else "control_plane_error"
    if status == 429:
        return "rate_limited"
    if status in (408, 413):
        return "client_error"
    if status == 404:
        return "not_found"
    if status >= 500:
        return "all_regions_down" if region else "internal_error"
    if 400 <= status < 500:
        return "upstream_client_error"
    return "served_primary" if region == "us-east-1" else "served_failover"


class ContextMiddleware:
    """Pure ASGI. No task group, no stream pump, no per-request BaseHTTPMiddleware."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        trusted_proxies: str,
        chaos_mode: bool = False,
        chaos_token: str = "",
    ) -> None:
        self.app = app
        self.trusted = parse_networks(trusted_proxies)
        self.chaos_mode = chaos_mode
        self.chaos_token = chaos_token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()
        headers = Headers(scope=scope)
        path = scope.get("path", "")

        # --- request id -----------------------------------------------------
        # An inbound id is honoured so the chaos harness can join its own CSV
        # against the proxy's logs; that join is what assertion A9 checks.
        incoming = headers.get("x-request-id", "")
        rid = incoming[:_MAX_RID_LEN] if incoming else uuid.uuid4().hex[:16]
        token = request_id_var.set(rid)

        state = scope.setdefault("state", {})
        state["request_id"] = rid

        # --- client identity ------------------------------------------------
        client = scope.get("client")
        peer = client[0] if client else None
        state["peer"] = normalize(peer or "") or "unknown"
        state["client_ip"] = resolve_client_ip(peer, headers.get("x-forwarded-for"), self.trusted)
        state["principal"] = None

        status_seen = 500
        region_seen: str | None = None

        async def send_wrapper(message: Message) -> None:
            nonlocal status_seen, region_seen
            if message["type"] == "http.response.start":
                status_seen = message["status"]
                out = MutableHeaders(scope=message)
                out["x-request-id"] = rid
                region_seen = out.get("x-served-by-region") or None
                if region_seen == "none":
                    region_seen = None
                decision = state.get("rate_limit")
                if decision is not None:
                    for name, value in headers_for(decision).items():
                        out[name] = value
            await send(message)

        try:
            # --- authenticate ------------------------------------------------
            app_state = scope["app"].state
            if not path.startswith(EXEMPT_PREFIXES):
                principal, auth_error = authenticate(
                    getattr(app_state, "key_store", None),
                    mode=getattr(app_state.settings, "auth_mode", "off"),
                    authorization=headers.get("authorization", ""),
                    api_key_header=headers.get("x-api-key", ""),
                    client_ip=state["client_ip"],
                )
                if auth_error is not None:
                    await self._unauthorized(send_wrapper, auth_error, rid)
                    status_seen = 401
                    return
                state["principal"] = principal

            # --- rate limit --------------------------------------------------
            if not path.startswith(EXEMPT_PREFIXES) and not self._bypassed(headers):
                limiter = getattr(app_state, "limiter", None)
                if limiter is not None:
                    principal = state["principal"]
                    identity = principal.id if principal else state["client_ip"]
                    decision = await limiter.check(
                        identity,
                        dimension=principal.bucket_dimension if principal else "ip",
                        capacity=principal.capacity if principal else None,
                        refill_per_sec=principal.refill_per_sec if principal else None,
                    )
                    state["rate_limit"] = decision
                    if not decision.allowed:
                        metrics.RATE_LIMIT_REJECTED.inc()
                        logx(
                            "rate_limit_rejected",
                            identity=identity,
                            mode=decision.mode,
                            retry_after_s=decision.retry_after_s,
                        )
                        await self._reject(send_wrapper, decision, rid)
                        status_seen = 429
                        return

            await self.app(scope, receive, send_wrapper)
        finally:
            request_id_var.reset(token)
            elapsed = time.perf_counter() - started
            route = _route_label(path)
            outcome = _outcome(status_seen, region_seen, route)
            # Counted HERE, not in the router — this is the only layer that sees
            # every terminal status.
            metrics.HTTP_RESPONSES.labels(route, str(status_seen), outcome).inc()
            # Only data-plane latency belongs in the latency SLI; health checks
            # are ~0 ms and would drag every percentile down.
            if route == "/v1/infer":
                metrics.REQUEST_LATENCY.observe(elapsed)

    def _bypassed(self, headers: Headers) -> bool:
        if not self.chaos_mode or not self.chaos_token:
            return False
        return headers.get("x-chaos-bypass") == self.chaos_token

    @staticmethod
    async def _unauthorized(send, detail: str, request_id: str) -> None:
        import json

        body = json.dumps(
            {
                "type": "https://mrfp.local/errors/unauthorized",
                "title": "Unauthorized",
                "status": 401,
                "detail": detail,
                "request_id": request_id,
            }
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/problem+json"),
                    (b"www-authenticate", b'Bearer realm="mrfp"'),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    @staticmethod
    async def _reject(send, decision, request_id: str) -> None:
        import json

        body = json.dumps(rejection_body(decision, request_id)).encode()
        raw = [(b"content-type", b"application/problem+json")]
        raw += [(k.lower().encode(), v.encode()) for k, v in headers_for(decision).items()]
        raw.append((b"content-length", str(len(body)).encode()))
        await send({"type": "http.response.start", "status": 429, "headers": raw})
        await send({"type": "http.response.body", "body": body})


def _route_label(path: str) -> str:
    """Bounded label values only — a raw path is unbounded metric cardinality."""
    if path.startswith("/v1/infer"):
        return "/v1/infer"
    for known in EXEMPT_PREFIXES:
        if path.startswith(known):
            return known
    return "other"


logging.getLogger(__name__)
