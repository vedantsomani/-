"""Turn cached arrays (exposures or stacks) into PNGs, and quick-look photometry."""

from __future__ import annotations

import io
import math
from functools import lru_cache

import numpy as np
from PIL import Image

from .fields import FieldJob
from .phot import aperture_flux
from .store import frame_arrays, stack_arrays

STRETCHES = ("linear", "asinh", "log", "sqrt")


def _limits(data: np.ndarray, lo: float, hi: float) -> tuple[float, float]:
    finite = data[np.isfinite(data)]
    if finite.size == 0:
        return 0.0, 1.0
    vmin, vmax = np.percentile(finite, [lo, hi])
    if not vmax > vmin:
        vmax = vmin + 1e-6
    return float(vmin), float(vmax)


@lru_cache(maxsize=256)
def detector_limits(key: str, detector: int, lo: float, hi: float, frame_ids: tuple[str, ...]):
    """Shared stretch for all frames of one detector (median of per-frame limits)."""
    lims = [_limits(frame_arrays(key, f)["sci"], lo, hi) for f in frame_ids]
    return float(np.median([a for a, _ in lims])), float(np.median([b for _, b in lims]))


def encode_png(data: np.ndarray, footprint: np.ndarray, vmin: float, vmax: float, stretch: str) -> bytes:
    """Grey + alpha PNG (alpha=0 where there is no data), north up."""
    data = data.astype(np.float64)
    x = np.clip((data - vmin) / (vmax - vmin), 0, 1)
    if stretch == "asinh":
        a = 0.1
        x = np.arcsinh(x / a) / np.arcsinh(1 / a)
    elif stretch == "log":
        x = np.log10(1 + 999 * x) / 3
    elif stretch == "sqrt":
        x = np.sqrt(x)
    covered = (footprint > 0.5) & np.isfinite(data)
    gray = np.where(covered, np.round(np.nan_to_num(x) * 255), 0).astype(np.uint8)
    alpha = np.where(covered, 255, 0).astype(np.uint8)
    # FITS row 0 is the bottom (south); PNG row 0 is the top -> flip to put north up.
    img = Image.fromarray(np.dstack([np.flipud(gray), np.flipud(alpha)]), mode="LA")
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _vlims(data, noise, scale, lo, hi, sig_lo, sig_hi, detector_lims=None):
    if scale == "noise" and noise:
        return sig_lo * noise, sig_hi * noise  # data are background-subtracted
    if scale == "detector" and detector_lims:
        return detector_lims()
    return _limits(data, lo, hi)


def render_frame_png(
    job: FieldJob, frame_id: str, stretch="asinh", lo=1.0, hi=99.5, scale="noise", sig_lo=-2.0, sig_hi=30.0
) -> bytes:
    """scale: 'noise' -> limits at sig_lo..sig_hi x the frame's robust noise (best for
    blinking noise-limited frames); 'frame' / 'detector' -> lo..hi percentiles."""
    arr = frame_arrays(job.key, frame_id)
    meta = job.frames[frame_id]

    def det_lims():
        ids = tuple(sorted(f for f, m in job.frames.items() if m["detector"] == meta["detector"]))
        return detector_limits(job.key, meta["detector"], lo, hi, ids)

    vmin, vmax = _vlims(arr["sci"], meta.get("noise_mjysr"), scale, lo, hi, sig_lo, sig_hi, det_lims)
    return encode_png(arr["sci"], arr["footprint"], vmin, vmax, stretch)


def render_stack_png(
    job: FieldJob, stack_id: str, stretch="asinh", lo=1.0, hi=99.5, scale="noise", sig_lo=-2.0, sig_hi=30.0
) -> bytes:
    arr = stack_arrays(job.key, stack_id)
    meta = job.stacks[stack_id]
    if scale == "detector":
        # One stretch for all seasons of this detector, so brightness changes are real.
        sibs = [s for s in job.stacks.values()
                if s["detector"] == meta["detector"] and s.get("bin") == meta.get("bin") and s.get("noise_mjysr")]
        noise = float(np.median([s["noise_mjysr"] for s in sibs])) if sibs else meta.get("noise_mjysr")
        vmin, vmax = sig_lo * noise, sig_hi * noise
    else:
        vmin, vmax = _vlims(arr["sci"], meta.get("noise_mjysr"), scale, lo, hi, sig_lo, sig_hi)
    return encode_png(arr["sci"], arr["footprint"], vmin, vmax, stretch)


# Diverging palette for significance maps: blue = fainter in the later season,
# orange = brighter. Zero is the dark background so the dipoles pop.
_NEG = np.array([74, 163, 255], float)
_POS = np.array([255, 128, 48], float)
_ZERO = np.array([10, 12, 18], float)


DIFF_FLOOR_SIGMA = 1.5  # |z| below this renders as background, so only real change shows colour
DIFF_GAMMA = 0.8        # colour contrast exponent of the difference palette


def render_diff_png(z: np.ndarray, zmax: float = 6.0, gamma: float = DIFF_GAMMA,
                    floor: float = DIFF_FLOOR_SIGMA) -> bytes:
    good = np.isfinite(z)
    t = np.clip(np.nan_to_num(z) / zmax, -1, 1)
    a = np.clip((np.abs(np.nan_to_num(z)) - floor) / max(zmax - floor, 1e-6), 0, 1) ** gamma
    col = np.where((t >= 0)[..., None], _POS, _NEG)
    rgb = _ZERO * (1 - a[..., None]) + col * a[..., None]
    rgba = np.dstack([rgb.astype(np.uint8), np.where(good, 255, 0).astype(np.uint8)])
    img = Image.fromarray(np.flipud(rgba), mode="RGBA")
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def aperture_photometry(job: FieldJob, ra: float, dec: float, r_ap: float = 1.5) -> list[dict]:
    """Quick-look forced photometry at a fixed (ra, dec) on every exposure of a field.
    (Superseded by the proper-motion-aware fingerprint photometry in Milestone 3.)"""
    grid = job.grid
    x0, y0 = (float(v) for v in grid.wcs().world_to_pixel_values(ra, dec))
    ix, iy = int(round(x0)), int(round(y0))
    out = []
    for fid, meta in job.frames.items():
        a = frame_arrays(job.key, fid)
        if not (0 <= ix < grid.n and 0 <= iy < grid.n) or a["footprint"][iy, ix] < 0.5:
            continue
        res = aperture_flux(a["sci"], a["var"], ~a["excluded"], x0, y0, grid.pixscale_arcsec, r_ap)
        if res is None:
            continue
        flags_here = int(a["flags"][iy, ix])
        out.append(
            {
                "frame_id": fid,
                "mjd": meta["mjd"],
                "detector": meta["detector"],
                "wavelength_um": _f(a["wave"][iy, ix]),
                "bandwidth_um": _f(a["bandwidth"][iy, ix]),
                "flux_ujy": round(res[0], 3),
                "flux_err_ujy": round(res[1], 3),
                "flags": [n for n, m in meta.get("flag_bits", {}).items() if flags_here & m],
            }
        )
    out.sort(key=lambda r: (r["wavelength_um"] or 0, r["mjd"]))
    return out


def _f(v) -> float | None:
    v = float(v)
    return round(v, 5) if math.isfinite(v) else None
