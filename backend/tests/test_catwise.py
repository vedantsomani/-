"""CatWISE2020 in the catalogue overlay, with fakes only (no network): unit conversion, the
label rule, novelty, the manager and failure fallbacks."""

from __future__ import annotations

import asyncio
import math
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import catalogs, overlay
from app.catalogs import CATWISE_PM_EPOCH_MJD, CatalogResult, CatwiseResult, CatwiseSource
from app.config import settings
from tests.test_hardening import C, FRAMES, GRID, MID, SEASONS, cand, star_at_centre

MJD_YEARS = (MID - CATWISE_PM_EPOCH_MJD) / 365.25


def cw_at_centre(name, pmra, pmdec, *, dra=0.0, ddec=0.0, w1=17.0, w2=14.0, snr=40.0):
    """A CatWISE source (dra, ddec) arcsec off the grid centre at MJD 61000 (motions in mas/yr)."""
    ra = GRID.ra + (dra / 3600 - pmra * MJD_YEARS / 3.6e6) / math.cos(math.radians(GRID.dec))
    dec = GRID.dec + ddec / 3600 - pmdec * MJD_YEARS / 3.6e6
    return CatwiseSource(name, ra, dec, CATWISE_PM_EPOCH_MJD, pmra, pmdec, snr, w1, w2)


def run(stars, cands, cw_sources=None, cw_ok=True):
    cat = CatalogResult(ok=True, stars=stars)
    cw = None if cw_sources is None else CatwiseResult(ok=cw_ok, sources=cw_sources,
                                                         note=None if cw_ok else "CatWISE2020 query failed (Timeout)")
    return overlay.build_overlay(GRID, FRAMES, SEASONS, cands, cat, cw)


# ------------------------------------------------------------------ units and columns
ROW = {"source_name": "J085506.91-071437.5", "ra": 133.7788255, "dec": -7.2437546, "ra_pm": 133.7835555,
       "dec_pm": -7.2440520, "pmra": -7.9199, "pmdec": 0.4939, "sigpmra": 0.1520, "sigpmdec": 0.2353,
       "w1mpro": 19.456, "w2mpro": 15.541, "meanobsmjd": 57853.135}


def test_row_conversion_arcsec_to_mas_and_snr():
    s = catalogs.catwise_source_from_row(ROW)
    assert s.pmra == pytest.approx(-7919.9) and s.pmdec == pytest.approx(493.9)          # arcsec/yr -> mas/yr
    assert s.pm_total == pytest.approx(math.hypot(7919.9, 493.9))
    assert (s.ra, s.dec, s.epoch_mjd) == (133.7835555, -7.2440520, CATWISE_PM_EPOCH_MJD)  # motion-fit position
    assert s.w1_w2 == pytest.approx(3.915) and s.moving
    # S/N = |mu| over the uncertainty projected on the motion: ~52 for this source
    assert 40 < s.pm_snr < 60


def test_row_without_motion_fit_falls_back_to_mean_epoch_and_masked_values_are_none():
    r = {**ROW, "ra_pm": None, "dec_pm": None, "sigpmra": None, "w1mpro": float("nan")}
    s = catalogs.catwise_source_from_row(r)
    assert (s.ra, s.dec, s.epoch_mjd) == (133.7788255, -7.2437546, pytest.approx(57853.135))
    assert s.pm_snr is None and not s.moving and s.w1 is None and s.w1_w2 is None
    assert catalogs.catwise_source_from_row({**ROW, "ra_pm": None, "ra": None}) is None


def test_positions_use_motion_only_when_significant():
    fast = cw_at_centre("fast", 8000.0, 500.0)
    noisy = cw_at_centre("noisy", 8000.0, 500.0, snr=3.0)           # same catalogue motion, S/N 3: ignored
    out = run([], [], [fast, noisy])
    rows = {r["source_name"]: r for r in out["catwise_sources"]}
    assert rows["fast"]["x"] == pytest.approx(C, abs=0.05) and rows["fast"]["y"] == pytest.approx(C, abs=0.05)
    # the unreliable motion is not applied, so the source stays where the catalogue put it (86" away)
    assert math.hypot(rows["noisy"]["x"] - C, rows["noisy"]["y"] - C) * 6.15 > 50
    # season 0 is 100 days earlier: the mover is 8"/yr * 0.274 yr east (+x) of where it is at MJD 61000
    assert rows["fast"]["seasons"]["0"][0] - rows["fast"]["x"] == pytest.approx(8.0 * 100 / 365.25 / 6.15, abs=0.1)
    assert rows["fast"]["moving"] and rows["fast"]["red"] and rows["fast"]["w1_w2"] == pytest.approx(3.0)


