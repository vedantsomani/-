"""Public-use hardening and the catalogue overlay, with fakes only (no network):
retry selection, job interest / cancellation / queueing, cache eviction, rate limits, overlay."""

import asyncio
import math
import os
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import fields, limits, overlay, store
from app.catalogs import GAIA_DR3_EPOCH_MJD, CatalogResult, Star
from app.config import settings
from app.fields import FieldJob, FieldManager, classify_failure, field_key, select_retry
from app.processing import Grid
from app.sia import ImageRecord

T503 = "RuntimeError: cutout download failed: HTTP 503"
T404 = "RuntimeError: cutout download failed: Client error '404 Not Found' for url 'https://x'"
NOCOV = "no coverage of target grid"


# ------------------------------------------------------------------------- retry selection
def test_classify_failure():
    assert classify_failure(T503) == "transient"
    assert classify_failure("RuntimeError: cutout download failed: HTTP 429") == "transient"
    assert classify_failure("RuntimeError: cutout download failed: ReadTimeout") == "transient"
    assert classify_failure("RuntimeError: cutout download failed: HTTP 404") == "permanent"
    assert classify_failure(T404) == "permanent"
    assert classify_failure(NOCOV) == "permanent"
    assert classify_failure("ValueError: bad WCS") == "permanent"


def test_select_retry_only_transient_and_cooldown():
    failed = {"a": T503, "b": NOCOV, "c": T404, "d": "RuntimeError: cutout download failed: HTTP 502"}
    kw = dict(now=1000.0, attempts=0, cooldown_s=900, auto_max=3)
    assert select_retry(failed, last_attempt=0.0, explicit=False, **kw) == ["a", "d"]
    assert select_retry(failed, last_attempt=500.0, explicit=False, **kw) == []           # cooling down
    assert select_retry(failed, last_attempt=500.0, explicit=True, **kw) == ["a", "d"]    # explicit ignores it
    assert select_retry(failed, last_attempt=0.0, explicit=False, **{**kw, "attempts": 3}) == []
    assert select_retry({"b": NOCOV, "c": T404}, last_attempt=0.0, explicit=True, **kw) == []


def _rec(i: int, det: int = 1) -> ImageRecord:
    return ImageRecord("spherex_dr1", f"2025W19_1B_{i:04d}_1", det, f"https://irsa/{i}.fits", 60800.0 + i,
                       60800.1 + i, 0.75, 5.0)


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "cache_dir", tmp_path)
    store._touched.clear()
    return tmp_path


@pytest.mark.anyio
async def test_retry_reattempts_only_transient_failures_then_reanalyses(cache, monkeypatch):
    grid = Grid.for_fov(150.11916, 2.20583, 5.0, 6.15)
    recs = [_rec(i) for i in range(4)]
    ids = [r.frame_id for r in recs]
    mgr = FieldManager()
    job = FieldJob(key=field_key(grid), grid=grid, status="complete", total=4)
    job.stack_version = fields.ANALYSIS_TAG
    job.frames = {ids[0]: {"id": ids[0], "mjd": 1.0, "detector": 1}}
    job.failed = {ids[1]: T503, ids[2]: NOCOV, ids[3]: T404}
    job.last_attempt = time.time() - 10_000
    job.analysis = {"old": True}
    mgr.jobs[job.key] = job
    attempted, analysed = [], []

    async def fake_find(client, ra, dec):
        return recs

    async def fake_frame(self, j, rec, season):
        attempted.append(rec.frame_id)
        j.frames[rec.frame_id] = {"id": rec.frame_id, "mjd": 2.0, "detector": 1}

    def fake_run(key, g, frames):
        analysed.append(len(frames))
        return {}, {"new": True}

    monkeypatch.setattr(fields, "find_images", fake_find)
    monkeypatch.setattr(FieldManager, "_frame", fake_frame)
    monkeypatch.setattr(fields.analysis, "run", fake_run)
    try:
        assert mgr.plan(grid, retry=True) == "retry" and mgr.plan(grid) == "none"
        got = mgr.get_or_start(grid)               # automatic: transient failure older than the cool-down
        assert got is job and job.retrying
        await job.task
        assert attempted == [ids[1]]               # only the transient failure was re-attempted
        assert job.failed == {ids[2]: NOCOV, ids[3]: T404}
        assert analysed == [2] and job.analysis == {"new": True}
        assert job.status == "complete" and job.retry_attempts == 1 and not job.retrying
        assert (cache / "fields" / job.key / "manifest.json").exists()
        mgr.get_or_start(grid)                     # within the cool-down and nothing transient left
        assert job.task.done() and not job.retrying
        mgr.get_or_start(grid, retry=True)         # explicit, but nothing transient left either
        assert not job.retrying
    finally:
        await mgr.close()


