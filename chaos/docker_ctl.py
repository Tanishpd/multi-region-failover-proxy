"""Docker control for fault injection.

Two properties this module guarantees:

  * Commands run via `create_subprocess_exec` with an argv LIST. Never
    `shell=True` — the project directory contains spaces and an "&", and a
    container name interpolated into a shell string is an injection waiting to
    happen.

  * Every action pushes its inverse onto an undo stack, drained in a `finally`
    block and on SIGINT. Ctrl-C during a run can never leave a region stopped
    or paused.

The literal command string of each action is recorded, so the generated report
can prove that `docker stop us-east-1-endpoint` — the brief's own command — is
what actually ran.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field


@dataclass(slots=True)
class Action:
    argv: list[str]
    started_at: float
    duration_ms: float
    returncode: int
    stderr: str = ""

    @property
    def command(self) -> str:
        return " ".join(self.argv)


@dataclass
class DockerControl:
    actions: list[Action] = field(default_factory=list)
    _undo: list[list[str]] = field(default_factory=list)

    async def _run(self, argv: list[str], *, undo: list[str] | None = None) -> Action:
        t0 = time.perf_counter()
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, err = await proc.communicate()
        action = Action(
            argv=argv,
            started_at=time.time(),
            duration_ms=(time.perf_counter() - t0) * 1000,
            returncode=proc.returncode or 0,
            stderr=err.decode(errors="replace").strip(),
        )
        self.actions.append(action)
        if undo and action.returncode == 0:
            self._undo.append(undo)
        return action

    async def stop(self, container: str) -> Action:
        """The brief's own fault: `docker stop <container>`, run literally."""
        return await self._run(["docker", "stop", container], undo=["docker", "start", container])

    async def start(self, container: str) -> Action:
        return await self._run(["docker", "start", container])

    async def pause(self, container: str) -> Action:
        """The only container-level fault that produces a real ReadTimeout."""
        return await self._run(
            ["docker", "pause", container], undo=["docker", "unpause", container]
        )

    async def unpause(self, container: str) -> Action:
        return await self._run(["docker", "unpause", container])

    async def kill(self, container: str) -> Action:
        return await self._run(["docker", "kill", container], undo=["docker", "start", container])

    async def restore(self) -> list[Action]:
        """Drain the undo stack, most recent first. Safe to call twice."""
        done = []
        while self._undo:
            argv = self._undo.pop()
            done.append(await self._run(argv))
        return done

    def as_dict(self) -> list[dict]:
        return [
            {
                "command": a.command,
                "at": a.started_at,
                "duration_ms": round(a.duration_ms, 1),
                "returncode": a.returncode,
                "stderr": a.stderr,
            }
            for a in self.actions
        ]


async def container_is_running(container: str) -> bool:
    proc = await asyncio.create_subprocess_exec(
        "docker",
        "inspect",
        "-f",
        "{{.State.Running}}",
        container,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    return out.decode().strip() == "true"
