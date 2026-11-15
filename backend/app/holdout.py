"""Predict, then reveal: a hold-out test of a proposed motion.

1. Pick one season (default the latest) and set it aside. Nothing from its pixels is
   read until the prediction is fixed: the code only loads held-out cutouts in step 5.
2. Registration floor: isolated Gaia stars in the field are measured with exactly the
   procedure used for the target, in the TRAINING groups only; their excess scatter
   about the Gaia positions is the floor added to every position error.
3. Train: one position per (season, detector) group (astrometry.measure_group),
   starting from the guess (ra/dec at ``epoch`` + optional proper motion; the proper
   motion only says where to start looking).
4. Fit linear motion (position at a reference epoch + proper motion) by weighted least
   squares to the training positions only; predict the position, with its error
   ellipse, at the held-out season's mean epoch (from metadata, not pixels).
5. Reveal: measure the held-out season with the same procedure, starting from the
   prediction. Compare, and contrast with the no-motion hypothesis.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np
from fastapi import APIRouter, HTTPException, Query, Request

from . import astrometry as am
from . import psf as psfmod
from .catalogs import CatalogResult, gaia_cone
from .config import settings
from .coords import PM_THRESHOLD_MASYR, SPHEREX_REF_MJD, CoordError, parse_dec, parse_ra
from .fingerprint import (NativeCutout, Source, Target, _clean, _sep_arcsec, frame_record, load_native,
                          path_samples, query_radius_deg, select_sources)
from .processing import Grid

log = logging.getLogger(__name__)
router = APIRouter()

HOLDOUT_VERSION = "2"
COMOVER_ARCSEC = 3.0          # a Gaia source that stays this close to the target IS the target
REF_STARS = 6
DEFAULT_FLOOR_ARCSEC = 1.0    # used only when too few reference measurements exist
MIN_FLOOR_ARCSEC = 0.05       # Gaia propagation + PSF-model limits; never claim better
CONFIRM_SIGMA = 2.0
FAIL_SIGMA = 3.0
RETRY_AFTER_S = 600
GAIA_MATCH_ARCSEC = 2.0       # the target IS this Gaia source when it sits this close (parallax model)
GAIA_PLX_MIN_SNR = 5.0        # parallax_over_error below this is not adopted
GAIA_PM_SLACK_MASYR = 30.0    # added to 3 sigma of the fitted proper motion in the consistency test
FREE_PLX_MIN_EPOCHS = 4
EPOCH_GAP_DAYS = 30.0         # measurements closer than this are one epoch
FREE_PLX_MAX_CORR = 0.95
IMPLIED_LABEL = "implied by the reveal, not a fit"

Loader = Callable[[dict], "tuple[NativeCutout, psfmod.PSFLibrary] | None"]


# =========================================================================== helpers
def _r(x, nd=6):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return round(x, nd) if math.isfinite(x) else None


def groups_of(frames: list[dict]) -> dict[tuple[int, int], list[dict]]:
    g: dict[tuple[int, int], list[dict]] = defaultdict(list)
    for f in frames:
        if f.get("mjd") is not None and f.get("season") is not None:
            g[(int(f["season"]), int(f["detector"]))].append(f)
    return {k: sorted(v, key=lambda m: m["mjd"]) for k, v in sorted(g.items())}


def neighbours_for(target: Target, cat: CatalogResult, mjds: list[float]) -> tuple[list[Source], list[str]]:
    """Gaia neighbours along the target's (guessed) path, minus any Gaia entry that moves
    WITH the target: that is the target itself (its catalogue proper motion would hand the
    answer to the fit as a fixed 'neighbour')."""
    sel = select_sources(target, cat, mjds)
    ts = path_samples(mjds)
    path = np.array([target.at(t) for t in ts])
    keep, notes = [], list(sel.notes)
    for s in sel.sources[1:]:
        pos = np.array([s.at(t) for t in ts])
        far = float(np.max(_sep_arcsec(path[:, 0], path[:, 1], pos[:, 0], pos[:, 1])))
        if far < COMOVER_ARCSEC and math.hypot(s.pmra, s.pmdec) > 0:
            notes.append(f"Gaia {s.gaia_id} co-moves with the target (always < {COMOVER_ARCSEC:.0f}\"); it is the "
                         "target itself and is not used as a fixed neighbour.")
            continue
        keep.append(s)
    return keep, notes


def _px(grid_wcs, ra: float, dec: float) -> tuple[float, float]:
    x, y = grid_wcs.world_to_pixel_values(ra, dec)
    return float(x), float(y)


def _ellipse_px(grid_wcs, ra: float, dec: float, cov: np.ndarray) -> dict:
    """The (east, north) arcsec covariance mapped to grid pixels (+x west, +y north):
    semi-axes in px and the major-axis angle from +x toward +y, degrees."""
    h = 1.0
    r, d = am.from_tp(np.array([h, -h, 0, 0]), np.array([0, 0, h, -h]), ra, dec)
    x, y = grid_wcs.world_to_pixel_values(r, d)
    J = np.array([[(x[0] - x[1]) / (2 * h), (x[2] - x[3]) / (2 * h)],
                  [(y[0] - y[1]) / (2 * h), (y[2] - y[3]) / (2 * h)]])
    w, v = np.linalg.eigh(J @ cov @ J.T)
    w = np.clip(w, 0, None)
    return {"a_px": _r(math.sqrt(w[1]), 4), "b_px": _r(math.sqrt(w[0]), 4),
            "theta_deg": _r(math.degrees(math.atan2(v[1, 1], v[0, 1])) % 180.0, 2)}


def _cov_dict(cov: np.ndarray | None) -> list | None:
    if cov is None:
        return None
    return [[_r(cov[0, 0], 8), _r(cov[0, 1], 8)], [_r(cov[1, 0], 8), _r(cov[1, 1], 8)]]


def measurement_dict(m: am.GroupMeasurement, grid_wcs) -> dict:
    d = {
        "group": m.group, "season": m.season, "detector": m.detector, "status": m.status,
        "used_in_fit": False, "reason": m.reason,
        "ra": _r(m.ra, 7), "dec": _r(m.dec, 7), "x": None, "y": None,
        "mjd": _r(m.mjd, 4), "mjd_mean": _r(m.mjd_mean, 4),
        "cov_arcsec2": _cov_dict(m.cov), "cov_fit_arcsec2": _cov_dict(m.cov_fit), "floor_arcsec": _r(m.floor_arcsec, 4),
        "sigma_east_arcsec": None, "sigma_north_arcsec": None, "ellipse": None, "ellipse_px": None,
        "snr": _r(m.snr, 2), "scatter_chi2_red": _r(m.scatter_chi2_red, 3), "chi2": _r(m.chi2, 2), "dof": m.dof, "chi2_red": _r(m.chi2_red, 3),
        "n_exposures": m.n_exposures, "n_used": m.n_used,
        "wavelength_um": [_r(m.lam_lo, 4), _r(m.lam_hi, 4)] if m.lam_lo is not None else None,
        "start": {"ra": _r(m.start_ra, 7), "dec": _r(m.start_dec, 7), "x": None, "y": None},
        "search_radius_arcsec": _r(m.search_radius_arcsec, 2),
        "offset_from_start_arcsec": _r(m.offset_from_start_arcsec, 3),
        "delta_chi2_vs_start": _r(m.delta_chi2_vs_start, 2),
        "flags": m.flags, "frames": m.frames,
    }
    d["start"]["x"], d["start"]["y"] = (_r(v, 3) for v in _px(grid_wcs, m.start_ra, m.start_dec))
    if m.ra is not None:
        d["x"], d["y"] = (_r(v, 3) for v in _px(grid_wcs, m.ra, m.dec))
    if m.cov is not None:
        c = m.cov
        d["sigma_east_arcsec"], d["sigma_north_arcsec"] = _r(math.sqrt(c[0, 0]), 4), _r(math.sqrt(c[1, 1]), 4)
        d["ellipse"] = {k: _r(v, 4) for k, v in am.ellipse(c).items()}
        d["ellipse_px"] = _ellipse_px(grid_wcs, m.ra, m.dec, c)
    return d


# =========================================================================== motion fit
@dataclass
class MotionFit:
    ra_t: float                 # tangent point
    dec_t: float
    t_ref: float                # MJD
    theta: np.ndarray           # xi0, eta0 (arcsec), mu_xi, mu_eta (arcsec/yr)
    cov: np.ndarray             # 4x4 (chi2-inflated when chi2_red > 1)
    chi2: float
    dof: int
    n: int
    plx: float = 0.0                      # arcsec, FIXED (not fitted)
    plx_err: float = 0.0                  # arcsec, propagated into the prediction
    dtheta_dplx: np.ndarray | None = None  # d(theta)/d(parallax) of the least-squares solution

    @property
    def chi2_red(self) -> float | None:
        return self.chi2 / self.dof if self.dof > 0 else None

    def at(self, mjd: float) -> tuple[float, float, np.ndarray]:
        """Predicted (ra, dec) and its 2x2 (east, north) covariance in arcsec^2."""
        dt = (mjd - self.t_ref) / 365.25
        A = np.array([[1, 0, dt, 0], [0, 1, 0, dt]], float)
        xi, eta = A @ self.theta
        cov = A @ self.cov @ A.T
        if self.plx or self.plx_err:
            f = np.array(am.parallax_factors(self.ra_t, self.dec_t, mjd))
            xi, eta = xi + self.plx * f[0], eta + self.plx * f[1]
            if self.plx_err:
                g = self.plx_gradient(mjd)
                cov = cov + self.plx_err**2 * np.outer(g, g)
        ra, dec = am.from_tp(xi, eta, self.ra_t, self.dec_t)
        return float(ra), float(dec), cov

    def plx_gradient(self, mjd: float) -> np.ndarray:
        """d(predicted east, north)/d(parallax) at ``mjd`` with the training data refitted:
        the parallax term itself plus the change it forces on the fitted position and
        proper motion. For the linear model this is the residual a parallax of 1" leaves."""
        dt = (mjd - self.t_ref) / 365.25
        A = np.array([[1, 0, dt, 0], [0, 1, 0, dt]], float)
        f = np.array(am.parallax_factors(self.ra_t, self.dec_t, mjd))
        return f + (A @ self.dtheta_dplx if self.dtheta_dplx is not None else 0.0)

    def target(self) -> Target:
        ra, dec, _ = self.at(self.t_ref)
        return Target(ra=ra, dec=dec, pmra=float(self.theta[2] * 1000), pmdec=float(self.theta[3] * 1000),
                      epoch_mjd=self.t_ref)


