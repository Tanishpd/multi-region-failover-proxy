"""Streaming passthrough, and the one rule that makes it safe to fail over.

THE IDEA. Replayability is not a property of the request. It is a property of
how far the response has already travelled.

ADR-006 buffers request bodies precisely so a failed attempt can be replayed to
another region. A streamed RESPONSE breaks that symmetry: once a byte has
reached the client, there is no way to un-send it, so no second region can serve
the request. But everything before that first byte is free.

So the proxy pulls the upstream's headers AND its first chunk before writing
anything downstream. That makes the entire time-to-first-byte window replayable
— which covers the most likely real failure shape, an upstream that accepts the
connection, returns `200 text/event-stream`, and then dies before producing a
token.

    PREROLL    headers + first chunk received, nothing sent to the client
               -> a region that dies here is replaced INVISIBLY
    COMMITTED  at least one byte written downstream
               -> failover is structurally impossible; signal truncation instead

TRUNCATION IS SIGNALLED TWICE, on purpose. A post-commit failure emits an
in-band `event: error` frame and then RAISES, which stops uvicorn writing the
terminal zero-length chunk. An SSE client sees no `[DONE]`; an HTTP/1.1-correct
client sees an incomplete body. Either signal alone is missable — a client that
ignores unknown SSE event types would read a clean early return as a complete
answer, which is the worst possible outcome for a token stream.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import Enum

import httpx

from app.logging_setup import logx

DONE = "[DONE]"


class Phase(str, Enum):
    PREROLL = "preroll"
    COMMITTED = "committed"


class StreamTruncated(Exception):
    """A committed stream died mid-body. Not replayable, by definition."""


@dataclass
class StreamLease:
    """An upstream stream held open, with its first chunk already in hand."""

    region: str
    response: httpx.Response
    first: bytes
    context: object  # the httpx stream context manager, closed on release
    phase: Phase = Phase.PREROLL
    emitted: int = 0
    headers: dict[str, str] = field(default_factory=dict)

    async def release(self) -> None:
        with contextlib.suppress(Exception):
            await self.context.__aexit__(None, None, None)


def error_frame(request_id: str, region: str, reason: str) -> bytes:
    """The in-band half of the truncation signal."""
    payload = json.dumps(
        {
            "error": "upstream_stream_truncated",
            "detail": reason,
            "region": region,
            "request_id": request_id,
            "complete": False,
        }
    )
    return f"event: error\ndata: {payload}\n\n".encode()


async def open_stream(
    client,
    region: str,
    *,
    method: str,
    path: str,
    content: bytes,
    headers: dict[str, str],
    ttfb_timeout_s: float,
) -> StreamLease:
    """Open an upstream stream and pull its first chunk.

    Raises like a normal attempt, so the router's existing failure taxonomy
    classifies it unchanged — this is why preroll failover needs no special
    case in the failover loop.

    The TTFB timeout covers headers AND the first chunk together. Bounding only
    the headers would let an upstream return 200 instantly and then never
    produce a token, which is exactly the failure this window exists to catch.
    """
    ctx = client.stream(method, path, content=content, headers=headers)
    response = await ctx.__aenter__()

    try:
        if response.status_code >= 500 or response.status_code == 429:
            from app.failures import UpstreamStatusError

            await response.aread()
            raise UpstreamStatusError(response.status_code, response)

        async with asyncio.timeout(ttfb_timeout_s):
            iterator = response.aiter_bytes()
            first = await anext(iterator, b"")
    except BaseException:
        # Never leave the context open on a failed preroll: an unclosed
        # streamed response holds its pool connection forever.
        with contextlib.suppress(Exception):
            await ctx.__aexit__(None, None, None)
        raise

    lease = StreamLease(region=region, response=response, first=first, context=ctx)
    lease.headers = dict(response.headers)
    lease._iterator = iterator  # type: ignore[attr-defined]
    return lease


async def pump(
    lease: StreamLease,
    *,
    request_id: str,
    inter_chunk_timeout_s: float,
) -> AsyncIterator[bytes]:
    """Yield the response body downstream. The first yield commits the request."""
    iterator = lease._iterator  # type: ignore[attr-defined]

    try:
        lease.phase = Phase.COMMITTED
        lease.emitted += len(lease.first)
        logx("stream_committed", region=lease.region, request_id=request_id)
        yield lease.first

        while True:
            try:
                # The timer wraps ONLY the upstream read. Putting the yield
                # inside it would make a slow CLIENT look like a dead region and
                # truncate a perfectly healthy stream.
                async with asyncio.timeout(inter_chunk_timeout_s):
                    chunk = await anext(iterator)
            except StopAsyncIteration:
                return  # clean EOF: the terminal chunk is written normally
            lease.emitted += len(chunk)
            yield chunk

    except (httpx.HTTPError, TimeoutError) as exc:
        logx(
            "stream_truncated",
            level=logging.ERROR,
            region=lease.region,
            request_id=request_id,
            emitted_bytes=lease.emitted,
            error_class=type(exc).__qualname__,
        )
        yield error_frame(request_id, lease.region, str(exc) or type(exc).__name__)
        # RAISE, do not return. Returning here would let uvicorn write the
        # terminal zero-length chunk, telling the client it received a complete
        # answer — the worst possible outcome for a token stream.
        raise StreamTruncated(str(exc)) from exc

    # asyncio.CancelledError is deliberately NOT caught: it means the client
    # hung up, and a disconnected client needs no error frame.
    finally:
        await lease.release()


def response_headers(lease: StreamLease, base: dict[str, str]) -> dict[str, str]:
    out = dict(base)
    out["X-Stream"] = "1"
    out["X-Stream-Terminator"] = DONE
    # Without this an intermediary may buffer the whole stream, which defeats
    # the entire time-to-first-byte design.
    out["X-Accel-Buffering"] = "no"
    return out
