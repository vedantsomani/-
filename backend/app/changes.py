"""Change detection between seasons: wavelength-matched differences, dipoles,
variables, and fast movers within a visit.

Science notes
-------------
* Differences are only ever taken between stacks of the SAME wavelength bin
  (see bins.py). Per-bin differences are then combined across the bins of a
  detector with inverse-variance weights: each term is colour-matched, so the
  sum keeps the full depth of the detector without colour residuals.
* Noise model of a difference pixel:
      var = var_A + var_B + (FRAC_FLOOR * max(|A|, |B|))**2
  The fractional floor absorbs PSF / sub-pixel registration / in-bin wavelength
  mismatch on bright stars. The resulting z-map is then rescaled by its robust
  standard deviation so thresholds are in real sigmas (confusion and resampling
  correlations make VARIANCE alone inaccurate).
* An event is a cluster of significant +/- pixels in a detector's combined
  z-map. It is then characterised in the season stacks: windowed centroids give
  the position shift (with error), aperture photometry gives the flux change.
      mover     both epochs detected, centroid shift >= 4 sigma and >= 0.15 px
      variable  both detected, flux change >= 5 sigma and >= 15%, no significant shift
      appeared / vanished   detected in only one epoch
      uncertain anything else (usually bright-star residuals)
"""

from __future__ import annotations

import math
from collections import defaultdict

import numpy as np
from astropy.stats import sigma_clipped_stats
from scipy import ndimage
from scipy.interpolate import RegularGridInterpolator
from scipy.optimize import least_squares

from .phot import aperture_flux

FRAC_FLOOR = 0.08
EDGE_PX = 3            # ignore detections this close to the grid edge (resampling edge effects)
DETECT_SIGMA = 4.5     # lobe threshold on the matched-filtered z-map
PAIR_RADIUS_PX = 3.5   # + and - lobes closer than this belong to one event
MERGE_RADIUS_PX = 1.5  # events from different detectors/pairs at the same place
SHIFT_SIGMA = 4.0
MIN_SHIFT_PX = 0.15
VAR_SIGMA = 5.0
VAR_MIN_FRAC = 0.15
CAL_FLOOR = 0.05       # 5% flux calibration / in-bin wavelength floor for variability
REG_FLOOR_PX = 0.05    # registration floor added to centroid errors
PSF_SIGMA_PX = 0.55    # approx. Gaussian sigma of the SPHEREx PSF in 6.15" pixels


# --------------------------------------------------------------------------- maps
def diff_maps(a: dict, b: dict) -> dict[str, np.ndarray]:
    """B - A for two stacks of the same wavelength bin, with the noise model above."""
    A, B = a["sci"].astype(np.float64), b["sci"].astype(np.float64)
    valid = (a["n"] > 0) & (b["n"] > 0) & np.isfinite(A) & np.isfinite(B)
    var = (
        np.nan_to_num(a["var"].astype(np.float64), nan=np.inf)
        + np.nan_to_num(b["var"].astype(np.float64), nan=np.inf)
        + (FRAC_FLOOR * np.maximum(np.abs(A), np.abs(B))) ** 2
    )
    d = np.where(valid, B - A, np.nan)
    var = np.where(valid, var, np.inf)
    return {"diff": d, "var": var}


def wavelength_balanced(As: list[dict], Bs: list[dict]):
    """Combine the per-bin stacks of two seasons into one comparable pair.

    As[i] and Bs[i] are stacks of the SAME wavelength bin in season A and season B.
    Both seasons are combined with identical per-bin weights (one scalar per bin, from
    the typical variance of both), so the pair has the same wavelength mixture to bin
    granularity while keeping nearly the full depth of the detector. A pixel is usable
    only where every bin is usable in that season; otherwise the mixture would differ
    from pixel to pixel. Returns (A, B, weights) or None.
    """
    w = []
    for a, b in zip(As, Bs):
        ok_a, ok_b = a["n"] > 0, b["n"] > 0
        if not (ok_a.any() and ok_b.any()):
            w.append(0.0)
            continue
        v = float(np.nanmedian(a["var"][ok_a]) + np.nanmedian(b["var"][ok_b]))
        w.append(1.0 / v if math.isfinite(v) and v > 0 else 0.0)
    w = np.asarray(w, float)
    if w.sum() <= 0:
        return None
    w = w / w.sum()

    def mix(parts: list[dict]) -> dict:
        valid = np.all([(p["n"] > 0) & np.isfinite(p["sci"]) for p in parts], axis=0)
        foot = np.min([p["footprint"] for p in parts], axis=0)
        sci = sum(wi * np.nan_to_num(p["sci"].astype(np.float64)) for wi, p in zip(w, parts))
        var = sum(wi**2 * np.nan_to_num(p["var"].astype(np.float64), nan=0.0, posinf=0.0) for wi, p in zip(w, parts))
        wave = sum(wi * np.nan_to_num(p["wave"].astype(np.float64)) for wi, p in zip(w, parts))
        return {
            "sci": np.where(foot > 0.5, sci, np.nan),
            "var": np.where(valid, var, np.nan),
            "n": np.where(valid, np.min([p["n"] for p in parts], axis=0), 0),
            "wave": np.where(valid, wave, np.nan),
            "footprint": foot,
        }

    return mix(As), mix(Bs), w


