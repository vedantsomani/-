"""Injection-recovery machinery on synthetic frames (no cache access)."""

from __future__ import annotations

import math

import numpy as np
import pytest

from app import analysis, store
from app import sensitivity as S
from app.config import settings
from app.phot import ujy_per_mjysr_pixel
from app.processing import Grid

PX = 6.15
GRID = Grid.for_fov(10.0, 0.0, 3.2, PX)   # 31 x 31
KEY = "sensitivity-test-field"
WAVES = {1: 0.9, 2: 1.35, 3: 2.0, 4: 3.1, 5: 4.1, 6: 4.7}
SEASON_MJD = {0: 60800.0, 1: 60990.0, 2: 61170.0}
SIGMA = {d: 0.62 for d in range(1, 7)}


def synthetic_field(n_per: int = 5, noise: float = 0.004, seed: int = 0):
    rng = np.random.default_rng(seed)
    n = GRID.n
    frames, base = [], {}
    for s, mjd0 in SEASON_MJD.items():
        for det, lam in WAVES.items():
            for i in range(n_per):
                fid = f"s{s}_d{det}_{i}"
                frames.append({"id": fid, "season": s, "detector": det, "mjd": mjd0 + 0.3 * i,
                               "wavelength_um": lam, "visit": f"v{s}{i}", "obs_id": fid, "noise_mjysr": noise})
                base[fid] = {
                    "sci": rng.normal(0, noise, (n, n)).astype(np.float32),
                    "var": np.full((n, n), noise**2, np.float32),
                    "flags": np.zeros((n, n), np.int32),
                    "footprint": np.ones((n, n), np.float32),
                    "excluded": np.zeros((n, n), bool),
                    "wave": np.full((n, n), lam, np.float32),
                    "bandwidth": np.full((n, n), lam / 50, np.float32),
                }
    return frames, base


def t_ref(frames):
    return float(np.mean([f["mjd"] for f in frames]))


def test_grid_plus_x_is_west():
    w = GRID.wcs()
    c = GRID.center_pix
    ra0, dec0 = w.pixel_to_world_values(c, c)
    ra1, _ = w.pixel_to_world_values(c + 1, c)
    _, dec1 = w.pixel_to_world_values(c, c + 1)
    assert ra1 < ra0 and dec1 > dec0


def test_injected_flux_position_and_motion_sign():
    frames, base = synthetic_field(n_per=1, noise=0.0)
    inj = S.Injection(x0=10.3, y0=20.6, mu=16.0, pa_deg=90.0, flux_ujy=500.0, shape="flat", t_ref=t_ref(frames))
    out = S.inject(frames, base, inj, SIGMA, PX)
    k = ujy_per_mjysr_pixel(PX)
    yy, xx = np.mgrid[0:GRID.n, 0:GRID.n]
    early = next(f for f in frames if f["season"] == 0)
    late = next(f for f in frames if f["season"] == 2)
    for f in (early, late):
        img = out[f["id"]]["sci"].astype(float)
        assert img.sum() * k == pytest.approx(500.0, rel=1e-3)
        x, y = (img * xx).sum() / img.sum(), (img * yy).sum() / img.sum()
        ex, ey = inj.position(f["mjd"], PX)
        assert (x, y) == (pytest.approx(ex, abs=1e-3), pytest.approx(ey, abs=1e-3))
    # moving due EAST (pa 90): x decreases with time (+x is west), y constant
    xe, ye = inj.position(early["mjd"], PX)
    xl, yl = inj.position(late["mjd"], PX)
    assert xl < xe and yl == pytest.approx(ye)
    assert (xe - xl) * PX == pytest.approx(16.0 * (late["mjd"] - early["mjd"]) / 365.25, rel=1e-6)
    # untouched arrays are shared, not copied; the base frames are not modified
    assert out[early["id"]]["var"] is base[early["id"]]["var"]
    assert float(np.abs(base[early["id"]]["sci"]).sum()) == 0.0


