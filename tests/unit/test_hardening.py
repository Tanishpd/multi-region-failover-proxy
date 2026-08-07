"""Regressions for the exploitable defects found in the production audit.

Each test corresponds to something that was demonstrably exploitable against a
running stack, with the measurement in its docstring. The point is that these
cannot silently come back.

One lesson is encoded structurally rather than in any single assertion: the
original XFF test asserted the right property against a LOOPBACK trust list
while the deployed compose file trusted the whole Docker bridge range. The test
passed and production was wide open. So the tests here read the SHIPPED
configuration rather than constructing their own.
"""

from __future__ import annotations

import ipaddress
import re
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "proxy"))

from app.config import Settings  # noqa: E402
from app.middleware.client_identity import parse_networks, resolve_client_ip  # noqa: E402
from app.schemas import passthrough_response_headers  # noqa: E402

pytestmark = pytest.mark.unit

COMPOSE = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
PROXY_ENV = COMPOSE["services"]["mrfp-proxy"]["environment"]


def code_only(path: Path) -> str:
    """Source with docstrings and comments removed.

    Matching raw source is a trap these tests fell into once already: the
    docstring explaining that `await request.body()` was REMOVED contains the
    string `await request.body()`, so a naive grep reports the bug it is
    describing as still present. Assert against executable code only.
    """
    import ast

    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            doc = ast.get_docstring(node, clean=False)
            if doc is not None and node.body:
                node.body = node.body[1:] or [ast.Pass()]
    # ast.unparse normalises string quotes, so callers matching a literal would
    # miss on quote style alone. Normalise to double quotes for matching.
    return ast.unparse(ast.fix_missing_locations(tree)).replace("'", '"')


def _compose_default(key: str) -> str:
    """The value compose uses when the operator sets nothing.

    Entries look like "${MRFP_X:-default}"; this returns `default`.
    """
    raw = str(PROXY_ENV.get(key, ""))
    if raw.startswith("${") and ":-" in raw:
        return raw.split(":-", 1)[1].rstrip("}")
    return raw


# --- the XFF bypass -------------------------------------------------------


def test_shipped_trust_list_excludes_the_docker_bridge() -> None:
    """The deployed config, not a config invented by the test.

    Compose previously trusted 172.16.0.0/12, which contains the bridge gateway
    every external request arrives from — so every client was a trusted proxy
    and could name its own rate-limit identity. Measured: an honest client got
    21/60 through a 20/s limit, an XFF-rotating client got 60/60.
    """
    trusted = parse_networks(_compose_default("MRFP_TRUSTED_PROXIES"))
    assert trusted, "compose must ship an explicit trusted-proxy list"

    v4_loopback = ipaddress.ip_network("127.0.0.0/8")
    v6_loopback = ipaddress.ip_network("::1/128")
    for net in trusted:
        # subnet_of raises across address families, so compare within one.
        loopback = v4_loopback if net.version == 4 else v6_loopback
        assert net.subnet_of(loopback), f"{net} is not loopback — XFF from it would be believed"

    # The specific address that broke it.
    for gateway in ("172.19.0.1", "172.17.0.1", "10.0.0.1", "192.168.1.1"):
        assert not any(ipaddress.ip_address(gateway) in net for net in trusted), (
            f"{gateway} is trusted — XFF from it would be believed"
        )


def test_spoofed_xff_cannot_mint_buckets_under_the_shipped_config() -> None:
    """The end-to-end property, asserted against what actually ships."""
    trusted = parse_networks(_compose_default("MRFP_TRUSTED_PROXIES"))
    bridge_peer = "172.19.0.1"  # measured peer for host traffic on this machine

    identities = {
        resolve_client_ip(bridge_peer, f"9.9.{i // 256}.{i % 256}", trusted) for i in range(200)
    }
    assert identities == {bridge_peer}, "a client chose its own rate-limit identity"


# --- the control plane ----------------------------------------------------


