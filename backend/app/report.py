"""Validation report generator: ``python -m app.report`` writes ``docs/validation.md``.

Everything in the report is computed at run time from the cached fields
(``FieldJob.load`` -> analysis dict), the cached Mover Fingerprints and a Gaia DR3 cone
search per field. The only hand-written inputs are the test targets themselves (position,
catalogue proper motion, what a human expects to see) in ``CASES``. Pure helpers (proper-motion
comparison, Gaia classification, table rendering, ...) are unit-tested in ``tests/test_report.py``.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import re
import subprocess
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from scipy import stats as sstats

from . import analysis, changes
from . import fingerprint as fpm
from . import psf as psfmod
from .bins import MIN_PER_SEASON
from .catalogs import CatalogResult, Star, gaia_cone
from .config import settings
from .coords import SPHEREX_REF_MJD
from .fields import FieldJob, field_key
from .processing import PROCESSING_VERSION, Grid
from .stacks import STACK_VERSION

log = logging.getLogger(__name__)

REPO = Path(__file__).resolve().parents[2]
FOV_ARCMIN = 5.0
MATCH_ARCSEC = 6.0              # Gaia cross-match radius
PM_DIR_TOL_DEG = 45.0           # catalogue motion "agrees" within this angle ...
PM_SPEED_FACTOR = 2.0           # ... and this speed ratio
GAIA_MOVING_MASYR = 100.0       # a Gaia star moving less than this cannot explain a mover candidate
# Methane feature index: the 3.3 um band against the flux peaks on either side of it (the local
# peak between the 2.7 um water and 3.3 um methane absorptions, and the 4 um peak).
METHANE_BAND = (3.1, 3.6)
CONTINUUM_BLUE = (2.9, 3.05)
CONTINUUM_RED = (3.8, 4.1)
DETECTED_SNR = 3.0              # a deblended flux below this S/N counts as "not detected"
NO_SPECTRUM_SAT_FRAC = 0.5      # more than this fraction of exposures with a saturated core -> no usable spectrum
SPREAD_FLAG = 0.25              # detector-to-detector speed spread (fraction of catalogue speed) worth a caveat

CLS_HIGH_PM = "consistent with a known high-proper-motion star"
CLS_VARIABLE = "known Gaia variable"
CLS_STATIC = "matches a non-moving, non-variable Gaia star (likely artefact or uncatalogued variability)"
CLS_PM_MISMATCH = "matches a Gaia star whose catalogue motion disagrees with the measured motion"
CLS_NONE = "no Gaia match"
CLASSES = [CLS_HIGH_PM, CLS_VARIABLE, CLS_STATIC, CLS_PM_MISMATCH, CLS_NONE]
LEVELS = ("strong", "moderate", "weak")


# ============================================================================ test cases
@dataclass(frozen=True)
class Case:
    slug: str
    name: str
    ra: float                       # field centre = position at MJD 61000 (SPHEREX_REF_MJD)
    dec: float
    expected: str                   # mover | variable | nothing
    expectation: str
    pm: tuple[float, float] | None = None   # catalogue (pmra*cos dec, pmdec) mas/yr
    context: tuple[str, ...] = ()


CASES = [
    Case("wise0855", "WISE 0855-0714 (Y dwarf)", 133.76002, -7.24221, "mover",
         "mover; only visible at 4-5 um; blended with a G=16.3 star 4.4 arcsec away",
         pm=(-8123.7, 673.2),
         context=("A Y dwarf is far too cold to be seen below about 4 um by SPHEREx, so detectors D1-D4 can only see "
                  "the neighbouring star.",)),
    Case("barnard", "Barnard's Star", 269.44629, 4.76788, "mover", "mover; saturated",
         pm=(-801.6, 10362.4),
         context=("A very bright star: the core is saturated in nearly every exposure, so fluxes come from "
                  "PSF wings only.",)),
    Case("epsindib", "Epsilon Indi B (T1+T6 pair, unresolved)", 331.09642, -56.80058, "mover",
         "mover; methane absorption", pm=(3981.9, -2466.8),
         context=("Two brown dwarfs closer than a SPHEREx pixel: the measurement is of the pair's combined light.",)),
    Case("rwaur", "RW Aurigae (T Tauri)", 76.95653, 30.40144, "variable", "variable",
         context=("A bright, nebulous young star: saturated in most exposures, with a fainter star 6 arcsec away.",)),
    Case("rdra", "R Draconis (Mira)", 248.16761, 66.75498, "variable", "variable; heavily saturated",
         context=("A very bright Mira: saturated in every exposure.",)),
    Case("control", "Control field (COSMOS, quiet sky)", 150.11916, 2.20583, "nothing", "nothing",
         context=("No known mover or variable at the field centre; used to count false alarms.",)),
]


# ============================================================================ pure helpers
def angle_between_deg(a: tuple[float, float], b: tuple[float, float]) -> float | None:
    """Angle (0-180 deg) between two 2-vectors; None if either has zero length."""
    na, nb = math.hypot(*a), math.hypot(*b)
    if na == 0 or nb == 0:
        return None
    c = (a[0] * b[0] + a[1] * b[1]) / (na * nb)
    return math.degrees(math.acos(max(-1.0, min(1.0, c))))


def compare_pm(meas: tuple[float, float], cat: tuple[float, float]) -> dict:
    """Measured vs catalogue proper motion: total speeds, % difference of the speed, direction difference."""
    tm, tc = math.hypot(*meas), math.hypot(*cat)
    return {"total_meas": tm, "total_cat": tc,
            "pct_diff": 100.0 * (tm - tc) / tc if tc else None,
            "angle_deg": angle_between_deg(meas, cat)}


def classify_gaia(meas_pm: tuple[float, float] | None, star: dict | None) -> str:
    """Classify a candidate from its nearest Gaia source (None: no source within the match radius).

    ``meas_pm`` is the candidate's measured motion (only for candidates still labelled movers).
    ``star`` has pmra, pmdec, has_pm and variable (True / False / None for 'no flag')."""
    if star is None:
        return CLS_NONE
    gaia_pm = (star["pmra"], star["pmdec"]) if star.get("has_pm", True) else None
    if meas_pm is not None and gaia_pm is not None:
        ang = angle_between_deg(meas_pm, gaia_pm)
        sm, sg = math.hypot(*meas_pm), math.hypot(*gaia_pm)
        if ang is not None and ang <= PM_DIR_TOL_DEG and sg > 0 and 1 / PM_SPEED_FACTOR <= sm / sg <= PM_SPEED_FACTOR:
            return CLS_HIGH_PM
    if star.get("variable") is True:
        return CLS_VARIABLE
    if meas_pm is not None and gaia_pm is not None and math.hypot(*gaia_pm) >= GAIA_MOVING_MASYR:
        return CLS_PM_MISMATCH
    return CLS_STATIC


def sky_sep_arcsec(ra1, dec1, ra2, dec2) -> np.ndarray:
    ra1, dec1, ra2, dec2 = (np.radians(np.asarray(v, float)) for v in (ra1, dec1, ra2, dec2))
    s = np.sin((dec2 - dec1) / 2) ** 2 + np.cos(dec1) * np.cos(dec2) * np.sin((ra2 - ra1) / 2) ** 2
    return np.degrees(2 * np.arcsin(np.sqrt(np.clip(s, 0, 1)))) * 3600


def nearest_star(ra: float, dec: float, stars: list[Star], mjd: float = SPHEREX_REF_MJD,
                 radius_arcsec: float = MATCH_ARCSEC) -> tuple[Star, float] | None:
    """Nearest catalogue star within the radius, with the stars moved to ``mjd`` by their proper motion."""
    if not stars:
        return None
    dt = np.array([(mjd - s.epoch_mjd) / 365.25 for s in stars])
    s_ra = np.array([s.ra for s in stars])
    s_dec = np.array([s.dec for s in stars])
    pmra = np.array([s.pmra for s in stars])
    pmdec = np.array([s.pmdec for s in stars])
    dec_now = s_dec + pmdec * dt / 3.6e6
    ra_now = s_ra + pmra * dt / 3.6e6 / np.cos(np.radians(s_dec))
    sep = sky_sep_arcsec(ra, dec, ra_now, dec_now)
    i = int(np.argmin(sep))
    return (stars[i], float(sep[i])) if sep[i] <= radius_arcsec else None


def _cell(v) -> str:
    s = "-" if v is None else str(v)
    return s.replace("|", "\\|").replace("\n", " ")


def md_table(headers: list[str], rows: list[list], align: list[str] | None = None) -> str:
    """GitHub-flavoured markdown table. ``align``: one of l/r/c per column."""
    n = len(headers)
    align = align or ["l"] * n
    if len(align) != n:
        raise ValueError("align length does not match headers")
    rule = {"l": ":---", "r": "---:", "c": ":---:"}
    lines = ["| " + " | ".join(_cell(h) for h in headers) + " |", "| " + " | ".join(rule[a] for a in align) + " |"]
    for r in rows:
        if len(r) != n:
            raise ValueError(f"row has {len(r)} cells, expected {n}")
        lines.append("| " + " | ".join(_cell(v) for v in r) + " |")
    return "\n".join(lines)


def fnum(x, nd: int = 2, plus: bool = False, comma: bool = False) -> str:
    if x is None or (isinstance(x, float) and not math.isfinite(x)):
        return "-"
    return format(x, f"{'+' if plus else ''}{',' if comma else ''}.{nd}f")


def fflux(v, e=None) -> str:
    """Flux in uJy as 'v ± e' with a sensible number of decimals."""
    if v is None:
        return "-"
    nd = 0 if abs(v) >= 100 or (e or 0) >= 10 else 1
    return f"{v:,.{nd}f}" + (f" ± {e:,.{nd}f}" if e is not None else "")


def merge_intervals(ivs: list[tuple[float, float]], gap: float = 0.0) -> list[tuple[float, float]]:
    out: list[list[float]] = []
    for lo, hi in sorted(ivs):
        if out and lo <= out[-1][1] + gap:
            out[-1][1] = max(out[-1][1], hi)
        else:
            out.append([lo, hi])
    return [(a, b) for a, b in out]


def fmt_intervals(ivs: list[tuple[float, float]]) -> str:
    return ", ".join(f"{a:.2f}-{b:.2f}" for a, b in ivs) + " µm" if ivs else "-"


def wmean(vals, errs) -> tuple[float, float, int] | None:
    """Inverse-variance mean, formal error, number of points; None when nothing usable."""
    pts = [(v, e) for v, e in zip(vals, errs) if v is not None and e and math.isfinite(v) and e > 0]
    if not pts:
        return None
    w = np.array([1 / e**2 for _, e in pts])
    v = np.array([v for v, _ in pts])
    return float(np.sum(w * v) / np.sum(w)), float(1 / math.sqrt(np.sum(w))), len(pts)


def window_mean(spec: list[dict], lo: float, hi: float) -> tuple[float, float, int] | None:
    sel = [r for r in spec if r.get("wavelength_um") is not None and lo <= r["wavelength_um"] < hi]
    return wmean([r.get("flux_ujy") for r in sel], [r.get("flux_err_ujy") for r in sel])


def band_depth(spec: list[dict], band=METHANE_BAND, blue=CONTINUUM_BLUE, red=CONTINUUM_RED,
               min_channels: int = 2) -> dict | None:
    """Mean flux in ``band`` over the mean of the two side-window means (ratio below 1 = absorption)."""
    b, c1, c2 = window_mean(spec, *band), window_mean(spec, *blue), window_mean(spec, *red)
    if None in (b, c1, c2) or min(b[2], c1[2], c2[2]) < min_channels or min(b[0], c1[0], c2[0]) <= 0:
        return None
    cont = 0.5 * (c1[0] + c2[0])
    e_cont = 0.5 * math.hypot(c1[1], c2[1])
    ratio = b[0] / cont
    err = ratio * math.hypot(b[1] / b[0], e_cont / cont)
    return {"ratio": ratio, "err": err, "band": b, "blue": c1, "red": c2, "cont": (cont, e_cont)}


def lc_judgement(lc: dict) -> tuple[str, str]:
    """(label, reason) for one light-curve bin, with the same rules the report states."""
    pts = lc.get("points") or []
    if lc.get("chi2_const") is None or not lc.get("matched"):
        return "inconclusive", "no channel observed in two or more seasons"
    if min(p["n"] for p in pts) < 2:
        return "inconclusive", "a season has a single exposure"
    suspect = lc.get("quality") != "good"
    if lc.get("variable"):
        return ("variable (indicative)", "saturated-core fits, 15% floor exceeded") if suspect else ("variable", "")
    return ("inconclusive", "saturated-core fits; the 15% floor hides smaller changes") if suspect \
        else ("constant within errors", "")


def lc_outliers(lcs: list[dict], factor: float = 2.0) -> list[dict]:
    """Bins whose flux in a season is more than ``factor`` away from both adjacent bins of the same detector."""
    out = []
    by_det: dict[int, list[dict]] = defaultdict(list)
    for lc in lcs:
        by_det[lc["detector"]].append(lc)
    for det, items in by_det.items():
        items.sort(key=lambda x: x["lam_lo_um"])
        for i in range(1, len(items) - 1):
            for p in items[i]["points"]:
                nb = []
                for j in (i - 1, i + 1):
                    q = next((q for q in items[j]["points"] if q["season"] == p["season"]), None)
                    if q and q.get("flux_ujy"):
                        nb.append(q["flux_ujy"])
                f = p.get("flux_ujy")
                if len(nb) == 2 and f and (f < min(nb) / factor or f > max(nb) * factor):
                    out.append({"bin": items[i]["bin"], "season": p["season"], "flux": f, "neighbours": nb,
                                "lam": (items[i]["lam_lo_um"], items[i]["lam_hi_um"])})
    return out


def detector_of_channel(ch: int) -> int:
    return ch // 17 + 1


# ============================================================================ loading
@dataclass
class Result:
    case: Case
    grid: Grid
    key: str
    job: FieldJob | None
    fp: dict | None = None
    fp_note: str | None = None
    gaia: CatalogResult | None = None
    caveats: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.job is not None and bool(self.job.analysis)

    @property
    def an(self) -> dict:
        return self.job.analysis

    @property
    def target(self) -> dict:
        return self.an["target"]

    @property
    def rows(self) -> list[dict]:
        return self.target["rows"]

    @property
    def nontarget(self) -> list[dict]:
        return [c for c in self.an["candidates"] if not c.get("is_target")]

    @property
    def target_cand(self) -> dict | None:
        return next((c for c in self.an["candidates"] if c.get("is_target")), None)


def load_fingerprint(case: Case, job: FieldJob, compute: bool) -> tuple[dict | None, str | None]:
    """Cached fingerprint of the case's target (computed in-process when missing and ``compute``)."""
    pm = case.pm or (0.0, 0.0)
    target = fpm.Target(ra=case.ra, dec=case.dec, pmra=pm[0], pmdec=pm[1], epoch_mjd=SPHEREX_REF_MJD)
    frames = [f for f in job.frames.values() if f.get("mjd") is not None]
    mjds = [f["mjd"] for f in frames]
    cat = gaia_cone(target.ra, target.dec, fpm.query_radius_deg(target, mjds))
    if not cat.ok:
        return None, cat.note
    sel = fpm.select_sources(target, cat, mjds)
    path = fpm.result_path(job.key, target, sel.sources)
    if path.exists():
        return json.loads(path.read_text()), None
    if not compute:
        return None, "no cached fingerprint (rerun with --compute to build it)"
    notes: list[str] = []
    need: dict[tuple[str, int], str] = {}
    for f in frames:
        k = (psfmod.pipeline_tag(f["source_url"]), int(f["detector"]))
        if k not in need and psfmod.load_library(f["source_url"], k[1]) is None:
            need[k] = f["source_url"]
    for k, url in need.items():
        try:
            psfmod.fetch_library(url, k[1])
        except Exception as exc:  # noqa: BLE001
            notes.append(f"ePSF for {k[0]} D{k[1]} unavailable ({type(exc).__name__}); those exposures are marked bad.")
    points = fpm.run_photometry(job.grid, frames, sel.sources)
    result = fpm.assemble(job.key, target, sel, cat, points, notes)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(result))
    tmp.replace(path)
    return result, None