def combine(maps: list[dict[str, np.ndarray]]) -> dict[str, np.ndarray]:
    """Pixel-wise inverse-variance combination of several difference maps."""
    w = [np.where(np.isfinite(m["var"]) & (m["var"] > 0), 1.0 / m["var"], 0.0) for m in maps]
    wsum = np.sum(w, axis=0)
    num = np.sum([wi * np.nan_to_num(m["diff"]) for wi, m in zip(w, maps)], axis=0)
    with np.errstate(invalid="ignore", divide="ignore"):
        d = np.where(wsum > 0, num / wsum, np.nan)
        var = np.where(wsum > 0, 1.0 / wsum, np.inf)
    return {"diff": d, "var": var}


def zmap(m: dict[str, np.ndarray]) -> tuple[np.ndarray, float]:
    """Significance map, rescaled so its robust std is 1. Returns (z, scale)."""
    with np.errstate(invalid="ignore", divide="ignore"):
        z = m["diff"] / np.sqrt(m["var"])
    good = np.isfinite(z)
    k = 1.0
    if good.sum() >= 25:
        _, _, std = sigma_clipped_stats(z[good], sigma=3.0, maxiters=5)
        if math.isfinite(std) and std > 0:
            k = float(np.clip(std, 0.3, 10.0))
    return z / k, k


def local_sigma(z: np.ndarray, tile: int = 8) -> np.ndarray:
    """Per-region robust sigma of a rescaled z-map, bilinearly interpolated between
    ~tile x tile cells and floored at the global sigma.

    The global rescale of `zmap` assumes one noise level for the whole image, which is
    untrue around saturated stars (their masked rims and wings keep the local residuals
    far above the sky noise). Dividing by this map gives a z that means the same thing
    everywhere; quiet regions are unaffected because of the global floor.

    The per-cell sigma is the interquartile range / 1.349, NOT a sigma-clipped std:
    clipping would discard the very halo residuals it is meant to measure, while the
    IQR of a cell still reflects them when they cover much of the cell. A single point
    source inside an otherwise quiet cell cannot move the IQR, so real events are
    not penalised.
    """
    good = np.isfinite(z)
    g = np.nan_to_num(z)
    if good.sum() < 25:
        return np.ones_like(z)
    _, _, std = sigma_clipped_stats(g[good], sigma=3.0, maxiters=5)
    floor = float(std) if math.isfinite(std) and std > 0 else 1.0
    n_y, n_x = z.shape
    nb_y, nb_x = max(1, round(n_y / tile)), max(1, round(n_x / tile))
    ey = np.linspace(0, n_y, nb_y + 1).astype(int)
    ex = np.linspace(0, n_x, nb_x + 1).astype(int)
    mesh = np.full((nb_y, nb_x), floor)
    for i in range(nb_y):
        for j in range(nb_x):
            cell = g[ey[i]:ey[i + 1], ex[j]:ex[j + 1]][good[ey[i]:ey[i + 1], ex[j]:ex[j + 1]]]
            if cell.size >= 10:
                q25, q75 = np.percentile(cell, [25, 75])
                s = float(q75 - q25) / 1.349
                if math.isfinite(s) and s > 0:
                    mesh[i, j] = s
    mesh = np.maximum(mesh, floor)
    if nb_y == 1 and nb_x == 1:
        return mesh[0, 0] * np.ones_like(z)
    cy = (ey[:-1] + ey[1:] - 1) / 2
    cx = (ex[:-1] + ex[1:] - 1) / 2
    yy, xx = np.mgrid[0:n_y, 0:n_x]
    interp = RegularGridInterpolator((cy, cx), mesh, bounds_error=False, fill_value=None)
    return np.maximum(interp((yy, xx)), floor)


def zmap_local(z: np.ndarray) -> np.ndarray:
    """The same significance re-expressed in LOCAL sigmas (see `local_sigma`)."""
    with np.errstate(invalid="ignore", divide="ignore"):
        return z / local_sigma(z)


