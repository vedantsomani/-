"""Two extra views: a six-detector false-colour image and a contamination-flag overlay.

GET /api/fields/{key}/rgb/s{season}.png    false-colour RGB of one season's detector stacks
GET /api/fields/{key}/rgb/s{season}.json   which detectors/bands went into it (also in X-RGB-* headers)
GET /api/fields/{key}/frames/{frame_id}/flags.png   transparent flag-class overlay for one exposure
GET /api/fields/{key}/stacks/{stack_id}/coverage.png   red where no usable exposure contributes

RGB: blue = D1+D2 (0.75-1.6 um), green = D3+D4 (1.6-3.8 um), red = D5+D6 (3.8-5.0 um). Each channel
is an inverse-variance mean of its detectors' season stacks (MJy/sr, already background-subtracted),
divided by its own robust noise so the sky is neutral, then one Lupton-style asinh stretch of the
summed intensity scales all three channels together (colour is preserved; saturated cores stay
coloured instead of clipping to white).
"""

from __future__ import annotations

import asyncio
import io

import numpy as np
from astropy.stats import sigma_clipped_stats
from fastapi import APIRouter, HTTPException, Query, Request, Response
from PIL import Image

from .config import settings
from .store import frame_arrays, stack_arrays, touch_field

router = APIRouter()

# channel -> (detectors, wavelength range in um)
CHANNELS: dict[str, tuple[tuple[int, int], tuple[float, float]]] = {
    "red": ((5, 6), (3.8, 5.0)),
    "green": ((3, 4), (1.6, 3.8)),
    "blue": ((1, 2), (0.75, 1.6)),
}


# --- RGB ----------------------------------------------------------------------------------------

def _noise(x: np.ndarray) -> float | None:
    good = np.isfinite(x)
    if good.sum() < 10:
        return None
    _, _, std = sigma_clipped_stats(x[good], sigma=3.0, maxiters=5)
    return float(std) if np.isfinite(std) and std > 0 else None


def combine_channel(stacks: list[dict[str, np.ndarray]]) -> np.ndarray | None:
    """Inverse-variance mean (by each stack's own noise) of detector stacks; NaN where none covers."""
    num = den = None
    for a in stacks:
        sci = a["sci"].astype(np.float64)
        ok = (a["footprint"] > 0.5) & np.isfinite(sci)
        sig = _noise(np.where(ok, sci, np.nan))
        if sig is None:
            continue
        w = np.where(ok, 1.0 / sig**2, 0.0)
        s = np.where(ok, w * np.nan_to_num(sci), 0.0)
        num, den = (s, w) if num is None else (num + s, den + w)
    if num is None:
        return None
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(den > 0, num / den, np.nan)


def lupton_rgb(
    chans: dict[str, np.ndarray | None], black: float = 1.0, white: float = 30.0, soft: float = 3.0
) -> tuple[np.ndarray, np.ndarray, dict[str, float | None]]:
    """chans: red/green/blue images in MJy/sr (NaN = no data). Returns (rgb uint8 HxWx3, covered
    mask, per-channel noise). black/white/soft are in channel sigma (white = where the mean
    intensity reaches full scale, soft = asinh linear-to-log knee)."""
    ref = next(c for c in chans.values() if c is not None)
    covered = np.zeros(ref.shape, bool)
    f = []
    noises: dict[str, float | None] = {}
    for name in ("red", "green", "blue"):
        img = chans.get(name)
        sig = _noise(img) if img is not None else None
        noises[name] = sig
        if img is None or sig is None:
            f.append(np.zeros(ref.shape))
            continue
        covered |= np.isfinite(img)
        f.append(np.nan_to_num(img / sig) - black)
    r, g, b = f
    inten = (r + g + b) / 3.0
    norm = np.arcsinh(max(white - black, 1e-6) / soft)
    stretched = np.arcsinh(np.clip(inten, 0, None) / soft) / norm
    with np.errstate(invalid="ignore", divide="ignore"):
        factor = np.where(inten > 1e-9, stretched / inten, 0.0)
    rgb = np.clip(np.dstack([r, g, b]), 0, None) * factor[..., None]
    peak = rgb.max(axis=2, keepdims=True)
    rgb = np.where(peak > 1, rgb / np.where(peak > 0, peak, 1), rgb)  # keep hue, never clip one channel
    return np.round(rgb * 255).astype(np.uint8), covered, noises


def _to_png(rgba: np.ndarray) -> bytes:
    # FITS row 0 is south; PNG row 0 is the top -> flip so north is up.
    buf = io.BytesIO()
    Image.fromarray(np.ascontiguousarray(np.flipud(rgba)), mode="RGBA").save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def season_stacks(job, season: int) -> dict[int, str]:
    """detector -> stack id of the whole-detector season stack."""
    return {
        m["detector"]: sid
        for sid, m in job.stacks.items()
        if m.get("season") == season and m.get("bin") is None and m.get("visit") is None
    }