def gaia_for_field(res: Result) -> CatalogResult:
    half_diag = res.grid.n * res.grid.pixscale_arcsec / 2 * math.sqrt(2)
    return gaia_cone(res.grid.ra, res.grid.dec, (half_diag + MATCH_ARCSEC + 5.0) / 3600)


def load_all(compute: bool, use_network: bool) -> list[Result]:
    results = []
    for case in CASES:
        grid = Grid.for_fov(case.ra, case.dec, FOV_ARCMIN, settings.pixel_scale_arcsec)
        key = field_key(grid)
        job = FieldJob.load(key, grid)
        res = Result(case, grid, key, job)
        if res.ok and case.expected != "nothing":
            try:
                res.fp, res.fp_note = load_fingerprint(case, job, compute)
            except Exception as exc:  # noqa: BLE001
                res.fp_note = f"{type(exc).__name__}: {exc}"
        results.append(res)

    def gaia(res: Result) -> CatalogResult:
        if not res.ok or not use_network:
            return CatalogResult(ok=False, note="Gaia query skipped (--offline)" if res.ok else "field not cached")
        return gaia_for_field(res)

    with ThreadPoolExecutor(max_workers=3) as ex:
        for res, cat in zip(results, ex.map(gaia, results)):
            res.gaia = cat
    return results


