"""Mover Fingerprint: forced, deblended PSF photometry of one source in every SPHEREx
exposure of a field, at its proper-motion-propagated position.

Per exposure (native detector pixels of the cached cutout, never the reprojected grid):
  1. propagate the target and every Gaia neighbour to the exposure MJD -> pixel (x, y);
  2. take the ePSF for each source's detector position (psf.PSFLibrary.at);
  3. weighted linear least squares for all fluxes + a constant (or plane) background in
     a box around the target grown to cover the neighbours' cores, weights 1/VARIANCE,
     pixels with mask_flags/suspect_flags excluded;
  4. also a naive aperture (r = 1.5 px, annulus sky), aperture-corrected with the same
     ePSF so that it is a total-flux estimate directly comparable to the PSF flux.
Then: inverse-variance, outlier-clipped combination into the 102 official channels
(overall and per season), per-neighbour spectra, and per-wavelength-bin light curves
with a chi2 test against constancy.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import time
import warnings
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np
from astropy.io import fits
from astropy.wcs import WCS, FITSFixedWarning
from scipy import optimize
from scipy import stats as sstats

from . import psf as psfmod
from .bands import BAND_CENTERS_UM, band_index
from .bins import build_bins
from .catalogs import CATALOG_NAME, GAIA_DR3_EPOCH_MJD, CatalogResult, Star, gaia_cone
from .classify import classify
from .config import settings
from .coords import SPHEREX_REF_MJD, propagate
from .processing import Grid, _celestial_header, _mask_for, _spectral_wcs, decode_flags, flag_bits
from .sia import ImageRecord

log = logging.getLogger(__name__)

# Bump whenever the output (numbers or shape) changes.
FINGERPRINT_VERSION = "3"

SELF_MATCH_ARCSEC = 2.0      # a Gaia source this close to the target (at the Gaia epoch) IS it
REL_FAINT_MAG = 7.5          # skip neighbours this much fainter than a Gaia-matched target
BRIGHT_TARGET_G = 11.0       # ... and no-PM Gaia sources this close to targets brighter than this
SPURIOUS_RADIUS_ARCSEC = 15.0
MAX_BOX_HALF = 6             # fit box never grows beyond 13 x 13 px
MIN_TEMPLATE_SUM = 0.02      # a source contributing < 2% of its flux to the box is not fitted
CHI2_SUSPECT = 5.0
RESID_CLIP = 5.0             # drop unmodelled >5 sigma pixels outside the fitted cores ...
PROTECT_R_PX = 2.0           # ... i.e. farther than this from every fitted source
UNRESOLVED_PX = 0.4          # a neighbour this close to the target cannot be separated
MIN_PSF_FRAC = 0.08          # bad when < 8% of the target's PSF lands on usable pixels
RECENTER_SNR = 15.0          # fit a common sub-pixel shift when a source is this significant
MAX_SHIFT_PX = 0.5
CLIP_SIGMA = 4.0
LC_SYS_FRAC = 0.02           # systematic floor added to season means before the chi2 test
LC_SYS_FRAC_SUSPECT = 0.15   # ... when only suspect (e.g. saturated, wings-only) points exist
LC_SYS_FRAC_WINGS = 0.10     # ... bins dominated by extended-profile wing fits (per-point errors
                             # already carry the calibration systematic; this is the common part)
WING_FLAG = "saturated_wings"
WING_MIN_PIX = 30
SLOPE_NODE_SNR = 10.0        # in-channel slope correction only from well-measured channels
WINGS_ONLY_FRAC = 0.5        # flag fits where < 50% of the target PSF is on usable pixels
LC_P_VARIABLE = 1e-3
SATURATION_FLAGS = {"OVERFLOW", "NONLINEAR", "SUR_ERROR"}
# Failed-catalogue results are kept in memory only this long, then retried.
RETRY_AFTER_S = 600


# =========================================================================== sources
@dataclass(frozen=True)
class Target:
    ra: float              # deg at epoch_mjd
    dec: float
    pmra: float = 0.0      # mas/yr (includes cos dec)
    pmdec: float = 0.0
    epoch_mjd: float = SPHEREX_REF_MJD

    def at(self, mjd: float) -> tuple[float, float]:
        return propagate(self.ra, self.dec, self.pmra, self.pmdec, self.epoch_mjd, mjd)

    def key(self) -> str:
        return f"{self.ra:.6f},{self.dec:.6f},{self.pmra:.1f},{self.pmdec:.1f},{self.epoch_mjd:.2f}"


@dataclass
class Source:
    id: str
    role: str              # target | neighbour
    ra0: float
    dec0: float
    pmra: float
    pmdec: float
    epoch0: float
    gaia_id: str | None = None
    g_mag: float | None = None
    variable: bool | None = None
    sep_arcsec: float = 0.0     # minimum separation from the target over the SPHEREx epochs

    def at(self, mjd: float) -> tuple[float, float]:
        return propagate(self.ra0, self.dec0, self.pmra, self.pmdec, self.epoch0, mjd)


def _sep_arcsec(ra1, dec1, ra2, dec2):
    ra1, dec1, ra2, dec2 = (np.radians(np.asarray(v, float)) for v in (ra1, dec1, ra2, dec2))
    s = np.sin((dec2 - dec1) / 2) ** 2 + np.cos(dec1) * np.cos(dec2) * np.sin((ra2 - ra1) / 2) ** 2
    return np.degrees(2 * np.arcsin(np.sqrt(np.clip(s, 0, 1)))) * 3600


def path_samples(mjds: list[float], n: int = 24) -> np.ndarray:
    if not mjds:
        return np.array([SPHEREX_REF_MJD])
    return np.linspace(min(mjds), max(mjds), n)


def query_radius_deg(target: Target, mjds: list[float]) -> float:
    ts = path_samples(mjds)
    ras, decs = zip(*(target.at(t) for t in ts))
    reach = float(np.max(_sep_arcsec(target.ra, target.dec, ras, decs)))
    # The target's own Gaia entry sits where it was in 2016 (92" away for Barnard's star).
    g_ra, g_dec = target.at(GAIA_DR3_EPOCH_MJD)
    back = float(_sep_arcsec(target.ra, target.dec, g_ra, g_dec))
    return (max(settings.neighbour_radius_arcsec + reach, back + SELF_MATCH_ARCSEC) + 10.0) / 3600


@dataclass
class Selection:
    sources: list[Source]
    blend_warning: bool
    blend_note: str
    notes: list[str] = field(default_factory=list)


def select_sources(target: Target, cat: CatalogResult, mjds: list[float]) -> Selection:
    """Target + Gaia neighbours within neighbour_radius of the target's SPHEREx path."""
    tgt = Source(id="target", role="target", ra0=target.ra, dec0=target.dec, pmra=target.pmra,
                 pmdec=target.pmdec, epoch0=target.epoch_mjd)
    notes: list[str] = []
    ts = path_samples(mjds)
    path = np.array([target.at(t) for t in ts])
    cands: list[tuple[float, Star]] = []
    selfs: list[tuple[float, Star]] = []
    for s in cat.stars:
        # Self-match at the catalogue epoch, where both positions are best known.
        t_ra, t_dec = target.at(s.epoch_mjd)
        if float(_sep_arcsec(t_ra, t_dec, s.ra, s.dec)) <= SELF_MATCH_ARCSEC:
            selfs.append((float(_sep_arcsec(t_ra, t_dec, s.ra, s.dec)), s))
            continue
        pos = np.array([propagate(s.ra, s.dec, s.pmra, s.pmdec, s.epoch_mjd, t) for t in ts])
        sep = float(np.min(_sep_arcsec(path[:, 0], path[:, 1], pos[:, 0], pos[:, 1])))
        if sep <= settings.neighbour_radius_arcsec:
            cands.append((sep, s))
    if selfs:
        m = min(selfs, key=lambda c: c[1].g_mag if c[1].g_mag is not None else 99)[1]
        tgt.gaia_id, tgt.g_mag, tgt.variable = m.source_id, m.g_mag, m.variable
        notes.append(f"Target matched to Gaia DR3 {m.source_id} (G={m.g_mag:.1f}); it is not fitted twice."
                     if m.g_mag is not None else f"Target matched to Gaia DR3 {m.source_id}.")
        for _, o in selfs:
            if o is not m:
                notes.append(f"Gaia {o.source_id} within {SELF_MATCH_ARCSEC:.0f}\" of the target is "
                             "unresolvable from it and not fitted.")
    if tgt.g_mag is not None:
        # Hopelessly faint next to the target (< 0.1% of its flux in G): not worth a parameter.
        rel = [c for c in cands if c[1].g_mag is not None and c[1].g_mag > tgt.g_mag + REL_FAINT_MAG]
        if rel:
            notes.append(f"{len(rel)} Gaia neighbour(s) more than {REL_FAINT_MAG:.1f} mag fainter than the "
                         "target in G not fitted.")
        cands = [c for c in cands if c not in rel]
        if tgt.g_mag < BRIGHT_TARGET_G:
            # 2-parameter Gaia sources (no PM) right next to a very bright star are mostly
            # spurious detections on its PSF wings / diffraction spikes.
            spur = [c for c in cands if not c[1].has_pm and c[0] <= SPURIOUS_RADIUS_ARCSEC]
            if spur:
                notes.append(f"{len(spur)} Gaia source(s) without proper motion within "
                             f"{SPURIOUS_RADIUS_ARCSEC:.0f}\" of this bright target treated as spurious.")
            cands = [c for c in cands if c not in spur]
    faint = [c for c in cands if c[1].g_mag is not None and c[1].g_mag > settings.neighbour_max_gmag]
    cands = [c for c in cands if c not in faint]
    # Every blend-radius neighbour first (brightest first), then the rest by brightness.
    close = sorted((c for c in cands if c[0] <= settings.blend_radius_arcsec), key=lambda c: c[1].g_mag or 99)
    far = sorted((c for c in cands if c[0] > settings.blend_radius_arcsec), key=lambda c: c[1].g_mag or 99)
    ordered = close + far
    keep, dropped = ordered[: settings.max_neighbours], ordered[settings.max_neighbours:]
    if faint:
        notes.append(f"{len(faint)} Gaia source(s) fainter than G={settings.neighbour_max_gmag} "
                     f"within {settings.neighbour_radius_arcsec:.0f}\" not fitted.")
    if dropped:
        notes.append(f"{len(dropped)} more Gaia neighbour(s) within {settings.neighbour_radius_arcsec:.0f}\" "
                     f"not fitted (cap of {settings.max_neighbours}, brightest kept).")
    sources = [tgt]
    for i, (sep, s) in enumerate(sorted(keep, key=lambda c: c[0]), start=1):
        if not s.has_pm:
            notes.append(f"n{i} (Gaia {s.source_id}) has no Gaia proper motion; assumed static.")
        sources.append(Source(id=f"n{i}", role="neighbour", ra0=s.ra, dec0=s.dec, pmra=s.pmra,
                              pmdec=s.pmdec, epoch0=s.epoch_mjd, gaia_id=s.source_id, g_mag=s.g_mag,
                              variable=s.variable, sep_arcsec=round(sep, 2)))
    blends = [s for s in sources[1:] if s.sep_arcsec <= settings.blend_radius_arcsec]
    if not cat.ok:
        note = "Catalogue unavailable, so blends with neighbours cannot be checked or removed."
    elif blends:
        b = min(blends, key=lambda s: s.sep_arcsec)
        g = f"G={b.g_mag:.1f}" if b.g_mag is not None else "no G mag"
        note = (f"{len(blends)} Gaia source(s) come within {settings.blend_radius_arcsec:.0f}\" of the target "
                f"(closest {b.sep_arcsec:.1f}\", {g}); they are fitted simultaneously, so the PSF fluxes "
                f"are deblended but the aperture fluxes are not.")
    elif len(sources) > 1:
        note = (f"No Gaia source within {settings.blend_radius_arcsec:.0f}\"; {len(sources) - 1} neighbour(s) "
                f"within {settings.neighbour_radius_arcsec:.0f}\" are fitted simultaneously.")
    else:
        note = f"No Gaia source within {settings.neighbour_radius_arcsec:.0f}\" of the target's path."
    return Selection(sources=sources, blend_warning=bool(blends), blend_note=note, notes=notes)


