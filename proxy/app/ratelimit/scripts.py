"""EVALSHA with correct NOSCRIPT recovery.

`EVALSHA` fails with NOSCRIPT after a Redis restart or `SCRIPT FLUSH`. Two
obvious fixes are both wrong:

  * Catch and re-EVAL unconditionally — every concurrent request then ships the
    full script body at once, exactly when the system is already recovering.
  * Put one lock around the reload — every in-flight request serialises behind a
    network round trip.

A generation counter fixes both: the first caller to notice takes the lock and
re-uploads; everyone else waits, sees the generation has moved, and retries the
cheap EVALSHA. Note that EVAL itself caches the script, so the recovery path
both runs and re-registers in one operation.
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

from redis.asyncio import Redis
from redis.exceptions import NoScriptError


class LuaScript:
    def __init__(self, redis: Redis, source: str) -> None:
        self._redis = redis
        self._source = source
        self.sha = hashlib.sha1(source.encode()).hexdigest()
        self._generation = 0
        self._lock = asyncio.Lock()
        self.noscript_recoveries = 0

    @classmethod
    def from_file(cls, redis: Redis, path: Path) -> LuaScript:
        return cls(redis, path.read_text())

    async def __call__(self, keys: list[str], args: list) -> list:
        try:
            return await self._redis.evalsha(self.sha, len(keys), *keys, *args)
        except NoScriptError:
            generation = self._generation
            async with self._lock:
                if self._generation != generation:
                    # Someone else re-cached it while we waited for the lock.
                    return await self._redis.evalsha(self.sha, len(keys), *keys, *args)
                try:
                    return await self._redis.evalsha(self.sha, len(keys), *keys, *args)
                except NoScriptError:
                    self.noscript_recoveries += 1
                    # EVAL runs AND caches, so this is recovery and re-register
                    # in one operation.
                    result = await self._redis.eval(self._source, len(keys), *keys, *args)
                    self._generation += 1
                    return result