# ============================================================================ derived numbers
def variable_summary(rows: list[dict]) -> dict:
    var = [r for r in rows if r["kind"] == "variable" and r.get("flux_ratio") is not None]
    sta = [r for r in rows if r["kind"] == "static" and r.get("lam_lo_um") is not None]

    def span(rs):
        return merge_intervals([(r["lam_lo_um"], r["lam_hi_um"]) for r in rs if r.get("lam_lo_um") is not None])

    ratios = [r["flux_ratio"] for r in var]
    return {"n": len(var), "n_rows": len(rows), "rmin": min(ratios) if ratios else None,
            "rmax": max(ratios) if ratios else None, "dets": sorted({r["detector"] for r in var}),
            "spans": span(var), "static_spans": span(sta)}


def row_pm(row: dict) -> tuple[float, float] | None:
    if row.get("kind") == "mover" and "pmra_masyr" in row:
        return float(row["pmra_masyr"]), float(row["pmdec_masyr"])
    return None


def coverage_counts(job: FieldJob) -> dict[tuple[int, int], int]:
    c: Counter = Counter()
    for f in job.frames.values():
        c[(f["season"], f["detector"])] += 1
    return dict(c)


def failure_summary(job: FieldJob) -> list[tuple[str, int, str]]:
    """(detector, count, reason) of failed downloads."""
    c: Counter = Counter()
    for fid, err in job.failed.items():
        m = re.search(r"_D(\d)$", fid)
        c[(f"D{m.group(1)}" if m else "?", re.sub(r"^RuntimeError: cutout download failed: ", "", err))] += 1
    return sorted((d, n, e) for (d, e), n in c.items())


def lc_vs_rows(rows: list[dict], lcs: list[dict]) -> list[dict]:
    """Per detector: flux ratio of the last over the first season from the stack differences and from
    the PSF-photometry light curves (median over bins). Flags a change of direction between the two."""
    out = []
    for det in range(1, 7):
        rr = [r for r in rows if r["detector"] == det and r.get("flux_ratio") is not None]
        if not rr:
            continue
        sa, sb = min(r["season_a"] for r in rr), max(r["season_b"] for r in rr)
        row = next((r for r in rr if r["season_a"] == sa and r["season_b"] == sb), None)
        ratios = []
        for lc in lcs:
            if lc["detector"] != det:
                continue
            f = {p["season"]: p["flux_ujy"] for p in lc["points"] if p.get("flux_ujy")}
            if sa in f and sb in f:
                ratios.append(f[sb] / f[sa])
        r_diff = row["flux_ratio"] if row else None
        r_lc = float(np.median(ratios)) if ratios else None
        if r_diff is None or r_lc is None or len(ratios) < 2:
            verdict = "too few bins"
        elif (r_diff - 1) * (r_lc - 1) > 0 or (abs(r_diff - 1) < 0.1 and abs(r_lc - 1) < 0.1):
            verdict = "agree"
        else:
            verdict = "DISAGREE"
        out.append({"det": det, "sa": sa, "sb": sb, "r_diff": r_diff, "r_lc": r_lc, "n_bins": len(ratios), "verdict": verdict})
    return out


def det_means(fp: dict) -> list[dict]:
    """Per detector: weighted means of the naive aperture, the deblended target and each neighbour."""
    spec = fp["spectrum"]
    out = []
    for det in range(1, 7):
        rs = [r for r in spec if r["detector"] == det]
        if not rs:
            continue
        d = {"det": det, "n": len(rs), "lam": (min(r["wavelength_um"] for r in rs), max(r["wavelength_um"] for r in rs)),
             "naive": wmean([r["aperture_flux_ujy"] for r in rs], [r["aperture_err_ujy"] for r in rs]),
             "target": wmean([r["flux_ujy"] for r in rs], [r["flux_err_ujy"] for r in rs]), "nb": {}}
        for nid, nrows in fp["neighbour_spectra"].items():
            ns = [r for r in nrows if detector_of_channel(r["channel"]) == det]
            d["nb"][nid] = wmean([r["flux_ujy"] for r in ns], [r["flux_err_ujy"] for r in ns])
        out.append(d)
    return out


def saturation_stats(fp: dict) -> dict:
    pts = fp["points"]
    n = len(pts)
    sat = sum("SATURATED_CORE" in p["flags"] for p in pts)
    return {"n": n, "saturated": sat, "bad": sum(p["quality"] == "bad" for p in pts),
            "good": sum(p["quality"] == "good" for p in pts), "suspect": sum(p["quality"] == "suspect" for p in pts),
            "good_channels": sum(s["quality"] == "good" for s in fp["spectrum"]), "channels": len(fp["spectrum"]),
            "no_usable": n > 0 and sat / n > NO_SPECTRUM_SAT_FRAC}


def pm_by_detector(res: Result, min_pairs: int = 1) -> dict[int, float]:
    by: dict[int, list[float]] = defaultdict(list)
    for r in res.rows:
        pm = row_pm(r)
        if pm:
            by[r["detector"]].append(math.hypot(*pm))
    return {d: float(np.mean(v)) for d, v in sorted(by.items()) if len(v) >= min_pairs}


def compute_caveats(res: Result) -> list[str]:
    """Things the checks in this report found that a reader should know before trusting a number."""
    out: list[str] = []
    if not res.ok:
        return out
    c, rows = res.case, res.rows
    if c.pm:
        cat = math.hypot(*c.pm)
        per = pm_by_detector(res, min_pairs=2)
        if len(per) >= 2:
            spread = (max(per.values()) - min(per.values())) / cat
            if spread > SPREAD_FLAG:
                lo, hi = min(per, key=per.get), max(per, key=per.get)
                out.append(f"{c.name}: the speed measured per detector ranges from {per[lo]:,.0f} mas/yr (D{lo}) to "
                           f"{per[hi]:,.0f} mas/yr (D{hi}), a spread of {100 * spread:.0f}% of the catalogue speed (detectors with two or "
                           "more pairs). One star has one speed, so this is a systematic"
                           + (" of the saturated-core pattern fits" if any("saturated" in (r.get("measure", {}).get("aperture") or "") for r in rows) else "")
                           + "; its cause is not determined here and the combined value averages over it.")
        pairs = [p for p in map(row_pm, rows) if p]
        if len(pairs) >= 3:
            ang = [angle_between_deg(p, c.pm) for p in pairs]
            n_off = sum(1 for a in ang if a is not None and a > 30)
            if n_off:
                out.append(f"{c.name}: {n_off} of {len(pairs)} single-pair motions differ from the catalogue direction by more "
                           "than 30 deg; the combined direction is a mean over pairs whose errors partly cancel.")
        fits = [r["measure"]["fit"]["chi2_red"] for r in rows if r.get("measure", {}).get("fit")]
        if fits and float(np.median(fits)) > 5:
            out.append(f"{c.name}: the saturated-core pattern fits have median reduced chi2 {np.median(fits):.0f} "
                       f"(range {min(fits):.0f}-{max(fits):.0f}), so their quoted shift significances are overconfident; "
                       "the mover verdict rests on shifts far larger than the fit errors, not on the sigma values.")
    movers = [r for r in rows if r["kind"] == "mover" and r.get("dflux_sigma") is not None]
    big = [r for r in movers if abs(r["dflux_sigma"]) >= changes.VAR_SIGMA]
    if c.expected == "mover" and big:
        out.append(f"{c.name}: {len(big)} of {len(movers)} mover pairs also change brightness by {changes.VAR_SIGMA:g} sigma or more "
                   f"(flux ratios {min(r['flux_ratio'] for r in big):.2f}-{max(r['flux_ratio'] for r in big):.2f}). "
                   "The verdict is 'mover' because the shift test takes precedence; the brightness changes are not interpreted.")
    if res.fp and c.expected == "variable":
        for x in lc_vs_rows(rows, res.fp["light_curves"]):
            if x["verdict"] == "DISAGREE":
                out.append(f"{c.name}: in D{x['det']} the stack differences say the flux changed by x{x['r_diff']:.2f} "
                           f"(season {x['sb']} / season {x['sa']}) while the PSF light curves say x{x['r_lc']:.2f} "
                           f"(median of {x['n_bins']} bins): opposite directions, so the direction of the change in that detector is unreliable.")
        for o in lc_outliers(res.fp["light_curves"]):
            out.append(f"{c.name}: light-curve bin {o['bin']} ({o['lam'][0]:.2f}-{o['lam'][1]:.2f} µm) in season {o['season']} "
                       f"is {o['flux'] / 1e6:.2f} Jy against {o['neighbours'][0] / 1e6:.2f} and {o['neighbours'][1] / 1e6:.2f} Jy in the "
                       "adjacent bins: an implausible jump for a smooth spectrum; treat that bin as a failed fit.")
    return out


