"""Predict-then-reveal on synthetic seasons; proves the held-out season cannot leak."""

import zlib

import numpy as np
import pytest

from app import fingerprint as fp
from app import holdout as ho
from app.catalogs import CatalogResult
from app.processing import Grid
from tests.test_fingerprint import DEC, RA, scene

PM = (-6000.0, 2000.0)
MOVER = fp.Source("target", "target", RA, DEC, PM[0], PM[1], 61000.0)
SEASONS = {0: 60800.0, 1: 60990.0, 2: 61170.0}
GRID = Grid.for_fov(RA, DEC, 3.0, 6.15)


def frames(seasons=SEASONS, dets=(5, 6), n=3):
    out = []
    for s, t0 in seasons.items():
        for d in dets:
            for i in range(n):
                out.append({"id": f"S{s}D{d}_{i}", "mjd": t0 + 4 * i + d, "season": s, "detector": d,
                            "date": None})
    return out


def loader(source=MOVER, flux=400.0, tamper_season=None, log=None, stage=None):
    def load(meta):
        if log is not None:
            log.append((meta["id"], meta["season"], stage[0] if stage else None))
        seed = zlib.crc32(meta["id"].encode()) % 1000
        if meta["season"] == tamper_season:  # different noise AND a displaced source
            shifted = fp.Source("t", "target", source.ra0 + 0.002, source.dec0 - 0.001, 0, 0, 61000.0)
            return scene([(shifted, flux * 3)], mjd=meta["mjd"], seed=seed + 7)
        return scene([(source, flux)], mjd=meta["mjd"], seed=seed)
    return load


EMPTY = CatalogResult(ok=True, stars=[])
GUESS = fp.Target(RA, DEC, 0.0, 0.0, 61000.0)  # no proper motion supplied


def test_prediction_confirmed_for_linear_mover():
    r = ho.run_holdout(frames(), loader(), EMPTY, GUESS, GRID)
    assert r["testable"] and r["holdout_season"] == 2
    f = r["fit"]
    assert f["pmra_masyr"] == pytest.approx(PM[0], abs=4 * f["sigma_pmra_masyr"] + 300)
    assert f["pmdec_masyr"] == pytest.approx(PM[1], abs=4 * f["sigma_pmdec_masyr"] + 300)
    c = r["comparison"]
    assert c["verdict"] == "prediction confirmed", c["summary"]
    assert c["combined"]["no_motion"]["offset_sigma"] > 3
    assert {m["season"] for m in r["train"]} == {0, 1}
    assert {m["season"] for m in r["reveal"]} == {2}
    for k in ("x", "y", "ellipse_px", "ellipse"):
        assert r["prediction"][k] is not None


def test_heldout_pixels_cannot_change_fit_or_prediction():
    stage = ["?"]
    log: list = []

    def progress(s, i, n):
        stage[0] = s

    clean = ho.run_holdout(frames(), loader(log=log, stage=stage), EMPTY, GUESS, GRID, progress=progress)
    tampered = ho.run_holdout(frames(), loader(tamper_season=2), EMPTY, GUESS, GRID)
    assert clean["fit"] == tampered["fit"]
    assert clean["prediction"] == tampered["prediction"]
    assert clean["train"] == tampered["train"]
    assert clean["registration"] == tampered["registration"]
    assert clean["reveal"] != tampered["reveal"]  # the reveal does see the new pixels
    # held-out cutouts are opened only in the reveal stage, after the prediction exists
    held = [e for e in log if e[1] == 2]
    assert held and all(e[2] == "reveal" for e in held)
    assert all(e[2] != "reveal" for e in log if e[1] != 2)


def test_static_source_matches_both_models():
    static = fp.Source("target", "target", RA, DEC, 0.0, 0.0, 61000.0)
    r = ho.run_holdout(frames(), loader(source=static), EMPTY, GUESS, GRID)
    c = r["comparison"]
    assert c["verdict"] == "prediction confirmed"
    assert c["combined"]["no_motion"]["offset_sigma"] < 3


def test_single_training_season_is_not_testable():
    r = ho.run_holdout(frames({0: 60800.0, 1: 60990.0}), loader(), EMPTY, GUESS, GRID)
    assert r["status"] == "complete" and r["testable"] is False
    assert "one" in r["reason"]
    assert "fit" not in r and r["train"]


def test_fit_motion_recovers_known_line():
    from app import astrometry as am

    ms = []
    for k, t in enumerate([60800.0, 60990.0, 61170.0, 61350.0]):
        ra, dec = MOVER.at(t)
        m = am.GroupMeasurement(group=str(k), season=k, detector=6, status="measured", start_ra=ra,
                                start_dec=dec, search_radius_arcsec=12, ra=ra, dec=dec,
                                cov_fit=np.eye(2) * 0.04, mjd=t)
        ms.append(m)
    fit = ho.fit_motion(ms, RA, DEC)
    assert fit.theta[2] * 1000 == pytest.approx(PM[0], abs=1)
    assert fit.theta[3] * 1000 == pytest.approx(PM[1], abs=1)
    assert fit.chi2 == pytest.approx(0, abs=1e-6)


