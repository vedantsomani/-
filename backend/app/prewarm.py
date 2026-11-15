"""Pre-build (download + process + stack) fields so the demo is instant.

usage: python -m app.prewarm [--fov 5] "WISE 0855" "Barnard's Star" "133.76 -7.24" ...
Runs in-process (no web server needed); results land in the shared cache.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import time

from .config import settings
from .coords import field_centre, resolve_name
from .fields import FieldManager
from .processing import Grid

log = logging.getLogger("prewarm")


async def warm(targets: list[str], fov: float) -> None:
    mgr = FieldManager()
    try:
        jobs = []
        for t in targets:
            r = await resolve_name(t, mgr.client)
            ra, dec = field_centre(r)
            grid = Grid.for_fov(round(ra, 5), round(dec, 5), fov, settings.pixel_scale_arcsec)
            job = mgr.get_or_start(grid)
            log.info("%-28s -> %s (%.5f, %.5f) status=%s", t, job.key, grid.ra, grid.dec, job.status)
            jobs.append((t, job))
        t0 = time.time()
        while any(j.status not in ("complete", "error") for _, j in jobs):
            await asyncio.sleep(10)
            log.info(" | ".join(f"{t[:14]}: {j.status} {len(j.frames)}/{j.total}" for t, j in jobs))
        for t, j in jobs:
            log.info("%s: %s, %d frames, %d stacks, %d failed (%.0fs)", t, j.status, len(j.frames),
                     len(j.stacks), len(j.failed), time.time() - t0)
    finally:
        await mgr.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("targets", nargs="+")
    ap.add_argument("--fov", type=float, default=settings.default_fov_arcmin)
    args = ap.parse_args()
    asyncio.run(warm(args.targets, args.fov))


if __name__ == "__main__":
    main()