def measurement_factors(meas: list[am.GroupMeasurement]) -> list[np.ndarray]:
    return [np.array(am.parallax_factors(m.ra, m.dec, m.mjd)) for m in meas]


def fit_motion(meas: list[am.GroupMeasurement], ra_t: float, dec_t: float, plx: float = 0.0,
               plx_err: float = 0.0, factors: list[np.ndarray] | None = None) -> MotionFit:
    """Weighted least squares, full 2x2 covariance per point: xi(t) = xi0 + mu_xi dt
    + plx f_e(t), eta(t) = eta0 + mu_eta dt + plx f_n(t) in the tangent plane at
    (ra_t, dec_t). The parallax ``plx`` (arcsec) is FIXED, never fitted: 0 = linear model."""
    fs = factors if factors is not None else measurement_factors(meas)
    ys, Ws, ts = [], [], []
    for m, f in zip(meas, fs):
        xi, eta = am.to_tp(m.ra, m.dec, ra_t, dec_t)
        ys.append(np.array([float(xi), float(eta)]) - plx * f)
        Ws.append(np.linalg.inv(m.cov))
        ts.append(m.mjd)
    wt = np.array([np.trace(W) for W in Ws])
    t_ref = float(np.sum(wt * np.array(ts)) / np.sum(wt))
    N = np.zeros((4, 4))
    b = np.zeros(4)
    bf = np.zeros(4)
    As = []
    for y, W, t, f in zip(ys, Ws, ts, fs):
        dt = (t - t_ref) / 365.25
        A = np.array([[1, 0, dt, 0], [0, 1, 0, dt]], float)
        As.append(A)
        N += A.T @ W @ A
        b += A.T @ W @ y
        bf += A.T @ W @ f
    cov = np.linalg.inv(N)
    theta = cov @ b
    dtheta = -cov @ bf
    chi2 = float(sum((y - A @ theta) @ W @ (y - A @ theta) for y, W, A in zip(ys, Ws, As)))
    dof = 2 * len(meas) - 4
    if dof > 0 and chi2 / dof > 1:
        cov = cov * (chi2 / dof)
    return MotionFit(ra_t=ra_t, dec_t=dec_t, t_ref=t_ref, theta=theta, cov=cov, chi2=chi2, dof=dof, n=len(meas),
                     plx=plx, plx_err=plx_err, dtheta_dplx=dtheta)


def n_epochs(mjds: list[float], gap_days: float = EPOCH_GAP_DAYS) -> int:
    ts = sorted(mjds)
    return 0 if not ts else 1 + sum(1 for a, b in zip(ts, ts[1:]) if b - a > gap_days)