def matched_filter(z: np.ndarray, sigma_px: float = PSF_SIGMA_PX) -> np.ndarray:
    """PSF-matched smoothing, renormalised to unit robust std."""
    good = np.isfinite(z)
    zs = ndimage.gaussian_filter(np.where(good, z, 0.0), sigma_px)
    norm = ndimage.gaussian_filter(good.astype(float), sigma_px)
    with np.errstate(invalid="ignore", divide="ignore"):
        zs = np.where(norm > 0.5, zs / norm, np.nan)
    ok = np.isfinite(zs)
    if ok.sum() >= 25:
        _, med, std = sigma_clipped_stats(zs[ok], sigma=3.0, maxiters=5)
        if std > 0:
            zs = (zs - med) / std
    return zs


# ------------------------------------------------------------------------ events
def find_lobes(zs: np.ndarray, thresh: float = DETECT_SIGMA, edge: int = EDGE_PX) -> list[dict]:
    """Local extrema of the matched-filtered z-map beyond +/- thresh (away from the edges)."""
    lobes = []
    z0 = np.nan_to_num(zs, nan=0.0)
    n = zs.shape[0]
    inner = np.zeros_like(z0, bool)
    inner[edge : n - edge, edge : n - edge] = True
    for sign in (1, -1):
        s = sign * z0
        peaks = (s == ndimage.maximum_filter(s, size=3)) & (s >= thresh) & inner
        for y, x in zip(*np.nonzero(peaks)):
            lobes.append({"x": float(x), "y": float(y), "z": float(sign * s[y, x]), "sign": sign})
    return lobes


def group_lobes(lobes: list[dict], radius: float = PAIR_RADIUS_PX) -> list[list[dict]]:
    """Friends-of-friends grouping of lobes into events."""
    n = len(lobes)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(n):
        for j in range(i + 1, n):
            if math.hypot(lobes[i]["x"] - lobes[j]["x"], lobes[i]["y"] - lobes[j]["y"]) <= radius:
                parent[find(i)] = find(j)
    groups: dict[int, list[dict]] = defaultdict(list)
    for i in range(n):
        groups[find(i)].append(lobes[i])
    return list(groups.values())


# ------------------------------------------------------------- event vetting
# Near a bright star the difference image is not "noise + real changes": PSF wings,
# diffraction spikes that rotate with the roll angle, halo and persistence leave
# residuals far above the global noise. The global z-scale cannot know that, so each
# event is also judged against the residual level in a ring around it.
LOCAL_R_IN_PX = 3.0      # ring around the event (beyond its own PSF / dipole) ...
LOCAL_R_OUT_PX = 8.0     # ... out to here
LOCAL_EXCLUDE_PX = 2.5   # ignore ring pixels this close to any lobe of the event itself
LOCAL_MIN_PIX = 10
# A real brightness change of a source is a scaled PSF centred ON the source. Residuals
# of a static star's PSF edge (sub-pixel PSF / wavelength / registration changes) peak
# off-centre. Lobe positions are pixel peaks, so allow ~0.7 px of quantisation + slack.
CENTRE_TOL_PX = 1.0
# Proper motion does not change brightness: a "shift" with a large flux change is a
# blend / residual (one source fading next to another), not motion.
MOVER_MAX_FLUX_RATIO = 2.0
# Residuals can only be blamed on a BRIGHTER neighbour. If the event's own source is the
# brightest thing within this radius, the ring residuals are its own moving / varying
# wings (signal, not contamination) and the local test does not apply.
NEIGHBOUR_PX = 15.0


def local_significance(zs: np.ndarray, lobes: list[dict], x0: float, y0: float) -> tuple[float | None, float | None]:
    """(rms of the matched-filtered z-map in a ring around the event, event peak / max(rms, 1)).
    The event's own lobes are excluded from the ring. (None, None) if the ring is unusable."""
    n = zs.shape[0]
    yy, xx = np.mgrid[0:n, 0:n]
    r = np.hypot(xx - x0, yy - y0)
    ring = (r >= LOCAL_R_IN_PX) & (r <= LOCAL_R_OUT_PX) & np.isfinite(zs)
    for l in lobes:
        ring &= np.hypot(xx - l["x"], yy - l["y"]) > LOCAL_EXCLUDE_PX
    if ring.sum() < LOCAL_MIN_PIX:
        return None, None
    rms = float(np.sqrt(np.mean(zs[ring] ** 2)))
    peak = max(abs(l["z"]) for l in lobes)
    return rms, peak / max(rms, 1.0)


