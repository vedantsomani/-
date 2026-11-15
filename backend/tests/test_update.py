"""'Check for new observations': re-query the archive (fakes only) and process only unseen exposures."""

import httpx
import pytest
from fastapi.testclient import TestClient

from app import fields, main, store
from app.config import settings
from app.fields import FieldJob, FieldManager, field_key
from app.processing import Grid
from app.sia import ImageRecord

NOCOV = "no coverage of target grid"
GRID = Grid.for_fov(150.11916, 2.20583, 5.0, 6.15)


def _rec(i: int, det: int = 1) -> ImageRecord:
    return ImageRecord("spherex_dr1", f"2025W19_1B_{i:04d}_1", det, f"https://irsa/{i}.fits", 60800.0 + i,
                       60800.1 + i, 0.75, 5.0)


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "cache_dir", tmp_path)
    store._touched.clear()
    return tmp_path


def _complete_job(mgr: FieldManager, ids: list[str]) -> FieldJob:
    """Three known exposures: two processed, one that never had coverage."""
    job = FieldJob(key=field_key(GRID), grid=GRID, status="complete", total=3)
    job.stack_version = fields.ANALYSIS_TAG
    job.frames = {i: {"id": i, "mjd": 60800.0, "detector": 1, "season": 0} for i in ids[:2]}
    job.failed = {ids[2]: NOCOV}
    job.last_checked = 1000.0
    mgr.jobs[job.key] = job
    return job


@pytest.mark.anyio
async def test_update_processes_only_unseen_exposures_then_reanalyses(cache, monkeypatch):
    recs = [_rec(i) for i in range(5)]
    ids = [r.frame_id for r in recs]
    mgr = FieldManager()
    job = _complete_job(mgr, ids)
    seen_fresh, attempted, analysed = [], [], []

    async def fake_find(client, ra, dec, fresh=False):
        seen_fresh.append(fresh)
        return recs

    async def fake_frame(self, j, rec, season):
        attempted.append(rec.frame_id)
        j.frames[rec.frame_id] = {"id": rec.frame_id, "mjd": 60805.0, "detector": 1, "season": season}

    def fake_run(key, g, frames):
        analysed.append(len(frames))
        return {}, {"new": True}

    monkeypatch.setattr(fields, "find_images", fake_find)
    monkeypatch.setattr(FieldManager, "_frame", fake_frame)
    monkeypatch.setattr(fields.analysis, "run", fake_run)
    try:
        got = await mgr.update(GRID)
        assert got is job and seen_fresh == [True]       # the SIA cache was bypassed
        assert job.new_frames == 2 and job.updating and job.status in ("queued", "processing")
        d = mgr.describe(job)
        assert d["new_frames"] == 2 and d["last_checked"].endswith("+00:00")
        await job.task
        assert sorted(attempted) == sorted(ids[3:])      # neither the processed nor the failed one
        assert analysed == [4] and job.analysis == {"new": True}
        assert job.status == "complete" and not job.updating and job.total == 5
        assert job.failed == {ids[2]: NOCOV}
        assert job.last_checked > 1000.0
        loaded = FieldJob.load(job.key, GRID)            # persisted
        assert loaded and len(loaded.frames) == 4 and abs(loaded.last_checked - job.last_checked) < 1
    finally:
        await mgr.close()


@pytest.mark.anyio
async def test_update_with_nothing_new_only_stamps_the_check(cache, monkeypatch):
    recs = [_rec(i) for i in range(3)]
    mgr = FieldManager()
    job = _complete_job(mgr, [r.frame_id for r in recs])
    analysed = []

    async def fake_find(client, ra, dec, fresh=False):
        return recs

    monkeypatch.setattr(fields, "find_images", fake_find)
    monkeypatch.setattr(fields.analysis, "run", lambda *a: analysed.append(1))
    try:
        await mgr.update(GRID)
        assert job.new_frames == 0 and job.task is None and not job.updating
        assert job.status == "complete" and analysed == [] and job.last_checked > 1000.0
        assert (cache / "fields" / job.key / "manifest.json").exists()
    finally:
        await mgr.close()


@pytest.mark.anyio
async def test_update_leaves_the_field_alone_when_the_archive_is_down_or_busy(cache, monkeypatch):
    recs = [_rec(i) for i in range(4)]
    mgr = FieldManager()
    job = _complete_job(mgr, [r.frame_id for r in recs[:3]])
    calls = []

    async def boom(client, ra, dec, fresh=False):
        calls.append(1)
        raise httpx.ConnectError("irsa down")

    monkeypatch.setattr(fields, "find_images", boom)
    try:
        with pytest.raises(httpx.ConnectError):
            await mgr.update(GRID)
        assert job.status == "complete" and job.last_checked == 1000.0 and job.new_frames is None
        job.updating = True                               # another update is running: just report it
        assert await mgr.update(GRID) is job and calls == [1]
        assert job.maintaining and not job.cancellable(1e12, 1, 1)
    finally:
        await mgr.close()


def test_plan_reports_update_without_charging_unknown_fields(cache):
    mgr = FieldManager()
    assert mgr.plan(GRID, update=True) == "build"        # nothing cached: an update is just a build
    job = _complete_job(mgr, ["a", "b", "c"])
    assert mgr.plan(GRID, update=True) == "update" and mgr.plan(GRID) == "none"
    job.status = "processing"
    assert mgr.plan(GRID, update=True) == "none"          # still building: nothing new to start


def test_frames_route_reports_new_frames_and_last_checked(cache, monkeypatch):
    recs = [_rec(i) for i in range(3)]
    mgr = FieldManager()
    _complete_job(mgr, [r.frame_id for r in recs])
    fail = []

    async def fake_find(client, ra, dec, fresh=False):
        if fail:
            raise httpx.ConnectError("irsa down")
        return recs

    monkeypatch.setattr(fields, "find_images", fake_find)
    monkeypatch.setattr(main.app.state, "fields", mgr, raising=False)
    c = TestClient(main.app)
    q = "/api/frames?ra=150.11916&dec=2.20583&fov=5&update=1"
    r = c.get(q)
    assert r.status_code == 200
    body = r.json()
    assert body["new_frames"] == 0 and body["last_checked"] and body["status"] == "complete"
    plain = c.get("/api/frames?ra=150.11916&dec=2.20583&fov=5").json()   # other callers see the same stamp
    assert plain["last_checked"] == body["last_checked"]
    fail.append(1)
    assert c.get(q).status_code == 502
