"""Resolve the real client IP. Security-relevant.

Rate limiting buckets on this value, so "what is the client IP" is a security
question, not a formatting one.

`request.client.host` is wrong behind any proxy — inside a container it is
frequently the Docker bridge gateway, identical for every caller, so all clients
would share one bucket. So we read X-Forwarded-For. But THE CLIENT SETS THAT
HEADER, so it is attacker input.

The rule is a rightmost-untrusted walk:

  1. If the direct peer is not a trusted proxy, use the peer. Never consult XFF
     from an untrusted peer.
  2. Otherwise walk `XFF ++ [peer]` right-to-left and return the first entry
     that is not itself a trusted proxy.
  3. If every entry is trusted, fall back to THE PEER — never chain[0].

Step 3 is the one that matters. chain[0] is the leftmost, fully
attacker-controlled value: an attacker sends a different X-Forwarded-For on
every request, mints a fresh full bucket each time, and the rate limiter does
nothing while appearing to work perfectly.

The trusted set defaults to loopback only, deliberately NOT all of RFC1918:
Docker Desktop's bridge gateway is an RFC1918 address, so the broad default
would grant XFF authority to every container on the network.
"""

from __future__ import annotations

import ipaddress

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request


def parse_networks(spec: str) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    nets = []
    for raw in spec.split(","):
        raw = raw.strip()
        if raw:
            nets.append(ipaddress.ip_network(raw, strict=False))
    return nets


def normalize(value: str) -> str | None:
    """Parse one chain entry to a canonical address, or None if it is junk."""
    value = value.strip()
    if not value:
        return None
    # Strip an optional port: "1.2.3.4:5678" or "[::1]:5678".
    if value.startswith("["):
        value = value.partition("]")[0].lstrip("[")
    elif value.count(":") == 1:
        value = value.split(":")[0]
    try:
        addr = ipaddress.ip_address(value)
    except ValueError:
        return None
    # IPv4-mapped IPv6 (::ffff:127.0.0.1) must fold to its IPv4 form, or the
    # same client gets two different buckets depending on the socket family.
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return str(addr)


def resolve_client_ip(peer: str | None, xff: str | None, trusted: list) -> str:
    def is_trusted(ip: str | None) -> bool:
        if ip is None:
            return False
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        return any(addr in net for net in trusted)

    peer_norm = normalize(peer or "") or "unknown"

    if not is_trusted(peer_norm):
        return peer_norm

    chain = [*(xff or "").split(","), peer_norm] if xff else [peer_norm]
    for entry in reversed(chain):
        ip = normalize(entry)
        if ip and not is_trusted(ip):
            return ip

    # Every hop trusted. Fall back to the peer, NOT chain[0].
    return peer_norm


class ClientIdentityMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, trusted_proxies: str) -> None:
        super().__init__(app)
        self.trusted = parse_networks(trusted_proxies)

    async def dispatch(self, request: Request, call_next):
        peer = request.client.host if request.client else None
        request.state.peer = normalize(peer or "") or "unknown"
        request.state.client_ip = resolve_client_ip(
            peer, request.headers.get("x-forwarded-for"), self.trusted
        )
        return await call_next(request)