def brightest_nearby(a: dict, b: dict, x0: float, y0: float) -> bool:
    """True if the source at (x0, y0) is at least as bright (peak, either epoch) as anything
    within NEIGHBOUR_PX. Masked (saturated) pixels inside the footprint count as brightest."""
    n = a["sci"].shape[0]
    yy, xx = np.mgrid[0:n, 0:n]
    r = np.hypot(xx - x0, yy - y0)

    def peak(region: np.ndarray) -> float:
        best = -math.inf
        for s in (a, b):
            fp = s["footprint"] > 0.5 if "footprint" in s else np.ones_like(region)
            if ((s["n"] == 0) & fp & region).any():
                return math.inf
            v = s["sci"][region & np.isfinite(s["sci"])]
            if v.size:
                best = max(best, float(v.max()))
        return best

    return peak(r <= 1.5) >= peak((r > 2.0) & (r <= NEIGHBOUR_PX))


def change_offset(zs: np.ndarray, lobes: list[dict], meas: dict) -> float | None:
    """Distance (px) between where the change is (sub-pixel centroid of the strongest lobe)
    and the source (windowed centroid in the epoch where it is detected best). None if
    there is no source centroid to compare with."""
    best = max(lobes, key=lambda l: abs(l["z"]))
    n = zs.shape[0]
    yy, xx = np.mgrid[0:n, 0:n]
    sel = (np.hypot(xx - best["x"], yy - best["y"]) <= 1.5) & np.isfinite(zs)
    w = np.clip(best["sign"] * zs[sel], 0, None)
    if w.sum() <= 0:
        return None
    cx, cy = float((xx[sel] * w).sum() / w.sum()), float((yy[sel] * w).sum() / w.sum())

    def snr(e: dict) -> float:
        return (e["flux_ujy"] or 0.0) / (e["flux_err_ujy"] or 1e30) if e["flux_ujy"] is not None else -1e30

    src = max((meas["a"], meas["b"]), key=snr)
    if src["centroid"] is None:
        return None
    return math.hypot(src["centroid"][0] - cx, src["centroid"][1] - cy)


def windowed_centroid(img, var, x0, y0, sigma_w=0.8, iters=10, rmax=3.0):
    """Gaussian-windowed first moment (SExtractor-style). Returns (x, y, err) or None."""
    n = img.shape[0]
    yy, xx = np.mgrid[0:n, 0:n]
    x, y = float(x0), float(y0)
    good_all = np.isfinite(img) & np.isfinite(var)
    for _ in range(iters):
        r2 = (xx - x) ** 2 + (yy - y) ** 2
        m = good_all & (r2 <= rmax**2)
        w = np.exp(-r2 / (2 * sigma_w**2)) * m
        wi = w * np.clip(np.nan_to_num(img), 0, None)
        s = wi.sum()
        if s <= 0:
            return None
        nx, ny = (wi * xx).sum() / s, (wi * yy).sum() / s
        if math.hypot(nx - x0, ny - y0) > rmax:
            return None
        done = math.hypot(nx - x, ny - y) < 1e-3
        x, y = nx, ny
        if done:
            break
    r2 = (xx - x) ** 2 + (yy - y) ** 2
    m = good_all & (r2 <= rmax**2)
    w = np.exp(-r2 / (2 * sigma_w**2)) * m
    s = (w * np.clip(np.nan_to_num(img), 0, None)).sum()
    v = np.nan_to_num(var, nan=0.0, posinf=0.0)
    ex = math.sqrt(float(((w * (xx - x)) ** 2 * v).sum())) / s
    ey = math.sqrt(float(((w * (yy - y)) ** 2 * v).sum())) / s
    return x, y, math.hypot(ex, ey) / math.sqrt(2)


RING_WIDTH_PX = 2.0
# Very bright stars (Miras) mask out to 6-10 px in thin wavelength-bin stacks, so reach far.
RING_RADII_PX = (1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 10.0)


def ring_flux(sci, var, usable, x0, y0, pixscale, r_in):
    """Flux in the PSF wings (annulus r_in..r_in+RING_WIDTH) minus a sky annulus further out.
    Proportional to the total flux for a fixed PSF, so valid for epoch-to-epoch ratios of
    sources whose saturated cores are masked. Returns (flux, err) or None."""
    n = sci.shape[0]
    yy, xx = np.mgrid[0:n, 0:n]
    rr = np.hypot(xx - x0, yy - y0)
    ring = (rr >= r_in) & (rr < r_in + RING_WIDTH_PX)
    sky = (rr >= r_in + RING_WIDTH_PX + 1) & (rr < r_in + RING_WIDTH_PX + 4)
    good = ring & usable & np.isfinite(sci)
    if good.sum() < 0.7 * ring.sum():
        return None
    skyv = sci[sky & usable & np.isfinite(sci)]
    level = float(np.median(skyv)) if skyv.size >= 5 else 0.0
    from .phot import ujy_per_mjysr_pixel

    k = ujy_per_mjysr_pixel(pixscale) * ring.sum() / good.sum()
    return float(np.sum(sci[good] - level)) * k, math.sqrt(float(np.nansum(var[good]))) * k