# =========================================================================== native cutouts
@dataclass
class NativeCutout:
    sci: np.ndarray          # MJy/sr, zodi subtracted
    var: np.ndarray
    flags: np.ndarray
    bits: dict[str, int]
    usable: np.ndarray       # not mask/suspect, finite
    wcs: WCS
    wcs_spec: WCS | None
    active_off: tuple[float, float]


def load_native(path: Path) -> NativeCutout:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", FITSFixedWarning)
        with fits.open(path) as hdul:
            hdr = hdul["IMAGE"].header
            img = hdul["IMAGE"].data.astype(np.float64)
            zodi = hdul["ZODI"].data.astype(np.float64) if "ZODI" in hdul else 0.0
            var = hdul["VARIANCE"].data.astype(np.float64)
            flags = hdul["FLAGS"].data.astype(np.int64)
            bits = flag_bits(hdul["FLAGS"].header)
            wcs = WCS(_celestial_header(hdr))
            wcs_spec = _spectral_wcs(hdr, hdul)
            off = psfmod.active_offset(hdr)
    sci = img - zodi
    bad = (_mask_for(settings.mask_flags, bits) | _mask_for(settings.suspect_flags, bits)
           | _mask_for(settings.fit_exclude_flags, bits))
    usable = ((flags & bad) == 0) & np.isfinite(sci) & np.isfinite(var) & (var > 0)
    return NativeCutout(sci=sci, var=var, flags=flags, bits=bits, usable=usable, wcs=wcs,
                        wcs_spec=wcs_spec, active_off=off)


def pixel_sr(wcs: WCS, x: float, y: float) -> float:
    """Local solid angle of one pixel (includes SIP distortion)."""
    xs = np.array([x - 0.5, x + 0.5, x, x])
    ys = np.array([y, y, y - 0.5, y + 0.5])
    ra, dec = wcs.pixel_to_world_values(xs, ys)
    cosd = math.cos(math.radians(float(dec.mean())))
    dra = ((ra + 180 - ra[0]) % 360 - 180) * cosd
    j = np.array([[dra[1] - dra[0], dra[3] - dra[2]], [dec[1] - dec[0], dec[3] - dec[2]]])
    return abs(float(np.linalg.det(j))) * (math.pi / 180) ** 2


def _num(x, nd: int = 4):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return round(x, nd) if math.isfinite(x) else None


