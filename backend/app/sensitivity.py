"""Injection-recovery test: how faint and how slow a mover the pipeline detects.

    python -m app.sensitivity [--field RA DEC] [--trials N] [--out docs/sensitivity.md]

Synthetic point sources are added to in-memory copies of every cached exposure of a quiet
field (``sci`` only; ``var``, flags and masks are untouched and no noise is added: the real
exposures supply the noise). ``analysis.run`` then runs UNCHANGED on those arrays: its frame
loader and its stack/diff writer are redirected to memory (``in_memory``), so nothing is read
from or written to the field's cached stacks/diffs. Only the fast-mover stage (per-exposure
transient search, which does not feed the candidate list) is skipped for speed.

Per trial ONE source is injected at a random position away from detected / Gaia sources,
the field centre (the target verdict would take it over) and the edges, moving linearly
(no parallax) with a chosen proper motion in a random direction. Recovery = a candidate
within ``MATCH_PX`` of the injected mean position. Pixel convention of the field grid:
0-based, +x WEST (RA decreasing), +y NORTH.
"""

from __future__ import annotations

import argparse
import glob
import json
import math
import os
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
from scipy.special import erf, expit

from . import analysis
from .phot import ujy_per_mjysr_pixel
from .processing import Grid
from .store import stack_path

FIELD = (150.11916, 2.20583)       # quiet COSMOS control field (fov 5', native pixels)
FOV_ARCMIN, PIX_ARCSEC = 5.0, 6.15
MOTIONS = (0.0, 0.5, 1.0, 2.0, 4.0, 8.0, 16.0)   # arcsec / yr
FLUXES = (10.0, 16.0, 25.0, 40.0, 63.0, 100.0, 160.0, 250.0, 400.0, 630.0, 1000.0, 1600.0, 2500.0)  # uJy, x10^0.2 (see SHAPES for the normalisation)
SHAPES = ("flat", "ydwarf", "tdwarf")
MATCH_PX = 1.5
EDGE_MARGIN_PX = 6.0
CENTRE_EXCL_PX = 7.0
CAND_EXCL_PX = 4.0
DAYS_PER_YEAR = 365.25

# "T dwarf-like": F_nu relative to the quoted flux, piecewise in wavelength (um); 0.05 elsewhere.
T_SEGMENTS = ((1.0, 1.3, 1.0), (1.55, 1.70, 0.8), (2.05, 2.20, 0.5), (4.0, 5.0, 1.0))
T_ELSEWHERE = 0.05
SHAPE_TEXT = {
    "flat": "flat in F_nu: the quoted flux at every wavelength",
    "ydwarf": "Y dwarf-like: the quoted flux in detectors D5 and D6 (about 3.8-5.0 um), zero in D1-D4",
    "tdwarf": ("T dwarf-like: the quoted flux at 1.0-1.3 and 4.0-5.0 um, 0.8x at 1.55-1.70 um, "
               "0.5x at 2.05-2.20 um, 0.05x elsewhere"),
}


# ----------------------------------------------------------------------------- injection model
@dataclass
class Injection:
    x0: float                 # position at t_ref (= mean exposure epoch), px
    y0: float
    mu: float                 # total proper motion, arcsec/yr
    pa_deg: float             # direction of motion, degrees east of north
    flux_ujy: float
    shape: str = "flat"
    t_ref: float = 0.0        # MJD
    season_factor: dict[int, float] = field(default_factory=dict)  # variable: flux x factor per season
    kind: str = "motion"      # "motion" | "variable"

    @property
    def pm_arcsec(self) -> tuple[float, float]:
        """(pmra*cos(dec), pmdec) in arcsec/yr."""
        a = math.radians(self.pa_deg)
        return self.mu * math.sin(a), self.mu * math.cos(a)

    def position(self, mjd: float, pixscale: float) -> tuple[float, float]:
        pmra, pmdec = self.pm_arcsec
        dt = (mjd - self.t_ref) / DAYS_PER_YEAR
        return self.x0 - pmra * dt / pixscale, self.y0 + pmdec * dt / pixscale  # +x is WEST


def shape_factor(shape: str, wave_um: float, detector: int) -> float:
    if shape == "flat":
        return 1.0
    if shape == "ydwarf":
        return 1.0 if detector in (5, 6) else 0.0
    if shape == "tdwarf":
        for lo, hi, f in T_SEGMENTS:
            if lo <= wave_um <= hi:
                return f
        return T_ELSEWHERE
    raise ValueError(shape)


def gaussian_stamp(n: int, x: float, y: float, sigma: float) -> np.ndarray:
    """Pixel-integrated circular Gaussian (sum 1 over an infinite grid) on an n x n grid."""
    edges = np.arange(n + 1) - 0.5
    s = sigma * math.sqrt(2)
    gx = np.diff(0.5 * erf((edges - x) / s))
    gy = np.diff(0.5 * erf((edges - y) / s))
    return np.outer(gy, gx)


def inject(frames: list[dict], base: dict[str, dict], inj: Injection, sigma_by_det: dict[int, float],
           pixscale: float) -> dict[str, dict]:
    """Copies of the frames' arrays with the source added to ``sci`` (MJy/sr)."""
    k = ujy_per_mjysr_pixel(pixscale)
    out = {}
    for f in frames:
        a = base[f["id"]]
        n = a["sci"].shape[0]
        x, y = inj.position(f["mjd"], pixscale)
        ix, iy = int(round(x)), int(round(y))
        wave = float("nan")
        if 0 <= ix < n and 0 <= iy < n:
            wave = float(a["wave"][iy, ix])
        if not math.isfinite(wave):
            wave = float(f.get("wavelength_um") or 0.0)
        amp = inj.flux_ujy * shape_factor(inj.shape, wave, f["detector"]) * inj.season_factor.get(f["season"], 1.0)
        b = dict(a)
        if amp:
            sigma = sigma_by_det.get(f["detector"], 1.0)
            b["sci"] = (a["sci"] + (amp / k) * gaussian_stamp(n, x, y, sigma)).astype(a["sci"].dtype)
        out[f["id"]] = b
    return out


# ----------------------------------------------------------------------------- real analysis in memory
@contextmanager
def in_memory(frame_store: dict[str, dict], skip_fast: bool = True):
    """Redirect analysis.run's frame loads to ``frame_store`` and its stack/diff writes and
    reads to a private dict. Nothing touches the on-disk cache."""
    mem: dict[str, dict] = {}
    saved = {n: getattr(analysis, n) for n in ("frame_arrays", "save_arrays", "stack_arrays", "_fast_movers")}
    analysis.frame_arrays = lambda key, fid: frame_store[fid]
    analysis.save_arrays = lambda path, arrays: mem.__setitem__(str(path), arrays)
    analysis.stack_arrays = lambda key, sid: mem[str(stack_path(key, sid))]
    if skip_fast:
        analysis._fast_movers = lambda *a, **k: []
    try:
        yield mem
    finally:
        for n, v in saved.items():
            setattr(analysis, n, v)


