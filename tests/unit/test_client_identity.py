"""Client identity resolution. Security-relevant: this picks the bucket key.

The attack these guard against: if the resolver can be talked into returning an
attacker-chosen value, every request mints a fresh full bucket and the rate
limiter does nothing while appearing to work perfectly.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "proxy"))

from app.middleware.client_identity import (  # noqa: E402
    normalize,
    parse_networks,
    resolve_client_ip,
)

pytestmark = pytest.mark.unit

LOOPBACK = parse_networks("127.0.0.1/32,::1/128")
WITH_BRIDGE = parse_networks("127.0.0.1/32,::1/128,172.16.0.0/12")


def test_untrusted_peer_xff_is_ignored() -> None:
    """XFF from an untrusted peer is attacker input and must not be consulted."""
    assert resolve_client_ip("203.0.113.9", "1.2.3.4", LOOPBACK) == "203.0.113.9"


def test_trusted_peer_consults_the_chain() -> None:
    assert resolve_client_ip("127.0.0.1", "203.0.113.9", LOOPBACK) == "203.0.113.9"


def test_rightmost_untrusted_wins() -> None:
    """Only the entries a trusted proxy appended can be believed."""
    chain = "1.1.1.1, 203.0.113.9, 127.0.0.1"
    assert resolve_client_ip("127.0.0.1", chain, LOOPBACK) == "203.0.113.9"


def test_all_trusted_chain_falls_back_to_the_peer_not_chain_zero() -> None:
    """THE bug this module exists to avoid.

    chain[0] is the leftmost, fully attacker-controlled value. Returning it
    means a client that varies X-Forwarded-For gets a brand new full bucket on
    every request.
    """
    chain = "127.0.0.1, 127.0.0.1"
    assert resolve_client_ip("127.0.0.1", chain, LOOPBACK) == "127.0.0.1"


def test_spoofing_cannot_mint_fresh_buckets() -> None:
    """200 distinct forged XFF values from an untrusted peer -> one identity."""
    identities = {
        resolve_client_ip("203.0.113.9", f"10.0.0.{i % 256}", LOOPBACK) for i in range(200)
    }
    assert identities == {"203.0.113.9"}


def test_docker_bridge_gateway_is_trusted_but_still_resolves_a_real_client() -> None:
    """Measured on this host: the peer for host traffic is 172.19.0.1.

    The bridge range is trusted so XFF is consulted at all, but a genuine
    client address in the chain must still win.
    """
    assert resolve_client_ip("172.19.0.1", "203.0.113.9", WITH_BRIDGE) == "203.0.113.9"
    # With no XFF at all, everything from the host shares this identity —
    # which is why the demo's 429 burst must be fired in parallel.
    assert resolve_client_ip("172.19.0.1", None, WITH_BRIDGE) == "172.19.0.1"


def test_ipv4_mapped_ipv6_folds_to_ipv4() -> None:
    """Otherwise one client gets two buckets depending on socket family."""
    assert normalize("::ffff:127.0.0.1") == "127.0.0.1"
    assert normalize("::ffff:203.0.113.9") == "203.0.113.9"


def test_ports_are_stripped() -> None:
    assert normalize("203.0.113.9:54321") == "203.0.113.9"
    assert normalize("[::1]:54321") == "::1"


def test_garbage_entries_are_skipped_not_used_as_identities() -> None:
    """A junk XFF entry must not become a bucket key of its own."""
    assert normalize("not-an-ip") is None
    assert resolve_client_ip("127.0.0.1", "not-an-ip, 203.0.113.9", LOOPBACK) == "203.0.113.9"
    # Entirely unusable chain -> fall back to the peer.
    assert resolve_client_ip("127.0.0.1", "not-an-ip, also-junk", LOOPBACK) == "127.0.0.1"


def test_missing_peer_is_not_an_exception() -> None:
    assert resolve_client_ip(None, None, LOOPBACK) == "unknown"


def test_default_trusted_set_excludes_rfc1918() -> None:
    """The default must not trust all private space.

    Docker Desktop's bridge gateway is an RFC1918 address; trusting the whole
    range by default would hand every container full XFF authority.
    """
    assert resolve_client_ip("10.0.0.5", "1.2.3.4", LOOPBACK) == "10.0.0.5"
    assert resolve_client_ip("192.168.1.5", "1.2.3.4", LOOPBACK) == "192.168.1.5"
