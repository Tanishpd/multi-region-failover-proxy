"""The streaming commit latch.

One rule under test: failover is possible until the first byte reaches the
client, and structurally impossible afterwards. Everything else here is a
consequence of it.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "proxy"))

from app.streaming import (  # noqa: E402
    DONE,
    Phase,
    StreamLease,
    StreamTruncated,
    error_frame,
    pump,
    response_headers,
)

pytestmark = pytest.mark.unit


class FakeCtx:
    def __init__(self) -> None:
        self.closed = False

    async def __aexit__(self, *exc) -> None:
        self.closed = True


def make_lease(first: bytes, rest, region: str = "us-east-1") -> StreamLease:
    ctx = FakeCtx()
    lease = StreamLease(region=region, response=None, first=first, context=ctx)

    async def gen():
        for item in rest:
            if isinstance(item, BaseException):
                raise item
            yield item

    lease._iterator = gen()
    return lease


async def collect(lease, **kw) -> tuple[list[bytes], BaseException | None]:
    out: list[bytes] = []
    try:
        async for chunk in pump(lease, request_id="rid", inter_chunk_timeout_s=1.0, **kw):
            out.append(chunk)
    except BaseException as exc:
        return out, exc
    return out, None


# --- the happy path ---------------------------------------------------------


async def test_a_clean_stream_ends_with_the_done_sentinel() -> None:
    lease = make_lease(b"data: a\n\n", [b"data: b\n\n", f"data: {DONE}\n\n".encode()])
    chunks, exc = await collect(lease)

    assert exc is None
    assert b"".join(chunks).endswith(f"data: {DONE}\n\n".encode())
    assert lease.phase is Phase.COMMITTED


async def test_the_upstream_context_is_always_released() -> None:
    """An unclosed streamed response holds its pool connection forever."""
    lease = make_lease(b"x", [])
    await collect(lease)
    assert lease.context.closed


async def test_the_context_is_released_even_when_truncated() -> None:
    lease = make_lease(b"x", [httpx.RemoteProtocolError("peer closed")])
    await collect(lease)
    assert lease.context.closed


# --- the commit latch -------------------------------------------------------


async def test_the_first_yield_commits() -> None:
    """Before the first byte the phase is PREROLL, which is what makes the
    entire time-to-first-byte window replayable."""
    lease = make_lease(b"first", [])
    assert lease.phase is Phase.PREROLL
    await collect(lease)
    assert lease.phase is Phase.COMMITTED


async def test_a_post_commit_failure_raises_rather_than_returning() -> None:
    """THE critical behaviour.

    Returning would let uvicorn write the terminal zero-length chunk, telling
    the client it received a complete answer. For a token stream that is the
    worst possible outcome: a silently truncated response that looks whole.
    """
    lease = make_lease(b"data: a\n\n", [httpx.RemoteProtocolError("peer closed")])
    chunks, exc = await collect(lease)

    assert isinstance(exc, StreamTruncated), "a truncated stream must abort the framing"
    body = b"".join(chunks)
    assert b"event: error" in body
    assert b'"complete": false' in body
    assert DONE.encode() not in body, "a truncated stream must never carry the sentinel"


async def test_truncation_is_signalled_twice() -> None:
    """Two independent signals, because either alone is missable.

    An SSE client that ignores unknown event types still sees no [DONE]; an
    HTTP/1.1-correct client sees an incomplete body from the aborted framing.
    """
    lease = make_lease(b"data: a\n\n", [httpx.ReadError("boom")])
    chunks, exc = await collect(lease)

    assert b"event: error" in b"".join(chunks)  # in-band
    assert isinstance(exc, StreamTruncated)  # framing


async def test_emitted_bytes_are_tracked_for_the_incident_log() -> None:
    lease = make_lease(b"12345", [b"678", httpx.ReadError("boom")])
    await collect(lease)
    assert lease.emitted == 8


# --- timers -----------------------------------------------------------------


async def test_a_stalled_upstream_truncates() -> None:
    async def slow():
        yield b"data: a\n\n"
        await asyncio.sleep(5)  # longer than the inter-chunk timeout
        yield b"never"

    ctx = FakeCtx()
    lease = StreamLease(region="us-east-1", response=None, first=b"first", context=ctx)
    lease._iterator = slow()

    chunks, exc = await collect(lease)
    assert isinstance(exc, StreamTruncated)
    assert b"event: error" in b"".join(chunks)


async def test_a_slow_consumer_does_not_truncate_a_healthy_stream() -> None:
    """The timer wraps only the upstream READ, never the downstream yield.

    Wrapping the yield would make a slow CLIENT look like a dead region and
    truncate a perfectly healthy generation.
    """
    lease = make_lease(b"a", [b"b", b"c"])
    out = []
    async for chunk in pump(lease, request_id="rid", inter_chunk_timeout_s=0.2):
        await asyncio.sleep(0.3)  # a consumer far slower than the timeout
        out.append(chunk)
    assert out == [b"a", b"b", b"c"]


async def test_client_disconnect_is_not_an_error() -> None:
    """CancelledError means the client hung up; it needs no error frame."""
    lease = make_lease(b"a", [b"b"])
    agen = pump(lease, request_id="rid", inter_chunk_timeout_s=1.0)
    assert await anext(agen) == b"a"
    await agen.aclose()
    assert lease.context.closed


# --- the wire contract ------------------------------------------------------


def test_streaming_headers_declare_the_contract() -> None:
    lease = make_lease(b"", [])
    out = response_headers(lease, {"X-Served-By-Region": "us-east-1"})
    assert out["X-Stream"] == "1"
    assert out["X-Stream-Terminator"] == DONE
    # Without this an intermediary may buffer the whole stream and defeat the
    # entire time-to-first-byte design.
    assert out["X-Accel-Buffering"] == "no"
    assert out["X-Served-By-Region"] == "us-east-1"


def test_the_error_frame_is_parseable_sse() -> None:
    import json

    raw = error_frame("rid-1", "us-east-1", "peer closed").decode()
    assert raw.startswith("event: error\ndata: ")
    assert raw.endswith("\n\n")
    payload = json.loads(raw.split("data: ", 1)[1])
    assert payload["complete"] is False
    assert payload["region"] == "us-east-1"
    assert payload["request_id"] == "rid-1"


# --- profile safety ---------------------------------------------------------


def test_streaming_is_off_in_the_demo_profile() -> None:
    """The graded behaviour must be unchanged by streaming's PRESENCE.

    Off, not merely equivalent — the demo diff for this feature is zero
    behaviour.
    """
    import os

    from app.config import Settings

    saved = dict(os.environ)
    os.environ.pop("MRFP_STREAMING_ENABLED", None)
    try:
        assert (
            Settings(regions_file=str(ROOT / "config" / "regions.yaml")).streaming_enabled is False
        )
    finally:
        os.environ.clear()
        os.environ.update(saved)


def test_the_fourth_chaos_mode_is_in_the_contract() -> None:
    """CONTRACT wins over code (ADR-001), so `truncate` had to be added there."""
    contract = (ROOT / "docs" / "CONTRACT.md").read_text()
    assert "exactly four" in contract
    assert "`truncate`" in contract

    region = (ROOT / "region" / "app" / "main.py").read_text()
    for mode in ("slow", "error", "blackhole", "truncate"):
        assert f'"{mode}"' in region
