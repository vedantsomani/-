"""Co-adds ("stacks") of reprojected exposures.

A stack is the inverse-variance weighted mean of a group of frames that share
the field grid, after excluding flagged pixels and per-pixel outliers (cosmic
rays, satellites, fast asteroids). Grouping is generic: Milestone 1 groups by
(season, detector); wavelength-binned stacks reuse the same machinery.
"""

from __future__ import annotations

import math
import warnings
from collections import defaultdict
from collections.abc import Callable, Iterable

import numpy as np
from astropy.convolution import Gaussian2DKernel, interpolate_replace_nans
from astropy.stats import sigma_clipped_stats
from astropy.time import Time

# Bump when stack arrays/meta change so cached stacks are rebuilt.
STACK_VERSION = "3"  # 2: per-visit stack meta; 3: reproduced outliers are kept

# Per-pixel outlier rejection: a frame is rejected at a pixel when it deviates
# from the per-pixel median by more than CLIP_SIGMA * its own sigma plus
# CLIP_FRAC * |median|. The fractional term protects bright-star cores, whose
# surface brightness legitimately differs between frames (each frame samples a
# different wavelength and a slightly different PSF).
CLIP_SIGMA = 5.0
CLIP_FRAC = 0.3
MIN_FRAMES_FOR_CLIP = 3
# ...unless another usable frame reproduces the outlying value: |x_j - x_i| <=
# CLIP_SIGMA * sqrt(var_i + var_j) + CLIP_FRAC * |x_i|. A cosmic ray or satellite
# hit is unique to one frame; a real source whose spectrum changes strongly inside
# the stack's wavelength range (deep T-dwarf bands) is bright in EVERY frame at its
# bright wavelengths, so those frames support each other and are kept. Rejecting
# them distorted the PSF differently per season and faked centroid shifts
# (docs/sensitivity.md). A value seen in only one frame is still rejected.


def _reproduced(x: np.ndarray, var: np.ndarray, ok: np.ndarray, outlier: np.ndarray) -> np.ndarray:
    """(n, ny, nx) bool: outlying pixel-frames whose value another usable frame matches."""
    out = np.zeros_like(outlier)
    sig = np.sqrt(np.where(ok, var, np.inf))
    for i in np.flatnonzero(outlier.reshape(len(x), -1).any(axis=1)):
        with np.errstate(invalid="ignore"):
            tol = CLIP_SIGMA * np.hypot(sig[i], sig) + CLIP_FRAC * np.abs(x[i])
            match = ok & (np.abs(x - x[i]) <= tol)
        match[i] = False
        out[i] = outlier[i] & match.any(axis=0)
    return out


def coadd(frames: list[dict[str, np.ndarray]]) -> dict[str, np.ndarray]:
    """Inverse-variance co-add of frames already on a common grid.

    Each frame dict needs: sci, var, footprint, excluded, wave.
    Returns sci (weighted mean, holes interpolated), var (1/sum w), n (frames
    used per pixel), wave (weighted-mean wavelength map), footprint (0/1).
    """
    sci = np.stack([f["sci"] for f in frames]).astype(np.float64)
    var = np.stack([f["var"] for f in frames]).astype(np.float64)
    foot = np.stack([f["footprint"] for f in frames]) > 0.5
    excl = np.stack([f["excluded"] for f in frames]).astype(bool)
    wave = np.stack([f["wave"] for f in frames]).astype(np.float64)

    ok = foot & ~excl & np.isfinite(sci) & np.isfinite(var) & (var > 0)

    if len(frames) >= MIN_FRAMES_FOR_CLIP:
        x = np.where(ok, sci, np.nan)
        with np.errstate(all="ignore"):
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)  # all-NaN columns
                med = np.nanmedian(x, axis=0)
            tol = CLIP_SIGMA * np.sqrt(np.where(var > 0, var, np.inf)) + CLIP_FRAC * np.abs(med)
            outlier = np.nan_to_num(np.abs(x - med) > tol, nan=False).astype(bool)
        if outlier.any():
            outlier &= ~_reproduced(x, var, ok, outlier)
        ok &= ~outlier

    w = np.where(ok, 1.0 / np.where(var > 0, var, np.inf), 0.0)
    wsum = w.sum(axis=0)
    n = ok.sum(axis=0)
    with np.errstate(invalid="ignore", divide="ignore"):
        mean = (w * np.nan_to_num(sci)).sum(axis=0) / wsum
        var_s = 1.0 / wsum
        wave_s = (w * np.nan_to_num(wave)).sum(axis=0) / wsum

    covered = foot.any(axis=0)
    holes = covered & (n == 0)
    mean[~covered] = np.nan
    if holes.any():  # every frame excluded here: fill for display only
        mean[holes] = np.nan
        with warnings.catch_warnings():  # large masked areas (saturated stars) are expected
            warnings.simplefilter("ignore")
            mean = interpolate_replace_nans(mean, Gaussian2DKernel(1.0))
        mean[~covered] = np.nan

    # Re-zero the background (frames are individually background-subtracted,
    # but different coverage per frame can leave a small pedestal).
    good = covered & (n > 0) & np.isfinite(mean)
    if good.sum() >= 10:
        _, bg, _ = sigma_clipped_stats(mean[good], sigma=3.0, maxiters=5)
        mean = mean - bg

    return {
        "sci": mean.astype(np.float32),
        "var": np.where(n > 0, var_s, np.nan).astype(np.float32),
        "n": n.astype(np.int16),
        "wave": np.where(n > 0, wave_s, np.nan).astype(np.float32),
        "footprint": covered.astype(np.float32),
    }


