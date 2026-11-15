"""Human-readable investigation report: one self-contained HTML page (or the same content as JSON)
that records what was measured for a target and how far it can be trusted.

Nothing here starts a computation. The field must be complete; the Mover Fingerprint is included
only when a result for exactly these parameters is already cached, otherwise the page says so."""

from __future__ import annotations

import asyncio
import base64
import html
import json
import logging
import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, Response

from .analysis import ANALYSIS_VERSION, diff_arrays
from .catalogs import CATALOG_NAME
from .classify import CLASSIFY_VERSION
from .config import settings
from .coords import CoordError, PM_THRESHOLD_MASYR, SPHEREX_REF_MJD, parse_dec, parse_ra
from .explain import EXPLAIN_VERSION, _season_labels, build_facts, build_sections
from .fields import FieldJob, field_key
from .fingerprint import FINGERPRINT_VERSION, Target
from .processing import PROCESSING_VERSION, Grid
from .render import render_diff_png, render_stack_png
from .stacks import STACK_VERSION

log = logging.getLogger(__name__)
router = APIRouter()

REPORT_VERSION = "1"  # bump when the structure of the report changes
SECTION_TITLES = {
    "supports": "What the measurements support", "uncertain": "What remains uncertain",
    "next_check": "What to check next", "conclusion": "Conclusion",
}
LIMITATIONS = (
    "SPHEREx pixels are 6.2 arcseconds across, so nearby stars blend with the target and a shift of a "
    "fraction of a pixel is the whole signal. Bright stars saturate the detector core, which degrades or "
    "prevents photometry. Each position has only a few observing seasons, so motion and brightness "
    "changes rest on a handful of comparisons. Evidence levels (strong, moderate, weak) summarise how many "
    "independent checks agree; they are rules of thumb, not probabilities. Everything here is a candidate "
    "to be followed up, not a confirmed discovery."
)
METHOD_NOTES = [
    "Exposures come from the IRSA SPHEREx level-2 image archive (Simple Image Access cutouts), are "
    "zodiacal-light subtracted, flagged pixels are interpolated over, and everything is resampled onto one "
    "common sky grid centred on the target.",
    "Season stacks are inverse-variance co-adds with per-pixel outlier rejection. Differences are taken "
    "between seasons only at matched wavelengths, so a change in colour is not mistaken for motion.",
    "Movers are judged from centroid shifts and flux changes in each detector and season pair; the "
    "evidence level counts independent detectors and comparisons whose motion agrees.",
    "The deblended spectrum (when present) fits every catalogued neighbour together with the target in "
    "each exposure using the SPHEREx ePSF; the aperture column is a plain aperture measurement that does "
    "not remove neighbours.",
    "The classification is rule-based on spectral indices and is not a probability.",
]
CREDITS = [
    "SPHEREx data: NASA/JPL-Caltech, SPHEREx mission; accessed through the NASA/IPAC Infrared Science "
    "Archive (IRSA).",
    f"Catalogue neighbours: {CATALOG_NAME} (ESA/Gaia/DPAC), queried through IRSA.",
    "Software: SPHEREx Time Machine (NASA Space Apps 2026), astropy, numpy, scipy.",
]


# --------------------------------------------------------------------------- helpers
def _safe(o: Any) -> Any:
    """JSON-safe copy: non-finite floats become None, numpy scalars become Python numbers."""
    if isinstance(o, dict):
        return {str(k): _safe(v) for k, v in o.items()}
    if isinstance(o, (list, tuple, set)):
        return [_safe(v) for v in o]
    if hasattr(o, "item") and not isinstance(o, (str, bytes)):
        try:
            o = o.item()
        except (ValueError, TypeError):
            return str(o)
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if o is None or isinstance(o, (str, int, bool)):
        return o
    return str(o)


def _mjd_date(mjd: float | None) -> str | None:
    if mjd is None or not math.isfinite(mjd):
        return None
    return (datetime(1858, 11, 17) + timedelta(days=float(mjd))).strftime("%Y-%m-%d")


