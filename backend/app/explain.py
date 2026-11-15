"""'Explain this candidate': turn measured evidence into plain language.

Two layers:

1. Facts layer (always on, deterministic, no AI). `build_explanation` reads the cached
   analysis (`analysis["target"]` / `analysis["candidates"]`) and writes four short sections.
   Every number in the text is copied from the measurement engine (or is a unit conversion
   of one) and every sentence carries refs to the detector / season pair it came from.
2. AI layer (optional, `ai=1` and ANTHROPIC_API_KEY set). The Claude API rewrites the facts in
   plainer language. It sees only the facts dict and the four sections, and its answer is
   rejected unless every number it contains is present in the facts. Successful text is
   cached on disk next to the field. Any failure degrades to the facts layer.

Settings are read from the environment here (not config.py):
  ANTHROPIC_API_KEY       enables the AI layer (read by the SDK, never logged or returned)
  SPHEREX_EXPLAIN_MODEL   model id for the AI layer (default: a fast, inexpensive model)
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import re
import time
from importlib.util import find_spec

from fastapi import APIRouter, HTTPException, Request

from .store import field_dir

log = logging.getLogger(__name__)
router = APIRouter()

EXPLAIN_VERSION = "1"  # bump when the facts/sections wording or structure changes
PROMPT_VERSION = "1"  # bump when the AI prompt or validation changes
DEFAULT_MODEL = "claude-haiku-5-5"
N_DETECTORS = 6
SECTIONS = ("supports", "uncertain", "next_check", "conclusion")
MAX_DETAIL_ROWS = 3  # measurements described one by one in "supports"

AI_TIMEOUT_S = 25.0
AI_MAX_TOKENS = 2000  # headroom for the model's short reasoning; the answer is ~120 words
AI_MAX_WORDS = 230

# Wording that must never appear in the conclusion or in AI text.
BANNED = [
    r"\bdiscover(?:y|ies|ed|s|ing)?\b",
    r"\bnew (?:planet|object|world|moon|comet|asteroid|body)s?\b",
    r"\bplanet (?:x|nine|9)\b",
    r"\bproves?\b", r"\bproven\b", r"\bproof\b",
    r"\bdefinitely\b", r"\bcertainly\b", r"\bundoubtedly\b", r"\bundeniabl[ey]\b",
    r"\bconfirmed\b",  # only allowed when negated, see banned_wording()
]
_BANNED_RE = re.compile("|".join(BANNED), re.I)
_NEGATED_CONFIRMED_RE = re.compile(r"\b(?:not|never|nor|yet)\b[^.;:]{0,25}?\bconfirmed\b|\bunconfirmed\b", re.I)


def banned_wording(text: str) -> str | None:
    """The first discovery-style phrase in the text ('confirmed' is fine when negated), else None."""
    m = _BANNED_RE.search(_NEGATED_CONFIRMED_RE.sub(" ", text))
    return m.group(0) if m else None

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

# Reasons the analysis attaches to rows / candidates, in plain words.
REASON_TEXT = {
    "one epoch too shallow to compare": "the source was too faint in one of the two visits to be compared",
    "insufficient usable pixels": "too few usable pixels were left around the source",
    "inside the halo of a saturated star": (
        "it sits in the glow around a saturated star (a star so bright it overloads the detector), "
        "where leftover patterns often look like changes"
    ),
    "residual of the target's PSF": (
        "it sits in the glow around the target itself, so it may only be the target's own light pattern"
    ),
}
STANDARD_CHECKS = ("Seen in two or more detectors", "Two or more independent season comparisons", "Motion agrees")
INCONSISTENT_PREFIX = "centroid shifts disagree"
NO_COMPARISON_PREFIX = "no wavelength bin"


# --------------------------------------------------------------------------- formatting
def _n(x: float, nd: int = 2) -> str:
    """Magnitude of x at nd decimals, trailing zeros trimmed (signs are expressed in words)."""
    s = f"{abs(x):.{nd}f}"
    return s.rstrip("0").rstrip(".") if "." in s else s


def _plural(n: int, one: str, many: str | None = None) -> str:
    return one if n == 1 else (many or one + "s")


def _dets(ds: list[int]) -> str:
    names = [f"D{d}" for d in sorted(set(ds))]
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def _season_labels(seasons: list[dict]) -> dict[str, str]:
    """'May 2025'-style names for each season index (full start date if two would collide)."""
    out: dict[str, str] = {}
    for s in seasons:
        m = re.match(r"(\d{4})-(\d{2})", str(s.get("start") or ""))
        out[str(s["season"])] = f"{_MONTHS[int(m.group(2)) - 1]} {m.group(1)}" if m else f"season {int(s['season']) + 1}"
    if len(set(out.values())) < len(out):
        for s in seasons:
            if s.get("start"):
                out[str(s["season"])] = str(s["start"])[:10]
    return out


# --------------------------------------------------------------------------- facts
_ROW_NUMBERS = {  # key -> decimals kept
    "shift_px": 3, "shift_sigma": 1, "flux_ratio": 3, "dflux_sigma": 1,
    "snr_a": 1, "snr_b": 1, "lam_lo_um": 2, "lam_hi_um": 2,
}


def _row_facts(r: dict, pixscale: float, diffs: dict[str, dict]) -> dict:
    out: dict = {
        "detector": int(r["detector"]), "season_a": int(r["season_a"]), "season_b": int(r["season_b"]),
        "kind": r["kind"],
    }
    for k, nd in _ROW_NUMBERS.items():
        if r.get(k) is not None:
            out[k] = round(float(r[k]), nd)
    if "shift_px" in out:
        out["shift_arcsec"] = round(out["shift_px"] * pixscale, 2)
    for k in ("method", "reason"):
        if r.get(k):
            out[k] = r[k]
    if r.get("no_comparison"):
        out["no_comparison"] = True
    if r.get("bins_missing"):
        out["n_bins_missing"] = len(r["bins_missing"])
    pair = f"s{out['season_a']}-s{out['season_b']}"
    diff_id = r.get("diff") or f"d{out['detector']}_{pair}"
    if diff_id in diffs:
        out["diff_id"] = diff_id
        out["baseline_days"] = round(float(diffs[diff_id]["dt_days"]), 1)
    out["bin_diffs"] = [d for b in r.get("bins", []) if (d := f"{b}_{pair}") in diffs]
    return out


def _subject(analysis: dict, cand: str) -> dict | None:
    """Normalise a target or a candidate to one shape; None if it does not exist."""
    target = analysis.get("target")
    cands = analysis.get("candidates", [])
    if cand == "target":
        c = next((x for x in cands if x.get("is_target")), None)
        return _from_target(target, c) if target else None
    c = next((x for x in cands if x.get("id") == cand), None)
    if c is None:
        return None
    if c.get("is_target") and target:
        return _from_target(target, c)
    return {
        "label": c["kind"], "kind": c["kind"], "possible": False, "is_target": False,
        "rows": c.get("events", []), "evidence": c["evidence"], "n_independent": c.get("n_independent", 0),
        "n_detectors_agree": c.get("n_detectors_agree", 0), "checks": c.get("checks", []),
        "motion": c.get("motion_check"), "pmra": c.get("pmra_masyr"), "pmdec": c.get("pmdec_masyr"),
        "flux": (c.get("flux_ratio_min"), c.get("flux_ratio_max")), "reason": c.get("reason"),
        "peak": c.get("max_z"), "position": (c.get("ra"), c.get("dec")), "detectors": c.get("detectors", []),
    }


def _from_target(t: dict, c: dict | None) -> dict:
    label = t["summary"]
    return {
        "label": label, "kind": label.replace("possible ", ""), "possible": label.startswith("possible"),
        "is_target": True, "rows": t.get("rows", []), "evidence": t["evidence"],
        "n_independent": t.get("n_independent", 0), "n_detectors_agree": t.get("n_detectors_agree", 0),
        "checks": t.get("checks", []), "motion": t.get("motion_check"),
        "pmra": t.get("pmra_masyr"), "pmdec": t.get("pmdec_masyr"),
        "flux": (None, None), "reason": None,
        "peak": c.get("max_z") if c else None, "position": (c.get("ra"), c.get("dec")) if c else (None, None),
        "detectors": c.get("detectors", []) if c else [],
    }


def build_facts(analysis: dict, cand: str, seasons: list[dict], pixscale: float) -> dict | None:
    """The compact numeric dict every sentence is built from. None for an unknown candidate."""
    s = _subject(analysis, cand)
    if s is None:
        return None
    diffs = {d["id"]: d for d in analysis.get("diffs", [])}
    rows = [_row_facts(r, pixscale, diffs) for r in s["rows"]]
    rows.sort(key=lambda r: (r["detector"], r["season_a"], r["season_b"]))
    checks = [{k: v for k, v in c.items() if k in ("label", "ok", "note")} for c in s["checks"]]
    standard = [c for c in checks if c["label"].startswith(STANDARD_CHECKS)]  # the rest are demotion reasons
    f: dict = {
        "label": s["label"], "kind": s["kind"], "possible": s["possible"], "is_target": s["is_target"],
        "evidence": s["evidence"], "n_independent": int(s["n_independent"]),
        "n_detectors_agree": int(s["n_detectors_agree"]), "n_detectors_total": N_DETECTORS,
        "checks": checks, "checks_passed": sum(bool(c["ok"]) for c in standard), "checks_total": len(standard),
        "pixel_scale_arcsec": round(float(pixscale), 2),
        "seasons": _season_labels(seasons), "rows": rows,
    }
    f["detectors"] = sorted(set(s["detectors"]) | {r["detector"] for r in rows if r["kind"] == s["kind"]})
    if s["peak"] is not None:
        f["peak_sigma"] = round(float(s["peak"]), 1)
    if s["position"][0] is not None:
        f["position"] = {"ra": s["position"][0], "dec": s["position"][1]}
    if s["reason"]:
        f["reason"] = s["reason"]
    m = s["motion"] or {}
    if m:
        f["motion"] = {"n_shifts": int(m.get("n_movers", 0)), "coherent": m.get("coherent")}
        if m.get("coherence") is not None:
            f["motion"]["coherence"] = round(float(m["coherence"]), 2)
            f["motion"]["speed_scatter_pct"] = int(round(100 * float(m.get("speed_scatter", 0))))
    if s["pmra"] is not None and s["pmdec"] is not None:
        f["proper_motion"] = {
            "pmra_masyr": int(s["pmra"]), "pmdec_masyr": int(s["pmdec"]),
            "pmra_arcsec_yr": round(s["pmra"] / 1000, 3), "pmdec_arcsec_yr": round(s["pmdec"] / 1000, 3),
            "total_arcsec_yr": round(math.hypot(s["pmra"], s["pmdec"]) / 1000, 2),
        }
    agree = [r["flux_ratio"] for r in rows if r["kind"] == s["kind"] and "flux_ratio" in r]
    lo, hi = s["flux"]
    if agree:
        f["flux_ratio"] = {"min": min(agree), "max": max(agree)}
    elif lo is not None and hi is not None:
        f["flux_ratio"] = {"min": round(lo, 3), "max": round(hi, 3)}
    return f


# --------------------------------------------------------------------------- text
class _W:
    """Writes sentences from a facts dict; defines each piece of jargon at first use."""

    def __init__(self, f: dict) -> None:
        self.f = f
        self.sigma_defined = False
        self.ratio_defined = False

    def season(self, s: int) -> str:
        return self.f["seasons"].get(str(s), f"season {int(s) + 1}")

    def pair(self, r: dict) -> str:
        return f"{self.season(r['season_a'])} and {self.season(r['season_b'])}"

    def where(self, r: dict) -> str:
        if "lam_lo_um" in r and "lam_hi_um" in r:
            return f"D{r['detector']} ({_n(r['lam_lo_um'])} to {_n(r['lam_hi_um'])} µm)"
        return f"D{r['detector']}"

    def sig(self, x: float) -> str:
        s = _n(x, 1)
        if self.sigma_defined:
            return f"{s} sigma"
        self.sigma_defined = True
        return f"{s} sigma (that is, {s} times its measurement uncertainty)"

    def vs(self, r: dict) -> str:
        return f"comparing {self.season(r['season_a'])} with {self.season(r['season_b'])}"

    def baseline(self, r: dict) -> str:
        return f", {_n(r['baseline_days'], 1)} days apart" if "baseline_days" in r else ""


def _item(text: str, *rows: dict) -> dict:
    refs, seen = [], set()
    for r in rows:
        k = (r["detector"], r["season_a"], r["season_b"])
        if k not in seen:
            seen.add(k)
            refs.append({"detector": k[0], "season_a": k[1], "season_b": k[2], "diff_id": r.get("diff_id")})
    return {"text": text, "refs": refs}


def _strongest(rows: list[dict], key: str) -> list[dict]:
    return sorted(rows, key=lambda r: -abs(r.get(key) or 0))


def _shift_sentence(w: _W, r: dict) -> str:
    return (
        f"In {w.where(r)}, the source's centre moved {_n(r['shift_px'], 3)} pixels "
        f"({_n(r['shift_arcsec'])} arcseconds) between the {w.pair(r)} visits{w.baseline(r)}. "
        f"That shift is {w.sig(r['shift_sigma'])}."
    )


def _flux_sentence(w: _W, r: dict) -> str:
    a, b = w.season(r["season_a"]), w.season(r["season_b"])
    ratio = r["flux_ratio"]
    direction = "fainter" if ratio < 1 else "brighter"
    tail = ""
    if not w.ratio_defined:
        w.ratio_defined = True
        tail = " (a ratio of 1 would mean no change)"
    sig = f", a change of {w.sig(r['dflux_sigma'])}" if "dflux_sigma" in r else ""
    return (
        f"In {w.where(r)}, the source was {direction} in {b} than in {a}: its flux was "
        f"{_n(ratio, 3)} times the earlier flux{tail}{sig}."
    )


def _glossary() -> dict:
    return _item(
        "SPHEREx scans the whole sky and returns to each patch about every six months. "
        "We call each return a season. It measures infrared light with "
        f"{N_DETECTORS} detectors (D1 to D{N_DETECTORS}), and each detector covers its own range of wavelengths. "
        "Every comparison below looks at the same wavelengths in two seasons."
    )


def _supports(w: _W) -> list[dict]:
    f = w.f
    kind, rows = f["kind"], f["rows"]
    out = [_glossary()]
    usable = [r for r in rows if not r.get("no_comparison")]
    agree = [r for r in rows if r["kind"] == kind]
    if "peak_sigma" in f and not f["is_target"]:
        out.append(_item(
            "A difference image (a later season minus an earlier one) shows a spot at this position "
            f"with a peak significance of {_n(f['peak_sigma'], 1)} sigma."
        ))

    if kind == "mover" and agree:
        top = _strongest(agree, "shift_sigma")
        for r in top[:MAX_DETAIL_ROWS]:
            out.append(_item(_shift_sentence(w, r), r))
        if len(agree) > 1 or len(usable) > len(agree):
            out.append(_item(
                f"In all, {len(agree)} of {len(usable)} season comparisons show a significant shift "
                f"({_dets([r['detector'] for r in agree])}).", *top[MAX_DETAIL_ROWS:]
            ))
        pm = f.get("proper_motion")
        if pm:
            ew = "west" if pm["pmra_arcsec_yr"] < 0 else "east"
            ns = "north" if pm["pmdec_arcsec_yr"] >= 0 else "south"
            out.append(_item(
                "Together the shifts give a proper motion (how far the source drifts across the sky each "
                f"year) of about {_n(pm['total_arcsec_yr'])} arcseconds per year: "
                f"{_n(pm['pmra_arcsec_yr'], 3)} toward the {ew} and {_n(pm['pmdec_arcsec_yr'], 3)} toward the {ns}."
            ))
        m = f.get("motion", {})
        if m.get("coherent") and m.get("n_shifts", 0) >= 2:
            out.append(_item(
                f"The {m['n_shifts']} separate shifts point the same way (direction agreement "
                f"{_n(m['coherence'])} on a scale where 1 is perfect) and give similar speeds "
                f"(they differ by {m['speed_scatter_pct']}%). Real motion should look like that."
            ))
        fr = f.get("flux_ratio")
        if fr:
            out.append(_item(
                f"Its brightness in the comparisons that show motion changed by a factor of {_n(fr['min'], 3)} to "
                f"{_n(fr['max'], 3)} (1 means unchanged)."
            ))
    elif kind in ("variable", "appeared", "vanished") and agree:
        top = _strongest(agree, "dflux_sigma")
        for r in top[:MAX_DETAIL_ROWS]:
            if kind == "variable" and "flux_ratio" in r:
                out.append(_item(_flux_sentence(w, r), r))
            else:
                a, b = w.season(r["season_a"]), w.season(r["season_b"])
                what = (f"was too faint to measure in {a} but clear in {b}" if kind == "appeared"
                        else f"was clear in {a} but too faint to measure in {b}")
                out.append(_item(
                    f"In {w.where(r)}, the source {what}. Its signal-to-noise (its strength compared with the noise) "
                    f"was {_n(r.get('snr_a', 0), 1)} then {_n(r.get('snr_b', 0), 1)}, "
                    f"a change of {w.sig(r.get('dflux_sigma', 0))}.", r
                ))
        if len(agree) > 1 or len(usable) > len(agree):
            out.append(_item(
                f"In all, {len(agree)} of {len(usable)} season comparisons show this change "
                f"({_dets([r['detector'] for r in agree])}).", *top[MAX_DETAIL_ROWS:]
            ))
        fr = f.get("flux_ratio")
        if kind == "variable" and fr and len(agree) > 1:
            out.append(_item(f"Across them the flux ratio runs from {_n(fr['min'], 3)} to {_n(fr['max'], 3)}."))
        shifts = [r for r in agree if "shift_px" in r]
        if kind == "variable" and shifts:
            big = _strongest(shifts, "shift_px")[0]
            out.append(_item(
                f"The largest position shift among these comparisons was {_n(big['shift_px'], 3)} pixels "
                f"({_n(big['shift_arcsec'])} arcseconds, {_n(big.get('shift_sigma', 0), 1)} sigma).", big
            ))
    elif kind == "static":
        shifts = [r for r in rows if "shift_px" in r]
        ratios = [r["flux_ratio"] for r in rows if "flux_ratio" in r]
        if shifts:
            big = _strongest(shifts, "shift_px")[0]
            out.append(_item(
                f"No comparison showed a significant shift. The largest was {_n(big['shift_px'], 3)} pixels "
                f"({_n(big['shift_arcsec'])} arcseconds, {_n(big.get('shift_sigma', 0), 1)} sigma) in {w.where(big)}.",
                big
            ))
        if ratios:
            out.append(_item(
                f"Brightness stayed within a factor of {_n(min(ratios), 3)} to {_n(max(ratios), 3)} "
                "between seasons (1 means unchanged)."
            ))
    else:  # uncertain / no data / a kind without agreeing rows
        measured = [r for r in usable if "shift_px" in r or "flux_ratio" in r]
        for r in _strongest(measured, "shift_sigma")[:2]:
            bits = []
            if "shift_px" in r:
                bits.append(f"a shift of {_n(r['shift_px'], 3)} pixels ({_n(r['shift_arcsec'])} arcseconds, "
                            f"{_n(r.get('shift_sigma', 0), 1)} sigma)")
            if "flux_ratio" in r:
                bits.append(f"a flux ratio of {_n(r['flux_ratio'], 3)}")
            out.append(_item(
                f"In {w.where(r)}, between {w.pair(r)}, the measurements show {' and '.join(bits)}, "
                "but the analysis could not settle what that means.", r
            ))
        if not measured:
            out.append(_item("No comparison gave a clear shift or brightness change."))
    return out


def _uncertain(w: _W) -> list[dict]:
    f = w.f
    kind, rows = f["kind"], f["rows"]
    out: list[dict] = []
    if f["checks_total"]:
        out.append(_item(
            f"The evidence level is {f['evidence']}: {f['checks_passed']} of {f['checks_total']} checks passed "
            f"({'; '.join(c['label'][0].lower() + c['label'][1:] for c in f['checks'] if c['label'].startswith(STANDARD_CHECKS))}). "
            "This level is a ranking aid built from those checks. It is not the probability that the object is real."
            + (" It was also lowered for the reason given below." if f.get("reason") else "")
        ))
    else:
        out.append(_item(
            f"The evidence level is {f['evidence']}. It is a ranking aid, not the probability that the object is real."
        ))
    motion = f.get("motion", {})
    for c in f["checks"]:
        if c["ok"]:
            continue
        label = c["label"]
        if label.startswith("Seen in two or more detectors"):
            n = f["n_detectors_agree"]
            dets = _dets(f["detectors"]) if f["detectors"] else ""
            lead = ("No detector shows a clear change" if n == 0 else
                    f"Only {n} {_plural(n, 'detector')} {'shows' if n == 1 else 'show'} this"
                    f"{' (' + dets + ')' if dets else ''}")
            out.append(_item(f"{lead}, so a fault specific to one detector is not ruled out."))
        elif label.startswith("Two or more independent season comparisons"):
            n = f["n_independent"]
            lead = ("No independent season comparison supports it" if n == 0 else
                    f"Only {n} independent season {_plural(n, 'comparison')} {'supports' if n == 1 else 'support'} it")
            out.append(_item(
                f"{lead} (comparisons that share a season are not independent), so a one-off glitch in one visit "
                "is not ruled out."
            ))
        elif label.startswith("Motion agrees") and motion.get("coherent") is None:
            out.append(_item(
                "Only one shift was measured, so we cannot check that its direction and speed agree with anything."
            ))
    if motion.get("coherent") is False:
        out.append(_item(
            f"The {motion['n_shifts']} measured shifts disagree with each other (direction agreement "
            f"{_n(motion.get('coherence', 0))} on a scale where 1 is perfect, speeds differing by "
            f"{motion.get('speed_scatter_pct', 0)}%). Real motion points the same way everywhere, so these shifts "
            "are more likely caused by a nearby star blending in, a saturated centre, or a change in the image blur. "
            "They are not counted as motion."
        ))

    reason = f.get("reason")
    if reason:
        out.append(_item(f"The analysis flagged this spot because {REASON_TEXT.get(reason, reason)}."))

    # detectors that see nothing while the verdict says something changed
    if kind in ("mover", "variable"):
        nulls = [r for r in rows if r["kind"] == "static"]
        if nulls:
            ex = max(nulls, key=lambda r: min(r.get("snr_a", 0), r.get("snr_b", 0)))
            dets = sorted({r["detector"] for r in nulls})
            bits = []
            if "shift_px" in ex:
                bits.append(f"a shift of {_n(ex['shift_px'], 3)} pixels ({_n(ex.get('shift_sigma', 0), 1)} sigma)")
            if "flux_ratio" in ex:
                bits.append(f"a flux ratio of {_n(ex['flux_ratio'], 3)} ({_n(ex.get('dflux_sigma', 0), 1)} sigma)")
            out.append(_item(
                f"{_dets(dets)} compared the same seasons and found no significant shift or brightness change "
                f"({len(nulls)} {_plural(len(nulls), 'comparison')}; for example {w.where(ex)}, {w.vs(ex)}, "
                f"shows {' and '.join(bits)}). "
                "A change seen at some wavelengths but not others needs an explanation, such as a steady "
                "neighbouring star dominating the light at those wavelengths.", *nulls[:2]
            ))

    # inconclusive and uncomparable rows
    inconclusive: dict[str, list[dict]] = {}
    for r in rows:
        why = r.get("reason")
        if r["kind"] == "uncertain" and why and not why.startswith((INCONSISTENT_PREFIX, NO_COMPARISON_PREFIX)):
            inconclusive.setdefault(why, []).append(r)
    for why, rs in inconclusive.items():
        dets = sorted({r["detector"] for r in rs})
        out.append(_item(
            f"{len(rs)} {_plural(len(rs), 'comparison')} in {_dets(dets)} "
            f"{'was' if len(rs) == 1 else 'were'} inconclusive: {REASON_TEXT.get(why, why)}.", *rs[:3]
        ))
    none = [r for r in rows if r.get("no_comparison")]
    if none:
        dets = sorted({r["detector"] for r in none})
        out.append(_item(
            f"{len(none)} season {_plural(len(none), 'pair')} could not be compared at all "
            f"({_dets(dets)}) because the two seasons share no wavelength range with enough exposures "
            "(at least 3 in each). Missing coverage is not evidence either way.", *none[:3]
        ))
    gaps = [r for r in rows if r.get("n_bins_missing") and r["kind"] == kind]
    if gaps:
        out.append(_item(
            "Some comparisons leave out part of a detector's wavelength range, because one of the two seasons "
            "has too few exposures there. Those comparisons cover less of the spectrum than the full detector.",
            *gaps[:2]
        ))
    sat = [r for r in rows if "saturated" in (r.get("method") or "")]
    if sat:
        out.append(_item(
            f"In {_dets([r['detector'] for r in sat])} the source's centre is saturated (too bright for the "
            "detector), so its position and brightness come from a fit to the fainter outer parts of the star. "
            "That is less direct than measuring the centre.", *sat[:3]
        ))
    out.append(_item(
        f"One SPHEREx pixel is {_n(f['pixel_scale_arcsec'])} arcseconds wide, so sources closer together than "
        "that blend, and a blend can fake or hide motion."
    ))
    return out


def _next_check(w: _W) -> list[dict]:
    f = w.f
    kind, rows = f["kind"], f["rows"]
    motion = f.get("motion", {})
    agree = [r for r in rows if r["kind"] == kind]
    nulls = [r for r in rows if r["kind"] == "static"]

    if motion.get("coherent") is False:
        with_bins = [r for r in rows if r.get("bin_diffs")]
        if with_bins:
            r = _strongest(with_bins, "shift_sigma")[0]
            n = len(r["bin_diffs"])
            return [_item(
                f"Open the per-wavelength difference images for {w.where(r)}, {w.vs(r)} "
                f"({n} wavelength {_plural(n, 'slice')}). If the shift points the same way in every slice, it is more "
                "likely real motion. If it flips or changes size, a blend or a bright star's glare is more likely.", r
            )]
    if kind in ("mover", "variable") and nulls:
        ex = max(nulls, key=lambda r: min(r.get("snr_a", 0), r.get("snr_b", 0)))
        return [_item(
            f"Open the difference view for {w.where(ex)}, {w.vs(ex)}, which sees no change while other "
            "detectors do. Look for a bright neighbouring star close to the target: if there is one, "
            "it likely dominates the light at those wavelengths and hides the change.", ex
        )]
    if kind == "mover" and agree:
        r = _strongest(agree, "shift_sigma")[0]
        return [_item(
            f"Open the difference view for {w.where(r)}, {w.vs(r)}. A moving source leaves a bright spot "
            f"next to a dark spot about {_n(r['shift_arcsec'])} arcseconds apart (a pair called a dipole) "
            "at the target.", r
        )]
    if kind in ("variable", "appeared", "vanished") and agree:
        r = _strongest(agree, "dflux_sigma")[0]
        return [_item(
            f"Open the difference view for {w.where(r)}, {w.vs(r)}. A source that only changed brightness leaves "
            "a single bright or dark spot with no partner. A bright and dark pair would instead mean it moved.", r
        )]
    with_diff = [r for r in rows if r.get("diff_id") and not r.get("no_comparison")]
    if with_diff:
        r = _strongest(with_diff, "shift_sigma")[0]
        return [_item(
            f"Open the difference view for {w.where(r)}, {w.vs(r)}, and look at its shape. A clean bright and dark "
            "pair suggests motion, a single spot suggests a brightness change, and a ring or messy pattern "
            "suggests a bright star's glare.", r
        )]
    return [_item(
        "Open the spectrum for this position (the brightness at each wavelength) and see whether it follows a "
        "smooth shape or is dominated by a few points."
    )]


def _conclusion(w: _W) -> list[dict]:
    f = w.f
    kind, level = f["kind"], f["evidence"]
    pm, fr = f.get("proper_motion"), f.get("flux_ratio")
    if kind == "mover":
        if f["possible"]:
            return [_item(
                "A single comparison hints at motion. That is not enough to call it a mover: it is a possible "
                "candidate only, and it is not confirmed."
            )]
        speed = f" at about {_n(pm['total_arcsec_yr'])} arcseconds per year" if pm else ""
        tail = ("It is a candidate mover, not a confirmed one." if level == "strong" else
                f"The evidence is only {level}, so treat it as a candidate that is not confirmed.")
        return [_item(f"These measurements are consistent with a source moving across the sky{speed}. {tail}")]
    if kind == "variable":
        rng = f" (flux ratios from {_n(fr['min'], 3)} to {_n(fr['max'], 3)})" if fr else ""
        lead = "A single comparison hints at" if f["possible"] else "These measurements are consistent with"
        return [_item(
            f"{lead} a source whose brightness changed between seasons{rng}. It is a candidate variable, "
            "not confirmed, and a blend or a saturated centre can imitate this."
        )]
    if kind in ("appeared", "vanished"):
        verb = "brightened enough to appear" if kind == "appeared" else "faded enough to drop out of view"
        return [_item(
            f"These measurements are consistent with a source that {verb} between seasons. "
            "It is a candidate, not confirmed, and a source near the detection limit can do this by chance."
        )]
    if kind == "static":
        return [_item(
            "No significant motion or brightness change was measured, which is consistent with a steady source. "
            "That does not rule out a faint or blended mover, because this check can miss those."
        )]
    return [_item(
        "The measurements do not settle what this is. Treat it as an unexplained feature, "
        "not as a detection of a moving or changing object, and not confirmed."
    )]


def build_sections(facts: dict) -> dict[str, list[dict]]:
    w = _W(facts)
    sections = {
        "supports": _supports(w), "uncertain": _uncertain(w),
        "next_check": _next_check(w), "conclusion": _conclusion(w),
    }
    for it in sections["conclusion"]:
        if banned_wording(it["text"]):  # a wording bug, never user input
            raise AssertionError(f"banned wording in conclusion: {it['text']}")
    return sections


def build_explanation(analysis: dict, field: str, cand: str, seasons: list[dict], pixscale: float) -> dict | None:
    """The facts-layer response (without the AI fields); None for an unknown candidate."""
    facts = build_facts(analysis, cand, seasons, pixscale)
    if facts is None:
        return None
    return {
        "status": "complete", "field": field, "cand": cand, "kind": facts["label"], "evidence": facts["evidence"],
        "sections": build_sections(facts), "facts": facts,
    }


# --------------------------------------------------------------------------- AI validation
_NUM_RE = re.compile(r"(?<![A-Za-z0-9_.])(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)")
_DET_RE = re.compile(r"\bD([0-9])\b")
_CAP_RE = re.compile(r"\b[A-Z][A-Za-z]+\b")
_DET_RANGE_RE = re.compile(r"\bD1\s*(?:to|through|-|–)\s*D6\b")
_ALLOWED_CAPS = {"spherex", "nasa", "ra", "dec"} | {
    m.lower() for m in (
        "January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
        "November", "December",
    )
}


def _numbers_in(text: str) -> list[tuple[float, int]]:
    """(value, decimals) for every number in the text; digits glued to letters (D6, d6b2) are skipped."""
    out = []
    for m in _NUM_RE.finditer(text):
        s = m.group(1).replace(",", "")
        out.append((float(s), len(s.split(".")[1]) if "." in s else 0))
    return out


def _walk(x, nums: list[float], strs: list[str]) -> None:
    if isinstance(x, bool) or x is None:
        return
    if isinstance(x, (int, float)):
        nums.append(abs(float(x)))
    elif isinstance(x, str):
        strs.append(x)
    elif isinstance(x, dict):
        for k, v in x.items():
            if k not in ("diff_id", "bin_diffs"):
                _walk(v, nums, strs)
    elif isinstance(x, (list, tuple)):
        for v in x:
            _walk(v, nums, strs)


def validate_ai_text(text: str, facts: dict, sections: dict) -> list[str]:
    """Problems that make AI text unusable (empty list = faithful).

    Every number must be in the facts or sections: whole numbers exactly, decimals up to
    rounding to the number of decimals written. Detectors mentioned must be ones in the facts,
    there must be no discovery wording, and no capitalised name the facts never use."""
    if not text.strip():
        return ["empty answer"]
    problems: list[str] = []
    if len(text.split()) > AI_MAX_WORDS:
        problems.append("too long")
    if bad := banned_wording(text):
        problems.append(f"banned wording '{bad}'")

    nums: list[float] = []
    strs: list[str] = []
    _walk(facts, nums, strs)
    _walk([it["text"] for k in SECTIONS for it in sections[k]], nums, strs)
    for s in strs:
        nums.extend(v for v, _ in _numbers_in(s))
    for v, d in _numbers_in(text):
        tol = 1e-9 if d == 0 else 0.5 * 10 ** (-d) + 1e-9
        if not any(abs(f - v) <= tol for f in nums):
            problems.append(f"number {v:g} is not in the measurements")

    known_dets = {r["detector"] for r in facts["rows"]} | set(facts.get("detectors", []))
    for m in _DET_RE.finditer(_DET_RANGE_RE.sub(" ", text)):
        if int(m.group(1)) not in known_dets:
            problems.append(f"detector D{m.group(1)} is not in the measurements")

    ground = set(re.findall(r"[a-z]+", " ".join(strs).lower())) | _ALLOWED_CAPS
    for sent in re.split(r"(?<=[.!?])\s+", text.strip()):
        caps = _CAP_RE.findall(sent)
        if caps and sent.lstrip().startswith(caps[0]):
            caps = caps[1:]  # a sentence's first word is capitalised anyway
        for tok in caps:
            if tok.lower() not in ground:
                problems.append(f"name '{tok}' is not in the measurements")
    return list(dict.fromkeys(problems))


# --------------------------------------------------------------------------- AI layer
SYSTEM_PROMPT = """You rewrite measurement summaries from a NASA SPHEREx infrared-sky tool for curious people who are not astronomers.