def run_analysis(key: str, grid: Grid, frames: list[dict], frame_store: dict[str, dict]) -> dict:
    with in_memory(frame_store):
        _, result = analysis.run(key, grid, frames)
    return result


def score(result: dict, inj: Injection) -> dict:
    """Best-ranked candidate within MATCH_PX of the injected mean position."""
    cands = result["candidates"]
    near = [c for c in cands if math.hypot(c["x"] - inj.x0, c["y"] - inj.y0) <= MATCH_PX]
    pmra, pmdec = inj.pm_arcsec
    rec = {"shape": inj.shape, "mu": inj.mu, "flux": inj.flux_ujy, "kind_inj": inj.kind,
           "pa": round(inj.pa_deg, 1), "x0": round(inj.x0, 2), "y0": round(inj.y0, 2),
           "pmra_inj": round(pmra * 1000), "pmdec_inj": round(pmdec * 1000),
           "n_cands": len(cands), "matched": bool(near)}
    within3 = [c for c in cands if math.hypot(c["x"] - inj.x0, c["y"] - inj.y0) <= 3.0]
    rec["kind3"] = within3[0]["kind"] if within3 else None
    if near:
        c = near[0]
        rec.update(kind=c["kind"], evidence=c["evidence"], max_z=c["max_z"],
                   dist=round(math.hypot(c["x"] - inj.x0, c["y"] - inj.y0), 2),
                   n_independent=c.get("n_independent"), detectors=c.get("detectors"),
                   reason=c.get("reason"), coherent=(c.get("motion_check") or {}).get("coherent"),
                   pmra_rec=c.get("pmra_masyr"), pmdec_rec=c.get("pmdec_masyr"),
                   event_kinds=sorted({e["kind"] for e in c.get("events", [])}))
    return rec


# ----------------------------------------------------------------------------- field context
def load_field(ra: float, dec: float) -> tuple[str, Grid, list[dict], dict[str, dict]]:
    from .fields import FieldJob, field_key
    from .store import frame_arrays

    grid = Grid.for_fov(ra, dec, FOV_ARCMIN, PIX_ARCSEC)
    key = field_key(grid)
    job = FieldJob.load(key, grid)
    if job is None:
        raise SystemExit(f"field {ra} {dec} is not cached and complete (key {key}); build it first")
    frames = sorted(job.frames.values(), key=lambda f: f["id"])
    base = {f["id"]: frame_arrays(key, f["id"]) for f in frames}
    return key, grid, frames, base


def _fit_gauss(cut: np.ndarray, ok: np.ndarray, x0: float, y0: float):
    from scipy.optimize import curve_fit

    yy, xx = np.nonzero(ok)
    if len(xx) < 15:
        return None

    def model(p, A, xc, yc, s, c):
        return A * np.exp(-((p[0] - xc) ** 2 + (p[1] - yc) ** 2) / (2 * s * s)) + c

    try:
        p, _ = curve_fit(model, (xx, yy), cut[yy, xx], p0=(cut[int(y0), int(x0)], x0, y0, 0.8, 0.0), maxfev=2000)
    except Exception:  # noqa: BLE001 — non-converged fit: skip this star/frame
        return None
    return p


def measure_psf(key: str, grid: Grid, frames: list[dict], base: dict[str, dict]) -> dict:
    """Gaussian sigma (px) of isolated, unsaturated stars in individual reprojected exposures,
    per detector. Stars are found in the cached season x detector stacks (read only)."""
    from scipy import ndimage

    from .store import stack_arrays

    n, half = grid.n, 3
    out: dict = {}
    for det in range(1, 7):
        fr = [f for f in frames if f["detector"] == det]
        sids = [f"s{s}_d{det}" for s in sorted({f["season"] for f in fr})]
        try:
            st = max((stack_arrays(key, s) for s in sids), key=lambda a: int(a["n"].sum()))
        except FileNotFoundError:
            continue
        sci = np.nan_to_num(st["sci"])
        noise = 1.4826 * np.median(np.abs(sci - np.median(sci)))
        z = (sci - np.median(sci)) / max(noise, 1e-12)
        peaks = (z == ndimage.maximum_filter(z, 9)) & (z > 30)
        stars = []
        for y, x in zip(*np.nonzero(peaks)):
            if not (half + 1 <= x < n - half - 1 and half + 1 <= y < n - half - 1):
                continue
            box = (slice(y - 6, y + 7), slice(x - 6, x + 7))
            zz = z[box].copy()
            zz[6 - 2:6 + 3, 6 - 2:6 + 3] = 0
            if zz.max() > 0.1 * z[y, x] or (st["n"][y - half:y + half + 1, x - half:x + half + 1] == 0).any():
                continue
            stars.append((x, y))
        sig = []
        for f in fr:
            a = base[f["id"]]
            ok_all = (a["footprint"] > 0.5) & ~a["excluded"].astype(bool) & np.isfinite(a["sci"])
            for x, y in stars:
                box = (slice(y - half, y + half + 1), slice(x - half, x + half + 1))
                cut, ok = a["sci"][box].astype(float), ok_all[box]
                p = _fit_gauss(cut, ok, half, half)
                if p is None:
                    continue
                snr = p[0] / max(math.sqrt(float(np.median(a["var"][box]))), 1e-12)
                if snr > 20 and 0.3 < abs(p[3]) < 3 and math.hypot(p[1] - half, p[2] - half) < 1.5:
                    sig.append(abs(p[3]))
        if sig:
            q = np.percentile(sig, [25, 50, 75])
            out[det] = {"sigma_px": round(float(q[1]), 3), "q25": round(float(q[0]), 3),
                        "q75": round(float(q[2]), 3), "n_fits": len(sig), "n_stars": len(stars)}
    return out


