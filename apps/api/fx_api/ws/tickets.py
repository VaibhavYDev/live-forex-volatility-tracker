"""Short-lived WebSocket auth tickets.

THE GOTCHA
----------
The browser ``WebSocket`` constructor **cannot set request headers**. There is no
API for it - it is not an oversight, it is the spec. So the reflex of sending
``Authorization: Bearer <jwt>`` simply does not work from a browser, and the
common workarounds are both bad:

* put the JWT in the query string - it then lands in every access log, every
  proxy log, and the browser's history, with its full lifetime intact;
* use a cookie - works, but reintroduces CSRF surface on an endpoint that has no
  other reason to care.

THE FIX
-------
Authenticate over ordinary HTTP (where headers work), receive a **single-use
ticket valid for 30 seconds**, and redeem it at the WebSocket handshake. If the
ticket leaks into a log it is already expired and already spent.

Tickets live in Redis rather than in process memory so any replica can redeem one
- which is the whole reason the API layer is stateless.
"""

from __future__ import annotations

import secrets

import redis.asyncio as aioredis

_PREFIX = "wsticket:"


class TicketStore:
    def __init__(self, redis: aioredis.Redis, ttl_s: int = 30) -> None:
        self._redis = redis
        self._ttl_s = ttl_s

    async def issue(self, subject: str = "anonymous") -> str:
        token = secrets.token_urlsafe(32)
        await self._redis.set(f"{_PREFIX}{token}", subject, ex=self._ttl_s)
        return token

    async def redeem(self, token: str) -> str | None:
        """Atomically consume a ticket. Returns the subject, or None if invalid.

        GETDEL rather than GET-then-DEL: two commands would let the same ticket
        be redeemed twice by two concurrent handshakes, which is exactly the
        replay the single-use property exists to prevent.
        """
        if not token:
            return None
        subject = await self._redis.getdel(f"{_PREFIX}{token}")
        return str(subject) if subject is not None else None