@pytest.mark.anyio
async def test_failed_retry_keeps_the_field_intact(cache, monkeypatch):
    grid = Grid.for_fov(10.0, 10.0, 5.0, 6.15)
    mgr = FieldManager()
    job = FieldJob(key=field_key(grid), grid=grid, status="complete", total=1)
    job.stack_version = fields.ANALYSIS_TAG
    job.failed = {"x_D1": T503}
    job.last_attempt = 0.0
    mgr.jobs[job.key] = job

    async def boom(client, ra, dec):
        raise RuntimeError("SIA down")

    monkeypatch.setattr(fields, "find_images", boom)
    try:
        mgr.get_or_start(grid, retry=True)
        await job.task
        assert job.status == "complete" and job.failed == {"x_D1": T503} and job.retry_attempts == 1
    finally:
        await mgr.close()


# --------------------------------------------------------- interest, cancellation, queueing
class _Task:
    def __init__(self, done=False):
        self._d, self.cancelled = done, False

    def done(self):
        return self._d

    def cancel(self):
        self.cancelled = True


def _job(status="processing", **kw):
    j = FieldJob(key="k", grid=Grid.for_fov(1.0, 1.0, 5.0, 6.15), status=status)
    j.task = _Task()
    for k, v in kw.items():
        setattr(j, k, v)
    return j


def test_cancellable_only_when_nobody_polls():
    now = 10_000.0
    j = _job(last_poll=now - 100)
    assert not j.cancellable(now, idle_s=600, grace_s=60)
    assert j.cancellable(now, idle_s=50, grace_s=60)                      # idle for too long
    assert not _job("complete", last_poll=0).cancellable(now, 600, 60)    # finished fields are never cancelled
    assert not _job(retrying=True, last_poll=0).cancellable(now, 600, 60)
    assert _job("queued", last_poll=0).cancellable(now, 600, 60)


def test_delete_only_drops_interest():
    now = 10_000.0
    j = _job(last_poll=now)
    j.touch("alice", now)
    j.touch("bob", now)
    assert j.release("alice", now) == 1                                   # bob still polls: keep building
    assert not j.cancellable(now + 500, idle_s=600, grace_s=60)           # bob only left; alice's poll is recent
    assert j.released_at is None
    assert j.release("bob", now + 1) == 0
    assert not j.cancellable(now + 30, 600, 60)                           # grace period
    assert j.cancellable(now + 120, 600, 60)
    j.touch("carol", now + 100)                                           # someone came back
    assert j.released_at is None and not j.cancellable(now + 120, 600, 60)


@pytest.mark.anyio
async def test_reap_cancels_abandoned_build_and_keeps_active_ones(cache):
    mgr = FieldManager()
    try:
        idle, busy = _job(last_poll=0.0), _job(last_poll=time.time())
        idle.key, busy.key = "idle", "busy"
        idle.task = asyncio.create_task(asyncio.sleep(60))
        busy.task = asyncio.create_task(asyncio.sleep(60))
        mgr.jobs = {"idle": idle, "busy": busy}
        assert mgr.reap_idle() == ["idle"]
        await asyncio.sleep(0)
        assert idle.task.cancelled() and idle.status == "cancelled" and "idle" not in mgr.jobs
        assert "busy" in mgr.jobs and not busy.task.cancelled()
        assert mgr.release("busy", "nobody") == {"key": "busy", "status": "processing", "interest": 0,
                                                 "cancel_scheduled": True}
        assert mgr.release("gone", "x")["status"] == "unknown"
        busy.task.cancel()
    finally:
        await mgr.close()