def allowed_mask(key: str, grid: Grid, frames: list[dict], base_cands: list[dict], margin: float) -> np.ndarray:
    """Pixels where a source may be injected: off detected and Gaia sources, the field centre,
    existing candidates and the edges."""
    from scipy import ndimage

    from .store import stack_arrays

    n = grid.n
    src = np.zeros((n, n), bool)
    for s in sorted({(f["season"], f["detector"]) for f in frames}):
        try:
            a = stack_arrays(key, f"s{s[0]}_d{s[1]}")
        except FileNotFoundError:
            continue
        sci = np.nan_to_num(a["sci"])
        noise = 1.4826 * np.median(np.abs(sci - np.median(sci)))
        src |= (sci - np.median(sci)) > 5 * noise
        src |= (a["n"] == 0) & (a["footprint"] > 0.5)
    src = ndimage.binary_dilation(src, iterations=3)
    yy, xx = np.mgrid[0:n, 0:n]
    w = grid.wcs()
    for st in gaia_stars(grid):
        x, y = (float(v) for v in w.world_to_pixel_values(st["ra"], st["dec"]))
        g = st.get("g_mag") or 21.0
        r = 4.0 if g < 17 else 2.5
        src |= np.hypot(xx - x, yy - y) <= r
    c = grid.center_pix
    ok = ~src & (np.hypot(xx - c, yy - c) > CENTRE_EXCL_PX)
    for cd in base_cands:
        ok &= np.hypot(xx - cd["x"], yy - cd["y"]) > CAND_EXCL_PX
    ok &= (xx >= margin) & (xx <= n - 1 - margin) & (yy >= margin) & (yy <= n - 1 - margin)
    return ok


def gaia_stars(grid: Grid) -> list[dict]:
    """Gaia DR3 stars inside the grid from the on-disk catalogue cache (read only; no query)."""
    from .config import settings

    half = grid.n * grid.pixscale_arcsec / 3600 / 2
    stars: dict[str, dict] = {}
    for p in glob.glob(str(settings.cache_dir / "catalogs" / "gaia_dr3" / "*.json")):
        try:
            data = json.loads(Path(p).read_text())
        except Exception:  # noqa: BLE001
            continue
        for s in data.get("stars", []):
            if abs(s["dec"] - grid.dec) < half * 1.2 and abs((s["ra"] - grid.ra) * math.cos(math.radians(grid.dec))) < half * 1.2:
                stars[s["source_id"]] = s
    return list(stars.values())


# ----------------------------------------------------------------------------- worker processes
_CTX: dict = {}


def _init_worker(ra: float, dec: float, sigma_by_det: dict[int, float]) -> None:
    import logging

    logging.getLogger("app").setLevel(logging.WARNING)
    key, grid, frames, base = load_field(ra, dec)
    _CTX.update(key=key, grid=grid, frames=frames, base=base, sigma=sigma_by_det)


def _trial(inj_d: dict) -> dict:
    inj = Injection(**inj_d)
    c = _CTX
    store = inject(c["frames"], c["base"], inj, c["sigma"], c["grid"].pixscale_arcsec)
    try:
        res = run_analysis(c["key"], c["grid"], c["frames"], store)
    except Exception as exc:  # noqa: BLE001 — record, don't kill the run
        return {**asdict(inj), "error": repr(exc)}
    return score(res, inj)


def plan_trials(frames: list[dict], allowed: np.ndarray, trials: int, var_trials: int, fluxes, motions,
                shapes, rng: np.random.Generator) -> list[dict]:
    t_ref = float(np.mean([f["mjd"] for f in frames]))
    ys, xs = np.nonzero(allowed)
    out = []

    def pos():
        i = rng.integers(len(xs))
        return float(xs[i] + rng.uniform(-0.5, 0.5)), float(ys[i] + rng.uniform(-0.5, 0.5))

    for shape in shapes:
        for mu in motions:
            for fl in fluxes:
                for _ in range(trials):
                    x, y = pos()
                    out.append(asdict(Injection(x, y, mu, float(rng.uniform(0, 360)), fl, shape, t_ref)))
    for fl in fluxes:
        for _ in range(var_trials):
            x, y = pos()
            out.append(asdict(Injection(x, y, 0.0, 0.0, fl, "flat", t_ref, {1: 2.0}, "variable")))
    return out


# ----------------------------------------------------------------------------- statistics
def fit_logistic(logf: np.ndarray, y: np.ndarray) -> tuple[float, float] | None:
    from scipy.optimize import minimize

    if y.sum() == 0 or y.sum() == len(y):
        return None

    def nll(p):
        a, lb = p
        b = math.exp(lb)
        q = expit((logf - a) / b)
        q = np.clip(q, 1e-6, 1 - 1e-6)
        return -float(np.sum(y * np.log(q) + (1 - y) * np.log(1 - q)))

    best = min((minimize(nll, (a0, math.log(0.15)), method="Nelder-Mead") for a0 in np.percentile(logf, [25, 50, 75])),
               key=lambda r: r.fun)
    a, lb = best.x
    return float(a), float(min(max(math.exp(lb), 0.01), 3.0))


def completeness(fl: np.ndarray, y: np.ndarray, rng: np.random.Generator, n_boot: int = 200) -> dict:
    """50% / 90% completeness flux from a logistic fit in log flux, with a bootstrap over trials
    (resampled within each flux level, i.e. binomial uncertainty) for the 16-84% range."""
    lf = np.log10(fl)
    lo, hi = float(fl.min()), float(fl.max())

    def levels(lf_, y_):
        p = fit_logistic(lf_, y_)
        if p is None:
            v = math.inf if y_.sum() == 0 else -math.inf
            return v, v
        a, b = p
        return a, a + b * math.log(9)

    a50, a90 = levels(lf, y)
    fit = fit_logistic(lf, y)
    grid_f = np.unique(fl)
    boots = []
    idx_by = [np.nonzero(lf == v)[0] for v in np.unique(lf)]
    for _ in range(n_boot):
        ii = np.concatenate([rng.choice(ix, len(ix)) for ix in idx_by])
        boots.append(levels(lf[ii], y[ii]))
    boots = np.array(boots)
    out = {}
    for name, val, col in (("f50", a50, 0), ("f90", a90, 1)):
        b = boots[:, col]
        b = b[np.isfinite(b)]
        out[name] = {"value": 10 ** val if math.isfinite(val) else val,
                     "lo": 10 ** float(np.percentile(b, 16)) if len(b) else None,
                     "hi": 10 ** float(np.percentile(b, 84)) if len(b) else None,
                     "range": (lo, hi), "bracket": None}
        # the whole transition lies between two adjacent tested fluxes: the fit cannot place it
        if fit is not None and fit[1] < 0.03 and math.isfinite(val):
            below = grid_f[grid_f <= 10 ** val]
            above = grid_f[grid_f >= 10 ** val]
            if len(below) and len(above):
                out[name]["bracket"] = (float(below.max()), float(above.min()))
    return out


def fmt_level(d: dict) -> str:
    v, (lo, hi) = d["value"], d["range"]
    if not math.isfinite(v) and v > 0 or (math.isfinite(v) and v > hi):
        return f"> {hi:g}"
    if not math.isfinite(v) or v < lo:
        return f"< {lo:g}"
    if d.get("bracket"):
        return f"{d['bracket'][0]:g}-{d['bracket'][1]:g} (step)"
    s = f"{v:.0f}"
    if d["lo"] is not None and d["hi"] is not None:
        s += f" ({min(d['lo'], v):.0f}-{min(max(d['hi'], v), 10 * hi):.0f})"
    return s