def _http_url(url: str) -> str:
    url = (url or "").strip()[:2000]
    return url if re.match(r"https?://", url, re.I) else ""


def field_key_for(ra: float, dec: float, pmra: float, pmdec: float, epoch: float, fov: float | None) -> str:
    """Same field the viewer and /api/fingerprint use: fast movers are centred at mid-survey."""
    t = Target(ra=ra, dec=dec, pmra=pmra, pmdec=pmdec, epoch_mjd=epoch)
    ra_c, dec_c = t.at(SPHEREX_REF_MJD) if math.hypot(pmra, pmdec) > PM_THRESHOLD_MASYR else (ra, dec)
    fov = fov or settings.default_fov_arcmin
    return field_key(Grid.for_fov(round(ra_c, 5), round(dec_c, 5), fov, settings.pixel_scale_arcsec))


# --------------------------------------------------------------------------- cached fingerprint
def cached_fingerprint(app, key: str, target: Target) -> dict | None:
    """A finished fingerprint for exactly this target and field, from memory or disk; never computes."""
    mgr = getattr(app.state, "fingerprints", None)
    job = getattr(mgr, "jobs", {}).get(f"{key}|{target.key()}") if mgr else None
    if job is not None and job.status == "complete" and (job.result or {}).get("status") == "complete":
        return job.result
    d = settings.cache_dir / "fingerprints" / key
    try:
        files = sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return None
    for p in files:
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
            t = r["target"]
            same = Target(ra=t["ra"], dec=t["dec"], pmra=t.get("pmra_masyr") or 0.0,
                          pmdec=t.get("pmdec_masyr") or 0.0, epoch_mjd=t["epoch_mjd"]).key() == target.key()
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if same and r.get("status") == "complete" and r.get("version") == FINGERPRINT_VERSION:
            return r
    return None


