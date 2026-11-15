"""Batch triage: parsing, cap, sequential progress, persistence, CSV (fake managers, no network)."""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import batch
from app.config import settings
from app.coords import CoordError, Resolved
from app.fields import field_key

EXAMPLES = (
    "133.76002 -7.24221 -8123.7 673.2 # WISE 0855\n"
    "269.44629 4.76788 -801.6 10362.4 # Barnard's Star\n"
    "76.95653 30.40144 # RW Aur\n"
)


# ---- parsing --------------------------------------------------------------------------------

def test_parse_line_formats():
    rows = batch.parse_targets(
        "# comment\n\n"
        "133.76002 -7.24221 -8123.7 673.2 # WISE 0855\n"
        "08:55:10.8 -07:14:42.5\n"
        "08 55 10.8 -07 14 42.5 100 -20 spaced\n"
        "08h55m10.8s, -07d14m42s, 5, 6, comma label\n"
        "Proxima Cen # nearest\n"
        "47 UMa\n"
    )
    assert [r["i"] for r in rows] == [1, 2, 3, 4, 5, 6]
    assert all(r["error"] is None for r in rows)
    r = rows[0]
    assert (r["ra"], r["dec"], r["pmra"], r["pmdec"], r["label"]) == (133.76002, -7.24221, -8123.7, 673.2, "WISE 0855")
    assert rows[1]["ra"] == pytest.approx(133.795) and rows[1]["pmra"] is None
    assert rows[2]["dec"] == pytest.approx(-(7 + 14 / 60 + 42.5 / 3600))
    assert rows[2]["pmdec"] == -20 and rows[2]["label"] == "spaced"
    assert rows[3]["ra"] == pytest.approx(133.795) and rows[3]["pmra"] == 5 and rows[3]["label"] == "comma label"
    assert rows[4]["name"] == "Proxima Cen" and rows[4]["ra"] is None and rows[4]["label"] == "nearest"
    assert rows[5]["name"] == "47 UMa"


def test_parse_bad_lines_are_rows_with_errors():
    rows = batch.parse_targets("200 95\n133.76 -7.2 5\n133.76 foo\nvega\n")
    assert [bool(r["error"]) for r in rows] == [True, True, True, False]
    assert "Dec out of range" in rows[0]["error"]
    assert "proper motion" in rows[1]["error"]
    assert "Dec" in rows[2]["error"]


def test_parse_csv_with_header():
    rows = batch.parse_targets("ra,dec,pmra,pmdec,name\n133.7,-7.2,-8123,673,WISE 0855\n,,,,Vega\n10,xx,,,bad\n")
    assert len(rows) == 3
    assert (rows[0]["ra"], rows[0]["pmra"], rows[0]["label"]) == (133.7, -8123.0, "WISE 0855")
    assert rows[1]["name"] == "Vega" and rows[1]["ra"] is None
    assert rows[2]["error"]


def test_band_flux_weighted_mean():
    spec = [{"wavelength_um": 4.4, "flux_ujy": 10.0, "flux_err_ujy": 1.0},
            {"wavelength_um": 4.5, "flux_ujy": 20.0, "flux_err_ujy": 2.0},
            {"wavelength_um": 3.0, "flux_ujy": 999.0, "flux_err_ujy": 1.0},
            {"wavelength_um": 4.6, "flux_ujy": None, "flux_err_ujy": None}]
    f, e, snr = batch.band_flux(spec)
    assert f == pytest.approx((10 / 1 + 20 / 4) / (1 + 0.25)) and e == pytest.approx(1.25 ** -0.5)
    assert snr == pytest.approx(f / e)
    assert batch.band_flux([]) == (None, None, None)


# ---- fake managers --------------------------------------------------------------------------

class FJob:
    def __init__(self, key: str, delay: float, frames: int = 4):
        self.key, self.born, self.delay, self.error, self.total = key, time.monotonic(), delay, None, frames
        self.frames = {f"f{k}": {} for k in range(frames)}
        self.seasons = [{"season": 1}, {"season": 2}]
        self.stacks = {f"s{s}_d{d}": {"id": f"s{s}_d{d}", "season": s, "detector": d, "bin": None}
                       for s in (1, 2) for d in (3, 6)}
        self.analysis = {"target": {"summary": "mover", "evidence": "strong", "pmra_masyr": -8000, "pmdec_masyr": 700}}

    @property
    def status(self) -> str:
        return "complete" if time.monotonic() - self.born >= self.delay else "processing"

    def touch(self, client=None):
        pass


class FakeFields:
    client = None

    def __init__(self, delay: float = 0.05):
        self.jobs: dict[str, FJob] = {}
        self.delay, self.started, self._admitted = delay, [], 0

    def get_or_start(self, grid, client=None, **kw):
        key = field_key(grid)
        if key not in self.jobs:
            self.jobs[key] = FJob(key, self.delay)
            self.started.append(key)
        return self.jobs[key]

    def building(self) -> int:
        return sum(j.status != "complete" for j in self.jobs.values())


class FakeFingerprints:
    def __init__(self, fields: FakeFields):
        self.fields, self.calls, self.max_busy, self.seen = fields, [], 0, set()

    async def request(self, fields, grid, target):
        self.max_busy = max(self.max_busy, fields.building())
        key = (field_key(grid), target.key())
        if key not in self.seen:
            self.seen.add(key)
            self.calls.append(target.ra)
            return {"status": "processing", "stage": "photometry", "done": 0, "total": 4}
        return {"status": "complete", "blend_warning": target.ra < 100,
                "spectrum": [{"wavelength_um": 4.5, "flux_ujy": 30.0, "flux_err_ujy": 3.0}],
                "classification": {"label": "T dwarf", "confidence": 0.8,
                                   "indices": {"CH4 index": {"value": 0.4, "err": 0.1, "sigma": 4.0}}}}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "cache_dir", tmp_path)
    monkeypatch.setattr(batch, "POLL_S", 0.01)
    fields = FakeFields()
    app = FastAPI()
    app.include_router(batch.router)
    app.state.fields, app.state.fingerprints = fields, FakeFingerprints(fields)
    return SimpleNamespace(app=app, fields=fields, fps=app.state.fingerprints, tmp=tmp_path)


