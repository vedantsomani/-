"""Catalogue overlay: Gaia DR3 sources on the field grid, and what they say about each candidate.

GET /api/overlay?key=<field key>
  - starts a cached background Gaia query (IRSA TAP, 3-80 s) and answers {"status": "processing"}
    until it and the field analysis are ready;
  - a failed query answers {"status": "complete", "catalog": {"ok": false, "note": ...}, "sources": []};
  - otherwise lists the Gaia sources in the field with their positions propagated (linear proper
    motion, no parallax) to MJD 61000 and to the mean MJD of every SPHEREx season, as pixel
    coordinates of the field grid (0-based, +x west, +y north: the convention of the candidates),
    and, per analysis candidate, the nearest Gaia source within 6" at the candidate's epoch, a
    label and ``candidate_new_object``.

CatWISE2020 (IRSA TAP, same cache/failure handling) is queried alongside: it reaches the very red, faint
objects Gaia misses and carries proper motions. Its sources that are red (W1-W2 > 0.8) or moving
(motion S/N > 5 and > 100 mas/yr), or that match a candidate, come back as ``catwise_sources`` with the
same per-season grid pixels; each candidate gets ``catwise_match`` (nearest source within 6" at its
epoch; the CatWISE motion is used only when its S/N > 5). A CatWISE failure only sets
``catwise: {ok: false, note}``.

Absence from Gaia and CatWISE2020 is NOT proof that an object is new: both are incomplete for very
faint or confused objects, and a match within a few arcsec can be a chance alignment.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

import numpy as np
from fastapi import APIRouter, HTTPException, Request

from .catalogs import (
    CATALOG_NAME, CATWISE_NAME, CATWISE_PM_EPOCH_MJD, CATWISE_PM_SNR_MIN, CATWISE_RED_W1W2, GAIA_DR3_EPOCH_MJD,
    CatalogResult, CatwiseResult, CatwiseSource, catwise_cone, gaia_cone,
)
from .catalogs import Star, WiseResult, WISE_CATALOG_NAME, WISE_EPOCH_MJD, WISE_PM_SIGMA_MAX, wise_cone
from .config import settings
from .coords import SPHEREX_REF_MJD
from .limits import client_ip
from .processing import Grid

log = logging.getLogger(__name__)
router = APIRouter()

L_HPM = "known high-proper-motion star"
L_VAR = "known Gaia variable"
L_CAT = "catalogued, not known to move or vary"
L_NONE = "no Gaia match"
L_CW_MOVER = "known mover in CatWISE (not in Gaia)"
CW_DRAW_MIN_PM_MASYR = 100.0   # a CatWISE source is returned when red, or moving faster than this
DIRECTION_TOL_DEG = 45.0
SPEED_FACTOR = 2.0
GRID_NOTE = "x, y: 0-based pixel on the field grid, +x west, +y north"

NOTES = [
    "Absence from Gaia and CatWISE2020 is not proof of novelty: Gaia is incomplete for very red or faint "
    "objects (many brown dwarfs are missing from it) and CatWISE2020 for faint or confused ones. "
    "candidate_new_object only means 'a mover with strong or moderate evidence and no Gaia DR3 or "
    f"CatWISE2020 source within {settings.overlay_novelty_arcsec:.0f} arcsec'; it still needs checking "
    "against other catalogues.",
    "Gaia positions are at J2016.0 and are moved with linear proper motion (parallax ignored). "
    "Sources with a 2-parameter solution have no proper motion and are treated as stationary.",
    "CatWISE2020 positions are at MJD 57170 (2015.4) and are moved with its fitted proper motion only when "
    f"that motion has S/N > {CATWISE_PM_SNR_MIN:.0f}; otherwise the source is treated as stationary.",
    "A catalogue match within a few arcsec can be a chance alignment in crowded fields.",
]


# ------------------------------------------------------------------ geometry (pure)
def star_arrays(stars) -> dict[str, np.ndarray]:
    f = np.float64
    return {
        "ra": np.array([s.ra for s in stars], f), "dec": np.array([s.dec for s in stars], f),
        "pmra": np.array([s.pmra for s in stars], f), "pmdec": np.array([s.pmdec for s in stars], f),
        "epoch": np.array([s.epoch_mjd for s in stars], f),
    }


def propagate_pixels(arr: dict[str, np.ndarray], wcs, mjd: float) -> tuple[np.ndarray, np.ndarray]:
    """Grid pixel coordinates (0-based) of every star at ``mjd``."""
    dt = (mjd - arr["epoch"]) / 365.25
    ra = arr["ra"] + arr["pmra"] * dt / 3.6e6 / np.cos(np.radians(arr["dec"]))
    dec = arr["dec"] + arr["pmdec"] * dt / 3.6e6
    x, y = wcs.world_to_pixel_values(ra % 360.0, dec)
    return np.asarray(x, float), np.asarray(y, float)


def season_epochs(frames: list[dict], seasons: list[dict]) -> dict[int, float]:
    """Mean MJD of the exposures of each season."""
    by: dict[int, list[float]] = {}
    for f in frames:
        m = f.get("mjd")
        if m is not None and math.isfinite(m) and f.get("season") is not None:
            by.setdefault(int(f["season"]), []).append(float(m))
    out = {s: sum(v) / len(v) for s, v in by.items()}
    for s in seasons:
        out.setdefault(int(s["season"]), (s["mjd_start"] + s["mjd_end"]) / 2)
    return dict(sorted(out.items()))


def candidate_epoch(cand: dict, epochs: dict[int, float]) -> float:
    """MJD at which a candidate's grid position applies: the target sits at the field-centre
    epoch; others at the mean epoch of the season pairs of their events."""
    mean = sum(epochs.values()) / len(epochs) if epochs else SPHEREX_REF_MJD
    if cand.get("is_target"):
        return SPHEREX_REF_MJD
    mids = []
    for e in cand.get("events") or []:
        a, b = epochs.get(e.get("season_a")), epochs.get(e.get("season_b"))
        if a is not None and b is not None:
            mids.append((a + b) / 2)
    return sum(mids) / len(mids) if mids else mean


def motion_agrees(meas: tuple[float, float], gaia: tuple[float, float]) -> bool:
    """Gaia motion within 45 deg of the measured direction and within a factor 2 of its speed."""
    sm, sg = math.hypot(*meas), math.hypot(*gaia)
    if sm <= 0 or sg <= 0:
        return False
    cos = (meas[0] * gaia[0] + meas[1] * gaia[1]) / (sm * sg)
    if cos < math.cos(math.radians(DIRECTION_TOL_DEG)):
        return False
    return 1 / SPEED_FACTOR <= sg / sm <= SPEED_FACTOR


def label_candidate(cand: dict, match: dict | None, cw_match: dict | None = None) -> tuple[str, str | None]:
    """(label, note). The rule, in order:
    no Gaia match within 6" but a CatWISE match whose motion (S/N > 5) agrees with
    the candidate's (direction within 45 deg, speed within x2)       -> 'known mover in CatWISE (not in Gaia)'
    no match within 6"                                              -> 'no Gaia match'
    Gaia motion > 150 mas/yr (and, for a mover candidate with a measured motion,
    agreeing in direction and speed)                                -> 'known high-proper-motion star'
    Gaia phot_variable_flag == VARIABLE                             -> 'known Gaia variable'
    otherwise                                                       -> 'catalogued, not known to move or vary'"""
    if match is None:
        if cw_match is not None and cw_match.get("pm_agrees") and cand.get("kind") == "mover":
            return L_CW_MOVER, (f"CatWISE2020 {cw_match['source_name']} lies {cw_match['separation_arcsec']:.1f} arcsec "
                                f"away and moves {cw_match['pm_total_masyr']:.0f} mas/yr in the measured direction.")
        if cw_match is not None and cand.get("kind") == "mover":
            return L_NONE, (f"CatWISE2020 {cw_match['source_name']} lies within "
                            f"{settings.overlay_match_arcsec:.0f} arcsec but its motion does not match this candidate.")
        return L_NONE, None
    pm = match.get("pm_total_masyr")
    fast = pm is not None and pm > settings.overlay_hpm_masyr
    constrained = cand.get("kind") == "mover" and cand.get("pmra_masyr") is not None
    if fast and (not constrained or match.get("pm_agrees")):
        return L_HPM, None
    note = None
    if fast:
        note = (f"The Gaia source moves at {pm:.0f} mas/yr, but not in the direction and at the speed "
                "measured for this candidate: it may be a different object.")
    if match.get("variable"):
        return L_VAR, note
    return L_CAT, note


def catwise_arrays(sources: list[CatwiseSource]) -> dict[str, np.ndarray]:
    """Like star_arrays, but a motion is used only when significant (S/N > CATWISE_PM_SNR_MIN)."""
    f = np.float64
    return {
        "ra": np.array([s.ra for s in sources], f), "dec": np.array([s.dec for s in sources], f),
        "pmra": np.array([s.pmra if s.moving else 0.0 for s in sources], f),
        "pmdec": np.array([s.pmdec if s.moving else 0.0 for s in sources], f),
        "epoch": np.array([s.epoch_mjd for s in sources], f),
    }


@dataclass
class CatwiseContext:
    """CatWISE inputs for candidate matching. ``checked`` is False when the query failed."""
    sources: list[CatwiseSource]
    px_at: Callable[[float], tuple]
    checked: bool = True


def catwise_row(s: CatwiseSource, **extra) -> dict:
    """The CatWISE fields shared by a candidate's ``catwise_match`` and a ``catwise_sources`` row."""
    w12 = s.w1_w2
    return {
        "source_name": s.name, "ra": round(s.ra, 7), "dec": round(s.dec, 7),
        "w1": round(s.w1, 3) if s.w1 is not None else None, "w2": round(s.w2, 3) if s.w2 is not None else None,
        "w1_w2": round(w12, 3) if w12 is not None else None,
        "pmra_masyr": round(s.pmra, 1), "pmdec_masyr": round(s.pmdec, 1),
        "pm_total_masyr": round(s.pm_total, 1),
        "pm_snr": round(s.pm_snr, 1) if s.pm_snr is not None else None, **extra,
    }


