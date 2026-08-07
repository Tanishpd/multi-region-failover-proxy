"""App factory and lifespan.

Middleware registration order is the REVERSE of execution order, because
Starlette's `add_middleware` prepends: the last one registered ends up
outermost. Getting this backwards is not cosmetic — it would put the router
outside the rate limiter, so 429s would be attributed to us-east-1 in the
metrics and `request.state.request_id` would be unset when the 429 body is
built.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, suppress

from starlette.applications import Starlette
from starlette.routing import Route

from app import api, metrics
from app.auth import KeyStore
from app.breaker import Breaker
from app.cluster import BreakerGossip, replica_count
from app.config import settings
from app.idempotency import IdempotencyStore
from app.logging_setup import configure, configure_policy, logx
from app.middleware.context import ContextMiddleware
from app.ratelimit.limiter import TokenBucketLimiter
from app.redis_client import build_redis, verify_redis
from app.region_client import RegionClient
from app.regions import RegionRegistry
from app.router import ResilientRouter

# Strong references to fire-and-forget gossip publishes.
_BACKGROUND: set[asyncio.Task] = set()


@asynccontextmanager
async def lifespan(app: Starlette):
    configure(settings.log_level)
    configure_policy(
        log_sample_rate=settings.log_sample_rate,
        log_client_ip=settings.log_client_ip,
    )

    # A wrong MRFP_ prefix is silently ignored by pydantic-settings, so the
    # resolved values are the only trustworthy record of what is actually
    # active. This is the first line in the log for that reason.
    logx(
        "boot",
        mode=settings.mode,
        read_timeout_ms=settings.read_timeout_ms,
        connect_timeout_ms=settings.connect_timeout_ms,
        total_budget_ms=settings.total_budget_ms,
        max_inflight=settings.max_inflight,
        trusted_proxies=settings.trusted_proxies,
        regions_file=settings.regions_file,
        breaker_enabled=settings.resilient,
    )

    # Refuse to start on a misconfiguration rather than degrading into one.
    # pydantic-settings ignores unknown variables silently, so a typo would
    # otherwise leave a default in place with no signal at all.
    problems = settings.validate_for_boot()
    if problems:
        for problem_text in problems:
            logx("config_invalid", level=logging.ERROR, problem=problem_text)
        raise RuntimeError("invalid configuration:\n  - " + "\n  - ".join(problems))

    if not settings.admin_token:
        logx(
            "admin_disabled",
            level=logging.WARNING,
            detail="MRFP_ADMIN_TOKEN unset; /admin and /debug return 404",
        )

    registry = RegionRegistry.load(settings.regions_file, settings.priority_override())

    # Clients are built HERE, not at import time: an httpx pool binds to the
    # running event loop on first use, and one created at import would bind to
    # the wrong loop (or none).
    clients = {
        spec.id: RegionClient(
            spec,
            connect_ms=settings.connect_timeout_ms,
            read_ms=settings.read_timeout_ms,
            pool_ms=settings.pool_timeout_ms,
        )
        for spec in registry.ordered()
    }
    for client in clients.values():
        # Fails loudly if `limits=` ever gets passed to AsyncClient instead of
        # the transport and is silently discarded.
        client.assert_pool_limits()

    # One breaker instance serves regions AND Redis. In literal mode (the
    # default) it is not created at all, so every request walks the priority
    # list and really enters the except block. (ADR-009)
    breaker = None
    if settings.resilient:

        async def _purge(region_id: str) -> None:
            client = clients.get(region_id)
            if client is not None:
                await client.purge_pool()

        breaker = Breaker(
            failure_threshold=settings.breaker.failure_threshold,
            cooldown_s=settings.breaker.cooldown_s,
            max_cooldown_s=settings.breaker.max_cooldown_s,
            on_open=_purge,
        )

    key_store = None
    if settings.auth_mode != "off":
        key_store = KeyStore(settings.api_keys_file)
        loaded = key_store.load()
        logx("api_keys_loaded", count=loaded, mode=settings.auth_mode)

    redis = (
        build_redis(
            settings.redis_url,
            sentinels=settings.redis_sentinels,
            master_name=settings.redis_master_name,
            password=settings.redis_password,
        )
        if (settings.redis_url or settings.redis_sentinels)
        else None
    )

    # Prove it ANSWERS, not merely that it is configured. `require_redis` only
    # ever checked the latter, which is how a prod profile shipped with
    # --requirepass on the server and no password on the client: NOAUTH on every
    # command, caught by the limiter, failed open exactly as designed, and no
    # rate limiting at all. A refusal is fatal here; an outage is not.
    redis_state = "absent"
    if redis is not None:
        redis_state = await verify_redis(redis)
        logx(
            "redis_verified",
            state=redis_state,
            level=logging.WARNING if redis_state != "ok" else logging.INFO,
            topology="sentinel" if settings.redis_sentinels else "url",
        )
    app.state.redis_boot_state = redis_state
    # Seed the gauge, or a proxy that has served nothing yet reports "fallback"
    # purely because no decision has been made.
    metrics.RATE_LIMIT_BACKEND_UP.set(1 if redis_state == "ok" else 0)

    limiter = TokenBucketLimiter(
        redis,
        capacity=settings.rl_capacity,
        refill_per_sec=settings.rl_refill_per_sec,
        ttl_pad_ms=settings.rl_ttl_pad_ms,
        fail_mode=settings.rl_fail_mode,
        breaker=breaker,
    )
    # With Redis down the fallback bucket is per-process, so N replicas would
    # collectively enforce N x the configured limit. Dividing keeps the fleet
    # total close to what was asked for.
    limiter.local.replicas = replica_count()

    gossip = None
    if breaker is not None and redis is not None:
        gossip = BreakerGossip(redis, breaker)

        def _announce(region: str, state: str, code: str) -> None:
            task = asyncio.create_task(gossip.publish(region, state, code))
            _BACKGROUND.add(task)
            task.add_done_callback(_BACKGROUND.discard)

        breaker.on_transition = _announce
        await gossip.start()

    app.state.settings = settings
    app.state.registry = registry
    app.state.clients = clients
    app.state.redis = redis
    app.state.limiter = limiter
    app.state.key_store = key_store
    app.state.idempotency = IdempotencyStore(redis, enabled=settings.idempotency_enabled)
    app.state.draining = False
    # Parsed once, not per request.
    from app.middleware.client_identity import parse_networks

    app.state.trusted_networks = parse_networks(settings.trusted_proxies)
    # Admission must never exceed the SMALLEST per-region connection pool.
    # Above it, overload becomes PoolTimeout, which used to be recorded as a
    # region failure — opening a breaker on a region that was never contacted.
    # 0 means "derive it", which is what the prod profile asks for.
    smallest_pool = min(spec.max_connections for spec in registry.ordered())
    inflight = settings.max_inflight or max(1, int(smallest_pool * 0.75))
    if inflight > smallest_pool:
        logx(
            "admission_above_pool",
            level=logging.WARNING,
            max_inflight=inflight,
            smallest_pool=smallest_pool,
            detail="overload will surface as PoolTimeout rather than a clean shed",
        )
    app.state.admission = asyncio.Semaphore(inflight)
    app.state.max_inflight = inflight
    app.state.primary_region = registry.ordered()[0].id
    app.state.router = ResilientRouter(registry, clients, settings, breaker=breaker)

    # The in-process fallback bucket has no TTL (Redis does), so without this
    # it grows one entry per distinct identity, forever — a slow memory leak
    # keyed by client IP.
    app.state.gossip = gossip
    sweeper = asyncio.create_task(_sweep_local_buckets(limiter))

    logx(
        "ready",
        regions=[s.id for s in registry.ordered()],
        primary=app.state.primary_region,
        rate_limit=f"{settings.rl_capacity} @ {settings.rl_refill_per_sec}/s",
        redis=bool(redis),
        replicas=replica_count(),
        gossip=gossip is not None,
        breaker=settings.resilient,
        admin=bool(settings.admin_token),
        chaos_bypass=bool(settings.chaos_mode),
    )
    try:
        yield
    finally:
        if gossip is not None:
            await gossip.stop()
        sweeper.cancel()
        with suppress(asyncio.CancelledError):
            await sweeper
        for client in clients.values():
            await client.aclose()
        if redis is not None:
            await redis.aclose()
        logx("shutdown")


async def _sweep_local_buckets(limiter, interval_s: float = 60.0) -> None:
    """Evict idle fallback buckets. `evict_older_than` existed but was dead code."""
    # Anything idle for longer than this has fully refilled, so dropping it is
    # indistinguishable from keeping it — except in memory.
    max_idle_ms = 300_000
    while True:
        await asyncio.sleep(interval_s)
        dropped = limiter.local.evict_older_than(max_idle_ms)
        if dropped:
            logx("local_buckets_evicted", count=dropped, remaining=len(limiter.local._buckets))


def create_app() -> Starlette:
    app = Starlette(
        lifespan=lifespan,
        routes=[
            Route("/v1/infer", api.infer, methods=["POST"]),
            Route("/healthz", api.healthz, methods=["GET"]),
            Route("/readyz", api.readyz, methods=["GET"]),
            Route("/admin/drain", api.drain, methods=["POST"]),
            Route("/admin/regions", api.admin_regions, methods=["GET"]),
            Route("/metrics", api.metrics_endpoint, methods=["GET"]),
            Route("/admin/ratelimit", api.admin_ratelimit, methods=["GET", "DELETE"]),
            Route("/debug/whoami", api.whoami, methods=["GET"]),
        ],
        exception_handlers={404: api.not_found, 500: api.server_error},
    )

    # ONE pure-ASGI layer doing request-id, client identity, rate limiting and
    # response telemetry. Three stacked BaseHTTPMiddleware layers measured
    # ~530 us/req against ~110 us for this; each one wraps the call in an anyio
    # task group and pumps the response through a memory stream.
    #
    # The middleware-ordering trap the three layers had is gone with them:
    # ordering is now explicit statements inside one __call__, not an implicit
    # consequence of add_middleware prepending.
    app.add_middleware(
        ContextMiddleware,
        trusted_proxies=settings.trusted_proxies,
        chaos_mode=bool(settings.chaos_mode),
        chaos_token=settings.chaos_token,
    )
    return app


app = create_app()

logging.getLogger(__name__)
