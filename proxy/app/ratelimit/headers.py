"""One source of truth for rate-limit headers and the 429 body.

docs/CONTRACT.md section 4. Headers go on 200s as well as 429s, so a client can
pace itself before being rejected rather than discovering the limit by hitting it.
"""

from __future__ import annotations

from app.ratelimit.limiter import Decision


def headers_for(decision: Decision) -> dict[str, str]:
    out = {
        # draft-ietf-httpapi-ratelimit-headers naming
        "RateLimit-Limit": str(decision.limit),
        "RateLimit-Remaining": str(decision.remaining),
        "RateLimit-Reset": str(decision.reset_after_s),
        # Legacy aliases, still what most clients actually read.
        "X-RateLimit-Limit": str(decision.limit),
        "X-RateLimit-Remaining": str(decision.remaining),
        "X-RateLimit-Reset": str(decision.reset_after_s),
        # Which limiter actually decided: redis | local | open.
        "X-RateLimit-Mode": decision.mode,
    }
    if not decision.allowed:
        out["Retry-After"] = str(decision.retry_after_s)
    return out


def rejection_body(decision: Decision, request_id: str) -> dict:
    return {
        "type": "https://mrfp.local/errors/rate-limit-exceeded",
        "title": "Rate limit exceeded",
        "status": 429,
        "detail": (
            f"Token bucket empty for this client. Capacity {decision.limit}, "
            f"refilling continuously; retry in {decision.retry_after_s}s."
        ),
        "retry_after_seconds": decision.retry_after_s,
        "mode": decision.mode,
        "request_id": request_id,
    }
