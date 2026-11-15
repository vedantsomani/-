"""Keep a free-tier host awake by requesting our own public health URL.

Hosts such as Render's free plan stop a web service after ~15 minutes without inbound
HTTP traffic. A request to the service's PUBLIC URL counts as inbound traffic, so pinging
ourselves on a timer prevents the idle stop while the process is running. It cannot wake
a service that is already stopped, and it does not make an ephemeral disk persistent.

Enabled when a URL is known: SPHEREX_KEEPALIVE_URL, else RENDER_EXTERNAL_URL (set by
Render automatically). Disable with SPHEREX_KEEPALIVE=0. Interval: SPHEREX_KEEPALIVE_S
(default 600 s, minimum 60 s).
"""

from __future__ import annotations

import asyncio
import logging
import os

import httpx

log = logging.getLogger(__name__)

DEFAULT_INTERVAL_S = 600.0
MIN_INTERVAL_S = 60.0


def keepalive_url(env: dict[str, str] | None = None) -> str | None:
    """Health URL to ping, or None when keep-alive is off or no public URL is known."""
    env = os.environ if env is None else env
    if env.get("SPHEREX_KEEPALIVE", "1").strip().lower() in ("0", "false", "off", "no"):
        return None
    base = (env.get("SPHEREX_KEEPALIVE_URL") or env.get("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")
    if not base.startswith(("http://", "https://")):
        return None
    return base if base.endswith("/api/health") else f"{base}/api/health"


def keepalive_interval(env: dict[str, str] | None = None) -> float:
    env = os.environ if env is None else env
    try:
        return max(MIN_INTERVAL_S, float(env.get("SPHEREX_KEEPALIVE_S", DEFAULT_INTERVAL_S)))
    except ValueError:
        return DEFAULT_INTERVAL_S


async def run(url: str, interval: float) -> None:
    """Ping `url` every `interval` seconds until cancelled. Failures are logged, never raised."""
    log.info("keep-alive: pinging %s every %.0f s", url, interval)
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        while True:
            await asyncio.sleep(interval)
            try:
                r = await client.get(url, headers={"User-Agent": "gamas-keepalive"})
                if r.status_code >= 400:
                    log.warning("keep-alive: %s returned %s", url, r.status_code)
            except httpx.HTTPError as exc:
                log.warning("keep-alive: %s failed: %s", url, exc)


def start() -> asyncio.Task | None:
    """Start the keep-alive task if configured. Call from the app lifespan."""
    url = keepalive_url()
    return asyncio.create_task(run(url, keepalive_interval())) if url else None