def is_mover_sm(r: dict) -> bool:
    return r.get("matched") and r.get("kind") == "mover" and r.get("evidence") in ("strong", "moderate")


def is_mover_any(r: dict) -> bool:
    return bool(r.get("matched") and r.get("kind") == "mover")


# ----------------------------------------------------------------------------- report
def _table(recs: list[dict], fluxes, motions, pred) -> list[str]:
    head = "| flux (uJy) | " + " | ".join(f'{m:g}"/yr' for m in motions) + " |"
    lines = [head, "|" + "---|" * (len(motions) + 1)]
    for fl in fluxes:
        cells = []
        for m in motions:
            rr = [r for r in recs if r["flux"] == fl and r["mu"] == m]
            cells.append(f"{sum(map(pred, rr)) / len(rr):.2f}" if rr else "-")
        lines.append(f"| {fl:g} | " + " | ".join(cells) + " |")
    return lines


def wilson(k: int, n: int, z: float = 1.0) -> tuple[float, float]:
    if n == 0:
        return 0.0, 1.0
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, c - h), min(1.0, c + h)


def render(records: list[dict], meta: dict) -> str:
    rng = np.random.default_rng(1)
    fluxes, motions, shapes = meta["fluxes"], meta["motions"], meta["shapes"]
    recs = [r for r in records if "error" not in r]
    mot = [r for r in recs if r["kind_inj"] == "motion"]
    var = [r for r in recs if r["kind_inj"] == "variable"]
    errors = [r for r in records if "error" in r]
    L: list[str] = []
    add = L.append
    add("# Sensitivity: injection-recovery test")
    add("")
    add("GENERATED by `python -m app.sensitivity` - do not edit by hand.")
    add("")

    # --- summary numbers used in the plain-language section
    comp = {}
    for shape in shapes:
        for m in motions:
            rr = [r for r in mot if r["shape"] == shape and r["mu"] == m]
            if not rr:
                continue
            fl = np.array([r["flux"] for r in rr], float)
            pred = is_mover_sm if m > 0 else (lambda r: bool(r.get("matched")))
            comp[(shape, m)] = completeness(fl, np.array([pred(r) for r in rr], float), rng)
    static = [r for r in mot if r["mu"] == 0]
    fp_any = sum(map(is_mover_any, static))
    fp_sm = sum(map(is_mover_sm, static))
    var_mover = sum(map(is_mover_any, var))

    add("## Summary")
    add("")
    for line in meta["summary"]:
        add(line)
    add("")

    add("## Method")
    add("")
    add(f"- Field: quiet COSMOS control field RA {meta['ra']}, Dec {meta['dec']}, {FOV_ARCMIN:g}' "
        f"({meta['n']} x {meta['n']} px of {PIX_ARCSEC}\"), cache key `{meta['key']}`; {meta['n_frames']} "
        f"exposures in {meta['n_seasons']} seasons ({meta['season_dates']}).")
    add("- Per trial ONE point source is added to in-memory copies of every exposure's `sci` "
        "(MJy/sr; flux converted with `phot.ujy_per_mjysr_pixel`). No noise is added and `var` is "
        "unchanged (the real exposures supply the noise; source shot noise is not modelled, which "
        "slightly flatters the brightest injections).")
    add("- PSF: pixel-integrated circular Gaussian, sigma MEASURED per detector by fitting isolated "
        "unsaturated stars (found in the season stacks) in the individual reprojected exposures:")
    add("")
    add("| detector | sigma (px) | IQR | fits (stars) |")
    add("|---|---|---|---|")
    for det, p in sorted(meta["psf"].items(), key=lambda kv: int(kv[0])):
        if p.get("borrowed_from"):
            add(f"| D{det} | {p['sigma_px']} | no usable star; value of D{p['borrowed_from']} | - |")
        else:
            add(f"| D{det} | {p['sigma_px']} | {p['q25']}-{p['q75']} | {p['n_fits']} ({p['n_stars']}) |")
    add("")
    add(f"- Motion: linear, no parallax, random direction; reference epoch = mean exposure epoch "
        f"(MJD {meta['t_ref']:.1f}), so the mean injected position is the reference position. "
        f"Positions are random but at least {EDGE_MARGIN_PX:g} px (+ the track length) from the edges, "
        f"{CENTRE_EXCL_PX:g} px from the field centre (where the target verdict would take over), "
        f"{CAND_EXCL_PX:g} px from candidates of the un-injected field and off pixels belonging to "
        "sources detected in the season stacks or Gaia DR3 stars from the catalogue cache.")
    add("- Spectrum shapes (quoted flux = F_nu in the bright parts):")
    for s in shapes:
        add(f"  - `{s}`: {SHAPE_TEXT[s]}")
    add("- Variable: static, flat spectrum, flux x1 in seasons 0 and 2 and x2 in season 1 (it doubles "
        "between consecutive seasons); quoted flux = the faint state.")
    add(f"- `analysis.run` runs unchanged on the injected arrays (frame loader and stack/diff writer "
        f"redirected to memory; the fast-mover stage, which does not feed candidates, is skipped). "
        f"Recovered = a candidate within {MATCH_PX} px of the injected mean position (best-ranked if "
        "several).")
    add(f"- Trials: {meta['trials']} per (shape, motion, flux) cell, {meta['var_trials']} per variable "
        f"flux; {len(records)} trials in total ({len(errors)} errors); seed {meta['seed']}; "
        f"{meta['workers']} worker processes; runtime {meta['runtime_s'] / 60:.1f} min.")
    add(f"- Code: git commit `{meta['commit']}`; versions {meta['versions']}.")
    add("")

    add("## Recovery fractions")
    add("")
    add("Each cell is the fraction of trials recovered; "
        f"{meta['trials']} trials per cell (binomial 1-sigma error about "
        f"{math.sqrt(0.25 / max(meta['trials'], 1)):.2f} at 50%).")
    add("")
    for s in shapes:
        rr = [r for r in mot if r["shape"] == s]
        add(f"### `{s}`: mover with strong or moderate evidence")
        add("")
        L.extend(_table(rr, fluxes, motions, is_mover_sm))
        add("")
        add(f"### `{s}`: any candidate (any kind or evidence) within {MATCH_PX} px")
        add("")
        L.extend(_table(rr, fluxes, motions, lambda r: bool(r.get("matched"))))
        add("")

    add("## Completeness flux")
    add("")
    add("Flux (uJy) at which 50% / 90% of injections are recovered, from a logistic fit in log flux; "
        "in brackets the 16-84% range of 200 bootstrap resamples of the trials (binomial). Criterion: "
        "mover with strong/moderate evidence for moving sources; any candidate for 0\"/yr (a static, "
        "constant source should NOT produce a candidate: this column is a false-alarm measure). "
        f"`> {max(fluxes):g}` = not reached within the tested fluxes; `A-B (step)` = recovery jumps "
        "from (almost) none to (almost) all between two adjacent tested fluxes, so only that bracket is known.")
    add("")
    add("| motion | " + " | ".join(f"{s} 50% | {s} 90%" for s in shapes) + " |")
    add("|---|" + "---|---|" * len(shapes))
    for m in motions:
        cells = []
        for s in shapes:
            c = comp.get((s, m))
            cells += [fmt_level(c["f50"]), fmt_level(c["f90"])] if c else ["-", "-"]
        add(f'| {m:g}"/yr | ' + " | ".join(cells) + " |")
    add("")

    add("## False positives on static sources")
    add("")
    lo, hi = wilson(fp_any, len(static))
    add(f"- Static, constant injections (0\"/yr, all shapes and fluxes) labelled **mover**: "
        f"{fp_any} of {len(static)} ({fp_any / max(len(static), 1):.1%}, 1-sigma {lo:.1%}-{hi:.1%}); "
        f"with strong/moderate evidence: {fp_sm}.")
    lo, hi = wilson(var_mover, len(var))
    add(f"- Variable injections labelled mover: {var_mover} of {len(var)} ({lo:.1%}-{hi:.1%} at 1 sigma).")
    kinds0 = {}
    for r in static:
        if r.get("matched"):
            kinds0[r["kind"]] = kinds0.get(r["kind"], 0) + 1
    add(f"- Candidates of any kind at static constant injections: {kinds0 or 'none'}.")
    add("")

    add("## Proper-motion accuracy")
    add("")
    add("Movers recovered (any evidence) with a measured proper motion; error = recovered - injected "
        "vector; speed bias = median (|recovered| - injected) / injected; all shapes pooled.")
    add("")
    add("| motion | n | median abs error (\"/yr) | median speed bias | median direction error (deg) |")
    add("|---|---|---|---|---|")
    pm_rows = {}
    for m in motions:
        if m == 0:
            continue
        rr = [r for r in mot if r["mu"] == m and is_mover_any(r) and r.get("pmra_rec") is not None]
        if not rr:
            add(f'| {m:g}"/yr | 0 | - | - | - |')
            continue
        err = [math.hypot(r["pmra_rec"] - r["pmra_inj"], r["pmdec_rec"] - r["pmdec_inj"]) / 1000 for r in rr]
        bias = [(math.hypot(r["pmra_rec"], r["pmdec_rec"]) / 1000 - m) / m for r in rr]
        dang = [abs((math.degrees(math.atan2(r["pmra_rec"], r["pmdec_rec"]))
                     - math.degrees(math.atan2(r["pmra_inj"], r["pmdec_inj"])) + 180) % 360 - 180) for r in rr]
        pm_rows[m] = (len(rr), float(np.median(err)), float(np.median(bias)), float(np.median(dang)))
        add(f'| {m:g}"/yr | {len(rr)} | {np.median(err):.2f} | {np.median(bias):+.0%} | {np.median(dang):.0f} |')
    add("")

    add("## Variable recovery")
    add("")
    add("| faint-state flux (uJy) | variable (strong/moderate) | variable (any) | any candidate | mover |")
    add("|---|---|---|---|---|")
    for fl in fluxes:
        rr = [r for r in var if r["flux"] == fl]
        if not rr:
            continue
        n = len(rr)
        vs = sum(r.get("matched") and r.get("kind") == "variable" and r.get("evidence") in ("strong", "moderate")
                 for r in rr)
        va = sum(r.get("matched") and r.get("kind") == "variable" for r in rr)
        an = sum(bool(r.get("matched")) for r in rr)
        add(f"| {fl:g} | {vs / n:.2f} | {va / n:.2f} | {an / n:.2f} | {sum(map(is_mover_any, rr)) / n:.2f} |")
    add("")

    add("## Minimum detectable motion")
    add("")
    for line in meta["min_motion"]:
        add(line)
    add("")

    add("## Why bright injections were missed")
    add("")
    for line in meta["findings"]:
        add(line)
    add("")

    add("## Planet Nine")
    add("")
    for line in meta["planet9"]:
        add(line)
    add("")
    if meta.get("svg"):
        add(f"![Completeness vs flux (flat spectrum)]({meta['svg']})")
        add("")
    return "\n".join(L)