def evaluate_candidate(cand: dict, arr: dict, px_at: Callable[[float], tuple], stars, epochs: dict[int, float],
                       pixscale: float, cw: CatwiseContext | None = None) -> dict:
    cx, cy = float(cand["x"]), float(cand["y"])
    t = candidate_epoch(cand, epochs)

    def seps(mjd: float) -> np.ndarray:
        x, y = px_at(mjd)
        return np.hypot(x - cx, y - cy) * pixscale

    s0 = seps(t)
    i = int(np.argmin(s0)) if len(s0) else -1
    nearest = float(s0[i]) if i >= 0 else None
    match = None
    if i >= 0 and nearest <= settings.overlay_match_arcsec:
        st = stars[i]
        pm_total = math.hypot(st.pmra, st.pmdec) if st.has_pm else None
        agrees = None
        if cand.get("kind") == "mover" and cand.get("pmra_masyr") is not None and st.has_pm:
            agrees = motion_agrees((cand["pmra_masyr"], cand["pmdec_masyr"]), (st.pmra, st.pmdec))
        match = {
            "source_id": st.source_id, "separation_arcsec": round(nearest, 2),
            "ra": round(st.ra, 7), "dec": round(st.dec, 7), "g_mag": st.g_mag, "variable": st.variable,
            "pmra_masyr": round(st.pmra, 1) if st.has_pm else None,
            "pmdec_masyr": round(st.pmdec, 1) if st.has_pm else None,
            "pm_total_masyr": round(pm_total, 1) if pm_total is not None else None,
            "pm_agrees": agrees,
        }
    # For "is there a Gaia source that could be this object?" also look at every season's epoch.
    closest = nearest
    for e in epochs.values():
        sm = seps(e)
        if len(sm):
            closest = min(closest, float(sm.min())) if closest is not None else float(sm.min())
    within = closest is not None and closest <= settings.overlay_novelty_arcsec
    cw_match, cw_closest = None, None
    if cw is not None and cw.checked and cw.sources:
        def cw_seps(mjd: float) -> np.ndarray:
            x, y = cw.px_at(mjd)
            return np.hypot(x - cx, y - cy) * pixscale

        c0 = cw_seps(t)
        j = int(np.argmin(c0))
        cw_closest = float(c0[j])
        if cw_closest <= settings.overlay_match_arcsec:
            src = cw.sources[j]
            agrees = None
            if cand.get("kind") == "mover" and cand.get("pmra_masyr") is not None and src.moving:
                agrees = motion_agrees((cand["pmra_masyr"], cand["pmdec_masyr"]), (src.pmra, src.pmdec))
            cw_match = catwise_row(src, separation_arcsec=round(cw_closest, 2), pm_agrees=agrees)
        for e in epochs.values():
            cw_closest = min(cw_closest, float(cw_seps(e).min()))
    cw_within = cw_closest is not None and cw_closest <= settings.overlay_novelty_arcsec
    label, note = label_candidate(cand, match, cw_match)
    # "new" needs both catalogues to have been searched and to have nothing nearby.
    searched = cw is None or cw.checked
    new_obj = (cand.get("kind") == "mover" and cand.get("evidence") in ("strong", "moderate")
               and not within and not cw_within and searched)
    out = {
        "id": cand["id"], "epoch_mjd": round(t, 2), "match": match, "label": label,
        "nearest_gaia_arcsec": round(closest, 2) if closest is not None else None,
        "gaia_within_novelty_radius": bool(within),
        "candidate_new_object": bool(new_obj),
    }
    if cw is not None:
        out.update(catwise_match=cw_match, catwise_checked=bool(cw.checked),
                   nearest_catwise_arcsec=round(cw_closest, 2) if cw_closest is not None else None,
                   catwise_within_novelty_radius=bool(cw_within))
    if note:
        out["label_note"] = note
    return out