def robust_noise(sci: np.ndarray, n: np.ndarray | None = None) -> float | None:
    """Robust pixel-to-pixel noise (sigma-clipped std) of a background-subtracted image."""
    good = np.isfinite(sci) if n is None else np.isfinite(sci) & (n > 0)
    if good.sum() < 10:
        return None
    _, _, std = sigma_clipped_stats(sci[good], sigma=3.0, maxiters=5)
    return float(std) if math.isfinite(std) and std > 0 else None


GroupKey = tuple


def group_frames(
    metas: Iterable[dict], key: Callable[[dict], GroupKey | None]
) -> dict[GroupKey, list[dict]]:
    groups: dict[GroupKey, list[dict]] = defaultdict(list)
    for m in metas:
        k = key(m)
        if k is not None:
            groups[k].append(m)
    return groups


def season_detector_key(m: dict) -> GroupKey | None:
    # Frames with a dead pixel at the target still contribute elsewhere (their
    # excluded mask removes the bad pixels), so keep them.
    return (m["season"], m["detector"])


def visit_detector_key(m: dict) -> GroupKey | None:
    """Group key for one spacecraft visit (one fly-over, a few days) of one detector.

    The visit id comes from the observation id ({year}W{week}_{segment}_{visit}), so it
    is unique per fly-over; frames of one visit sit within ~a day of each other.
    """
    if not m.get("visit"):
        return None
    return (m["season"], m["visit"], m["detector"])


def stack_meta(stack_id: str, members: list[dict], arrays: dict[str, np.ndarray], grid_n: int,
               visit: str | None = None) -> dict:
    mjds = [m["mjd"] for m in members]
    waves = [m["wavelength_um"] for m in members if m.get("wavelength_um")]
    c = grid_n // 2
    noise = robust_noise(arrays["sci"], arrays["n"])
    frame_noise = [m["noise_mjysr"] for m in members if m.get("noise_mjysr")]
    mjd = float(np.mean(mjds))
    wave_c = float(arrays["wave"][c, c])
    return {
        "id": stack_id,
        "season": members[0]["season"],
        "detector": members[0]["detector"],
        # set for per-visit stacks (one fly-over), None for season-long stacks
        "visit": visit,
        "n_frames": len(members),
        "frame_ids": sorted(m["id"] for m in members),
        "mjd": mjd,
        "mjd_min": float(min(mjds)),
        "mjd_max": float(max(mjds)),
        "date": Time(mjd, format="mjd").isot[:19],
        "wavelength_um": round(wave_c, 5) if math.isfinite(wave_c) else None,
        "wavelength_min_um": round(min(waves), 5) if waves else None,
        "wavelength_max_um": round(max(waves), 5) if waves else None,
        "noise_mjysr": round(noise, 7) if noise else None,
        # Depth gain vs the median single exposure (ideal: sqrt(n_frames)).
        "depth_gain": round(float(np.median(frame_noise)) / noise, 2) if noise and frame_noise else None,
        "n_at_target": int(arrays["n"][c, c]),
    }