def test_admin_token_has_no_default_anywhere() -> None:
    """Unset must mean DISABLED, never open.

    /admin/ratelimit can reset any client's bucket, and /admin/* is exempt from
    rate limiting — so an unauthenticated DELETE took a measured 20/40 up to
    100/100 through a 20/s limit.
    """
    assert Settings(regions_file=str(ROOT / "config" / "regions.yaml")).admin_token == ""
    assert _compose_default("MRFP_ADMIN_TOKEN") == "", "compose must not ship an admin token"


def test_admin_routes_are_guarded_in_source() -> None:
    """Every mutating or disclosing route calls the guard."""
    api = code_only(ROOT / "proxy" / "app" / "api.py")
    for handler in ("admin_regions", "admin_ratelimit", "whoami"):
        start = api.index(f"async def {handler}(")
        body = api[start : start + 900]
        assert "_admin_denied(request)" in body, f"{handler} is unguarded"


def test_whoami_does_not_disclose_the_trust_list() -> None:
    """Echoing trusted_proxies told an attacker the spoof would be honoured."""
    api = code_only(ROOT / "proxy" / "app" / "api.py")
    start = api.index("async def whoami(")
    assert "trusted_proxies" not in api[start : start + 900]


# --- the chaos bypass -----------------------------------------------------


def test_chaos_bypass_ships_disarmed_and_tokenless() -> None:
    """It skips rate limiting entirely, so it must not ship armed.

    The committed literal `dev-chaos-bypass` was a permanent bypass in a public
    repo, and `make chaos` left the proxy armed with it for the container's life.
    """
    assert _compose_default("MRFP_CHAOS_MODE") == "0"
    assert _compose_default("MRFP_CHAOS_TOKEN") == ""

    settings = Settings(regions_file=str(ROOT / "config" / "regions.yaml"))
    assert settings.chaos_token == ""
    assert settings.chaos_mode == 0

    # And chaos.sh must disarm on every exit path, not just success.
    chaos_sh = (ROOT / "ops" / "scripts" / "chaos.sh").read_text()
    assert "trap restore EXIT INT TERM" in chaos_sh
    assert "dev-chaos-bypass" not in chaos_sh, "no committed bypass token"


def test_no_committed_bypass_token_anywhere() -> None:
    import subprocess

    tracked = subprocess.run(
        ["git", "grep", "-l", "dev-chaos-bypass"], cwd=ROOT, capture_output=True, text=True
    ).stdout.split()
    # This test names the string it forbids, so it must exclude itself.
    offenders = [f for f in tracked if not f.endswith("test_hardening.py")]
    assert not offenders, f"the old bypass token is still committed in: {offenders}"


# --- boot-time configuration ----------------------------------------------


def _settings(**kw) -> Settings:
    return Settings(regions_file=str(ROOT / "config" / "regions.yaml"), **kw)


def test_missing_redis_url_refuses_to_boot() -> None:
    """It used to disable distributed limiting silently.

    With N replicas on the per-process fallback the effective limit is N x the
    configured one, and the only signal was `redis: false` in one log line.
    """
    problems = _settings(redis_url="").validate_for_boot()
    assert any("MRFP_REDIS_URL" in p for p in problems)
    # ...and can be waived deliberately, for a Redis-less run.
    assert not any(
        "MRFP_REDIS_URL" in p
        for p in _settings(redis_url="", require_redis=False).validate_for_boot()
    )


def test_typos_in_enumerated_settings_are_caught() -> None:
    """pydantic-settings ignores unknown vars, so a typo left a default in place."""
    assert any(
        "MRFP_MODE" in p for p in _settings(redis_url="x", mode="resilent").validate_for_boot()
    )
    assert any(
        "MRFP_RL_FAIL_MODE" in p
        for p in _settings(redis_url="x", rl_fail_mode="close").validate_for_boot()
    )


def test_budget_must_fit_the_walk_it_bounds() -> None:
    """Too small and the last region is silently never attempted."""
    problems = _settings(redis_url="x", total_budget_ms=50).validate_for_boot()
    assert any("TOTAL_BUDGET" in p for p in problems)
    assert not _settings(redis_url="x", total_budget_ms=900).validate_for_boot()