def limitations(res: Result) -> list[str]:
    c, rows, job = res.case, res.rows, res.job
    out = []
    wings = [r for r in rows if "saturated" in (r.get("measure", {}).get("aperture") or "")]
    if wings:
        out.append(f"Saturation: {len(wings)} of {len(rows)} comparisons use the PSF wings because the core is saturated "
                   "in the stacks; fluxes from wings are biased and their error bars are underestimated.")
    if res.fp:
        s = saturation_stats(res.fp)
        if s["saturated"]:
            out.append(f"Saturation in single exposures: {s['saturated']} of {s['n']} exposures have a saturated core "
                       f"({s['bad']} unusable, {s['suspect']} suspect, {s['good']} good).")
    nc = [r for r in rows if r.get("no_comparison")]
    shallow = [r for r in rows if r["kind"] == "uncertain" and not r.get("no_comparison") and "shallow" in r.get("reason", "")]
    if nc or shallow:
        out.append(f"Not comparable: {len(nc)} pairs have no wavelength bin with {MIN_PER_SEASON} or more exposures in both "
                   f"seasons, and {len(shallow)} more have one epoch too shallow for the source to be measured. "
                   "Missing coverage is not evidence of anything.")
    inc = [r for r in rows if r.get("reason") == changes.INCONSISTENT]
    if inc:
        out.append(f"{len(inc)} pairs were relabelled because their centroid shifts disagreed with the other pairs "
                   "(blend, saturation or PSF change).")
    if res.fp and res.fp.get("blend_warning"):
        out.append("Blending: " + res.fp["blend_note"])
    if job.failed:
        fs = failure_summary(job)
        out.append(f"{len(job.failed)} of {job.total} cutouts failed to download "
                   + "(" + ", ".join(f"{d}: {n}x {e}" for d, n, e in fs) + "); they are missing from every number above.")
    cov = coverage_counts(job)
    thin = [f"D{d} season {s} ({cov.get((s, d), 0)})" for s in sorted({x["season"] for x in job.seasons})
            for d in range(1, 7) if cov.get((s, d), 0) < MIN_PER_SEASON]
    if thin:
        out.append(f"Fewer than {MIN_PER_SEASON} exposures (cannot form a wavelength-matched bin): " + ", ".join(thin) + ".")
    out.append(f"Only {len(job.seasons)} seasons exist; two or three epochs cannot give a light curve or an orbit.")
    out += list(c.context)
    return out


# ============================================================================ git / formatting
def git_info() -> str:
    try:
        h = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO, capture_output=True, text=True,
                           timeout=15).stdout.strip()
        dirty = subprocess.run(["git", "diff", "--quiet", "HEAD", "--", "backend/app"], cwd=REPO,
                               timeout=15).returncode != 0
        return (f"`{h}`" + (" (backend/app has uncommitted changes)" if dirty else "")) if h else "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


def season_dates(job: FieldJob) -> str:
    if not job.seasons:
        return "-"
    return f"{min(s['start'] for s in job.seasons)} to {max(s['end'] for s in job.seasons)}"


def releases(job: FieldJob) -> str:
    c = Counter(f["collection"] for f in job.frames.values())
    return ", ".join(f"{k} ({n})" for k, n in sorted(c.items())) or "-"


def pipelines(job: FieldJob) -> str:
    tags = Counter(psfmod.pipeline_tag(f.get("source_url", "")) for f in job.frames.values())
    return ", ".join(f"{k} ({n})" for k, n in sorted(tags.items())) or "-"


def n_exposures(job: FieldJob) -> int:
    return len({f["obs_id"] for f in job.frames.values()})


# ============================================================================ sections
def measured_text(res: Result) -> str:
    c = res.case
    if not res.ok:
        return "field not cached"
    t = res.target
    if c.expected == "mover":
        if "pmra_masyr" not in t:
            return "no coherent motion measured"
        m = (t["pmra_masyr"], t["pmdec_masyr"])
        cmp = compare_pm(m, c.pm)
        return (f"{cmp['total_meas']:,.0f} mas/yr ({m[0]:+,.0f}, {m[1]:+,.0f}) vs catalogue {cmp['total_cat']:,.0f} mas/yr "
                f"({c.pm[0]:+,.0f}, {c.pm[1]:+,.0f}): speed {cmp['pct_diff']:+.1f}%, direction {cmp['angle_deg']:.1f} deg")
    if c.expected == "variable":
        v = variable_summary(res.rows)
        if not v["n"]:
            return "no variable pair"
        s = (f"flux ratio (later/earlier season) {v['rmin']:.2f}-{v['rmax']:.2f} in {v['n']} of {v['n_rows']} pairs; "
             f"{fmt_intervals(v['spans'])} (D{', D'.join(map(str, v['dets']))})")
        sta = [f"D{r['detector']} {r['season_a']}-{r['season_b']}" for r in res.rows if r["kind"] == "static"]
        if sta:
            s += "; no significant change in " + ", ".join(sta)
        return s
    cnt = Counter(x["evidence"] for x in res.nontarget)
    return (f"no source at the field centre; {len(res.nontarget)} other candidates "
            f"(strong {cnt['strong']}, moderate {cnt['moderate']}, weak {cnt['weak']})")


def outcome(res: Result) -> str:
    c, s = res.case, res.target["summary"]
    if c.expected in ("mover", "variable"):
        return "as expected" if s == c.expected else "NOT as expected"
    firm = [x for x in res.nontarget if x["evidence"] in ("strong", "moderate")]
    return "as expected" if s not in ("mover", "variable") and not firm else "NOT as expected"


def classification_text(res: Result) -> str:
    """Spectral classification of the target from its fingerprint ('-' when no fingerprint is cached)."""
    c = (res.fp or {}).get("classification")
    if not c:
        return "-"
    return f"{c.get('label', '-')} ({c.get('confidence', 'unknown')} confidence)"


def summary_section(results: list[Result]) -> list[str]:
    rows = []
    for r in results:
        if not r.ok:
            rows.append([r.case.name, r.case.expected, "field not cached", "-", "-", "-", "-", "-", "-"])
            continue
        t, j = r.target, r.job
        verdict = "no detection at the field centre" if t["summary"] == "no data" else t["summary"]
        rows.append([
            r.case.name, r.case.expected, f"{verdict} ({outcome(r)})", classification_text(r),
            f"{t['evidence']} ({t['n_independent']} independent comparison(s) in {t['n_detectors_agree']} detector(s))",
            measured_text(r),
            f"{len(j.frames)} of {j.total} images ({n_exposures(j)} exposures)",
            f"{len(j.seasons)} seasons, {season_dates(j)}", releases(j),
        ])
    L = [md_table(["Target", "Expected", "Verdict", "Spectral class", "Evidence", "Measured vs expected", "Data used", "Seasons", "Release (images)"],
                  rows), ""]
    L.append("Flux ratios are later season divided by earlier season. 'Images' are single-detector cutouts of one "
             "exposure (the target falls on several detectors per exposure). Evidence levels rank candidates; they are "
             "not probabilities (section 5).")
    cav = [x for r in results for x in r.caveats]
    if cav:
        L += ["", "**Problems the checks in this report found** (details in section 2 and 3):", ""]
        L += [f"- {x}" for x in cav]
    return L


def coverage_table(job: FieldJob) -> str:
    cov = coverage_counts(job)
    rows = []
    for s in sorted(job.seasons, key=lambda x: x["season"]):
        n = [cov.get((s["season"], d), 0) for d in range(1, 7)]
        rows.append([s["season"], f"{s['start']} to {s['end']}", *n, sum(n)])
    tot = [sum(cov.get((s["season"], d), 0) for s in job.seasons) for d in range(1, 7)]
    rows.append(["all", "", *tot, sum(tot)])
    return md_table(["Season", "Dates"] + [f"D{d}" for d in range(1, 7)] + ["Total"], rows, ["r", "l"] + ["r"] * 7)


def method_text(r: dict) -> str:
    if r.get("no_comparison"):
        return "-"
    ap = (r.get("measure") or {}).get("aperture") or ""
    return f"{r.get('method') or 'centroid + aperture'}; {ap}" if ap else (r.get("method") or "-")


def rows_table(res: Result) -> str:
    c = res.case
    headers = ["Det", "Seasons", "Kind", "Shift (px)", "Shift (σ)", "Flux ratio", "Flux change (σ)", "λ compared (µm)", "Method"]
    align = ["r", "c", "l", "r", "r", "r", "r", "l", "l"]
    if c.pm:
        headers += ["PM of pair (mas/yr)", "vs catalogue"]
        align += ["r", "r"]
    headers.append("Note")
    align.append("l")
    out = []
    for r in res.rows:
        lam = f"{r['lam_lo_um']:.2f}-{r['lam_hi_um']:.2f}" if r.get("lam_lo_um") is not None else None
        row = [f"D{r['detector']}", f"{r['season_a']}-{r['season_b']}", r["kind"], fnum(r.get("shift_px"), 2),
               fnum(r.get("shift_sigma"), 1), fnum(r.get("flux_ratio"), 2), fnum(r.get("dflux_sigma"), 1, plus=True),
               lam, method_text(r)]
        if c.pm:
            pm = row_pm(r)
            if pm:
                cmp = compare_pm(pm, c.pm)
                row += [f"{cmp['total_meas']:,.0f}", f"{cmp['pct_diff']:+.0f}%, {cmp['angle_deg']:.0f} deg"]
            else:
                row += [None, None]
        row.append(r.get("reason") or ("not comparable" if r.get("no_comparison") else ""))
        out.append(row)
    return md_table(headers, out, align)


