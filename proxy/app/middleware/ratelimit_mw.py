"""Rate limiting. Brief requirement 2.

MUST subclass BaseHTTPMiddleware. `add_middleware` accepts any ASGI middleware,
but `dispatch` is only ever called by BaseHTTPMiddleware's machinery — a plain
pass-through class that happens to define `async def dispatch` never runs it,
requests sail straight through, and rate limiting silently does not exist. The
integration test that fires capacity+1 requests is what catches that.
"""

from __future__ import annotations

import logging

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.logging_setup import logx
from app.ratelimit.headers import headers_for, rejection_body

# Control-plane routes are exempt: rate-limiting your own health check means an
# orchestrator can restart you for being popular.
EXEMPT_PREFIXES = ("/healthz", "/readyz", "/metrics", "/admin", "/debug")


class RateLimitMiddleware(BaseHTTPMiddleware):
    """The limiter is resolved from app.state per request, not captured here.

    Middleware is constructed when the app object is built; the limiter is built
    in the lifespan handler, because its Redis pool must bind to the running
    event loop. Capturing it at construction time would capture None.
    """

    def __init__(self, app, *, chaos_mode: bool = False, chaos_token: str = "") -> None:
        super().__init__(app)
        self.chaos_mode = chaos_mode
        self.chaos_token = chaos_token

    def _bypassed(self, request: Request) -> bool:
        """Let the chaos flood measure failover rather than the rate limiter.

        Only honoured when the proxy was started with MRFP_CHAOS_MODE=1 and the
        token matches, so it cannot be used against a normally-configured proxy.
        """
        if not self.chaos_mode or not self.chaos_token:
            return False
        return request.headers.get("x-chaos-bypass") == self.chaos_token

    async def dispatch(self, request: Request, call_next):
        if request.url.path.startswith(EXEMPT_PREFIXES) or self._bypassed(request):
            return await call_next(request)

        limiter = getattr(request.app.state, "limiter", None)
        if limiter is None:  # pragma: no cover - only before lifespan completes
            return await call_next(request)

        identity = getattr(request.state, "client_ip", "unknown")
        decision = await limiter.check(identity)
        request.state.rate_limit = decision

        if not decision.allowed:
            # Reject WITHOUT touching an upstream: a rate-limited request must
            # not cost a region anything.
            logx(
                "rate_limit_rejected",
                level=logging.INFO,
                identity=identity,
                mode=decision.mode,
                retry_after_s=decision.retry_after_s,
            )
            request_id = getattr(request.state, "request_id", "-")
            return JSONResponse(
                status_code=429,
                media_type="application/problem+json",
                content=rejection_body(decision, request_id),
                headers=headers_for(decision),
            )

        response = await call_next(request)
        # Headers on success too, so a client can pace itself rather than
        # discovering the limit by being rejected.
        for name, value in headers_for(decision).items():
            response.headers[name] = value
        return response
