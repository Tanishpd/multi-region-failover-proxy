"""The calibration maths, and the gate it drives.

Runs with the Docker daemon off: the probing is separated from the fitting and
the deciding so the parts that carry the judgement can be tested without a
stack. (ADR-028)
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from chaos.calibrate import (  # noqa: E402
    HEADROOM,
    RegionProfile,
    Step,
    check,
    derive,
    serviceable_tokens,
)

pytestmark = pytest.mark.unit


def profile(ttft: float, per_token: float, *, region: str = "r") -> RegionProfile:
    """Synthesise a region whose latency is exactly ttft + n * per_token."""
    steps = []
    for tokens in (8, 32, 128, 512):
        latency = ttft + tokens * per_token
        steps.append(Step(max_tokens=tokens, samples=[latency] * 20))
    return RegionProfile(region=region, backend="synthetic", model="m", steps=steps)


def test_the_fit_recovers_the_generating_parameters() -> None:
    """If the estimator cannot recover a curve it was handed exactly, every
    number downstream of it is decoration."""
    prof = profile(150.0, 0.25)
    assert prof.slope_ms_per_token == pytest.approx(0.25, abs=1e-6)
    assert prof.ttft_ms == pytest.approx(150.0, abs=1e-6)


def test_a_flat_backend_yields_a_zero_slope() -> None:
    """The pre-ADR-028 mock. A zero slope is the signature of a backend whose
    cost does not depend on the work asked of it — which is why no timeout
    could be derived from it."""
    assert profile(150.0, 0.0).slope_ms_per_token == pytest.approx(0.0, abs=1e-9)


def test_serviceable_tokens_finds_the_capability_ceiling() -> None:
    """ap-southeast-1's real shape: 168ms TTFT, 0.25ms/token, 200ms timeout."""
    prof = profile(168.0, 0.25)
    assert serviceable_tokens(prof, 200) == pytest.approx(128, abs=2)
    # The same region is unconstrained once the timeout is production-sized.
    assert serviceable_tokens(prof, 30_000) > 512


def test_a_region_slower_than_the_timeout_can_serve_nothing() -> None:
    """Not negative. A negative ceiling would sort as 'smallest' and could read
    as the most capable region in any comparison that forgets to clamp."""
    assert serviceable_tokens(profile(500.0, 0.25), 200) == 0


def test_a_flat_backend_has_no_token_ceiling() -> None:
    assert serviceable_tokens(profile(50.0, 0.0), 200) > 10**6


def test_derive_uses_the_worst_region_not_the_average() -> None:
    """A timeout sized for the mean fails every request to the slow region."""
    fast, slow = profile(20.0, 0.25, region="fast"), profile(168.0, 0.25, region="slow")
    derived = derive([fast, slow], attempts=2, connect_ms=100)
    worst = slow.worst_p99
    assert derived["read_timeout_ms"] == int(worst * HEADROOM) + 1
    assert derived["total_budget_ms"] == 2 * (100 + derived["read_timeout_ms"])


def test_the_stream_gap_floor_holds_at_one_second() -> None:
    """Below ~1s, scheduler jitter on a loaded host kills healthy streams, and
    the failure this bounds — a region that stopped producing — is never a
    sub-second event."""
    prof = profile(20.0, 0.25)
    prof.stream_gaps_ms = [0.4] * 50
    assert derive([prof], attempts=2, connect_ms=100)["stream_inter_chunk_timeout_s"] == 1.0


def test_check_fails_a_timeout_below_the_measurement() -> None:
    derived = derive([profile(168.0, 0.25)], attempts=2, connect_ms=100)
    problems = check(derived, {"read_timeout_ms": 50})
    assert problems and "below the measured requirement" in problems[0]


def test_check_passes_a_timeout_above_the_measurement() -> None:
    derived = derive([profile(168.0, 0.25)], attempts=2, connect_ms=100)
    assert check(derived, {"read_timeout_ms": 30_000}) == []


def test_check_flags_a_token_ceiling_below_the_api_limit() -> None:
    """The gate that would have caught the 503-from-a-healthy-region case."""
    derived = derive([profile(168.0, 0.25)], attempts=2, connect_ms=100)
    derived["region_ceilings"] = [
        {"region": "ap-southeast-1", "serviceable_max_tokens": 122, "schema_max_tokens": 512}
    ]
    problems = check(derived, {"read_timeout_ms": 30_000})
    assert any("503 from a healthy region" in p for p in problems)


def test_check_is_silent_when_every_region_covers_the_api_limit() -> None:
    derived = derive([profile(20.0, 0.25)], attempts=2, connect_ms=100)
    derived["region_ceilings"] = [
        {"region": "us-east-1", "serviceable_max_tokens": 512, "schema_max_tokens": 512}
    ]
    assert check(derived, {"read_timeout_ms": 30_000}) == []
