"""Maps a caught exception to what it MEANS operationally.

"The region went down" describes at least four different physical events, and
they produce different exceptions, different latencies, and different correct
responses. This module is where that distinction lives.

  docker stop            -> ConnectError        ~2 ms   Docker's embedded DNS
                                                        withdraws the A record,
                                                        so the NAME stops
                                                        resolving. Not a timeout.
  docker stop, warm pool -> RemoteProtocolError ~2 ms   No DNS lookup happens;
                                                        we write into a socket
                                                        whose peer is gone.
  docker kill            -> ReadError           ~2 ms   RST on pooled sockets.
  docker pause           -> ReadTimeout         200 ms  The kernel still ACKs the
                                                        SYN from the listen
                                                        backlog, so connect
                                                        SUCCEEDS and then hangs.
  network disconnect     -> ConnectError        ~2 ms   Disconnect withdraws the
                                                        endpoint's DNS record
                                                        too, so it fails like
                                                        stop — a true unanswered
                                                        SYN needs a firewall.
  pool saturated         -> PoolTimeout         50 ms

The headline consequence: the brief specifies a 200 ms timeout AND `docker stop`
as the fault injection, and those two never meet. Only `docker pause` or the
region's `blackhole` chaos mode reaches the read timeout. (ADR-012)
"""

from __future__ import annotations

from enum import Enum

import httpx


class FailureClass(str, Enum):
    """Did the request reach the server?

    NOT_SENT   no bytes arrived; replaying costs nothing.
    MAYBE_SENT bytes may have arrived and only the response was lost; replaying
               could duplicate a side effect. Safe here only because inference
               is deterministic and side-effect-free, which is recorded as an
               explicit property rather than assumed. (ADR-006)
    """

    NOT_SENT = "NOT_SENT"
    MAYBE_SENT = "MAYBE_SENT"
    RESPONSE_ERROR = "RESPONSE_ERROR"


class UpstreamStatusError(Exception):
    """A region answered, but with a status that means it is unhealthy."""

    def __init__(self, status: int, response: httpx.Response) -> None:
        super().__init__(f"upstream returned {status}")
        self.status = status
        self.response = response


# Order matters: httpx.ConnectTimeout is a subclass of httpx.TimeoutException,
# and ReadTimeout/WriteTimeout/PoolTimeout are siblings — but ConnectTimeout is
# NOT a subclass of ConnectError, despite the names. Checking the specific
# classes before their bases keeps the mapping honest.
_TAXONOMY: tuple[tuple[type[Exception], FailureClass, str], ...] = (
    (httpx.ConnectTimeout, FailureClass.NOT_SENT, "connect_timeout"),
    (httpx.PoolTimeout, FailureClass.NOT_SENT, "pool_timeout"),
    (httpx.ConnectError, FailureClass.NOT_SENT, "connect_error"),
    (httpx.ReadTimeout, FailureClass.MAYBE_SENT, "read_timeout"),
    (httpx.WriteTimeout, FailureClass.MAYBE_SENT, "write_timeout"),
    (httpx.RemoteProtocolError, FailureClass.MAYBE_SENT, "protocol_error"),
    (httpx.LocalProtocolError, FailureClass.NOT_SENT, "local_protocol_error"),
    (httpx.ReadError, FailureClass.MAYBE_SENT, "read_error"),
    (httpx.WriteError, FailureClass.MAYBE_SENT, "write_error"),
    (httpx.ProxyError, FailureClass.NOT_SENT, "proxy_error"),
    (httpx.UnsupportedProtocol, FailureClass.NOT_SENT, "unsupported_protocol"),
    # Broad nets last.
    (httpx.TimeoutException, FailureClass.MAYBE_SENT, "timeout"),
    (httpx.TransportError, FailureClass.MAYBE_SENT, "transport_error"),
)


def classify(exc: BaseException) -> tuple[FailureClass, str]:
    """Exception -> (class, code). Re-raises anything it does not recognise.

    An unknown exception is OUR bug. Silently counting it as an upstream failure
    would blame a healthy region for a defect in this process, and would trip a
    circuit breaker on evidence that has nothing to do with the region.
    """
    if isinstance(exc, UpstreamStatusError):
        return FailureClass.RESPONSE_ERROR, f"http_{exc.status}"

    # TimeoutError (builtin) is what asyncio.timeout raises when the per-attempt
    # ceiling fires before httpx notices. Distinct code so the two are never
    # confused in the report.
    if isinstance(exc, TimeoutError) and not isinstance(exc, httpx.TimeoutException):
        return FailureClass.MAYBE_SENT, "attempt_deadline"

    for exc_type, fclass, code in _TAXONOMY:
        if isinstance(exc, exc_type):
            return fclass, code

    raise exc


def is_replayable(fclass: FailureClass, *, idempotent: bool) -> bool:
    """May we send this request to another region after this failure?

    NOT_SENT is always safe. MAYBE_SENT needs the request to be idempotent,
    which for this API it is: inference is a pure function of its input.
    """
    if fclass is FailureClass.NOT_SENT:
        return True
    return idempotent