def free_parallax_fit(meas: list[am.GroupMeasurement], ra_t: float, dec_t: float,
                      factors: list[np.ndarray]) -> dict:
    """5-parameter fit (position, proper motion, parallax), attempted only with >= 4
    well-separated epochs and reported only when parallax is not degenerate with the
    other parameters. Informational: the verdict never uses it."""
    ne = n_epochs([m.mjd for m in meas])
    if ne < FREE_PLX_MIN_EPOCHS:
        plural = "s" if ne != 1 else ""
        return {"fitted": False, "n_epochs": ne,
                "reason": f"not fitted: {ne} training epoch{plural} (need >= {FREE_PLX_MIN_EPOCHS} separated by "
                          f"> {EPOCH_GAP_DAYS:.0f} d). Each SPHEREx season samples one parallactic phase, so with "
                          "so few epochs parallax and proper motion are degenerate."}
    ys = [np.array([float(v) for v in am.to_tp(m.ra, m.dec, ra_t, dec_t)]) for m in meas]
    Ws = [np.linalg.inv(m.cov) for m in meas]
    wt = np.array([np.trace(W) for W in Ws])
    t_ref = float(np.sum(wt * np.array([m.mjd for m in meas])) / np.sum(wt))
    As = [np.array([[1, 0, (m.mjd - t_ref) / 365.25, 0, f[0]], [0, 1, 0, (m.mjd - t_ref) / 365.25, f[1]]], float)
          for m, f in zip(meas, factors)]
    N = sum(A.T @ W @ A for A, W in zip(As, Ws))
    b = sum(A.T @ W @ y for A, W, y in zip(As, Ws, ys))
    try:
        cov = np.linalg.inv(N)
    except np.linalg.LinAlgError:
        return {"fitted": False, "n_epochs": ne, "reason": "not fitted: the 5-parameter normal matrix is singular."}
    err = np.sqrt(np.clip(np.diag(cov), 0, None))
    with np.errstate(invalid="ignore", divide="ignore"):
        corr = np.abs(cov[4, :4] / (err[4] * err[:4]))
    if not np.all(np.isfinite(corr)) or float(np.max(corr)) > FREE_PLX_MAX_CORR:
        return {"fitted": False, "n_epochs": ne, "max_correlation": _r(np.max(corr), 3),
                "reason": f"not reported: parallax is degenerate with position/proper motion (|correlation| "
                          f"{float(np.max(corr)):.2f} > {FREE_PLX_MAX_CORR})."}
    theta = cov @ b
    chi2 = float(sum((y - A @ theta) @ W @ (y - A @ theta) for A, W, y in zip(As, Ws, ys)))
    dof = 2 * len(meas) - 5
    infl = math.sqrt(chi2 / dof) if dof > 0 and chi2 / dof > 1 else 1.0
    return {"fitted": True, "n_epochs": ne, "parallax_mas": _r(theta[4] * 1000, 2),
            "parallax_err_mas": _r(err[4] * 1000 * infl, 2), "max_correlation": _r(np.max(corr), 3),
            "chi2": _r(chi2, 2), "dof": dof,
            "note": "informational: fitted to the training data only; the verdict does not use it"}


def gaia_parallax(cat: CatalogResult, meas: list[am.GroupMeasurement], ra_t: float, dec_t: float,
                  factors: list[np.ndarray], fit_lin: MotionFit) -> tuple[float | None, float | None, dict]:
    """Gaia DR3 parallax (mas) of the TARGET itself: a Gaia source with a significant
    parallax whose position (within GAIA_MATCH_ARCSEC at the training reference epoch) and
    proper motion agree with a fit of the training data that adopts that parallax.
    Catalogue values only: no held-out pixel is involved."""
    if not cat.ok:
        return None, None, {"matched": False, "reason": "Gaia catalogue unavailable"}
    t = fit_lin.t_ref
    l_ra, l_dec, _ = fit_lin.at(t)
    best = None
    info = {"matched": False, "reason": f"no Gaia source with a parallax within {GAIA_MATCH_ARCSEC:.0f}\" of the "
                                        "fitted position"}
    if not any(s.parallax is not None for s in cat.stars):
        info["reason"] += " (the catalogue has no parallaxes)"
    for s in cat.stars:
        if s.parallax is None or not s.has_pm:
            continue
        gra, gdec = Target(ra=s.ra, dec=s.dec, pmra=s.pmra, pmdec=s.pmdec, epoch_mjd=s.epoch_mjd).at(t)
        if float(_sep_arcsec(l_ra, l_dec, gra, gdec)) > GAIA_MATCH_ARCSEC + 1.5 * abs(s.parallax) / 1000:
            continue  # cheap pre-filter: the linear fit absorbs up to ~1.5x the parallax
        plx, perr = s.parallax / 1000, (s.parallax_error or 0.0) / 1000
        fb = fit_motion(meas, ra_t, dec_t, plx=plx, plx_err=perr, factors=factors)
        b_ra, b_dec, _ = fb.at(t)
        f = np.array(am.parallax_factors(gra, gdec, t))
        g_ra, g_dec = am.from_tp(plx * f[0], plx * f[1], gra, gdec)
        sep = float(_sep_arcsec(b_ra, b_dec, float(g_ra), float(g_dec)))
        dpm = fb.theta[2:] * 1000 - np.array([s.pmra, s.pmdec])
        tol = 3 * math.sqrt(max(np.trace(fb.cov[2:, 2:]) * 1e6 / 2, 0)) + GAIA_PM_SLACK_MASYR
        row = {"gaia_id": s.source_id, "g_mag": _r(s.g_mag, 2), "parallax_mas": _r(s.parallax, 3),
               "parallax_error_mas": _r(s.parallax_error, 3), "sep_arcsec": _r(sep, 3),
               "pm_difference_masyr": _r(float(np.hypot(*dpm)), 1), "pm_tolerance_masyr": _r(tol, 1)}
        if sep > GAIA_MATCH_ARCSEC:
            continue
        if float(np.hypot(*dpm)) > tol:
            info = {"matched": False, **row,
                    "reason": "a Gaia source is close but its proper motion is inconsistent with the training data"}
            continue
        snr = s.parallax / s.parallax_error if s.parallax_error else float("inf")
        if snr < GAIA_PLX_MIN_SNR:
            info = {"matched": False, **row, "reason": f"Gaia parallax S/N {snr:.1f} < {GAIA_PLX_MIN_SNR:g}"}
            continue
        if best is None or sep < best[2]["sep_arcsec"]:
            best = (s.parallax, s.parallax_error, {"matched": True, **row})
    if best is None:
        return None, None, info
    return best