def _cached_px(arr: dict, n: int, wcs):
    """px_at(mjd) -> grid pixels of every source in ``arr``, memoised per epoch."""
    cache: dict[float, tuple] = {}

    def px_at(mjd: float):
        k = round(mjd, 3)
        if k not in cache:
            cache[k] = propagate_pixels(arr, wcs, mjd) if n else (np.zeros(0), np.zeros(0))
        return cache[k]

    return px_at


def catwise_payload(grid: Grid, epochs: dict[int, float], cw: CatwiseResult, px_at, matched: set[str]) -> dict:
    """``catwise`` status and ``catwise_sources``: sources in the field that are red or moving
    (> CW_DRAW_MIN_PM_MASYR with S/N > 5), or that matched a candidate; at most overlay_max_sources."""
    if not cw.ok:
        return {"catwise": {"name": cw.name, "ok": False, "note": cw.note}, "catwise_sources": []}
    src = cw.sources
    ref = px_at(SPHEREX_REF_MJD)
    per_season = {s: px_at(m) for s, m in epochs.items()}
    lo, hi = -2.5, grid.n + 1.5
    inside = np.zeros(len(src), bool)
    for x, y in [ref, *per_season.values()]:
        inside |= (x >= lo) & (x <= hi) & (y >= lo) & (y <= hi)
    idx = [int(i) for i in np.flatnonzero(inside)]

    def red(s: CatwiseSource) -> bool:
        return s.w1_w2 is not None and s.w1_w2 > CATWISE_RED_W1W2

    def fast(s: CatwiseSource) -> bool:
        return s.moving and s.pm_total > CW_DRAW_MIN_PM_MASYR

    wanted = [i for i in idx if red(src[i]) or fast(src[i]) or src[i].name in matched]
    wanted.sort(key=lambda i: (not fast(src[i]), src[i].w2 is None, src[i].w2 or 0.0))
    keep = wanted[: settings.overlay_max_sources]
    keep += [i for i in wanted[settings.overlay_max_sources:] if src[i].name in matched]
    rows = [catwise_row(
        src[i], x=round(float(ref[0][i]), 2), y=round(float(ref[1][i]), 2),
        seasons={str(k): [round(float(v[0][i]), 2), round(float(v[1][i]), 2)] for k, v in per_season.items()},
        moving=bool(fast(src[i])), red=bool(red(src[i])),
    ) for i in keep]
    return {
        "catwise": {"name": cw.name, "ok": True, "note": cw.note, "ref_epoch_mjd": CATWISE_PM_EPOCH_MJD,
                    "n_in_field": len(idx), "n_returned": len(rows), "truncated": len(wanted) > len(rows)},
        "catwise_sources": rows,
    }