def render_rgb(job, season: int, white: float = 30.0, black: float = 1.0, soft: float = 3.0):
    """-> (png bytes, info dict). Raises LookupError when the season has no stacks."""
    have = season_stacks(job, season)
    if not have:
        raise LookupError(season)
    chans: dict[str, np.ndarray | None] = {}
    info_ch: dict[str, dict] = {}
    for name, (dets, band) in CHANNELS.items():
        used = [d for d in dets if d in have]
        chans[name] = combine_channel([stack_arrays(job.key, have[d]) for d in used]) if used else None
        info_ch[name] = {"detectors": used, "band_um": list(band)}
    if all(c is None for c in chans.values()):
        raise LookupError(season)
    rgb, covered, noises = lupton_rgb(chans, black=black, white=white, soft=soft)
    for name in info_ch:
        info_ch[name]["noise_mjysr"] = None if noises[name] is None else round(noises[name], 7)
    alpha = np.where(covered, 255, 0).astype(np.uint8)
    rgba = np.dstack([np.where(covered[..., None], rgb, 0).astype(np.uint8), alpha])
    present = sorted(d for c in info_ch.values() for d in c["detectors"])
    info = {
        "season": season,
        "detectors": present,
        "missing": [d for d in range(1, 7) if d not in present],
        "channels": info_ch,
    }
    return _to_png(rgba), info


# --- flag overlay -------------------------------------------------------------------------------

FLAG_COLOURS = {  # RGBA, semi-transparent so the image shows through
    "masked": (255, 50, 50, 150),
    "suspect": (255, 215, 0, 150),
    "artifact": (255, 0, 255, 130),
}


def _class_mask(bits: dict[str, int], names: list[str]) -> int:
    m = 0
    for n in names:
        m |= int(bits.get(n, 0))
    return m


def render_flags_png(flags: np.ndarray, footprint: np.ndarray, flag_bits: dict[str, int]) -> bytes:
    """Colour each pixel by its most severe flag class: masked > suspect > artefact.
    Informational flags (SOURCE, DICHROIC, FULLSAMPLE, ...) are not drawn."""
    fl = flags.astype(np.int64)
    rgba = np.zeros(fl.shape + (4,), np.uint8)
    classes = (
        ("artifact", settings.artifact_flags),
        ("suspect", settings.suspect_flags),
        ("masked", settings.mask_flags),  # last = wins
    )
    for cls, names in classes:
        m = _class_mask(flag_bits, names)
        if m:
            rgba[(fl & m) != 0] = FLAG_COLOURS[cls]
    rgba[footprint <= 0.5] = 0
    return _to_png(rgba)


def render_coverage_png(n: np.ndarray, footprint: np.ndarray) -> bytes:
    """Red where a covered pixel has no usable exposure (n == 0; the stack there is interpolated)."""
    rgba = np.zeros(n.shape + (4,), np.uint8)
    rgba[(n == 0) & (footprint > 0.5)] = FLAG_COLOURS["masked"]
    return _to_png(rgba)


# --- routes -------------------------------------------------------------------------------------

def _job(request: Request, key: str):
    job = request.app.state.fields.jobs.get(key)
    if job is None:
        raise HTTPException(404, "unknown field (request /api/frames first)")
    job.touch()
    touch_field(key)
    return job


def _png(data: bytes, headers: dict[str, str] | None = None) -> Response:
    return Response(data, media_type="image/png", headers={"Cache-Control": "public, max-age=86400", **(headers or {})})


def _rgb_headers(info: dict) -> dict[str, str]:
    return {
        "X-RGB-Detectors": ",".join(map(str, info["detectors"])),
        "X-RGB-Missing": ",".join(map(str, info["missing"])),
        "Access-Control-Expose-Headers": "X-RGB-Detectors, X-RGB-Missing",
    }


@router.get("/api/fields/{key}/rgb/s{season:int}.png")
async def rgb_png(
    request: Request,
    key: str,
    season: int,
    sig_hi: float = Query(30.0, ge=2, le=1000, description="mean intensity reaches full scale at this many sigma"),
    sig_lo: float = Query(1.0, ge=-5, le=10, description="black point in channel sigma"),
    soft: float = Query(3.0, ge=0.1, le=50, description="asinh softening (sigma): linear below, logarithmic above"),
):
    job = _job(request, key)
    try:
        data, info = await asyncio.to_thread(render_rgb, job, season, sig_hi, sig_lo, soft)
    except LookupError as exc:
        raise HTTPException(404, "no stacks for this season (field may still be building)") from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, "stack not cached") from exc
    return _png(data, _rgb_headers(info))


@router.get("/api/fields/{key}/rgb/s{season:int}.json")
async def rgb_info(request: Request, key: str, season: int, sig_hi: float = Query(30.0, ge=2, le=1000)):
    job = _job(request, key)
    try:
        _, info = await asyncio.to_thread(render_rgb, job, season, sig_hi)
    except (LookupError, FileNotFoundError) as exc:
        raise HTTPException(404, "no stacks for this season") from exc
    return info


@router.get("/api/fields/{key}/frames/{frame_id}/flags.png")
async def frame_flags_png(request: Request, key: str, frame_id: str):
    job = _job(request, key)
    meta = job.frames.get(frame_id)
    if meta is None:
        raise HTTPException(404, "unknown frame")
    try:
        a = frame_arrays(key, frame_id)
    except FileNotFoundError as exc:
        raise HTTPException(404, "frame not cached") from exc
    return _png(await asyncio.to_thread(render_flags_png, a["flags"], a["footprint"], meta.get("flag_bits", {})))


@router.get("/api/fields/{key}/stacks/{stack_id}/coverage.png")
async def stack_coverage_png(request: Request, key: str, stack_id: str):
    job = _job(request, key)
    if stack_id not in job.stacks:
        raise HTTPException(404, "unknown stack (field may still be building)")
    try:
        a = stack_arrays(key, stack_id)
    except FileNotFoundError as exc:
        raise HTTPException(404, "stack not cached") from exc
    return _png(await asyncio.to_thread(render_coverage_png, a["n"], a["footprint"]))
