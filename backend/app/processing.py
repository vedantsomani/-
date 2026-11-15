"""Pure (no I/O beyond reading a FITS blob) processing of one SPHEREx cutout onto
a common target grid. Runs in a worker thread."""

from __future__ import annotations

import io
import logging
import math
import warnings
from dataclasses import dataclass

import numpy as np
from astropy.convolution import Gaussian2DKernel, interpolate_replace_nans
from astropy.io import fits
from astropy.stats import sigma_clipped_stats
from astropy.time import Time
from astropy.wcs import WCS, FITSFixedWarning
from reproject import reproject_interp
from scipy import ndimage
from scipy.interpolate import RegularGridInterpolator

from .config import settings

log = logging.getLogger(__name__)

# Bump when the processing changes so stale cached frames are not reused.
PROCESSING_VERSION = "3"


@dataclass(frozen=True)
class Grid:
    """North-up, east-left TAN grid centred exactly on the target."""

    ra: float
    dec: float
    n: int                   # grid is n x n pixels; n is odd so the target is the centre pixel
    pixscale_arcsec: float

    @classmethod
    def for_fov(cls, ra: float, dec: float, fov_arcmin: float, pixscale_arcsec: float) -> "Grid":
        n = max(9, int(round(fov_arcmin * 60 / pixscale_arcsec)))
        if n % 2 == 0:
            n += 1
        return cls(ra=ra, dec=dec, n=n, pixscale_arcsec=pixscale_arcsec)

    @property
    def fov_arcmin(self) -> float:
        return self.n * self.pixscale_arcsec / 60

    @property
    def center_pix(self) -> int:
        return self.n // 2  # 0-based

    def wcs(self) -> WCS:
        w = WCS(naxis=2)
        w.wcs.ctype = ["RA---TAN", "DEC--TAN"]
        w.wcs.crval = [self.ra, self.dec]
        w.wcs.crpix = [(self.n + 1) / 2, (self.n + 1) / 2]  # 1-based FITS convention
        s = self.pixscale_arcsec / 3600
        w.wcs.cdelt = [-s, s]
        w.wcs.radesys = "ICRS"
        return w

    def cutout_size_deg(self) -> float:
        # diagonal of the grid + margin so any detector rotation still covers it
        return (self.fov_arcmin * math.sqrt(2) + 2 * settings.cutout_margin_arcmin) / 60

    def header_dict(self) -> dict:
        return {k: v for k, v in self.wcs().to_header().items()}


def flag_bits(header: fits.Header) -> dict[str, int]:
    """Flag-bit name -> mask, read from MSKN####/MSKM#### keywords."""
    out = {}
    for k in header:
        if k.startswith("MSKN"):
            mk = "MSKM" + k[4:]
            if mk in header:
                out[str(header[k])] = int(header[mk])
    return out


def _mask_for(names: list[str], bits: dict[str, int]) -> int:
    m = 0
    for n in names:
        m |= bits.get(n, 0)
    return m


def decode_flags(value: int, bits: dict[str, int]) -> list[str]:
    return [name for name, m in bits.items() if value & m]


def _celestial_header(hdr: fits.Header) -> fits.Header:
    """Primary (celestial) WCS only. Drop alternate WCSs so astropy doesn't get confused."""
    h = hdr.copy()
    for k in list(h.keys()):
        if (k.endswith("A") or k.endswith("W")) and k[:-1] in {
            "WCSAXES", "CRPIX1", "CRPIX2", "CDELT1", "CDELT2", "CUNIT1", "CUNIT2",
            "CTYPE1", "CTYPE2", "CRVAL1", "CRVAL2", "LATPOLE", "WCSNAME", "CNAME1", "CNAME2",
            "PV1_3", "PV2_3", "PS1_0", "PS2_0", "PS1_1", "PS2_1", "PS1_2", "PS2_2",
        }:
            del h[k]
    return h


def _spectral_wcs(hdr: fits.Header, hdul: fits.HDUList) -> WCS | None:
    """The 'W' alternate WCS: per-pixel wavelength & bandwidth via the WCS-WAVE lookup table."""
    if "CTYPE1W" not in hdr or "WCS-WAVE" not in hdul:
        return None
    h = hdr.copy()
    for k in list(h.keys()):  # SIP keywords make astropy emit warnings for the W WCS
        if k.split("_")[0] in {"A", "B", "AP", "BP"}:
            del h[k]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return WCS(h, fobj=hdul, key="W")


