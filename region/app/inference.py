"""Deterministic mock inference.

The output is a pure function of (prompt, max_tokens, seed) — no region input.
That is what makes replaying a request to a second region defensible: the caller
cannot tell which region served it from the completion alone, so a failover
cannot corrupt a response. It also lets the chaos harness diff responses across
a failover without noise. docs/CONTRACT.md section 3.
"""

from __future__ import annotations

import hashlib
import random

_WORDS = (
    "latency budget region failover timeout bucket token retry quorum replica "
    "shard gateway upstream backoff circuit breaker probe drain saturate "
    "throughput percentile histogram idempotent partition consensus"
).split()


def _rng(prompt: str, max_tokens: int, seed: int | None) -> random.Random:
    material = f"{prompt}\x00{max_tokens}\x00{seed if seed is not None else ''}"
    digest = hashlib.blake2b(material.encode("utf-8"), digest_size=16).digest()
    return random.Random(int.from_bytes(digest, "big"))


def complete(prompt: str, max_tokens: int, seed: int | None) -> str:
    rng = _rng(prompt, max_tokens, seed)
    return " ".join(rng.choice(_WORDS) for _ in range(max_tokens))


def completion_id(prompt: str, max_tokens: int, seed: int | None) -> str:
    material = f"{prompt}\x00{max_tokens}\x00{seed if seed is not None else ''}"
    return "inf_" + hashlib.blake2b(material.encode("utf-8"), digest_size=4).hexdigest()


def token_count(text: str) -> int:
    return max(1, len(text.split()))