# ----------------------------------------------------------------------------- derived text
def season_epochs(frames: list[dict]) -> dict[int, float]:
    out: dict[int, list[float]] = {}
    for f in frames:
        out.setdefault(f["season"], []).append(f["mjd"])
    return {s: float(np.mean(v)) for s, v in sorted(out.items())}


def min_motion_text(records: list[dict], frames: list[dict], motions, fluxes) -> list[str]:
    from . import changes as ch

    ep = season_epochs(frames)
    seasons = sorted(ep)
    gaps = {(a, b): (ep[b] - ep[a]) / DAYS_PER_YEAR for i, a in enumerate(seasons) for b in seasons[i + 1:]}
    mot = [r for r in records if r.get("kind_inj") == "motion" and "error" not in r]
    lines = ["Season mean epochs (MJD): " + ", ".join(f"S{s} {v:.0f}" for s, v in ep.items()) + "; pair "
             "baselines " + ", ".join(f"S{a}-S{b} {g:.2f} yr" for (a, b), g in gaps.items()) + "."]
    floor = math.sqrt(2) * ch.REG_FLOOR_PX
    need = max(ch.MIN_SHIFT_PX, ch.SHIFT_SIGMA * floor)
    lines.append(
        f"A season pair calls a 'mover' only if the centroid shift is >= {ch.MIN_SHIFT_PX} px and >= "
        f"{ch.SHIFT_SIGMA:g} sigma, where every centroid error includes a {ch.REG_FLOOR_PX} px registration "
        f"floor. Even for an infinitely bright source the shift must therefore exceed {need:.2f} px "
        f"({need * PIX_ARCSEC:.2f}\"), i.e. a proper motion of at least "
        + ", ".join(f"{need * PIX_ARCSEC / g:.1f}\"/yr over S{a}-S{b}" for (a, b), g in gaps.items())
        + ". 'Strong'/'moderate' evidence further needs two or more independent season comparisons "
        "(per detector: seasons in agreeing pairs - 1, summed over detectors), e.g. the S0-S2 pair in "
        "two detectors. In this field D3 and D6 have no season-0 exposures (only the 0.47 yr S1-S2 pair), "
        "which is why the Y dwarf-like shape (D5/D6 only) needs about twice the motion of a flat one.")
    for shape in sorted({r["shape"] for r in mot}):
        best = None
        for m in sorted(x for x in motions if x > 0):
            rr = [r for r in mot if r["shape"] == shape and r["mu"] == m and r["flux"] == max(fluxes)]
            if rr and sum(map(is_mover_sm, rr)) / len(rr) >= 0.5:
                best = m
                break
        lines.append(f"- `{shape}`: slowest tested motion recovered as a strong/moderate mover in >= 50% of "
                     f"trials at the brightest flux ({max(fluxes):g} uJy): "
                     + (f'{best:g}"/yr.' if best is not None else "none of the tested motions."))
    return lines