def implied_parallax(fit_lin: MotionFit, aux: dict) -> tuple[float | None, float | None]:
    """Parallax (arcsec) that would turn the LINEAR model's held-out residual into zero,
    with its 1-sigma error: least squares of residual = plx * g, g = plx_gradient. This
    uses the reveal, so it is NOT a fit and never enters the verdict."""
    if not aux:
        return None, None
    g = fit_lin.plx_gradient(aux["mjd"])
    _, _, pcov = fit_lin.at(aux["mjd"])
    Si = np.linalg.inv(pcov + aux["cov"])
    h = float(g @ Si @ g)
    if not h > 0:
        return None, None
    return float(g @ Si @ aux["d"]) / h, 1 / math.sqrt(h)


def static_mean(meas: list[am.GroupMeasurement], ra_t: float, dec_t: float) -> tuple[np.ndarray, np.ndarray, float, int]:
    """No-motion model: inverse-covariance weighted mean position (tangent plane), its
    covariance (NOT inflated by the scatter) and chi2/dof of the training data about it."""
    ys = [np.array([float(v) for v in am.to_tp(m.ra, m.dec, ra_t, dec_t)]) for m in meas]
    Ws = [np.linalg.inv(m.cov) for m in meas]
    C = np.linalg.inv(sum(Ws))
    mu = C @ sum(W @ y for W, y in zip(Ws, ys))
    chi2 = float(sum((y - mu) @ W @ (y - mu) for y, W in zip(ys, Ws)))
    return mu, C, chi2, 2 * len(meas) - 2


# =========================================================================== orchestration
def _exposures(metas: list[dict], load: Loader) -> list:
    out = []
    for m in metas:
        r = load(m)
        if r is not None:
            out.append((r[0], r[1], m))
    return out


def measure_reference_stars(groups: dict, load: Loader, cat: CatalogResult, grid: Grid,
                            avoid: list[tuple[float, float]], mjds: list[float]) -> tuple[float, dict]:
    half = grid.n * grid.pixscale_arcsec / 2 - 20.0
    refs = am.pick_reference_stars(cat, grid.ra, grid.dec, half, avoid,
                                   settings.neighbour_radius_arcsec + am.SEARCH_RADIUS_ARCSEC, n=REF_STARS)
    offsets, rows = [], []
    for s in refs:
        tgt = Target(ra=s.ra, dec=s.dec, pmra=s.pmra, pmdec=s.pmdec, epoch_mjd=s.epoch_mjd)
        nbs, _ = neighbours_for(tgt, cat, mjds)
        for (season, det), metas in groups.items():
            exps = _exposures(metas, load)
            mjd0 = float(np.mean([m["mjd"] for m in metas]))
            sra, sdec = tgt.at(mjd0)
            gm = am.measure_group(exps, nbs, sra, sdec, group=f"ref{s.source_id}_S{season}_D{det}",
                                  season=season, detector=det)
            ok = gm.usable and not any(f.startswith(("SATURATED", "MASKED", "WINGS")) for f in gm.flags)
            row = {"gaia_id": s.source_id, "g_mag": _r(s.g_mag, 2), "season": season, "detector": det,
                   "status": gm.status, "used": ok, "snr": _r(gm.snr, 1)}
            if ok:
                cra, cdec = tgt.at(gm.mjd)
                xi, eta = am.to_tp(gm.ra, gm.dec, cra, cdec)
                d = np.array([float(xi), float(eta)])
                offsets.append((d, gm.cov_fit))
                row.update({"d_east_arcsec": _r(d[0], 3), "d_north_arcsec": _r(d[1], 3),
                            "sigma_fit_arcsec": _r(math.sqrt(np.trace(gm.cov_fit) / 2), 3)})
            rows.append(row)
    floor, info = am.registration_floor(offsets, min_floor=MIN_FLOOR_ARCSEC)
    info["stars"] = [{"gaia_id": s.source_id, "g_mag": _r(s.g_mag, 2), "ra": _r(s.ra, 7), "dec": _r(s.dec, 7)}
                     for s in refs]
    info["measurements"] = rows
    if info.get("n_used", 0) < 4:
        info["note"] = (f"Only {info.get('n_used', 0)} usable reference measurements; the floor falls back to "
                        f"{DEFAULT_FLOOR_ARCSEC}\".")
        floor = max(floor, DEFAULT_FLOOR_ARCSEC)
    info["floor_arcsec"] = _r(floor, 4)
    return floor, info


def _compare(meas: list[am.GroupMeasurement], fit: MotionFit, mu: np.ndarray, C_mu: np.ndarray,
             grid_wcs, model: str = "linear", aux: dict | None = None) -> dict:
    rows, res_p, res_s, Ws, mjds = [], [], [], [], []
    for m in meas:
        if not m.usable:
            continue
        pra, pdec, pcov = fit.at(m.mjd)
        d = np.array([float(v) for v in am.to_tp(m.ra, m.dec, pra, pdec)])
        dist, sig, p = am.sigma_equivalent(d, pcov + m.cov)
        xi, eta = am.to_tp(m.ra, m.dec, fit.ra_t, fit.dec_t)
        ds = np.array([float(xi), float(eta)]) - mu
        dist_s, sig_s, p_s = am.sigma_equivalent(ds, C_mu + m.cov)
        px, py = _px(grid_wcs, pra, pdec)
        rows.append({"group": m.group, "detector": m.detector, "mjd": _r(m.mjd, 4),
                     "predicted": {"ra": _r(pra, 7), "dec": _r(pdec, 7), "x": _r(px, 3), "y": _r(py, 3)},
                     "offset_arcsec": _r(np.hypot(*d), 3), "offset_east_arcsec": _r(d[0], 3),
                     "offset_north_arcsec": _r(d[1], 3), "offset_sigma": _r(sig, 2), "mahalanobis": _r(dist, 2),
                     "p_value": _r(p, 5),
                     "no_motion": {"offset_arcsec": _r(np.hypot(*ds), 3), "offset_sigma": _r(sig_s, 2),
                                   "p_value": _r(p_s, 8)}})
        res_p.append(d)
        res_s.append(ds)
        Ws.append(np.linalg.inv(m.cov))
        mjds.append(m.mjd)
    if not rows:
        return {"model": model, "per_detector": [], "combined": None, "verdict": "not testable",
                "summary": "The target was not measured in any detector of the held-out season, so the "
                           "prediction cannot be checked."}
    # Combine the detectors: inverse-covariance mean of the residuals; the prediction error
    # is common to all of them, so it is added once (at the mean epoch).
    Cm = np.linalg.inv(sum(Ws))
    dp = Cm @ sum(W @ r for W, r in zip(Ws, res_p))
    ds = Cm @ sum(W @ r for W, r in zip(Ws, res_s))
    n = len(rows)
    chi2_int = float(sum((r - dp) @ W @ (r - dp) for r, W in zip(res_p, Ws)))
    if n > 1 and chi2_int / (2 * n - 2) > 1:
        Cm = Cm * chi2_int / (2 * n - 2)  # detectors disagree more than their errors: say so via larger errors
    t_mean = float(np.sum([np.trace(W) * t for W, t in zip(Ws, mjds)]) / np.sum([np.trace(W) for W in Ws]))
    _, _, pcov = fit.at(t_mean)
    if aux is not None:
        aux.update(d=dp, cov=Cm, mjd=t_mean)
    dist, sig, p = am.sigma_equivalent(dp, pcov + Cm)
    dist_s, sig_s, p_s = am.sigma_equivalent(ds, C_mu + Cm)
    if sig <= CONFIRM_SIGMA:
        verdict = "prediction confirmed"
    elif sig <= FAIL_SIGMA:
        verdict = "marginal"
    else:
        verdict = "prediction failed"
    nm = ("no-motion hypothesis excluded" if sig_s > FAIL_SIGMA else
          "no-motion hypothesis also consistent" if sig_s <= CONFIRM_SIGMA else "no-motion hypothesis disfavoured")
    summary = (f"Predicted from the training seasons only, the target was found {np.hypot(*dp):.2f}\" "
               f"({sig:.1f} sigma) from the prediction in the held-out season: {verdict}. A source that did not "
               f"move would sit {np.hypot(*ds):.2f}\" ({sig_s:.1f} sigma) from where it was found: {nm}.")
    return {
        "model": model,
        "per_detector": rows,
        "combined": {"n_detectors": n, "mjd": _r(t_mean, 4), "offset_arcsec": _r(np.hypot(*dp), 3),
                     "offset_east_arcsec": _r(dp[0], 3), "offset_north_arcsec": _r(dp[1], 3),
                     "offset_sigma": _r(sig, 2), "mahalanobis": _r(dist, 2), "p_value": _r(p, 5),
                     "detector_consistency_chi2": _r(chi2_int, 2), "detector_consistency_dof": 2 * n - 2,
                     "no_motion": {"offset_arcsec": _r(np.hypot(*ds), 3), "offset_sigma": _r(sig_s, 2),
                                   "p_value": _r(p_s, 8)}},
        "verdict": verdict, "no_motion_verdict": nm, "summary": summary,
    }