def wait_done(client, bid, timeout=15.0):
    end, d = time.time() + timeout, {}
    while time.time() < end:
        d = client.get(f"/api/batch/{bid}").json()
        if d["status"] == "complete":
            return d
        time.sleep(0.02)
    raise AssertionError(f"batch not finished: {d}")


# ---- API ------------------------------------------------------------------------------------

def test_cap_rejects_big_lists(env, monkeypatch):
    monkeypatch.setenv("SPHEREX_BATCH_MAX", "2")
    with TestClient(env.app) as c:
        r = c.post("/api/batch", json={"targets": EXAMPLES})
        assert r.status_code == 422 and "limit is 2" in r.json()["detail"] and "3 rows" in r.json()["detail"]
        assert c.post("/api/batch", json={"targets": "# nothing\n\n"}).status_code == 422
        assert c.post("/api/batch", json={"targets": "1 2", "fov": 999}).status_code == 422


def test_sequential_progress_and_results(env):
    with TestClient(env.app) as c:
        r = c.post("/api/batch", json={"targets": EXAMPLES + "200 95\n", "fov": 5})
        assert r.status_code == 200 and r.json()["n"] == 4
        d = wait_done(c, r.json()["id"])
    assert (d["done"], d["total"], d["status"]) == (4, 4, "complete")
    rows = {r["i"]: r for r in d["rows"]}
    assert env.fps.calls == [133.76002, 269.44629, 76.95653]            # strictly in order
    assert env.fps.max_busy <= 2 and len(env.fields.started) == 3
    w = rows[1]
    assert w["status"] == "complete" and w["label"] == "WISE 0855"
    assert w["verdict"] == "mover" and w["evidence"] == "strong" and w["measured_pm_masyr"] == [-8000, 700]
    assert w["flux_4p5_ujy"] == pytest.approx(30.0) and w["snr_4p5"] == pytest.approx(10.0)
    assert (w["ch4_index"], w["ch4_sigma"], w["class_label"], w["class_confidence"]) == (0.4, 4.0, "T dwarf", 0.8)
    assert w["blend_warning"] is False and rows[3]["blend_warning"] is True
    assert w["n_exposures"] == 4 and w["n_seasons"] == 2
    assert w["thumb_url"] == f"/api/fields/{w['field_key']}/stacks/s2_d6.png"
    assert "ra=133.760020" in w["view_url"] and "pmepoch=61000" in w["view_url"] and "name=WISE+0855" in w["view_url"]
    assert "pmra" not in rows[3]["view_url"]
    assert rows[4]["status"] == "error" and "Dec out of range" in rows[4]["error"]


def test_persistence_and_downloads(env):
    with TestClient(env.app) as c:
        bid = c.post("/api/batch", json={"targets": EXAMPLES}).json()["id"]
        d = wait_done(c, bid)
        assert (env.tmp / "batches" / f"{bid}.json").is_file()
        res = c.get(f"/api/batch/{bid}.csv")
        assert "attachment" in res.headers["content-disposition"]
        lines = res.text.strip().splitlines()
        assert lines[0].startswith("i,label,input,ra,dec") and len(lines) == 4
        assert "WISE 0855" in lines[1] and "mover" in lines[1] and "-8000" in lines[1]
        assert len(c.get(f"/api/batch/{bid}.json").json()) == 3
        assert c.get("/api/batch/nope").status_code == 404
        assert c.get("/api/batch/..%2Fx").status_code == 404
    # a fresh process (empty registry) reads the same results back from disk
    app2 = FastAPI()
    app2.include_router(batch.router)
    with TestClient(app2) as c2:
        d2 = c2.get(f"/api/batch/{bid}").json()
    assert d2["rows"] == d["rows"] and d2["status"] == "complete"


def test_names_resolved_and_failures_isolated(env, monkeypatch):
    async def fake_resolve(name, client):
        if name == "Nowhere":
            raise CoordError("Sesame could not resolve 'Nowhere'")
        return Resolved(name="* alf Lyr", ra=279.23473, dec=38.78369, source="Simbad",
                        pm_ra_masyr=200.0, pm_dec_masyr=286.0)

    monkeypatch.setattr(batch, "resolve_name", fake_resolve)
    with TestClient(env.app) as c:
        bid = c.post("/api/batch", json={"targets": "Nowhere\nVega\n"}).json()["id"]
        d = wait_done(c, bid)
    bad, ok = d["rows"]
    assert bad["status"] == "error" and "could not resolve" in bad["error"]
    assert ok["status"] == "complete" and ok["label"] == "* alf Lyr" and ok["ra"] == pytest.approx(279.23473)
    assert "pmepoch=51544.5" in ok["view_url"]


def test_cancel_marks_remaining_rows(env):
    env.fields.delay = 60.0                       # field builds never finish
    with TestClient(env.app) as c:
        bid = c.post("/api/batch", json={"targets": EXAMPLES}).json()["id"]
        time.sleep(0.1)
        d = c.delete(f"/api/batch/{bid}").json()
        assert d["status"] == "complete" and d["cancelled"] is True
        assert [r["status"] for r in d["rows"]] == ["error"] * 3 and d["rows"][0]["error"] == "cancelled"
        time.sleep(0.1)
        assert c.get(f"/api/batch/{bid}").json()["rows"][0]["error"] == "cancelled"
