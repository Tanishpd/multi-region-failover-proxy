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
from contextlib import asynccontextmanager

from starlette.applications import Starlette
from starlette.routing import Route

from app import api
from app.config import settings
from app.logging_setup import configure, logx
from app.middleware.client_identity import ClientIdentityMiddleware
from app.middleware.request_id import RequestIdMiddleware
from app.region_client import RegionClient
from app.regions import RegionRegistry
from app.router import ResilientRouter


@asynccontextmanager
async def lifespan(app: Starlette):
    configure(settings.log_level)

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

    app.state.settings = settings
    app.state.registry = registry
    app.state.clients = clients
    app.state.admission = asyncio.Semaphore(settings.max_inflight)
    app.state.primary_region = registry.ordered()[0].id
    app.state.router = ResilientRouter(registry, clients, settings, breaker=None)

    logx(
        "ready",
        regions=[s.id for s in registry.ordered()],
        primary=app.state.primary_region,
    )
    try:
        yield
    finally:
        for client in clients.values():
            await client.aclose()
        logx("shutdown")


def create_app() -> Starlette:
    app = Starlette(
        lifespan=lifespan,
        routes=[
            Route("/v1/infer", api.infer, methods=["POST"]),
            Route("/healthz", api.healthz, methods=["GET"]),
            Route("/readyz", api.readyz, methods=["GET"]),
            Route("/admin/regions", api.admin_regions, methods=["GET"]),
            Route("/debug/whoami", api.whoami, methods=["GET"]),
        ],
        exception_handlers={404: api.not_found, 500: api.server_error},
    )

    # Reverse of execution order. Execution is:
    #   RequestId (outermost) -> ClientIdentity -> [RateLimit, Phase 4] -> route
    app.add_middleware(ClientIdentityMiddleware, trusted_proxies=settings.trusted_proxies)
    app.add_middleware(RequestIdMiddleware)
    return app


app = create_app()

logging.getLogger(__name__)