def _pred_dict(fit: MotionFit, t_pred: float, gw) -> dict:
    p_ra, p_dec, p_cov = fit.at(t_pred)
    px, py = _px(gw, p_ra, p_dec)
    return {"mjd": _r(t_pred, 4), "ra": _r(p_ra, 7), "dec": _r(p_dec, 7), "x": _r(px, 3), "y": _r(py, 3),
            "cov_arcsec2": _cov_dict(p_cov), "ellipse": {k: _r(v, 4) for k, v in am.ellipse(p_cov).items()},
            "ellipse_px": _ellipse_px(gw, p_ra, p_dec, p_cov),
            "extrapolation_yr": _r((t_pred - fit.t_ref) / 365.25, 3)}


def _model_dict(fit: MotionFit, model: str, pred: dict, comparison: dict | None) -> dict:
    err = np.sqrt(np.diag(fit.cov))
    d = {"model": model, "parallax_mas": _r(fit.plx * 1000, 3), "parallax_err_mas": _r(fit.plx_err * 1000, 3),
         "pmra_masyr": _r(fit.theta[2] * 1000, 1), "pmdec_masyr": _r(fit.theta[3] * 1000, 1),
         "sigma_pmra_masyr": _r(err[2] * 1000, 1), "sigma_pmdec_masyr": _r(err[3] * 1000, 1),
         "chi2": _r(fit.chi2, 3), "dof": fit.dof, "prediction": pred}
    if comparison is not None:
        d["comparison"] = {k: comparison.get(k) for k in ("combined", "verdict", "no_motion_verdict", "summary")}
    return d