# --------------------------------------------------------------------------- images
def _data_uri(png: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


def render_images(job: FieldJob) -> list[dict]:
    """Latest two season stacks of the reddest detector that has two, and their difference."""
    by_det: dict[int, list[dict]] = {}
    for s in job.stacks.values():
        if not s.get("bin"):
            by_det.setdefault(int(s["detector"]), []).append(s)
    pick = [d for d in sorted(by_det, reverse=True) if len(by_det[d]) >= 2] or sorted(by_det, reverse=True)[:1]
    if not pick:
        return []
    det = pick[0]
    stacks = sorted(by_det[det], key=lambda s: s["season"])[-2:]
    out: list[dict] = []
    for s in stacks:
        try:
            png = render_stack_png(job, s["id"], scale="detector")
        except Exception as exc:  # noqa: BLE001 — a missing array must not sink the report
            log.warning("report: stack %s not rendered: %s", s["id"], exc)
            continue
        dates = f"{_mjd_date(s.get('mjd_min'))} to {_mjd_date(s.get('mjd_max'))}"
        out.append({"caption": f"D{det} stack, season {s['season'] + 1} ({dates}, {s['n_frames']} exposures)",
                    "data_uri": _data_uri(png)})
    if len(stacks) == 2:
        did = f"d{det}_s{stacks[0]['season']}-s{stacks[1]['season']}"
        if any(d.get("id") == did for d in (job.analysis or {}).get("diffs", [])):
            try:
                z = diff_arrays(job.key, did)["z"]
                out.append({"caption": f"D{det} difference, season {stacks[1]['season'] + 1} minus season "
                                       f"{stacks[0]['season'] + 1} (red up, blue down, saturates at 6 sigma)",
                            "data_uri": _data_uri(render_diff_png(z, 6.0))})
            except Exception as exc:  # noqa: BLE001
                log.warning("report: difference %s not rendered: %s", did, exc)
    return out


# --------------------------------------------------------------------------- the document
def data_used(job: FieldJob) -> dict:
    cells: dict[tuple[int, int], list[float | None]] = {}
    for f in job.frames.values():
        cells.setdefault((int(f.get("season", 0)), int(f["detector"])), []).append(f.get("mjd"))
    per = []
    for (s, d), mjds in sorted(cells.items()):
        ok = [m for m in mjds if m is not None]
        per.append({"season": s, "detector": d, "n": len(mjds),
                    "first": _mjd_date(min(ok)) if ok else None, "last": _mjd_date(max(ok)) if ok else None})
    checked = None
    if job.last_checked:
        checked = datetime.fromtimestamp(job.last_checked, timezone.utc).isoformat(timespec="seconds")
    return {
        "collections": list(settings.collections),
        "field": {"key": job.key, "fov_arcmin": round(job.grid.fov_arcmin, 3),
                  "pixel_scale_arcsec": job.grid.pixscale_arcsec, "ra": job.grid.ra, "dec": job.grid.dec},
        "n_exposures": len(job.frames), "n_found": job.total, "n_failed": len(job.failed),
        "failed": [{"id": k, "reason": v} for k, v in sorted(job.failed.items())],
        "seasons": [{k: s.get(k) for k in ("season", "start", "end", "n_frames", "detectors")} for s in job.seasons],
        "per_season_detector": per,
        "last_checked": checked,
        "versions": {
            "report": REPORT_VERSION, "processing": PROCESSING_VERSION, "stacks": STACK_VERSION,
            "analysis": (job.analysis or {}).get("version", ANALYSIS_VERSION),
            "analysis_code": ANALYSIS_VERSION, "fingerprint": FINGERPRINT_VERSION,
            "classification": CLASSIFY_VERSION, "explanation": EXPLAIN_VERSION,
        },
    }


def fingerprint_section(fp: dict | None) -> dict:
    if fp is None:
        return {"computed": False, "note": "Not computed yet. Run the Mover Fingerprint for this target in "
                                           "the app, then download the report again."}
    sources = fp.get("sources") or []
    return {
        "computed": True, "version": fp.get("version"),
        "classification": fp.get("classification"),
        "spectrum": fp.get("spectrum") or [],
        "light_curves": fp.get("light_curves") or [],
        "gaia_neighbours": [s for s in sources if s.get("role") == "neighbour"],
        "blend_warning": fp.get("blend_warning"), "blend_note": fp.get("blend_note"),
        "catalog": fp.get("catalog"), "method_notes": (fp.get("method") or {}).get("notes") or [],
    }


def build_report(job: FieldJob, target: Target, *, name: str = "", page_url: str = "",
                 fingerprint: dict | None = None, images: list[dict] | None = None,
                 generated: datetime | None = None) -> dict:
    """The whole report as plain, JSON-safe data. Pure: no I/O, no computation."""
    analysis = job.analysis or {}
    facts = build_facts(analysis, "target", job.seasons, job.grid.pixscale_arcsec) if "target" in analysis else None
    sections = build_sections(facts) if facts else {}
    now = generated or datetime.now(timezone.utc)
    label = name.strip() or "Custom position"
    doc = {
        "title": f"Investigation report: {label}",
        "version": REPORT_VERSION,
        "generated": now.isoformat(timespec="seconds"),
        "page_url": _http_url(page_url),
        "target": {"name": label, "ra": target.ra, "dec": target.dec, "pmra_masyr": target.pmra,
                   "pmdec_masyr": target.pmdec, "epoch_mjd": target.epoch_mjd},
        "data": data_used(job),
        "season_labels": (facts or {}).get("seasons") or _season_labels(job.seasons),
        "verdict": facts,
        "measurements": (facts or {}).get("rows", []),
        "images": images or [],
        "fingerprint": fingerprint_section(fingerprint),
        "explanation": [{"id": k, "title": SECTION_TITLES[k], "items": [it["text"] for it in sections.get(k, [])]}
                        for k in SECTION_TITLES if sections.get(k)],
        "method_notes": METHOD_NOTES,
        "limitations": LIMITATIONS,
        "credits": CREDITS,
    }
    return _safe(doc)


# --------------------------------------------------------------------------- HTML
CSS = """
:root{color-scheme:light}*{box-sizing:border-box}
body{margin:0;background:#fff;color:#1b2430;font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:920px;margin:0 auto;padding:24px 20px 48px}
h1{font-size:1.7rem;margin:0 0 4px}h2{font-size:1.2rem;margin:28px 0 8px;padding-bottom:4px;border-bottom:1px solid #d5dbe3}
h3{font-size:1rem;margin:16px 0 6px}p{margin:6px 0}.sub{color:#556070;font-size:.9rem}
table{border-collapse:collapse;width:100%;font-size:.85rem;margin:8px 0}
th,td{border:1px solid #d5dbe3;padding:3px 7px;text-align:left;vertical-align:top}
th{background:#eef2f7}td.n{text-align:right;font-variant-numeric:tabular-nums}
.badge{display:inline-block;padding:1px 9px;border-radius:10px;background:#e3ecf8;font-weight:600}
.ok{color:#17692f}.no{color:#a12727}.note{background:#fff6dd;border:1px solid #ecd48a;padding:8px 12px;border-radius:6px}
.strip{display:flex;flex-wrap:wrap;gap:12px}figure{margin:0;width:200px}
figure img{width:200px;height:200px;image-rendering:pixelated;background:#111;display:block}
figcaption{font-size:.78rem;color:#556070}ul{margin:6px 0;padding-left:22px}
.small{font-size:.8rem}a{color:#1a5fb4;word-break:break-all}
@media print{main{max-width:none;padding:0}h2{break-after:avoid}table,figure{break-inside:avoid}a{color:inherit}}
"""


def _e(x: Any) -> str:
    return html.escape("" if x is None else str(x), quote=True)


def _n(x: Any, nd: int = 2, plus: bool = False) -> str:
    if x is None or isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x):
        return "—"
    return f"{x:+.{nd}f}" if plus else f"{x:.{nd}f}"