FIT_BOX_PX = 6.0
FIT_FLOOR = 0.03  # fractional model error (PSF / wavelength mismatch) in the shift fit


def shift_fit(a: dict, b: dict, x0: float, y0: float, rbox: float = FIT_BOX_PX) -> dict | None:
    """Fit stack B ~= s * A(x - dx, y - dy) around (x0, y0), using only pixels that are
    usable in both epochs and not adjacent to masked (saturated / non-linear) pixels.

    For saturated stars the whole PSF pattern (wings, halo, spikes) moves with the star,
    so this measures motion AND brightness ratio where centroids/apertures fail.
    """
    n = a["sci"].shape[0]
    yy, xx = np.mgrid[0:n, 0:n]
    box = np.hypot(xx - x0, yy - y0) <= rbox
    bad = ~((a["n"] > 0) & (b["n"] > 0) & np.isfinite(a["sci"]) & np.isfinite(b["sci"]))
    bad = ndimage.binary_dilation(bad, iterations=1)  # non-linear rim around saturated cores
    use = box & ~bad
    if use.sum() < 12:
        return None
    A = np.nan_to_num(a["sci"].astype(np.float64))
    va = np.nan_to_num(a["var"].astype(np.float64), nan=0.0, posinf=0.0)
    B = b["sci"][use].astype(np.float64)
    vb = np.nan_to_num(b["var"][use].astype(np.float64), nan=0.0, posinf=0.0)
    ys, xs = yy[use].astype(float), xx[use].astype(float)

    def model(p):
        dx, dy, s = p
        return s * ndimage.map_coordinates(A, [ys - dy, xs - dx], order=3, mode="nearest")

    def resid(p):
        m = model(p)
        va_s = ndimage.map_coordinates(va, [ys - p[1], xs - p[0]], order=1, mode="nearest")
        return (B - m) / np.sqrt(p[2] ** 2 * va_s + vb + (FIT_FLOOR * np.abs(B)) ** 2 + 1e-30)

    try:
        r = least_squares(resid, x0=[0.0, 0.0, 1.0], bounds=([-3, -3, 0.05], [3, 3, 20]), x_scale=[0.3, 0.3, 0.3])
    except Exception:  # noqa: BLE001
        return None
    dof = max(len(B) - 3, 1)
    chi2 = float((r.fun**2).sum() / dof)
    try:
        cov = np.linalg.inv(r.jac.T @ r.jac) * max(chi2, 1.0)
        err = np.sqrt(np.diag(cov))
    except np.linalg.LinAlgError:
        return None
    at_bound = np.any(np.isclose(r.x, [-3, -3, 0.05])) or np.any(np.isclose(r.x, [3, 3, 20]))
    if at_bound or not np.all(np.isfinite(err)) or err[2] > 0.3 * r.x[2]:
        return None  # not constrained: nothing of the PSF wings left to fit
    return {
        "dx_px": round(float(r.x[0]), 3), "dy_px": round(float(r.x[1]), 3), "scale": round(float(r.x[2]), 4),
        "dx_err": round(float(err[0]), 4), "dy_err": round(float(err[1]), 4), "scale_err": round(float(err[2]), 4),
        "chi2_red": round(chi2, 2), "n_pix": int(use.sum()),
    }


def measure_pair(a: dict, b: dict, x0: float, y0: float, pixscale: float) -> dict:
    """Centroids and fluxes of a source in two stacks around (x0, y0).

    Uses core aperture photometry; if the core is unusable in either epoch (saturated
    and masked), both epochs are measured in the SAME PSF-wing annulus instead.
    """
    out: dict = {"aperture": "core"}
    fluxes = [aperture_flux(s["sci"], s["var"], s["n"] > 0, x0, y0, pixscale) for s in (a, b)]
    if fluxes[0] is None or fluxes[1] is None:
        fluxes = [None, None]
        for r_in in RING_RADII_PX:
            f = [ring_flux(s["sci"], s["var"], s["n"] > 0, x0, y0, pixscale, r_in) for s in (a, b)]
            if f[0] is not None and f[1] is not None:
                fluxes = f
                out["aperture"] = f"wings {r_in:g}-{r_in + RING_WIDTH_PX:g} px (saturated core)"
                out["fit"] = shift_fit(a, b, x0, y0, rbox=max(FIT_BOX_PX, r_in + RING_WIDTH_PX + 2))
                break
    for tag, s, f in (("a", a, fluxes[0]), ("b", b, fluxes[1])):
        c = windowed_centroid(s["sci"], s["var"], x0, y0)
        out[tag] = {
            "centroid": None if c is None else (round(c[0], 3), round(c[1], 3), round(c[2], 4)),
            "flux_ujy": None if f is None else round(f[0], 2),
            "flux_err_ujy": None if f is None else round(f[1], 2),
        }
    return out