def test_only_red_moving_or_matched_sources_are_returned():
    plain = cw_at_centre("plain", 0.0, 0.0, dra=20.0, w1=15.0, w2=14.8, snr=0.5)
    red = cw_at_centre("red", 0.0, 0.0, dra=-20.0, w1=18.0, w2=16.9, snr=0.5)
    slow = cw_at_centre("slow", 60.0, 0.0, dra=0.0, ddec=30.0, w1=15.0, w2=14.9)   # moving, but < 100 mas/yr
    out = run([], [], [plain, red, slow])
    assert [r["source_name"] for r in out["catwise_sources"]] == ["red"]
    assert out["catwise"]["ok"] and out["catwise"]["n_in_field"] == 3 and out["catwise"]["n_returned"] == 1
    # a matched source is returned even when plain
    out = run([], [cand(kind="static", pm=None, x=C - 20 / 6.15, y=C)], [plain])
    assert [r["source_name"] for r in out["catwise_sources"]] == ["plain"]


def test_no_catwise_keys_when_not_queried():
    out = run([], [cand()])
    assert "catwise" not in out and "catwise_sources" not in out
    assert "catwise_match" not in out["candidates"]["c0"]


# ------------------------------------------------------------------ label rule and novelty
TARGET = cand(is_target=True, pm=(-8120, 670))      # a mover moving like WISE 0855


def test_known_mover_in_catwise_not_in_gaia():
    out = run([], [TARGET], [cw_at_centre("J0855", -7920.0, 494.0, dra=1.0)])
    c = out["candidates"]["c0"]
    assert c["label"] == overlay.L_CW_MOVER == "known mover in CatWISE (not in Gaia)"
    assert c["match"] is None and c["catwise_match"]["source_name"] == "J0855"
    assert c["catwise_match"]["pm_agrees"] is True and c["catwise_match"]["separation_arcsec"] == pytest.approx(1.0, abs=0.2)
    assert c["catwise_match"]["w1_w2"] == pytest.approx(3.0)
    assert c["candidate_new_object"] is False and c["catwise_within_novelty_radius"] is True


def test_catwise_label_needs_agreeing_motion_and_significant_snr():
    for src in (
        cw_at_centre("opposite", 7920.0, -494.0),             # right speed, opposite direction
        cw_at_centre("slow", -2000.0, 120.0),                 # same direction, 4x too slow
        cw_at_centre("noisy", -7920.0, 494.0, snr=3.0),       # motion not significant: not used at all
    ):
        c = run([], [TARGET], [src])["candidates"]["c0"]
        assert c["label"] == overlay.L_NONE, src.name
        assert c["candidate_new_object"] is (src.name == "noisy"), src.name   # the others sit within 10"
    # noisy: stationary at its catalogue position, ~86" from the candidate -> no match at all
    assert run([], [TARGET], [cw_at_centre("noisy", -7920.0, 494.0, snr=3.0)])["candidates"]["c0"]["catwise_match"] is None
    # a mismatching nearby source says why in the note
    c = run([], [TARGET], [cw_at_centre("opposite", 7920.0, -494.0)])["candidates"]["c0"]
    assert c["catwise_match"]["pm_agrees"] is False and "does not match" in c["label_note"]


def test_gaia_label_wins_when_gaia_matches_and_non_movers_never_get_the_catwise_label():
    gaia = star_at_centre("g1", 0, 0, g=12.0)
    cw = cw_at_centre("J0855", -7920.0, 494.0)
    assert run([gaia], [TARGET], [cw])["candidates"]["c0"]["label"] == overlay.L_CAT
    static = cand(kind="variable", pm=None)
    c = run([], [static], [cw])["candidates"]["c0"]
    assert c["label"] == overlay.L_NONE and c["catwise_match"] is not None


def test_new_object_needs_both_catalogues_empty_within_10_arcsec():
    far = cw_at_centre("far", 0.0, 0.0, dra=60.0)
    assert run([], [cand()], [far])["candidates"]["c0"]["candidate_new_object"] is True
    near = cw_at_centre("near", 0.0, 0.0, dra=8.0, snr=1.0)            # inside 10", outside the 6" match
    c = run([], [cand()], [near])["candidates"]["c0"]
    assert c["candidate_new_object"] is False and c["catwise_match"] is None and c["nearest_catwise_arcsec"] < 10
    # Gaia alone also still vetoes
    assert run([star_at_centre("g", 0, 0, dra=8.0)], [cand()], [far])["candidates"]["c0"]["candidate_new_object"] is False


def test_catwise_not_queried_keeps_gaia_only_novelty():
    assert run([], [cand()])["candidates"]["c0"]["candidate_new_object"] is True


def test_notes_name_both_catalogues_and_keep_the_not_proof_caveat():
    note = overlay.NOTES[0]
    assert note.startswith("Absence from Gaia") and "CatWISE2020" in note and "not proof of novelty" in note