@pytest.mark.anyio
async def test_builds_are_bounded_and_reported_queued(cache, monkeypatch):
    monkeypatch.setattr(settings, "max_concurrent_builds", 1)
    mgr = FieldManager()

    async def fake_run(job):
        async with mgr._build_slots:
            job.status = "processing"
            await asyncio.sleep(0.2)
            job.status = "complete"

    mgr._run = fake_run
    try:
        g1, g2 = Grid.for_fov(10.0, 10.0, 5.0, 6.15), Grid.for_fov(20.0, 20.0, 5.0, 6.15)
        j1, j2 = mgr.get_or_start(g1), mgr.get_or_start(g2)
        assert j1.status == "pending" and j2.status == "queued"
        await asyncio.sleep(0.05)
        assert j1.status == "processing" and j2.status == "queued"
        assert mgr.describe(j2)["queue_position"] == 1 and mgr.describe(j1)["queue_position"] is None
        await asyncio.gather(j1.task, j2.task)
        assert j2.status == "complete" and mgr._admitted == 0
    finally:
        await mgr.close()


# ------------------------------------------------------------------------------ cache limit
def _unit(kind, key, size, age, now=10_000.0):
    return store.Unit(kind, key, [], size, now - age)


def test_plan_eviction_order_and_protections():
    now = 10_000.0
    units = [
        _unit("raw", "a", 100, 5000), _unit("processed", "a", 100, 5000),
        _unit("processed", "old", 100, 9000), _unit("processed", "mid", 100, 6000),
        _unit("processed", "busy", 100, 9500), _unit("processed", "fresh", 100, 10),
        _unit("raw", "older-raw", 100, 9900 - 1),
    ]
    kw = dict(now=now, protect_s=600, active_keys={"busy"}, target=1.0)
    assert store.plan_eviction(units, 700, 1000, **kw) == []                       # under the limit
    plan = store.plan_eviction(units, 1000, 600, **kw)
    keys = [(u.kind, u.key) for u in plan]
    assert keys == [("processed", "old"), ("processed", "mid"), ("processed", "a"), ("raw", "older-raw")][: len(keys)]
    assert not {"busy", "fresh"} & {u.key for u in plan}                           # active / recent: never
    # processed products go before any raw cutout, least recently used first
    plan = store.plan_eviction(units, 1000, 0, **kw)
    assert [(u.kind, u.key) for u in plan] == [
        ("processed", "old"), ("processed", "mid"), ("processed", "a"), ("raw", "older-raw"), ("raw", "a")
    ][:len(plan)]
    assert [u.kind for u in plan] == sorted([u.kind for u in plan])
    # stops as soon as the target is reached
    assert len(store.plan_eviction(units, 1000, 900, **{**kw, "target": 1.0})) == 1


def test_plan_eviction_protects_active_cutout_files(tmp_path):
    f = tmp_path / "x.fits"
    u = store.Unit("raw", "orphan:x", [f], 100, 0.0, orphan=True)
    assert store.plan_eviction([u], 200, 10, 10_000.0, 600, set(), {f}) == []
    assert store.plan_eviction([u], 200, 10, 10_000.0, 600, set(), set()) == [u]


def _put(path, n, age, now):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * n)
    os.utime(path, (now - age, now - age))


