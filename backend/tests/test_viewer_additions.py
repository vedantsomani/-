"""Regression checks for visit stacks, difference controls, and AllWISE (offline)."""
import asyncio
from types import SimpleNamespace
import numpy as np
from app import analysis, catalogs, changes, colour, overlay
from app.config import settings
from app.processing import Grid
from app.render import render_diff_png


def test_visit_stacks_stay_separate_from_season_products(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "cache_dir", tmp_path)
    from app.store import frame_path, save_arrays
    grid = Grid(10, 10, 31, 6.15)
    n = grid.n
    frames = []
    for i, (visit, flux) in enumerate([("a", 1.), ("a", 3.), ("b", 8.)]):
        fid = str(i)
        frames.append(dict(id=fid, season=0, detector=1, visit=visit, mjd=61000.+i,
                           wavelength_um=1., date="2025-01-01T00:00:00"))
        arr = dict(sci=np.full((n,n), flux), var=np.ones((n,n)),
                   footprint=np.ones((n,n)), excluded=np.zeros((n,n),bool), wave=np.ones((n,n)))
        frame_path("visits",fid).parent.mkdir(parents=True,exist_ok=True)
        save_arrays(frame_path("visits",fid),arr)
    stacks, result = analysis.run("visits",grid,frames)
    assert stacks["s0_d1"]["n_frames"] == 3
    assert stacks["s0v0_d1"]["n_frames"] == 2
    assert stacks["s0v1_d1"]["n_frames"] == 1
    assert colour.season_stacks(SimpleNamespace(stacks=stacks),0) == {1:"s0_d1"}


def test_difference_palette_and_local_noise():
    rng = np.random.default_rng(7)
    z = rng.normal(size=(64,64))
    z[32:,:] *= 12
    local = changes.zmap_local(z)
    assert np.std(local[40:56,8:56]) < np.std(z[40:56,8:56])/3
    assert render_diff_png(z,floor=0) != render_diff_png(z)
    assert render_diff_png(z,gamma=2) != render_diff_png(z)
    z[0,0] = np.nan
    assert np.isnan(changes.zmap_local(z)[0,0])


def test_allwise_query_cache_and_http_failure(tmp_path,monkeypatch):
    monkeypatch.setattr(settings,"cache_dir",tmp_path)
    monkeypatch.setattr(catalogs.httpx,"get",lambda *a,**kw: SimpleNamespace(raise_for_status=lambda:None,text="ok"))
    monkeypatch.setattr(catalogs,"_parse_wise_sources",lambda text:[catalogs.WiseSource("test",10,10)])
    assert catalogs.wise_cone(10,10,.01).ok
    def fail(*a,**kw):
        raise RuntimeError("offline")
    monkeypatch.setattr(catalogs.httpx,"get",fail)
    assert catalogs.wise_cone(10,10,.01).sources[0].designation == "test"
    assert not catalogs.wise_cone(20,10,.01).ok


def test_allwise_manager_keeps_catwise_and_suppresses_novelty():
    async def run():
        async def gaia(g): return catalogs.CatalogResult(ok=True)
        async def cw(g): return catalogs.CatwiseResult(ok=True)
        async def wise(g): return catalogs.WiseResult(ok=True,sources=[catalogs.WiseSource("known",10,10)])
        mgr=overlay.OverlayManager(fetch=gaia,fetch_catwise=cw,fetch_wise=wise)
        grid=Grid(10,10,31,6.15)
        c=dict(id="c0",x=grid.center_pix,y=grid.center_pix,kind="mover",evidence="strong")
        args=("k",grid,[],[],[c],True)
        assert mgr.request(*args)["status"] == "processing"
        await asyncio.gather(*(t for e in mgr.entries.values() for t in (e.task,e.cw_task,e.wise_task)))
        body=mgr.request(*args)
        assert body["catwise"]["ok"] and body["wise"]["catalog"]["ok"]
        assert body["wise"]["candidates"]["c0"]["designation"] == "known"
        assert not body["candidates"]["c0"]["candidate_new_object"]
        await mgr.close()
    asyncio.run(run())