def bins_table(res: Result) -> str:
    used = {b for r in res.rows for b in r.get("bins", [])}
    rows = []
    for b in res.an["bins"]:
        cnt = ", ".join(f"S{s}: {n}" for s, n in sorted(b["counts"].items(), key=lambda kv: int(kv[0])))
        rows.append([b["id"], f"D{b['detector']}", f"{b['lam_lo_um']:.3f}-{b['lam_hi_um']:.3f}", f"{b['width_um']:.3f}",
                     f"{b['resolving_power']:.1f}", b["n_channels"], cnt, "yes" if b["id"] in used else "no"])
    return md_table(["Bin", "Det", "λ range (µm)", "Width (µm)", "R = λ/Δλ", "Channels", "Exposures per season",
                     "Used by target"], rows, ["l", "l", "l", "r", "r", "r", "l", "c"])


def case_section(res: Result) -> list[str]:
    c = res.case
    L = [f"### {c.name}", ""]
    if not res.ok:
        return L + [f"The field is not in the cache (key `{res.key}`); nothing could be computed."]
    t, j = res.target, res.job
    L.append(f"- **Expected:** {c.expectation}.")
    L.append(f"- **Field:** centre RA {c.ra}, Dec {c.dec} (the position at MJD {SPHEREX_REF_MJD:.0f}), {FOV_ARCMIN:g} arcmin "
             f"square, {res.grid.n} x {res.grid.n} px of {res.grid.pixscale_arcsec} arcsec; cache key `{res.key}`.")
    if c.pm:
        L.append(f"- **Catalogue proper motion used:** ({c.pm[0]:+,.1f}, {c.pm[1]:+,.1f}) mas/yr = "
                 f"{math.hypot(*c.pm):,.0f} mas/yr (RA*cos(Dec), Dec).")
    L.append(f"- **Result:** {t['summary']} ({outcome(res)}); evidence {t['evidence']}. {measured_text(res)}.")
    L.append(f"- **Data:** {releases(j)}; pipeline {pipelines(j)}.")
    L += ["", "#### Data coverage (images per season and detector)", "", coverage_table(j), ""]
    if j.failed:
        L += [f"Failed downloads: {len(j.failed)} of {j.total} cutouts (" +
              ", ".join(f"{d}: {n}x {e}" for d, n, e in failure_summary(j)) + ").", ""]
    L += ["#### Measurements at the field centre (one row per detector and season pair)", "", rows_table(res), ""]
    L.append("Seasons are numbered from 0; flux ratio is later/earlier; 'flux change' is the significance of the flux "
             "difference. Pairs marked 'not comparable' have no wavelength bin shared by both seasons.")
    mc = t.get("motion_check") or {}
    L += ["", "**Motion coherence.** "
          + (f"{mc['n_movers']} mover measurements; direction coherence R = {mc['coherence']:.2f} (1 = identical directions, "
             f"threshold {changes.MOVER_MIN_COHERENCE}); fractional speed scatter {100 * mc['speed_scatter']:.0f}% "
             f"(threshold {100 * changes.MOVER_MAX_SPEED_SCATTER:.0f}%); "
             f"{'coherent' if mc['coherent'] else 'NOT coherent'}."
             if mc.get("coherent") is not None else
             f"{mc.get('n_movers', 0)} mover measurement(s); too few to check coherence.")]
    if c.pm:
        per = pm_by_detector(res)
        if per:
            cat = math.hypot(*c.pm)
            L.append("Mean speed per detector: " + "; ".join(f"D{d} {v:,.0f} mas/yr ({100 * (v - cat) / cat:+.0f}%)"
                                                             for d, v in per.items()) + ".")
    L += ["", "#### Evidence checks", "",
          md_table(["Check", "Result", "Note"],
                   [[k["label"], "pass" if k["ok"] else "fail", k.get("note", "")] for k in t["checks"]]), "",
          f"Level: **{t['evidence']}** ({t['n_independent']} independent season comparison(s), "
          f"{t['n_detectors_agree']} detector(s) agreeing with the verdict). A level is a ranking aid, not a probability.",
          "", "#### Wavelength bins", "", bins_table(res), "",
          "Comparisons only ever difference stacks of the same bin. Bins start from the 102 channels and merge until every "
          f"season that can contribute has {MIN_PER_SEASON} or more exposures; R is the resolving power of the bin.", "",
          "#### Limitations that apply", ""]
    L += [f"- {x}" for x in limitations(res)]
    L.append("")
    return L


# ---------------------------------------------------------------------------- spectra
def deblend_section(res: Result, fig: str | None) -> list[str]:
    fp = res.fp
    dm = det_means(fp)
    nbs = [s for s in fp["sources"] if s["role"] == "neighbour"]
    headers = ["Det", "λ range (µm)", "Channels", "Naive aperture (µJy)", "Deblended target (µJy)", "Target S/N"]
    for s in nbs:
        headers.append(f"Neighbour {s['id']} (G={fnum(s['g_mag'], 1)}, {s['sep_arcsec']:.1f}\") (µJy)")
    rows, undetected = [], []
    for d in dm:
        tg = d["target"]
        snr = tg[0] / tg[1] if tg else None
        if snr is None or snr < DETECTED_SNR:
            undetected.append(d)
        rows.append([f"D{d['det']}", f"{d['lam'][0]:.2f}-{d['lam'][1]:.2f}", d["n"],
                     fflux(*d["naive"][:2]) if d["naive"] else None, fflux(*tg[:2]) if tg else None, fnum(snr, 1)]
                    + [fflux(*d["nb"][s["id"]][:2]) if d["nb"].get(s["id"]) else None for s in nbs])
    L = [f"#### {res.case.name}: naive aperture against deblended fit", "",
         md_table(headers, rows, ["l", "l", "r", "r", "r", "r"] + ["r"] * len(nbs)), "",
         "Each cell is the inverse-variance weighted mean over the channels of one detector, with the formal error of that "
         "mean; the spectral shape inside a detector is not removed, so these are band averages, not a spectrum "
         f"(the spectrum is in the figure). Target S/N below {DETECTED_SNR:g} counts as not detected.", ""]
    if undetected:
        names = ", ".join(f"D{d['det']}" for d in undetected)
        naive = [d["naive"][0] for d in undetected if d["naive"]]
        L.append(f"- In {names} the deblended fit finds no target flux, yet the naive aperture reports "
                 f"{min(naive):,.0f} to {max(naive):,.0f} µJy: that is the neighbour's light (compare the neighbour column).")
    det = [d for d in dm if d not in undetected and d["naive"] and d["target"]]
    if det:
        ratios = [d["naive"][0] / d["target"][0] for d in det if d["target"][0] > 0]
        if ratios:
            L.append(f"- Where the target is detected ({', '.join('D' + str(d['det']) for d in det)}) the naive aperture is "
                     f"{min(ratios):.2f}-{max(ratios):.2f} times the deblended flux.")
    L.append(f"- {fp['blend_note']}")
    if fig:
        L += ["", f"![Deblended spectrum]({fig})", "",
              "*Figure: deblended target flux (points, 1σ), naive aperture flux (grey line) and the nearest neighbour "
              "(blue line) against wavelength.*"]
    return L + [""]