def test_enforce_cache_limit_end_to_end(cache, monkeypatch):
    now = time.time()
    monkeypatch.setattr(settings, "cache_protect_s", 600)
    monkeypatch.setattr(settings, "cache_evict_target", 1.0)
    for key, age in (("old", 9000), ("mid", 5000), ("recent", 5), ("busy", 9500)):
        _put(cache / "fields" / key / "frames" / "f.npz", 1000, age, now)
        _put(cache / "fields" / key / "manifest.json", 10, age, now)
        os.utime(cache / "fields" / key, (now - age, now - age))
    _put(cache / "fingerprints" / "old" / "r.json", 500, 9000, now)
    _put(cache / "psf" / "lib.fits", 5000, 99999, now)                      # never evicted
    _put(cache / "cutouts" / "spherex_dr1" / "o_aaa.fits", 3000, 9999, now)  # orphan raw
    keep = lambda: ({"busy"}, set())  # noqa: E731
    before = store.cache_usage()["bytes"]
    assert store.enforce_cache_limit(lambda k: [], keep, limit=before + 1)["evicted"] == []
    res = store.enforce_cache_limit(lambda k: [], keep, limit=before - 2000, now=now)
    kinds = [(k, key) for k, key, _ in res["evicted"]]
    assert kinds[0] == ("processed", "old") and ("processed", "mid") in kinds
    assert ("processed", "recent") not in kinds and ("processed", "busy") not in kinds
    assert (cache / "psf" / "lib.fits").exists()
    assert not (cache / "fields" / "old").exists() and not (cache / "fingerprints" / "old").exists()
    assert (cache / "fields" / "busy").exists() and (cache / "fields" / "recent").exists()
    assert res["processed_keys"][0] == "old" and res["bytes_after"] <= before - 2000


def test_touch_field_updates_lru_clock(cache):
    d = cache / "fields" / "k"
    d.mkdir(parents=True)
    os.utime(d, (1.0, 1.0))
    assert store.field_last_access("k") < 100
    store.touch_field("k")
    assert store.field_last_access("k") > time.time() - 5


# ------------------------------------------------------------------------------- rate limits
def test_token_bucket():
    b = limits.TokenBucket(rate=1.0, burst=3, now=0.0)
    assert [b.take(0.0) for _ in range(3)] == [0.0, 0.0, 0.0]
    assert b.take(0.0) == pytest.approx(1.0)                  # empty: wait 1 s
    assert b.take(0.5) == pytest.approx(0.5)                  # half a token refilled
    assert b.take(1.0) == 0.0
    assert b.take(100.0) == 0.0 and b.tokens == pytest.approx(2.0)   # refill is capped at the burst


def test_limiter_is_per_client_and_per_category():
    t = [0.0]
    lim = limits.RateLimiter(
        policies={"general": limits.Policy(600, 5), "build": limits.Policy(6, 2), "ai": limits.Policy(1, 1)},
        clock=lambda: t[0])
    assert [lim.check("a", "build") for _ in range(2)] == [0.0, 0.0]
    assert lim.check("a", "build") == pytest.approx(10.0)
    assert lim.check("b", "build") == 0.0 and lim.check("a", "general") == 0.0
    t[0] = 10.0
    assert lim.check("a", "build") == 0.0


def _limited_app(**policies):
    app = FastAPI()
    lim = limits.RateLimiter(policies={"general": limits.Policy(60, 3), "build": limits.Policy(6, 1),
                                       "ai": limits.Policy(6, 1), **policies})
    app.state.limiter = lim
    app.add_middleware(limits.RateLimitMiddleware, limiter=lim)

    @app.get("/api/x")
    def x():
        return {"ok": True}

    @app.get("/api/health")
    def h():
        return {"ok": True}

    @app.get("/api/explain")
    def e(ai: int = 0):
        return {"ai": ai}

    return app


def test_middleware_429_with_retry_after_and_exemptions():
    c = TestClient(_limited_app())
    assert [c.get("/api/x").status_code for _ in range(3)] == [200, 200, 200]
    r = c.get("/api/x")
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1
    assert c.get("/api/health").status_code == 200            # monitors are exempt
    c2 = TestClient(_limited_app())
    assert c2.get("/api/explain?ai=1").status_code == 200
    r = c2.get("/api/explain?ai=1")
    assert r.status_code == 429 and "Retry-After" in r.headers
    assert c2.get("/api/explain").status_code == 200          # plain explain uses the general bucket


def test_build_limit_is_charged_by_the_route_and_429s():
    app = _limited_app()

    @app.get("/api/start")
    def start(request: limits.Request):
        request.app.state.limiter.consume(request, limits.BUILD)
        return {"started": True}

    c = TestClient(app)
    assert c.get("/api/start").status_code == 200
    r = c.get("/api/start")
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1