def mesh_background(img: np.ndarray, valid: np.ndarray, box: int) -> np.ndarray:
    """SExtractor-style background: sigma-clipped median in ~box x box cells,
    3x3 median-filtered, bilinearly interpolated between cell centres (constant
    beyond the outermost centres). Small grids fall back to a single constant."""
    n_y, n_x = img.shape
    nb_y, nb_x = max(1, round(n_y / box)), max(1, round(n_x / box))
    ey = np.linspace(0, n_y, nb_y + 1).astype(int)
    ex = np.linspace(0, n_x, nb_x + 1).astype(int)
    mesh = np.full((nb_y, nb_x), np.nan)
    for i in range(nb_y):
        for j in range(nb_x):
            m = valid[ey[i] : ey[i + 1], ex[j] : ex[j + 1]]
            if m.sum() >= max(10, 0.3 * m.size):
                cell = img[ey[i] : ey[i + 1], ex[j] : ex[j + 1]][m]
                _, med, _ = sigma_clipped_stats(cell, sigma=2.5, maxiters=5)
                mesh[i, j] = med
    if np.isnan(mesh).all():
        _, med, _ = sigma_clipped_stats(img[valid], sigma=2.5, maxiters=5)
        return np.full(img.shape, med)
    mesh = np.where(np.isnan(mesh), np.nanmedian(mesh), mesh)
    if nb_y >= 3 and nb_x >= 3:
        mesh = ndimage.median_filter(mesh, size=3, mode="nearest")
    if nb_y == 1 or nb_x == 1:
        return np.full(img.shape, float(np.median(mesh)))
    cy = (ey[:-1] + ey[1:] - 1) / 2
    cx = (ex[:-1] + ex[1:] - 1) / 2
    interp = RegularGridInterpolator((cy, cx), mesh)
    yy, xx = np.mgrid[0:n_y, 0:n_x]
    return interp((np.clip(yy, cy[0], cy[-1]), np.clip(xx, cx[0], cx[-1])))


def strip_cutout(raw: bytes, access_url: str | None = None, detector: int | None = None) -> bytes:
    """Drop the (large, ~4 MB) EPSF table from an IRSA cutout before caching it.
    With ``access_url``/``detector``, first save the ePSF library for that detector
    (one per pipeline version; it is identical for every exposure, see psf.py)."""
    with fits.open(io.BytesIO(raw)) as hdul:
        if access_url and detector:
            try:
                from .psf import capture_from_hdul

                capture_from_hdul(hdul, access_url, int(detector))
            except Exception as exc:  # noqa: BLE001 — never fail a download over the PSF
                log.warning("could not save ePSF from cutout: %s", exc)
        keep = fits.HDUList([hdu for hdu in hdul if hdu.name != "EPSF"])
        buf = io.BytesIO()
        keep.writeto(buf)
        return buf.getvalue()


