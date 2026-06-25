"""In-process rate limiting: a global per-IP blanket plus stricter per-endpoint
limits on the auth routes (login / register).

This is a sliding-window log kept in memory, which is the right tool for the current
single-process DigitalOcean App Platform deployment and for defense-in-depth. It is NOT
shared across processes: behind a multi-instance deploy each instance keeps its own
counters, so the real DoS front line there is a CDN/WAF — see ``docs/DEPLOY.md``.
"""
from __future__ import annotations

import threading
import time
from collections import deque

from fastapi import HTTPException, Request, status

from .config import settings

# Stop the key map from growing without bound under a flood of distinct IPs: once it
# exceeds this, evict keys whose window has fully drained. Bounds memory, not behavior.
_MAX_KEYS = 50_000


class InMemoryRateLimiter:
    """Sliding-window-log limiter, thread-safe (the app runs background worker threads
    alongside the request handlers, so the shared state must be locked)."""

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def hit(self, key: str, limit: int, window_s: float) -> tuple[bool, int]:
        """Record a request against ``key`` and report whether it's allowed.

        Returns ``(allowed, retry_after_seconds)``. When over the limit nothing is
        recorded (a rejected request shouldn't extend its own penalty), and the caller
        gets the seconds until the oldest hit in the window expires."""
        now = time.monotonic()
        cutoff = now - window_s
        with self._lock:
            log = self._hits.get(key)
            if log is None:
                log = deque()
                self._hits[key] = log
            while log and log[0] <= cutoff:
                log.popleft()
            if len(log) >= limit:
                retry_after = max(1, int(log[0] + window_s - now) + 1)
                return False, retry_after
            log.append(now)
            if len(self._hits) > _MAX_KEYS:
                self._evict(cutoff)
            return True, 0

    def _evict(self, cutoff: float) -> None:
        """Drop keys whose entire window has drained. Caller holds the lock."""
        stale = [k for k, dq in self._hits.items() if not dq or dq[-1] <= cutoff]
        for k in stale:
            del self._hits[k]

    def reset(self) -> None:
        """Clear all state — used by tests to isolate the process-global limiter."""
        with self._lock:
            self._hits.clear()


limiter = InMemoryRateLimiter()


def client_ip(request: Request) -> str:
    """Best-effort client IP. Behind a proxy (a load balancer, nginx, …) the socket peer is the
    proxy, so trust the left-most ``X-Forwarded-For`` hop when configured to. Disable
    ``JOBSCOUT_TRUST_FORWARDED_FOR`` for a directly-exposed server, where the header is
    attacker-controlled and would let a client forge a fresh IP per request."""
    if settings.trust_forwarded_for:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def check_global(request: Request) -> tuple[bool, int]:
    """The per-IP blanket limit, for the global middleware. Returns ``(allowed,
    retry_after)`` rather than raising, since ASGI middleware must return a response
    itself (FastAPI's HTTPException handler wraps the router, not the middleware)."""
    if not settings.rate_limit_enabled:
        return True, 0
    return limiter.hit(
        f"global:{client_ip(request)}", settings.rate_limit_global_per_minute, 60.0
    )


def enforce(request: Request, *, scope: str, limit: int, window_s: float) -> None:
    """Raise 429 (with a ``Retry-After`` header) if this IP is over ``limit`` for
    ``scope`` in the window. No-op when rate limiting is disabled."""
    if not settings.rate_limit_enabled:
        return
    allowed, retry_after = limiter.hit(f"{scope}:{client_ip(request)}", limit, window_s)
    if not allowed:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Too many requests. Please slow down and try again later.",
            headers={"Retry-After": str(retry_after)},
        )


def rate_limit(scope: str, limit: int, window_s: float):
    """A FastAPI dependency that enforces a per-IP limit on a single route, e.g.
    ``Depends(rate_limit("login", 5, 60))``."""

    def _dep(request: Request) -> None:
        enforce(request, scope=scope, limit=limit, window_s=window_s)

    return _dep
