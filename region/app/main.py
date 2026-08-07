"""A mock cloud region serving fake model inference.

One image runs as all three of us-east-1-endpoint, eu-west-1-endpoint and
ap-southeast-1-endpoint — they differ only by environment. docs/CONTRACT.md
section 3 owns the wire contract.
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import Annotated, Literal

from fastapi import FastAPI, Header, HTTPException, Response
from pydantic import BaseModel, Field

from app import chaos, inference
from app.settings import settings

app = FastAPI(title=f"mrfp-region:{settings.region_id}", docs_url=None, redoc_url=None)


class InferRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=8192)
    max_tokens: int = Field(default=16, ge=1, le=512)
    seed: int | None = None


class ChaosRequest(BaseModel):
    mode: Literal["slow", "error", "blackhole"]
    ms: int = Field(default=0, ge=0, le=60_000)


def _region_headers() -> dict[str, str]:
    return {"X-Region-Id": settings.region_id, "X-Region-Zone": settings.region_zone}


async def _simulate_latency() -> None:
    """Base latency plus jitter, plus any injected `slow` penalty."""
    delay_ms = settings.base_latency_ms + random.uniform(0, settings.jitter_ms)
    if chaos.state.mode == "slow":
        delay_ms += chaos.state.ms
    await asyncio.sleep(delay_ms / 1000.0)


@app.post("/v1/infer")
async def infer(body: InferRequest, response: Response) -> dict[str, object]:
    if chaos.state.mode == "blackhole":
        await chaos.blackhole()  # never returns

    started = time.perf_counter()
    await _simulate_latency()

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
        "model": settings.model_name,
        "base_latency_ms": settings.base_latency_ms,
        "jitter_ms": settings.jitter_ms,
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
