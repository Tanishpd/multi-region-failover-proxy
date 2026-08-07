"""Request/response plumbing and the header contract.

docs/CONTRACT.md sections 3 and 4 own every name here.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.failures import FailureClass

# Hop-by-hop headers are meaningful only for a single connection and must not be
# forwarded (RFC 9110 section 7.6.1). Forwarding Connection or
# Transfer-Encoding to an upstream produces framing bugs that look like random
# corruption. `host` is dropped so httpx sets it from the region's base_url.
_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
    }
)


@dataclass(slots=True)
class Attempt:
    region: str
    code: str
    ms: float
    fclass: FailureClass | None = None
    sent: bool = True

    def render(self) -> str:
        return f"{self.region}:{self.code}({self.ms:.1f}ms)"


@dataclass(slots=True)
class ProxyRequest:
    method: str
    path: str
    body: bytes
    headers: dict[str, str]
    request_id: str
    client_ip: str
    idempotent: bool = True

    @classmethod
    async def from_starlette(cls, request: Request, *, max_body: int) -> ProxyRequest:
        """Read the body ONCE, into memory.

        A streamed body is consumed by the first attempt and cannot be replayed
        to a second region — and replay is the entire feature. The cap plus the
        admission semaphore is what stops buffered bodies from being an
        unbounded memory commitment. (ADR-006)
        """
        body = await request.body()
        if len(body) > max_body:
            raise ValueError(f"body of {len(body)} bytes exceeds the {max_body} byte cap")

        headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP_BY_HOP}
        return cls(
            method=request.method,
            path=request.url.path,
            body=body,
            headers=headers,
            request_id=getattr(request.state, "request_id", "-"),
            client_ip=getattr(request.state, "client_ip", "-"),
        )

    def upstream_headers(self, region_id: str) -> dict[str, str]:
        out = dict(self.headers)
        out["x-request-id"] = self.request_id
        out["x-mrfp-attempt-region"] = region_id
        return out


@dataclass(slots=True)
class ProxyResult:
    status: int
    body: bytes
    media_type: str
    region: str | None
    attempts: list[Attempt] = field(default_factory=list)
    total_ms: float = 0.0
    upstream_headers: dict[str, str] = field(default_factory=dict)
    breaker_state: str = "n/a"

    @classmethod
    def ok(
        cls, resp: httpx.Response, region: str, attempts: list[Attempt], total_ms: float
    ) -> ProxyResult:
        return cls(
            status=resp.status_code,
            body=resp.content,
            media_type=resp.headers.get("content-type", "application/json"),
            region=region,
            attempts=attempts,
            total_ms=total_ms,
        )

    @classmethod
    def all_failed(cls, attempts: list[Attempt], total_ms: float) -> ProxyResult:
        import json

        body = json.dumps(
            {
                "type": "https://mrfp.local/errors/all-regions-unavailable",
                "title": "All regions unavailable",
                "status": 503,
                "detail": "Every configured region failed. See X-Failover-Path.",
                "attempts": [a.render() for a in attempts],
            }
        ).encode()
        return cls(
            status=503,
            body=body,
            media_type="application/problem+json",
            region=None,
            attempts=attempts,
            total_ms=total_ms,
        )

    def headers(self, *, primary_region: str) -> dict[str, str]:
        """The decision, exposed. Present on the 503 as well as on success —
        a failure response that cannot explain itself is useless in a demo."""
        return {
            "X-Served-By-Region": self.region or "none",
            "X-Failover-Path": ",".join(a.render() for a in self.attempts) or "-",
            "X-Attempts": str(len(self.attempts)),
            "X-Total-Latency-Ms": f"{self.total_ms:.1f}",
            "X-Degraded": "0" if self.region == primary_region else "1",
            "X-Breaker-State": self.breaker_state,
        }

    def to_response(self, *, primary_region: str) -> Response:
        headers = self.headers(primary_region=primary_region)
        if self.status == 503:
            headers["Retry-After"] = "1"
        return Response(
            content=self.body,
            status_code=self.status,
            media_type=self.media_type,
            headers=headers,
        )


def problem(status: int, type_: str, title: str, detail: str, **extra: object) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        media_type="application/problem+json",
        content={
            "type": f"https://mrfp.local/errors/{type_}",
            "title": title,
            "status": status,
            "detail": detail,
            **extra,
        },
    )
