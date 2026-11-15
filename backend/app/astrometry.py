"""Astrometry: one sky position of a source per EPOCH GROUP of exposures.

Single SPHEREx exposures give positions that are far too noisy for faint targets
(S/N 3-5 for WISE 0855), so a group of exposures that were taken close together in
time (one season, one detector) is fitted jointly:

  * native detector pixels of each cached cutout (never the reprojected grid);
  * ONE free sky position (xi, eta) of the target in a tangent plane around the
    starting guess, shared by all exposures of the group;
  * per exposure: free target flux (>= 0), free fluxes of the Gaia neighbours held at
    their catalogue positions propagated to that exposure's MJD, and a local
    background (settings.fit_background), weights 1/VARIANCE, masked/suspect pixels
    excluded, unmodelled > 5 sigma pixels clipped outside the fitted cores;
  * the linear parameters are profiled out analytically, so the 2-parameter chi2
    surface is searched on a grid over a bounded disk around the start and refined
    with Nelder-Mead; the covariance is 2 H^-1 of that profiled chi2 (H = Hessian),
    inflated by the reduced chi2 when > 1, plus a registration floor added by the
    caller (see ``registration_floor``).

The group epoch is the (flux S/N)^2-weighted mean MJD of the exposures: for a source
moving linearly during the group, the joint static fit converges to (very nearly) its
position at that epoch.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from functools import lru_cache

import numpy as np
from scipy import optimize
from scipy import stats as sstats

from . import psf as psfmod
from .catalogs import CatalogResult, Star
from .config import settings
from .fingerprint import SATURATION_FLAGS, NativeCutout, Source, _sep_arcsec
from .processing import decode_flags

ARCSEC_PER_RAD = 180 / math.pi * 3600

SEARCH_RADIUS_ARCSEC = 12.0   # ~2 native pixels around the starting guess
MAX_SEARCH_ARCSEC = 24.0
DETECT_SNR = 5.0              # S/N of the inverse-variance combined flux at the solution
GRID_STEP_PX = 0.35           # coarse search spacing before Nelder-Mead
BOUND_TOL_PX = 0.08           # solutions this close to the search bound are not trusted
MIN_PSF_FRAC = 0.08           # exposure unusable when < 8% of the target PSF is on good pixels
WINGS_ONLY_FRAC = 0.5
MIN_PIX = 12
MAX_BOX_HALF = 7
MIN_TEMPLATE_SUM = 0.02
UNRESOLVED_PX = 0.4
PROTECT_R_PX = 2.0
RESID_CLIP = 5.0
CHI2_POOR = 5.0
HESS_STEP_ARCSEC = 0.3
SINGLE_SNR = 5.0              # exposures used for the empirical scatter check
N_ROUNDS = 3                  # grid search (raw weights) + 2 clip/re-weight rounds


# =========================================================================== tangent plane
def to_tp(ra, dec, ra0: float, dec0: float):
    """Gnomonic projection about (ra0, dec0): (xi east, eta north) in arcsec."""
    ra, dec = np.radians(np.asarray(ra, float)), np.radians(np.asarray(dec, float))
    a0, d0 = math.radians(ra0), math.radians(dec0)
    cosc = math.sin(d0) * np.sin(dec) + math.cos(d0) * np.cos(dec) * np.cos(ra - a0)
    xi = np.cos(dec) * np.sin(ra - a0) / cosc
    eta = (math.cos(d0) * np.sin(dec) - math.sin(d0) * np.cos(dec) * np.cos(ra - a0)) / cosc
    return xi * ARCSEC_PER_RAD, eta * ARCSEC_PER_RAD


def from_tp(xi, eta, ra0: float, dec0: float):
    xi = np.asarray(xi, float) / ARCSEC_PER_RAD
    eta = np.asarray(eta, float) / ARCSEC_PER_RAD
    a0, d0 = math.radians(ra0), math.radians(dec0)
    rho = np.hypot(xi, eta)
    c = np.arctan(rho)
    with np.errstate(invalid="ignore", divide="ignore"):
        dec = np.arcsin(np.cos(c) * math.sin(d0) + np.where(rho > 0, eta * np.sin(c) * math.cos(d0) / rho, 0.0))
    ra = a0 + np.arctan2(xi * np.sin(c), rho * math.cos(d0) * np.cos(c) - eta * math.sin(d0) * np.sin(c))
    return np.degrees(ra) % 360.0, np.degrees(dec)


@lru_cache(maxsize=4096)
def _earth_xyz_au(mjd_r: float) -> tuple[float, float, float]:
    from astropy import units as u
    from astropy.coordinates import get_body_barycentric
    from astropy.time import Time

    x, y, z = get_body_barycentric("earth", Time(mjd_r, format="mjd", scale="utc")).xyz.to_value(u.au)
    return float(x), float(y), float(z)


def parallax_factors(ra: float, dec: float, mjd: float) -> tuple[float, float]:
    """Parallactic displacement (east, north) in arcsec per arcsec of parallax for a source
    at (ra, dec) observed at MJD (UTC), from the Earth's barycentric position (ICRS, AU).
    The geocentre stands in for SPHEREx: its low Earth orbit (~7000 km = 5e-5 AU) changes
    the displacement by < 1e-4 of the parallax."""
    X, Y, Z = _earth_xyz_au(round(float(mjd), 4))
    a, d = math.radians(ra), math.radians(dec)
    fe = X * math.sin(a) - Y * math.cos(a)
    fn = X * math.cos(a) * math.sin(d) + Y * math.sin(a) * math.sin(d) - Z * math.cos(d)
    return fe, fn


def ellipse(cov: np.ndarray) -> dict:
    """1-sigma error ellipse of a 2x2 (east, north) covariance: semi-axes (arcsec) and
    position angle of the major axis, degrees east of north."""
    w, v = np.linalg.eigh(np.asarray(cov, float))
    w = np.clip(w, 0, None)
    major = v[:, 1]
    pa = math.degrees(math.atan2(major[0], major[1])) % 180.0
    return {"a_arcsec": math.sqrt(w[1]), "b_arcsec": math.sqrt(w[0]), "pa_deg": pa}


def sigma_equivalent(d: np.ndarray, cov: np.ndarray) -> tuple[float, float, float]:
    """(Mahalanobis distance, two-sided Gaussian-equivalent sigma, p-value) of a 2-D
    offset ``d`` with covariance ``cov``. The 'sigma' is the 1-D number of standard
    deviations with the same p-value as chi2 = d^T C^-1 d with 2 degrees of freedom."""
    m2 = float(d @ np.linalg.solve(cov, d))
    p = float(math.exp(-m2 / 2))  # chi2(2 dof) survival function
    if p > 1e-300:
        sig = float(sstats.norm.isf(p / 2))
    else:  # asymptotic inversion of the Gaussian tail: p/2 ~ phi(s)/s
        s = math.sqrt(m2)
        for _ in range(20):
            s = math.sqrt(max(m2 - 2 * math.log(s * math.sqrt(2 * math.pi) / 2), 1.0))
        sig = s
    return math.sqrt(m2), sig, p


# =========================================================================== measurement
@dataclass
class GroupMeasurement:
    group: str
    season: int | None
    detector: int
    status: str                     # measured | not_detected | unusable | at_bound | no_minimum
    start_ra: float
    start_dec: float
    search_radius_arcsec: float
    ra: float | None = None
    dec: float | None = None
    cov_fit: np.ndarray | None = None   # 2x2 (east, north) arcsec^2 from the fit (chi2-inflated)
    floor_arcsec: float = 0.0
    mjd: float | None = None            # (S/N)^2-weighted mean epoch
    mjd_mean: float | None = None       # plain mean over the exposures (metadata only)
    n_exposures: int = 0
    n_used: int = 0
    chi2: float | None = None
    dof: int = 0
    snr: float | None = None
    lam_lo: float | None = None
    lam_hi: float | None = None
    offset_from_start_arcsec: float | None = None
    delta_chi2_vs_start: float | None = None
    flags: list[str] = field(default_factory=list)
    frames: list[str] = field(default_factory=list)
    reason: str | None = None
    scatter_chi2_red: float | None = None

    @property
    def usable(self) -> bool:
        return self.status == "measured" and self.ra is not None and self.cov_fit is not None

    @property
    def chi2_red(self) -> float | None:
        return self.chi2 / self.dof if self.chi2 is not None and self.dof > 0 else None

    @property
    def cov(self) -> np.ndarray | None:
        if self.cov_fit is None:
            return None
        return self.cov_fit + np.eye(2) * self.floor_arcsec**2


@dataclass
class _Exp:
    meta: dict
    mjd: float
    pix0: np.ndarray                 # (2,) pixel of the start position
    jac: np.ndarray                  # (2,2) d(pixel)/d(xi, eta), px per arcsec
    xx: np.ndarray                   # box pixel coordinates
    yy: np.ndarray
    sci: np.ndarray                  # box arrays
    var: np.ndarray
    usable: np.ndarray
    psf: np.ndarray                  # target ePSF (oversampled)
    os: int
    nb: list[np.ndarray]             # neighbour templates on the box (fixed positions)
    nb_pos: list[tuple[float, float]]
    flags: list[str]
    psf_frac: float
    lam: tuple[float, float] | None
    # per round
    use: np.ndarray | None = None
    veff: np.ndarray | None = None
    sw: np.ndarray | None = None
    q: np.ndarray | None = None
    bperp: np.ndarray | None = None
    bb: float = 0.0
    k: int = 0

    def pix(self, p: np.ndarray) -> tuple[float, float]:
        x, y = self.pix0 + self.jac @ p
        return float(x), float(y)

    def template(self, p: np.ndarray) -> np.ndarray:
        x, y = self.pix(p)
        return psfmod.render(self.psf, x, y, self.xx, self.yy, self.os)


def _prepare(cut: NativeCutout, lib: psfmod.PSFLibrary, meta: dict, ra0: float, dec0: float,
             neighbours: list[Source], search_px: float) -> tuple[_Exp | None, str | None]:
    mjd = float(meta["mjd"])
    ny, nx = cut.sci.shape
    x0, y0 = (float(v) for v in cut.wcs.world_to_pixel_values(ra0, dec0))
    # local affine sky->pixel map (SIP distortion over a few pixels is ~1e-4 px)
    h = 3.0
    rr, dd = from_tp(np.array([h, -h, 0, 0]), np.array([0, 0, h, -h]), ra0, dec0)
    px, py = cut.wcs.world_to_pixel_values(rr, dd)
    jac = np.array([[(px[0] - px[1]) / (2 * h), (px[2] - px[3]) / (2 * h)],
                    [(py[0] - py[1]) / (2 * h), (py[2] - py[3]) / (2 * h)]])
    ix, iy = int(round(x0)), int(round(y0))
    if not (0 <= ix < nx and 0 <= iy < ny):
        return None, "OFF_IMAGE"
    flags: list[str] = []
    yy0, xx0 = np.mgrid[0:ny, 0:nx]
    core = np.hypot(xx0 - x0, yy0 - y0) <= 1.0
    core_bad = core & ~cut.usable
    if core_bad.any():
        cf: set[str] = set()
        for v in np.unique(cut.flags[core_bad]):
            cf |= set(decode_flags(int(v), cut.bits))
        flags.append("SATURATED_CORE" if cf & SATURATION_FLAGS else "MASKED_CORE")
    half = min(MAX_BOX_HALF, 3 + int(math.ceil(search_px)) + (1 if core_bad.any() else 0))
    xa, xb = max(0, ix - half), min(nx, ix + half + 1)
    ya, yb = max(0, iy - half), min(ny, iy + half + 1)
    if (xb - xa) < 2 * half + 1 or (yb - ya) < 2 * half + 1:
        flags.append("EDGE")
    yy, xx = np.mgrid[ya:yb, xa:xb]
    yy, xx = yy.astype(float), xx.astype(float)
    ox, oy = cut.active_off
    tpsf = lib.at(x0 + ox, y0 + oy)
    r_psf = psfmod.psf_radius_px(lib.epsf[0, 0], lib.oversample)
    nb, nb_pos = [], []
    for s in neighbours:
        xn, yn = (float(v) for v in cut.wcs.world_to_pixel_values(*s.at(mjd)))
        if math.hypot(xn - x0, yn - y0) < UNRESOLVED_PX:
            if "UNRESOLVED_BLEND" not in flags:
                flags.append("UNRESOLVED_BLEND")
            continue
        if max(abs(xn - x0), abs(yn - y0)) - 1 > half + r_psf:
            continue
        t = psfmod.render(lib.at(xn + ox, yn + oy), xn, yn, xx, yy, lib.oversample)
        if t.sum() >= MIN_TEMPLATE_SUM:
            nb.append(t)
            nb_pos.append((xn, yn))
    sci = cut.sci[ya:yb, xa:xb]
    var = cut.var[ya:yb, xa:xb]
    usable = cut.usable[ya:yb, xa:xb] & np.isfinite(sci) & np.isfinite(var) & (var > 0)
    t0 = psfmod.render(tpsf, x0, y0, xx, yy, lib.oversample)
    if t0.sum() < 0.3:
        return None, "OFF_IMAGE"
    psf_frac = float(t0[usable].sum() / max(t0.sum(), 1e-9))
    if psf_frac < MIN_PSF_FRAC or usable.sum() < MIN_PIX:
        return None, "TOO_FEW_PIXELS"
    if psf_frac < WINGS_ONLY_FRAC:
        flags.append("WINGS_ONLY")
    lam = None
    if cut.wcs_spec is not None:
        try:
            w, bw = cut.wcs_spec.pixel_to_world_values(x0, y0)
            if math.isfinite(float(w)) and math.isfinite(float(bw)):
                lam = (float(w) - float(bw) / 2, float(w) + float(bw) / 2)
        except Exception:  # noqa: BLE001
            lam = None
    if lam is None and meta.get("wavelength_um"):
        w, bw = float(meta["wavelength_um"]), float(meta.get("bandwidth_um") or 0.0)
        lam = (w - bw / 2, w + bw / 2)
    e = _Exp(meta=meta, mjd=mjd, pix0=np.array([x0, y0]), jac=jac, xx=xx, yy=yy, sci=sci, var=var,
             usable=usable, psf=tpsf, os=lib.oversample, nb=nb, nb_pos=nb_pos, flags=flags,
             psf_frac=psf_frac, lam=lam)
    return e, None


def _setup(e: _Exp, use: np.ndarray, veff: np.ndarray) -> bool:
    """Freeze pixel mask and weights; orthonormal basis of the fixed (neighbour +
    background) columns so the target flux and the others can be profiled out."""
    n = int(use.sum())
    cols = [t[use] for t in e.nb]
    if settings.fit_background in ("constant", "plane"):
        cols.append(np.ones(n))
        if settings.fit_background == "plane":
            cols += [e.xx[use] - e.xx[use].mean(), e.yy[use] - e.yy[use].mean()]
    if n < max(MIN_PIX, len(cols) + 4):
        return False
    sw = 1.0 / np.sqrt(veff[use])
    b = e.sci[use] * sw
    if cols:
        m = np.stack(cols, axis=1) * sw[:, None]
        u, s, _ = np.linalg.svd(m, full_matrices=False)
        q = u[:, s > 1e-9 * s.max()]
        bperp = b - q @ (q.T @ b)
    else:
        q = np.zeros((n, 0))
        bperp = b
    e.use, e.veff, e.sw, e.q, e.bperp = use, veff, sw, q, bperp
    e.bb = float(bperp @ bperp)
    e.k = q.shape[1] + 1
    return True


def _exp_chi2(e: _Exp, p: np.ndarray) -> tuple[float, float, float]:
    """(chi2, target flux, flux sigma) with every other linear parameter profiled out;
    the target flux is constrained >= 0 (a negative 'source' is a hole, not the target)."""
    t = e.template(p)[e.use] * e.sw
    tp = t - e.q @ (e.q.T @ t)
    tt = float(tp @ tp)
    if tt <= 1e-30:
        return e.bb, 0.0, float("inf")
    tb = float(tp @ e.bperp)
    f = tb / tt
    sig = 1 / math.sqrt(tt)
    return (e.bb - tb * tb / tt if f > 0 else e.bb), f, sig


def _total(exps: list[_Exp], p: np.ndarray) -> float:
    return sum(_exp_chi2(e, p)[0] for e in exps)


def _reweight(exps: list[_Exp], p: np.ndarray) -> list[_Exp]:
    """Full linear fit at p with residual clipping and the PSF-model error term; the
    resulting mask and effective variance are frozen for the next search round."""
    out = []
    for e in exps:
        tpl = [e.template(p)] + e.nb
        xt, yt = e.pix(p)
        protect = np.hypot(e.xx - xt, e.yy - yt) <= PROTECT_R_PX
        for xn, yn in e.nb_pos:
            protect |= np.hypot(e.xx - xn, e.yy - yn) <= PROTECT_R_PX
        region = np.ones(e.sci.shape, bool)
        res = psfmod.fit_fluxes(e.sci, e.var, e.usable, region, tpl, background=settings.fit_background,
                                min_pix=MIN_PIX, clip=RESID_CLIP, protect=protect,
                                model_error=settings.psf_model_error)
        if res is None or res.use is None:
            continue
        src = np.zeros(e.sci.shape)
        for f, t in zip(res.flux, tpl):
            src += abs(float(f)) * t
        veff = e.var + (settings.psf_model_error * src) ** 2
        if _setup(e, res.use, veff):
            out.append(e)
    return out


def _search(exps: list[_Exp], radius: float, step: float, p_init: np.ndarray, grid: bool) -> np.ndarray:
    """Global grid search over the disk |p| <= radius (when ``grid``), then Nelder-Mead
    from the best node, bounded to the disk."""
    g = np.arange(-radius, radius + 1e-9, step) if grid else []
    best_p, best_c = p_init.copy(), _total(exps, p_init)
    for gx in g:
        for gy in g:
            q = np.array([gx, gy])
            if q @ q > radius * radius:
                continue
            c = _total(exps, q)
            if c < best_c:
                best_p, best_c = q, c

    def cost(q):
        r = math.hypot(q[0], q[1])
        if r > radius:
            return _total(exps, q * radius / r) + 1e4 * (r - radius) ** 2
        return _total(exps, q)

    s0 = step / 2
    opt = optimize.minimize(cost, best_p, method="Nelder-Mead",
                            options={"xatol": 0.01, "fatol": 1e-3, "maxiter": 400,
                                     "initial_simplex": [best_p, best_p + [s0, 0], best_p + [0, s0]]})
    p = np.asarray(opt.x, float)
    r = math.hypot(*p)
    return p * radius / r if r > radius else p


def _hessian(exps: list[_Exp], p: np.ndarray, h: float) -> np.ndarray:
    f0 = _total(exps, p)
    H = np.zeros((2, 2))
    for i in range(2):
        ei = np.eye(2)[i] * h
        H[i, i] = (_total(exps, p + ei) - 2 * f0 + _total(exps, p - ei)) / h**2
    e0, e1 = np.eye(2)[0] * h, np.eye(2)[1] * h
    H[0, 1] = H[1, 0] = (_total(exps, p + e0 + e1) - _total(exps, p + e0 - e1)
                         - _total(exps, p - e0 + e1) + _total(exps, p - e0 - e1)) / (4 * h * h)
    return H


def measure_group(exposures: list[tuple[NativeCutout, psfmod.PSFLibrary, dict]], neighbours: list[Source],
                  start_ra: float, start_dec: float, *, group: str, season: int | None, detector: int,
                  search_radius_arcsec: float = SEARCH_RADIUS_ARCSEC,
                  detect_snr: float = DETECT_SNR) -> GroupMeasurement:
    """Joint position fit of one target over a group of exposures (see module doc).
    ``neighbours`` are held at their own (catalogue) propagated positions."""
    gm = GroupMeasurement(group=group, season=season, detector=detector, status="unusable",
                          start_ra=start_ra, start_dec=start_dec, search_radius_arcsec=search_radius_arcsec,
                          n_exposures=len(exposures))
    if exposures:
        gm.mjd_mean = float(np.mean([float(m["mjd"]) for _, _, m in exposures]))
    exps: list[_Exp] = []
    skipped: dict[str, int] = {}
    pixscale = None
    for cut, lib, meta in exposures:
        if pixscale is None:
            pixscale = math.sqrt(abs(np.linalg.det(cut.wcs.pixel_scale_matrix))) * 3600
        e, why = _prepare(cut, lib, meta, start_ra, start_dec, neighbours,
                          search_radius_arcsec / (pixscale or settings.pixel_scale_arcsec))
        if e is None:
            skipped[why or "?"] = skipped.get(why or "?", 0) + 1
        else:
            exps.append(e)
    pixscale = pixscale or settings.pixel_scale_arcsec
    for k, v in sorted(skipped.items()):
        gm.flags.append(f"{k}x{v}")
    exps = [e for e in exps if _setup(e, e.usable, e.var)]
    if not exps:
        gm.reason = "no exposure has enough unmasked pixels on the target"
        return gm
    radius = search_radius_arcsec
    p = np.zeros(2)
    step = GRID_STEP_PX * pixscale
    for rnd in range(N_ROUNDS):
        if rnd > 0:
            exps = _reweight(exps, p)
            if not exps:
                gm.reason = "fits failed after clipping"
                return gm
        p = _search(exps, radius, step, p, grid=rnd == 0)

    # ---- final numbers at the solution ------------------------------------------------
    chi2 = 0.0
    fl, sg, mj = [], [], []
    for e in exps:
        c, f, s = _exp_chi2(e, p)
        chi2 += c
        fl.append(f)
        sg.append(s)
        mj.append(e.mjd)
    dof = sum(int(e.use.sum()) - e.k for e in exps) - 2
    gm.chi2, gm.dof = float(chi2), int(dof)
    gm.n_used = len(exps)
    gm.frames = [str(e.meta.get("id")) for e in exps]
    chi2_red = chi2 / dof if dof > 0 else float("inf")
    infl = math.sqrt(max(1.0, chi2_red))
    fl, sg, mj = np.array(fl), np.array(sg) * infl, np.array(mj)
    ok = np.isfinite(sg) & (sg > 0)
    w = 1 / sg[ok] ** 2
    gm.snr = float(np.sum(w * fl[ok]) / math.sqrt(np.sum(w))) if ok.any() else 0.0
    ww = np.clip(fl[ok], 0, None) ** 2 * w
    gm.mjd = float(np.sum(ww * mj[ok]) / np.sum(ww)) if ww.sum() > 0 else float(np.mean(mj))
    lams = [e.lam for e in exps if e.lam]
    if lams:
        gm.lam_lo, gm.lam_hi = min(v[0] for v in lams), max(v[1] for v in lams)
    for f in ("SATURATED_CORE", "MASKED_CORE", "WINGS_ONLY", "EDGE", "UNRESOLVED_BLEND"):
        n = sum(f in e.flags for e in exps)
        if n:
            gm.flags.append(f if n == len(exps) else f"{f}({n}/{len(exps)})")
    if chi2_red > CHI2_POOR:
        gm.flags.append("POOR_FIT")
    gm.offset_from_start_arcsec = float(math.hypot(*p))
    gm.delta_chi2_vs_start = float(_total(exps, np.zeros(2)) - chi2)

    if gm.snr < detect_snr:
        gm.status = "not_detected"
        gm.reason = f"S/N {gm.snr:.1f} of the combined target flux is below {detect_snr:g}"
        return gm
    ra, dec = from_tp(p[0], p[1], start_ra, start_dec)
    gm.ra, gm.dec = float(ra), float(dec)
    if math.hypot(*p) > radius - BOUND_TOL_PX * pixscale:
        gm.status = "at_bound"
        gm.reason = (f"best position lies on the {radius:.0f}\" search bound: the source is not where it was "
                     "looked for (or something brighter is just outside)")
        return gm
    H = _hessian(exps, p, HESS_STEP_ARCSEC)
    try:
        ev = np.linalg.eigvalsh(H)
        if ev.min() <= 0:
            raise np.linalg.LinAlgError
        cov = 2 * np.linalg.inv(H) * max(1.0, chi2_red)
    except np.linalg.LinAlgError:
        gm.status = "no_minimum"
        gm.reason = "chi2 surface has no well-defined minimum"
        return gm
    k = _scatter_factor(exps, p, radius)
    if k is not None:
        gm.scatter_chi2_red = k
        if k > 1:
            cov = cov * k
            gm.flags.append("SCATTER_INFLATED")
    gm.cov_fit = cov
    gm.status = "measured"
    return gm


def _scatter_factor(exps: list[_Exp], p: np.ndarray, radius: float) -> float | None:
    """Empirical check of the joint-fit error: fit each well-detected exposure's own
    position (S/N >= SINGLE_SNR) and return chi2/dof of those positions about the joint
    solution, using each one's own curvature error. > 1 means exposure-to-exposure
    systematics (saturation non-linearity, PSF mismatch, WCS jitter) that the pixel
    chi2 does not capture; the caller inflates the covariance by it."""
    pts = []
    for e in exps:
        c0, f, sg = _exp_chi2(e, p)
        if not (sg > 0 and math.isfinite(sg)) or f / sg < SINGLE_SNR:
            continue
        one = [e]

        def cost(q):
            r = math.hypot(q[0], q[1])
            if r > radius:
                return _total(one, q * radius / r) + 1e4 * (r - radius) ** 2
            return _total(one, q)

        opt = optimize.minimize(cost, p, method="Nelder-Mead",
                                options={"xatol": 0.01, "fatol": 1e-3, "maxiter": 200})
        q = np.asarray(opt.x, float)
        if math.hypot(*q) > radius - 0.5:
            continue
        H = _hessian(one, q, HESS_STEP_ARCSEC)
        if np.linalg.eigvalsh(H).min() <= 0:
            continue
        dof = int(e.use.sum()) - e.k - 2
        red = max(1.0, float(cost(q)) / dof) if dof > 0 else 1.0
        pts.append((q - p, 2 * np.linalg.inv(H) * red))
    if len(pts) < 3:
        return None
    chi2 = sum(float(d @ np.linalg.solve(C, d)) for d, C in pts)
    return chi2 / (2 * len(pts) - 2)


# =========================================================================== registration
def registration_floor(offsets: list[tuple[np.ndarray, np.ndarray]], clip: float = 5.0,
                       min_floor: float = 0.0) -> tuple[float, dict]:
    """Extra per-axis scatter (arcsec) that static reference stars show beyond their
    formal errors: solve sum d^T (C + s^2 I)^-1 d = 2n for s (0 when the formal errors
    already explain the scatter), with iterative rejection of > ``clip`` sigma offsets
    (blends, mismatches). ``offsets`` = [(measured - catalogue (east, north), C), ...]."""
    keep = list(range(len(offsets)))
    s = 0.0
    info: dict = {"n": len(offsets)}

    def stat(s2: float, idx: list[int]) -> float:
        return sum(float(offsets[i][0] @ np.linalg.solve(offsets[i][1] + np.eye(2) * s2, offsets[i][0]))
                   for i in idx)

    for _ in range(10):
        if len(keep) < 3:
            break
        target = 2 * len(keep)
        if stat(0.0, keep) <= target:
            s = 0.0
        else:
            lo, hi = 0.0, 100.0
            for _ in range(60):
                mid = (lo + hi) / 2
                if stat(mid * mid, keep) > target:
                    lo = mid
                else:
                    hi = mid
            s = hi
        z = [math.sqrt(float(offsets[i][0] @ np.linalg.solve(offsets[i][1] + np.eye(2) * s * s, offsets[i][0])) / 2)
             for i in keep]
        worst = int(np.argmax(z))
        if z[worst] > clip:
            keep.pop(worst)
        else:
            break
    s = max(s, min_floor)
    info.update({"n_used": len(keep), "chi2_formal": stat(0.0, keep) if keep else None,
                 "dof": 2 * len(keep), "rejected": len(offsets) - len(keep)})
    if keep:
        d = np.array([offsets[i][0] for i in keep])
        info["rms_east_arcsec"] = float(np.sqrt(np.mean(d[:, 0] ** 2)))
        info["rms_north_arcsec"] = float(np.sqrt(np.mean(d[:, 1] ** 2)))
        info["mean_east_arcsec"] = float(np.mean(d[:, 0]))
        info["mean_north_arcsec"] = float(np.mean(d[:, 1]))
        info["median_formal_sigma_arcsec"] = float(np.median([math.sqrt(np.trace(offsets[i][1]) / 2) for i in keep]))
    return s, info


def pick_reference_stars(cat: CatalogResult, centre_ra: float, centre_dec: float, half_size_arcsec: float,
                         avoid: list[tuple[float, float]], avoid_radius_arcsec: float, n: int = 6,
                         g_range: tuple[float, float] = (11.0, 16.5), isolation_arcsec: float = 18.0
                         ) -> list[Star]:
    """Isolated Gaia stars with a proper motion, well inside the field and far from the
    target's path, brightest first. They are measured exactly like the target to learn
    how well the procedure recovers known positions (the registration floor)."""
    stars = [s for s in cat.stars if s.has_pm and s.g_mag is not None]
    out: list[Star] = []
    for s in sorted(stars, key=lambda s: s.g_mag):
        if not g_range[0] <= s.g_mag <= g_range[1]:
            continue
        xi, eta = to_tp(s.ra, s.dec, centre_ra, centre_dec)
        if max(abs(float(xi)), abs(float(eta))) > half_size_arcsec:
            continue
        if avoid and min(float(_sep_arcsec(s.ra, s.dec, a[0], a[1])) for a in avoid) < avoid_radius_arcsec:
            continue
        close = [o for o in cat.stars if o is not s and o.g_mag is not None and o.g_mag < s.g_mag + 3.0
                 and float(_sep_arcsec(s.ra, s.dec, o.ra, o.dec)) < isolation_arcsec]
        if close:
            continue
        out.append(s)
        if len(out) >= n:
            break
    return out