# =========================================================================== per exposure
def measure_wings(cut: NativeCutout, lib: psfmod.PSFLibrary, ext: psfmod.ExtendedProfile,
                  sources: list[Source], pos: list[tuple[float, float]], row: dict, flags: list[str]) -> dict | None:
    """Saturated target: fit the amplitude of ePSF + empirical wing (psf.ExtendedProfile) on
    unmasked pixels out to settings.wing_fit_r_px, excluding the masked core blob dilated by
    settings.wing_rim_px, with neighbours (ePSF) and a background. The amplitude is divided by
    the calibrated wing/core ratio for this inner radius; the error includes the calibration's
    per-exposure systematic. Returns the updated row, or None when the wings cannot be fitted."""
    from scipy import ndimage

    ny, nx = cut.sci.shape
    xt, yt = pos[0]
    r_out = min(settings.wing_fit_r_px, float(ext.r[-1]))
    yy, xx = np.mgrid[0:ny, 0:nx]
    rr = np.hypot(xx - xt, yy - yt)
    bad = ~cut.usable
    lab, _ = ndimage.label(bad)
    ids = np.unique(lab[bad & (rr <= 1.5)])
    blob = np.isin(lab, ids[ids > 0]) & (rr <= r_out - 2)
    excl = ndimage.binary_dilation(blob, iterations=settings.wing_rim_px) if blob.any() else blob
    r_in = float(rr[excl].max()) if excl.any() else 0.0
    if r_in >= r_out - 3:
        return None
    region = rr <= r_out
    half = int(math.ceil(r_out))
    x0, x1 = max(0, int(round(xt)) - half), min(nx, int(round(xt)) + half + 1)
    y0, y1 = max(0, int(round(yt)) - half), min(ny, int(round(yt)) + half + 1)
    box = np.zeros((ny, nx), bool)
    box[y0:y1, x0:x1] = True
    region &= box
    r_psf = psfmod.psf_radius_px(lib.epsf[0, 0], lib.oversample)
    ox, oy = cut.active_off
    sy, sx = np.mgrid[y0:y1, x0:x1]
    t0 = np.zeros((ny, nx))
    t0[y0:y1, x0:x1] = psfmod.render_extended(lib.at(xt + ox, yt + oy), ext, xt, yt, sx, sy, lib.oversample)
    templates, fitted = [t0], [0]
    protect = np.zeros((ny, nx), bool)
    for k, (xn, yn) in enumerate(pos[1:], start=1):
        d = math.hypot(xn - xt, yn - yt)
        if d < UNRESOLVED_PX or d > r_out + r_psf:
            continue
        t = np.zeros((ny, nx))
        t[y0:y1, x0:x1] = psfmod.render(lib.at(xn + ox, yn + oy), xn, yn, sx, sy, lib.oversample)
        if (t * region).sum() >= MIN_TEMPLATE_SUM:
            templates.append(t)
            fitted.append(k)
            protect |= np.hypot(xx - xn, yy - yn) <= PROTECT_R_PX
    usable = cut.usable & ~excl
    fit = psfmod.fit_fluxes(cut.sci, cut.var, usable, region, templates, background=settings.wing_fit_background,
                            clip=RESID_CLIP, protect=protect, model_error=settings.wing_model_error)
    if fit is None or fit.n_pix < WING_MIN_PIX or not fit.flux[0] > 0:
        return None
    ratio, sys = ext.calibration(r_in)
    k_ujy = pixel_sr(cut.wcs, xt, yt) * 1e12
    f = fit.flux[0] / ratio
    stat = fit.err[0] / ratio
    err = math.hypot(stat, sys * f)
    used = fit.use if fit.use is not None else (region & usable)
    frac = float(t0[used].sum() / max(t0[region].sum(), 1e-9))
    row.update({
        "flux_ujy": _num(f * k_ujy, 3), "flux_err_ujy": _num(err * k_ujy, 3),
        "aperture_flux_ujy": None, "aperture_err_ujy": None,
        "chi2_red": _num(fit.chi2_red, 3), "n_pix": fit.n_pix, "psf_frac": round(frac, 3), "shift_px": None,
        "quality": "suspect",
        "flags": [fl for fl in flags if fl != "TOO_FEW_PIXELS"] + ["WINGS_ONLY", WING_FLAG],
        "wing_fit": {"r_in_px": round(r_in, 2), "r_out_px": round(r_out, 2), "ratio": round(ratio, 4),
                     "sys_frac": round(sys, 4), "stat_err_ujy": _num(stat * k_ujy, 3),
                     "raw_flux_ujy": _num(fit.flux[0] * k_ujy, 3)},
    })
    if "WINGS_ONLY" in flags:
        row["flags"] = list(dict.fromkeys(row["flags"]))
    for j, k in enumerate(fitted[1:], start=1):
        row["neighbours"][sources[k].id] = {"flux_ujy": _num(fit.flux[j] * k_ujy, 3),
                                            "flux_err_ujy": _num(fit.err[j] * k_ujy, 3)}
    return row


