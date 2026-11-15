"""SPHEREx effective PSFs (ePSFs) and forced multi-source PSF photometry in native
detector pixels. Pure numerics except for the small PSF-library cache/fetch helpers.

PSF library
-----------
Every IRSA L2 file (and every cutout of it) carries an ``EPSF`` table: 11 x 41 zones
tiling the 2040 x 2040 active pixels ("uniform active-pixel tiling"), each a 33 x 33
ePSF oversampled 5x (unit sum over the oversampled grid), with zone centres
XCENTER/YCENTER in 0-based active-pixel coordinates. Cutouts map their pixels to
active pixels through the alternate WCS 'A' (``CRPIX1A`` ...).

Verified Nov 2026: the table is byte-identical for every exposure of a detector
(all l2b-v27 files, DR1 and QR3 alike), so one library per (pipeline, detector) is
stored in ``cache/psf/{pipeline}/D{n}.npz``. It is captured from new cutout downloads
before the table is stripped, or range-read once (~4 MB, never the 70 MB file) from
the AWS open-data copy of any L2 file of that detector.
"""

from __future__ import annotations

import logging
import math
import re
import threading
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np
from astropy.io import fits
from scipy import ndimage

from .config import settings

log = logging.getLogger(__name__)

EPSF_DTYPE = np.dtype([
    ("BINX", ">i4"), ("BINY", ">i4"), ("XCENTER", ">f4"), ("YCENTER", ">f4"),
    ("XWIDTH", ">f4"), ("YWIDTH", ">f4"), ("NSTAR", ">i4"), ("NEFF_MEAN", ">f4"),
    ("CWAVE", ">f4"), ("CBAND", ">f4"), ("EPSF", ">f8", (33, 33)),
])
_IRSA_PREFIX = "/ibe/data/spherex/"
_lock = threading.Lock()


# --------------------------------------------------------------------------- library
@dataclass
class PSFLibrary:
    detector: int
    xc: np.ndarray          # (nx,) zone centres, 0-based active pixels
    yc: np.ndarray          # (ny,)
    epsf: np.ndarray        # (ny, nx, m, m) oversampled ePSFs, each with unit sum
    cwave: np.ndarray       # (ny, nx) um
    oversample: int = 5

    def at(self, ax: float, ay: float) -> np.ndarray:
        """ePSF at active-pixel position (ax, ay): bilinear between the 4 nearest zones."""
        def bracket(c: np.ndarray, v: float) -> tuple[int, int, float]:
            if v <= c[0]:
                return 0, 0, 0.0
            if v >= c[-1]:
                return len(c) - 1, len(c) - 1, 0.0
            i = int(np.searchsorted(c, v) - 1)
            return i, i + 1, float((v - c[i]) / (c[i + 1] - c[i]))

        i0, i1, ty = bracket(self.yc, ay)
        j0, j1, tx = bracket(self.xc, ax)
        p = ((1 - ty) * (1 - tx) * self.epsf[i0, j0] + (1 - ty) * tx * self.epsf[i0, j1]
             + ty * (1 - tx) * self.epsf[i1, j0] + ty * tx * self.epsf[i1, j1])
        p = np.asarray(p, np.float64)
        return p / p.sum()


def pipeline_tag(access_url: str) -> str:
    """'l2b-v27' from .../l2b-v27-2026-197/... (the ePSF depends on the pipeline version)."""
    m = re.search(r"/(l\d[a-z]?)(?:_[a-z]+)?-(v\d+)-", access_url or "")  # l2b_retry-v27 -> l2b-v27
    return f"{m.group(1)}-{m.group(2)}" if m else "unknown"


def library_path(tag: str, detector: int) -> Path:
    return settings.cache_dir / "psf" / tag / f"D{detector}.npz"