def classify(m: dict) -> dict:
    """Turn a two-epoch measurement into a verdict with numbers."""
    a, b = m["a"], m["b"]
    if a["flux_ujy"] is None or b["flux_ujy"] is None:
        return {"kind": "uncertain", "reason": "insufficient usable pixels"}
    fa, fb = a["flux_ujy"], b["flux_ujy"]
    ea, eb = a["flux_err_ujy"] or 1e-9, b["flux_err_ujy"] or 1e-9
    snr_a, snr_b = fa / ea, fb / eb
    res: dict = {"snr_a": round(snr_a, 1), "snr_b": round(snr_b, 1)}
    fit = m.get("fit")
    if fit and snr_a >= 5 and snr_b >= 5:
        # saturated core: motion and brightness ratio from the PSF-pattern shift fit
        shift_px = math.hypot(fit["dx_px"], fit["dy_px"])
        err = math.sqrt(((fit["dx_err"] * fit["dx_px"]) ** 2 + (fit["dy_err"] * fit["dy_px"]) ** 2)
                        / max(shift_px, 1e-9) ** 2 + 2 * REG_FLOOR_PX**2)
        shift_sig = shift_px / err
        s, s_err = fit["scale"], math.hypot(fit["scale_err"], CAL_FLOOR * fit["scale"])
        res.update(method="shift fit (saturated core)", dx_px=fit["dx_px"], dy_px=fit["dy_px"],
                   shift_px=round(shift_px, 3), shift_sigma=round(shift_sig, 1),
                   flux_ratio=round(s, 3), dflux_sigma=round((s - 1) / s_err, 1))
        if shift_sig >= SHIFT_SIGMA and shift_px >= MIN_SHIFT_PX:
            res["kind"] = "mover"
        elif abs(s - 1) / s_err >= VAR_SIGMA and abs(s - 1) >= VAR_MIN_FRAC:
            res["kind"] = "variable"
        else:
            res["kind"] = "static"
        return res
    if snr_a >= 5 and snr_b >= 5:
        mean = (fa + fb) / 2
        sig_df = math.sqrt(ea**2 + eb**2 + (CAL_FLOOR * mean) ** 2)
        df = fb - fa
        res.update(flux_ratio=round(fb / fa, 3), dflux_sigma=round(df / sig_df, 1))
        shift_px, shift_sig = None, 0.0
        if a["centroid"] and b["centroid"]:
            dx, dy = b["centroid"][0] - a["centroid"][0], b["centroid"][1] - a["centroid"][1]
            shift_px = math.hypot(dx, dy)
            err = math.sqrt(a["centroid"][2] ** 2 + b["centroid"][2] ** 2 + 2 * REG_FLOOR_PX**2)
            shift_sig = shift_px / err
            res.update(dx_px=round(dx, 3), dy_px=round(dy, 3), shift_px=round(shift_px, 3),
                       shift_sigma=round(shift_sig, 1))
        if shift_px is not None and shift_sig >= SHIFT_SIGMA and shift_px >= MIN_SHIFT_PX:
            res["kind"] = "mover"
        elif abs(df) / sig_df >= VAR_SIGMA and abs(df) / max(abs(mean), 1e-9) >= VAR_MIN_FRAC:
            res["kind"] = "variable"
        else:
            res["kind"] = "static"
    else:
        # Detected in only one epoch. Call it appeared/vanished only if the flux CHANGE
        # is significant; otherwise the other epoch was simply too shallow.
        dsig = (fb - fa) / math.sqrt(ea**2 + eb**2)
        res["dflux_sigma"] = round(dsig, 1)
        if snr_a < 3 and snr_b >= 5 and dsig >= VAR_SIGMA:
            res["kind"] = "appeared"
        elif snr_b < 3 and snr_a >= 5 and -dsig >= VAR_SIGMA:
            res["kind"] = "vanished"
        else:
            res["kind"] = "uncertain"
            res["reason"] = "one epoch too shallow to compare"
    return res


# A real mover moves the same way in every detector and every season pair. Shifts
# caused by blends, saturation or PSF changes point in random directions.
MOVER_MIN_COHERENCE = 0.8     # mean resultant length of the shift directions
MOVER_MAX_SPEED_SCATTER = 0.5  # weighted std / mean of the speeds
INCONSISTENT = "centroid shifts disagree between detectors/seasons (blend, saturation or PSF change)"
MOVER_MIN_KEEP_FRAC = 0.8     # a coherent subset must keep this share (4 or fewer events: none dropped)
OUTLYING_PAIR = "centroid shift disagrees with the other season pairs (dropped from the motion fit)"