def _pm(v: Any, e: Any, nd: int = 2) -> str:
    return "—" if v is None else f"{_n(v, nd)} ± {_n(e, nd)}"


def _table(headers: list[str], rows: list[list[Any]], num_from: int = 99) -> str:
    head = "".join(f"<th>{_e(h)}</th>" for h in headers)
    num = ' class="n"'
    body = "".join(
        "<tr>" + "".join(f"<td{num if i >= num_from else ''}>{_e(c)}</td>" for i, c in enumerate(r)) + "</tr>"
        for r in rows)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _list(items: list[str]) -> str:
    return "<ul>" + "".join(f"<li>{_e(i)}</li>" for i in items) + "</ul>" if items else ""


def _section_data(doc: dict) -> str:
    d, lab = doc["data"], doc["season_labels"]
    rows = [[f"Season {s['season'] + 1} ({s['start']} to {s['end']})", s["n_frames"],
             ", ".join(f"D{x}" for x in s["detectors"] or [])] for s in d["seasons"]]
    cov = [[lab.get(str(c["season"]), c["season"] + 1), f"D{c['detector']}", c["n"], c["first"], c["last"]]
           for c in d["per_season_detector"]]
    failed = d["failed"][:25]
    checked = f" Archive last checked {d['last_checked']}." if d["last_checked"] else ""
    parts = [
        "<h2>Data used</h2>",
        f"<p>Collections: {_e(', '.join(d['collections']))}. Field {_e(d['field']['fov_arcmin'])} arcmin across, "
        f"{_e(d['field']['pixel_scale_arcsec'])} arcsec per pixel (field {_e(d['field']['key'])}). "
        f"{d['n_exposures']} exposures processed of {d['n_found']} found; {d['n_failed']} could not be used."
        f"{_e(checked)}</p>",
        _table(["Season", "Exposures found", "Detectors"], rows, 1) if rows else "<p>No seasons.</p>",
        "<h3>Exposures per season and detector</h3>",
        _table(["Season", "Detector", "Exposures", "First", "Last"], cov, 2) if cov else "<p>None.</p>",
    ]
    if failed:
        parts += [f"<h3>Failed downloads ({d['n_failed']})</h3>",
                  _table(["Exposure", "Reason"], [[f["id"], f["reason"]] for f in failed]),
                  "<p class='small'>Only the first 25 are listed.</p>" if d["n_failed"] > 25 else ""]
    versions = "; ".join(f"{k} {x}" for k, x in d["versions"].items())
    parts.append(f"<p class='small'>Versions: {_e(versions)}.</p>")
    return "".join(parts)


