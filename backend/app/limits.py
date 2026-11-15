"""In-process rate limiting (no external services).

One token bucket per (client, category). Categories:
  general  cheap GETs and PNGs (generous: a blink preloads hundreds of images)
  build    starting a NEW field build or an explicit retry (strict; charged by the route that
           knows whether a build would really start, see ``RateLimiter.consume``)
  ai       /api/explain?ai=1 (strict; each answer can cost an API call)

The client is the peer address, or, only when ``settings.trust_proxy`` is on, the entry of
X-Forwarded-For that our own proxy appended (``trusted_proxy_hops`` from the right; entries
further left are client-controlled and ignored). Over the limit: 429 with ``Retry-After``.
State lives in this process; with several workers each one limits separately.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Callable

from fastapi import HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from .config import settings

GENERAL, BUILD, AI = "general", "build", "ai"
EXEMPT_PATHS = {"/api/health", "/docs", "/openapi.json", "/redoc"}
MAX_BUCKETS = 20_000


class TokenBucket:
    """Refills continuously at ``rate`` tokens/s up to ``burst``."""

    __slots__ = ("rate", "burst", "tokens", "stamp")

    def __init__(self, rate: float, burst: float, now: float) -> None:
        self.rate, self.burst = rate, burst
        self.tokens, self.stamp = burst, now

    def _refill(self, now: float) -> None:
        if now > self.stamp:
            self.tokens = min(self.burst, self.tokens + (now - self.stamp) * self.rate)
        self.stamp = now

    def take(self, now: float, cost: float = 1.0) -> float:
        """Take ``cost`` tokens. Returns 0.0 if allowed, else the seconds until it would be."""
        self._refill(now)
        if self.tokens >= cost:
            self.tokens -= cost
            return 0.0
        return (cost - self.tokens) / self.rate if self.rate > 0 else math.inf

    def idle(self, now: float) -> bool:
        self._refill(now)
        return self.tokens >= self.burst


@dataclass
class Policy:
    per_min: float
    burst: float


def default_policies() -> dict[str, Policy]:
    return {
        GENERAL: Policy(settings.rate_general_per_min, settings.rate_general_burst),
        BUILD: Policy(settings.rate_build_per_min, settings.rate_build_burst),
        AI: Policy(settings.rate_ai_per_min, settings.rate_ai_burst),
    }


@dataclass
class RateLimiter:
    policies: dict[str, Policy] = field(default_factory=default_policies)
    clock: Callable[[], float] = time.monotonic
    buckets: dict[tuple[str, str], TokenBucket] = field(default_factory=dict)

    def check(self, client: str, category: str, cost: float = 1.0) -> float:
        """0.0 if the request may proceed (a token is taken), else seconds to wait."""
        pol = self.policies[category]
        now = self.clock()
        b = self.buckets.get((client, category))
        if b is None:
            if len(self.buckets) >= MAX_BUCKETS:
                self._prune(now)
            b = self.buckets[(client, category)] = TokenBucket(pol.per_min / 60.0, pol.burst, now)
        return b.take(now, cost)

    def consume(self, request: Request, category: str, cost: float = 1.0) -> None:
        """Charge the request's client; raise 429 (with Retry-After) when it is over the limit."""
        wait = self.check(client_ip(request), category, cost)
        if wait > 0:
            raise too_many(wait, category)

    def _prune(self, now: float) -> None:
        for k in [k for k, b in self.buckets.items() if b.idle(now)]:
            del self.buckets[k]
        if len(self.buckets) >= MAX_BUCKETS:  # everyone is active: drop the oldest half
            for k in sorted(self.buckets, key=lambda k: self.buckets[k].stamp)[: MAX_BUCKETS // 2]:
                del self.buckets[k]


def retry_after(wait: float) -> str:
    return str(max(1, math.ceil(wait)))


def too_many(wait: float, category: str) -> HTTPException:
    what = {BUILD: "starting new field builds", AI: "AI summaries"}.get(category, "requests")
    return HTTPException(
        429, f"Too many {what}; try again in {retry_after(wait)} s.",
        headers={"Retry-After": retry_after(wait)},
    )


def client_ip(request: Request) -> str:
    if settings.trust_proxy:
        hops = [h.strip() for h in request.headers.get("x-forwarded-for", "").split(",") if h.strip()]
        n = max(1, settings.trusted_proxy_hops)
        if len(hops) >= n:
            return hops[-n]
    return request.client.host if request.client else "unknown"


def category_for(request: Request) -> str | None:
    """Category charged by the middleware; None = not limited. Field builds are NOT charged
    here: only the route can tell a cache hit from a real build."""
    if request.method == "OPTIONS" or request.url.path in EXEMPT_PATHS:
        return None
    if request.url.path == "/api/explain" and request.query_params.get("ai", "0").lower() in ("1", "true", "yes"):
        return AI
    return GENERAL


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, limiter: RateLimiter | None = None) -> None:
        super().__init__(app)
        self.limiter = limiter or RateLimiter()

    async def dispatch(self, request: Request, call_next):
        cat = category_for(request)
        if cat is not None:
            wait = self.limiter.check(client_ip(request), cat)
            if wait > 0:
                exc = too_many(wait, cat)
                return JSONResponse({"detail": exc.detail}, status_code=429, headers=exc.headers)
        return await call_next(request)


def get_limiter(request: Request) -> RateLimiter:
    """The limiter shared by the middleware and the routes (``app.state.limiter``)."""
    return request.app.state.limiter