def test_chaos_mode_requires_an_explicit_token() -> None:
    problems = _settings(redis_url="x", chaos_mode=1, chaos_token="").validate_for_boot()
    assert any("MRFP_CHAOS_TOKEN" in p for p in problems)
    assert not _settings(redis_url="x", chaos_mode=1, chaos_token="fresh").validate_for_boot()


# --- body handling --------------------------------------------------------


def test_body_is_capped_before_it_is_buffered() -> None:
    """`await request.body()` buffered everything, then checked the cap.

    A 512 MiB POST OOM-killed the container, which then stayed dead because
    regions use `restart: "no"`. After the fix a 400 MB body moved container
    memory by 0.22 MiB.
    """
    src = code_only(ROOT / "proxy" / "app" / "schemas.py")
    start = src.index("async def from_starlette(")
    body = src[start : src.index("def upstream_headers(")]

    assert "request.body()" not in body, "the unbounded read is back"
    assert 'request.headers.get("content-length")' in body, "no pre-read rejection"
    assert "request.stream()" in body, "chunked bodies must be capped incrementally"
    assert "asyncio.timeout(read_timeout_s)" in body, "the read must be time-bounded"


def test_slow_body_read_is_bounded() -> None:
    """An admission slot is held across the read.

    130 sockets at ~130 bytes/sec previously took /v1/infer to 100% 503 while
    /healthz still returned 200.
    """
    assert _settings(redis_url="x").body_read_timeout_s > 0


# --- response contract ----------------------------------------------------


def test_upstream_response_headers_are_forwarded() -> None:
    """Everything but Content-Type used to be dropped."""
    forwarded = passthrough_response_headers(
        {
            "content-type": "application/json",
            "content-length": "42",
            "connection": "keep-alive",
            "cache-control": "no-store",
            "etag": '"abc"',
            "x-region-id": "us-east-1",
            "ratelimit-remaining": "7",
        }
    )
    assert forwarded["cache-control"] == "no-store"
    assert forwarded["etag"] == '"abc"'
    assert forwarded["x-region-id"] == "us-east-1"
    assert forwarded["ratelimit-remaining"] == "7"
    # Hop-by-hop and anything Starlette recomputes must not be copied.
    for dropped in ("connection", "content-length", "content-type"):
        assert dropped not in forwarded


def test_proxy_headers_win_over_upstream_ones() -> None:
    """A region must not be able to forge X-Served-By-Region."""
    src = code_only(ROOT / "proxy" / "app" / "schemas.py")
    start = src.index("def to_response(")
    body = src[start : start + 700]
    assert body.index("dict(self.upstream_headers)") < body.index("headers.update(self.headers(")


# --- the fallback bucket leak ---------------------------------------------


def test_local_bucket_eviction_is_actually_scheduled() -> None:
    """`evict_older_than` existed and was never called: an unbounded dict keyed
    by client IP, reachable through the (then unauthenticated) `?id=` param."""
    main = code_only(ROOT / "proxy" / "app" / "main.py")
    assert "_sweep_local_buckets" in main
    assert "evict_older_than" in main
    assert "sweeper.cancel()" in main, "the sweeper must be cancelled on shutdown"


def test_admin_identity_parameter_is_validated() -> None:
    """`?id=` was unvalidated, so arbitrary keys could be allocated."""
    api = code_only(ROOT / "proxy" / "app" / "api.py")
    assert "_valid_identity" in api
    start = api.index("async def admin_ratelimit(")
    assert "_valid_identity(identity)" in api[start : start + 1200]


# --- feature flags must be reachable, not just implemented ------------------


def test_every_prod_setting_is_plumbed_through_compose() -> None:
    """A flag the deployment cannot set is a flag that does not exist.

    Hit twice for real: MRFP_STREAMING_ENABLED and MRFP_IDEMPOTENCY_ENABLED
    were both implemented, tested, and unreachable — compose simply never
    forwarded them, so the feature was silently off and the live check quietly
    exercised the old path instead. Unit tests could not see it, because they
    construct Settings directly.
    """
    import re

    from app.profiles import preset

    raw = (ROOT / "docker-compose.yml").read_text()
    declared = set(re.findall(r"^\s+(MRFP_[A-Z0-9_]+):", raw, flags=re.M))

    missing = sorted(
        f"MRFP_{field.upper()}"
        for field in preset("prod")
        if f"MRFP_{field.upper()}" not in declared
    )
    assert not missing, f"prod-profile settings unreachable through compose: {missing}"