def library_from_table(data, detector: int, oversample: int = 5) -> PSFLibrary:
    xs = np.unique(np.round(np.asarray(data["XCENTER"], float), 3))
    ys = np.unique(np.round(np.asarray(data["YCENTER"], float), 3))
    bx = np.asarray(data["BINX"], int)
    by = np.asarray(data["BINY"], int)
    m = np.asarray(data["EPSF"]).shape[-1]
    epsf = np.zeros((len(ys), len(xs), m, m), np.float32)
    cw = np.zeros((len(ys), len(xs)), np.float32)
    epsf[by, bx] = np.asarray(data["EPSF"], np.float64)
    cw[by, bx] = np.asarray(data["CWAVE"], np.float32)
    return PSFLibrary(detector=detector, xc=xs, yc=ys, epsf=epsf, cwave=cw, oversample=oversample)


def save_library(lib: PSFLibrary, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp.npz")
    np.savez_compressed(tmp, detector=lib.detector, xc=lib.xc, yc=lib.yc, epsf=lib.epsf,
                        cwave=lib.cwave, oversample=lib.oversample)
    tmp.replace(path)


@lru_cache(maxsize=32)
def _load(path: str, mtime: float) -> PSFLibrary:
    with np.load(path) as z:
        return PSFLibrary(detector=int(z["detector"]), xc=z["xc"], yc=z["yc"], epsf=z["epsf"],
                          cwave=z["cwave"], oversample=int(z["oversample"]))


def load_library(access_url: str, detector: int) -> PSFLibrary | None:
    p = library_path(pipeline_tag(access_url), detector)
    return _load(str(p), p.stat().st_mtime) if p.exists() else None


def capture_from_hdul(hdul: fits.HDUList, access_url: str, detector: int) -> bool:
    """Save the library from an unstripped cutout if this (pipeline, detector) has none yet."""
    if "EPSF" not in hdul:
        return False
    p = library_path(pipeline_tag(access_url), detector)
    if p.exists():
        return False
    hdu = hdul["EPSF"]
    lib = library_from_table(hdu.data, detector, int(hdu.header.get("OVSMPX", 5)))
    with _lock:
        if not p.exists():
            save_library(lib, p)
    return True


def s3_url(access_url: str) -> str:
    i = access_url.find(_IRSA_PREFIX)
    if i < 0:
        raise ValueError(f"not an IRSA SPHEREx URL: {access_url}")
    return settings.spherex_s3_base.rstrip("/") + "/" + access_url[i + len(_IRSA_PREFIX):]


def fetch_library(access_url: str, detector: int) -> PSFLibrary:
    """Range-read only the EPSF table of an L2 file from the AWS open-data bucket
    (headers are read lazily via fsspec; the image planes are skipped). Blocking."""
    import httpx

    url = s3_url(access_url)
    with fits.open(url, use_fsspec=True, lazy_load_hdus=True,
                   fsspec_kwargs={"block_size": 32 * 1024, "cache_type": "bytes"}) as h:
        hdu = next((x for x in h if x.name == "EPSF"), None)
        if hdu is None:
            raise RuntimeError("no EPSF extension")
        off, hdr = hdu._data_offset, hdu.header.copy()
    nrow = int(hdr["NAXIS2"])
    nbytes = int(hdr["NAXIS1"]) * nrow
    if int(hdr["NAXIS1"]) != EPSF_DTYPE.itemsize:
        raise RuntimeError("unexpected EPSF table layout")
    r = httpx.get(url, headers={"Range": f"bytes={off}-{off + nbytes - 1}"},
                  timeout=settings.http_timeout_s)
    r.raise_for_status()
    if len(r.content) != nbytes:
        raise RuntimeError(f"short EPSF read ({len(r.content)} of {nbytes} bytes)")
    data = np.frombuffer(r.content, dtype=EPSF_DTYPE)
    lib = library_from_table(data, detector, int(hdr.get("OVSMPX", 5)))
    p = library_path(pipeline_tag(access_url), detector)
    with _lock:
        save_library(lib, p)
    return load_library(access_url, detector) or lib


def active_offset(header: fits.Header) -> tuple[float, float]:
    """(dx, dy) such that active = cutout_pixel_0based + (dx, dy), from the 'A' WCS."""
    def one(i: int) -> float:
        crpix = float(header.get(f"CRPIX{i}A", 1.0))
        crval = float(header.get(f"CRVAL{i}A", 0.0))
        cdelt = float(header.get(f"CDELT{i}A", 1.0))
        if cdelt != 1.0:
            raise ValueError("unexpected active-pixel WCS scale")
        return crval + 1.0 - crpix  # active = crval + (p1 - crpix), p1 = p0 + 1
    return one(1), one(2)


# --------------------------------------------------------------------------- rendering
def render(psf_os: np.ndarray, x: float, y: float, xs: np.ndarray, ys: np.ndarray,
           oversample: int) -> np.ndarray:
    """ePSF value (fraction of total flux) in native pixels centred at (xs, ys) for a
    source at (x, y). ePSFs are pixel-integrated, so they are *sampled*, not integrated:
    value = ePSF(dx * os, dy * os) * os^2. Cubic-spline interpolation; 0 outside."""
    c = (psf_os.shape[0] - 1) / 2
    u = c + (np.asarray(xs, float) - x) * oversample
    v = c + (np.asarray(ys, float) - y) * oversample
    out = ndimage.map_coordinates(psf_os, [v.ravel(), u.ravel()], order=3, mode="constant",
                                  cval=0.0, prefilter=True)
    return np.clip(out.reshape(np.shape(u)), 0.0, None) * oversample**2


def psf_radius_px(psf_os: np.ndarray, oversample: int) -> float:
    return (psf_os.shape[0] - 1) / 2 / oversample


# --------------------------------------------------------------------------- fitting
@dataclass
class FitResult:
    flux: np.ndarray        # (n_src,) in image units x pixel
    err: np.ndarray         # (n_src,) 1-sigma, scaled by sqrt(chi2_red) when > 1
    background: np.ndarray  # background coefficients (constant [, d/dx, d/dy])
    chi2_red: float
    n_pix: int
    dof: int
    cov: np.ndarray
    model: np.ndarray | None = None
    use: np.ndarray | None = None     # pixels actually used (after clipping)


def fit_fluxes(sci: np.ndarray, var: np.ndarray, usable: np.ndarray, region: np.ndarray,
               templates: list[np.ndarray], background: str = "constant",
               min_pix: int = 8, clip: float | None = None,
               protect: np.ndarray | None = None, max_iter: int = 3,
               model_error: float = 0.0) -> FitResult | None:
    """Weighted linear least squares: sci = sum_k f_k T_k + background, weights 1/var,
    over ``region & usable``. ``templates`` are full-size unit-flux images.
    background: 'constant' | 'plane' | 'none'. Returns None when under-determined.

    ``model_error`` (fraction): PSF-model uncertainty added to the variance as
    (model_error * source model)^2 and the fit re-weighted, so bright sources are not
    judged (chi2) or weighted as if the ePSF were perfect.

    With ``clip``, pixels whose residual exceeds ``clip`` sigma and that are not in
    ``protect`` (the cores of the fitted sources) are dropped and the fit repeated:
    this removes uncatalogued contaminants (another star's core at the box edge, a
    cosmic ray the pipeline missed) without touching the modelled sources."""
    use = region & usable & np.isfinite(sci) & np.isfinite(var) & (var > 0)
    res = _solve(sci, var, use, templates, background, min_pix)
    veff = var
    for it in range(max_iter + 1):
        if res is None:
            return None
        if model_error > 0:
            src = np.zeros(sci.shape)
            for f, t in zip(res.flux, templates):
                src += abs(float(f)) * t
            veff = var + (model_error * src) ** 2
            res = _solve(sci, veff, use, templates, background, min_pix)
            if res is None:
                return None
        if clip is None or it == max_iter:
            break
        z = (sci - res.model) / np.sqrt(np.where(veff > 0, veff, np.inf))
        bad = use & (np.abs(np.nan_to_num(z)) > clip)
        if protect is not None:
            bad &= ~protect
        if not bad.any():
            break
        use = use & ~bad
        res = _solve(sci, var, use, templates, background, min_pix)
    return res


def _solve(sci: np.ndarray, var: np.ndarray, use: np.ndarray, templates: list[np.ndarray],
           background: str, min_pix: int) -> FitResult | None:
    n = int(use.sum())
    yy, xx = np.nonzero(use)
    cols = [t[use] for t in templates]
    if background in ("constant", "plane"):
        cols.append(np.ones(n))
        if background == "plane" and n:
            cols += [xx - xx.mean(), yy - yy.mean()]
    k = len(cols)
    if n < max(min_pix, k + 3):
        return None
    A = np.stack(cols, axis=1).astype(float)
    sw = 1.0 / np.sqrt(var[use])
    Aw = A * sw[:, None]
    bw = sci[use] * sw
    ata = Aw.T @ Aw
    try:
        cov = np.linalg.inv(ata)
    except np.linalg.LinAlgError:
        cov = np.linalg.pinv(ata)
    coef = cov @ (Aw.T @ bw)
    resid = bw - Aw @ coef
    dof = n - k
    chi2_red = float(resid @ resid) / dof
    scale2 = chi2_red if chi2_red > 1 else 1.0
    ns = len(templates)
    err = np.sqrt(np.clip(np.diag(cov)[:ns] * scale2, 0, None))
    model = np.full(sci.shape, np.nan)
    model[use] = A @ coef
    return FitResult(flux=coef[:ns], err=err, background=coef[ns:], chi2_red=chi2_red,
                     n_pix=n, dof=dof, cov=cov * scale2, model=model, use=use)


def aperture(sci: np.ndarray, var: np.ndarray, usable: np.ndarray, x: float, y: float,
             r_ap: float, r_in: float, r_out: float, min_frac: float = 0.7
             ) -> tuple[float, float, float] | None:
    """Circular aperture sum minus annulus median sky, in image units x pixel.
    Masked aperture pixels are compensated by area. Returns (sum, err, used_frac)."""
    yy, xx = np.mgrid[0:sci.shape[0], 0:sci.shape[1]]
    rr = np.hypot(xx - x, yy - y)
    ap = rr <= r_ap
    if ap.sum() == 0:
        return None
    good = ap & usable & np.isfinite(sci)
    frac = good.sum() / ap.sum()
    if frac < min_frac:
        return None
    ann = (rr >= r_in) & (rr <= r_out) & usable & np.isfinite(sci)
    sky = float(np.median(sci[ann])) if ann.sum() >= 5 else 0.0
    k = ap.sum() / good.sum()
    s = float(np.sum(sci[good] - sky)) * k
    e = math.sqrt(float(np.sum(var[good]))) * k
    return s, e, float(frac)


# --------------------------------------------------------------------------- extended PSF
# The ePSF stops at 3.3 px (33 x 33 at 5x oversampling). A saturated star masks its core
# out to ~3-5 px (OVERFLOW/SUR_ERROR/TRANSIENT/BLOOM), so its only information is in wings
# the ePSF does not model. The extended profile is EMPIRICAL: the azimuthally averaged
# residual (image - sky) / F_core - ePSF of many bright, unsaturated, isolated stars, where
# F_core is their ordinary ePSF-fit flux. The template ePSF + wing(r) is therefore on the
# same flux scale as ordinary fits; a residual calibration (wing-only vs core flux of
# unsaturated stars with an artificially masked core) is stored with the profile.
EXT_VERSION = "1"
EXT_R_MIN = 2.0                     # the wing term starts here (the ePSF owns the core)
EXT_R_MAX = 12.0
EXT_DR = 0.5
EXT_BG_ANNULUS = (13.0, 16.0)       # sky for the profile: sigma-clipped median in this annulus
EXT_DEFAULT_SYS = 0.25              # fractional systematic when no calibration exists


@dataclass
class ExtendedProfile:
    detector: int
    r: np.ndarray            # bin centres (px)
    w: np.ndarray            # wing beyond the ePSF: fraction of the core flux per pixel
    w_err: np.ndarray
    n_star: int
    cal_r: np.ndarray        # equivalent masked-core radius (px) ...
    cal_ratio: np.ndarray    # ... median wing-fit / core-fit flux of unsaturated stars
    cal_sys: np.ndarray      # ... and the 1-sigma fractional systematic of one wing fit
    version: str = EXT_VERSION

    def wing(self, rr: np.ndarray) -> np.ndarray:
        r = np.concatenate([[EXT_R_MIN - EXT_DR / 2], self.r])
        w = np.concatenate([[0.0], self.w])
        return np.interp(rr, r, w, left=0.0, right=0.0)

    def calibration(self, r_mask: float) -> tuple[float, float]:
        """(ratio, fractional systematic) for a core masked out to r_mask px. Beyond the
        calibrated range the systematic grows linearly with the extrapolation distance."""
        if len(self.cal_r) == 0:
            return 1.0, EXT_DEFAULT_SYS
        ratio = float(np.interp(r_mask, self.cal_r, self.cal_ratio))
        sys = float(np.interp(r_mask, self.cal_r, self.cal_sys))
        if r_mask > self.cal_r[-1]:
            sys *= 1.0 + (r_mask - self.cal_r[-1]) / 2.0
        return ratio, sys


def ext_path(tag: str, detector: int) -> Path:
    return settings.cache_dir / "psf" / tag / f"ext{EXT_VERSION}_D{detector}.npz"


def save_extended(ext: ExtendedProfile, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp.npz")
    np.savez_compressed(tmp, detector=ext.detector, r=ext.r, w=ext.w, w_err=ext.w_err, n_star=ext.n_star,
                        cal_r=ext.cal_r, cal_ratio=ext.cal_ratio, cal_sys=ext.cal_sys, version=ext.version)
    tmp.replace(path)


@lru_cache(maxsize=32)
def _load_ext(path: str, mtime: float) -> ExtendedProfile:
    with np.load(path) as z:
        return ExtendedProfile(detector=int(z["detector"]), r=z["r"], w=z["w"], w_err=z["w_err"],
                               n_star=int(z["n_star"]), cal_r=z["cal_r"], cal_ratio=z["cal_ratio"],
                               cal_sys=z["cal_sys"], version=str(z["version"]))


def load_extended(access_url: str, detector: int) -> ExtendedProfile | None:
    """Cached profile for this (pipeline, detector), else the built-in one measured for
    l2b-v27 (see DEFAULT_EXT), else None."""
    tag = pipeline_tag(access_url)
    p = ext_path(tag, detector)
    if p.exists():
        return _load_ext(str(p), p.stat().st_mtime)
    return default_extended(detector) if tag in DEFAULT_EXT_TAGS else None


def render_extended(psf_os: np.ndarray, ext: ExtendedProfile | None, x: float, y: float,
                    xs: np.ndarray, ys: np.ndarray, oversample: int) -> np.ndarray:
    t = render(psf_os, x, y, xs, ys, oversample)
    if ext is not None:
        t = t + ext.wing(np.hypot(np.asarray(xs, float) - x, np.asarray(ys, float) - y))
    return t


# ---- builder pieces (pure numerics; orchestration in fingerprint.build_extended_profiles)
def find_stars(sci: np.ndarray, var: np.ndarray, usable: np.ndarray, saturated: np.ndarray,
               snr_min: float = 30.0, margin: float = EXT_BG_ANNULUS[1] + 1,
               isolation_px: float = EXT_R_MAX) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """(candidates, all_peaks) as (y, x). Candidates: >= snr_min peaks with a fully usable
    core (r <= 3 px), no saturated pixel within the profile + sky radius, at least `margin`
    from the edge, and no brighter peak within `isolation_px`. all_peaks (>= 5 sigma) are
    masked by the profile builder."""
    ny, nx = sci.shape
    good = usable & np.isfinite(sci)
    bg = float(np.median(sci[good])) if good.any() else 0.0
    s = np.where(good, sci - bg, -np.inf)
    with np.errstate(invalid="ignore", divide="ignore"):
        snr = np.where(good, s / np.sqrt(np.where(good & (var > 0), var, np.inf)), -np.inf)
    mx = ndimage.maximum_filter(s, size=3, mode="constant", cval=-np.inf)
    peaks = np.argwhere((s == mx) & (snr >= 5.0) & np.isfinite(s))
    allp = [(int(y), int(x)) for y, x in peaks]
    if not allp:
        return [], []
    py = np.array([p[0] for p in allp])
    px = np.array([p[1] for p in allp])
    pv = s[py, px]
    sat_yx = np.argwhere(saturated)
    yy, xx = np.mgrid[0:ny, 0:nx]
    out = []
    for (y, x), v in zip(allp, pv):
        if snr[y, x] < snr_min or not (margin <= x < nx - margin and margin <= y < ny - margin):
            continue
        core = np.hypot(xx - x, yy - y) <= 3.0
        if not good[core].all():
            continue
        if len(sat_yx) and np.min(np.hypot(sat_yx[:, 1] - x, sat_yx[:, 0] - y)) <= margin:
            continue
        d = np.hypot(px - x, py - y)
        if np.any((d > 0) & (d <= isolation_px) & (pv > v)):
            continue
        out.append((y, x))
    return out, allp


def fit_star(sci: np.ndarray, var: np.ndarray, usable: np.ndarray, x: float, y: float,
             psf_os: np.ndarray, oversample: int, box_px: int = 7, model_error: float = 0.03,
             n_iter: int = 5) -> tuple[float, float, float, float, float] | None:
    """Core ePSF fit of one isolated star with Gauss-Newton recentring: (x, y, flux, err, chi2)."""
    ny, nx = sci.shape
    half = (box_px - 1) // 2
    ix, iy = int(round(x)), int(round(y))
    region = np.zeros((ny, nx), bool)
    region[max(0, iy - half):iy + half + 1, max(0, ix - half):ix + half + 1] = True
    yy, xx = np.mgrid[0:ny, 0:nx]
    e = 0.05
    for _ in range(n_iter):
        t = render(psf_os, x, y, xx, yy, oversample) * region
        tx = (render(psf_os, x + e, y, xx, yy, oversample) - render(psf_os, x - e, y, xx, yy, oversample)) / (2 * e)
        ty = (render(psf_os, x, y + e, xx, yy, oversample) - render(psf_os, x, y - e, xx, yy, oversample)) / (2 * e)
        r = fit_fluxes(sci, var, usable, region, [t, tx * region, ty * region], clip=None,
                       model_error=model_error)
        if r is None or r.flux[0] <= 0:
            return None
        dx = float(np.clip(r.flux[1] / r.flux[0], -0.3, 0.3))
        dy = float(np.clip(r.flux[2] / r.flux[0], -0.3, 0.3))
        x, y = x + dx, y + dy
        if math.hypot(dx, dy) < 0.005:
            break
    if math.hypot(x - ix, y - iy) > 1.0:
        return None
    protect = np.hypot(xx - x, yy - y) <= 2.0
    t = render(psf_os, x, y, xx, yy, oversample) * region
    res = fit_fluxes(sci, var, usable, region, [t], clip=5.0, protect=protect, model_error=model_error)
    if res is None or res.flux[0] <= 0:
        return None
    return x, y, float(res.flux[0]), float(res.err[0]), float(res.chi2_red)


def star_wing_profile(sci: np.ndarray, var: np.ndarray, usable: np.ndarray, x: float, y: float,
                      flux: float, psf_os: np.ndarray, oversample: int,
                      exclude: np.ndarray | None = None) -> tuple[np.ndarray, np.ndarray]:
    """Per radial bin: clipped mean of (image - sky)/flux - ePSF, and its error."""
    ny, nx = sci.shape
    yy, xx = np.mgrid[0:ny, 0:nx]
    rr = np.hypot(xx - x, yy - y)
    ok = usable & np.isfinite(sci)
    if exclude is not None:
        ok &= ~exclude
    ann = ok & (rr >= EXT_BG_ANNULUS[0]) & (rr <= EXT_BG_ANNULUS[1])
    edges = np.arange(EXT_R_MIN, EXT_R_MAX + 1e-9, EXT_DR)
    nb = len(edges) - 1
    mean, err = np.full(nb, np.nan), np.full(nb, np.nan)
    if ann.sum() < 30:
        return mean, err
    a = sci[ann]
    for _ in range(3):
        med = np.median(a)
        mad = 1.4826 * np.median(np.abs(a - med))
        a = a[np.abs(a - med) <= 3 * mad + 1e-30]
    sky = float(np.median(a))
    sky_err = 1.2533 * float(np.std(a)) / math.sqrt(len(a))
    resid = (sci - sky) / flux - render(psf_os, x, y, xx, yy, oversample)
    sig = np.sqrt(np.where(var > 0, var, np.inf)) / flux
    for i in range(nb):
        m = ok & (rr >= edges[i]) & (rr < edges[i + 1])
        if m.sum() < 4:
            continue
        v, s = resid[m], sig[m]
        keep = np.ones(v.size, bool)
        for _ in range(3):
            med = np.median(v[keep])
            mad = max(1.4826 * np.median(np.abs(v[keep] - med)), float(np.median(s)))
            nk = np.abs(v - med) <= 4 * mad
            if (nk == keep).all():
                break
            keep = nk
        if keep.sum() < 4:
            continue
        mean[i] = float(np.mean(v[keep]))
        err[i] = math.hypot(float(np.std(v[keep])) / math.sqrt(keep.sum()), sky_err / flux)
    return mean, err


def combine_profiles(means: np.ndarray, errs: np.ndarray, err_floor: float = 0.0
                     ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Stars x bins -> (w, w_err, n): per bin, inverse-variance mean after iterative 4-sigma
    clipping (scatter-scaled), so a few contaminated stars do not bias the wing."""
    nb = means.shape[1]
    w, we, n = np.full(nb, np.nan), np.full(nb, np.nan), np.zeros(nb, int)
    for i in range(nb):
        f, e = means[:, i], errs[:, i]
        ok = np.isfinite(f) & np.isfinite(e) & (e > 0)
        f, e = f[ok], np.hypot(e[ok], err_floor)
        if f.size < 3:
            continue
        keep = np.ones(f.size, bool)
        for _ in range(10):
            ww = 1 / e[keep] ** 2
            m = float(np.sum(ww * f[keep]) / np.sum(ww))
            z = (f - m) / e
            sc = max(1.0, 1.4826 * float(np.median(np.abs(z[keep] - np.median(z[keep])))))
            nk = np.abs(z) <= 4 * sc
            if (nk == keep).all() or nk.sum() < 3:
                break
            keep = nk
        ww = 1 / e[keep] ** 2
        m = float(np.sum(ww * f[keep]) / np.sum(ww))
        chi2 = float(np.sum(ww * (f[keep] - m) ** 2)) / max(1, keep.sum() - 1)
        w[i], we[i], n[i] = m, math.sqrt(1 / float(np.sum(ww)) * max(1.0, chi2)), int(keep.sum())
    return w, we, n


# Built-in profiles (l2b-v27), measured by fingerprint.build_extended_profiles from the six
# validation fields; used when cache/psf/{tag}/ext{EXT_VERSION}_D{n}.npz is absent.
DEFAULT_EXT_TAGS = {"l2b-v27"}
DEFAULT_EXT: dict[int, dict] = {}


def default_extended(detector: int) -> ExtendedProfile | None:
    d = DEFAULT_EXT.get(detector)
    if not d:
        return None
    r = EXT_R_MIN + EXT_DR * (np.arange(len(d["w"])) + 0.5)
    return ExtendedProfile(detector=detector, r=r, w=np.array(d["w"], float), w_err=np.array(d["w_err"], float),
                           n_star=int(d["n_star"]), cal_r=np.array(d["cal_r"], float),
                           cal_ratio=np.array(d["cal_ratio"], float), cal_sys=np.array(d["cal_sys"], float))