def measure_frame(cut: NativeCutout, lib: psfmod.PSFLibrary, sources: list[Source], meta: dict,
                  ext: psfmod.ExtendedProfile | None = None) -> dict:
    """Forced photometry of all sources in one exposure. Returns a contract 'points' row.
    With ``ext`` (extended PSF profile), a saturated target whose usable ePSF fraction is
    below WINGS_ONLY_FRAC is measured from its wings instead (measure_wings)."""
    mjd = float(meta["mjd"])
    ny, nx = cut.sci.shape
    pos = []
    for s in sources:
        ra, dec = s.at(mjd)
        x, y = cut.wcs.world_to_pixel_values(ra, dec)
        pos.append((float(x), float(y)))
    xt, yt = pos[0]
    row = {
        "frame_id": meta["id"], "mjd": mjd, "date": meta.get("date"), "season": meta.get("season"),
        "detector": meta["detector"], "wavelength_um": None, "bandwidth_um": None, "channel": None,
        "flux_ujy": None, "flux_err_ujy": None, "aperture_flux_ujy": None, "aperture_err_ujy": None,
        "chi2_red": None, "n_pix": 0, "flags": [], "quality": "bad", "shift_px": None, "psf_frac": None,
        "neighbours": {s.id: {"flux_ujy": None, "flux_err_ujy": None} for s in sources[1:]},
    }
    if cut.wcs_spec is not None:
        w, bw = cut.wcs_spec.pixel_to_world_values(xt, yt)
        row["wavelength_um"], row["bandwidth_um"] = _num(w, 5), _num(bw, 5)
        row["channel"] = band_index(row["wavelength_um"], meta["detector"])
    ix, iy = int(round(xt)), int(round(yt))
    if not (0 <= ix < nx and 0 <= iy < ny):
        row["flags"] = ["OFF_IMAGE"]
        return row
    flags = decode_flags(int(cut.flags[iy, ix]), cut.bits)

    # ---- core health (saturation / masked pixels at the target) -------------
    yy0, xx0 = np.mgrid[0:ny, 0:nx]
    core = np.hypot(xx0 - xt, yy0 - yt) <= 1.0
    core_bad = core & ~cut.usable
    if core_bad.any():
        core_flags: set[str] = set()
        for v in np.unique(cut.flags[core_bad]):
            core_flags |= set(decode_flags(int(v), cut.bits))
        flags.append("SATURATED_CORE" if core_flags & SATURATION_FLAGS else "MASKED_CORE")
    wings_ok = ext is not None and settings.saturated_wings and "SATURATED_CORE" in flags

    def try_wings() -> dict | None:
        return measure_wings(cut, lib, ext, sources, pos, row, flags) if wings_ok else None

    # ---- fit box: base size, grown to cover neighbour cores (and the wings of a masked core)
    half = (settings.fit_box_px - 1) // 2
    if core_bad.any():
        half = max(half, 4)
    r_psf = psfmod.psf_radius_px(lib.epsf[0, 0], lib.oversample)
    active = [0]
    for k, (xn, yn) in enumerate(pos[1:], start=1):
        if math.hypot(xn - xt, yn - yt) < UNRESOLVED_PX:
            if "UNRESOLVED_BLEND" not in flags:
                flags.append("UNRESOLVED_BLEND")  # same pixel as the target: cannot be separated
            continue
        d = max(abs(xn - xt), abs(yn - yt))
        if d - 1 <= half + r_psf:
            active.append(k)
            if d + 1 <= MAX_BOX_HALF:
                half = max(half, int(math.ceil(d + 1)))
    x0, x1 = max(0, ix - half), min(nx, ix + half + 1)
    y0, y1 = max(0, iy - half), min(ny, iy + half + 1)
    if (x1 - x0) < 2 * half + 1 or (y1 - y0) < 2 * half + 1:
        flags.append("EDGE")
    region = np.zeros((ny, nx), bool)
    region[y0:y1, x0:x1] = True
    yy, xx = np.mgrid[y0:y1, x0:x1]

    ox, oy = cut.active_off
    psfs = {k: lib.at(pos[k][0] + ox, pos[k][1] + oy) for k in active}

    def templates_at(dx: float, dy: float) -> tuple[list[np.ndarray], list[int]]:
        ts, ks = [], []
        for k in active:
            t = np.zeros((ny, nx))
            t[y0:y1, x0:x1] = psfmod.render(psfs[k], pos[k][0] + dx, pos[k][1] + dy, xx, yy, lib.oversample)
            if k == 0 or t.sum() >= MIN_TEMPLATE_SUM:
                ts.append(t)
                ks.append(k)
        return ts, ks

    templates, fitted = templates_at(0.0, 0.0)
    target_tpl = templates[0]
    if target_tpl.sum() < 0.3:
        row["flags"] = flags + ["OFF_IMAGE"]
        return row
    protect = np.zeros((ny, nx), bool)
    for k in fitted:
        protect[y0:y1, x0:x1] |= np.hypot(xx - pos[k][0], yy - pos[k][1]) <= PROTECT_R_PX

    def do_fit(ts, clip=RESID_CLIP, usable=cut.usable):
        return psfmod.fit_fluxes(cut.sci, cut.var, usable, region, ts, background=settings.fit_background,
                                 clip=clip, protect=protect, model_error=settings.psf_model_error)

    fit = do_fit(templates)
    # Information left after masking: fraction of the target's PSF on usable pixels.
    psf_frac = float(target_tpl[region & cut.usable].sum() / max(target_tpl[region].sum(), 1e-9))
    box_px = (x1 - x0) * (y1 - y0)
    if fit is None or fit.n_pix < max(10, 0.25 * box_px) or psf_frac < MIN_PSF_FRAC:
        w = try_wings()
        if w is not None:
            return w
        row["flags"] = flags + ["TOO_FEW_PIXELS"]
        row["n_pix"] = fit.n_pix if fit else int((region & cut.usable).sum())
        row["psf_frac"] = round(psf_frac, 3)
        return row

    # ---- recentring: one common sub-pixel shift for all sources, when something in the
    # box is bright enough to measure it. Relative positions come from the catalogue and
    # proper motion; the shift absorbs per-exposure WCS error (and catalogue offsets).
    shift = (0.0, 0.0)
    snr = np.abs(fit.flux) / np.where(fit.err > 0, fit.err, np.inf)
    if float(snr.max()) >= RECENTER_SNR:
        use0 = fit.use

        def cost(p) -> float:
            if abs(p[0]) > MAX_SHIFT_PX or abs(p[1]) > MAX_SHIFT_PX:
                return 1e30
            r = do_fit(templates_at(p[0], p[1])[0], clip=None, usable=use0)
            return r.chi2_red * r.dof if r is not None else 1e30

        opt = optimize.minimize(cost, [0.0, 0.0], method="Nelder-Mead",
                                options={"xatol": 0.005, "fatol": 0.01, "maxiter": 120,
                                         "initial_simplex": [[0, 0], [0.15, 0], [0, 0.15]]})
        if math.hypot(*opt.x) <= MAX_SHIFT_PX and opt.fun < cost([0.0, 0.0]):
            shift = (float(opt.x[0]), float(opt.x[1]))
            templates, fitted = templates_at(*shift)
            target_tpl = templates[0]
            fit = do_fit(templates) or fit
        row["shift_px"] = [round(shift[0], 3), round(shift[1], 3)]

    k_ujy = pixel_sr(cut.wcs, xt, yt) * 1e12
    row["flux_ujy"] = _num(fit.flux[0] * k_ujy, 3)
    row["flux_err_ujy"] = _num(fit.err[0] * k_ujy, 3)
    row["chi2_red"] = _num(fit.chi2_red, 3)
    row["n_pix"] = fit.n_pix
    for j, k in enumerate(fitted[1:], start=1):
        row["neighbours"][sources[k].id] = {"flux_ujy": _num(fit.flux[j] * k_ujy, 3),
                                            "flux_err_ujy": _num(fit.err[j] * k_ujy, 3)}

    # ---- naive aperture, aperture-corrected with the same ePSF at the same position --
    xa, ya = xt + shift[0], yt + shift[1]
    r_in, r_out = settings.annulus_px
    ap = psfmod.aperture(cut.sci, cut.var, cut.usable, xa, ya, settings.aperture_r_px, r_in, r_out)
    ap_t = psfmod.aperture(target_tpl, np.zeros_like(cut.var), cut.usable, xa, ya,
                           settings.aperture_r_px, r_in, r_out)
    if ap is not None and ap_t is not None and ap_t[0] > 0.2:
        row["aperture_flux_ujy"] = _num(ap[0] / ap_t[0] * k_ujy, 3)
        row["aperture_err_ujy"] = _num(ap[1] / ap_t[0] * k_ujy, 3)

    if psf_frac < WINGS_ONLY_FRAC:
        w = try_wings()
        if w is not None:
            return w
    if fit.chi2_red > CHI2_SUSPECT:
        flags.append("POOR_FIT")
    if psf_frac < WINGS_ONLY_FRAC:
        flags.append("WINGS_ONLY")
    row["psf_frac"] = round(psf_frac, 3)
    art = set(flags) & set(settings.artifact_flags)
    if {"SATURATED_CORE", "MASKED_CORE", "EDGE", "UNRESOLVED_BLEND", "POOR_FIT"} & set(flags) or art:
        q = "suspect"
    else:
        q = "good"
    row["flags"] = flags
    row["quality"] = q
    return row


# =========================================================================== combination
def combine(f, e, clip: float = CLIP_SIGMA) -> tuple[float, float, int] | None:
    """Inverse-variance mean with iterative outlier rejection. The error is scaled by
    sqrt(chi2_red) when the scatter exceeds the quoted errors."""
    f = np.asarray(f, float)
    e = np.asarray(e, float)
    ok = np.isfinite(f) & np.isfinite(e) & (e > 0)
    f, e = f[ok], e[ok]
    if f.size == 0:
        return None
    keep = np.ones(f.size, bool)
    for _ in range(10):
        w = 1 / e[keep] ** 2
        m = float(np.sum(w * f[keep]) / np.sum(w))
        if keep.sum() < 3:
            break
        z = (f - m) / e
        zk = z[keep]
        scale = max(1.0, 1.4826 * float(np.median(np.abs(zk - np.median(zk)))))
        worst = int(np.argmax(np.where(keep, np.abs(z), -1)))
        if abs(z[worst]) > clip * scale:
            keep[worst] = False
        else:
            break
    w = 1 / e[keep] ** 2
    m = float(np.sum(w * f[keep]) / np.sum(w))
    err = math.sqrt(1 / float(np.sum(w)))
    n = int(keep.sum())
    if n > 1:
        chi2 = float(np.sum(w * (f[keep] - m) ** 2)) / (n - 1)
        err *= math.sqrt(max(1.0, chi2))
    return m, err, n


def _usable(points: list[dict]) -> tuple[list[dict], str]:
    """Good points; fall back to good+suspect when good ones are scarce (e.g. a saturated
    star whose every exposure is 'suspect')."""
    ok = [p for p in points if p["quality"] != "bad" and p["flux_ujy"] is not None]
    good = [p for p in ok if p["quality"] == "good"]
    if good and len(good) >= 0.3 * len(ok):
        return good, "good"
    return ok, "suspect" if ok else "bad"


def _is_wing(p: dict) -> bool:
    return WING_FLAG in (p.get("flags") or [])