def test_compose_defaults_do_not_break_coercion() -> None:
    """An empty-string default fails pydantic coercion on a bool or int field.

    The first attempt at plumbing used "${VAR:-}" for everything, which would
    have stopped the proxy booting the moment compose passed an empty string
    for MRFP_REQUIRE_TLS.

    Empty is CORRECT for the string fields where it means "disabled"
    (MRFP_ADMIN_TOKEN, MRFP_CHAOS_TOKEN), so the check is scoped by the field's
    declared type rather than by name.
    """
    raw = (ROOT / "docker-compose.yml").read_text()
    empty = re.findall(r'^\s+(MRFP_[A-Z0-9_]+): "\$\{[A-Z0-9_]+:-\}"', raw, flags=re.M)

    offenders = []
    for env in empty:
        field = Settings.model_fields.get(env.removeprefix("MRFP_").lower())
        if field is not None and field.annotation is not str:
            offenders.append(f"{env} ({field.annotation.__name__})")
    assert not offenders, f"empty-string defaults will fail coercion: {offenders}"


def test_saturation_metrics_are_actually_written() -> None:
    """A gauge that is never set makes its alert permanently silent.

    proxy_region_pool_waiters was declared, alerted on in slo.yml, and never
    written — so MRFPPoolQueueing could not fire. A dead alert is worse than no
    alert: it creates confidence that something is being watched.
    """
    src = code_only(ROOT / "proxy" / "app" / "metrics.py")
    api = code_only(ROOT / "proxy" / "app" / "api.py")
    assert "def observe_pools" in src
    assert "observe_pools" in api, "pool gauges are never refreshed at scrape time"

    rules = (ROOT / "ops" / "observability" / "rules" / "slo.yml").read_text()
    for metric in re.findall(r"(proxy_[a-z_]+)", rules):
        if metric.endswith("_total"):
            continue
        assert metric in src, f"{metric} is alerted on but never defined"


# --- the limiter must announce WHY it degraded (ADR-029) ---------------------


def test_the_degraded_path_records_a_reason() -> None:
    """`auth` and `unreachable` come from one exception hierarchy but are
    opposite operational events: a config bug that never heals versus the
    outage fail-open exists for. Logging them identically is how a production
    profile ran with no rate limiting at all."""
    body = code_only(ROOT / "proxy" / "app" / "ratelimit" / "limiter.py")
    assert "classify_redis_error(exc)" in body
    assert "reason=reason" in body
    assert "RATE_LIMIT_DEGRADED.labels(reason)" in body


def test_an_auth_failure_logs_at_ERROR_not_WARNING() -> None:
    """It never recovers on its own, so it must not sit at the same level as a
    transient blip that does."""
    body = code_only(ROOT / "proxy" / "app" / "ratelimit" / "limiter.py")
    assert 'logging.ERROR if reason == "auth" else logging.WARNING' in body


def test_boot_verifies_that_redis_answers() -> None:
    """`require_redis` asks whether Redis is CONFIGURED. Nothing asked whether
    it replied, which is the entire gap this closes."""
    body = code_only(ROOT / "proxy" / "app" / "main.py")
    assert "await verify_redis(redis)" in body


def test_readyz_reports_the_limiter_without_gating_on_it() -> None:
    """Reported so an operator can see it; not part of the verdict, because a
    degraded limiter still serves every request correctly and failing readiness
    would turn a degradation into a self-inflicted outage."""
    body = code_only(ROOT / "proxy" / "app" / "api.py")
    assert '"rate_limit"' in body
    start = body.index("async def readyz(")
    section = body[start : start + 1600]
    assert "status_code=200 if usable else 503" in section, "readiness must still hinge on regions"
    assert "rate_limit_backend_up() else" in section
