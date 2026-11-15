"""SPHEREx Time Machine API."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from .config import settings
from .coords import CoordError, parse_dec, parse_ra, resolve_name
from .coords import PM_THRESHOLD_MASYR, SPHEREX_REF_MJD
from .fields import FieldManager
from .fingerprint import FingerprintManager, Target
from .processing import Grid
from .analysis import diff_arrays
from . import batch, colour, explain, investigation, overlay
from .limits import BUILD, RateLimiter, RateLimitMiddleware, client_ip
from .store import touch_field
from .render import aperture_photometry, render_diff_png, render_frame_png, render_stack_png

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("spherex")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.cache_dir.mkdir(parents=True, exist_ok=True)
    app.state.fields = FieldManager()
    app.state.fingerprints = FingerprintManager()
    app.state.overlays = overlay.OverlayManager()
    janitor = asyncio.create_task(app.state.fields.janitor())
    yield
    janitor.cancel()
    await asyncio.gather(janitor, return_exceptions=True)
    await app.state.overlays.close()
    await app.state.fingerprints.close()
    await app.state.fields.close()


app = FastAPI(title="SPHEREx Time Machine", version="0.1.0", lifespan=lifespan)
app.include_router(explain.router)
app.include_router(overlay.router)
app.include_router(colour.router)
app.include_router(investigation.router)
app.include_router(batch.router)
try:  # the held-out validation routes live in their own module
    from . import holdout
    app.include_router(holdout.router)
except ImportError as exc:  # pragma: no cover
    log.info("holdout routes not loaded: %s", exc)

limiter = RateLimiter()
app.state.limiter = limiter
app.add_middleware(RateLimitMiddleware, limiter=limiter)
# CORS goes on last = outermost, so 429 responses carry the CORS headers too.
app.add_middleware(
    CORSMiddleware, allow_origins=settings.cors_origins, allow_methods=["GET", "DELETE"],
    allow_headers=["*"], expose_headers=["Retry-After"],
)


def _manager() -> FieldManager:
    return app.state.fields


def _parse(ra: str, dec: str) -> tuple[float, float]:
    try:
        return parse_ra(ra), parse_dec(dec)
    except CoordError as exc:
        raise HTTPException(422, str(exc)) from exc


def _grid(ra: float, dec: float, fov: float | None) -> Grid:
    fov = fov or settings.default_fov_arcmin
    if not 0.5 <= fov <= settings.max_fov_arcmin:
        raise HTTPException(422, f"fov must be between 0.5 and {settings.max_fov_arcmin} arcmin")
    # Snap the centre to 1e-5 deg (0.036", << 6" pixel) so requests for "the same"
    # position computed slightly differently (e.g. proper-motion propagation in the
    # browser) share one cached field instead of re-downloading every epoch.
    return Grid.for_fov(round(ra, 5), round(dec, 5), fov, settings.pixel_scale_arcsec)


@app.get("/api/health")
async def health():
    mgr = _manager()
    states: dict[str, int] = {}
    for j in list(mgr.jobs.values()):
        states[j.status] = states.get(j.status, 0) + 1
    u = await mgr.cache_status()
    return {
        "ok": True, "collections": settings.collections,
        "cache": {"bytes": u["bytes"], "limit_bytes": u["limit_bytes"], "fraction": u["fraction"],
                  "by_dir": u["by_dir"]},
        "jobs": states,
        "builds": {"max_concurrent": settings.max_concurrent_builds},
    }


def _admit(request: Request, grid: Grid, refresh: bool = False, retry: bool = False, update: bool = False) -> str:
    """Charge the strict build rate limit only when this request would really start work."""
    plan = _manager().plan(grid, refresh=refresh, retry=retry, update=update)
    if plan != "none":
        request.app.state.limiter.consume(request, BUILD)
    return plan


@app.get("/api/frames")
async def frames(
    request: Request,
    ra: str = Query(..., description="RA: decimal deg or sexagesimal hours"),
    dec: str = Query(..., description="Dec: decimal deg or sexagesimal deg"),
    fov: float | None = Query(None, description="field of view, arcmin"),
    refresh: bool = Query(False, description="ignore cached manifest and rebuild"),
    retry: bool = Query(False, description="re-attempt exposures that failed with transient errors"),
    update: bool = Query(False, description="re-query the archive and process only exposures not seen before"),
):
    """Start (or return) the field build. Poll until ``status == 'complete'``;
    ``frames`` grows as cutouts are processed."""
    ra_d, dec_d = _parse(ra, dec)
    grid = _grid(ra_d, dec_d, fov)
    _admit(request, grid, refresh, retry, update)
    mgr = _manager()
    if update and not refresh:
        try:  # response carries `new_frames` (found by this very check) and `last_checked`
            return mgr.describe(await mgr.update(grid, client=client_ip(request)))
        except httpx.HTTPError as exc:
            raise HTTPException(502, f"IRSA archive unavailable: {exc}") from exc
    return mgr.describe(mgr.get_or_start(grid, refresh=refresh, retry=retry, client=client_ip(request)))


@app.delete("/api/fields/{key}")
async def release_field(key: str, request: Request):
    """'I no longer need this field.' Drops the caller's interest only; a build other visitors
    still poll keeps running, and one nobody polls any more is cancelled after a grace period
    (finished frames stay cached, so asking again resumes)."""
    return _manager().release(key, client_ip(request))


class RenderParams:
    """Shared query parameters for every rendered PNG."""

    def __init__(
        self,
        stretch: str = Query("asinh", pattern="^(linear|asinh|log|sqrt)$"),
        scale: str = Query("noise", pattern="^(noise|frame|detector)$"),
        lo: float = Query(1.0, ge=0, le=50, description="percentile black point (scale=frame|detector)"),
        hi: float = Query(99.5, ge=50, le=100, description="percentile white point"),
        sig_lo: float = Query(-2.0, ge=-20, le=10, description="noise scale: black point in sigma"),
        sig_hi: float = Query(30.0, ge=1, le=1000, description="noise scale: white point in sigma"),
    ):
        self.kw = dict(stretch=stretch, scale=scale, lo=lo, hi=hi, sig_lo=sig_lo, sig_hi=sig_hi)


def _png(data: bytes) -> Response:
    return Response(data, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})


def _job(key: str):
    job = _manager().jobs.get(key)
    if job is None:
        raise HTTPException(404, "unknown field (request /api/frames first)")
    job.touch()
    touch_field(key)
    return job


@app.get("/api/fields/{key}/frames/{frame_id}.png")
async def frame_png(key: str, frame_id: str, p: RenderParams = Depends()):
    job = _job(key)
    if frame_id not in job.frames:
        raise HTTPException(404, "unknown frame")
    try:
        return _png(await asyncio.to_thread(render_frame_png, job, frame_id, **p.kw))
    except FileNotFoundError as exc:
        raise HTTPException(404, "frame not cached") from exc


@app.get("/api/fields/{key}/stacks/{stack_id}.png")
async def stack_png(key: str, stack_id: str, p: RenderParams = Depends()):
    job = _job(key)
    if stack_id not in job.stacks:
        raise HTTPException(404, "unknown stack (field may still be building)")
    try:
        return _png(await asyncio.to_thread(render_stack_png, job, stack_id, **p.kw))
    except FileNotFoundError as exc:
        raise HTTPException(404, "stack not cached") from exc


@app.get("/api/fields/{key}/diffs/{diff_id}.png")
async def diff_png(
    key: str,
    diff_id: str,
    zmax: float = Query(6.0, ge=1, le=50, description="colour saturates at +/- zmax sigma"),
    smooth: bool = Query(False, description="show the PSF-matched (detection) map"),
    norm: str = Query("global", pattern="^(global|local)$"),
    floor: float = Query(1.5, ge=0, le=5),
    gamma: float = Query(0.8, ge=0.3, le=2.5),
):
    job = _job(key)
    if not any(d["id"] == diff_id for d in job.analysis.get("diffs", [])):
        raise HTTPException(404, "unknown difference image")
    arr = diff_arrays(key, diff_id)
    z = arr["zs"] if smooth and "zs" in arr else arr["z"]
    if norm == "local":
        z = arr.get("zl", z)
    return _png(await asyncio.to_thread(render_diff_png, z, zmax, gamma=gamma, floor=floor))


@app.get("/api/resolve")
async def resolve(name: str = Query(..., min_length=1)):
    try:
        return (await resolve_name(name, _manager().client)).to_dict()
    except CoordError as exc:
        raise HTTPException(404, str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Sesame unavailable: {exc}") from exc


@app.get("/api/spectrum")
async def spectrum(request: Request, ra: str, dec: str, fov: float | None = None):
    """Quick-look forced photometry at (ra, dec) on every SPHEREx frame.

    Uses an already-loaded field that contains the position; otherwise starts a
    small field around it and returns ``status: 'processing'`` (poll again)."""
    ra_d, dec_d = _parse(ra, dec)
    mgr = _manager()
    job = mgr.find_job_containing(ra_d, dec_d)
    if job is None:
        grid = _grid(ra_d, dec_d, fov or 2.0)
        _admit(request, grid)
        job = mgr.get_or_start(grid, client=client_ip(request))
    points = aperture_photometry(job, ra_d, dec_d) if job.frames else []
    return {
        "ra": ra_d,
        "dec": dec_d,
        "field": job.key,
        "status": job.status,
        "done": len(job.frames),
        "total": job.total,
        "units": {"flux": "uJy", "wavelength": "um"},
        "points": points,
    }


@app.get("/api/fingerprint")
async def fingerprint(
    request: Request,
    ra: str = Query(..., description="RA at MJD `epoch`: decimal deg or sexagesimal hours"),
    dec: str = Query(..., description="Dec at MJD `epoch`"),
    fov: float | None = Query(None, description="field of view, arcmin (same field as the viewer)"),
    pmra: float = Query(0.0, ge=-30000, le=30000, description="proper motion RA*cos(dec), mas/yr"),
    pmdec: float = Query(0.0, ge=-30000, le=30000, description="proper motion Dec, mas/yr"),
    epoch: float = Query(SPHEREX_REF_MJD, ge=30000, le=90000, description="MJD of ra/dec"),
):
    """Mover Fingerprint: deblended forced PSF photometry of one source in every exposure
    of the field. Non-blocking like /api/frames: poll until ``status == 'complete'``."""
    ra_d, dec_d = _parse(ra, dec)
    target = Target(ra=ra_d, dec=dec_d, pmra=pmra, pmdec=pmdec, epoch_mjd=epoch)
    # Same field centre rule as the viewer (coords.field_centre): fast movers are centred
    # at their SPHEREx mid-survey position, everything else at the given position.
    if (pmra**2 + pmdec**2) ** 0.5 > PM_THRESHOLD_MASYR:
        ra_c, dec_c = target.at(SPHEREX_REF_MJD)
    else:
        ra_c, dec_c = ra_d, dec_d
    grid = _grid(ra_c, dec_c, fov)
    _admit(request, grid)
    return await app.state.fingerprints.request(_manager(), grid, target)
