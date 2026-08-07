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
    TOKEN_STEPS,
    RegionProfile,
    Step,
    check,
    derive,
    live_models,
    plan,
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


# --- the tool must not derive from nothing (ADR-028) ------------------------


def test_no_stream_samples_derives_NOTHING() -> None:
    """The tool reproducing the disease it treats.

    The first version applied a 1s floor unconditionally, so a run that
    collected zero chunks still printed a confident
    `MRFP_STREAM_INTER_CHUNK_TIMEOUT_S = 1.0` — a number with no measurement
    behind it, from the tool built to stop numbers having no measurement behind
    them. It surfaced the moment the regions were pointed at a real backend
    whose passthrough did not stream.
    """
    derived = derive([profile(20.0, 0.25)], attempts=2, connect_ms=100)
    assert derived["stream_inter_chunk_timeout_s"] is None
    assert derived["measured_stream_gap_p99_ms"] is None
    assert derived["stream_samples"] == 0


def test_an_unmeasured_requirement_cannot_fail_the_gate() -> None:
    """Gating on something never measured is how the tool would start inventing
    the numbers it exists to replace."""
    derived = derive([profile(20.0, 0.25)], attempts=2, connect_ms=100)
    assert check(derived, {"stream_inter_chunk_timeout_s": 0.001}) == []


def test_stream_samples_are_counted_when_present() -> None:
    prof = profile(20.0, 0.25)
    prof.stream_gaps_ms = [5.0] * 40
    derived = derive([prof], attempts=2, connect_ms=100)
    assert derived["stream_samples"] == 40
    assert derived["measured_stream_gap_p99_ms"] == 5.0


# --- do not spend someone else's money by accident --------------------------


def test_the_plan_counts_warmups() -> None:
    """One warm-up per step is discarded but still generated, still billed."""
    requests, tokens = plan(samples=10)
    assert requests == 3 * len(TOKEN_STEPS) * 11
    assert tokens == 3 * sum(t * 11 for t in TOKEN_STEPS)


def test_a_mock_backend_needs_no_confirmation() -> None:
    """The demo path must stay a single command with no prompt."""
    mock = RegionProfile(region="r", backend="mock", model="mock-echo-1", steps=[])
    assert live_models([mock]) == []


def test_a_live_backend_is_named_so_the_refusal_can_say_which() -> None:
    """A default run is 252 requests and ~43k generated tokens. Small in money,
    but it lands on someone else's rate limit, and it happened silently."""
    live = RegionProfile(region="r", backend="upstream", model="gpt-4o-mini", steps=[])
    mock = RegionProfile(region="q", backend="mock", model="mock-echo-1", steps=[])
    assert live_models([live, mock]) == ["gpt-4o-mini"]


def test_the_refusal_is_wired_and_exits_nonzero() -> None:
    body = (ROOT / "chaos" / "calibrate.py").read_text()
    assert "REFUSING" in body
    assert "if live and not args.yes:" in body
    assert "return 2" in body


# --- calibrating an idle backend under-sizes the timeout --------------------


def test_concurrency_defaults_to_one_and_is_documented_as_a_trap() -> None:
    """Concurrency 1 measures SERVICE latency, which is the right floor — and
    the wrong number for a shared backend. Calibrate a hosted endpoint while it
    is idle and the derived timeout is too small the moment it queues, which is
    exactly when it matters. The default stays 1 (reproducible, cheap); the
    docstring and --help both have to say why that is dangerous."""
    body = (ROOT / "chaos" / "calibrate.py").read_text()
    assert '"--concurrency",' in body
    assert "default=1," in body
    assert "under-sizes" in body


def test_the_sweep_binds_both_loop_variables() -> None:
    """ruff B023 caught this: `one()` closed over the loop's `step`, so it read
    whatever the loop had last assigned. Harmless only because the gather is
    awaited inside the same iteration — an accident that survives until someone
    moves the await."""
    body = (ROOT / "chaos" / "calibrate.py").read_text()
    assert "async def one(tokens: int = tokens, step: Step = step)" in body


def test_concurrency_is_recorded_in_the_report() -> None:
    """A latency number without the concurrency it was measured at is not
    reproducible, and two runs at different concurrency are not comparable."""
    body = (ROOT / "chaos" / "calibrate.py").read_text()
    assert '"concurrency": args.concurrency,' in body
    assert "| concurrency |" in body