def methane_section(results: list[Result], res: Result, fig: str | None) -> list[str]:
    fp = res.fp
    d = band_depth(fp["spectrum"])
    L = [f"#### {res.case.name}: methane absorption", ""]
    if d is None:
        return L + ["The 3.1-3.6 µm band or one of its continuum windows has too few usable channels; no depth was measured.", ""]
    rows = [["Blue continuum", f"{CONTINUUM_BLUE[0]}-{CONTINUUM_BLUE[1]}", d["blue"][2], fflux(d["blue"][0], d["blue"][1])],
            ["Methane band", f"{METHANE_BAND[0]}-{METHANE_BAND[1]}", d["band"][2], fflux(d["band"][0], d["band"][1])],
            ["Red continuum", f"{CONTINUUM_RED[0]}-{CONTINUUM_RED[1]}", d["red"][2], fflux(d["red"][0], d["red"][1])],
            ["Mean of the two continua", "", "", fflux(d["cont"][0], d["cont"][1])]]
    L += [md_table(["Window", "λ (µm)", "Channels", "Weighted mean flux (µJy)"], rows, ["l", "l", "r", "r"]), "",
          f"**Band / continuum = {d['ratio']:.3f} ± {d['err']:.3f}** (a flux deficit of {100 * (1 - d['ratio']):.0f}% in the "
          f"3.1-3.6 µm band). The error is statistical only: the two continuum windows lie in different detectors "
          "(D4 and D5), so an inter-detector calibration offset of a few percent is not included, and the windows "
          "are not corrected for spectral slope.", ""]
    comps = []
    for r in results:
        if not r.fp or r is res or saturation_stats(r.fp)["no_usable"]:
            continue
        for s in r.fp["sources"]:
            spec = fp_spectrum_of(r.fp, s)
            dd = band_depth(spec) if spec else None
            if dd and dd["err"] / dd["ratio"] < 0.5:
                label = (f"{r.case.name}: " + ("the target" if s["role"] == "target" else
                         f"neighbour {s['id']} (Gaia G={fnum(s['g_mag'], 1)}, {s['sep_arcsec']:.1f}\" away)"))
                comps.append((dd["err"] / dd["ratio"], label, dd, s["role"] == "target"))
    comps.sort(key=lambda x: x[0])
    if comps:
        L += ["Same index for other sources fitted in the cached fingerprints (where measurable to 50% or better):", "",
              md_table(["Source", "Band / continuum", "Expected"],
                       [[lab, f"{dd['ratio']:.2f} ± {dd['err']:.2f}",
                         "methane (Y dwarf)" if tgt else "no methane (ordinary star, not verified spectroscopically)"]
                        for _, lab, dd, tgt in comps[:5]]), "",
              "A smooth stellar spectrum should give a ratio near 1; the offset of a comparison source from 1 shows how much "
              "of the index is due to the continuum windows and the detector boundary rather than methane.", ""]
    else:
        L += ["No other source in the cached fingerprints has usable flux in all three windows, so there is no "
              "comparison measurement where no methane is expected; the depth above is not validated against a control.", ""]
    if fig:
        L += [f"![Epsilon Indi B spectrum]({fig})", "",
              "*Figure: deblended spectrum with the three windows shaded (blue continuum, methane band, red continuum).*", ""]
    return L


def fp_spectrum_of(fp: dict, src: dict) -> list[dict] | None:
    return fp["spectrum"] if src["role"] == "target" else fp["neighbour_spectra"].get(src["id"])


def no_spectrum_lines(res: Result) -> list[str]:
    s = saturation_stats(res.fp)
    if s["channels"] == 0:
        what = "no channel could be measured"
    else:
        what = f"only {s['good_channels']} of {s['channels']} measured channels are 'good', the rest come from wings-only fits"
    return [f"- **{res.case.name}: no usable spectrum.** {s['saturated']} of {s['n']} exposures have a saturated core "
            f"({s['bad']} unusable, {s['suspect']} suspect, {s['good']} good); {what}. Fluxes fitted to unsaturated wings "
            "are biased high (the fingerprint notes quote up to about 2x), so no spectrum is shown or interpreted."]


def lightcurve_section(res: Result) -> list[str]:
    fp = res.fp
    L = [f"#### {res.case.name}: same-wavelength light curve", ""]
    lcs = fp["light_curves"] if fp else []
    if not lcs:
        why = (f"all {len(fp['points'])} exposures are unusable for PSF photometry (saturated core)"
               if fp else f"no fingerprint ({res.fp_note})")
        return L + [f"No light curve could be built: {why}. The brightness evidence is the season-difference measurement "
                    "alone (section 2), which is **inconclusive on the amount and the wavelength dependence** of the change "
                    "because it also relies on saturated wings.", ""]
    seasons = sorted({p["season"] for lc in lcs for p in lc["points"]})
    rows, counts = [], Counter()
    for lc in lcs:
        label, why = lc_judgement(lc)
        counts[label] += 1
        by = {p["season"]: p for p in lc["points"]}
        pv = sstats.chi2.sf(lc["chi2_const"], lc["dof"]) if lc.get("chi2_const") is not None and lc["dof"] else None
        rows.append([lc["bin"], f"D{lc['detector']}", f"{lc['lam_lo_um']:.2f}-{lc['lam_hi_um']:.2f}"]
                    + [fflux(by[s]["flux_ujy"] / 1e3, by[s]["flux_err_ujy"] / 1e3) + f" (n={by[s]['n']})" if s in by else None
                       for s in seasons]
                    + [fnum(lc.get("chi2_const"), 1) + f" / {lc['dof']}" if lc.get("chi2_const") is not None else None,
                       "0" if pv is not None and pv < 1e-300 else (f"{pv:.1e}" if pv is not None else None),
                       label + (f" ({why})" if why else "")])
    L += [md_table(["Bin", "Det", "λ (µm)"] + [f"Season {s} flux (mJy)" for s in seasons] + ["χ² / dof vs constant", "p", "Judgement"],
                   rows, ["l", "l", "l"] + ["r"] * len(seasons) + ["r", "r", "l"]), "",
          f"Bins: {', '.join(f'{n} {k}' for k, n in sorted(counts.items()))}. Fluxes of one bin are compared only at channels "
          f"observed in two or more seasons (no spectral slope can mimic variability). The χ² test includes a "
          f"{fpm.LC_SYS_FRAC:.0%} systematic floor ({fpm.LC_SYS_FRAC_SUSPECT:.0%} for saturated-core fits) and calls a bin variable "
          f"at p below {fpm.LC_P_VARIABLE:g}. Absolute fluxes of saturated stars are biased high and should not be quoted as "
          "photometry; only the season-to-season pattern within a bin is informative.", ""]
    x = lc_vs_rows(res.rows, lcs)
    if x:
        L += ["**Cross-check between the two independent brightness measurements** (last season over first season):", "",
              md_table(["Det", "Seasons", "Stack difference ratio", "PSF light-curve ratio (median of bins)", "Bins", "Directions"],
                       [[f"D{r['det']}", f"{r['sb']}/{r['sa']}", fnum(r["r_diff"], 2), fnum(r["r_lc"], 2), r["n_bins"], r["verdict"]]
                        for r in x], ["l", "c", "r", "r", "r", "l"]), ""]
    return L


def spectra_section(results: list[Result], figs: dict[str, str]) -> list[str]:
    L = []
    notes = [f"- **{r.case.name}:** fingerprint unavailable ({r.fp_note})." for r in results
             if r.ok and r.case.expected != "nothing" and not r.fp]
    for r in results:
        if r.fp and r.case.expected == "mover" and r.fp.get("blend_warning") and not saturation_stats(r.fp)["no_usable"]:
            L += deblend_section(r, figs.get(r.case.slug + "-deblend"))
    for r in results:
        if r.fp and "methane" in r.case.expectation and not saturation_stats(r.fp)["no_usable"]:
            L += methane_section(results, r, figs.get(r.case.slug + "-spectrum"))
    sat = [r for r in results if r.fp and saturation_stats(r.fp)["no_usable"]]
    if sat or notes:
        L += ["#### Targets without a usable spectrum", ""]
        for r in sat:
            L += no_spectrum_lines(r)
        L += notes + [""]
    for r in results:
        if r.ok and r.case.expected == "variable":
            L += lightcurve_section(r)
    return L


# ---------------------------------------------------------------------------- other candidates
def crossmatch(res: Result) -> list[dict]:
    out = []
    for c in res.nontarget:
        meas = (float(c["pmra_masyr"]), float(c["pmdec_masyr"])) if c["kind"] == "mover" and "pmra_masyr" in c else None
        hit = nearest_star(c["ra"], c["dec"], res.gaia.stars) if res.gaia and res.gaia.ok else None
        star = None if hit is None else {"pmra": hit[0].pmra, "pmdec": hit[0].pmdec, "has_pm": hit[0].has_pm,
                                         "variable": hit[0].variable}
        out.append({"cand": c, "meas": meas, "hit": hit,
                    "cls": classify_gaia(meas, star) if res.gaia and res.gaia.ok else None})
    return out