def planet9_text() -> list[str]:
    au_per_yr_parallax = 2.0  # Earth moves ~2 AU across its orbit between seasons ~6 months apart
    lines = ["This was NOT measured; the model injected here (linear motion of 0-16\"/yr, no parallax) "
             "does not describe such an object. Geometry alone shows why: between two SPHEREx seasons "
             "about six months apart the Earth is displaced by up to ~2 AU, so an object at distance d "
             "shifts by up to ~2/d radians from parallax:"]
    for d in (300, 500, 800):
        shift = au_per_yr_parallax / d * 206265
        lines.append(f"- d = {d} AU: up to ~{shift:.0f}\" ({shift / 60:.1f}') between seasons, versus a 5' "
                     "field and the <= 16\"/yr linear motions tested here.")
    lines.append("Such an object would leave the field or land far from where a linear proper-motion "
                 "search looks, so this pipeline's season-difference mover search cannot recover it, at "
                 "any brightness; the flux limits above do not apply. (Distances are the commonly "
                 "discussed few-hundred-AU range; brightness predictions are model dependent and were not "
                 "used.)")
    return lines


def git_commit() -> str:
    try:
        root = Path(__file__).resolve().parents[2]
        h = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=root, capture_output=True, text=True,
                           timeout=10).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--", "backend/app"], cwd=root,
                               capture_output=True, text=True, timeout=10).stdout.strip()
        return h + (" (+ uncommitted changes in backend/app)" if dirty else "")
    except Exception:  # noqa: BLE001
        return "unknown"


def versions() -> str:
    from . import processing, stacks

    return (f"PROCESSING {processing.PROCESSING_VERSION}, STACK {getattr(stacks, 'STACK_VERSION', '?')}, "
            f"ANALYSIS {analysis.ANALYSIS_VERSION}")


def write_svg(records: list[dict], fluxes, motions, path: Path) -> None:
    """Small completeness-vs-flux figure: flat spectrum, mover strong/moderate, one line per motion."""
    W, H, ml, mr, mt, mb = 520, 300, 52, 110, 16, 40
    lo, hi = math.log10(min(fluxes)), math.log10(max(fluxes))
    X = lambda f: ml + (math.log10(f) - lo) / (hi - lo) * (W - ml - mr)  # noqa: E731
    Y = lambda p: mt + (1 - p) * (H - mt - mb)  # noqa: E731
    cols = ["#9ecae1", "#6baed6", "#4292c6", "#2171b5", "#08519c", "#08306b"]
    s = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
         'font-family="system-ui,sans-serif" font-size="11">',
         f'<rect width="{W}" height="{H}" fill="#fff"/>']
    for p in (0, 0.5, 0.9, 1):
        s.append(f'<line x1="{ml}" x2="{W - mr}" y1="{Y(p):.1f}" y2="{Y(p):.1f}" stroke="#e5e5e5"/>')
        s.append(f'<text x="{ml - 6}" y="{Y(p) + 4:.1f}" text-anchor="end" fill="#555">{p:.0%}</text>')
    for f in fluxes[::2]:
        s.append(f'<text x="{X(f):.1f}" y="{H - mb + 16}" text-anchor="middle" fill="#555">{f:g}</text>')
    s.append(f'<text x="{(ml + W - mr) / 2}" y="{H - 6}" text-anchor="middle" fill="#333">injected flux (uJy), '
             'flat spectrum</text>')
    s.append(f'<text x="12" y="{(mt + H - mb) / 2}" transform="rotate(-90 12 {(mt + H - mb) / 2})" '
             'text-anchor="middle" fill="#333">recovered as mover (strong/moderate)</text>')
    for i, m in enumerate(x for x in motions if x > 0):
        pts = []
        for f in fluxes:
            rr = [r for r in records if r.get("kind_inj") == "motion" and r.get("shape") == "flat"
                  and r["mu"] == m and r["flux"] == f and "error" not in r]
            if rr:
                pts.append((X(f), Y(sum(map(is_mover_sm, rr)) / len(rr))))
        c = cols[min(i, len(cols) - 1)]
        s.append(f'<polyline fill="none" stroke="{c}" stroke-width="2" points="'
                 + " ".join(f"{x:.1f},{y:.1f}" for x, y in pts) + '"/>')
        s.append(f'<line x1="{W - mr + 14}" x2="{W - mr + 32}" y1="{mt + 10 + 16 * i}" y2="{mt + 10 + 16 * i}" '
                 f'stroke="{c}" stroke-width="2"/><text x="{W - mr + 36}" y="{mt + 14 + 16 * i}" fill="#333">'
                 f'{m:g}"/yr</text>')
    s.append("</svg>")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(s), encoding="utf-8")


def clip_diagnostic(key: str, grid: Grid, frames: list[dict], base: dict[str, dict], sigma: dict[int, float],
                    allowed: np.ndarray, t_ref: float, flux: float, shapes) -> list[dict]:
    """For one bright 8"/yr injection per shape at a fixed position: how many pixel-frames within
    2 px of the source the stacks' per-pixel outlier rejection removed (vs the un-injected field),
    and what the pipeline called it."""
    with in_memory(base) as m0:
        analysis.run(key, grid, frames)
        ref = dict(m0)
    ys, xs = np.nonzero(allowed)
    c = grid.n / 2
    i = int(np.argmin(np.hypot(xs - c, ys - c * 0.6)))
    out = []
    for shape in shapes:
        inj = Injection(float(xs[i]), float(ys[i]), 8.0, 45.0, flux, shape, t_ref)
        with in_memory(inject(frames, base, inj, sigma, grid.pixscale_arcsec)) as m:
            _, res = analysis.run(key, grid, frames)
            mem = dict(m)
        ix, iy = int(round(inj.x0)), int(round(inj.y0))
        box = (slice(iy - 2, iy + 3), slice(ix - 2, ix + 3))
        clipped = {}
        for k, v in mem.items():
            if f"{os.sep}stacks{os.sep}" in k and k in ref:
                d = int(ref[k]["n"][box].sum() - v["n"][box].sum())
                if d > 0:
                    clipped[Path(k).stem] = d
        r = score(res, inj)
        out.append({"shape": shape, "x0": inj.x0, "y0": inj.y0, "flux": flux, "kind": r.get("kind"),
                    "evidence": r.get("evidence"), "reason": r.get("reason"), "event_kinds": r.get("event_kinds"),
                    "clipped_total": sum(clipped.values()), "clipped": clipped})
    return out