def test_spectrum_shapes():
    assert S.shape_factor("flat", 2.7, 4) == 1.0
    assert S.shape_factor("ydwarf", 1.2, 2) == 0.0 and S.shape_factor("ydwarf", 4.6, 6) == 1.0
    assert S.shape_factor("tdwarf", 1.1, 1) == 1.0 and S.shape_factor("tdwarf", 4.5, 6) == 1.0
    assert S.shape_factor("tdwarf", 1.45, 2) == S.T_ELSEWHERE
    frames, base = synthetic_field(n_per=1, noise=0.0)
    inj = S.Injection(15, 8, 0.0, 0.0, 300.0, "ydwarf", t_ref(frames))
    out = S.inject(frames, base, inj, SIGMA, PX)
    k = ujy_per_mjysr_pixel(PX)
    for f in frames:
        total = float(out[f["id"]]["sci"].sum()) * k
        assert total == pytest.approx(300.0 if f["detector"] in (5, 6) else 0.0, abs=1e-3)


def _run(frames, base, inj):
    store_ = S.inject(frames, base, inj, SIGMA, PX)
    return S.score(S.run_analysis(KEY, GRID, frames, store_), inj)


def test_in_memory_restores_and_writes_nothing():
    frames, base = synthetic_field(n_per=3)
    orig = (analysis.frame_arrays, analysis.save_arrays, analysis.stack_arrays, analysis._fast_movers)
    S.run_analysis(KEY, GRID, frames, base)
    assert (analysis.frame_arrays, analysis.save_arrays, analysis.stack_arrays, analysis._fast_movers) == orig
    assert analysis.frame_arrays is store.frame_arrays
    assert not (settings.cache_dir / "fields" / KEY).exists()


def test_bright_fast_mover_recovered_with_correct_motion():
    frames, base = synthetic_field()
    inj = S.Injection(8.4, 22.3, 8.0, 60.0, 3000.0, "flat", t_ref(frames))
    r = _run(frames, base, inj)
    assert r["matched"] and r["kind"] == "mover" and r["evidence"] in ("strong", "moderate")
    assert r["pmra_rec"] == pytest.approx(r["pmra_inj"], rel=0.25, abs=600)
    assert r["pmdec_rec"] == pytest.approx(r["pmdec_inj"], rel=0.25, abs=600)
    assert r["pmra_inj"] > 0  # pa 60: towards the east


def test_static_injection_is_not_a_mover():
    frames, base = synthetic_field(seed=3)
    for x, y in ((8.2, 9.7), (22.6, 21.1)):
        r = _run(frames, base, S.Injection(x, y, 0.0, 0.0, 3000.0, "flat", t_ref(frames)))
        assert r.get("kind") != "mover"


def test_variable_injection_recovered():
    frames, base = synthetic_field(seed=4)
    inj = S.Injection(9.0, 21.0, 0.0, 0.0, 1500.0, "flat", t_ref(frames), {1: 2.0}, "variable")
    r = _run(frames, base, inj)
    assert r["matched"] and r["kind"] == "variable"


def test_completeness_fit_finds_the_step():
    rng = np.random.default_rng(0)
    fl = np.repeat([10.0, 30.0, 100.0, 300.0, 1000.0], 40)
    p = 1 / (1 + np.exp(-(np.log10(fl) - 2.0) / 0.1))
    y = (rng.uniform(size=fl.size) < p).astype(float)
    c = S.completeness(fl, y, rng, n_boot=50)
    assert c["f50"]["value"] == pytest.approx(100.0, rel=0.3)
    assert c["f90"]["value"] > c["f50"]["value"]
    assert c["f50"]["lo"] <= c["f50"]["value"] <= c["f50"]["hi"]
    none = S.completeness(fl, np.zeros_like(fl), rng, n_boot=5)
    assert S.fmt_level(none["f50"]) == "> 1000"
    assert math.isinf(none["f90"]["value"])