# ------------------------------------------------------------------ failure fallbacks
def test_failed_catwise_leaves_gaia_overlay_and_blocks_new_object_claim():
    out = run([star_at_centre("1", 0, 0, g=10.0)], [cand(x=C + 30, y=C), cand("c1")], [], cw_ok=False)
    assert out["catalog"]["ok"] and out["sources"][0]["source_id"] == "1"
    assert out["catwise"] == {"name": "CatWISE2020", "ok": False, "note": "CatWISE2020 query failed (Timeout)"}
    assert out["catwise_sources"] == []
    c = out["candidates"]["c0"]
    assert c["catwise_checked"] is False and c["catwise_match"] is None and c["candidate_new_object"] is False


def test_failed_gaia_still_reports_catwise():
    cat = CatalogResult(ok=False, note="Gaia DR3 query failed (Timeout)")
    cw = CatwiseResult(ok=True, sources=[cw_at_centre("J0855", -7920.0, 494.0)])
    out = overlay.build_overlay(GRID, FRAMES, SEASONS, [cand()], cat, cw)
    assert out["catalog"]["ok"] is False and out["candidates"] == {} and out["sources"] == []
    assert out["catwise"]["ok"] and [r["source_name"] for r in out["catwise_sources"]] == ["J0855"]


def test_catwise_cone_never_raises_and_caches(tmp_path, monkeypatch):
    import pyvo

    monkeypatch.setattr(settings, "cache_dir", tmp_path)
    calls = []

    class Boom:
        def __init__(self, *a, **k):
            calls.append(1)
            raise ConnectionError("down")

    monkeypatch.setattr(pyvo.dal, "TAPService", Boom)
    res = catalogs.catwise_cone(10.0, 10.0, 0.05)
    assert res.ok is False and "CatWISE2020 query failed (ConnectionError)" in res.note and not list(tmp_path.rglob("*.json"))

    from astropy.table import Table

    class Fake:
        def __init__(self, *a, **k):
            calls.append(1)

        def run_sync(self, q):
            assert "catwise_2020" in q and "CIRCLE('ICRS', 10.0000000, 10.0000000" in q
            t = Table(rows=[ROW], names=list(ROW))

            return SimpleNamespace(to_table=lambda: t)

    monkeypatch.setattr(pyvo.dal, "TAPService", Fake)
    res = catalogs.catwise_cone(10.0, 10.0, 0.05)
    assert res.ok and len(res.sources) == 1 and res.sources[0].pmra == pytest.approx(-7919.9)
    n = len(calls)
    monkeypatch.setattr(pyvo.dal, "TAPService", Boom)
    again = catalogs.catwise_cone(10.0, 10.0, 0.05)        # served from the disk cache
    assert again.ok and again.sources == res.sources and len(calls) == n


# ------------------------------------------------------------------ manager and route
class _FakeJob(SimpleNamespace):
    def touch(self, client=None):
        pass


def _app(mgr, cands):
    job = _FakeJob(grid=GRID, frames=dict(enumerate(FRAMES)), seasons=SEASONS, status="complete", error=None,
                   stack_version="1", analysis={"target": {}, "candidates": cands})
    app = FastAPI()
    app.include_router(overlay.router)
    app.state.fields = SimpleNamespace(jobs={"k": job})
    app.state.overlays = mgr
    return app


def test_manager_waits_for_both_queries_then_answers_and_retries_a_failed_catwise(monkeypatch):
    monkeypatch.setattr(settings, "overlay_retry_after_s", 0.5)
    n = {"gaia": 0, "cw": 0}

    async def gaia(grid):
        n["gaia"] += 1
        return CatalogResult(ok=True, stars=[])

    async def cw(grid):
        n["cw"] += 1
        await asyncio.sleep(0.3)
        if n["cw"] == 1:
            raise RuntimeError("boom")                       # an exception becomes a failed result
        return CatwiseResult(ok=True, sources=[cw_at_centre("J0855", -7920.0, 494.0)])

    with TestClient(_app(None, [TARGET])) as c:
        c.app.state.overlays = overlay.OverlayManager(fetch=gaia, fetch_catwise=cw)
        assert c.get("/api/overlay?key=k").json() == {"status": "processing"}
        time.sleep(0.15)
        assert c.get("/api/overlay?key=k").json() == {"status": "processing"}      # Gaia is in, CatWISE is not
        time.sleep(0.4)
        body = c.get("/api/overlay?key=k").json()
        assert body["status"] == "complete" and body["catwise"]["ok"] is False and body["catalog"]["ok"]
        assert body["catwise"]["note"] == "CatWISE2020 query failed (RuntimeError)."
        time.sleep(0.5)
        c.get("/api/overlay?key=k")                         # retry window passed: only CatWISE is queried again
        time.sleep(0.45)
        body = c.get("/api/overlay?key=k").json()
        assert n == {"gaia": 1, "cw": 2}
        assert body["catwise"]["ok"] and body["candidates"]["c0"]["label"] == overlay.L_CW_MOVER


def test_default_manager_queries_catwise_but_a_custom_fetch_alone_does_not():
    assert overlay.OverlayManager().fetch_catwise is overlay.default_fetch_catwise
    assert overlay.OverlayManager(fetch=lambda g: None).fetch_catwise is None
