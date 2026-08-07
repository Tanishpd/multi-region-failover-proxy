"""Phase 1: the region image and compose topology honour the frozen contract.

These parse docker-compose.yml and the region source rather than talking to a
daemon, so they run with Docker stopped. Several encode traps that are invisible
until a chaos run is already in flight.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
DOCKERFILE = (ROOT / "region" / "Dockerfile").read_text()

pytestmark = pytest.mark.unit


def test_compose_project_name_is_pinned() -> None:
    """The directory name has spaces and an '&'; the derived name would mangle."""
    assert COMPOSE["name"] == "mrfp"


def test_the_three_brief_container_names_exist() -> None:
    """Brief requirement 1. The chaos script runs `docker stop us-east-1-endpoint`."""
    names = {s.get("container_name") for s in COMPOSE["services"].values()}
    assert {
        "us-east-1-endpoint",
        "eu-west-1-endpoint",
        "ap-southeast-1-endpoint",
    } <= names, "the brief's container names are load-bearing"
    assert "redis" in names, "the brief requires a Redis container alongside"


def test_redis_publishes_on_6380_never_6379() -> None:
    """A native redis-server with real data owns 6379 on this machine. (ADR-004)"""
    ports = COMPOSE["services"]["redis"]["ports"]
    assert ports == ["127.0.0.1:6380:6379"]
    for spec in ports:
        host_port = spec.split(":")[1]
        assert host_port != "6379", "publishing on 6379 would collide with the native Redis"


def test_regions_never_restart_automatically() -> None:
    """A policy would resurrect a CRASHED region mid-run and wreck the timeline.

    (Policies do not fire after a manual `docker stop`, so the chaos stop itself
    is safe either way — but a crash under `on-failure` is not.) (ADR-011)
    """
    for name in ("us-east-1-endpoint", "eu-west-1-endpoint", "ap-southeast-1-endpoint"):
        assert COMPOSE["services"][name]["restart"] == "no"


def test_stop_grace_period_is_not_zero() -> None:
    """0s would make `docker stop` behave as `docker kill`.

    That changes which exception the client sees — RST on a pooled socket rather
    than a clean close — so the failure taxonomy would silently shift.
    """
    for name in ("us-east-1-endpoint", "eu-west-1-endpoint", "ap-southeast-1-endpoint"):
        assert COMPOSE["services"][name]["stop_grace_period"] == "2s"


def test_uvicorn_disables_graceful_shutdown() -> None:
    """The single most consequential flag in the region image.

    Without it, uvicorn's shutdown waits on the proxy's warm keep-alive sockets,
    so `docker stop` blocks for the full grace period WHILE STILL SERVING 200s.
    The demo then shows no failover and the router looks broken. (ADR-011)

    Measured with the flag present: `docker stop` returns in ~430 ms.
    """
    assert "--timeout-graceful-shutdown" in DOCKERFILE
    assert re.search(r'"--timeout-graceful-shutdown",\s*"0"', DOCKERFILE)


def test_healthcheck_does_not_use_curl() -> None:
    """python:*-slim ships no curl.

    A curl-based healthcheck marks every service unhealthy, and `up --wait`
    then hangs until its timeout with no useful error. Checks the instructions
    only — comments are free to explain why curl is absent.
    """
    instructions = "\n".join(
        line for line in DOCKERFILE.splitlines() if not line.lstrip().startswith("#")
    )
    assert "curl" not in instructions.lower()
    assert "urllib.request" in instructions


def test_no_platform_pin_anywhere() -> None:
    """This host is arm64; emulation jitter would make a 200 ms threshold flap."""
    raw = (ROOT / "docker-compose.yml").read_text()
    assert "linux/amd64" not in raw
    assert "platform:" not in raw


def test_shared_region_env_is_a_committed_env_file_not_an_anchor() -> None:
    """YAML anchors shallow-merge.

    A per-service `environment:` block replaces the anchor's map wholesale, so
    shared keys defined in the anchor would vanish from every service that sets
    even one variable of its own — which all three do. env_file merges per-key.

    It must also be committed: `env_file:` pointing at an untracked path is a
    hard Compose error on a fresh checkout.
    """
    svc = COMPOSE["services"]["us-east-1-endpoint"]
    assert svc["env_file"] == ["./region/region.env"]
    assert (ROOT / "region" / "region.env").exists()

    # The per-service override survived the merge...
    assert svc["environment"]["MRFP_REGION_ID"] == "us-east-1"
    # ...and so did the shared file's contents, because they are not in the anchor.
    shared = (ROOT / "region" / "region.env").read_text()
    assert "MRFP_ADMIN_TOKEN=" in shared


def test_region_latencies_are_ordered_by_priority() -> None:
    """us-east fast, ap-southeast slow — so the priority order means something.

    Without distinct latencies, failover has no measurable cost and the circuit
    breaker has nothing to optimise.
    """
    latency = {
        name: int(COMPOSE["services"][name]["environment"]["MRFP_BASE_LATENCY_MS"])
        for name in (
            "us-east-1-endpoint",
            "eu-west-1-endpoint",
            "ap-southeast-1-endpoint",
        )
    }
    assert (
        latency["us-east-1-endpoint"]
        < latency["eu-west-1-endpoint"]
        < latency["ap-southeast-1-endpoint"]
    )


def test_compose_matches_the_region_registry() -> None:
    """config/regions.yaml is the source of truth; compose must not drift."""
    registry = yaml.safe_load((ROOT / "config" / "regions.yaml").read_text())
    for region in registry["regions"]:
        svc = COMPOSE["services"][region["container"]]
        assert svc["container_name"] == region["container"]
        assert svc["environment"]["MRFP_REGION_ID"] == region["id"]
        assert svc["environment"]["MRFP_REGION_ZONE"] == region["zone"]
        assert int(svc["environment"]["MRFP_BASE_LATENCY_MS"]) == region["base_latency_ms"]
        assert f"127.0.0.1:{region['host_port']}:8080" in svc["ports"]


def test_inference_is_deterministic_and_region_independent() -> None:
    """Why replaying a POST to a second region is safe.

    All three regions return byte-identical output for identical input, so a
    failover cannot change what the caller sees. Verified live across all three
    containers; this pins the property in the unit suite. (ADR-006)
    """
    import sys

    sys.path.insert(0, str(ROOT / "region"))
    from app import inference

    a = inference.complete("hello", 6, None)
    b = inference.complete("hello", 6, None)
    assert a == b
    assert inference.complete("hello", 6, 42) != a, "seed must change the output"
    assert inference.completion_id("hello", 6, None) == inference.completion_id("hello", 6, None)


def test_blackhole_parks_without_a_timer() -> None:
    """A blackholed region holds one hung task per in-flight request.

    Waiting on an Event that is never set parks each task with no timer; a
    sleep loop would wake all of them periodically for nothing, and one long
    sleep does the same thing less often. Both still cancel cleanly, so this is
    about not burning wakeups under a flood.
    """
    src = (ROOT / "region" / "app" / "chaos.py").read_text()
    assert "asyncio.Event().wait()" in src
    assert not re.search(r"asyncio\.sleep\(", src), "no timer in the blackhole path"