def run_holdout(frames: list[dict], load: Loader, cat: CatalogResult, guess: Target, grid: Grid,
                holdout: int | None = None, field_key: str = "",
                progress: Callable[[str, int, int], None] | None = None, plx_mas: float | None = None) -> dict:
    """Blocking. ``load(meta)`` returns (NativeCutout, PSFLibrary) or None; it is never
    called for a held-out frame before the prediction has been computed. ``plx_mas``
    fixes the parallax of the parallax model; when None, the target's own Gaia DR3
    parallax is used if it matches (catalogue values only, never held-out pixels)."""
    gw = grid.wcs()
    frames = [f for f in frames if f.get("mjd") is not None and f.get("season") is not None]
    seasons = sorted({int(f["season"]) for f in frames})
    base = {"status": "complete", "version": HOLDOUT_VERSION, "field": field_key,
            "target": {"ra": guess.ra, "dec": guess.dec, "pmra_masyr": guess.pmra, "pmdec_masyr": guess.pmdec,
                       "epoch_mjd": guess.epoch_mjd},
            "grid": {"ra": grid.ra, "dec": grid.dec, "n": grid.n, "pixscale_arcsec": grid.pixscale_arcsec,
                     "convention": "0-based pixels, +x west, +y north (north up, east left)"},
            "units": {"position": "deg", "offset": "arcsec", "pm": "mas/yr (pmra includes cos dec)",
                      "time": "MJD", "covariance": "arcsec^2 (east, north)"}}
    if not seasons:
        return {**base, "testable": False, "reason": "no exposures in this field"}
    hold = seasons[-1] if holdout is None else int(holdout)
    if hold not in seasons:
        return {**base, "testable": False, "reason": f"season {hold} does not exist (seasons: {seasons})"}
    train_frames = [f for f in frames if int(f["season"]) != hold]
    held_frames = [f for f in frames if int(f["season"]) == hold]
    season_info = []
    for s in seasons:
        fs = [f for f in frames if int(f["season"]) == s]
        season_info.append({"season": s, "role": "holdout" if s == hold else "train", "n_frames": len(fs),
                            "mjd_start": _r(min(f["mjd"] for f in fs), 4), "mjd_end": _r(max(f["mjd"] for f in fs), 4),
                            "mjd_mean": _r(np.mean([f["mjd"] for f in fs]), 4),
                            "detectors": sorted({int(f["detector"]) for f in fs})})
    base.update({"holdout_season": hold, "seasons": season_info})
    notes = [
        f"Season {hold} was held out: none of its pixels were read until the prediction was fixed; the "
        "registration floor, neighbour list, fit and prediction use only the other seasons.",
        "The proper motion supplied with the request (if any) only sets where the search starts in the "
        "training seasons; it is not used in the fit being tested.",
        f"Positions: one joint PSF fit per (season, detector) group in native pixels; search bounded to "
        f"{am.SEARCH_RADIUS_ARCSEC:.0f}\" around the start (up to {am.MAX_SEARCH_ARCSEC:.0f}\" in the reveal when the "
        f"prediction is that uncertain); groups with combined S/N < {am.DETECT_SNR:g} report 'not_detected'.",
        "Detectors D1/D4, D2/D5, D3/D6 are exposed simultaneously; their registration errors may be "
        "correlated, which the per-group floor treats as independent.",
    ]
    tick = progress or (lambda *a: None)

    # ---- 1. registration floor from static reference stars, training groups only ----------
    train_groups = groups_of(train_frames)
    train_mjds = [f["mjd"] for f in train_frames]
    avoid = [guess.at(t) for t in path_samples(train_mjds)]
    tick("reference stars", 0, len(train_groups))
    floor, reg = measure_reference_stars(train_groups, load, cat, grid, avoid, train_mjds)

    # ---- 2. training positions ---------------------------------------------------------
    nbs, nb_notes = neighbours_for(guess, cat, train_mjds)
    notes += nb_notes
    train: list[am.GroupMeasurement] = []
    for i, ((season, det), metas) in enumerate(train_groups.items()):
        tick("training", i, len(train_groups))
        mjd0 = float(np.mean([m["mjd"] for m in metas]))
        sra, sdec = guess.at(mjd0)
        gm = am.measure_group(_exposures(metas, load), nbs, sra, sdec, group=f"S{season}_D{det}",
                              season=season, detector=det)
        gm.floor_arcsec = floor
        train.append(gm)
    usable = [m for m in train if m.usable]
    used_seasons = sorted({m.season for m in usable})
    base.update({"registration": reg, "neighbours": [
        {"gaia_id": s.gaia_id, "g_mag": _r(s.g_mag, 2), "min_sep_arcsec": s.sep_arcsec} for s in nbs]})
    method = {
        "position": "joint ePSF fit per (season, detector): shared free sky position, per-exposure free "
                    "target flux (>= 0), Gaia neighbours fixed at catalogue-propagated positions with free "
                    f"fluxes, {settings.fit_background} background, weights 1/VARIANCE, 5-sigma residual "
                    "clipping outside fitted cores, 3% PSF-model error term",
        "position_error": "2 H^-1 of the profiled chi2 (H = Hessian), x pixel reduced chi2 when > 1, x the "
                          "chi2/dof of single-exposure positions about the joint one when > 1 (scatter_chi2_red), "
                          "plus the registration floor in quadrature on each axis",
        "epoch": "(flux S/N)^2-weighted mean MJD of the group's exposures",
        "motion_model": "linear: position at reference epoch + proper motion, weighted least squares with "
                        "full 2x2 covariances, covariance x chi2/dof when > 1. Parallax model (when a parallax is "
                        "supplied with plx= or the target's own Gaia DR3 parallax matches): the same fit with "
                        "a FIXED parallax term (Earth barycentric position, astropy builtin ephemeris); its "
                        "error is propagated into the prediction. Parallax is never fitted freely to 2-3 epochs.",
        "verdict": f"Gaussian-equivalent sigma of the 2-D offset (chi2 with 2 dof): <= {CONFIRM_SIGMA:g} "
                   f"confirmed, <= {FAIL_SIGMA:g} marginal, else failed",
    }
    train_d = []
    for m in train:
        d = measurement_dict(m, gw)
        d["used_in_fit"] = m.usable
        train_d.append(d)
    base.update({"train": train_d, "method": method})
    if len(used_seasons) < 2:
        why = ("the target was measured in only one training season (" + ", ".join(map(str, used_seasons)) + ")"
               if used_seasons else "the target was not measured in any training season")
        return _clean({**base, "testable": False,
                       "reason": f"{why}: one epoch of independent data cannot constrain a motion, so there is "
                                 "nothing to predict.", "notes": notes})

    # ---- 3. motion fit on training data only ------------------------------------------------
    t_mean = float(np.mean([m.mjd for m in usable]))
    ra_t, dec_t = guess.at(t_mean)
    factors = measurement_factors(usable)
    fit_lin = fit_motion(usable, ra_t, dec_t, factors=factors)
    plx_src: str | None = None
    plx_used = plx_err_used = None
    gaia_info: dict | None = None
    if plx_mas is not None:
        plx_src, plx_used, plx_err_used = "query", float(plx_mas), 0.0
    else:
        g, ge, gaia_info = gaia_parallax(cat, usable, ra_t, dec_t, factors, fit_lin)
        if g is not None:
            plx_src, plx_used, plx_err_used = "gaia", float(g), float(ge or 0.0)
    fit_p = (fit_motion(usable, ra_t, dec_t, plx=plx_used / 1000, plx_err=plx_err_used / 1000, factors=factors)
             if plx_used is not None else None)
    fit = fit_p or fit_lin
    model = "linear+parallax" if fit_p is not None else "linear"
    free = free_parallax_fit(usable, ra_t, dec_t, factors)
    notes.append("Parallax factors use the Earth's barycentric position (astropy, builtin ephemeris) at each "
                 "group's mean epoch; the geocentre stands in for SPHEREx, whose low Earth orbit (~7000 km = "
                 "5e-5 AU) changes the displacement by < 1e-4 of the parallax.")
    if fit_p is not None:
        how = ("supplied with the request (plx=)" if plx_src == "query" else
               f"the target's own Gaia DR3 parallax (source {gaia_info['gaia_id']}, {gaia_info['sep_arcsec']}\" "
               "from the training fit, proper motion consistent)")
        notes.append(f"Parallax model: parallax FIXED at {plx_used:.1f}"
                     + (f" +/- {plx_err_used:.1f}" if plx_err_used else "")
                     + f" mas, {how}; it is not fitted. The verdict uses this model; the straight-line model is "
                     "reported alongside.")
    else:
        why = gaia_info.get("reason") if gaia_info else "none"
        notes.append("No parallax value was available (none supplied with plx=; Gaia: " + str(why) + "), so the "
                     "straight-line model is used. For nearby stars the parallactic displacement (0.27\" for "
                     "Epsilon Indi B, 0.44\" for WISE 0855, 0.55\" for Barnard's Star) is absorbed into the "
                     "fitted motion and can bias the prediction by a comparable amount.")
    notes.append(f"Free parallax fit: {free['reason']}" if not free["fitted"] else
                 f"Free parallax fit to the training data ({free['n_epochs']} epochs): {free['parallax_mas']} +/- "
                 f"{free['parallax_err_mas']} mas (informational; the verdict does not use it).")
    mu, C_mu, chi2_static, dof_static = static_mean(usable, ra_t, dec_t)
    f_ra, f_dec, _ = fit.at(fit.t_ref)
    err = np.sqrt(np.diag(fit.cov))
    corr = fit.cov / np.outer(err, err)
    s_ra, s_dec = am.from_tp(mu[0], mu[1], ra_t, dec_t)
    fit_d = {
        "ref_mjd": _r(fit.t_ref, 4), "ra": _r(f_ra, 7), "dec": _r(f_dec, 7),
        "x": _r(_px(gw, f_ra, f_dec)[0], 3), "y": _r(_px(gw, f_ra, f_dec)[1], 3),
        "pmra_masyr": _r(fit.theta[2] * 1000, 1), "pmdec_masyr": _r(fit.theta[3] * 1000, 1),
        "sigma_ra_arcsec": _r(err[0], 4), "sigma_dec_arcsec": _r(err[1], 4),
        "sigma_pmra_masyr": _r(err[2] * 1000, 1), "sigma_pmdec_masyr": _r(err[3] * 1000, 1),
        "pm_total_masyr": _r(math.hypot(*fit.theta[2:]) * 1000, 1),
        "cov": [[_r(v, 10) for v in row] for row in fit.cov],
        "cov_units": "[east arcsec, north arcsec, pmra arcsec/yr, pmdec arcsec/yr]",
        "correlation": [[_r(v, 4) for v in row] for row in corr],
        "chi2": _r(fit.chi2, 3), "dof": fit.dof, "chi2_red": _r(fit.chi2_red, 3),
        "n_measurements": fit.n, "n_seasons": len(used_seasons), "seasons": used_seasons,
        "model": model, "parallax_mas": _r(fit.plx * 1000, 3) if fit_p is not None else None,
        "static_model": {"ra": _r(s_ra, 7), "dec": _r(s_dec, 7), "x": _r(_px(gw, s_ra, s_dec)[0], 3),
                         "y": _r(_px(gw, s_ra, s_dec)[1], 3), "chi2": _r(chi2_static, 2), "dof": dof_static,
                         "chi2_red": _r(chi2_static / dof_static, 3) if dof_static > 0 else None},
    }
    if len(used_seasons) == 2:
        notes.append("Two training seasons: the motion is essentially the line through two epochs; "
                     "chi2/dof only tests agreement between detectors within each season.")

    # ---- 4. prediction (held-out epoch from metadata only) ----------------------------------
    t_pred = float(np.mean([f["mjd"] for f in held_frames]))
    pred_lin = _pred_dict(fit_lin, t_pred, gw)
    pred_p = _pred_dict(fit_p, t_pred, gw) if fit_p is not None else None
    pred_d = {**(pred_p or pred_lin), "model": model}

    # ---- 5. reveal: held-out pixels are read only from here on ------------------------------
    fitted = fit.target()
    held_groups = groups_of(held_frames)
    held_mjds = [f["mjd"] for f in held_frames]
    nbs_h, nb_notes_h = neighbours_for(fitted, cat, held_mjds)
    reveal: list[am.GroupMeasurement] = []
    for i, ((season, det), metas) in enumerate(held_groups.items()):
        tick("reveal", i, len(held_groups))
        mjd0 = float(np.mean([m["mjd"] for m in metas]))
        sra, sdec, scov = fit.at(mjd0)
        radius = min(am.MAX_SEARCH_ARCSEC, max(am.SEARCH_RADIUS_ARCSEC, 3 * am.ellipse(scov)["a_arcsec"]))
        gm = am.measure_group(_exposures(metas, load), nbs_h, sra, sdec, group=f"S{season}_D{det}",
                              season=season, detector=det, search_radius_arcsec=radius)
        gm.floor_arcsec = floor
        reveal.append(gm)
    reveal_d = [measurement_dict(m, gw) for m in reveal]
    aux: dict = {}
    comp_lin = _compare(reveal, fit_lin, mu, C_mu, gw, model="linear", aux=aux)
    comp_p = _compare(reveal, fit_p, mu, C_mu, gw, model="linear+parallax") if fit_p is not None else None
    comparison = comp_p or comp_lin
    if comp_p is not None:
        notes.append("The held-out season was measured once, starting from the parallax-model prediction; both "
                     "models are compared with that same measurement.")
    imp, imp_err = implied_parallax(fit_lin, aux)
    fac = [{"group": m.group, "role": role, "mjd": _r(m.mjd if m.mjd is not None else m.mjd_mean, 4),
            "f_east": _r(f[0], 5), "f_north": _r(f[1], 5)}
           for role, ms in (("train", train), ("holdout", reveal)) for m in ms
           if (m.mjd if m.mjd is not None else m.mjd_mean) is not None
           for f in [am.parallax_factors(m.ra if m.ra is not None else m.start_ra,
                                         m.dec if m.dec is not None else m.start_dec,
                                         m.mjd if m.mjd is not None else m.mjd_mean)]]
    fp_ = am.parallax_factors(ra_t, dec_t, t_pred)
    parallax = {
        "used_mas": _r(plx_used, 3), "used_err_mas": _r(plx_err_used, 3), "source": plx_src,
        "factors": fac, "factors_prediction": {"mjd": _r(t_pred, 4), "f_east": _r(fp_[0], 5), "f_north": _r(fp_[1], 5)},
        "factors_units": "arcsec of (east, north) displacement per arcsec of parallax",
        "implied_mas": _r(imp * 1000, 1) if imp is not None else None,
        "implied_err_mas": _r(imp_err * 1000, 1) if imp_err is not None else None,
        "implied_label": IMPLIED_LABEL,
        "implied_note": "the parallax that would make the straight-line model's held-out residual vanish; it uses "
                        "the held-out season, so it is not a fit and never enters the verdict",
        "gaia": gaia_info, "free_fit": free,
    }
    models = {"linear": _model_dict(fit_lin, "linear", pred_lin, comp_lin),
              "parallax": _model_dict(fit_p, "linear+parallax", pred_p, comp_p) if fit_p is not None else None}
    for nt in nb_notes_h:
        if nt not in notes:
            notes.append(nt)
    return _clean({**base, "testable": True, "fit": fit_d, "prediction": pred_d, "reveal": reveal_d,
                   "comparison": comparison, "parallax": parallax, "models": models, "notes": notes})