def candidates_section(results: list[Result]) -> list[str]:
    L, rows, matches = [], [], {}
    failed = [r for r in results if r.ok and not (r.gaia and r.gaia.ok)]
    for r in results:
        if not r.ok:
            continue
        matches[r.case.slug] = crossmatch(r)
        for m in matches[r.case.slug]:
            c, hit = m["cand"], m["hit"]
            kind = f"{c['kind']} / {c['evidence']}" + (f" ({c['reason']})" if c.get("reason") else "")
            gaia_pm = f"{math.hypot(hit[0].pmra, hit[0].pmdec):,.0f}" if hit and hit[0].has_pm else None
            flag = {True: "VARIABLE", False: "CONSTANT", None: "not flagged"}[hit[0].variable] if hit else None
            rows.append([r.case.name.split(" (")[0], c["id"], kind, fnum(c["max_z"], 1),
                         f"{c['ra']:.5f}, {c['dec']:+.5f}",
                         f"{math.hypot(*m['meas']):,.0f}" if m["meas"] else None,
                         f"{hit[0].source_id} ({hit[1]:.1f}\")" if hit else None, gaia_pm,
                         f"{fnum(hit[0].g_mag, 1)}, {flag}" if hit else None, m["cls"] or "Gaia unavailable"])
    if failed:
        L += ["**Gaia DR3 cross-match unavailable for: " + ", ".join(r.case.name for r in failed) + ".** Reasons: "
              + "; ".join(sorted({r.gaia.note or 'unknown' for r in failed})) + ". Candidates in those fields are listed "
              "without classification.", ""]
    L += ["Every candidate found in the six fields other than the target itself (the target is the source at the field "
          f"centre). Each is matched to the nearest Gaia DR3 source within {MATCH_ARCSEC:g} arcsec, with Gaia positions moved "
          f"to MJD {SPHEREX_REF_MJD:.0f} by their catalogue proper motion. Candidates labelled `uncertain` were demoted by the "
          "pipeline (residuals of a bright or saturated source).", "",
          "Classes: (1) " + CLS_HIGH_PM + f" = the measured motion agrees with the Gaia motion within {PM_DIR_TOL_DEG:g} deg "
          f"and a factor {PM_SPEED_FACTOR:g} in speed; (2) " + CLS_VARIABLE + " = Gaia phot_variable_flag is VARIABLE; (3) "
          + CLS_STATIC + "; (4) " + CLS_PM_MISMATCH + f" (Gaia motion at least {GAIA_MOVING_MASYR:g} mas/yr); (5) " + CLS_NONE + ".", ""]
    if rows:
        L += [md_table(["Field", "Cand.", "Kind / evidence", "Peak σ", "RA, Dec (deg)", "Measured PM (mas/yr)",
                        "Nearest Gaia DR3 (separation)", "Gaia PM (mas/yr)", "G, variability flag", "Classification"],
                       rows, ["l", "l", "l", "r", "l", "r", "l", "r", "l", "l"]), ""]
    else:
        L += ["No non-target candidates were found.", ""]
    # counts
    cnt_rows, tot = [], Counter()
    for r in results:
        if not r.ok:
            continue
        ev = Counter(x["evidence"] for x in r.nontarget)
        phys = sum(x["kind"] in ("mover", "variable") for x in r.nontarget)
        cnt_rows.append([r.case.name.split(" (")[0], *[ev[k] for k in LEVELS], len(r.nontarget), phys])
        tot.update(ev)
        tot["n"] += len(r.nontarget)
        tot["phys"] += phys
    cnt_rows.append(["**all fields**", *[tot[k] for k in LEVELS], tot["n"], tot["phys"]])
    L += ["#### False-alarm counts (non-target candidates per 5 x 5 arcmin field)", "",
          md_table(["Field", "Strong", "Moderate", "Weak", "All", "Labelled mover or variable"], cnt_rows,
                   ["l", "r", "r", "r", "r", "r"]), ""]
    if any(r.gaia and r.gaia.ok for r in results):
        short = ["High-PM star", "Gaia variable", "Static Gaia star", "PM mismatch", "No Gaia match"]
        rows2, ctot = [], Counter()
        for r in results:
            if r.ok and r.case.slug in matches and r.gaia and r.gaia.ok:
                c = Counter(m["cls"] for m in matches[r.case.slug])
                rows2.append([r.case.name.split(" (")[0], *[c[k] for k in CLASSES]])
                ctot.update(c)
        rows2.append(["**all fields**", *[ctot[k] for k in CLASSES]])
        firm = [(r, m) for r in results if r.ok and r.case.slug in matches for m in matches[r.case.slug]
                if m["cand"]["evidence"] in ("strong", "moderate") and m["cls"]]
        unexplained = [(r, m) for r, m in firm if m["cls"] in (CLS_STATIC, CLS_NONE, CLS_PM_MISMATCH)]
        L += ["#### Gaia classification counts", "", md_table(["Field", *short], rows2, ["l"] + ["r"] * 5), "",
              f"Of {len(firm)} non-target candidates with moderate or strong evidence, {len(firm) - len(unexplained)} are "
              f"explained by a known high-proper-motion star or a Gaia variable and {len(unexplained)} are not "
              "(" + (", ".join(f"{r.case.name.split(' (')[0]} {m['cand']['id']}" for r, m in unexplained) or "none") + "). "
              "In crowded fields a Gaia source lies within the match radius of a random position by chance fairly often, so "
              "'matches a static Gaia star' does not prove the candidate is that star.", ""]
    ctrl = next((r for r in results if r.case.expected == "nothing" and r.ok), None)
    if ctrl:
        cnt = Counter(x["evidence"] for x in ctrl.nontarget)
        L += [f"**Control field.** {len(ctrl.nontarget)} candidates (strong {cnt['strong']}, moderate {cnt['moderate']}, "
              f"weak {cnt['weak']}) in a field with no known mover or variable, from {len(ctrl.job.frames)} of {ctrl.job.total} images "
              + (f"({len(ctrl.job.failed)} failed to download, so coverage is thinner than elsewhere). "
                 if ctrl.job.failed else "(every image downloaded). ") +
              "One field is a count of one; it is not a false-alarm rate.", ""]
    return L


# ---------------------------------------------------------------------------- method
def method_section() -> list[str]:
    px = settings.pixel_scale_arcsec
    return [
        f"- **Wavelength-matched comparison.** SPHEREx observes a point at a different wavelength in every exposure, so a red "
        f"star seems to 'change' if two epochs sample different wavelengths. Exposures are grouped per detector into adaptive "
        f"wavelength bins (at least {MIN_PER_SEASON} exposures per season in each bin); only stacks of the same bin are "
        "compared, and per-bin differences are then combined per detector.",
        f"- **Detection and classification.** Differences are normalised to a robust sigma; lobes above {changes.DETECT_SIGMA:g} sigma "
        f"in a PSF-matched map are measured in the season stacks. A *mover* has a centroid shift of at least "
        f"{changes.SHIFT_SIGMA:g} sigma and {changes.MIN_SHIFT_PX} px; a *variable* a flux change of at least {changes.VAR_SIGMA:g} sigma and "
        f"{changes.VAR_MIN_FRAC:.0%} (with a {changes.CAL_FLOOR:.0%} calibration floor) without such a shift. Movers must agree in direction "
        f"(R at least {changes.MOVER_MIN_COHERENCE}) and speed (scatter at most {changes.MOVER_MAX_SPEED_SCATTER:.0%}) across measurements or they are relabelled.",
        "- **Evidence levels** are the number of explicit checks passed (seen in two or more detectors; two or more independent season "
        "comparisons; coherent motion for movers). They rank candidates. They are not probabilities and have not been "
        "calibrated against a labelled sample; the six fields here are too few to do so.",
        "- **Independent comparisons.** Season pairs share stacks, so three seasons give two independent comparisons, not three.",
        f"- **Resolution.** Pixels are {px} arcsec and the PSF about two pixels wide. Sources closer than that are not resolved "
        "(Epsilon Indi B is two objects in one pixel). Sub-pixel centroid shifts are measured to a few hundredths of a pixel in "
        "good cases, but the pipeline adds a registration floor of "
        f"{changes.REG_FLOOR_PX} px per epoch and the real accuracy is worse for saturated or blended sources.",
        "- **Saturation.** Bright stars saturate the detector core. Their position and brightness are then derived from PSF wings, "
        "which are biased and have underestimated errors; the report says so wherever it applies.",
        f"- **Seasons.** There are two to four widely separated epochs per field (this is the survey's cadence, not a choice), so there is "
        "no light curve in the sense of a time series, and the proper-motion baseline is at most about a year.",
        "- **Proper-motion comparison.** 'Speed' and 'direction' differences compare our combined measurement with a catalogue value "
        "supplied as input. They are differences, not uncertainties: the measured value has no independent error bar here beyond "
        "the pair-to-pair scatter listed in section 2.",
        "- **Gaia cross-match.** Gaia DR3 through IRSA TAP; nearest source within the match radius after propagating by catalogue proper "
        "motion, no magnitude cut.",
        "- **Status of the objects.** All six fields are known objects (or a deliberately quiet field) chosen to test the tool. "
        "Nothing here is a discovery claim.",
    ]


# ---------------------------------------------------------------------------- figures
def nice_ticks(lo: float, hi: float, n: int = 6) -> list[float]:
    raw = (hi - lo) / n
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    start = math.ceil(lo / step) * step
    return [start + i * step for i in range(int((hi - start) / step + 1e-9) + 1)]


