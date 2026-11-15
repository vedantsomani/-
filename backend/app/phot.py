"""Aperture photometry primitives on the field grid (no I/O)."""

from __future__ import annotations

import math

import numpy as np


def ujy_per_mjysr_pixel(pixscale_arcsec: float) -> float:
    pix_sr = (pixscale_arcsec / 206264.806) ** 2
    return pix_sr * 1e12  # MJy/sr * sr = MJy -> uJy


def aperture_masks(n: int, x0: float, y0: float, r_ap: float, r_in: float, r_out: float):
    yy, xx = np.mgrid[0:n, 0:n]
    rr = np.hypot(xx - x0, yy - y0)
    return rr <= r_ap, (rr >= r_in) & (rr <= r_out)


def aperture_flux(sci, var, usable, x0, y0, pixscale_arcsec, r_ap=1.5, r_in=4.0, r_out=7.0):
    """Sum in a circular aperture minus annulus median. Returns (flux_uJy, err_uJy) or None
    when fewer than 70% of the aperture pixels are usable."""
    n = sci.shape[0]
    ap, ann = aperture_masks(n, x0, y0, r_ap, r_in, r_out)
    good = ap & usable & np.isfinite(sci)
    if good.sum() < max(1, 0.7 * ap.sum()):
        return None
    annv = sci[ann & usable & np.isfinite(sci)]
    sky = float(np.median(annv)) if annv.size >= 5 else 0.0
    k = ujy_per_mjysr_pixel(pixscale_arcsec) * ap.sum() / good.sum()
    flux = float(np.sum(sci[good] - sky)) * k
    err = math.sqrt(float(np.nansum(var[good]))) * k
    return flux, err