def motion_coherence(movers: list[dict]) -> tuple[float, float]:
    """(mean resultant length R in 0..1, fractional speed scatter) of mover events,
    weighted by shift significance."""
    w = np.array([e["shift_sigma"] for e in movers], float)
    pm = np.array([[e["pmra_masyr"], e["pmdec_masyr"]] for e in movers], float)
    speed = np.hypot(pm[:, 0], pm[:, 1])
    unit = pm / np.maximum(speed, 1e-9)[:, None]
    R = float(np.hypot(*(w[:, None] * unit).sum(0)) / w.sum())
    mean = float((w * speed).sum() / w.sum())
    scatter = float(np.sqrt((w * (speed - mean) ** 2).sum() / w.sum()) / max(mean, 1e-9))
    return R, scatter


def _coherent(R: float, scatter: float) -> bool:
    return R >= MOVER_MIN_COHERENCE and scatter <= MOVER_MAX_SPEED_SCATTER


def _demote_mover(e: dict, reason: str) -> None:
    big_change = (abs(e.get("dflux_sigma") or 0) >= VAR_SIGMA
                  and abs((e.get("flux_ratio") or 1) - 1) >= VAR_MIN_FRAC)
    e["kind"] = "variable" if big_change else "uncertain"
    e["reason"] = reason
    e.pop("pmra_masyr", None)
    e.pop("pmdec_masyr", None)


def coherent_subset(movers: list[dict]) -> tuple[list[dict], list[dict]]:
    """(kept, dropped): drop the mover event farthest from the median proper motion of the
    rest, one at a time, until the remainder is coherent, keeping at least
    max(2, MOVER_MIN_KEEP_FRAC of the events). A single outlying season pair (low S/N, a
    neighbour pulling one centroid) then no longer vetoes motion that the other pairs agree
    on. ``dropped`` is empty and ``kept`` incoherent if no such subset exists."""
    keep = list(movers)
    floor = max(2, math.ceil(MOVER_MIN_KEEP_FRAC * len(movers)))
    while not _coherent(*motion_coherence(keep)) and len(keep) > floor:
        pm = np.array([[e["pmra_masyr"], e["pmdec_masyr"]] for e in keep], float)
        dev = []
        for i in range(len(keep)):
            centre = np.median(np.delete(pm, i, axis=0), axis=0)
            dev.append(math.hypot(*(pm[i] - centre)) / max(math.hypot(*centre), 1e-9))
        keep.pop(int(np.argmax(dev)))
    if not _coherent(*motion_coherence(keep)):
        return list(movers), []
    kept_ids = {id(e) for e in keep}
    return keep, [e for e in movers if id(e) not in kept_ids]


def reconcile_movers(events: list[dict]) -> dict:
    """Check that the 'mover' events of one source agree (see `coherent_subset`). Outlying
    events of an otherwise coherent set are relabelled; if no coherent majority exists, all
    of them are (variable if the flux changed significantly, else uncertain). Mutates events."""
    movers = [e for e in events if e.get("kind") == "mover" and "pmra_masyr" in e]
    if len(movers) < 2:
        return {"n_movers": len(movers), "coherent": None}
    kept, dropped = coherent_subset(movers)
    R, scatter = motion_coherence(kept)
    ok = _coherent(R, scatter)
    if not ok:
        for e in movers:
            _demote_mover(e, INCONSISTENT)
    for e in dropped:
        _demote_mover(e, OUTLYING_PAIR)
    out = {"n_movers": len(movers), "coherent": ok, "coherence": round(R, 2), "speed_scatter": round(scatter, 2)}
    if dropped:
        out["n_dropped"] = len(dropped)
    return out


def pm_from_shift(dx_px: float, dy_px: float, dt_days: float, pixscale: float) -> tuple[float, float]:
    """Pixel shift on the north-up/east-left grid -> (pmra*cosdec, pmdec) in mas/yr."""
    yrs = dt_days / 365.25
    # +x is WEST (RA decreasing), +y is NORTH.
    return (-dx_px * pixscale * 1000 / yrs, dy_px * pixscale * 1000 / yrs)


# ------------------------------------------------------------------- fast movers
SINGLE_SIGMA = 6.0
# A detection in one exposure must be NEW flux: the residual must exceed the static
# sky (reference stack) at that spot by this factor. Removes colour/PSF residuals of
# stars (each exposure sits at a slightly different wavelength than its bin's mean).
NEW_FLUX_RATIO = 2.0
MAX_RATE_ARCSEC_PER_MIN = 20.0  # covers main-belt (~0.6"/min) to fast NEOs
TRACK_TOL_PX = 0.8
STATIONARY_PX = 0.3


