"""The failover loop, driven by httpx.MockTransport. No Docker, no sockets.

Every failure mode in docs/ARCHITECTURE.md section 4 is reproducible here by
raising the corresponding httpx exception, which is why this suite can assert
the router's behaviour without any of it being a container.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "proxy"))

from app.config import Settings  # noqa: E402
from app.failures import FailureClass, classify  # noqa: E402
from app.regions import RegionRegistry  # noqa: E402
from app.router import ResilientRouter  # noqa: E402
from app.schemas import ProxyRequest  # noqa: E402

pytestmark = pytest.mark.unit

REGIONS_FILE = str(ROOT / "config" / "regions.yaml")


class FakeClient:
    """Stands in for RegionClient, driven by a per-region behaviour callable."""

    def __init__(self, region_id: str, behaviour: dict) -> None:
        self.region_id = region_id
        self.behaviour = behaviour
        self.calls = 0
        self.bodies: list[bytes] = []

    async def send(self, method, path, *, content, headers, timeout_s=None):
        self.calls += 1
        self.bodies.append(content)
        action = self.behaviour.get(self.region_id, 200)

        if isinstance(action, BaseException):
            raise action
        if callable(action):
            return await action(self)
        if isinstance(action, int):
            return httpx.Response(
                action,
                json={"region": self.region_id, "completion": "ok"},
                request=httpx.Request(method, f"http://{self.region_id}{path}"),
            )
        raise AssertionError(f"unknown behaviour {action!r}")

    async def aclose(self) -> None:  # pragma: no cover
        pass


def build_router(behaviour: dict, **overrides) -> ResilientRouter:
    settings = Settings(regions_file=REGIONS_FILE, **overrides)
    registry = RegionRegistry.load(REGIONS_FILE)
    clients = {s.id: FakeClient(s.id, behaviour) for s in registry.ordered()}
    return ResilientRouter(registry, clients, settings, breaker=None)


def make_request(body: bytes = b'{"prompt":"x"}') -> ProxyRequest:
    return ProxyRequest(
        method="POST",
        path="/v1/infer",
        body=body,
        headers={"content-type": "application/json"},
        request_id="test-rid",
        client_ip="127.0.0.1",
    )


async def test_prefers_us_east_when_healthy() -> None:
    router = build_router({})
    result = await router.route(make_request())
    assert result.status == 200
    assert result.region == "us-east-1"
    assert len(result.attempts) == 1


async def test_falls_back_on_connect_error() -> None:
    """`docker stop`: DNS withdrawn, fast fail. Measured at 2-9 ms live."""
    router = build_router({"us-east-1": httpx.ConnectError("[Errno -2] Name or service not known")})
    result = await router.route(make_request())
    assert result.status == 200
    assert result.region == "eu-west-1"
    assert result.attempts[0].code == "connect_error"
    assert result.attempts[0].fclass is FailureClass.NOT_SENT


async def test_falls_back_on_read_timeout() -> None:
    """`docker pause` / blackhole: connect succeeds, then silence.

    The only fault that actually exercises the 200 ms read timeout — measured
    live at 203-211 ms, versus 2-9 ms for `docker stop`. (ADR-012)
    """
    router = build_router({"us-east-1": httpx.ReadTimeout("timed out")})
    result = await router.route(make_request())
    assert result.region == "eu-west-1"
    assert result.attempts[0].code == "read_timeout"
    assert result.attempts[0].fclass is FailureClass.MAYBE_SENT


async def test_falls_back_on_remote_protocol_error() -> None:
    """A warm keep-alive pool turns a killed upstream into this, not a timeout."""
    router = build_router({"us-east-1": httpx.RemoteProtocolError("peer closed")})
    result = await router.route(make_request())
    assert result.region == "eu-west-1"
    assert result.attempts[0].code == "protocol_error"


async def test_falls_back_on_5xx() -> None:
    router = build_router({"us-east-1": 503})
    result = await router.route(make_request())
    assert result.region == "eu-west-1"
    assert result.attempts[0].code == "http_503"
    assert result.attempts[0].fclass is FailureClass.RESPONSE_ERROR


async def test_does_not_failover_on_4xx() -> None:
    """The region is alive; the CALLER is at fault.

    Retrying would turn one client error into three units of work and report a
    phantom outage. Verified live: a malformed body returns 422 with
    X-Attempts: 1.
    """
    router = build_router({"us-east-1": 422})
    result = await router.route(make_request())
    assert result.status == 422
    assert result.region == "us-east-1"
    assert len(result.attempts) == 1
    assert router.clients["eu-west-1"].calls == 0


async def test_cascades_to_ap_southeast() -> None:
    """Proves the router is a loop over the priority list, not an if/else."""
    router = build_router(
        {
            "us-east-1": httpx.ConnectError("gone"),
            "eu-west-1": httpx.ConnectError("gone"),
        }
    )
    result = await router.route(make_request())
    assert result.region == "ap-southeast-1"
    assert len(result.attempts) == 3


async def test_returns_503_not_500_when_all_down() -> None:
    router = build_router(
        {
            "us-east-1": httpx.ConnectError("gone"),
            "eu-west-1": httpx.ConnectError("gone"),
            "ap-southeast-1": httpx.ConnectError("gone"),
        }
    )
    result = await router.route(make_request())
    assert result.status == 503
    assert result.region is None
    assert len(result.attempts) == 3
    # The 503 must explain itself, or it is useless during an outage.
    headers = result.headers(primary_region="us-east-1")
    assert headers["X-Attempts"] == "3"
    assert "us-east-1:connect_error" in headers["X-Failover-Path"]


async def test_body_is_replayable_across_attempts() -> None:
    """The reason bodies are buffered rather than streamed.

    A streamed body is consumed by the first attempt; the second region would
    receive an empty request. (ADR-006)
    """
    body = b'{"prompt":"replay me"}'
    router = build_router({"us-east-1": httpx.ConnectError("gone")})
    await router.route(make_request(body))
    assert router.clients["us-east-1"].bodies == [body]
    assert router.clients["eu-west-1"].bodies == [body]


async def test_emits_upstream_failover_log_line(caplog) -> None:
    """Brief requirement 4's logging clause, guarded.

    If this stops firing, the demo's headline evidence disappears — which is
    also what chaos assertion A13 checks at runtime.
    """
    import logging

    caplog.set_level(logging.WARNING)
    router = build_router({"us-east-1": httpx.ConnectError("boom")})
    await router.route(make_request())

    failovers = [r for r in caplog.records if getattr(r, "event", "") == "upstream_failover"]
    assert len(failovers) == 1
    rec = failovers[0]
    assert rec.from_region == "us-east-1"
    assert rec.to_region == "eu-west-1"
    assert rec.error_class == "ConnectError"
    assert rec.request_id == "test-rid"


async def test_no_failover_log_line_for_the_last_region(caplog) -> None:
    """There is nowhere to fail over TO, so claiming a failover would be a lie."""
    import logging

    caplog.set_level(logging.WARNING)
    router = build_router(
        {
            "us-east-1": httpx.ConnectError("gone"),
            "eu-west-1": httpx.ConnectError("gone"),
            "ap-southeast-1": httpx.ConnectError("gone"),
        }
    )
    await router.route(make_request())
    failovers = [r for r in caplog.records if getattr(r, "event", "") == "upstream_failover"]
    assert [r.from_region for r in failovers] == ["us-east-1", "eu-west-1"]


async def test_cancellation_does_not_record_a_failure() -> None:
    """CancelledError is a BaseException and means the CLIENT hung up.

    Catching it would record a phantom failure against a healthy region and,
    under a flood where clients time out, could trip a breaker on a region that
    never did anything wrong.
    """

    async def hang(_client):
        raise asyncio.CancelledError()

    router = build_router({"us-east-1": hang})
    with pytest.raises(asyncio.CancelledError):
        await router.route(make_request())

    assert router.health["us-east-1"].ok is None, "no health verdict from a cancelled request"
    assert router.clients["eu-west-1"].calls == 0, "must not fail over on client hangup"


async def test_budget_stops_the_walk_before_a_doomed_attempt() -> None:
    """Better a fast 503 than a fourth attempt that cannot finish in time."""
    router = build_router(
        {
            "us-east-1": httpx.ConnectError("gone"),
            "eu-west-1": httpx.ConnectError("gone"),
            "ap-southeast-1": httpx.ConnectError("gone"),
        },
        total_budget_ms=1,
    )
    result = await router.route(make_request())
    assert result.status == 503
    assert result.attempts[-1].code == "budget_exhausted"
    assert result.attempts[-1].sent is False


async def test_passive_health_feeds_readyz() -> None:
    """No poller anywhere: health is only what real requests learned. (ADR-009)"""
    router = build_router({"us-east-1": httpx.ConnectError("gone")})
    assert router.usable_regions() == ["us-east-1", "eu-west-1", "ap-southeast-1"]

    await router.route(make_request())
    assert router.health["us-east-1"].ok is False
    assert router.health["eu-west-1"].ok is True
    assert "us-east-1" not in router.usable_regions()
    # ap-southeast was never tried, so it stays usable: with passive detection,
    # absence of evidence is not evidence of failure.
    assert router.health["ap-southeast-1"].ok is None
    assert "ap-southeast-1" in router.usable_regions()


def test_connect_timeout_is_not_a_connect_error() -> None:
    """The httpx hierarchy trap, pinned.

    Catching only ConnectError silently misses every timeout class.
    """
    assert not issubclass(httpx.ConnectTimeout, httpx.ConnectError)
    assert issubclass(httpx.ConnectTimeout, httpx.TimeoutException)
    assert classify(httpx.ConnectTimeout("x")) == (FailureClass.NOT_SENT, "connect_timeout")
    assert classify(httpx.ConnectError("x")) == (FailureClass.NOT_SENT, "connect_error")


def test_unknown_exceptions_are_reraised_not_blamed_on_a_region() -> None:
    """An unrecognised exception is OUR bug.

    Counting it as an upstream failure would blame a healthy region for a defect
    in this process and could trip its breaker on unrelated evidence.
    """
    with pytest.raises(ValueError):
        classify(ValueError("a bug in the proxy"))
