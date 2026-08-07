"""Capture the proxy's JSON logs for the run window.

Read from `docker logs --since`, not by tailing a file: the harness runs on the
host and the proxy's stdout belongs to Docker.
"""

from __future__ import annotations

import asyncio
import json


async def capture(container: str, since_epoch: float) -> list[dict]:
    """Every parseable JSON log line the proxy emitted since `since_epoch`."""
    proc = await asyncio.create_subprocess_exec(
        "docker",
        "logs",
        "--since",
        f"{since_epoch:.0f}",
        container,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()

    lines = []
    # uvicorn writes its own startup banner to stderr in plain text; the app's
    # own logs are JSON on stdout. Scan both and keep whatever parses.
    for stream in (out, err):
        for raw in stream.decode(errors="replace").splitlines():
            raw = raw.strip()
            if not raw.startswith("{"):
                continue
            try:
                lines.append(json.loads(raw))
            except json.JSONDecodeError:
                continue
    return lines


def failover_lines(logs: list[dict]) -> list[dict]:
    return [line for line in logs if line.get("event") == "upstream_failover"]


def event_counts(logs: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for line in logs:
        event = line.get("event")
        if event:
            counts[event] = counts.get(event, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))
