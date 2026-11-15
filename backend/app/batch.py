"""Batch triage: a pasted/uploaded list of targets, run as a background queue, ranked in a table.

``POST /api/batch`` parses the list and starts ONE background worker for it. The worker takes
the rows one at a time: build the field (FieldManager), wait for ``complete``, run the Mover
Fingerprint (FingerprintManager), then store a flat summary. While measuring row *i* it may
start the field of row *i+1* (so at most 2 fields of a batch build at once) and only when the
shared build queue still has a free slot, so interactive visitors are never starved.

Batches live in memory while running and are persisted as ``cache/batches/{id}.json`` after
every row transition, so a shared link survives restarts (an unfinished batch resumes the
first time its link is opened again). Managers are reached through ``request.app.state``.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from .config import settings
from .coords import (CoordError, J2000_MJD, PM_THRESHOLD_MASYR, SPHEREX_REF_MJD, parse_dec, parse_ra,
                     resolve_name)
from .processing import Grid

log = logging.getLogger("spherex.batch")
router = APIRouter()

DEFAULT_MAX_ROWS = 200
POLL_S = 2.0                                  # worker poll interval (tests shorten it)
ROW_TIMEOUT_S = float(os.environ.get("SPHEREX_BATCH_ROW_TIMEOUT_S", 1800))
MAX_RUNNING_BATCHES = int(os.environ.get("SPHEREX_BATCH_RUNNING_MAX", 3))
BAND_4P5 = (4.3, 4.7)                         # um, the methane-free window used for the flux column
ID_RE = re.compile(r"^[A-Za-z0-9_-]{4,40}$")
FINAL = ("complete", "error")


def batch_max() -> int:
    try:
        return max(1, int(os.environ.get("SPHEREX_BATCH_MAX", DEFAULT_MAX_ROWS)))
    except ValueError:
        return DEFAULT_MAX_ROWS


def batch_dir() -> Path:
    return settings.cache_dir / "batches"


# ---- parsing --------------------------------------------------------------------------------

_DEC = re.compile(r"^[+-]?\d+(\.\d*)?([eE][+-]?\d+)?$")
_SEXA = re.compile(r"^[+-]?\d{1,3}([:hdms°'\"]+\d*\.?\d*)+[hdms°'\"]*$|^[+-]?\d{1,3}[hd]\d.*$")
_COLS = {
    "ra": {"ra", "ra_deg", "raj2000", "ra_j2000"},
    "dec": {"dec", "dec_deg", "dej2000", "dec_j2000", "decl"},
    "pmra": {"pmra", "pm_ra", "pmra_masyr", "pmra_mas_yr", "pmra_mas/yr"},
    "pmdec": {"pmdec", "pm_dec", "pmdec_masyr", "pmdec_mas_yr", "pmdec_mas/yr", "pmde"},
    "name": {"name", "label", "target", "object", "id", "comment"},
}


def _num(s: str) -> float | None:
    s = s.strip()
    return float(s) if _DEC.match(s) else None


def _coords(ra: str, dec: str) -> tuple[float, float]:
    return parse_ra(ra), parse_dec(dec)


def _pm(a: str | None, b: str | None) -> tuple[float, float] | None:
    x, y = (_num(a) if a is not None else None), (_num(b) if b is not None else None)
    if x is None or y is None:
        raise CoordError("proper motion needs two numbers (pmra pmdec, mas/yr)")
    if abs(x) > 30000 or abs(y) > 30000:
        raise CoordError("proper motion out of range (|pm| <= 30000 mas/yr)")
    return x, y


def _split_line(body: str) -> tuple[tuple[str, str] | None, tuple[str, str] | None, str, str | None]:
    """-> (ra/dec text, pm text, label from extra columns, name to resolve). Raises CoordError."""
    if "," in body:
        cols = [c.strip() for c in next(csv.reader([body], skipinitialspace=True))]
        try:
            _coords(cols[0], cols[1] if len(cols) > 1 else "")
        except CoordError:
            if _DEC.match(cols[0]) or _SEXA.match(cols[0]):
                raise                              # looks like a position, but is not a valid one
            return None, None, " ".join(cols[1:]), cols[0]   # "Name, free-text label"
        rest = cols[2:]
        pm = None
        if len(rest) >= 2 and _num(rest[0]) is not None and _num(rest[1]) is not None:
            pm, rest = (rest[0], rest[1]), rest[2:]
        return (cols[0], cols[1]), pm, " ".join(c for c in rest if c), None
    tok = body.split()
    lead = 0
    while lead < len(tok) and _DEC.match(tok[lead]):
        lead += 1
    if tok and (_SEXA.match(tok[0]) or lead >= 2) and len(tok) >= 2:
        if lead >= 6 and re.fullmatch(r"\d+", tok[0]) and re.fullmatch(r"\d+", tok[1]):
            pos, rest = (" ".join(tok[:3]), " ".join(tok[3:6])), tok[6:]   # 08 55 10.8 -07 14 42.5
        else:
            pos, rest = (tok[0], tok[1]), tok[2:]
        pm = None
        if rest and _DEC.match(rest[0]):
            if len(rest) < 2 or not _DEC.match(rest[1]):
                raise CoordError("proper motion needs two numbers (pmra pmdec, mas/yr)")
            pm, rest = (rest[0], rest[1]), rest[2:]
        return pos, pm, " ".join(rest), None
    if lead == 1 and (len(tok) == 1 or "." in tok[0]):
        raise CoordError("RA given without a valid Dec; give 'ra dec' or an object name")
    return None, None, "", body.strip()


def _header(line: str) -> list[str] | None:
    delim = "\t" if "\t" in line and "," not in line else ","
    cols = [c.strip().strip('"').lower() for c in line.split(delim)]
    vocab = {a for v in _COLS.values() for a in v}
    if all(c in vocab for c in cols) and (len(cols) >= 2 or cols[0] in ("name", "target", "object")):
        return [next(k for k, v in _COLS.items() if c in v) for c in cols]
    return None


def parse_targets(text: str) -> list[dict]:
    """One dict per target line: i, input, label, name, ra, dec, pmra, pmdec, error.

    Accepted lines (blank lines and lines starting with ``#`` are skipped):
      ``ra dec [pmra pmdec] [# label]``   decimal degrees or sexagesimal (08:55:10.8 -07:14:42,
      ``08h55m10.8s -07d14m42s`` or ``08 55 10.8 -07 14 42``), proper motion in mas/yr
      ``ra, dec[, pmra, pmdec][, label...]``  comma separated
      ``Object name [# label]``           resolved with Sesame when the row runs
      CSV with a header row (``ra,dec,pmra,pmdec,name``), columns in any order.
    A bad line becomes a row with ``error`` set; it never fails the batch.
    """
    out: list[dict] = []
    header: list[str] | None = None
    first = True
    for raw in text.splitlines():
        line = raw.strip().lstrip("﻿")
        if not line or line.startswith("#"):
            continue
        if first:
            first = False
            header = _header(line)
            if header:
                continue
        row = {"i": len(out) + 1, "input": line, "label": "", "name": None, "ra": None, "dec": None,
               "pmra": None, "pmdec": None, "error": None}
        out.append(row)
        try:
            if header:
                delim = "\t" if "\t" in line and "," not in line else ","
                vals = [v.strip() for v in next(csv.reader([line], delimiter=delim))]
                d = {k: vals[j] for j, k in enumerate(header) if j < len(vals)}
                row["label"] = d.get("name", "")
                if d.get("ra") or d.get("dec"):
                    row["ra"], row["dec"] = _coords(d.get("ra", ""), d.get("dec", ""))
                    if d.get("pmra") or d.get("pmdec"):
                        row["pmra"], row["pmdec"] = _pm(d.get("pmra"), d.get("pmdec"))
                elif d.get("name"):
                    row["name"] = d["name"]
                else:
                    raise CoordError("no coordinates and no name")
                continue
            body, _, comment = line.partition("#")
            body = body.strip()
            if not body:
                raise CoordError("empty line")
            pos, pm, label, name = _split_line(body)
            row["label"] = comment.strip() or label or (name or "")
            if pos:
                row["ra"], row["dec"] = _coords(*pos)
                if pm:
                    row["pmra"], row["pmdec"] = _pm(*pm)
            else:
                row["name"] = name
        except CoordError as exc:
            row["error"] = str(exc)
        except Exception as exc:  # noqa: BLE001 - malformed CSV etc.: record, don't fail the batch
            row["error"] = f"cannot parse line: {exc}"
    return out


# ---- batch model ----------------------------------------------------------------------------

def new_row(p: dict, epoch: float) -> dict:
    ra = p["ra"]
    return {
        "i": p["i"], "label": p["label"] or (f"{ra:.5f} {p['dec']:+.5f}" if ra is not None else p["name"] or ""),
        "input": p["input"], "name": p["name"], "ra": ra, "dec": p["dec"],
        "pmra_masyr": p["pmra"], "pmdec_masyr": p["pmdec"],
        "epoch_mjd": epoch if p["pmra"] is not None else SPHEREX_REF_MJD,
        "status": "error" if p["error"] else "queued", "error": p["error"], "stage": None,
        "field_key": None, "n_exposures": None, "n_seasons": None, "verdict": None, "evidence": None,
        "measured_pm_masyr": None, "flux_4p5_ujy": None, "flux_err_4p5_ujy": None, "snr_4p5": None,
        "ch4_index": None, "ch4_sigma": None, "class_label": None, "class_confidence": None,
        "blend_warning": None, "thumb_url": None, "view_url": None,
    }


@dataclass
class Batch:
    id: str
    fov: float
    created: float
    rows: list[dict]
    cancelled: bool = False
    task: asyncio.Task | None = field(default=None, repr=False)

    @property
    def done(self) -> int:
        return sum(r["status"] in FINAL for r in self.rows)

    @property
    def status(self) -> str:
        return "complete" if self.done == len(self.rows) else "running"

    def to_dict(self) -> dict:
        rows = [{k: v for k, v in r.items() if k not in ("name", "epoch_mjd")} for r in self.rows]
        return {"id": self.id, "created": self.created, "fov": self.fov, "status": self.status,
                "cancelled": self.cancelled, "done": self.done, "total": len(self.rows), "rows": rows}

    def save(self) -> None:
        path = batch_dir() / f"{self.id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({**self.to_dict(), "rows": self.rows}))
        tmp.replace(path)


def load_batch(bid: str) -> Batch | None:
    path = batch_dir() / f"{bid}.json"
    if not path.is_file():
        return None
    d = json.loads(path.read_text())
    return Batch(id=d["id"], fov=d["fov"], created=d["created"], rows=d["rows"], cancelled=d.get("cancelled", False))


def _registry(app) -> dict[str, Batch]:
    if not hasattr(app.state, "batches"):
        app.state.batches = {}
    return app.state.batches


def _get(request: Request, bid: str) -> Batch:
    if not ID_RE.match(bid):
        raise HTTPException(404, "unknown batch")
    reg = _registry(request.app)
    b = reg.get(bid)
    if b is None:
        b = load_batch(bid)
        if b is None:
            raise HTTPException(404, "unknown batch")
        reg[bid] = b
    if b.status == "running" and not b.cancelled and (b.task is None or b.task.done()):
        for r in b.rows:                      # server restarted mid-batch: resume the unfinished rows
            if r["status"] not in FINAL:
                r["status"], r["stage"] = "queued", None
        _start(request.app, b)
    return b


# ---- worker ---------------------------------------------------------------------------------

class RowError(Exception):
    pass


def grid_for(row: dict, fov: float) -> tuple[Grid, "Target"]:
    from .fingerprint import Target
    target = Target(ra=row["ra"], dec=row["dec"], pmra=row["pmra_masyr"] or 0.0,
                    pmdec=row["pmdec_masyr"] or 0.0, epoch_mjd=row["epoch_mjd"])
    # Same field centre rule as /api/fingerprint and the viewer (coords.field_centre).
    if (target.pmra**2 + target.pmdec**2) ** 0.5 > PM_THRESHOLD_MASYR:
        ra, dec = target.at(SPHEREX_REF_MJD)
    else:
        ra, dec = row["ra"], row["dec"]
    return Grid.for_fov(round(ra, 5), round(dec, 5), fov, settings.pixel_scale_arcsec), target


def view_query(row: dict) -> str:
    q = {"ra": f"{row['ra']:.6f}", "dec": f"{row['dec']:+.6f}", "name": row["label"]}
    if row["pmra_masyr"] is not None:
        q.update(pmra=f"{row['pmra_masyr']:.1f}", pmdec=f"{row['pmdec_masyr']:.1f}",
                 pmepoch=f"{row['epoch_mjd']:g}")
    return "?" + urlencode(q)


def band_flux(spectrum: list[dict], lo: float = BAND_4P5[0], hi: float = BAND_4P5[1]):
    """Inverse-variance mean flux of the channels in [lo, hi] um -> (flux, err, snr) or Nones."""
    num = den = 0.0
    for c in spectrum or []:
        f, e, w = c.get("flux_ujy"), c.get("flux_err_ujy"), c.get("wavelength_um")
        if f is None or not e or e <= 0 or w is None or not lo <= w <= hi:
            continue
        num += f / e**2
        den += 1 / e**2
    if den == 0:
        return None, None, None
    flux, err = num / den, den ** -0.5
    return flux, err, flux / err


def summarise(row: dict, job, res: dict | None) -> None:
    """Fill the result columns of ``row`` from a finished field job and fingerprint result."""
    target = (getattr(job, "analysis", None) or {}).get("target") or {}
    row.update(
        field_key=job.key, n_exposures=len(job.frames), n_seasons=len(job.seasons),
        verdict=target.get("summary") or "no data", evidence=target.get("evidence"),
        measured_pm_masyr=[target["pmra_masyr"], target["pmdec_masyr"]] if "pmra_masyr" in target else None,
    )
    stacks = [s for s in job.stacks.values() if s.get("bin") is None and s.get("visit") is None]
    if stacks:
        s = max(stacks, key=lambda s: (s["detector"], s["season"]))
        row["thumb_url"] = s.get("png") or f"/api/fields/{job.key}/stacks/{s['id']}.png"
    if res is None:
        return
    flux, err, snr = band_flux(res.get("spectrum", []))
    row.update(flux_4p5_ujy=flux, flux_err_4p5_ujy=err, snr_4p5=snr, blend_warning=res.get("blend_warning"))
    cl = res.get("classification")
    if isinstance(cl, dict):
        row.update(class_label=cl.get("label"), class_confidence=cl.get("confidence"))
        for name, ix in (cl.get("indices") or {}).items():
            if re.search(r"ch4|methane", name, re.I) and isinstance(ix, dict):
                row.update(ch4_index=ix.get("value"), ch4_sigma=ix.get("sigma"))
                break


async def _wait(b: Batch, deadline: float) -> None:
    await asyncio.sleep(POLL_S)
    if b.cancelled:
        raise RowError("cancelled")
    if time.monotonic() > deadline:
        raise RowError(f"timed out after {ROW_TIMEOUT_S:.0f} s")


def _free_slot(fields) -> bool:
    return getattr(fields, "_admitted", 0) < max(1, settings.max_concurrent_builds - 1)


async def _process(app, b: Batch, row: dict, ahead: dict | None) -> None:
    fields, fps = app.state.fields, app.state.fingerprints
    client = f"batch:{b.id}"
    deadline = time.monotonic() + ROW_TIMEOUT_S
    row["status"], row["stage"] = "building", "resolving"
    b.save()
    if row["ra"] is None:
        try:
            r = await resolve_name(row["name"], fields.client)
        except CoordError as exc:
            raise RowError(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - network failure: this row only
            raise RowError(f"name lookup failed: {type(exc).__name__}: {exc}") from exc
        pm = (r.pm_ra_masyr or 0.0, r.pm_dec_masyr or 0.0)
        row.update(ra=r.ra, dec=r.dec, label=row["label"] if row["label"] != row["name"] else r.name)
        if any(pm):
            row.update(pmra_masyr=pm[0], pmdec_masyr=pm[1], epoch_mjd=J2000_MJD)
    grid, target = grid_for(row, b.fov)
    row["view_url"] = view_query(row)
    if ahead is not None and ahead["ra"] is not None and _free_slot(fields):
        fields.get_or_start(grid_for(ahead, b.fov)[0], client=client)   # lookahead: <= 2 builds
    job = fields.get_or_start(grid, client=client)
    row["field_key"], row["stage"] = job.key, "field"
    while job.status != "complete":
        if job.status in ("error", "cancelled"):
            raise RowError(f"field build failed: {job.error or job.status}")
        row["progress"] = [len(job.frames), job.total]
        await _wait(b, deadline)
        job = fields.jobs.get(job.key) or fields.get_or_start(grid, client=client)
        job.touch(client)
        if ahead is not None and ahead["ra"] is not None and (aj := fields.jobs.get(grid_key(ahead, b.fov))):
            aj.touch(client)
    row.pop("progress", None)
    if not job.frames:
        summarise(row, job, None)
        row.update(status="complete", stage=None, verdict="no data", error="no SPHEREx exposures at this position")
        return
    row["status"], row["stage"] = "measuring", "fingerprint"
    b.save()
    while True:
        res = await fps.request(fields, grid, target)
        if res["status"] == "complete":
            break
        if res["status"] == "error":
            raise RowError(res.get("error") or "fingerprint failed")
        row["stage"] = f"fingerprint: {res.get('stage', '')}"
        await _wait(b, deadline)
    summarise(row, job, res)
    row.update(status="complete", stage=None, error=None)


def grid_key(row: dict, fov: float) -> str:
    from .fields import field_key
    return field_key(grid_for(row, fov)[0])


def _finish(b: Batch, why: str = "cancelled") -> None:
    for r in b.rows:
        if r["status"] not in FINAL:
            r.update(status="error", error=why, stage=None)
    b.save()


async def _run(app, b: Batch) -> None:
    try:
        todo = [r for r in b.rows if r["status"] not in FINAL]
        for k, row in enumerate(todo):
            if b.cancelled:
                break
            try:
                await _process(app, b, row, todo[k + 1] if k + 1 < len(todo) else None)
            except RowError as exc:
                row.update(status="error", error=str(exc), stage=None)
            except Exception as exc:  # noqa: BLE001 - one bad row must not kill the batch
                log.exception("batch %s row %s failed", b.id, row["i"])
                row.update(status="error", error=f"{type(exc).__name__}: {exc}", stage=None)
            b.save()
    except asyncio.CancelledError:
        if not b.cancelled:
            raise                              # server shutdown: keep the rows resumable
    finally:
        if b.cancelled:
            _finish(b)


def _start(app, b: Batch) -> None:
    b.task = asyncio.create_task(_run(app, b))


# ---- routes ---------------------------------------------------------------------------------

class BatchRequest(BaseModel):
    targets: str = Field(..., max_length=400_000)
    fov: float | None = None
    epoch_mjd: float = Field(SPHEREX_REF_MJD, ge=30000, le=90000,
                             description="epoch of ra/dec for rows that carry a proper motion")


@router.post("/api/batch")
async def create_batch(body: BatchRequest, request: Request):
    fov = body.fov or settings.default_fov_arcmin
    if not 0.5 <= fov <= settings.max_fov_arcmin:
        raise HTTPException(422, f"fov must be between 0.5 and {settings.max_fov_arcmin} arcmin")
    parsed = parse_targets(body.targets)
    if not parsed:
        raise HTTPException(422, "no targets found: give one target per line, e.g. '133.76 -7.24'")
    if len(parsed) > batch_max():
        raise HTTPException(422, f"too many targets: {len(parsed)} rows, the limit is {batch_max()} per batch "
                                 "(split the list)")
    reg = _registry(request.app)
    if sum(b.status == "running" and not b.cancelled for b in reg.values()) >= MAX_RUNNING_BATCHES:
        raise HTTPException(429, "the server is already running several batches; try again in a few minutes",
                            headers={"Retry-After": "120"})
    limiter = getattr(request.app.state, "limiter", None)
    if limiter is not None:
        from .limits import BUILD
        limiter.consume(request, BUILD)
    b = Batch(id=secrets.token_urlsafe(6), fov=fov, created=time.time(),
              rows=[new_row(p, body.epoch_mjd) for p in parsed])
    reg[b.id] = b
    b.save()
    if b.status == "running":
        _start(request.app, b)
    return {"id": b.id, "n": len(b.rows)}


# The .csv / .json routes must be registered before the bare {bid} route.
CSV_COLUMNS = ["i", "label", "input", "ra", "dec", "pmra_masyr", "pmdec_masyr", "status", "error", "field_key",
               "n_exposures", "n_seasons", "verdict", "evidence", "measured_pmra_masyr", "measured_pmdec_masyr",
               "flux_4p5_ujy", "flux_err_4p5_ujy", "snr_4p5", "ch4_index", "ch4_sigma", "class_label",
               "class_confidence", "blend_warning", "view_url"]


def rows_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, CSV_COLUMNS, extrasaction="ignore", lineterminator="\n")
    w.writeheader()
    for r in rows:
        pm = r.get("measured_pm_masyr") or [None, None]
        w.writerow({**r, "measured_pmra_masyr": pm[0], "measured_pmdec_masyr": pm[1]})
    return buf.getvalue()


def _download(data: str, media: str, bid: str, ext: str) -> Response:
    return Response(data, media_type=media,
                    headers={"Content-Disposition": f'attachment; filename="batch-{bid}.{ext}"'})


@router.get("/api/batch/{bid}.csv")
async def batch_csv(bid: str, request: Request):
    return _download(rows_csv(_get(request, bid).to_dict()["rows"]), "text/csv", bid, "csv")


@router.get("/api/batch/{bid}.json")
async def batch_json(bid: str, request: Request):
    return _download(json.dumps(_get(request, bid).to_dict()["rows"], indent=1), "application/json", bid, "json")


@router.get("/api/batch/{bid}")
async def get_batch(bid: str, request: Request):
    return _get(request, bid).to_dict()


@router.delete("/api/batch/{bid}")
async def cancel_batch(bid: str, request: Request):
    b = _get(request, bid)
    b.cancelled = True
    if b.task is not None and not b.task.done():
        b.task.cancel()                       # the worker's finally-block marks the rest cancelled
        await asyncio.gather(b.task, return_exceptions=True)
    _finish(b)
    return b.to_dict()