def test_allwise_parser_keeps_position_and_epoch_from_same_fit():
    import io
    from astropy.table import Table
    table = Table(dict(designation=["moving", "unreliable"], ra=[10., 11.], dec=[20., 21.],
                       ra_pm=[10.001, 11.001], dec_pm=[20.001, 21.001],
                       pmra=[1000., 1000.], pmdec=[2000., 2000.],
                       sigpmra=[10., 999.], sigpmdec=[10., 999.],
                       w2mjdmean=[55350., 55360.], var_flg=["0000", "nnnn"], ph_qual=["AAAA", "AAAA"]))
    text = io.StringIO()
    table.write(text, format="ascii.ipac")
    moving, stationary = catalogs._parse_wise_sources(text.getvalue())
    assert (moving.ra, moving.dec, moving.epoch_mjd) == (10.001, 20.001, 55400.)
    assert (stationary.ra, stationary.dec, stationary.epoch_mjd) == (11., 21., 55360.)
    assert stationary.pmra is None and stationary.pmdec is None
    # Missing errors cannot justify extrapolating an apparent motion.
    table.remove_columns(["sigpmra", "sigpmdec", "ra_pm", "dec_pm"])
    text = io.StringIO()
    table.write(text, format="ascii.ipac")
    assert catalogs._parse_wise_sources(text.getvalue())[0].pmra is None


def test_allwise_position_propagation_uses_catalogue_epoch():
    import pytest
    source = catalogs.WiseSource("moving", 10., 0., pmra=1000., pmdec=0., sigpmra=10., sigpmdec=10.)
    grid = Grid(10., 0., 31, 6.15)
    arr = overlay.wise_arrays([source])
    x, y = overlay.wise_positions(arr, grid.wcs(), 55400. + 365.25 * 10)
    assert x[0] == pytest.approx(grid.center_pix - 10 / 6.15, abs=1e-5)
    source.sigpmra = None
    arr = overlay.wise_arrays([source])
    x, y = overlay.wise_positions(arr, grid.wcs(), 55400. + 365.25 * 10)
    assert x[0] == pytest.approx(grid.center_pix, abs=1e-5)


def test_allwise_failure_retries_without_claiming_novelty(monkeypatch):
    async def run():
        calls = {"gaia": 0, "wise": 0}
        async def gaia(g):
            calls["gaia"] += 1
            return catalogs.CatalogResult(ok=True)
        async def wise(g):
            calls["wise"] += 1
            return catalogs.WiseResult(ok=calls["wise"] > 1)
        mgr = overlay.OverlayManager(fetch=gaia, fetch_wise=wise)
        grid = Grid(10, 10, 31, 6.15)
        args = ("retry", grid, [], [], [dict(id="c0", x=15, y=15, kind="mover", evidence="strong")], True)
        mgr.request(*args)
        await asyncio.gather(mgr.entries["retry"].task, mgr.entries["retry"].wise_task)
        result = mgr.request(*args)
        assert not result["candidates"]["c0"]["candidate_new_object"]
        assert not result["candidates"]["c0"]["allwise_checked"]
        mgr.entries["retry"].wise_expires = 1
        mgr.request(*args)
        await mgr.entries["retry"].wise_task
        result = mgr.request(*args)
        assert result["wise"]["catalog"]["ok"]
        assert calls == {"gaia": 1, "wise": 2}
        await mgr.close()
    asyncio.run(run())


def test_allwise_matches_survive_gaia_failure():
    async def run():
        async def gaia(g): return catalogs.CatalogResult(ok=False, note="offline")
        async def wise(g): return catalogs.WiseResult(ok=True, sources=[catalogs.WiseSource("known", 10., 10.)])
        mgr = overlay.OverlayManager(fetch=gaia, fetch_wise=wise)
        grid = Grid(10, 10, 31, 6.15)
        args = ("independent", grid, [], [], [dict(id="c0", x=15, y=15, kind="mover", evidence="strong")], True)
        mgr.request(*args)
        await asyncio.gather(mgr.entries["independent"].task, mgr.entries["independent"].wise_task)
        result = mgr.request(*args)
        assert not result["catalog"]["ok"]
        assert result["wise"]["candidates"]["c0"]["designation"] == "known"
        await mgr.close()
    asyncio.run(run())


def test_difference_http_controls_and_validation(monkeypatch):
    from fastapi.testclient import TestClient
    from app import main
    z = np.arange(81, dtype=float).reshape(9, 9) / 10
    monkeypatch.setattr(main, "_job", lambda key: SimpleNamespace(analysis={"diffs": [{"id": "d1"}]}))
    monkeypatch.setattr(main, "diff_arrays", lambda *args: {"z": z, "zl": z / 3})
    client = TestClient(main.app)
    url = "/api/fields/test/diffs/d1.png"
    base = client.get(url)
    assert base.status_code == 200 and base.content.startswith(b"\x89PNG")
    assert client.get(url + "?floor=0&gamma=1.5&norm=local").content != base.content
    assert client.get(url + "?floor=-1").status_code == 422
    assert client.get(url + "?norm=invalid").status_code == 422
    assert client.get("/api/fields/test/diffs/missing.png").status_code == 404