def process_cutout(fits_bytes: bytes, grid: Grid) -> tuple[dict[str, np.ndarray], dict]:
    """Zodi-subtract, mask bad pixels and reproject one cutout onto ``grid``.

    Returns (arrays, metadata). Arrays: sci (MJy/sr, background-subtracted; dead
    pixels interpolated over), var, flags (int32), footprint (0..1), excluded
    (bool: don't use for photometry), wave/bandwidth maps (um).
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", FITSFixedWarning)
        hdul = fits.open(io.BytesIO(fits_bytes))
        img_hdu = hdul["IMAGE"]
        hdr = img_hdu.header
        image = img_hdu.data.astype(np.float64)
        zodi = hdul["ZODI"].data.astype(np.float64) if "ZODI" in hdul else 0.0
        var = hdul["VARIANCE"].data.astype(np.float64)
        flags = hdul["FLAGS"].data.astype(np.int64)
        bits = flag_bits(hdul["FLAGS"].header)

        wcs_in = WCS(_celestial_header(hdr))
        wcs_spec = _spectral_wcs(hdr, hdul)

    mask_bits = _mask_for(settings.mask_flags, bits)
    suspect_bits = _mask_for(settings.suspect_flags, bits)

    sci = image - zodi
    # Pixels excluded from photometry: masked + suspect (+ anything already NaN).
    excluded = ((flags & (mask_bits | suspect_bits)) != 0) | ~np.isfinite(sci)
    # For display, interpolate over dead pixels so bilinear resampling doesn't
    # grow each one into a 2x2 hole. Suspect (OUTLIER) pixels are kept as-is:
    # they may be the moving/variable source we are looking for.
    sci[((flags & mask_bits) != 0) | ~np.isfinite(sci)] = np.nan
    if np.isnan(sci).any():
        with warnings.catch_warnings():  # large masked areas (saturated stars) are expected
            warnings.simplefilter("ignore")
            sci = interpolate_replace_nans(sci, Gaussian2DKernel(1.0))
    var = np.where(np.isfinite(var), var, np.nanmedian(var))

    wcs_out = grid.wcs()
    shape = (grid.n, grid.n)
    sci_r, footprint = reproject_interp((sci, wcs_in), wcs_out, shape_out=shape, order="bilinear")
    var_r, _ = reproject_interp((var, wcs_in), wcs_out, shape_out=shape, order="bilinear")
    excl_r, _ = reproject_interp(
        (excluded.astype(np.float64), wcs_in), wcs_out, shape_out=shape, order="bilinear"
    )
    excl_r = np.nan_to_num(excl_r, nan=1.0) > 0.25
    flags_r, _ = reproject_interp(
        (flags.astype(np.float64), wcs_in), wcs_out, shape_out=shape, order="nearest-neighbor"
    )
    flags_r = np.nan_to_num(flags_r, nan=0).astype(np.int64)

    # Per-output-pixel wavelength: grid pixel -> sky -> input pixel -> W WCS.
    yy, xx = np.mgrid[0 : grid.n, 0 : grid.n]
    sky_ra, sky_dec = wcs_out.pixel_to_world_values(xx, yy)
    in_x, in_y = wcs_in.world_to_pixel_values(sky_ra, sky_dec)
    if wcs_spec is not None:
        wave, bw = wcs_spec.pixel_to_world_values(in_x, in_y)
        wave = np.asarray(wave, dtype=np.float32)
        bw = np.asarray(bw, dtype=np.float32)
    else:
        wave = np.full(shape, np.nan, np.float32)
        bw = np.full(shape, np.nan, np.float32)

    covered = footprint > 0.5
    finite = covered & np.isfinite(sci_r) & ~excl_r
    if finite.sum() >= 10:
        # Background SURFACE, not a constant: some exposures carry scattered-light
        # gradients that a constant leaves behind (and that then survive stacking).
        bg_map = mesh_background(sci_r, finite, settings.background_box_px)
        sci_r = sci_r - bg_map
        bg = float(np.nanmedian(bg_map))
        _, _, noise = sigma_clipped_stats(sci_r[finite], sigma=3.0, maxiters=5)
    else:
        bg, noise = 0.0, float("nan")

    c = grid.center_pix
    # Flags at the target, read from the native-resolution pixel (not the reprojected one)
    tx, ty = wcs_in.world_to_pixel_values(grid.ra, grid.dec)
    ix, iy = int(round(float(tx))), int(round(float(ty)))
    if 0 <= iy < flags.shape[0] and 0 <= ix < flags.shape[1]:
        target_flags = decode_flags(int(flags[iy, ix]), bits)
    else:
        target_flags = ["OFF_IMAGE"]
    if "OFF_IMAGE" in target_flags or set(target_flags) & set(settings.mask_flags):
        target_quality = "bad"
    elif set(target_flags) & (set(settings.suspect_flags) | set(settings.artifact_flags)):
        target_quality = "suspect"
    else:
        target_quality = "good"

    n_cov = max(int(covered.sum()), 1)
    artifact_frac = {
        name: round(float(((flags_r & bits[name]) != 0)[covered].sum()) / n_cov, 4)
        for name in settings.artifact_flags
        if name in bits and ((flags_r & bits[name]) != 0)[covered].any()
    }

    mjd = float(hdr.get("MJD-AVG", hdr.get("MJD-OBS", float("nan"))))
    meta = {
        "mjd": mjd,
        "date": Time(mjd, format="mjd").isot[:19] if math.isfinite(mjd) else None,
        "exptime_s": hdr.get("XPOSURE"),
        "wavelength_um": _finite_or_none(wave[c, c]),
        "bandwidth_um": _finite_or_none(bw[c, c]),
        "coverage": round(float(covered.mean()), 4),
        "excluded_frac": round(float(excl_r[covered].sum()) / n_cov, 4),
        "artifact_frac": artifact_frac,
        "target_flags": target_flags,
        "target_quality": target_quality,
        "background_mjysr": round(float(bg), 6),
        "noise_mjysr": _finite_or_none(noise),
        "zodi_mjysr": _finite_or_none(np.nanmedian(zodi)) if np.ndim(zodi) else None,
        "psf_fwhm": hdr.get("PSF_FWHM"),
        "l2_qa": hdr.get("L2DQAFLG"),
        "flag_bits": bits,
    }
    arrays = {
        "sci": sci_r.astype(np.float32),
        "var": var_r.astype(np.float32),
        "flags": flags_r.astype(np.int32),
        "footprint": footprint.astype(np.float32),
        "excluded": excl_r,
        "wave": wave,
        "bandwidth": bw,
    }
    return arrays, meta


def _finite_or_none(x) -> float | None:
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return round(x, 6) if math.isfinite(x) else None