def build_overlay(grid: Grid, frames: list[dict], seasons: list[dict], candidates: list[dict],
                  cat: CatalogResult, cw: CatwiseResult | None = None) -> dict:
    """The /api/overlay payload for a complete field (pure: no network). ``cw`` None = CatWISE not
    queried (no catwise keys in the answer)."""
    w = grid.wcs()
    epochs = season_epochs(frames, seasons)
    cw_ctx, cw_px = None, None
    if cw is not None:
        cw_arr = catwise_arrays(cw.sources)
        cw_px = _cached_px(cw_arr, len(cw.sources), w)
        cw_ctx = CatwiseContext(cw.sources, cw_px, checked=cw.ok)
    if not cat.ok:
        out = {"status": "complete", "catalog": {"name": cat.name, "ok": False, "note": cat.note},
               "sources": [], "candidates": {}, "notes": NOTES}
        if cw is not None:
            out.update(catwise_payload(grid, epochs, cw, cw_px, set()))
        return out
    stars = cat.stars
    arr = star_arrays(stars)
    px_at = _cached_px(arr, len(stars), w)

    ref = px_at(SPHEREX_REF_MJD)
    per_season = {s: px_at(m) for s, m in epochs.items()}
    n = grid.n
    lo, hi = -2.5, n + 1.5
    inside = np.zeros(len(stars), bool)
    for x, y in [ref, *per_season.values()]:
        inside |= (x >= lo) & (x <= hi) & (y >= lo) & (y <= hi)
    idx = np.flatnonzero(inside)
    cand_out = {c["id"]: evaluate_candidate(c, arr, px_at, stars, epochs, grid.pixscale_arcsec, cw_ctx)
                for c in candidates}
    matched = {c["match"]["source_id"] for c in cand_out.values() if c["match"]}
    order = sorted(idx, key=lambda i: (stars[i].g_mag is None, stars[i].g_mag or 0.0))
    keep = order[: settings.overlay_max_sources]
    kept = set(keep)
    keep += [i for i in order[settings.overlay_max_sources:] if stars[i].source_id in matched and i not in kept]
    rows = []
    for i in keep:
        s = stars[i]
        pm = math.hypot(s.pmra, s.pmdec) if s.has_pm else None
        rows.append({
            "source_id": s.source_id, "ra": round(s.ra, 7), "dec": round(s.dec, 7),
            "pmra_masyr": round(s.pmra, 2) if s.has_pm else None,
            "pmdec_masyr": round(s.pmdec, 2) if s.has_pm else None,
            "pm_total_masyr": round(pm, 2) if pm is not None else None,
            "g_mag": s.g_mag, "variable": s.variable,
            "x": round(float(ref[0][i]), 2), "y": round(float(ref[1][i]), 2),
            "seasons": {str(k): [round(float(v[0][i]), 2), round(float(v[1][i]), 2)]
                        for k, v in per_season.items()},
        })
    out = {
        "status": "complete",
        "catalog": {
            "name": cat.name, "ok": True, "note": cat.note, "ref_epoch_mjd": GAIA_DR3_EPOCH_MJD,
            "n_in_field": int(len(idx)), "n_returned": len(rows), "truncated": len(idx) > len(rows),
        },
        "epochs": {"reference_mjd": SPHEREX_REF_MJD,
                   "seasons": [{"season": k, "mjd": round(v, 3)} for k, v in epochs.items()]},
        "grid": {"n": n, "pixscale_arcsec": grid.pixscale_arcsec, "convention": GRID_NOTE},
        "sources": rows,
        "candidates": cand_out,
        "notes": NOTES,
    }
    if cw is not None:
        names = {c["catwise_match"]["source_name"] for c in cand_out.values() if c.get("catwise_match")}
        out.update(catwise_payload(grid, epochs, cw, cw_px, names))
    return out


