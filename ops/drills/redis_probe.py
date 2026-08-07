"""Hammer the Sentinel-backed client and report the promotion blip.

Runs INSIDE the docker network, because Sentinel hands back the primary's
container address and Docker Desktop does not route those from the macOS host.
The caller kills the primary partway through; this measures what that costs.

Emits one JSON object on stdout so the drill script can assert on it.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time

sys.path.insert(0, "/app")

from app.redis_client import build_redis

DURATION_S = float(os.getenv("PROBE_DURATION_S", "25"))
INTERVAL_S = float(os.getenv("PROBE_INTERVAL_S", "0.05"))


async def main() -> None:
    client = build_redis(
        "",
        sentinels=os.environ["PROBE_SENTINELS"],
        master_name=os.getenv("PROBE_MASTER_NAME", "mrfp-master"),
    )

    started_master = await client.connection_pool.get_master_address()

    ok = err = 0
    first_error: str | None = None
    first_error_at = recovered_at = 0.0
    t0 = time.monotonic()

    while time.monotonic() - t0 < DURATION_S:
        try:
            await client.set("mrfp:drill", str(time.time()), ex=30)
            await client.get("mrfp:drill")
            ok += 1
            # First success AFTER the first failure is the moment service
            # returned. Anything later is steady state.
            if first_error is not None and recovered_at == 0.0:
                recovered_at = time.monotonic()
        except Exception as exc:  # any failure counts as downtime
            err += 1
            if first_error is None:
                first_error = f"{type(exc).__qualname__}: {exc}"
                first_error_at = time.monotonic()
        await asyncio.sleep(INTERVAL_S)

    try:
        ended_master = await client.connection_pool.get_master_address()
    except Exception as exc:
        ended_master = f"rediscovery failed: {type(exc).__qualname__}: {exc}"

    print(
        json.dumps(
            {
                "ok": ok,
                "errors": err,
                "started_master": list(started_master),
                "ended_master": (
                    list(ended_master) if isinstance(ended_master, tuple) else ended_master
                ),
                "promoted": started_master != ended_master,
                "first_error": first_error,
                # 0.0 with errors > 0 means it never came back inside the run.
                "blip_s": round(recovered_at - first_error_at, 3) if recovered_at else 0.0,
            }
        )
    )


asyncio.run(main())
