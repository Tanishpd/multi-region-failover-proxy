"""Request/response plumbing and the header contract.

docs/CONTRACT.md sections 3 and 4 own every name here.
"""

from __future__ import annotations

import asyncio
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


# Response headers the proxy must NOT copy from an upstream.
#
# Hop-by-hop for the same reason as the request side, plus:
#   content-length / content-type  Starlette recomputes these from the body we
#                                  actually send; copying them can contradict it.
#   content-encoding               we hand the body through verbatim, so the
#                                  encoding header must travel with it — but
#                                  only via the explicit allowance below, never
#                                  alongside a recomputed content-length.
_RESPONSE_DROP = _HOP_BY_HOP | {"content-length", "content-type", "server", "date"}


def passthrough_response_headers(headers) -> dict[str, str]:
    """Forward the upstream's response headers to the client.

    Previously nothing did this: `ProxyResult.upstream_headers` was declared and
    never written, so every upstream header except Content-Type was silently
    dropped. For a real API that loses Cache-Control, ETag, Set-Cookie, the
    upstream's own RateLimit-* headers, and any request-id echo — which makes
    this a forwarder rather than a proxy.
    """
    return {k: v for k, v in headers.items() if k.lower() not in _RESPONSE_DROP}


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
    # Whether this request may be re-sent after a MAYBE_SENT failure.
    #
    # This was hardcoded True, which against a real billed inference API means
    # every read timeout re-issues to all three regions: three paid generations
    # and a 503, showing up in the availability dashboard rather than the
    # billing one. It is now derived from the caller's Idempotency-Key, with the
    # demo profile keeping the permissive behaviour requirement 4 relies on.
    idempotent: bool = True
    idempotency_key: str = ""

    @classmethod
    async def from_starlette(
        cls,
        request: Request,
        *,
        max_body: int,
        read_timeout_s: float = 5.0,
        replay_maybe_sent: bool = True,
    ) -> ProxyRequest:
        """Read the body ONCE, into memory, under a size cap AND a deadline.

        A streamed body is consumed by the first attempt and cannot be replayed
        to a second region — and replay is the entire feature. (ADR-006)

        This used to be `await request.body()` with the cap checked afterwards,
        which meant the cap did not exist: Starlette buffers every chunk and
        joins them with no limit, so the check only ever ran on a body already
        in memory. A single 512 MiB POST OOM-killed the container (peak is ~2x
        the body, because the chunk list and the joined bytes coexist during the
        join), and `restart: "no"` meant it stayed dead.

        Three bounds now, in increasing cost order:
          1. Content-Length, when present — rejects before a byte is read.
          2. A running total across `request.stream()` — aborts mid-upload for
             chunked bodies that declare no length, or that lie about it.
          3. A wall-clock deadline — an admission slot is held across this read,
             so an unbounded read is a slot held forever. 130 sockets trickling
             ~130 bytes/sec previously wedged the whole data plane while
             /healthz still reported 200.
        """
        declared = request.headers.get("content-length")
        if declared is not None:
            try:
                if int(declared) > max_body:
                    raise ValueError(f"Content-Length {declared} exceeds the {max_body} byte cap")
            except ValueError as exc:
                if "exceeds" in str(exc):
                    raise
                raise ValueError(f"malformed Content-Length: {declared!r}") from exc

        chunks: list[bytes] = []
        total = 0
        try:
            async with asyncio.timeout(read_timeout_s):
                async for chunk in request.stream():
                    total += len(chunk)
                    if total > max_body:
                        # Stop reading. Do not accumulate the rest.
                        raise ValueError(f"body exceeds the {max_body} byte cap")
                    chunks.append(chunk)
        except TimeoutError as exc:
            raise ValueError(
                f"body not received within {read_timeout_s:.1f}s (slow or stalled upload)"
            ) from exc

        body = b"".join(chunks)
        headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP_BY_HOP}
        key = request.headers.get("idempotency-key", "")
        return cls(
            method=request.method,
            path=request.url.path,
            body=body,
            headers=headers,
            request_id=getattr(request.state, "request_id", "-"),
            client_ip=getattr(request.state, "client_ip", "-"),
            # An explicit key makes replay safe regardless of policy. Without
            # one, `replay_maybe_sent` decides — permissive in the demo, refuse
            # in production.
            idempotent=bool(key) or replay_maybe_sent,
            idempotency_key=key,
        )

    def upstream_headers(self, region_id: str) -> dict[str, str]:
        out = dict(self.headers)
        out["x-request-id"] = self.request_id
        out["x-mrfp-attempt-region"] = region_id
        # Forwarded so the upstream can dedupe a replay we could not avoid.
        if self.idempotency_key:
            out["idempotency-key"] = self.idempotency_key
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
            upstream_headers=passthrough_response_headers(resp.headers),
        )

    @classmethod
    def passthrough(cls, resp: httpx.Response, region: str, attempts: list[Attempt]) -> ProxyResult:
        """Hand an upstream response straight back without treating it as a failure.

        Used for 4xx (the caller is at fault, the region is fine) and for a
        deliberate load shed, where the upstream's own Retry-After is the most
        useful thing the client can receive.
        """
        return cls(
            status=resp.status_code,
            body=resp.content,
            media_type=resp.headers.get("content-type", "application/json"),
            region=region,
            attempts=attempts,
            upstream_headers=passthrough_response_headers(resp.headers),
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
        # Upstream headers FIRST, so the proxy's own X-* always win a collision
        # and a region cannot forge its own X-Served-By-Region.
        headers = dict(self.upstream_headers)
        headers.update(self.headers(primary_region=primary_region))
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