def test_x_forwarded_for_only_when_trusted(monkeypatch):
    app = _limited_app()
    c = TestClient(app)
    h = {"x-forwarded-for": "6.6.6.6, 9.9.9.9"}
    monkeypatch.setattr(settings, "trust_proxy", False)
    for _ in range(3):
        c.get("/api/x", headers={"x-forwarded-for": f"1.1.1.{_}"})
    assert c.get("/api/x", headers={"x-forwarded-for": "2.2.2.2"}).status_code == 429   # header ignored
    monkeypatch.setattr(settings, "trust_proxy", True)
    monkeypatch.setattr(settings, "trusted_proxy_hops", 1)
    assert c.get("/api/x", headers=h).status_code == 200            # keyed by 9.9.9.9
    # a client cannot dodge the limit by spoofing the left-most entry
    for i in range(4):
        r = c.get("/api/x", headers={"x-forwarded-for": f"7.7.7.{i}, 9.9.9.9"})
    assert r.status_code == 429


# --------------------------------------------------------------------------------- overlay
GRID = Grid.for_fov(150.0, 2.0, 5.0, 6.15)
C = GRID.center_pix
MID = 61000.0


def star_at_centre(sid, pmra, pmdec, *, dra=0.0, ddec=0.0, g=12.0, variable=False, has_pm=True):
    """A Gaia star that is (dra, ddec) arcsec off the grid centre at MJD 61000."""
    dt = (MID - GAIA_DR3_EPOCH_MJD) / 365.25
    ra = GRID.ra + (dra / 3600 - pmra * dt / 3.6e6) / math.cos(math.radians(GRID.dec))
    dec = GRID.dec + ddec / 3600 - pmdec * dt / 3.6e6
    return Star(sid, ra, dec, pmra, pmdec, GAIA_DR3_EPOCH_MJD, g, variable, has_pm)


def cand(i="c0", kind="mover", evidence="strong", pm=(-884, 9461), x=C, y=C, **kw):
    c = {"id": i, "kind": kind, "evidence": evidence, "x": float(x), "y": float(y), "max_z": 9.0,
         "events": [], **kw}
    if pm and kind == "mover":
        c.update(pmra_masyr=pm[0], pmdec_masyr=pm[1], pm_total_masyr=round(math.hypot(*pm)))
    return c


FRAMES = [{"mjd": 60900.0, "season": 0}, {"mjd": 61000.0, "season": 1}, {"mjd": 61100.0, "season": 2}]
SEASONS = [{"season": s, "mjd_start": 60900.0 + 100 * s, "mjd_end": 60900.0 + 100 * s} for s in range(3)]


def run(stars, cands, ok=True):
    cat = CatalogResult(ok=ok, stars=stars, note=None if ok else "Gaia DR3 query failed (Timeout)")
    return overlay.build_overlay(GRID, FRAMES, SEASONS, cands, cat)


def test_propagation_to_pixels():
    # 1000 mas/yr north for 9.888 yr = 9.89" = 1.61 px north (+y); 1000 mas/yr east moves -x
    s = Star("1", GRID.ra, GRID.dec, 0.0, 1000.0, GAIA_DR3_EPOCH_MJD, 10.0, False)
    out = run([s], [])
    src = out["sources"][0]
    yrs = (MID - GAIA_DR3_EPOCH_MJD) / 365.25
    assert src["x"] == pytest.approx(C, abs=0.01) and src["y"] == pytest.approx(C + yrs / 6.15, abs=0.01)
    assert src["seasons"]["0"][1] == pytest.approx(C + (60900.0 - GAIA_DR3_EPOCH_MJD) / 365.25 / 6.15, abs=0.01)
    assert set(src["seasons"]) == {"0", "1", "2"} and src["pm_total_masyr"] == pytest.approx(1000)
    e = Star("2", GRID.ra, GRID.dec, 1000.0, 0.0, GAIA_DR3_EPOCH_MJD, 10.0, None)
    assert run([e], [])["sources"][0]["x"] < C - 1.0                     # east is -x
    assert out["epochs"]["reference_mjd"] == 61000.0 and out["grid"]["n"] == GRID.n