# ------------------------------------------------------------------ background query
def search_radius_deg(grid: Grid) -> float:
    half_diag = grid.n * grid.pixscale_arcsec * math.sqrt(2) / 2
    return (half_diag + settings.overlay_margin_arcsec) / 3600


async def default_fetch(grid: Grid) -> CatalogResult:
    return await asyncio.to_thread(gaia_cone, grid.ra, grid.dec, search_radius_deg(grid))


async def default_fetch_catwise(grid: Grid) -> CatwiseResult:
    return await asyncio.to_thread(catwise_cone, grid.ra, grid.dec, search_radius_deg(grid))


@dataclass
class Entry:
    task: asyncio.Task | None = None
    result: CatalogResult | None = None
    expires: float | None = None       # a failed query is retried after this
    wise_task: asyncio.Task | None = None
    wise_result: WiseResult | None = None
    wise_expires: float | None = None
    cw_task: asyncio.Task | None = None
    cw_result: CatwiseResult | None = None
    cw_expires: float | None = None
    payload: dict | None = None
    payload_key: tuple | None = None


_UNSET = object()


def _retry_due(res, expires: float | None) -> bool:
    return res is not None and not res.ok and bool(expires) and time.time() > expires


class OverlayManager:
    """Gaia (``fetch``) and CatWISE2020 (``fetch_catwise``) queries run concurrently per field. With a
    custom ``fetch`` and no ``fetch_catwise``, CatWISE is not queried (no network in tests)."""

    def __init__(self, fetch: Callable[[Grid], Awaitable[CatalogResult]] = default_fetch,
                 fetch_catwise=_UNSET, fetch_wise=_UNSET) -> None:
        self.fetch = fetch
        self.fetch_wise = (default_wise_fetch if fetch is default_fetch else None) if fetch_wise is _UNSET else fetch_wise
        self.fetch_catwise: Callable[[Grid], Awaitable[CatwiseResult]] | None = (
            (default_fetch_catwise if fetch is default_fetch else None) if fetch_catwise is _UNSET else fetch_catwise
        )
        self.entries: dict[str, Entry] = {}
        self._sem = asyncio.Semaphore(settings.overlay_gaia_concurrency)

    async def close(self) -> None:
        tasks = [t for e in self.entries.values() for t in (e.task, e.cw_task, e.wise_task) if t and not t.done()]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _run(self, entry: Entry, grid: Grid) -> None:
        try:
            async with self._sem:
                res = await self.fetch(grid)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — never raise out of a background query
            log.warning("overlay catalogue query failed: %s", exc)
            res = CatalogResult(ok=False, note=f"Gaia DR3 query failed ({type(exc).__name__}).")
        entry.result = res
        entry.expires = None if res.ok else time.time() + settings.overlay_retry_after_s

    async def _run_catwise(self, entry: Entry, grid: Grid) -> None:
        try:
            async with self._sem:
                res = await self.fetch_catwise(grid)  # type: ignore[misc]
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("overlay CatWISE query failed: %s", exc)
            res = CatwiseResult(ok=False, note=f"CatWISE2020 query failed ({type(exc).__name__}).")
        entry.cw_result = res
        entry.cw_expires = None if res.ok else time.time() + settings.overlay_retry_after_s

    async def _run_wise(self, entry: Entry, grid: Grid) -> None:
        try:
            async with self._sem:
                res = await self.fetch_wise(grid)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("AllWISE query failed: %s", exc)
            res = WiseResult(ok=False, note=f"AllWISE query failed ({type(exc).__name__}).")
        entry.wise_result = res
        entry.wise_expires = None if res.ok else time.time() + settings.overlay_retry_after_s

    def request(self, key: str, grid: Grid, frames: list[dict], seasons: list[dict],
                candidates: list[dict] | None, field_ready: bool, version: str = "") -> dict:
        if grid.fov_arcmin > settings.overlay_max_fov_arcmin:
            note = (f"Catalogue overlay is limited to fields up to {settings.overlay_max_fov_arcmin:.0f} "
                    "arcmin across.")
            return {"status": "complete", "catalog": {"name": CATALOG_NAME, "ok": False, "note": note},
                    "sources": [], "candidates": {}, "notes": NOTES,
                    "wise": {"status": "complete", "catalog": {"name": WISE_CATALOG_NAME, "ok": False, "note": note,
                             "ref_epoch_mjd": WISE_EPOCH_MJD}, "sources": [], "candidates": {}}}
        e = self.entries.get(key)
        if e is None:
            e = self.entries[key] = Entry()
        if e.task is None or _retry_due(e.result, e.expires):
            e.result, e.expires = None, None
            e.task = asyncio.create_task(self._run(e, grid))
        if self.fetch_catwise is not None and (e.cw_task is None or _retry_due(e.cw_result, e.cw_expires)):
            e.cw_result, e.cw_expires = None, None
            e.cw_task = asyncio.create_task(self._run_catwise(e, grid))
        if self.fetch_wise is not None and (e.wise_task is None or _retry_due(e.wise_result, e.wise_expires)):
            e.wise_result, e.wise_expires = None, None
            e.wise_task = asyncio.create_task(self._run_wise(e, grid))
        if self.fetch_wise is not None and e.wise_result is None:
            return {"status": "processing"}
        if e.result is None or not field_ready or (self.fetch_catwise is not None and e.cw_result is None):
            return {"status": "processing"}
        pk = (version, len(frames), id(e.result), id(e.cw_result), id(e.wise_result))
        if e.payload is None or e.payload_key != pk:
            e.payload = build_overlay(grid, frames, seasons, candidates or [], e.result, e.cw_result)
            if e.wise_result is not None:
                e.payload["wise"] = build_wise_overlay(grid, frames, seasons, candidates or [], e.wise_result)
                for cid, match in e.payload["wise"]["candidates"].items():
                    if match["within_novelty_radius"] and cid in e.payload["candidates"]:
                        e.payload["candidates"][cid]["candidate_new_object"] = False
                for candidate in e.payload["candidates"].values():
                    candidate["allwise_checked"] = e.wise_result.ok
                    if not e.wise_result.ok:
                        candidate["candidate_new_object"] = False
                e.payload["notes"] = [*e.payload["notes"],
                    "AllWISE motion-fit positions use MJD 55400.0; stationary fits use their mean observation epoch. "
                    "Unknown or unreliable motions are not propagated. A missing catalogue match is not proof of novelty."]
            e.payload_key = pk
        return {**e.payload, "key": key}


