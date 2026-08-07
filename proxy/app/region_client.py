"""One httpx.AsyncClient per region.

Two decisions here are load-bearing enough to have their own tests.

ONE CLIENT PER REGION, not one shared client. httpx.Limits.max_connections is a
global cap across every host a client talks to. With a shared client, a single
blackholed region would occupy the whole pool and healthy regions would start
failing with PoolTimeout — the exact opposite of what a failover proxy is for.

LIMITS GO ON THE TRANSPORT. `limits=` is consumed only by
`Client._init_transport()`. Passing an explicit `transport=` discards it
silently and you get httpx's defaults (100/20/5s) while your config says
otherwise. There is no warning. `assert_pool_limits()` exists so a future
refactor cannot re-break this quietly.
"""

from __future__ import annotations

import asyncio

import httpx

from app.regions import RegionSpec


class RegionClient:
    def __init__(self, spec: RegionSpec, *, connect_ms: int, read_ms: int, pool_ms: int) -> None:
        self.spec = spec
        self._connect_ms = connect_ms
        self._read_ms = read_ms
        self._pool_ms = pool_ms
        self._client = self._build()

    def _build(self) -> httpx.AsyncClient:
        limits = httpx.Limits(
            max_connections=self.spec.max_connections,
            max_keepalive_connections=self.spec.max_keepalive_connections,
            keepalive_expiry=self.spec.keepalive_expiry_s,
        )
        return httpx.AsyncClient(
            base_url=self.spec.base_url,
            timeout=httpx.Timeout(
                self._read_ms / 1000,  # 0.200 — the brief's number
                connect=self._connect_ms / 1000,
                pool=self._pool_ms / 1000,
            ),
            # limits= would be DISCARDED here because transport= is explicit.
            transport=httpx.AsyncHTTPTransport(limits=limits, retries=0),
            http2=False,
            follow_redirects=False,
        )

    @property
    def client(self) -> httpx.AsyncClient:
        return self._client

    def assert_pool_limits(self) -> None:
        """Fail loudly at boot if the bulkhead silently reverted to defaults."""
        pool = self._client._transport._pool
        actual = pool._max_connections
        if actual != self.spec.max_connections:
            raise RuntimeError(
                f"{self.spec.id}: pool max_connections is {actual}, expected "
                f"{self.spec.max_connections} — `limits=` was probably passed to "
                "AsyncClient instead of to the transport, and silently dropped."
            )

    async def send(
        self,
        method: str,
        path: str,
        *,
        content: bytes,
        headers: dict[str, str],
        timeout_s: float | None = None,
    ) -> httpx.Response:
        return await self._client.request(
            method,
            path,
            content=content,
            headers=headers,
            timeout=(
                httpx.Timeout(
                    timeout_s,
                    connect=min(self._connect_ms / 1000, timeout_s),
                    pool=self._pool_ms / 1000,
                )
                if timeout_s is not None
                else httpx.USE_CLIENT_DEFAULT
            ),
        )

    def stream(self, method: str, path: str, *, content: bytes, headers: dict[str, str]):
        """The httpx streaming context manager, unentered.

        Deliberately no timeout= here: a streamed response needs a
        time-to-first-byte bound and an inter-chunk bound, which are different
        numbers, and both are applied by app.streaming rather than by a single
        httpx read timeout that would kill a legitimately long generation.
        """
        return self._client.stream(
            method,
            path,
            content=content,
            headers=headers,
            timeout=httpx.Timeout(None, connect=self._connect_ms / 1000),
        )

    async def purge_pool(self, drain_after_s: float = 0.25) -> None:
        """Swap in a fresh client, then close the old one once attempts drain.

        Called when the breaker opens. Stale keep-alive sockets to a container
        that restarted are precisely what turn a dead upstream into
        RemoteProtocolError rather than a clean connect failure.

        The swap happens FIRST so new requests immediately get fresh
        connections; the old client is closed after a short delay so attempts
        already in flight finish rather than being aborted mid-response.
        """
        old = self._client
        self._client = self._build()
        await asyncio.sleep(drain_after_s)
        await old.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()