# =========================================================================== job / route
def _grid(ra: float, dec: float, fov: float | None) -> Grid:
    fov = fov or settings.default_fov_arcmin
    if not 0.5 <= fov <= settings.max_fov_arcmin:
        raise HTTPException(422, f"fov must be between 0.5 and {settings.max_fov_arcmin} arcmin")
    return Grid.for_fov(round(ra, 5), round(dec, 5), fov, settings.pixel_scale_arcsec)


def make_loader(grid: Grid) -> Loader:
    from .fields import cutout_path

    cache: dict[str, tuple | None] = {}

    def load(meta: dict):
        k = str(meta["id"])
        if k not in cache:
            lib = psfmod.load_library(meta["source_url"], int(meta["detector"]))
            path = cutout_path(frame_record(meta), grid)
            r = None
            if lib is not None and path.exists():
                try:
                    r = (load_native(path), lib)
                except Exception as exc:  # noqa: BLE001
                    log.warning("holdout: cannot load %s: %s", meta.get("id"), exc)
            cache[k] = r
        return cache[k]

    return load


def result_path(field_key: str, guess: Target, holdout: int | None, plx: float | None = None) -> Path:
    h = hashlib.sha1(f"{guess.key()}|{holdout}|{plx}|{HOLDOUT_VERSION}|{settings.fit_background}|"
                     f"{settings.psf_model_error}|{settings.neighbour_radius_arcsec}".encode()).hexdigest()[:16]
    return settings.cache_dir / "holdout" / field_key / f"{h}.json"