# ------------------------------------------------------------------ route
@router.get("/api/overlay")
async def overlay(request: Request, key: str):
    mgr = request.app.state.fields
    job = mgr.jobs.get(key)
    if job is None:
        raise HTTPException(404, "unknown field (request /api/frames first)")
    job.touch(client_ip(request))
    if job.status == "error":
        return {"status": "error", "error": job.error or "the field failed to build"}
    if getattr(request.app.state, "overlays", None) is None:
        request.app.state.overlays = OverlayManager()
    ready = job.status == "complete" and "target" in (job.analysis or {})
    return request.app.state.overlays.request(
        key, job.grid, list(job.frames.values()), job.seasons,
        (job.analysis or {}).get("candidates"), ready, version=str(job.stack_version),
    )

WISE_VAR_FLAG_VARIABLE = 6      # AllWISE var_flg char flag; >= this is a high-probability variable


def _wise_variable(src) -> bool:
    """True when any band of the AllWISE variability flag reaches the high-probability threshold."""
    fl = src.var_flg or ""
    return any(c.isdigit() and int(c) >= WISE_VAR_FLAG_VARIABLE for c in fl if c.isdigit())



def wise_arrays(sources: list) -> dict[str, np.ndarray]:
    """Array view of a source list with the keys `wise_positions` expects (plus WISE mags)."""
    f = np.float64
    ra_l: list[float] = []
    dec_l: list[float] = []
    pmra_l: list[float] = []
    pmdec_l: list[float] = []
    epoch_l: list[float] = []
    var_l: list[bool] = []
    desig_l: list[str] = []
    w1_l: list[float] = []
    w2_l: list[float] = []
    w3_l: list[float] = []
    w4_l: list[float] = []
    for s in sources:
        ra_l.append(float(s.ra))
        dec_l.append(float(s.dec))
        if isinstance(s, Star):
            pmra_l.append(float(s.pmra or 0.0))
            pmdec_l.append(float(s.pmdec or 0.0))
            var_l.append(bool(s.variable))
        else:
            ok = (s.sigpmra is not None and 0 < float(s.sigpmra) <= WISE_PM_SIGMA_MAX) and \
                 (s.sigpmdec is not None and 0 < float(s.sigpmdec) <= WISE_PM_SIGMA_MAX)
            pmra_l.append(float(s.pmra) if (s.pmra is not None and ok) else 0.0)
            pmdec_l.append(float(s.pmdec) if (s.pmdec is not None and ok) else 0.0)
            var_l.append(_wise_variable(s))
        epoch_l.append(float(getattr(s, "epoch_mjd", None) or WISE_EPOCH_MJD))
        desig_l.append(str(getattr(s, "designation", getattr(s, "source_id", "")) or ""))
        w1_l.append(float(s.w1) if s.w1 is not None else 0.0)
        w2_l.append(float(s.w2) if s.w2 is not None else 0.0)
        w3_l.append(float(s.w3) if s.w3 is not None else 0.0)
        w4_l.append(float(s.w4) if s.w4 is not None else 0.0)
    return {
        "ra": np.array(ra_l, f), "dec": np.array(dec_l, f),
        "pmra": np.array(pmra_l, f), "pmdec": np.array(pmdec_l, f),
        "epoch": np.array(epoch_l, f),
        "designation": np.array(desig_l, object),
        "w1": np.array(w1_l, f), "w2": np.array(w2_l, f), "w3": np.array(w3_l, f), "w4": np.array(w4_l, f),
        "variable": np.array(var_l, bool),
    }


