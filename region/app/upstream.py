"""Optional passthrough to a real OpenAI-compatible endpoint.

WHY THIS EXISTS. Every latency number this stack produces is otherwise a
property of `asyncio.sleep`, so any timeout derived from it is circular: the
mock was configured to be fast, the measurement says it is fast, and the
timeout that follows says nothing about a real deployment. Pointing a region at
a real model makes `make calibrate` measure a real latency distribution, which
is the only version of that number worth configuring.

It is OFF unless `MRFP_UPSTREAM_BASE_URL` is set. The demo, the chaos matrix and
CI all keep using the deterministic mock, because a chaos run whose baseline
depends on someone else's inference queue is not a controlled experiment.

The region, not the proxy, owns this translation. The proxy speaks one schema to
all three regions and must not learn a second one — and a region IS the
deployment-specific edge in the real topology this models.
"""

from __future__ import annotations

import httpx

from app.settings import settings

# One client for the process, built lazily: a pool binds to the event loop on
# first use, so constructing it at import time attaches it to whichever loop
# happens to be current.
_client: httpx.AsyncClient | None = None


def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        headers = {"content-type": "application/json"}
        if settings.upstream_api_key:
            headers["authorization"] = f"Bearer {settings.upstream_api_key}"
        _client = httpx.AsyncClient(
            base_url=settings.upstream_base_url.rstrip("/"),
            timeout=httpx.Timeout(settings.upstream_timeout_s, connect=10.0),
            headers=headers,
        )
    return _client


async def aclose() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


class UpstreamError(RuntimeError):
    """The real endpoint failed. Surfaced as a 502 so the proxy fails over."""


async def complete(prompt: str, max_tokens: int, seed: int | None) -> tuple[str, dict]:
    """Return (completion_text, usage). Raises UpstreamError on any failure."""
    payload: dict[str, object] = {
        "model": settings.upstream_model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
    }
    if seed is not None:
        # Best effort. Not every provider honours it, and the ones that do call
        # it "best effort" too — so the cross-region response equality that the
        # mock guarantees does NOT hold on this path. docs/CONTRACT.md §3.
        payload["seed"] = seed

    try:
        response = await get_client().post("/v1/chat/completions", json=payload)
    except httpx.HTTPError as exc:
        raise UpstreamError(f"{type(exc).__qualname__}: {exc}") from exc

    if response.status_code >= 400:
        raise UpstreamError(f"upstream returned {response.status_code}: {response.text[:200]}")

    try:
        body = response.json()
        text = body["choices"][0]["message"]["content"] or ""
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise UpstreamError(f"unparseable upstream response: {type(exc).__qualname__}") from exc

    usage = body.get("usage") or {}
    return text, {
        "prompt_tokens": usage.get("prompt_tokens", 0),
        "completion_tokens": usage.get("completion_tokens", 0),
    }