# ---------------------------------------------------------------------------- parallax
from app import astrometry as am  # noqa: E402

PLX_ARCSEC = 2.5  # exaggerated so the straight-line miss is far outside the synthetic errors


class PlxSource(fp.Source):
    """A source with proper motion AND parallax (geocentric, as the model assumes)."""

    def at(self, t):
        ra, dec = super().at(t)
        fe, fn = am.parallax_factors(ra, dec, t)
        r, d = am.from_tp(PLX_ARCSEC * fe, PLX_ARCSEC * fn, ra, dec)
        return float(r), float(d)


FLUX_PLX = 2000.0
PLX_MOVER = PlxSource("target", "target", RA, DEC, PM[0], PM[1], 61000.0)


def test_parallax_factors_are_annual_and_bounded():
    f = [np.hypot(*am.parallax_factors(RA, DEC, 61000.0 + d)) for d in range(0, 366, 30)]
    assert max(f) <= 1.02 and max(f) > 0.3
    assert am.parallax_factors(RA, DEC, 61000.0) == pytest.approx(am.parallax_factors(RA, DEC, 61365.25), abs=0.02)


def test_fixed_parallax_model_recovers_parallax_mover_where_linear_misses():
    r = ho.run_holdout(frames(), loader(flux=FLUX_PLX, source=PLX_MOVER), EMPTY, GUESS, GRID, plx_mas=PLX_ARCSEC * 1000)
    lin, plx = r["models"]["linear"]["comparison"], r["models"]["parallax"]["comparison"]
    assert lin["combined"]["offset_sigma"] > 3, lin["summary"]
    assert plx["verdict"] == "prediction confirmed", plx["summary"]
    assert r["comparison"]["model"] == "linear+parallax" and r["prediction"]["model"] == "linear+parallax"
    assert r["parallax"]["source"] == "query" and r["parallax"]["used_mas"] == pytest.approx(PLX_ARCSEC * 1000)
    assert r["parallax"]["implied_mas"] == pytest.approx(PLX_ARCSEC * 1000, abs=3 * r["parallax"]["implied_err_mas"] + 100)
    assert not r["parallax"]["free_fit"]["fitted"]  # two training epochs: never fitted freely
    assert len(r["parallax"]["factors"]) == len(r["train"]) + len(r["reveal"])
    # without a parallax value the top level stays the straight line
    r0 = ho.run_holdout(frames(), loader(flux=FLUX_PLX, source=PLX_MOVER), EMPTY, GUESS, GRID)
    assert r0["models"]["parallax"] is None and r0["comparison"]["model"] == "linear"
    assert r0["parallax"]["source"] is None and r0["comparison"]["verdict"] != "prediction confirmed"


def test_heldout_pixels_cannot_change_either_model():
    kw = dict(plx_mas=PLX_ARCSEC * 1000)
    clean = ho.run_holdout(frames(), loader(flux=FLUX_PLX, source=PLX_MOVER), EMPTY, GUESS, GRID, **kw)
    tampered = ho.run_holdout(frames(), loader(flux=FLUX_PLX, source=PLX_MOVER, tamper_season=2), EMPTY, GUESS, GRID, **kw)
    assert clean["fit"] == tampered["fit"] and clean["prediction"] == tampered["prediction"]
    for k in ("linear", "parallax"):
        a, b = clean["models"][k], tampered["models"][k]
        assert {x: v for x, v in a.items() if x != "comparison"} == {x: v for x, v in b.items() if x != "comparison"}
    assert clean["models"]["parallax"]["comparison"] != tampered["models"]["parallax"]["comparison"]


def test_gaia_parallax_is_adopted_only_for_the_target_itself():
    from app.catalogs import Star

    t16 = fp.GAIA_DR3_EPOCH_MJD
    ra16, dec16 = MOVER.at(t16)
    me = Star("self", ra16, dec16, PM[0], PM[1], t16, 15.0, None, True, PLX_ARCSEC * 1000, 5.0)
    other = Star("bg", ra16 + 0.01, dec16, 0.0, 0.0, t16, 15.0, None, True, 300.0, 1.0)
    r = ho.run_holdout(frames(), loader(flux=FLUX_PLX, source=PLX_MOVER), CatalogResult(ok=True, stars=[me, other]), GUESS, GRID)
    assert r["parallax"]["source"] == "gaia" and r["parallax"]["gaia"]["gaia_id"] == "self"
    assert r["parallax"]["used_err_mas"] == pytest.approx(5.0)
    wrong_pm = Star("self", ra16, dec16, -PM[0], PM[1], t16, 15.0, None, True, PLX_ARCSEC * 1000, 5.0)
    r2 = ho.run_holdout(frames(), loader(flux=FLUX_PLX, source=PLX_MOVER), CatalogResult(ok=True, stars=[wrong_pm]), GUESS, GRID)
    assert r2["parallax"]["source"] is None