def exposure_residual(frame: dict, ref: dict) -> np.ndarray:
    """z-map of one exposure minus a reference stack (same detector & wavelength bin)."""
    m = diff_maps(ref, {**frame, "n": (~frame["excluded"] & (frame["footprint"] > 0.5)).astype(np.int16)})
    z, _ = zmap(m)
    return matched_filter(z)


def new_flux_detections(frame: dict, ref: dict, thresh: float = SINGLE_SIGMA) -> list[dict]:
    """Positive residual peaks in one exposure that are not explained by the static sky."""
    zs = exposure_residual(frame, ref)
    out = []
    for lobe in find_lobes(zs, thresh):
        if lobe["sign"] < 0:
            continue
        x, y = int(lobe["x"]), int(lobe["y"])
        sl = (slice(max(y - 1, 0), y + 2), slice(max(x - 1, 0), x + 2))
        resid = float(np.nansum(frame["sci"][sl] - ref["sci"][sl]))
        static = float(np.nansum(np.clip(ref["sci"][sl], 0, None)))
        if resid > 0 and resid >= NEW_FLUX_RATIO * static:
            out.append({**lobe, "resid": resid, "static": static})
    return out


def link_visit(dets: list[dict], pixscale: float) -> list[dict]:
    """Link single-exposure detections within one visit into tracks.

    dets: dicts with t (MJD), x, y, z, frame_id, detector, obs_id.
    A detection is "confirmed" when the paired detector saw it in the same exposure
    (D1/D4, D2/D5 and D3/D6 image the same sky simultaneously).
    Accepted tracks:
      fast_mover            >= 3 epochs on a straight line, or 2 confirmed epochs
      stationary_transient  as above but moving < STATIONARY_PX over the visit
      transient             a single confirmed epoch (real, motion unknown)
    """
    max_rate_px = MAX_RATE_ARCSEC_PER_MIN / pixscale
    # collapse simultaneous twin detections into one epoch point
    epochs: list[dict] = []
    for d in sorted(dets, key=lambda d: -d["z"]):
        for e in epochs:
            if e["obs_id"] == d["obs_id"] and math.hypot(e["x"] - d["x"], e["y"] - d["y"]) <= TRACK_TOL_PX:
                if d["detector"] not in e["detectors"]:
                    e["detectors"].append(d["detector"])
                    e["members"].append(d)
                break
        else:
            epochs.append({**d, "detectors": [d["detector"]], "members": [d]})
    for e in epochs:
        e["confirmed"] = len(e["detectors"]) >= 2
    epochs.sort(key=lambda e: e["t"])

    used: set[int] = set()
    tracks = []
    for i, ei in enumerate(epochs):
        if i in used:
            continue
        best = None
        for j in range(i + 1, len(epochs)):
            ej = epochs[j]
            if j in used or ej["obs_id"] == ei["obs_id"]:
                continue
            dt_min = (ej["t"] - ei["t"]) * 1440
            if dt_min <= 0:
                continue
            vx, vy = (ej["x"] - ei["x"]) / dt_min, (ej["y"] - ei["y"]) / dt_min
            if math.hypot(vx, vy) > max_rate_px:
                continue
            members = [i, j]
            for k, ek in enumerate(epochs):
                if k in members or k in used or ek["obs_id"] in {epochs[m]["obs_id"] for m in members}:
                    continue
                tk = (ek["t"] - ei["t"]) * 1440
                if math.hypot(ek["x"] - (ei["x"] + vx * tk), ek["y"] - (ei["y"] + vy * tk)) <= TRACK_TOL_PX:
                    members.append(k)
            n_conf = sum(epochs[m]["confirmed"] for m in members)
            if len(members) >= 3 or n_conf >= 2:
                score = sum(epochs[m]["z"] for m in members)
                if best is None or score > best[0]:
                    best = (score, members, vx, vy)
        if best is not None:
            _, members, vx, vy = best
            used.update(members)
            pts = [p for m in sorted(members, key=lambda m: epochs[m]["t"]) for p in epochs[m]["members"]]
            span_min = (max(p["t"] for p in pts) - min(p["t"] for p in pts)) * 1440
            moved = math.hypot(vx, vy) * span_min
            tracks.append({
                "kind": "stationary_transient" if moved < STATIONARY_PX else "fast_mover",
                "members": pts,
                "rate": math.hypot(vx, vy) * pixscale,  # arcsec/min
                "pa": (math.degrees(math.atan2(-vx, vy)) + 360) % 360,  # east of north; +x is west
            })
        elif ei["confirmed"]:
            used.add(i)
            tracks.append({"kind": "transient", "members": ei["members"], "rate": None, "pa": None})
    return tracks
