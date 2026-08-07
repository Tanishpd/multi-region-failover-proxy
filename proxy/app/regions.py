"""The region registry, loaded from config/regions.yaml.

Order in the file is the failover order: us-east-1 -> eu-west-1 ->
ap-southeast-1. MRFP_REGION_PRIORITY overrides it, and is the only supported
way to reorder. docs/CONTRACT.md section 2.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True, slots=True)
class RegionSpec:
    id: str
    zone: str
    priority: int
    container: str
    base_url: str
    host_port: int
    base_latency_ms: int
    max_connections: int
    max_keepalive_connections: int
    keepalive_expiry_s: float


class RegionRegistry:
    def __init__(self, specs: list[RegionSpec]) -> None:
        self._specs = specs
        self._by_id = {s.id: s for s in specs}

    @classmethod
    def load(cls, path: str | None = None, priority: list[str] | None = None) -> RegionRegistry:
        # Resolved from an env var, never Path(__file__).parents[N]: the module
        # sits at /app/app/regions.py in the image but at a different depth on
        # the host, so a relative walk works in one place and IndexErrors in the
        # other — at import time, before any override can apply.
        target = Path(path or os.getenv("MRFP_REGIONS_FILE", "/config/regions.yaml"))
        raw = yaml.safe_load(target.read_text())

        defaults = raw.get("client_defaults", {})
        specs = [
            RegionSpec(
                id=r["id"],
                zone=r["zone"],
                priority=r["priority"],
                container=r["container"],
                base_url=r["base_url"],
                host_port=r["host_port"],
                base_latency_ms=r["base_latency_ms"],
                max_connections=defaults.get("max_connections", 64),
                max_keepalive_connections=defaults.get("max_keepalive_connections", 32),
                keepalive_expiry_s=defaults.get("keepalive_expiry_s", 30),
            )
            for r in raw["regions"]
        ]
        specs.sort(key=lambda s: s.priority)

        if priority:
            index = {rid: i for i, rid in enumerate(priority)}
            unknown = set(priority) - {s.id for s in specs}
            if unknown:
                raise ValueError(f"MRFP_REGION_PRIORITY names unknown regions: {sorted(unknown)}")
            specs.sort(key=lambda s: index.get(s.id, len(index)))

        return cls(specs)

    def ordered(self) -> list[RegionSpec]:
        return list(self._specs)

    def get(self, region_id: str) -> RegionSpec | None:
        return self._by_id.get(region_id)

    def next_after(self, region_id: str) -> RegionSpec | None:
        """The region we would fail over to. None if this is the last one."""
        for i, spec in enumerate(self._specs):
            if spec.id == region_id:
                return self._specs[i + 1] if i + 1 < len(self._specs) else None
        return None

    def __len__(self) -> int:
        return len(self._specs)
