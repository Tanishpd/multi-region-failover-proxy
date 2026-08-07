"""In-process fault injection, so a region can misbehave without being killed.

Exactly three modes (docs/CONTRACT.md section 3):

  slow       add latency — a region that is up but degraded
  error      return 503 — a response-level failure, not a transport one
  blackhole  accept the connection and never respond — the only way to get a
             genuine ReadTimeout without `docker pause`

`docker stop` and `docker pause` cover the transport-level failures; these cover
the ones a container-level fault cannot express.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass


@dataclass
class ChaosState:
    mode: str | None = None
    ms: int = 0

    def as_dict(self) -> dict[str, object]:
        return {"mode": self.mode, "ms": self.ms if self.mode == "slow" else None}


state = ChaosState()


async def blackhole() -> None:
    """Never return.

    An Event that is never set, rather than a sleep loop. Under a flood, a
    blackholed region holds one hung task per in-flight request; a polling loop
    would wake every one of them on a timer for no reason, and a single long
    `asyncio.sleep` would do the same thing once. Waiting on an Event parks the
    task with no timer at all, and still unwinds promptly on cancellation —
    which is what shutdown and client disconnect both deliver.
    """
    await asyncio.Event().wait()