# ----------------------------------------------------------------------------- CLI
def main(argv: list[str] | None = None) -> None:
    from app import sensitivity as S  # picklable references for the worker processes

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--field", nargs=2, type=float, metavar=("RA", "DEC"), default=FIELD)
    ap.add_argument("--trials", type=int, default=20, help="trials per (shape, motion, flux) cell")
    ap.add_argument("--var-trials", type=int, default=None, help="trials per variable flux (default 2x --trials)")
    ap.add_argument("--fluxes", type=float, nargs="+", default=FLUXES)
    ap.add_argument("--motions", type=float, nargs="+", default=MOTIONS)
    ap.add_argument("--shapes", nargs="+", default=SHAPES, choices=SHAPES)
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) // 2))
    ap.add_argument("--seed", type=int, default=2026)
    ap.add_argument("--out", default="docs/sensitivity.md", help="relative to the repository root")
    ap.add_argument("--svg", default="docs/media/sensitivity/completeness.svg", help="'' to skip")
    ap.add_argument("--records", default=None, help="also save raw trial records (JSON) here")
    ap.add_argument("--from-records", default=None, help="re-render the report from saved records")
    ap.add_argument("--findings", default=None, help="text file with investigated findings to include")
    args = ap.parse_args(argv)
    root = Path(__file__).resolve().parents[2]
    ra, dec = args.field

    if args.from_records:
        saved = json.loads(Path(args.from_records).read_text())
        records, meta = saved["records"], saved["meta"]
        key, grid, frames, _ = load_field(meta["ra"], meta["dec"])
    else:
        t0 = time.time()
        key, grid, frames, base = load_field(ra, dec)
        base_res = run_analysis(key, grid, frames, base)
        t_base = time.time() - t0
        psf = measure_psf(key, grid, frames, base)
        if not psf:
            raise SystemExit("no isolated unsaturated star found to measure the PSF")
        for det in range(1, 7):  # detectors without a usable star borrow the nearest measured one
            if det not in psf:
                src = min(psf, key=lambda d: (abs(d - det), -d))
                psf[det] = {**psf[src], "borrowed_from": src}
        sigma = {int(d): p["sigma_px"] for d, p in psf.items()}
        max_track = max(args.motions) * (max(f["mjd"] for f in frames) - min(f["mjd"] for f in frames)) \
            / DAYS_PER_YEAR / PIX_ARCSEC / 2
        allowed = allowed_mask(key, grid, frames, base_res["candidates"], EDGE_MARGIN_PX + max_track)
        rng = np.random.default_rng(args.seed)
        var_trials = args.var_trials if args.var_trials is not None else 2 * args.trials
        plan = plan_trials(frames, allowed, args.trials, var_trials, args.fluxes, args.motions, args.shapes, rng)
        print(f"field {key}: {len(frames)} frames, base analysis {t_base:.1f}s, {int(allowed.sum())} allowed px, "
              f"PSF sigma {sigma}, {len(plan)} trials on {args.workers} workers", flush=True)
        records = []
        t1 = time.time()
        with ProcessPoolExecutor(args.workers, initializer=S._init_worker, initargs=(ra, dec, sigma)) as ex:
            for i, r in enumerate(ex.map(S._trial, plan, chunksize=2)):
                records.append(r)
                if (i + 1) % 50 == 0:
                    el = time.time() - t1
                    print(f"  {i + 1}/{len(plan)}  {el:.0f}s  eta {el / (i + 1) * (len(plan) - i - 1):.0f}s", flush=True)
        ep = season_epochs(frames)
        t_ref = float(np.mean([f["mjd"] for f in frames]))
        diag = clip_diagnostic(key, grid, frames, base, sigma, allowed, t_ref, max(args.fluxes), args.shapes)
        from astropy.time import Time

        meta = {"ra": ra, "dec": dec, "key": key, "n": grid.n, "n_frames": len(frames), "n_seasons": len(ep),
                "season_dates": ", ".join(Time(v, format="mjd").isot[:10] for v in ep.values()),
                "t_ref": float(np.mean([f["mjd"] for f in frames])), "psf": {str(k): v for k, v in psf.items()},
                "fluxes": list(args.fluxes), "motions": list(args.motions), "shapes": list(args.shapes),
                "trials": args.trials, "var_trials": var_trials, "seed": args.seed, "workers": args.workers,
                "runtime_s": time.time() - t0, "commit": git_commit(), "versions": versions(),
                "clip_diag": diag}
    if args.records:
        Path(args.records).write_text(json.dumps({"records": records, "meta": meta}))

    meta["min_motion"] = min_motion_text(records, frames, meta["motions"], meta["fluxes"])
    meta["planet9"] = planet9_text()
    meta["findings"] = (Path(args.findings).read_text(encoding="utf-8").splitlines() if args.findings
                        else auto_findings(records, meta))
    meta["summary"] = summary_text(records, meta)
    if args.svg:
        write_svg(records, meta["fluxes"], meta["motions"], root / args.svg)
        meta["svg"] = os.path.relpath(root / args.svg, (root / args.out).parent).replace("\\", "/")
    out = root / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(records, meta) + "\n", encoding="utf-8")
    print(f"wrote {out}")