You are given FACTS (JSON) and four SECTIONS of pre-written sentences built from them. Write about 120 words of plain, friendly prose in two short paragraphs. No headings, no bullet points, no markdown.

Hard rules:
- Use only information contained in FACTS and SECTIONS. Never add a number, name, date, distance, cause or claim that is not there. When you quote a number, copy it exactly as written there and write it with digits.
- Do not name any star, galaxy, planet, catalogue or other astronomical object.
- Never say or imply a discovery, a new planet, a new object or a confirmed detection. Use hedged wording such as "consistent with", "candidate" and "not confirmed".
- Keep the uncertainty: say plainly what could make the result wrong.
- Do not give probabilities or percentages unless they appear in the FACTS.
- Define any technical word the first time you use it.
- Output only the summary text."""


class _AiRefused(Exception):
    pass


class _AiTruncated(Exception):
    pass


def ai_model() -> str:
    return os.environ.get("SPHEREX_EXPLAIN_MODEL", "").strip() or DEFAULT_MODEL


def _sdk_present() -> bool:
    return find_spec("anthropic") is not None


def ai_available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY", "").strip()) and _sdk_present()


def make_client():
    """Anthropic client. The SDK reads ANTHROPIC_API_KEY itself: the key never passes through our code."""
    import anthropic

    return anthropic.Anthropic(timeout=AI_TIMEOUT_S, max_retries=1)


def _supports_effort(model: str) -> bool:
    return bool(re.match(r"claude-(?:haiku|sonnet|opus|fable|mythos)-(?:[5-9]|4-[6-9])", model))


def _payload(facts: dict, sections: dict) -> str:
    slim = {**facts, "rows": [{k: v for k, v in r.items() if k not in ("diff_id", "bin_diffs")} for r in facts["rows"]]}
    text = "\n".join(f"{k.upper()}:\n" + "\n".join(f"- {it['text']}" for it in sections[k]) for k in SECTIONS)
    return f"FACTS:\n{json.dumps(slim, ensure_ascii=False)}\n\nSECTIONS:\n{text}"


def _generate(model: str, facts: dict, sections: dict) -> str:
    """Blocking call to the Claude API; returns the answer text or raises."""
    client = make_client()
    kwargs: dict = {"output_config": {"effort": "low"}} if _supports_effort(model) else {}
    resp = client.messages.create(
        model=model, max_tokens=AI_MAX_TOKENS, system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": _payload(facts, sections)}], **kwargs,
    )
    if getattr(resp, "stop_reason", None) == "refusal":
        raise _AiRefused()
    if getattr(resp, "stop_reason", None) == "max_tokens":
        raise _AiTruncated()
    return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()


def _describe_error(exc: Exception) -> str:
    """A short reason for the user. Never includes exception text (which could echo request details)."""
    name, status = type(exc).__name__, getattr(exc, "status_code", None)
    if isinstance(exc, ImportError):
        return "The server is missing the 'anthropic' package."
    if isinstance(exc, _AiRefused):
        return "The AI service declined to write this summary."
    if isinstance(exc, _AiTruncated):
        return "The AI answer was cut off, so it was discarded."
    if "Timeout" in name:
        return "The AI service took too long to answer."
    if name in ("AuthenticationError", "PermissionDeniedError") or status in (401, 403):
        return "The AI service did not accept the server's credentials."
    if name == "RateLimitError" or status == 429:
        return "The AI service is busy right now. Try again in a minute."
    if "Connection" in name:
        return "The AI service could not be reached."
    if status:
        return f"The AI service returned an error ({status})."
    return "The AI summary could not be produced."


def _cache_file(key: str, cand: str):
    return field_dir(key) / "explain" / f"{cand}.json"


def _fingerprint(facts: dict, sections: dict) -> str:
    raw = json.dumps({"f": facts, "s": sections, "v": [EXPLAIN_VERSION, PROMPT_VERSION]}, sort_keys=True)
    return hashlib.sha1(raw.encode()).hexdigest()


def _read_cache(key: str, cand: str, analysis_version: str, fp: str) -> dict | None:
    try:
        d = json.loads(_cache_file(key, cand).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if d.get("analysis_version") == analysis_version and d.get("fingerprint") == fp and d.get("text"):
        return {"text": d["text"], "model": d.get("model", "")}
    return None


def _write_cache(key: str, cand: str, analysis_version: str, fp: str, ai: dict) -> None:
    p = _cache_file(key, cand)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"analysis_version": analysis_version, "fingerprint": fp, **ai}), encoding="utf-8")
        tmp.replace(p)
    except OSError:
        log.warning("could not cache explanation for %s/%s", key, cand)


_locks: dict[tuple[str, str], asyncio.Lock] = {}


async def ai_summary(
    key: str, cand: str, analysis_version: str, facts: dict, sections: dict
) -> tuple[dict | None, str | None]:
    """(ai, error). Cached, validated and never raises."""
    fp = _fingerprint(facts, sections)
    if hit := _read_cache(key, cand, analysis_version, fp):
        return hit, None
    model = ai_model()
    async with _locks.setdefault((key, cand), asyncio.Lock()):  # two quick clicks cost one API call
        if hit := _read_cache(key, cand, analysis_version, fp):
            return hit, None
        try:
            text = await asyncio.to_thread(_generate, model, facts, sections)
        except Exception as exc:  # noqa: BLE001 - degrade to the facts layer, whatever went wrong
            log.warning("explain AI call failed for %s/%s: %s", key, cand, type(exc).__name__)
            return None, _describe_error(exc)
        problems = validate_ai_text(text, facts, sections)
        if problems:
            log.warning("explain AI text rejected for %s/%s: %s", key, cand, "; ".join(problems))
            return None, f"The AI text was discarded because it did not match the measurements ({problems[0]})."
        ai = {"text": text, "model": model}
        _write_cache(key, cand, analysis_version, fp, {**ai, "created": int(time.time())})
        return ai, None


# --------------------------------------------------------------------------- route
@router.get("/api/explain")
async def explain(request: Request, key: str, cand: str = "target", ai: int = 0):
    job = request.app.state.fields.jobs.get(key)
    if job is None:
        raise HTTPException(404, "unknown field (request /api/frames first)")
    if job.status == "error":
        return {"status": "error", "error": job.error or "the field failed to build"}
    if job.status != "complete" or "target" not in (job.analysis or {}):
        return {"status": "processing"}
    if not re.fullmatch(r"target|c\d+", cand):
        raise HTTPException(404, "unknown candidate")
    out = build_explanation(job.analysis, key, cand, job.seasons, job.grid.pixscale_arcsec)
    if out is None:
        raise HTTPException(404, "unknown candidate")
    available = ai_available()
    out.update(ai=None, ai_available=available, ai_error=None)
    if ai:
        if not available:
            out["ai_error"] = "AI summaries are not set up on this server."
        else:
            out["ai"], out["ai_error"] = await ai_summary(
                key, cand, str(job.analysis.get("version", "")), out["facts"], out["sections"]
            )
    return out