def build_spectrum(points: list[dict]) -> list[dict]:
    by: dict[int, list[dict]] = defaultdict(list)
    for p in points:
        if p["channel"] is not None:
            by[p["channel"]].append(p)
    out = []
    for ch in sorted(by):
        use, q = _usable(by[ch])
        c = combine([p["flux_ujy"] for p in use], [p["flux_err_ujy"] for p in use])
        if c is None:
            continue
        apts = [p for p in use if p["aperture_flux_ujy"] is not None]
        a = combine([p["aperture_flux_ujy"] for p in apts], [p["aperture_err_ujy"] for p in apts])
        seasons = {}
        for s in sorted({p["season"] for p in use}):
            sp = [p for p in use if p["season"] == s]
            cs = combine([p["flux_ujy"] for p in sp], [p["flux_err_ujy"] for p in sp])
            if cs:
                seasons[str(s)] = {"flux_ujy": _num(cs[0], 3), "flux_err_ujy": _num(cs[1], 3), "n": cs[2]}
        out.append({
            "channel": ch, "wavelength_um": _num(BAND_CENTERS_UM[ch], 4), "detector": ch // 17 + 1,
            "flux_ujy": _num(c[0], 3), "flux_err_ujy": _num(c[1], 3),
            "aperture_flux_ujy": _num(a[0], 3) if a else None,
            "aperture_err_ujy": _num(a[1], 3) if a else None,
            "n": c[2], "quality": q, "seasons": seasons,
            "wing_fit": any(_is_wing(p) for p in use), "n_wing": sum(_is_wing(p) for p in use),
        })
    return out