def auto_findings(records: list[dict], meta: dict) -> list[str]:
    """Tally of what the pipeline did with the brightest moving injections it did not call movers."""
    top = max(meta["fluxes"])
    rr = [r for r in records if r.get("kind_inj") == "motion" and r.get("mu", 0) >= 4 and r["flux"] == top
          and "error" not in r and not is_mover_sm(r)]
    if not rr:
        return [f"Every injection at {top:g} uJy moving >= 4\"/yr was recovered as a strong/moderate mover."]
    tally: dict[str, int] = {}
    for r in rr:
        k = (f"{r.get('kind')}/{r.get('evidence')}: {r.get('reason') or 'no reason given'}" if r.get("matched")
             else f"no candidate within {MATCH_PX} px (within 3 px: {r.get('kind3')})")
        tally[k] = tally.get(k, 0) + 1
    lines = [f"Injections at {top:g} uJy moving >= 4\"/yr NOT recovered as strong/moderate movers "
             f"({len(rr)} of {sum(1 for r in records if r.get('kind_inj') == 'motion' and r.get('mu', 0) >= 4 and r.get('flux') == top)}), "
             "by shape:"]
    for s in meta["shapes"]:
        n = sum(r["shape"] == s for r in rr)
        lines.append(f"- `{s}`: {n}")
    lines += ["", "What the pipeline made of them (kind/evidence: reason):", ""]
    lines += [f"- {n} x {k}" for k, n in sorted(tally.items(), key=lambda kv: -kv[1])]
    diag = meta.get("clip_diag") or []
    if diag:
        lines += ["", f"Stack outlier rejection, one 8\"/yr injection of {top:g} uJy per shape at the same "
                  "place: pixel-frames within 2 px of the source that the per-pixel rejection of `coadd()` "
                  "removed (season and bin stacks, compared with the un-injected field), and the outcome:", ""]
        for d in diag:
            top3 = ", ".join(f"{k} {v}" for k, v in sorted(d["clipped"].items(), key=lambda kv: -kv[1])[:4])
            lines.append(f"- `{d['shape']}`: {d['clipped_total']} pixel-frames removed"
                         + (f" (most in {top3})" if top3 else "")
                         + f"; result {d['kind']}/{d['evidence']}" + (f" ({d['reason']})" if d.get("reason") else "")
                         + (f", events {d['event_kinds']}" if d.get("event_kinds") else "") + ".")
        by = {d["shape"]: d["clipped_total"] for d in diag}
        if by.get("tdwarf", 0) > 5 * max(by.get("flat", 0), 1):
            lines += ["", "Interpretation: the exposures in one wavelength bin (and even more in a season x "
                      "detector stack) sample different wavelengths. For a source whose spectrum changes "
                      "strongly within a bin, the exposures at its bright wavelengths differ from the per-pixel "
                      "median by more than 5 sigma + 0.3 |median| and are rejected pixel by pixel, mostly in the "
                      "PSF core. The remaining pixels form a distorted PSF that differs between seasons, which "
                      "produces spurious centroid shifts (directions then disagree between pairs, 'centroid "
                      "shifts disagree') and flux ratios beyond 2, or 'appeared'/'variable' events. A spectrum "
                      "that is flat within each bin (flat, Y dwarf-like) loses nothing. The T dwarf-like shape "
                      "used here has sharp 20x steps, harsher than real T dwarfs, but real methane/water bands "
                      "are deep enough that this should be checked on real T dwarfs before relying on the "
                      "mover search for them."]
    inc = [r for r in records if r.get("kind_inj") == "motion" and r.get("mu", 0) >= 2 and r.get("matched")
           and r.get("coherent") is False and not is_mover_any(r) and "error" not in r]
    if inc:
        by_s = {s: sum(r["shape"] == s for r in inc) for s in meta["shapes"]}
        bright = sum(r["flux"] >= 400 for r in inc)
        lines += ["", f"Incoherent motion: {len(inc)} injections moving >= 2\"/yr (by shape {by_s}; {bright} of "
                  "them at >= 400 uJy) had a candidate whose mover events failed `changes.reconcile_movers` "
                  "(direction coherence R < 0.8 or speed scatter > 0.5), so EVERY mover event of the candidate "
                  "was relabelled and it ended 'uncertain'. The check is all-or-nothing: one outlying season "
                  "pair (a low-S/N pair, or a pair whose centroid is pulled by a faint neighbour) vetoes a "
                  "candidate even when ten other pairs agree, and because events are weighted by "
                  "`shift_sigma` (shift / error), a spuriously LARGE shift gets a LARGE weight. Dropping "
                  "outlying pairs before the coherence test (e.g. iterative clipping on the per-pair proper "
                  "motion) would recover these; not changed here."]
    st = [r for r in records if r.get("kind_inj") == "motion" and r.get("mu") == 0 and r.get("matched")
          and "error" not in r]
    fake = {}
    for r in st:
        if "mover" in (r.get("event_kinds") or []):
            fake[r["shape"]] = fake.get(r["shape"], 0) + 1
    if fake:
        lines += ["", "Static (0\"/yr) injections whose candidate contained a 'mover' event before vetting "
                  f"(spurious centroid shifts): {fake}. Vetting or reconciliation demoted these unless "
                  "counted in the false-positive line above."]
    return lines


def summary_text(records: list[dict], meta: dict) -> list[str]:
    mot = [r for r in records if r.get("kind_inj") == "motion" and "error" not in r]
    static = [r for r in mot if r["mu"] == 0]
    fp = sum(map(is_mover_any, static))
    top = max(meta["fluxes"])
    lines = []
    for s in meta["shapes"]:
        parts = []
        for m in meta["motions"]:
            if m == 0:
                continue
            rr = [r for r in mot if r["shape"] == s and r["mu"] == m and r["flux"] == top]
            if rr:
                parts.append(f'{m:g}"/yr {sum(map(is_mover_sm, rr))}/{len(rr)}')
        lines.append(f"- `{s}` at {top:g} uJy, recovered as strong/moderate movers: " + ", ".join(parts) + ".")
    rng = np.random.default_rng(2)
    plain = []
    for s in meta["shapes"]:
        parts = []
        for m in (2.0, 4.0, 8.0, 16.0):
            rr = [r for r in mot if r["shape"] == s and r["mu"] == m]
            if rr:
                c = completeness(np.array([r["flux"] for r in rr], float), np.array([is_mover_sm(r) for r in rr], float),
                                 rng, n_boot=50)
                parts.append(f'{m:g}"/yr: {fmt_level(c["f50"]).split(" (")[0]} uJy')
        plain.append(f"`{s}` " + ", ".join(parts))
    lines.append(f"- Static sources were labelled movers in {fp} of {len(static)} trials (none with "
                 "strong/moderate evidence)." if not sum(map(is_mover_sm, static)) else
                 f"- Static sources were labelled movers in {fp} of {len(static)} trials.")
    lines += ["", "In plain words: on this quiet field, with about a year between the first and last season, "
              "the tool reliably flags a moving object as a mover once it moves at least about 2-4\"/yr AND "
              "is bright enough. Flux at which half of the injected movers are flagged (strong/moderate "
              "evidence): " + "; ".join(plain) + ". Slower than about 2\"/yr nothing is flagged at any "
              "brightness, because the shift between seasons is then below the pipeline's registration floor. "
              "Sources faint in most of the band (Y dwarf-like) must be brighter where they do shine, and a "
              "spectrum with deep features inside a wavelength bin (T dwarf-like) is partly lost to the stack "
              "outlier rejection (see 'Why bright injections were missed'). These numbers hold for this "
              "field's depth and season coverage; a field with more or fewer exposures, crowding or bright "
              "stars will differ."]
    return lines


if __name__ == "__main__":
    sys.exit(main())
