"""Correlation id, bound to a ContextVar so every log line carries it.

Honouring an INBOUND X-Request-Id matters: the chaos harness generates the id
client-side and uses it to join its own requests.csv against the proxy's logs.
Minting a fresh one unconditionally would break that join and, with it,
assertion A9.
"""

from __future__ import annotations

import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from app.logging_setup import request_id_var

_MAX_LEN = 64


class RequestIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        incoming = request.headers.get("x-request-id", "")
        # Truncate rather than reject: an oversized id is a client bug, not a
        # reason to fail a request, but it must not bloat every log line.
        rid = incoming[:_MAX_LEN] if incoming else uuid.uuid4().hex[:16]

        token = request_id_var.set(rid)
        request.state.request_id = rid
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)
        response.headers["X-Request-Id"] = rid
        return response
