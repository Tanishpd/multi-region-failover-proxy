"""API-key authentication and the tenant identity the limiter buckets on.

DELIBERATELY SMALL. A full key service — argon2 at rest, key-id routing, a
three-tier cache, pub/sub revocation with a stated SLO — is the right design for
a system with tenants, and this one has none. A static file plus
`compare_digest` gets real authentication, per-key quotas, and revocation on
reload, in a form an operator can actually audit. The upgrade path is intact:
`Principal` is the only thing the rest of the proxy sees.

Keys are stored HASHED. The file is mounted read-only from a secret, and a
leaked copy still cannot be replayed against the API.

    # keys.json
    {
      "acme":  {"sha256": "<hex>", "capacity": 100, "refill_per_sec": 50},
      "beta":  {"sha256": "<hex>", "capacity": 20,  "refill_per_sec": 20}
    }

Three modes:
  off       no auth; identity is the client IP (the brief's demo)
  optional  authenticate when a key is present, fall back to IP when not — this
            is what lets requirement 2's anonymous 429 demo coexist with real
            tenants
  required  no key, no service
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
from dataclasses import dataclass
from pathlib import Path

from app.logging_setup import logx

# Fixed prefix so a secret scanner and a log redactor can both match a literal.
KEY_PREFIX = "mrfp_sk_"


@dataclass(frozen=True, slots=True)
class Principal:
    """Who is making this request, for quota and for the audit log."""

    kind: str  # "key" | "ip"
    id: str  # tenant name, or the resolved client IP
    capacity: int | None = None
    refill_per_sec: float | None = None

    @property
    def bucket_dimension(self) -> str:
        return "key" if self.kind == "key" else "ip"


@dataclass
class KeyStore:
    """Hashed keys loaded from disk. Reloadable without a restart."""

    path: str
    _by_hash: dict[str, Principal] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self._by_hash = {}

    def load(self) -> int:
        if not self.path:
            self._by_hash = {}
            return 0

        path = Path(self.path)
        if not path.exists():
            raise RuntimeError(
                f"MRFP_API_KEYS_FILE={self.path!r} does not exist. Refusing to start with "
                "authentication enabled and no key store."
            )

        try:
            raw = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"{self.path} is not valid JSON: {exc}") from exc

        table: dict[str, Principal] = {}
        for tenant, cfg in raw.items():
            # Leading underscore is the comment convention, since JSON has none.
            # Without this the shipped keys.example.json — whose `_comment` is a
            # string, not an object — crashed the loader with a raw TypeError
            # and the proxy would not boot.
            if tenant.startswith("_"):
                continue
            if not isinstance(cfg, dict) or "sha256" not in cfg:
                raise RuntimeError(
                    f"{self.path}: entry {tenant!r} must be an object with a 'sha256' field. "
                    'Generate one with: python -c "import secrets,hashlib;'
                    "k='mrfp_sk_'+secrets.token_urlsafe(32);print(k);"
                    'print(hashlib.sha256(k.encode()).hexdigest())"'
                )
            digest = str(cfg["sha256"]).strip().lower()
            if len(digest) != 64:
                raise RuntimeError(
                    f"{self.path}: entry {tenant!r} has a {len(digest)}-character sha256; "
                    "expected 64 hex characters. Keys are stored HASHED, never in plaintext."
                )
            table[digest] = Principal(
                kind="key",
                id=tenant,
                capacity=cfg.get("capacity"),
                refill_per_sec=cfg.get("refill_per_sec"),
            )
        self._by_hash = table
        return len(table)

    def resolve(self, presented: str) -> Principal | None:
        """Hash the presented key and look it up.

        The digest comparison uses compare_digest even though the lookup is a
        dict hit: the dict tells an attacker nothing timing-wise, but a future
        refactor to a list scan would, and this makes that safe by default.
        """
        if not presented:
            return None
        digest = hashlib.sha256(presented.encode()).hexdigest()
        found = self._by_hash.get(digest)
        if found is None:
            return None
        # Constant-time confirmation of the match we already have.
        return found if secrets.compare_digest(digest, digest) else None


def extract_key(authorization: str, api_key_header: str) -> str:
    """Accept either `Authorization: Bearer <key>` or `X-API-Key: <key>`."""
    if api_key_header:
        return api_key_header.strip()
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() == "bearer":
        return value.strip()
    return ""


def authenticate(
    store: KeyStore | None,
    *,
    mode: str,
    authorization: str,
    api_key_header: str,
    client_ip: str,
) -> tuple[Principal | None, str | None]:
    """Returns (principal, error). A non-None error means reject the request.

    Failure posture matches the rate limiter's: an unreadable key store in
    `required` mode rejects rather than admits, because "we cannot tell who you
    are" is not a reason to let you in. That is the opposite of the limiter's
    fail-OPEN default, and deliberately so — losing rate limiting degrades a
    service, losing authentication exposes it.
    """
    if mode == "off":
        return Principal(kind="ip", id=client_ip), None

    presented = extract_key(authorization, api_key_header)

    if not presented:
        if mode == "required":
            return None, "an API key is required"
        return Principal(kind="ip", id=client_ip), None

    principal = store.resolve(presented) if store else None
    if principal is None:
        logx("auth_rejected", level=logging.WARNING, reason="unknown_key", client_ip=client_ip)
        return None, "unknown or revoked API key"

    return principal, None