@dataclass
class HoldoutJob:
    key: str
    status: str = "processing"
    stage: str = "starting"
    done: int = 0
    total: int = 0
    result: dict | None = None
    error: str | None = None
    expires: float | None = None
    task: asyncio.Task | None = None

    def response(self) -> dict:
        if self.status == "complete" and self.result is not None:
            return self.result
        if self.status == "error":
            return {"status": "error", "error": self.error}
        return {"status": "processing", "stage": self.stage, "done": self.done, "total": self.total}


class HoldoutManager:
    def __init__(self) -> None:
        self.jobs: dict[str, HoldoutJob] = {}
        self._cpu = asyncio.Semaphore(2)

    async def request(self, fields, grid: Grid, guess: Target, holdout: int | None,
                      plx: float | None = None) -> dict:
        fjob = fields.get_or_start(grid)
        if fjob.status == "error":
            return {"status": "error", "error": f"field build failed: {fjob.error}"}
        if fjob.status != "complete":
            return {"status": "processing", "stage": "field", "done": len(fjob.frames), "total": fjob.total}
        key = f"{fjob.key}|{guess.key()}|{holdout}|{plx}"
        job = self.jobs.get(key)
        if job and (job.status == "error" or (job.expires and time.time() > job.expires)):
            job = None
        if job is None:
            job = HoldoutJob(key=key)
            self.jobs[key] = job
            job.task = asyncio.create_task(self._run(job, fjob.key, grid, list(fjob.frames.values()), guess, holdout,
                                                      plx))
            await asyncio.wait({job.task}, timeout=0.5)
        return job.response()

    async def _run(self, job: HoldoutJob, field_key: str, grid: Grid, frames: list[dict], guess: Target,
                   holdout: int | None, plx: float | None = None) -> None:
        try:
            path = result_path(field_key, guess, holdout, plx)
            if path.exists():
                job.result = json.loads(path.read_text())
                job.status = "complete"
                return
            frames = [f for f in frames if f.get("mjd") is not None]
            mjds = [f["mjd"] for f in frames]
            job.stage = "catalog"
            radius = max(query_radius_deg(guess, mjds),
                         (grid.n * grid.pixscale_arcsec / 2 * math.sqrt(2) + 30) / 3600)
            cat = await asyncio.to_thread(gaia_cone, grid.ra, grid.dec, radius, True)
            job.stage = "psf"
            for f in frames:
                if psfmod.load_library(f["source_url"], int(f["detector"])) is None:
                    try:
                        await asyncio.to_thread(psfmod.fetch_library, f["source_url"], int(f["detector"]))
                    except Exception as exc:  # noqa: BLE001
                        log.warning("holdout ePSF fetch failed: %s", exc)

            def progress(stage: str, i: int, n: int) -> None:
                job.stage, job.done, job.total = stage, i, n

            async with self._cpu:
                result = await asyncio.to_thread(run_holdout, frames, make_loader(grid), cat, guess, grid,
                                                 holdout, field_key, progress, plx)
            result["catalog"] = {"ok": cat.ok, "note": cat.note}
            if cat.ok:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(".tmp")
                tmp.write_text(json.dumps(result))
                tmp.replace(path)
            else:
                job.expires = time.time() + RETRY_AFTER_S
                result.setdefault("notes", []).append("Gaia catalogue unavailable: no neighbours were modelled and "
                                                      "no registration floor could be measured.")
            job.result = result
            job.status = "complete"
        except Exception as exc:  # noqa: BLE001
            log.exception("holdout %s failed", job.key)
            job.status = "error"
            job.error = f"{type(exc).__name__}: {exc}"


def _manager(request: Request) -> HoldoutManager:
    m = getattr(request.app.state, "holdouts", None)
    if m is None:
        m = HoldoutManager()
        request.app.state.holdouts = m
    return m


@router.get("/api/holdout")
async def holdout_route(
    request: Request,
    ra: str = Query(..., description="approximate RA at MJD `epoch`: decimal deg or sexagesimal hours"),
    dec: str = Query(..., description="approximate Dec at MJD `epoch`"),
    fov: float | None = Query(None, description="field of view, arcmin (same field as the viewer)"),
    pmra: float = Query(0.0, ge=-30000, le=30000, description="starting-guess proper motion RA*cos(dec), mas/yr"),
    pmdec: float = Query(0.0, ge=-30000, le=30000, description="starting-guess proper motion Dec, mas/yr"),
    epoch: float = Query(SPHEREX_REF_MJD, ge=30000, le=90000, description="MJD of ra/dec"),
    holdout: int | None = Query(None, ge=0, description="season index to hold out (default: the latest)"),
    plx: float | None = Query(None, ge=0, le=2000, description="parallax to FIX in the parallax model, mas "
                                                               "(default: the target's Gaia DR3 parallax if it matches)"),
):
    """Predict-then-reveal test of the motion of one source. Non-blocking: poll until
    ``status == 'complete'``."""
    try:
        ra_d, dec_d = parse_ra(ra), parse_dec(dec)
    except CoordError as exc:
        raise HTTPException(422, str(exc)) from exc
    guess = Target(ra=ra_d, dec=dec_d, pmra=pmra, pmdec=pmdec, epoch_mjd=epoch)
    if (pmra**2 + pmdec**2) ** 0.5 > PM_THRESHOLD_MASYR:
        ra_c, dec_c = guess.at(SPHEREX_REF_MJD)
    else:
        ra_c, dec_c = ra_d, dec_d
    return await _manager(request).request(request.app.state.fields, _grid(ra_c, dec_c, fov), guess, holdout,
                                              plx)