def _section_verdict(doc: dict) -> str:
    f = doc["verdict"]
    if not f:
        return "<h2>Verdict</h2><p>No verdict is available for this field (no analysis was produced).</p>"
    lab = doc["season_labels"]
    checks = "".join(
        f"<li class='{'ok' if c['ok'] else 'no'}'>{'Pass' if c['ok'] else 'Fail'}: {_e(c['label'])}"
        f"{_e(' (' + c['note'] + ')') if c.get('note') else ''}</li>" for c in f["checks"])
    rows = []
    for r in doc["measurements"]:
        rows.append([
            f"D{r['detector']}",
            f"{lab.get(str(r['season_a']), r['season_a'])} vs {lab.get(str(r['season_b']), r['season_b'])}",
            r["kind"],
            f"{_n(r.get('shift_arcsec'))} ({_n(r.get('shift_sigma'), 1)} σ)" if "shift_px" in r else "—",
            f"{_n(r.get('flux_ratio'), 3)} ({_n(r.get('dflux_sigma'), 1, True)} σ)" if "flux_ratio" in r else "—",
            f"{_n(r.get('snr_a'), 1)} / {_n(r.get('snr_b'), 1)}",
            f"{_n(r.get('lam_lo_um'))} to {_n(r.get('lam_hi_um'))}" if "lam_lo_um" in r else "—",
            r.get("reason", ""),
        ])
    pm = f.get("proper_motion")
    pm_text = f" Measured proper motion {pm['pmra_masyr']} / {pm['pmdec_masyr']} mas/yr (RA, Dec)." if pm else ""
    return (
        f"<h2>Verdict</h2><p><span class='badge'>{_e(f['label'])}</span> evidence: <b>{_e(f['evidence'])}</b>; "
        f"{f['n_independent']} independent comparison(s), {f['n_detectors_agree']} of "
        f"{f['n_detectors_total']} detectors agree.{_e(pm_text)}</p>"
        f"<h3>Evidence checks</h3><ul>{checks}</ul><h3>Measurements per detector</h3>"
        + _table(["Detector", "Seasons compared", "Result", "Shift, arcsec", "Flux ratio", "S/N",
                  "Wavelength, µm", "Note"], rows)
        + "<p class='small'>Shift is the centroid change between the two seasons; flux ratio is later over "
          "earlier. σ is the measurement uncertainty.</p>")


def _section_images(doc: dict) -> str:
    if not doc["images"]:
        return "<h2>Images</h2><p>No images are available for this field.</p>"
    figs = "".join(
        f"<figure><img src=\"{_e(i['data_uri'])}\" alt=\"{_e(i['caption'])}\">"
        f"<figcaption>{_e(i['caption'])}</figcaption></figure>" for i in doc["images"])
    return f"<h2>Images</h2><div class='strip'>{figs}</div>"