def build_neighbour_spectra(points: list[dict], sources: list[Source]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for s in sources[1:]:
        by: dict[int, list[tuple[float, float]]] = defaultdict(list)
        for p in points:
            nb = p["neighbours"].get(s.id) or {}
            if p["channel"] is None or p["quality"] == "bad" or nb.get("flux_ujy") is None:
                continue
            by[p["channel"]].append((nb["flux_ujy"], nb["flux_err_ujy"]))
        rows = []
        for ch in sorted(by):
            c = combine([v[0] for v in by[ch]], [v[1] for v in by[ch]])
            if c:
                rows.append({"channel": ch, "wavelength_um": _num(BAND_CENTERS_UM[ch], 4),
                             "flux_ujy": _num(c[0], 3), "flux_err_ujy": _num(c[1], 3), "n": c[2]})
        out[s.id] = rows
    return out


def _in_channel_correction(pts: list[dict]) -> dict[str, float]:
    """frame_id -> additive flux correction that moves a point from its own wavelength to
    the mean wavelength of its channel, using the piecewise-linear all-season spectrum of
    its detector. A channel is ~1/17 of a band: on a steep spectrum (T-dwarf methane
    edges) two seasons sampling the blue and red side of one channel differ by 30%."""
    out: dict[str, float] = {}
    for det in {p["detector"] for p in pts}:
        dp = [p for p in pts if p["detector"] == det and p["quality"] != "bad" and p["flux_ujy"] is not None
              and p["flux_err_ujy"]]
        nodes = []
        for ch in sorted({p["channel"] for p in dp}):
            cp = [p for p in dp if p["channel"] == ch]
            c = combine([p["flux_ujy"] for p in cp], [p["flux_err_ujy"] for p in cp])
            w = np.array([1 / p["flux_err_ujy"] ** 2 for p in cp])
            lam = float(np.sum(w * np.array([p["wavelength_um"] for p in cp])) / np.sum(w))
            # S/N from the quoted errors (not the scatter, which the slope itself inflates);
            # noisy nodes would add noise instead of removing bias.
            if c and c[0] * math.sqrt(float(np.sum(w))) >= SLOPE_NODE_SNR:
                nodes.append((ch, lam, c[0]))
        if len(nodes) < 2:
            continue
        lam_n = np.array([n[1] for n in nodes])
        f_n = np.array([n[2] for n in nodes])
        order = np.argsort(lam_n)
        lam_n, f_n = lam_n[order], f_n[order]
        mean_lam = {n[0]: n[1] for n in nodes}
        for p in dp:
            if p["channel"] in mean_lam:
                out[p["frame_id"]] = float(np.interp(p["wavelength_um"], lam_n, f_n)
                                           - np.interp(mean_lam[p["channel"]], lam_n, f_n))
    return out


def build_light_curves(points: list[dict]) -> list[dict]:
    """Per adaptive wavelength bin: one flux per season, compared at MATCHED channels.

    Within a bin the spectrum can be steep (a T dwarf changes 6x across 1.40-1.65 um),
    and seasons sample different channels, so raw season means would "vary" for a
    constant star. Instead, for every channel seen in >= 2 seasons, each season's mean
    is differenced against that channel's all-season mean; the season's light-curve
    point is bin mean + weighted mean difference. Channels seen in one season only carry
    no variability information and are left out. If no channel is shared, the raw
    season means are returned with ``matched: false`` and no chi2 test."""
    pts = [p for p in points if p["channel"] is not None and p["wavelength_um"] is not None]
    frames = [{"id": p["frame_id"], "detector": p["detector"], "season": p["season"],
               "wavelength_um": p["wavelength_um"]} for p in pts if p["quality"] != "bad"]
    bins, assign = build_bins(frames)
    by: dict[str, list[dict]] = defaultdict(list)
    for p in pts:
        if p["frame_id"] in assign:
            by[assign[p["frame_id"]]].append(p)
    shift = _in_channel_correction(pts)
    out = []
    for b in bins:
        use, q = _usable(by.get(b.id, []))
        n_wing = sum(_is_wing(p) for p in use)
        wing_frac = n_wing / len(use) if use else 0.0
        seasons_all = sorted({p["season"] for p in use})
        # per (channel, season) means, each point first moved to its channel's mean wavelength
        cs: dict[int, dict[int, tuple[float, float, int, float]]] = defaultdict(dict)
        for ch in sorted({p["channel"] for p in use}):
            for s in seasons_all:
                sp = [p for p in use if p["channel"] == ch and p["season"] == s]
                c = combine([p["flux_ujy"] - shift.get(p["frame_id"], 0.0) for p in sp],
                            [p["flux_err_ujy"] for p in sp])
                if c:
                    cs[ch][s] = (c[0], c[1], c[2], float(np.mean([p["mjd"] for p in sp])))
        shared = {ch: v for ch, v in cs.items() if len(v) >= 2}
        seasons: list[dict] = []
        chi2, dof, variable, matched = None, 0, False, bool(shared)
        if shared:
            f_ch, w_ch = [], []
            diffs: dict[int, list[tuple[float, float, int, float]]] = defaultdict(list)
            for ch, v in shared.items():
                f = np.array([x[0] for x in v.values()])
                e = np.array([x[1] for x in v.values()])
                w = 1 / e**2
                fc = float(np.sum(w * f) / np.sum(w))
                var_fc = 1 / float(np.sum(w))
                f_ch.append(fc)
                w_ch.append(1 / var_fc)
                for s, (fs, es, ns, ms) in v.items():
                    diffs[s].append((fs - fc, math.sqrt(max(es**2 - var_fc, 0.25 * es**2)), ns, ms))
            f_bin = float(np.sum(np.array(w_ch) * np.array(f_ch)) / np.sum(w_ch))
            for s in sorted(diffs):
                d = np.array([x[0] for x in diffs[s]])
                e = np.array([x[1] for x in diffs[s]])
                w = 1 / e**2
                seasons.append({"season": s, "mjd": _num(np.average([x[3] for x in diffs[s]],
                                                                     weights=[x[2] for x in diffs[s]]), 4),
                                "flux_ujy": _num(f_bin + float(np.sum(w * d) / np.sum(w)), 3),
                                "flux_err_ujy": _num(math.sqrt(1 / float(np.sum(w))), 3),
                                "n": int(sum(x[2] for x in diffs[s])),
                                "n_wing": sum(_is_wing(p) for p in use if p["season"] == s and p["channel"] in shared)})
            if len(seasons) >= 2:
                f = np.array([s["flux_ujy"] for s in seasons])
                if wing_frac >= 0.5:
                    sys_frac = LC_SYS_FRAC_WINGS
                else:
                    sys_frac = LC_SYS_FRAC if q == "good" else LC_SYS_FRAC_SUSPECT
                e = np.hypot([s["flux_err_ujy"] for s in seasons], sys_frac * abs(f_bin))
                w = 1 / e**2
                m = np.sum(w * f) / np.sum(w)
                chi2 = float(np.sum(w * (f - m) ** 2))
                dof = len(seasons) - 1
                variable = bool(sstats.chi2.sf(chi2, dof) < LC_P_VARIABLE)
        else:
            for s in seasons_all:
                sp = [p for p in use if p["season"] == s]
                c = combine([p["flux_ujy"] for p in sp], [p["flux_err_ujy"] for p in sp])
                if c:
                    seasons.append({"season": s, "mjd": _num(np.mean([p["mjd"] for p in sp]), 4),
                                    "flux_ujy": _num(c[0], 3), "flux_err_ujy": _num(c[1], 3), "n": c[2],
                                    "n_wing": sum(_is_wing(p) for p in sp)})
        out.append({"bin": b.id, "detector": b.detector, "lam_lo_um": b.lam_lo_um, "lam_hi_um": b.lam_hi_um,
                    "chi2_const": _num(chi2, 3), "dof": dof, "variable": variable, "matched": matched,
                    "quality": q, "points": seasons, "wing_fit": n_wing > 0, "n_wing": n_wing,
                    "sys_frac": (LC_SYS_FRAC_WINGS if wing_frac >= 0.5 else
                                 LC_SYS_FRAC if q == "good" else LC_SYS_FRAC_SUSPECT)})
    return out


# =========================================================================== orchestration
def frame_record(meta: dict) -> ImageRecord:
    return ImageRecord(meta["collection"], meta["obs_id"], int(meta["detector"]), meta["source_url"],
                       0.0, 0.0, 0.0, 0.0)


def run_photometry(grid: Grid, frames: list[dict], sources: list[Source],
                   progress: Callable[[int], None] | None = None) -> list[dict]:
    """Blocking: measure every frame. Frames without a PSF library or cutout are 'bad'."""
    from .fields import cutout_path

    points = []
    for i, meta in enumerate(sorted(frames, key=lambda m: (m["mjd"], m["detector"]))):
        lib = psfmod.load_library(meta["source_url"], int(meta["detector"]))
        ext = psfmod.load_extended(meta["source_url"], int(meta["detector"])) if settings.saturated_wings else None
        path = cutout_path(frame_record(meta), grid)
        try:
            if lib is None:
                raise LookupError("NO_PSF")
            if not path.exists():
                raise LookupError("NO_CUTOUT")
            row = measure_frame(load_native(path), lib, sources, meta, ext)
        except Exception as exc:  # noqa: BLE001 — one broken file must not sink the fingerprint
            tag = str(exc) if isinstance(exc, LookupError) else "ERROR"
            if not isinstance(exc, LookupError):
                log.warning("fingerprint frame %s failed: %s", meta.get("id"), exc)
            row = {"frame_id": meta["id"], "mjd": meta["mjd"], "date": meta.get("date"),
                   "season": meta.get("season"), "detector": meta["detector"],
                   "wavelength_um": meta.get("wavelength_um"), "bandwidth_um": meta.get("bandwidth_um"),
                   "channel": band_index(meta.get("wavelength_um"), meta["detector"]),
                   "flux_ujy": None, "flux_err_ujy": None, "aperture_flux_ujy": None, "aperture_err_ujy": None,
                   "chi2_red": None, "n_pix": 0, "flags": [tag], "quality": "bad", "shift_px": None,
                   "psf_frac": None,
                   "neighbours": {s.id: {"flux_ujy": None, "flux_err_ujy": None} for s in sources[1:]}}
        points.append(row)
        if progress:
            progress(i + 1)
    return points


def assemble(field_key: str, target: Target, sel: Selection, cat: CatalogResult,
             points: list[dict], extra_notes: list[str]) -> dict:
    def src(s: Source) -> dict:
        ra, dec = s.at(target.epoch_mjd)
        d = {"id": s.id, "role": s.role, "ra": round(ra, 7), "dec": round(dec, 7),
             "pmra_masyr": _num(s.pmra, 2), "pmdec_masyr": _num(s.pmdec, 2),
             "sep_arcsec": s.sep_arcsec, "gaia_id": s.gaia_id, "g_mag": _num(s.g_mag, 3)}
        if s.role == "neighbour":
            d["variable"] = s.variable
        return d

    n_bad = sum(p["quality"] == "bad" for p in points)
    n_sus = sum(p["quality"] == "suspect" for p in points)
    notes = list(sel.notes) + list(extra_notes) + [
        f"{len(points)} exposures: {len(points) - n_bad - n_sus} good, {n_sus} suspect, {n_bad} bad.",
        "PSF fluxes are total fluxes within the ePSF footprint (33x33 at 5x oversampling = 6.6 px); "
        "on isolated stars they match a r=4 px aperture to 0-2% (D1-D3) and 2-5% low (D4-D6).",
        "Aperture fluxes (r=1.5 px, sky annulus 4-7 px) are aperture-corrected with the same ePSF, "
        "so both columns estimate total flux; only the PSF fit removes neighbours.",
        "Errors are 1-sigma from the fit covariance, scaled by sqrt(chi2_red) when > 1; channel and "
        "season means are inverse-variance weighted with iterative 4-sigma rejection.",
        "Light curves compare seasons only at channels observed in >= 2 seasons (differences to each "
        "channel's mean, after moving every point to its channel's mean wavelength along the all-season "
        "spectrum), so spectral slope cannot mimic variability; chi2 vs a constant with a "
        f"{LC_SYS_FRAC:.0%} systematic floor ({LC_SYS_FRAC_SUSPECT:.0%} for bins with only suspect points), "
        f"variable when p < {LC_P_VARIABLE:g}.",
        "Saturated stars: when < 50% of the ePSF is usable, the flux comes from a calibrated extended-PSF "
        "wing fit (flag saturated_wings; see method.saturated) whose error includes the calibration "
        "systematic; without an extended profile only the unmasked wings within the 3.3 px ePSF radius are "
        "fitted (WINGS_ONLY, biased high by up to ~2x) and exposures with < 8% usable PSF are 'bad'.",
        "Per exposure, one common sub-pixel shift of all sources is fitted when any source has S/N >= 15 "
        "(shift_px; typical 0.1 px), absorbing WCS jitter; otherwise positions are fixed.",
    ]
    spectrum = build_spectrum(points)
    neighbour_spectra = build_neighbour_spectra(points, sel.sources)
    sources = [src(s) for s in sel.sources]
    pm = math.hypot(target.pmra or 0.0, target.pmdec or 0.0)
    return _clean({
        "status": "complete", "field": field_key, "version": FINGERPRINT_VERSION,
        "target": {"ra": target.ra, "dec": target.dec, "pmra_masyr": target.pmra,
                   "pmdec_masyr": target.pmdec, "epoch_mjd": target.epoch_mjd},
        "sources": sources,
        "blend_warning": sel.blend_warning, "blend_note": sel.blend_note,
        "catalog": {"name": CATALOG_NAME, "ok": cat.ok, "note": cat.note},
        "points": points,
        "spectrum": spectrum,
        "neighbour_spectra": neighbour_spectra,
        "classification": classify(spectrum, points, neighbour_spectra, sources, pm),
        "light_curves": build_light_curves(points),
        "method": {
            "psf": "SPHEREx L2 ePSF (IRSA EPSF extension, 5x oversampled, bilinear between zones), "
                   "cubic-spline sampled at each source's sub-pixel position in native detector pixels",
            "background": settings.fit_background,
            "fit_box_px": settings.fit_box_px,
            "neighbour_radius_arcsec": settings.neighbour_radius_arcsec,
            "saturated": _saturated_method(points),
            "notes": notes,
        },
        "units": {"flux": "uJy", "wavelength": "um", "time": "MJD"},
    })


def _saturated_method(points: list[dict]) -> dict:
    wf = [p["wing_fit"] for p in points if p.get("wing_fit")]
    return {
        "enabled": bool(settings.saturated_wings),
        "n_points": len(wf),
        "flag": WING_FLAG,
        "description": (
            "EXPERIMENTAL, off by default (settings.saturated_wings): absolute fluxes failed validation "
            "(Barnard's Star: ~2x jumps at detector overlaps, 2-3x dependence on the excluded rim). "
            "Saturated cores (SATURATED_CORE with < 50% of the ePSF on usable pixels) are measured from "
            "their wings: amplitude fit of ePSF + an empirical azimuthal wing profile (per detector, from "
            "bright unsaturated isolated stars in cached fields, normalised by their ePSF-fit flux) on "
            f"unmasked pixels out to {settings.wing_fit_r_px:g} px, excluding the masked core dilated by "
            f"{settings.wing_rim_px} px (non-linear rim), with neighbours and a background fitted. The "
            "amplitude is divided by the wing/core ratio measured on unsaturated stars whose cores were "
            "artificially masked to the same radius, and the per-exposure systematic measured there is "
            "added in quadrature (wing_fit.sys_frac). Points are 'suspect'; spectrum channels and "
            "light-curve bins carry wing_fit / n_wing; light-curve bins dominated by them use a "
            f"{LC_SYS_FRAC_WINGS:.0%} common systematic floor."),
        "median_sys_frac": _num(np.median([w["sys_frac"] for w in wf]), 4) if wf else None,
        "median_r_in_px": _num(np.median([w["r_in_px"] for w in wf]), 3) if wf else None,
    }


def _clean(o):
    if isinstance(o, dict):
        return {str(k): _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, (bool, np.bool_)):
        return bool(o)
    if isinstance(o, (int, np.integer)):
        return int(o)
    if isinstance(o, (float, np.floating)):
        return float(o) if math.isfinite(o) else None
    return o


def result_path(field_key: str, target: Target, sources: list[Source]) -> Path:
    ids = ",".join(f"{s.id}:{s.gaia_id}" for s in sources)
    h = hashlib.sha1(f"{target.key()}|{ids}|{FINGERPRINT_VERSION}|{settings.fit_box_px}|"
                     f"{settings.fit_background}|{settings.neighbour_radius_arcsec}".encode()).hexdigest()[:16]
    return settings.cache_dir / "fingerprints" / field_key / f"{h}.json"


@dataclass
class FingerprintJob:
    key: str
    status: str = "processing"     # processing | complete | error
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


class FingerprintManager:
    def __init__(self) -> None:
        self.jobs: dict[str, FingerprintJob] = {}
        self._psf_sem = asyncio.Semaphore(settings.psf_fetch_concurrency)
        self._cpu_sem = asyncio.Semaphore(settings.fingerprint_workers)

    async def request(self, fields, grid: Grid, target: Target) -> dict:
        fjob = fields.get_or_start(grid)
        if fjob.status == "error":
            return {"status": "error", "error": f"field build failed: {fjob.error}"}
        if fjob.status != "complete":
            stage = "stacking" if fjob.status == "stacking" else "field"
            return {"status": "processing", "stage": stage, "done": len(fjob.frames), "total": fjob.total}
        key = f"{fjob.key}|{target.key()}"
        job = self.jobs.get(key)
        if job and (job.status == "error" or (job.expires and time.time() > job.expires)):
            job = None
        if job is None:
            job = FingerprintJob(key=key)
            self.jobs[key] = job
            job.task = asyncio.create_task(self._run(job, fjob.key, grid, list(fjob.frames.values()), target))
            await asyncio.wait({job.task}, timeout=0.5)  # a cached result answers immediately
        return job.response()

    async def close(self) -> None:
        tasks = [j.task for j in self.jobs.values() if j.task and not j.task.done()]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _run(self, job: FingerprintJob, field_key: str, grid: Grid, frames: list[dict],
                   target: Target) -> None:
        try:
            frames = [f for f in frames if f.get("mjd") is not None]
            mjds = [f["mjd"] for f in frames]
            job.stage = "catalog"
            cat = await asyncio.to_thread(gaia_cone, target.ra, target.dec, query_radius_deg(target, mjds))
            sel = select_sources(target, cat, mjds)
            path = result_path(field_key, target, sel.sources)
            if cat.ok and path.exists():
                job.result = json.loads(path.read_text())
                job.status = "complete"
                return
            job.stage = "psf"
            notes = await self._ensure_psfs(job, frames)
            job.stage = "photometry"
            job.total, job.done = len(frames), 0

            def progress(n: int) -> None:
                job.done = n

            async with self._cpu_sem:
                points = await asyncio.to_thread(run_photometry, grid, frames, sel.sources, progress)
            job.stage = "combining"
            result = await asyncio.to_thread(assemble, field_key, target, sel, cat, points, notes)
            if cat.ok:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(".tmp")
                tmp.write_text(json.dumps(result))
                tmp.replace(path)
            else:
                job.expires = time.time() + RETRY_AFTER_S
            job.result = result
            job.status = "complete"
        except Exception as exc:  # noqa: BLE001
            log.exception("fingerprint %s failed", job.key)
            job.status = "error"
            job.error = f"{type(exc).__name__}: {exc}"

    async def _ensure_psfs(self, job: FingerprintJob, frames: list[dict]) -> list[str]:
        """One ePSF library per (pipeline, detector); fetch missing ones from S3."""
        need: dict[tuple[str, int], str] = {}
        for f in frames:
            k = (psfmod.pipeline_tag(f["source_url"]), int(f["detector"]))
            if k not in need and psfmod.load_library(f["source_url"], k[1]) is None:
                need[k] = f["source_url"]
        job.total, job.done = len(need), 0
        notes: list[str] = []

        async def one(k, url):
            async with self._psf_sem:
                try:
                    await asyncio.to_thread(psfmod.fetch_library, url, k[1])
                except Exception as exc:  # noqa: BLE001
                    log.warning("ePSF fetch failed for %s D%d: %s", k[0], k[1], exc)
                    notes.append(f"ePSF for {k[0]} D{k[1]} unavailable ({type(exc).__name__}); "
                                 "those exposures are marked bad.")
                job.done += 1

        await asyncio.gather(*(one(k, u) for k, u in need.items()))
        if need and len(notes) == len(need) and all(
            psfmod.load_library(f["source_url"], int(f["detector"])) is None for f in frames
        ):
            raise RuntimeError("no ePSF library could be obtained")
        return notes


# =========================================================================== extended PSF builder
EXT_CAL_RADII = (2.0, 3.0, 4.0, 5.0, 6.0)
EXT_CAL_MAX_STARS = 80        # brightest held-out stars per detector used for the calibration


def _profile_stars(cut: NativeCutout, lib: psfmod.PSFLibrary) -> list[dict]:
    """Bright, unsaturated, isolated stars of one cutout with their wing residual profiles."""
    from scipy import ndimage

    sat = (cut.flags & _mask_for(["OVERFLOW"], cut.bits)) != 0
    lab, nl = ndimage.label(sat)
    sz = ndimage.sum(sat, lab, range(1, nl + 1)) if nl else np.array([])
    sat = np.isin(lab, 1 + np.nonzero(sz >= 2)[0])
    # a saturated star's masked core (OVERFLOW/SUR_ERROR/TRANSIENT/BLOOM blob) has a halo
    # reaching far beyond it: keep profile stars well away from any big unusable blob
    lab, nl = ndimage.label(~cut.usable)
    sz = ndimage.sum(~cut.usable, lab, range(1, nl + 1)) if nl else np.array([])
    big = np.isin(lab, 1 + np.nonzero(sz >= 9)[0])
    sat |= ndimage.binary_dilation(big, iterations=13)
    cands, allp = psfmod.find_stars(cut.sci, cut.var, cut.usable, sat)
    ny, nx = cut.sci.shape
    yy, xx = np.mgrid[0:ny, 0:nx]
    ox, oy = cut.active_off
    out = []
    for (y, x) in cands:
        e = lib.at(x + ox, y + oy)
        r = psfmod.fit_star(cut.sci, cut.var, cut.usable, x, y, e, lib.oversample)
        if r is None or r[4] >= 5:
            continue
        xs, ys, flux = r[0], r[1], r[2]
        excl = np.zeros((ny, nx), bool)
        for (py, px) in allp:
            if math.hypot(px - xs, py - ys) > 2.5:
                excl |= np.hypot(xx - px, yy - py) <= 3.0
        m, me = psfmod.star_wing_profile(cut.sci, cut.var, cut.usable, xs, ys, flux, e, lib.oversample, excl)
        out.append({"x": xs, "y": ys, "flux": flux, "m": m, "me": me})
    return out


def build_extended_profiles(field_keys: list[str] | None = None, log_fn: Callable[[str], None] = print
                            ) -> dict[str, psfmod.ExtendedProfile]:
    """Blocking, offline (minutes): build the empirical extended PSF per (pipeline, detector)
    from every cached cutout of the given (default: all complete) fields, and calibrate it.

    Stars are split in two halves by a hash. Half A builds the wing profile; half B
    validates it end to end: each B star is measured normally (measure_frame), then again
    with its core artificially masked to r = 2..6 px and flagged OVERFLOW, which sends it
    through measure_wings. The median wing/core ratio and the per-exposure scatter beyond the
    statistical errors, per masked radius, are stored as the calibration. Saved to
    cache/psf/{tag}/ext{EXT_VERSION}_D{n}.npz."""
    import copy

    from .fields import cutout_path

    root = settings.cache_dir / "fields"
    keys = field_keys or [p.name for p in root.iterdir() if (p / "manifest.json").exists()]
    stars: dict[tuple[str, int], list[dict]] = defaultdict(list)
    seen: set[str] = set()       # fields of the same target share cutouts
    for key in keys:
        m = json.loads((root / key / "manifest.json").read_text())
        if m.get("status") != "complete" or not m.get("grid"):
            continue
        g = m["grid"]
        grid = Grid(ra=m["target"]["ra"], dec=m["target"]["dec"], n=g["n"], pixscale_arcsec=g["pixscale_arcsec"])
        for f in m.get("frames", []):
            det = int(f["detector"])
            lib = psfmod.load_library(f["source_url"], det)
            path = cutout_path(frame_record(f), grid)
            if lib is None or not path.exists() or str(path) in seen:
                continue
            seen.add(str(path))
            cut = load_native(path)
            for s in _profile_stars(cut, lib):
                h = int(hashlib.sha1(f"{f['id']}|{s['x']:.1f}|{s['y']:.1f}".encode()).hexdigest(), 16) % 2
                stars[(psfmod.pipeline_tag(f["source_url"]), det)].append(
                    dict(s, half=h, frame=f, path=str(path), k_ujy=pixel_sr(cut.wcs, s["x"], s["y"]) * 1e12))
        log_fn(f"field {key}: {sum(len(v) for v in stars.values())} stars so far")

    out: dict[str, psfmod.ExtendedProfile] = {}
    for (tag, det), ss in sorted(stars.items()):
        a = [s for s in ss if s["half"] == 0]
        w, we, n = psfmod.combine_profiles(np.array([s["m"] for s in a]), np.array([s["me"] for s in a]))
        r = psfmod.EXT_R_MIN + psfmod.EXT_DR * (np.arange(len(w)) + 0.5)
        ext = psfmod.ExtendedProfile(detector=det, r=r, w=np.nan_to_num(w), w_err=np.nan_to_num(we, nan=1.0),
                                     n_star=len(a), cal_r=np.array([]), cal_ratio=np.array([]),
                                     cal_sys=np.array([]))
        cal: dict[float, list[tuple[float, float, float]]] = defaultdict(list)   # r_in -> (ratio, err, r_in)
        b = sorted([s for s in ss if s["half"] == 1], key=lambda s: -s["flux"] * s["k_ujy"])[:EXT_CAL_MAX_STARS]
        for s in b:
            f = s["frame"]
            cut = load_native(Path(s["path"]))
            lib = psfmod.load_library(f["source_url"], det)
            ra, dec = map(float, cut.wcs.pixel_to_world_values(s["x"], s["y"]))
            src = Source(id="target", role="target", ra0=ra, dec0=dec, pmra=0.0, pmdec=0.0, epoch0=f["mjd"])
            base = measure_frame(cut, lib, [src], f, ext)
            if base["quality"] != "good" or not base["flux_ujy"]:
                continue
            ny, nx = cut.sci.shape
            yy, xx = np.mgrid[0:ny, 0:nx]
            rr = np.hypot(xx - s["x"], yy - s["y"])
            for R in EXT_CAL_RADII:
                c2 = copy.copy(cut)
                c2.usable = cut.usable & (rr > R)
                c2.flags = cut.flags.copy()
                c2.flags[rr <= 1.0] |= cut.bits.get("OVERFLOW", 2)
                row = measure_frame(c2, lib, [src], f, ext)
                wf = row.get("wing_fit")
                if wf and wf.get("raw_flux_ujy") is not None:
                    cal[R].append((wf["raw_flux_ujy"] / base["flux_ujy"],
                                   math.hypot(wf["stat_err_ujy"] / base["flux_ujy"],
                                              wf["raw_flux_ujy"] * base["flux_err_ujy"] / base["flux_ujy"] ** 2),
                                   wf["r_in_px"]))
        cr, cq, cs = [], [], []
        for R in EXT_CAL_RADII:
            v = cal.get(R) or []
            if len(v) < 8:
                continue
            q = np.array([x[0] for x in v])
            e = np.array([x[1] for x in v])
            med = float(np.median(q))
            mad = 1.4826 * float(np.median(np.abs(q - med)))
            stat = float(np.median(e))
            sys = math.sqrt(max(mad**2 - stat**2, 0.0))
            # a systematic below what this sample can resolve is not claimed: floor at the
            # uncertainty of the median and at 3%
            sys = max(sys, 1.2533 * mad / math.sqrt(len(q)), abs(med - 1.0) / 2, 0.03)
            cr.append(float(np.median([x[2] for x in v])))
            cq.append(med)
            cs.append(sys)
            log_fn(f"{tag} D{det} mask r={R:.0f} (r_in {cr[-1]:.1f}): n={len(q)} median wing/core={med:.3f} "
                   f"scatter={mad:.3f} median stat={stat:.3f} -> sys={sys:.3f}")
        order = np.argsort(cr)
        ext.cal_r, ext.cal_ratio, ext.cal_sys = (np.array(cr)[order], np.array(cq)[order], np.array(cs)[order])
        psfmod.save_extended(ext, psfmod.ext_path(tag, det))
        out[f"{tag}/D{det}"] = ext
    return out


if __name__ == "__main__":  # python -m app.fingerprint build-ext [field_key ...]
    import sys

    if len(sys.argv) >= 2 and sys.argv[1] == "build-ext":
        build_extended_profiles(sys.argv[2:] or None)