def svg_plot(series: list[dict], title: str, xlim: tuple[float, float], ylim: tuple[float, float],
             shades: list[tuple[float, float, str, str]] = (), xlabel: str = "Wavelength (µm)",
             ylabel: str = "Flux (µJy)", width: int = 720, height: int = 340) -> str:
    """Tiny dependency-free SVG line/point plot. series: label, x, y, err (optional), color, kind (line|points)."""
    from html import escape
    ml, mr, mt, mb = 74, 16, 30, 44
    pw, ph = width - ml - mr, height - mt - mb
    sx = lambda x: ml + (x - xlim[0]) / (xlim[1] - xlim[0]) * pw
    sy = lambda y: mt + ph - (min(max(y, ylim[0]), ylim[1]) - ylim[0]) / (ylim[1] - ylim[0]) * ph
    o = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
         f'font-family="Helvetica, Arial, sans-serif" font-size="12">',
         f'<rect width="{width}" height="{height}" fill="#ffffff"/>',
         f'<text x="{ml}" y="18" font-size="14" font-weight="bold" fill="#222">{escape(title)}</text>']
    for lo, hi, color, label in shades:
        o.append(f'<rect x="{sx(lo):.1f}" y="{mt}" width="{sx(hi) - sx(lo):.1f}" height="{ph}" fill="{color}" opacity="0.18"/>')
        o.append(f'<text x="{(sx(lo) + sx(hi)) / 2:.1f}" y="{mt + 12}" text-anchor="middle" font-size="10" fill="#444">{escape(label)}</text>')
    for t in nice_ticks(*ylim):
        o.append(f'<line x1="{ml}" x2="{ml + pw}" y1="{sy(t):.1f}" y2="{sy(t):.1f}" stroke="#ddd"/>')
        o.append(f'<text x="{ml - 6}" y="{sy(t) + 4:.1f}" text-anchor="end" fill="#444">{t:,.0f}</text>')
    for t in nice_ticks(*xlim):
        o.append(f'<line x1="{sx(t):.1f}" x2="{sx(t):.1f}" y1="{mt}" y2="{mt + ph}" stroke="#eee"/>')
        o.append(f'<text x="{sx(t):.1f}" y="{mt + ph + 16}" text-anchor="middle" fill="#444">{t:g}</text>')
    o.append(f'<rect x="{ml}" y="{mt}" width="{pw}" height="{ph}" fill="none" stroke="#888"/>')
    o.append(f'<text x="{ml + pw / 2}" y="{height - 6}" text-anchor="middle" fill="#222">{escape(xlabel)}</text>')
    o.append(f'<text transform="translate(14,{mt + ph / 2}) rotate(-90)" text-anchor="middle" fill="#222">{escape(ylabel)}</text>')
    for i, s in enumerate(series):
        pts = [(x, y, (s.get("err") or [None] * len(s["x"]))[k]) for k, (x, y) in enumerate(zip(s["x"], s["y"])) if y is not None]
        if s["kind"] == "line":
            o.append('<polyline fill="none" stroke="%s" stroke-width="1.5" points="%s"/>'
                     % (s["color"], " ".join(f"{sx(x):.1f},{sy(y):.1f}" for x, y, _ in pts)))
        else:
            for x, y, e in pts:
                if e:
                    o.append(f'<line x1="{sx(x):.1f}" x2="{sx(x):.1f}" y1="{sy(y - e):.1f}" y2="{sy(y + e):.1f}" stroke="{s["color"]}" stroke-width="1"/>')
                o.append(f'<circle cx="{sx(x):.1f}" cy="{sy(y):.1f}" r="2.6" fill="{s["color"]}"/>')
        ly = mt + 28 + 15 * i
        o.append(f'<rect x="{ml + pw - 250}" y="{ly - 9}" width="10" height="10" fill="{s["color"]}"/>')
        o.append(f'<text x="{ml + pw - 235}" y="{ly}" fill="#222">{escape(s["label"])}</text>')
    o.append("</svg>")
    return "\n".join(o)


def make_figures(results: list[Result], fig_dir: Path) -> dict[str, str]:
    """Write the SVG figures; returns {id: path relative to docs/}."""
    out: dict[str, str] = {}
    fig_dir.mkdir(parents=True, exist_ok=True)
    for r in results:
        if not r.fp or saturation_stats(r.fp)["no_usable"]:
            continue
        spec = r.fp["spectrum"]
        x = [s["wavelength_um"] for s in spec]
        if r.fp.get("blend_warning") and r.case.expected == "mover":
            nb = next((s for s in r.fp["sources"] if s["role"] == "neighbour"), None)
            series = [{"label": "naive aperture (target + neighbour)", "x": x, "y": [s["aperture_flux_ujy"] for s in spec],
                       "color": "#777777", "kind": "line"},
                      {"label": "deblended target (1σ)", "x": x, "y": [s["flux_ujy"] for s in spec],
                       "err": [s["flux_err_ujy"] for s in spec], "color": "#D55E00", "kind": "points"}]
            if nb:
                nrows = r.fp["neighbour_spectra"].get(nb["id"], [])
                series.append({"label": f"neighbour {nb['id']} (Gaia G={nb['g_mag']:.1f}, {nb['sep_arcsec']:.1f} arcsec)",
                               "x": [q["wavelength_um"] for q in nrows], "y": [q["flux_ujy"] for q in nrows],
                               "color": "#0072B2", "kind": "line"})
            ys = [v for s in series for v in s["y"] if v is not None]
            name = f"{r.case.slug}-deblend.svg"
            (fig_dir / name).write_text(svg_plot(series, f"{r.case.name}: naive aperture vs deblended fit", (0.7, 5.05),
                                                 (min(0, min(ys)) * 1.1 - 50, max(ys) * 1.08)), encoding="utf-8")
            out[r.case.slug + "-deblend"] = f"media/validation/{name}"
        if "methane" in r.case.expectation:
            series = [{"label": "deblended spectrum (1σ)", "x": x, "y": [s["flux_ujy"] for s in spec],
                       "err": [s["flux_err_ujy"] for s in spec], "color": "#D55E00", "kind": "points"}]
            ys = [s["flux_ujy"] for s in spec if s["flux_ujy"] is not None]
            name = f"{r.case.slug}-spectrum.svg"
            shades = [(*CONTINUUM_BLUE, "#0072B2", "blue"), (*METHANE_BAND, "#009E73", "methane"), (*CONTINUUM_RED, "#0072B2", "red")]
            (fig_dir / name).write_text(svg_plot(series, f"{r.case.name}: spectrum", (0.7, 5.05), (0, max(ys) * 1.1), shades), encoding="utf-8")
            out[r.case.slug + "-spectrum"] = f"media/validation/{name}"
    return out


# ---------------------------------------------------------------------------- driver
def render(results: list[Result], figs: dict[str, str]) -> str:
    for r in results:
        r.caveats = compute_caveats(r)
        if r.ok and r.fp and r.case.expected == "mover" and r.fp.get("blend_warning"):
            und = [d["det"] for d in det_means(r.fp) if not d["target"] or d["target"][0] / d["target"][1] < DETECTED_SNR]
            stat = sorted({x["detector"] for x in r.rows if x["kind"] == "static" and x["detector"] in und})
            if stat:
                r.caveats.append(f"{r.case.name}: the 'static' rows in {', '.join('D' + str(d) for d in stat)} measure the "
                                 "neighbouring star, not the target: the deblended fit finds no target flux there. "
                                 "Only the detectors where the target is detected carry motion information.")
    v = {"processing": PROCESSING_VERSION, "stack": STACK_VERSION, "analysis": analysis.ANALYSIS_VERSION,
         "fingerprint": fpm.FINGERPRINT_VERSION}
    stale = [r.case.name for r in results if r.ok and r.an.get("version") != analysis.ANALYSIS_VERSION]
    L = ["# SPHEREx Time Machine: validation report", "",
         f"Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} · commit {git_info()} · "
         f"processing v{v['processing']} · stack v{v['stack']} · analysis v{v['analysis']} · fingerprint v{v['fingerprint']} · "
         f"collections {', '.join(settings.collections)}", "",
         "This file is generated by `python -m app.report` from the cached SPHEREx data; every number is computed, none is typed "
         "by hand. Do not edit it; rerun the generator. The targets are known objects used to test the tool, and nothing "
         "here is a discovery claim.", ""]
    if stale:
        L += [f"**Warning:** the cached analysis of {', '.join(stale)} was built with another analysis version than "
              f"v{v['analysis']}; open the field in the app (it re-analyses on load) and regenerate.", ""]
    L += ["## 1. Summary", ""] + summary_section(results) + ["", "## 2. Per-target detail", ""]
    for r in results:
        L += case_section(r)
    L += ["## 3. Spectra and light curves", ""] + spectra_section(results, figs)
    L += ["## 4. Other candidates and false alarms", ""] + candidates_section(results)
    L += ["", "## 5. Method and limits", ""] + method_section() + [""]
    return "\n".join(L)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Generate docs/validation.md from the cached validation fields.")
    ap.add_argument("--out", type=Path, default=REPO / "docs" / "validation.md")
    ap.add_argument("--offline", action="store_true", help="skip the per-field Gaia cone searches")
    ap.add_argument("--compute", action="store_true", help="compute missing fingerprints in-process (needs network)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)
    results = load_all(compute=args.compute, use_network=not args.offline)
    for r in results:
        if not r.ok:
            print(f"warning: field for {r.case.slug} is not cached (key {r.key})", file=sys.stderr)
    figs = make_figures(results, args.out.parent / "media" / "validation")
    text = render(results, figs)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text, encoding="utf-8")
    print(f"wrote {args.out} ({len(text.splitlines())} lines, {len(figs)} figures)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