def _section_fingerprint(doc: dict) -> str:
    fp = doc["fingerprint"]
    if not fp["computed"]:
        return (f"<h2>Classification and spectrum</h2><p class='note'>{_e(fp['note'])}</p>"
                "<h2>Light curves</h2><p>Not computed yet.</p>")
    out = ["<h2>Classification and spectrum</h2>"]
    c = fp.get("classification")
    if c:
        out.append(f"<p><span class='badge'>{_e(c.get('label'))}</span> confidence: <b>{_e(c.get('confidence'))}</b> "
                   f"({_e(c.get('confidence_note') or 'rule-based, not a probability')}).</p>")
        out.append(_list(c.get("reasons") or []))
        if c.get("caveats"):
            out.append("<h3>Caveats</h3>" + _list(c["caveats"]))
        ix = c.get("indices")
        if isinstance(ix, dict) and ix:
            out.append("<h3>Spectral indices</h3>" + _table(
                ["Index", "Value", "σ", "Note"],
                [[k, _pm(v.get("value"), v.get("err")), _n(v.get("sigma"), 1), v.get("note") or ""]
                 for k, v in ix.items() if isinstance(v, dict)]))
    if fp.get("blend_warning"):
        out.append(f"<p class='note'>{_e(fp.get('blend_note') or 'A neighbouring star may contaminate the aperture.')}</p>")
    spec = fp["spectrum"]
    out.append("<h3>Spectrum</h3>")
    out.append(_table(
        ["Channel", "λ, µm", "Det.", "Deblended, µJy", "Aperture, µJy", "N", "Quality"],
        [[s["channel"], _n(s.get("wavelength_um"), 3), f"D{s['detector']}",
          _pm(s.get("flux_ujy"), s.get("flux_err_ujy")), _pm(s.get("aperture_flux_ujy"), s.get("aperture_err_ujy")),
          s.get("n"), (s.get("quality") or "") + (" (wing fit)" if s.get("wing_fit") else "")] for s in spec], 1)
        if spec else "<p>No usable channels.</p>")
    lab = doc["season_labels"]
    lcs = fp["light_curves"]
    out.append("<h2>Light curves</h2>")
    out.append(_table(
        ["Bin", "λ, µm", "Flux per season, µJy", "χ² / dof", "Verdict"],
        [[b.get("bin"), f"{_n(b.get('lam_lo_um'))} to {_n(b.get('lam_hi_um'))}",
          "; ".join(f"{lab.get(str(p['season']), p['season'])}: {_pm(p.get('flux_ujy'), p.get('flux_err_ujy'))}"
                    for p in b.get("points", [])),
          f"{_n(b.get('chi2_const'), 1)} / {b.get('dof', '—')}" if b.get("matched") else "—",
          "variable" if b.get("variable") else ("consistent with constant" if b.get("matched") else "not compared")]
         for b in lcs]) if lcs else "<p>No light-curve bins.</p>")
    return "".join(out)


def _section_gaia(doc: dict) -> str:
    fp = doc["fingerprint"]
    if not fp["computed"]:
        return "<h2>Gaia neighbours</h2><p>Listed with the Mover Fingerprint, which has not been computed yet.</p>"
    nb = fp["gaia_neighbours"]
    cat = fp.get("catalog") or {}
    note = "" if cat.get("ok", True) else f"<p class='note'>{_e(cat.get('note') or 'The catalogue query failed.')}</p>"
    if not nb:
        return f"<h2>Gaia neighbours</h2>{note}<p>No {_e(CATALOG_NAME)} neighbours were selected near the target.</p>"
    return "<h2>Gaia neighbours</h2>" + note + _table(
        ["Gaia source", "Separation, arcsec", "G mag", "PM RA, mas/yr", "PM Dec, mas/yr", "Variable"],
        [[s.get("gaia_id") or s.get("id"), _n(s.get("sep_arcsec")), _n(s.get("g_mag"), 2), _n(s.get("pmra_masyr"), 1),
          _n(s.get("pmdec_masyr"), 1), {True: "yes", False: "no"}.get(s.get("variable"), "unknown")] for s in nb], 1)