def test_sources_outside_the_field_are_not_listed_but_still_match():
    far = star_at_centre("far", 0, 0, dra=600.0)                          # 10 arcmin east
    assert run([far], [])["sources"] == []
    out = run([star_at_centre("in", 0, 0, dra=30.0)], [])
    assert [s["source_id"] for s in out["sources"]] == ["in"] and out["catalog"]["n_in_field"] == 1


def test_barnards_star_is_a_known_high_proper_motion_star():
    barnard = star_at_centre("4472832130942575872", -802.8, 10362.5, g=8.2)
    out = run([barnard], [cand(is_target=True)])
    c = out["candidates"]["c0"]
    assert c["label"] == "known high-proper-motion star" and c["match"]["source_id"] == barnard.source_id
    assert c["match"]["separation_arcsec"] < 1 and c["match"]["pm_agrees"] is True
    assert c["candidate_new_object"] is False


def test_mover_with_no_gaia_source_is_flagged_but_documented_as_not_proof():
    out = run([star_at_centre("faraway", 0, 0, dra=40.0, ddec=40.0)], [cand(is_target=True, pm=(-7661, 1192))])
    c = out["candidates"]["c0"]
    assert c["label"] == "no Gaia match" and c["match"] is None and c["candidate_new_object"] is True
    text = " ".join(out["notes"])
    assert "not proof of novelty" in text and "brown dwarfs" in text


def test_new_object_requires_strong_or_moderate_mover_and_no_gaia_within_10_arcsec():
    star10 = star_at_centre("near", 0, 0, dra=8.0)                       # beyond the 6" match radius, inside 10"
    for kw, expect in [
        (dict(), True),
        (dict(evidence="moderate"), True),
        (dict(evidence="weak"), False),
        (dict(kind="variable"), False),
        (dict(kind="uncertain"), False),
        (dict(kind="appeared"), False),
    ]:
        out = run([], [cand(**kw)])
        assert out["candidates"]["c0"]["candidate_new_object"] is expect, kw
    c = run([star10], [cand()])["candidates"]["c0"]
    assert c["match"] is None and c["label"] == "no Gaia match"          # outside 6"
    assert c["gaia_within_novelty_radius"] is True and c["candidate_new_object"] is False
    near = run([star_at_centre("near", 0, 0, dra=4.0)], [cand()])["candidates"]["c0"]
    assert near["match"]["separation_arcsec"] == pytest.approx(4.0, abs=0.1) and not near["candidate_new_object"]


def test_label_rules():
    # static catalogued star at a variable-kind candidate
    static = star_at_centre("s", 20, -10)
    assert run([static], [cand(kind="uncertain", evidence="weak", pm=None)])["candidates"]["c0"]["label"] == \
        "catalogued, not known to move or vary"
    var = star_at_centre("v", 20, -10, variable=True)
    assert run([var], [cand(kind="variable", pm=None)])["candidates"]["c0"]["label"] == "known Gaia variable"
    # high proper motion + non-mover candidate: Gaia motion alone decides
    fast = star_at_centre("f", 300, 0)
    assert run([fast], [cand(kind="variable", pm=None)])["candidates"]["c0"]["label"] == \
        "known high-proper-motion star"
    # mover candidate: Gaia motion must agree in direction (45 deg) and speed (factor 2)
    meas = (-884, 9461)
    for g, ok in [((-800, 10362), True), ((-800, 4800), True), ((-800, 4000), False),    # too slow / ok / too slow
                  ((-800, 20000), False), ((9000, 3000), False), ((800, -10362), False)]:
        lab = run([star_at_centre("m", *g)], [cand(pm=meas)])["candidates"]["c0"]
        assert (lab["label"] == "known high-proper-motion star") is ok, g
        if not ok:
            assert "label_note" in lab and lab["candidate_new_object"] is False
    # a slow Gaia star under a fast mover candidate is not "known high proper motion"
    slow = run([star_at_centre("sl", 40, 30)], [cand(pm=meas)])["candidates"]["c0"]
    assert slow["label"] == "catalogued, not known to move or vary"


