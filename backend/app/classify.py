"""'What is it': spectral indices and a cautious, rule-based classification of the deblended
0.75-5 um SPHEREx spectrum built by fingerprint.py. Pure functions, no I/O.

Indices are flux-density ratios (F_nu, uJy) of inverse-variance weighted window means.
Absorption indices divide a band window by a continuum interpolated to the band centre as a
power law (straight line in log F - log lambda) through two continuum windows; a power law
reproduces a Rayleigh-Jeans slope exactly, so a featureless hot star gives ~1. SPHEREx has
R ~ 40-130, so these are broad-band depths, not line measurements, and the windows below are
chosen from the 102 channel centres (bands.BAND_CENTERS_UM) around well-known molecular bands.

The confidence is the outcome of fixed rules, NOT a probability.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

CLASSIFY_VERSION = "1"

LABEL_STAR = "star (no molecular absorption)"
LABEL_M = "M dwarf / late-type star (H2O)"
LABEL_L = "L dwarf-like"
LABEL_T = "T dwarf-like (methane)"
LABEL_Y = "Y dwarf-like (flux only at 4-5 um)"
LABEL_UNCLEAR = "galaxy/blend-like or unclear"
LABEL_NONE = "not classifiable (too faint, saturated, or blended)"

N_CHANNELS = 102
MIN_CHANNELS = N_CHANNELS // 2   # refuse when fewer than half the channels were measured
SAT_FRAC_REFUSE = 0.5            # ... or when most usable exposures have a saturated core
SAT_FRAC_CAVEAT = 0.2            # ... and cap confidence at "low" above this
SUSPECT_FRAC_REFUSE = 0.5        # most channels only from 'suspect' exposures
WING_FLAG = "saturated_wings"    # fingerprint: flux from the calibrated extended-PSF wing fit
WING_FRAC_CAP = 0.2              # cap confidence when this fraction of exposures are wing fits ...
WING_SYS_MEDIUM = 0.10           # ... at "medium" if their median calibration systematic is below this, else "low"
MIN_DETECTION_SNR = 5.0          # some broad window must be detected this well
SIG = 3.0                        # significance required for a depth / detection
MIN_DEPTH = 0.10                 # smaller depths are within systematics (PSF, slope, curvature)
STRONG_CH4_DEPTH = 0.30          # T dwarfs: 3.3 um CH4 removes most of the flux; late L only ~10-30%
BLEND_FLOOR_FRAC = 0.02          # fit systematic floor: 2% of a close neighbour's flux
CLOSE_NEIGHBOUR_ARCSEC = 18.5    # 3 SPHEREx pixels (6.15"/px)
HIGH_PM_MASYR = 200.0
# Colour thresholds (F_nu ratios). Vega zero points J 1594 Jy, K 667 Jy, [4.5] 180 Jy:
# J-K = 0.85 (mid M) -> F(1.25)/F(2.15) ~ 1.1; J-K = 1.2 (L0) -> ~0.8; Sun-like (J-K 0.4) ~ 1.7.
LK_COLOUR_MAX = 0.8              # redder than this in J/K: L-like
STAR_JK_MIN = 1.3                # bluer than this and declining: hot-star-like
STAR_J45_MIN = 3.0               # F(1.0-1.3)/F(4.4-5.0); mid M ~3, G star ~6, Rayleigh-Jeans ~17
# Y dwarfs: [3.6]-[4.5] >~ 2.5 mag (F_nu ratio >~ 6) and J-[4.5] >~ 5 mag (ratio <~ 0.09).
Y_RED_MIN = 5.0                  # F(4.4-5.0)/F(3.2-3.8), or its lower limit
Y_RED_HIGH = 8.0
Y_NIR_LIMIT = 0.2                # 3-sigma upper limit of F(1.0-1.3)/F(4.4-5.0)
Y_NIR_LIMIT_HIGH = 0.1


@dataclass(frozen=True)
class IndexDef:
    name: str
    kind: str                                  # "absorption" | "colour"
    band: tuple[float, float]
    continuum: tuple[tuple[float, float], ...]  # 2 windows (absorption) or 1 denominator (colour)
    feature: str


INDEX_DEFS: tuple[IndexDef, ...] = (
    # CH4 nu3 fundamental, the strongest methane band (T and Y dwarfs; weak in late L).
    # Band 3.20-3.48 um (4 channels); continuum on both sides avoiding the 2.7 um H2O core and
    # the 4.1 um peak: 2.87-3.03 and 3.77-3.94 um.
    IndexDef("CH4_3.3", "absorption", (3.15, 3.55), ((2.85, 3.05), (3.75, 3.95)), "CH4 3.3 um"),
    # CH4 2v3 overtone on the red side of the H peak (T dwarfs). Continuum = H peak 1.55-1.59
    # and K peak 2.04-2.14; partly overlaps the blue wing of the 1.9 um H2O band.
    IndexDef("CH4_1.6", "absorption", (1.63, 1.75), ((1.55, 1.60), (2.04, 2.14)), "CH4 1.6-1.7 um"),
    # CH4 v2+v3 combination band on the K peak's red side; continuum is extrapolated from the H and
    # K peaks. CO 2.29 um (M/L) also falls in the band, so this alone does not mean methane.
    IndexDef("CH4_2.2", "absorption", (2.19, 2.30), ((1.55, 1.60), (2.04, 2.14)), "CH4 2.2-2.3 um"),
    # H2O between the J and H peaks (all cool dwarfs; deepens from M to T).
    IndexDef("H2O_1.4", "absorption", (1.34, 1.46), ((1.22, 1.29), (1.55, 1.60)), "H2O 1.4 um"),
    # H2O between the H and K peaks.
    IndexDef("H2O_1.9", "absorption", (1.80, 1.96), ((1.55, 1.60), (2.04, 2.14)), "H2O 1.9 um"),
    # H2O fundamental region; continuum K peak and 3.77-3.94 um.
    IndexDef("H2O_2.7", "absorption", (2.55, 2.80), ((2.04, 2.14), (3.75, 3.95)), "H2O 2.7 um"),
    # CO fundamental (M/L dwarfs; also T/Y dwarfs through non-equilibrium chemistry).
    IndexDef("CO_4.6", "absorption", (4.55, 4.80), ((4.00, 4.30), (4.85, 5.00)), "CO 4.6 um"),
    # Broad colours (numerator / denominator).
    IndexDef("F4.7/F3.5", "colour", (4.40, 5.00), ((3.20, 3.80),), "[4.5]/[3.6]-like colour"),
    IndexDef("F1.25/F2.15", "colour", (1.20, 1.30), ((2.08, 2.25),), "J-peak / K colour"),
    IndexDef("F1.15/F4.7", "colour", (1.00, 1.30), ((4.40, 5.00),), "near-IR / 4.5 um colour"),
)


@dataclass
class Window:
    lo: float
    hi: float
    mean: float | None = None
    err: float | None = None      # stat (+ blend floor in quadrature)
    stat_err: float | None = None
    floor: float = 0.0
    wl: float | None = None
    n: int = 0
    below_floor: bool = False
    raw_mean: float | None = None  # before the blend-floor zeroing

    @property
    def snr(self) -> float:
        if self.mean is None or not self.err:
            return 0.0
        return self.mean / self.err


def _rows(spectrum: list[dict]) -> list[tuple[float, float, float]]:
    out = []
    for r in spectrum or []:
        wl, f, e = r.get("wavelength_um"), r.get("flux_ujy"), r.get("flux_err_ujy")
        if wl is None or f is None or e is None or not (e > 0) or not math.isfinite(f):
            continue
        out.append((float(wl), float(f), float(e)))
    return out


def _wmean(rows, lo: float, hi: float) -> tuple[float, float, float, int] | None:
    """Inverse-variance mean, its error (scaled by sqrt(chi2_red) when > 1) and mean wavelength."""
    sel = [r for r in rows if lo <= r[0] <= hi]
    if not sel:
        return None
    w = [1.0 / r[2] ** 2 for r in sel]
    sw = sum(w)
    m = sum(wi * r[1] for wi, r in zip(w, sel)) / sw
    err = 1.0 / math.sqrt(sw)
    if len(sel) > 1:
        chi2 = sum(wi * (r[1] - m) ** 2 for wi, r in zip(w, sel)) / (len(sel) - 1)
        err *= math.sqrt(max(1.0, chi2))
    wl = sum(wi * r[0] for wi, r in zip(w, sel)) / sw
    return m, err, wl, len(sel)


def window(rows, lo: float, hi: float, neighbour_rows: list | None = None) -> Window:
    win = Window(lo, hi)
    m = _wmean(rows, lo, hi)
    if m is None:
        return win
    win.mean, win.stat_err, win.wl, win.n = m[0], m[1], m[2], m[3]
    win.raw_mean = m[0]
    floor = 0.0
    for nr in neighbour_rows or []:
        nm = _wmean(nr, lo, hi)
        if nm is not None:
            floor += BLEND_FLOOR_FRAC * abs(nm[0])
    win.floor = floor
    win.err = math.hypot(win.stat_err, floor)
    if floor > 0 and abs(win.mean) < floor:   # below the deblending floor: treat as zero
        win.mean, win.below_floor = 0.0, True
    return win


def _r(x: float | None, nd: int = 3) -> float | None:
    return None if x is None or not math.isfinite(x) else round(x, nd)


def compute_index(d: IndexDef, rows, neighbour_rows=None) -> dict[str, Any]:
    out: dict[str, Any] = {"value": None, "err": None, "sigma": None, "kind": d.kind,
                           "feature": d.feature, "band_um": list(d.band),
                           "continuum_um": [list(c) for c in d.continuum], "note": None}
    b = window(rows, *d.band, neighbour_rows)
    cs = [window(rows, *c, neighbour_rows) for c in d.continuum]
    if b.mean is None or any(c.mean is None for c in cs):
        out["note"] = "no usable channels in a window"
        return out
    if any(c.mean <= 0 or c.snr < 2.0 for c in cs):
        out["note"] = "reference window not detected (S/N < 2)"
        return out
    if d.kind == "colour":
        c, s_lnc = cs[0].mean, cs[0].err / cs[0].mean
    else:
        (c1, c2) = cs
        w = math.log(b.wl / c1.wl) / math.log(c2.wl / c1.wl)
        c = math.exp((1 - w) * math.log(c1.mean) + w * math.log(c2.mean))
        s_lnc = math.hypot((1 - w) * c1.err / c1.mean, w * c2.err / c2.mean)
    r = b.mean / c
    err = math.hypot(b.err / c, r * s_lnc)
    out["value"], out["err"] = _r(r), _r(err)
    # absorption: significance of the depth (1 - value); colour: S/N of the ratio
    out["sigma"] = _r((1 - r) / err if d.kind == "absorption" else r / err, 1)
    if b.below_floor:
        out["note"] = "band flux below the 2% blend floor; treated as zero"
    return out


def _depth_sig(ix: dict | None, min_depth: float = MIN_DEPTH) -> bool:
    return bool(ix and ix["value"] is not None and 1 - ix["value"] >= min_depth and ix["sigma"] >= SIG)


def _fmt_ix(name: str, ix: dict) -> str:
    return f"{name} = {ix['value']:.2f} ± {ix['err']:.2f}"


def _close_neighbour_rows(neighbour_spectra, sources) -> list:
    if not neighbour_spectra or not sources:
        return []
    out = []
    for s in sources:
        if s.get("role") == "neighbour" and (s.get("sep_arcsec") or 1e9) < CLOSE_NEIGHBOUR_ARCSEC:
            rows = _rows(neighbour_spectra.get(s.get("id"), []))
            if rows:
                out.append(rows)
    return out


def classify(spectrum: list[dict], points: list[dict] | None = None,
             neighbour_spectra: dict[str, list[dict]] | None = None,
             sources: list[dict] | None = None,
             target_pm_masyr: float | None = None) -> dict[str, Any]:
    """Indices + a cautious label. `points`, `neighbour_spectra` and `sources` (fingerprint result
    shapes) are optional: without them saturation and blend-floor checks are skipped."""
    rows = _rows(spectrum)
    nrows = _close_neighbour_rows(neighbour_spectra, sources)
    indices = {d.name: compute_index(d, rows, nrows) for d in INDEX_DEFS}
    reasons: list[str] = []
    caveats = [
        "Confidence is the outcome of fixed rules on the indices, not a probability.",
        "Indices are broad-band ratios at SPHEREx resolution (R ~ 40-130); '-like' labels are not "
        "spectral types, which need higher-resolution spectroscopy.",
    ]
    if nrows:
        caveats.append(f"A neighbour within {CLOSE_NEIGHBOUR_ARCSEC:.0f}\" is fitted out; a systematic floor of "
                       f"{BLEND_FLOOR_FRAC:.0%} of its flux is added to every window and target flux below it is "
                       "treated as zero.")

    def result(label: str, confidence: str) -> dict[str, Any]:
        return {"label": label, "confidence": confidence, "reasons": reasons, "indices": indices,
                "caveats": caveats, "confidence_note": "rule-based, not a probability",
                "version": CLASSIFY_VERSION}

    # ---------------------------------------------------------------- refusals
    pts = [p for p in points or [] if p.get("channel") is not None]
    usable = [p for p in pts if p.get("quality") != "bad"]
    # Wing fits of saturated cores are calibrated (fingerprint.measure_wings): they are not
    # counted as distorted saturated points, but they cap the confidence below.
    wing = [p for p in usable if WING_FLAG in (p.get("flags") or [])]
    sat = [p for p in usable if {"SATURATED_CORE", "WINGS_ONLY"} & set(p.get("flags") or [])
           and WING_FLAG not in (p.get("flags") or [])]
    wing_frac = len(wing) / len(usable) if usable else 0.0
    sat_frac = len(sat) / len(usable) if usable else 0.0
    bad_frac = 1 - len(usable) / len(pts) if pts else 0.0
    n = len(rows)
    n_sus = sum(1 for r in spectrum or [] if r.get("quality") == "suspect" and not r.get("wing_fit"))
    if n < MIN_CHANNELS:
        reasons.append(f"Only {n} of {N_CHANNELS} channels have a usable measurement"
                       + (f" ({bad_frac:.0%} of exposures were rejected)" if pts else "") + ".")
        if pts and (bad_frac > 0.5 or sat):
            caveats.append("Most exposures are unusable, as for a saturated bright star.")
        return result(LABEL_NONE, "low")
    if sat_frac > SAT_FRAC_REFUSE:
        reasons.append(f"{sat_frac:.0%} of usable exposures have a saturated core (only PSF wings fitted); "
                       "near-saturation non-linearity distorts the spectrum.")
        caveats.append("Saturated: fluxes can be biased high by up to ~2x, so band depths are unreliable.")
        return result(LABEL_NONE, "low")
    if n_sus > SUSPECT_FRAC_REFUSE * n:
        reasons.append(f"{n_sus} of {n} channels come only from 'suspect' exposures.")
        return result(LABEL_NONE, "low")
    cap = "high"
    if sat_frac > SAT_FRAC_CAVEAT:
        cap = "low"
        caveats.append(f"{sat_frac:.0%} of exposures are saturated in the core; confidence capped at low.")
    if wing_frac > WING_FRAC_CAP:
        sysf = [float((p.get("wing_fit") or {}).get("sys_frac") or 1.0) for p in wing]
        med = float(sorted(sysf)[len(sysf) // 2])
        wcap = "medium" if med < WING_SYS_MEDIUM else "low"
        cap = "low" if "low" in (cap, wcap) else "medium"
        caveats.append(f"Saturated star: {wing_frac:.0%} of exposures are measured from the PSF wings with an "
                       f"empirical extended PSF (median calibration systematic {med:.0%} per exposure); "
                       f"band depths carry that uncertainty, so confidence is capped at {cap}.")

    nir = window(rows, 1.00, 1.30, nrows)
    m35 = window(rows, 3.20, 3.80, nrows)
    m47 = window(rows, 4.40, 5.00, nrows)
    best = max(nir.snr, m35.snr, m47.snr)
    if best < MIN_DETECTION_SNR:
        raw = max([(w.raw_mean or 0) / w.stat_err for w in (nir, m35, m47) if w.stat_err] or [0.0])
        if nrows and raw >= MIN_DETECTION_SNR:
            reasons.append(f"The target is detected (S/N {raw:.0f}) only below the {BLEND_FLOOR_FRAC:.0%} "
                           "blend floor of a close neighbour; the deblended spectrum cannot be trusted.")
        else:
            reasons.append(f"Too faint: the best broad window (1.0-1.3, 3.2-3.8 or 4.4-5.0 um) has "
                           f"S/N {best:.1f} < {MIN_DETECTION_SNR:.0f}.")
        return result(LABEL_NONE, "low")

    def done(label: str, conf: str) -> dict[str, Any]:
        if target_pm_masyr is not None and target_pm_masyr > HIGH_PM_MASYR and label != LABEL_UNCLEAR:
            reasons.append(f"Proper motion {target_pm_masyr / 1000:.2f}\"/yr: a nearby object, not a galaxy.")
        order = ["low", "medium", "high"]
        return result(label, order[min(order.index(conf), order.index(cap))])

    # ---------------------------------------------------------------- Y: flux only at 4-5 um
    if (None not in (nir.mean, m35.mean, m47.mean)
            and m47.snr >= MIN_DETECTION_SNR and nir.snr < SIG):
        nir_lim = (max(nir.mean, 0.0) + SIG * nir.err) / m47.mean
        red = m47.mean / m35.mean if m35.snr >= SIG else m47.mean / (max(m35.mean, 0.0) + SIG * m35.err)
        red_txt = "" if m35.snr >= SIG else " >"
        if nir_lim < Y_NIR_LIMIT and red >= Y_RED_MIN:
            reasons.append(f"Detected at 4.4-5.0 um: {m47.mean:.0f} ± {m47.err:.0f} uJy (S/N {m47.snr:.0f}).")
            reasons.append(f"1.0-1.3 um flux {nir.mean:.0f} ± {nir.err:.0f} uJy is consistent with zero: "
                           f"F(1.0-1.3)/F(4.4-5.0) < {nir_lim:.2f} (3 sigma).")
            reasons.append(f"F(4.4-5.0)/F(3.2-3.8) ={red_txt} {red:.1f}, redder than T dwarfs "
                           f"(Y dwarfs have [3.6]-[4.5] >~ 2.5 mag).")
            high = m47.snr >= 10 and nir_lim < Y_NIR_LIMIT_HIGH and red >= Y_RED_HIGH
            caveats.append("An extremely red background object could mimic this; proper motion or parallax "
                           "is needed to confirm a cold brown dwarf.")
            return done(LABEL_Y, "high" if high else "medium")

    ch4 = indices["CH4_3.3"]
    h2o = [k for k in ("H2O_1.4", "H2O_1.9", "H2O_2.7") if _depth_sig(indices[k])]
    nir_ch4 = [k for k in ("CH4_1.6", "CH4_2.2") if _depth_sig(indices[k])]
    jk = indices["F1.25/F2.15"]
    j45 = indices["F1.15/F4.7"]
    h2o_txt = ", ".join(_fmt_ix(k, indices[k]) for k in h2o)

    # ---------------------------------------------------------------- T: strong 3.3 um methane
    if _depth_sig(ch4, STRONG_CH4_DEPTH):
        reasons.append(f"Strong 3.3 um CH4 absorption: {_fmt_ix('CH4_3.3', ch4)} "
                       f"(depth {1 - ch4['value']:.0%}, {ch4['sigma']:.0f} sigma).")
        if h2o:
            reasons.append(f"H2O absorption present: {h2o_txt}.")
        else:
            reasons.append("No significant H2O band, which T dwarfs normally show.")
        if nir_ch4:
            reasons.append("Near-IR methane also seen: " + ", ".join(_fmt_ix(k, indices[k]) for k in nir_ch4) + ".")
        if not h2o:
            conf = "low"
        elif ch4["sigma"] >= 5 and len(h2o) >= 2:
            conf = "high"
        else:
            conf = "medium"
        return done(LABEL_T, conf)

    red_jk = jk["value"] is not None and jk["value"] < LK_COLOUR_MAX and jk["sigma"] >= SIG
    weak_ch4 = _depth_sig(ch4)
    if h2o:
        reasons.append(f"H2O absorption present: {h2o_txt}.")
        if weak_ch4:
            reasons.append(f"Weak 3.3 um CH4: {_fmt_ix('CH4_3.3', ch4)} (depth {1 - ch4['value']:.0%}).")
        if jk["value"] is not None:
            reasons.append(f"F(1.2-1.3)/F(2.1-2.25) = {jk['value']:.2f} ± {jk['err']:.2f} "
                           f"({'red, L-like' if red_jk else 'not as red as L dwarfs'}; L0 ~ {LK_COLOUR_MAX}).")
        conf = "high" if len(h2o) >= 2 and jk["sigma"] is not None and jk["sigma"] >= 10 else "medium"
        if red_jk:
            if weak_ch4:
                reasons.append("Weak 3.3 um methane is seen in late L dwarfs.")
            return done(LABEL_L, conf)
        if weak_ch4:
            reasons.append("Weak methane with H2O and a blue J/K colour: possibly an early T dwarf.")
            return done(LABEL_T, "low")
        if jk["value"] is None:
            return done(LABEL_M, "low")
        return done(LABEL_M, conf)

    # ---------------------------------------------------------------- no molecular absorption
    deep = [k for k, ix in indices.items() if ix["kind"] == "absorption" and _depth_sig(ix)]
    star = (not deep and jk["value"] is not None and jk["value"] >= STAR_JK_MIN and jk["sigma"] >= SIG
            and j45["value"] is not None and j45["value"] >= STAR_J45_MIN and j45["sigma"] >= SIG)
    if star:
        reasons.append(f"Declining, Rayleigh-Jeans-like spectrum: F(1.2-1.3)/F(2.1-2.25) = {jk['value']:.2f} ± "
                       f"{jk['err']:.2f}, F(1.0-1.3)/F(4.4-5.0) = {j45['value']:.1f} ± {j45['err']:.1f}.")
        measured = [k for k in ("CH4_3.3", "H2O_1.4", "H2O_1.9") if indices[k]["value"] is not None]
        reasons.append("No band deeper than 10% at 3 sigma ("
                       + ", ".join(_fmt_ix(k, indices[k]) for k in measured) + ")." if measured
                       else "No molecular band could be measured.")
        good = (len(measured) == 3 and all(indices[k]["err"] < 0.05 for k in measured)
                and jk["sigma"] >= 10 and j45["sigma"] >= 10)
        return done(LABEL_STAR, "high" if good else "medium")

    if deep:
        reasons.append("Absorption only in " + ", ".join(_fmt_ix(k, indices[k]) for k in deep)
                       + ", without the H2O/CH4 pattern of cool dwarfs.")
    ex = ch4
    if ex["value"] is not None and ex["value"] - 1 >= MIN_DEPTH and -ex["sigma"] >= SIG:
        reasons.append(f"Excess at 3.3 um ({_fmt_ix('CH4_3.3', ex)}), as from PAH emission in a galaxy.")
    if j45["value"] is not None:
        reasons.append(f"F(1.0-1.3)/F(4.4-5.0) = {j45['value']:.2f} ± {j45['err']:.2f}"
                       + (" (flat or rising to the red: dust, galaxy or blend?)" if j45["value"] < 1 else "") + ".")
    if jk["value"] is not None:
        reasons.append(f"F(1.2-1.3)/F(2.1-2.25) = {jk['value']:.2f} ± {jk['err']:.2f}.")
    reasons.append("The indices do not match any of the rule sets (star, M, L, T, Y).")
    return done(LABEL_UNCLEAR, "low")
