"""A mock cloud region serving fake model inference.

One image runs as all three of us-east-1-endpoint, eu-west-1-endpoint and
ap-southeast-1-endpoint — they differ only by environment. docs/CONTRACT.md
section 3 owns the wire contract.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from contextlib import asynccontextmanager
from typing import Annotated, Literal

from fastapi import FastAPI, Header, HTTPException, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app import chaos, inference, upstream
from app.settings import settings


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    # Only does anything when the real-upstream passthrough is enabled; the
    # client is built lazily so the mock path never constructs one.
    await upstream.aclose()


app = FastAPI(
    title=f"mrfp-region:{settings.region_id}",
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)


class InferRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=8192)
    max_tokens: int = Field(default=16, ge=1, le=512)
    seed: int | None = None
    stream: bool = False


class ChaosRequest(BaseModel):
    mode: Literal["slow", "error", "blackhole", "truncate"]
    ms: int = Field(default=0, ge=0, le=60_000)


def _region_headers() -> dict[str, str]:
    return {"X-Region-Id": settings.region_id, "X-Region-Zone": settings.region_zone}


async def _simulate_latency(max_tokens: int) -> None:
    """TTFT plus jitter plus generation, plus any injected `slow` penalty.

    The `max_tokens` term is what makes this a model of inference rather than a
    constant. Without it every response costs the same regardless of how much
    work was requested, and no measurement of this stack can justify a read
    timeout larger than the constant — which is exactly how the prod profile's
    timeout ended up being a guess. (ADR-028)
    """
    delay_ms = settings.base_latency_ms + random.uniform(0, settings.jitter_ms)
    delay_ms += max_tokens * settings.per_token_ms
    if chaos.state.mode == "slow":
        delay_ms += chaos.state.ms
    await asyncio.sleep(delay_ms / 1000.0)


async def _token_stream(body: InferRequest):
    """SSE frames, one per token, with a [DONE] sentinel.

    Under the `truncate` chaos mode the generator RAISES partway through rather
    than returning, so uvicorn abandons the chunked framing without writing the
    terminal chunk — which is what a real upstream dying mid-response looks
    like. Returning early would send a well-formed but short body, i.e. tell the
    client it received a complete answer.
    """
    text = inference.complete(body.prompt, body.max_tokens, body.seed)
    tokens = text.split()
    cut = max(1, len(tokens) // 3)

    for index, tok in enumerate(tokens):
        if chaos.state.mode == "truncate" and index == cut:
            raise RuntimeError("chaos: truncating the stream mid-body")
        yield f"data: {json.dumps({'token': tok, 'index': index})}\n\n".encode()
        await asyncio.sleep(settings.stream_inter_token_ms / 1000.0)

    yield b"data: [DONE]\n\n"


@app.post("/v1/infer")
async def infer(body: InferRequest, response: Response):
    if chaos.state.mode == "blackhole":
        await chaos.blackhole()  # never returns

    started = time.perf_counter()

    if settings.upstream_enabled:
        # The stream branch must come FIRST. Falling through to the buffered
        # path returned a 200 with content-length to a client that asked for
        # SSE — no error, wrong content type, and `make calibrate` silently
        # measured zero chunks.
        if body.stream:
            return StreamingResponse(
                upstream.stream_complete(body.prompt, body.max_tokens, body.seed),
                media_type="text/event-stream",
                headers={**_region_headers(), "x-stream": "1", "x-accel-buffering": "no"},
            )
        return await _infer_upstream(body, response, started)

    await _simulate_latency(body.max_tokens)

    if body.stream and chaos.state.mode != "error":
        return StreamingResponse(
            _token_stream(body),
            media_type="text/event-stream",
            headers={**_region_headers(), "x-stream": "1", "x-accel-buffering": "no"},
        )

    if chaos.state.mode == "error":
        raise HTTPException(
            status_code=503,
            detail=f"chaos: {settings.region_id} is injecting failures",
            headers=_region_headers(),
        )

    text = inference.complete(body.prompt, body.max_tokens, body.seed)
    response.headers.update(_region_headers())
    return {
        "id": inference.completion_id(body.prompt, body.max_tokens, body.seed),
        "region": settings.region_id,
        "zone": settings.region_zone,
        "model": settings.model_name,
        "completion": text,
        "usage": {
            "prompt_tokens": inference.token_count(body.prompt),
            "completion_tokens": body.max_tokens,
        },
        "latency_ms": round((time.perf_counter() - started) * 1000, 2),
    }


async def _infer_upstream(body: InferRequest, response: Response, started: float) -> dict:
    """Forward to a real model. No simulated latency — that is the whole point."""
    try:
        text, usage = await upstream.complete(body.prompt, body.max_tokens, body.seed)
    except upstream.UpstreamError as exc:
        # 502, not 503: 503 is what `chaos error` injects, and conflating "the
        # real model failed" with "I was told to fail" would make a calibration
        # run against a flaky provider look like an injected fault.
        raise HTTPException(status_code=502, detail=str(exc), headers=_region_headers()) from exc

    response.headers.update(_region_headers())
    return {
        "id": inference.completion_id(body.prompt, body.max_tokens, body.seed),
        "region": settings.region_id,
        "zone": settings.region_zone,
        "model": settings.upstream_model,
        "completion": text,
        "usage": usage,
        "latency_ms": round((time.perf_counter() - started) * 1000, 2),
    }


@app.get("/healthz")
async def healthz() -> Response:
    """Honest about injected faults.

    A region in `error` or `blackhole` mode reports itself unhealthy, so
    `docker compose ps` agrees with reality. Note this means a chaos run will
    show a region as unhealthy while the proxy correctly routes around it —
    that is the system working, not breaking. docs/RUNBOOK.md section 4.
    """
    if chaos.state.mode in {"error", "blackhole"}:
        return Response(
            content=f'{{"status":"unhealthy","chaos":"{chaos.state.mode}"}}',
            status_code=503,
            media_type="application/json",
            headers=_region_headers(),
        )
    return Response(
        content='{"status":"ok"}',
        media_type="application/json",
        headers=_region_headers(),
    )


@app.get("/")
async def metadata() -> dict[str, object]:
    return {
        "region": settings.region_id,
        "zone": settings.region_zone,
        # The model that will actually answer. Reporting `model_name` here while
        # passthrough is on would stamp a REAL measurement with the mock's name
        # in the calibration report — the precise mislabelling the `backend`
        # field exists to prevent.
        "model": settings.upstream_model if settings.upstream_enabled else settings.model_name,
        "base_latency_ms": settings.base_latency_ms,
        "jitter_ms": settings.jitter_ms,
        "per_token_ms": settings.per_token_ms,
        "stream_inter_token_ms": settings.stream_inter_token_ms,
        # `make calibrate` stamps this into the report. A derived timeout is
        # only meaningful next to a statement of what was on the other end —
        # a number measured against the mock must never be mistaken for one
        # measured against a real model. (ADR-028)
        "backend": "upstream" if settings.upstream_enabled else "mock",
        "chaos": chaos.state.as_dict(),
    }


def _require_token(token: str | None) -> None:
    if token != settings.admin_token:
        raise HTTPException(status_code=403, detail="bad or missing X-Admin-Token")


@app.post("/admin/chaos")
async def set_chaos(
    body: ChaosRequest,
    x_admin_token: Annotated[str | None, Header()] = None,
) -> dict[str, object]:
    _require_token(x_admin_token)
    chaos.state.mode = body.mode
    chaos.state.ms = body.ms
    return chaos.state.as_dict()


@app.get("/admin/chaos")
async def get_chaos() -> dict[str, object]:
    return chaos.state.as_dict()


@app.delete("/admin/chaos")
async def clear_chaos(
    x_admin_token: Annotated[str | None, Header()] = None,
) -> dict[str, object]:
    _require_token(x_admin_token)
    chaos.state.mode = None
    chaos.state.ms = 0
    return chaos.state.as_dict()