def test_match_uses_candidate_epoch_for_fast_stars():
    # a star moving 10"/yr is 2.7" south of the centre 100 days before MJD 61000: the epoch matters
    fast = star_at_centre("f", 0, 10000)
    y_s0 = C - 10.0 * (100 / 365.25) / 6.15
    ev = {"season_a": 0, "season_b": 0}
    s0 = cand("c1", events=[ev], pm=(0, 10000), x=C, y=y_s0)
    out = run([fast], [cand(is_target=True), s0, cand("c2", events=[ev], pm=(0, 10000), x=C, y=C)])["candidates"]
    assert out["c0"]["match"]["separation_arcsec"] < 1                 # the target sits at the MJD 61000 epoch
    assert out["c1"]["epoch_mjd"] == pytest.approx(60900.0)            # season 0 mean epoch
    assert out["c1"]["match"]["separation_arcsec"] < 0.5
    assert out["c2"]["match"]["separation_arcsec"] == pytest.approx(2.74, abs=0.2)  # wrong epoch: 2.7" off


def test_failed_catalogue_query_and_unmatched_stay_honest():
    out = run([], [cand()], ok=False)
    assert out == {"status": "complete", "catalog": {"name": "Gaia DR3", "ok": False,
                                                     "note": "Gaia DR3 query failed (Timeout)"},
                   "sources": [], "candidates": {}, "notes": out["notes"]}
    big = Grid.for_fov(150.0, 2.0, 40.0, 6.15)
    mgr = overlay.OverlayManager(fetch=None)
    r = mgr.request("k", big, [], [], [], True)
    assert r["catalog"]["ok"] is False and r["sources"] == [] and "limited" in r["catalog"]["note"]


class _FakeJob(SimpleNamespace):
    def touch(self, client=None):
        pass


def test_overlay_route_processes_in_background_then_completes():
    calls = []

    async def fetch(grid):
        calls.append(grid)
        await asyncio.sleep(0.2)
        return CatalogResult(ok=True, stars=[star_at_centre("1", 0, 0, g=10.0)])

    job = _FakeJob(grid=GRID, frames=dict(enumerate(FRAMES)), seasons=SEASONS, status="complete", error=None,
                   stack_version="1.5", analysis={"target": {}, "candidates": [cand(is_target=True)]})
    app = FastAPI()
    app.include_router(overlay.router)
    app.state.fields = SimpleNamespace(jobs={"k": job})
    with TestClient(app) as c:
        app.state.overlays = overlay.OverlayManager(fetch=fetch)
        assert c.get("/api/overlay?key=nope").status_code == 404
        assert c.get("/api/overlay?key=k").json() == {"status": "processing"}
        assert c.get("/api/overlay?key=k").json() == {"status": "processing"}
        time.sleep(0.5)
        body = c.get("/api/overlay?key=k").json()
        assert body["status"] == "complete" and body["key"] == "k" and len(calls) == 1   # cached: one query
        assert body["sources"][0]["source_id"] == "1" and body["candidates"]["c0"]["match"]["source_id"] == "1"
        job.status = "building"
        assert c.get("/api/overlay?key=k").json() == {"status": "processing"}
        job.status, job.error = "error", "boom"
        assert c.get("/api/overlay?key=k").json()["status"] == "error"


def test_failed_gaia_query_returns_complete_with_catalog_note_then_retries(monkeypatch):
    monkeypatch.setattr(settings, "overlay_retry_after_s", 0.6)
    n = []

    async def fetch(grid):
        n.append(1)
        return CatalogResult(ok=False, note="Gaia DR3 query failed (ReadTimeout)")

    job = _FakeJob(grid=GRID, frames={}, seasons=[], status="complete", error=None, stack_version="1",
                   analysis={"target": {}, "candidates": []})
    app = FastAPI()
    app.include_router(overlay.router)
    app.state.fields = SimpleNamespace(jobs={"k": job})
    with TestClient(app) as c:
        app.state.overlays = overlay.OverlayManager(fetch=fetch)
        c.get("/api/overlay?key=k")
        time.sleep(0.2)
        body = c.get("/api/overlay?key=k").json()
        assert body["status"] == "complete" and body["catalog"]["ok"] is False and body["sources"] == []
        assert len(n) == 1
        time.sleep(0.6)
        c.get("/api/overlay?key=k")                       # past the retry time: queried again
        time.sleep(0.1)
        assert len(n) == 2
