"""Chaos harness configuration and the per-request record type."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "artifacts"

REGIONS = ("us-east-1", "eu-west-1", "ap-southeast-1")
CONTAINERS = {
    "us-east-1": "us-east-1-endpoint",
    "eu-west-1": "eu-west-1-endpoint",
    "ap-southeast-1": "ap-southeast-1-endpoint",
}


@dataclass(slots=True)
class Record:
    """One request. `t_send` is monotonic and is what assertions key on.

    Keying a timing assertion on RECEIVE time is a real trap: a request
    correctly dispatched to a healthy region microseconds before the kill lands
    after it, so a receive-time assertion fails for correct behaviour.
    """

    seq: int
    request_id: str
    t_send: float
    t_recv: float
    status: int  # 0 means the client never got a response
    region: str
    attempts: int
    failover_path: str
    latency_ms: float
    error: str = ""
    late_by_ms: float = 0.0  # how far behind schedule this request was issued

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 400


@dataclass(slots=True)
class FloodConfig:
    url: str = os.getenv("MRFP_PROXY_URL", "http://127.0.0.1:8000") + "/v1/infer"
    rps: int = 50
    duration_s: float = 12.0
    # 5s is ~25x the proxy's 200ms read timeout, so a client-side abort can
    # never be mistaken for a proxy failure.
    client_timeout_s: float = 5.0
    payload: dict = field(default_factory=lambda: {"prompt": "chaos", "max_tokens": 8})
    bypass_token: str = os.getenv("MRFP_CHAOS_TOKEN", "dev-chaos-bypass")


@dataclass(slots=True)
class Window:
    """A slice of the run, in monotonic time."""

    name: str
    start: float
    end: float

    def contains(self, t: float) -> bool:
        return self.start <= t < self.end