def render_html(doc: dict) -> str:
    t = doc["target"]
    moving = bool(t["pmra_masyr"] or t["pmdec_masyr"])
    pm = (f", proper motion {_n(t['pmra_masyr'], 1)} / {_n(t['pmdec_masyr'], 1)} mas/yr at MJD "
          f"{_n(t['epoch_mjd'], 0)}") if moving else ""
    link = f"<a href=\"{_e(doc['page_url'])}\">{_e(doc['page_url'])}</a>" if doc["page_url"] else "not provided"
    expl = "".join(f"<h3>{_e(s['title'])}</h3>{_list(s['items'])}" for s in doc["explanation"])
    notes = list(doc["method_notes"]) + list(doc["fingerprint"].get("method_notes") or [])
    body = [
        f"<h1>{_e(doc['title'])}</h1>",
        f"<p class='sub'>RA {_n(t['ra'], 5)}°, Dec {_n(t['dec'], 5)}°{_e(pm)}<br>Generated {_e(doc['generated'])}"
        f"<br>Interactive view: {link}</p>",
        _section_data(doc), _section_verdict(doc), _section_images(doc), _section_fingerprint(doc),
        "<h2>In plain language</h2>" + (expl or "<p>No explanation is available.</p>"),
        _section_gaia(doc),
        "<h2>Method notes</h2>" + _list(notes),
        f"<h2>Limitations</h2><p class='note'>{_e(doc['limitations'])}</p>",
        "<h2>Data credits</h2>" + _list(doc["credits"]),
    ]
    return ("<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<meta name=\"robots\" content=\"noindex\">"
            f"<title>{_e(doc['title'])}</title><style>{CSS}</style></head><body><main>"
            + "".join(body) + "</main></body></html>")


# --------------------------------------------------------------------------- route
@router.get("/api/investigation")
async def investigation(
    request: Request,
    key: str | None = Query(None, description="field key; derived from ra/dec/fov when omitted"),
    ra: str | None = None,
    dec: str | None = None,
    pmra: float = Query(0.0, ge=-30000, le=30000),
    pmdec: float = Query(0.0, ge=-30000, le=30000),
    epoch: float = Query(SPHEREX_REF_MJD, ge=30000, le=90000),
    fov: float | None = Query(None, gt=0, le=60),
    url: str = Query("", max_length=4000, description="the shareable page URL"),
    name: str = Query("", max_length=200),
    format: str = Query("html", pattern="^(html|json)$"),
):
    """One self-contained report for a finished field. Never starts any computation."""
    try:
        ra_d = parse_ra(ra) if ra else None
        dec_d = parse_dec(dec) if dec else None
    except CoordError as exc:
        raise HTTPException(422, str(exc)) from exc
    if key is None:
        if ra_d is None or dec_d is None:
            raise HTTPException(422, "give either key or ra and dec")
        key = field_key_for(ra_d, dec_d, pmra, pmdec, epoch, fov)
    job = request.app.state.fields.jobs.get(key)
    if job is None:
        raise HTTPException(404, "unknown field (open the target in the app first)")
    if job.status != "complete":
        raise HTTPException(409, f"the field is still being built ({job.status}); try again when it is complete")
    target = Target(ra=job.grid.ra if ra_d is None else ra_d, dec=job.grid.dec if dec_d is None else dec_d,
                    pmra=pmra, pmdec=pmdec, epoch_mjd=epoch)
    fp = await asyncio.to_thread(cached_fingerprint, request.app, key, target)
    images = await asyncio.to_thread(render_images, job)
    doc = await asyncio.to_thread(build_report, job, target, name=name, page_url=url, fingerprint=fp, images=images)
    if format == "json":
        return Response(json.dumps(doc, allow_nan=False), media_type="application/json")
    slug = re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-")[:40] or key
    return Response(render_html(doc), media_type="text/html; charset=utf-8",
                    headers={"Content-Disposition": f'inline; filename="spherex-investigation-{slug}.html"',
                             "Cache-Control": "no-store"})