def wise_positions(arr: dict[str, np.ndarray], wcs, mjd: float, pmra: float | None = None, pmdec: float | None = None) -> tuple[np.ndarray, np.ndarray]:
    """Grid pixel coordinates (0-based, +x west, +y north) of every WISE source at ``mjd``.
    ``pmra``/``pmdec`` override the catalogue motion (used for "propagate a mover's measured motion
    back to the catalogue epoch", e.g. to test whether a fast mover is the WISE source itself)."""
    dt = (mjd - arr["epoch"]) / 365.25
    p = arr["pmra"] if pmra is None else np.full_like(arr["pmra"], float(pmra))
    q = arr["pmdec"] if pmdec is None else np.full_like(arr["pmdec"], float(pmdec))
    ra = arr["ra"] + p * dt / 3.6e6 / np.cos(np.radians(arr["dec"]))
    dec = arr["dec"] + q * dt / 3.6e6
    x, y = wcs.world_to_pixel_values(ra % 360.0, dec)
    return np.asarray(x, float), np.asarray(y, float)



def build_wise_overlay(grid: Grid, frames: list[dict], seasons: list[dict], candidates: list[dict],
                      wise: WiseResult, gaia_sources: list[Star] | None = None) -> dict:
    """AllWISE sources drawn on the field grid (and their cross-match with the movers).
    Pure: no network. Positions: propagated from each source's reference epoch; a source with a
    reliable PM fit is moved, everything else is stationary. A mover candidate is additionally
    matched by propagating it back to the AllWISE epoch with the candidate's own measured motion.
    """
    empty = {"status": "complete", "catalog": {"name": WISE_CATALOG_NAME, "ok": wise.ok, "note": wise.note,
                                               "ref_epoch_mjd": WISE_EPOCH_MJD},
             "sources": [], "candidates": {}}
    if not wise.ok:
        return empty
    sources = wise.sources
    arr = wise_arrays(sources)
    w = grid.wcs()
    epochs = season_epochs(frames, seasons)
    ref = wise_positions(arr, w, SPHEREX_REF_MJD)
    per_season = {s: wise_positions(arr, w, m) for s, m in epochs.items()}
    n = grid.n
    lo, hi = -2.5, n + 1.5
    inside = np.zeros(len(sources), bool)
    for x, y in [ref, *per_season.values()]:
        inside |= (x >= lo) & (x <= hi) & (y >= lo) & (y <= hi)
    idx = np.flatnonzero(inside)
    cand_out = {}
    for c in candidates or []:
        t = candidate_epoch(c, epochs)
        cx, cy = float(c["x"]), float(c["y"])
        pmra = float(c["pmra_masyr"]) if c.get("pmra_masyr") is not None else None
        pmdec = float(c["pmdec_masyr"]) if c.get("pmdec_masyr") is not None else None
        x, y = wise_positions(arr, w, t, pmra=pmra, pmdec=pmdec)
        sep = np.hypot(x - cx, y - cy) * grid.pixscale_arcsec
        order = np.argsort(sep, kind="stable")
        match_i = None
        match_sep = None
        for i in order:
            if np.isfinite(sep[i]) and (match_i is None or sep[i] < match_sep):
                match_i, match_sep = int(i), float(sep[i])
        entry = {"id": c["id"], "designation": None, "separation_arcsec": None, "w1": None, "w2": None,
                 "variable": False, "pm_source": None, "pm_agrees": None, "within_novelty_radius": False}
        within = False
        if match_i is not None and np.isfinite(match_sep) and float(match_sep) <= settings.overlay_novelty_arcsec:
            within = True
            entry["within_novelty_radius"] = True
        if match_i is not None and np.isfinite(match_sep) and match_sep <= settings.overlay_match_arcsec:
            s = sources[match_i]
            entry["designation"] = s.designation
            entry["separation_arcsec"] = round(float(match_sep), 2)
            entry["w1"] = s.w1
            entry["w2"] = s.w2
            entry["variable"] = bool(_wise_variable(s))
            entry["pm_source"] = "candidate" if pmra is not None else "self"
        cand_out[c["id"]] = entry
    order = sorted(idx, key=lambda i: (arr["w1"][i] is None or arr["w1"][i] == 0.0, arr["w1"][i]))
    keep = order[: settings.overlay_wise_max_sources]
    rows = []
    for i in keep:
        s = sources[int(i)]
        rows.append({
            "source_id": str(s.designation), "designation": str(s.designation),
            "ra": round(float(s.ra), 7), "dec": round(float(s.dec), 7),
            "pmra_masyr": round(float(arr["pmra"][i]), 2),
            "pmdec_masyr": round(float(arr["pmdec"][i]), 2),
            "pm_total_masyr": round(float(np.hypot(arr["pmra"][i], arr["pmdec"][i])), 2),
            "w1": s.w1, "w2": s.w2, "w3": s.w3, "w4": s.w4,
            "variable": bool(_wise_variable(s)),
            "x": round(float(ref[0][int(i)]), 2), "y": round(float(ref[1][int(i)]), 2),
            "seasons": {str(k): [round(float(v[0][int(i)]), 2), round(float(v[1][int(i)]), 2)]
                        for k, v in per_season.items()},
        })
    return {
        "status": "complete",
        "catalog": {
            "name": wise.name, "ok": True, "note": wise.note, "ref_epoch_mjd": WISE_EPOCH_MJD,
            "n_in_field": int(len(idx)), "n_returned": len(rows),
            "truncated": bool(len(idx) > settings.overlay_wise_max_sources),
        },
        "sources": rows,
        "candidates": cand_out,
    }



async def default_wise_fetch(grid: Grid) -> WiseResult:
    return await asyncio.to_thread(wise_cone, grid.ra, grid.dec, search_radius_deg(grid),
                                  margin_arcsec=settings.overlay_wise_margin_arcsec)
