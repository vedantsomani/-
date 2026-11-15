"""Fingerprint orchestration on synthetic exposures (no network, no cache)."""

import json

import numpy as np
import pytest
from astropy.wcs import WCS

from app import fingerprint as fp
from app.bands import BAND_CENTERS_UM
from app.catalogs import CatalogResult, Star
from tests.test_psf import gaussian_epsf, library

RA, DEC = 133.76, -7.24
BITS = {"OVERFLOW": 1, "OUTLIER": 2, "SOURCE": 4, "HALO": 8}


def make_wcs(n=41):
    w = WCS(naxis=2)
    w.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    w.wcs.crval = [RA, DEC]
    w.wcs.crpix = [(n + 1) / 2, (n + 1) / 2]
    s = 6.15 / 3600
    w.wcs.cdelt = [-s, s]
    return w


def scene(sources_ujy: list[tuple[fp.Source, float]], mjd=61000.0, seed=0, noise=1e-4, n=41,
          masked_core=False):
    """Native cutout with the given sources (flux in uJy) rendered with the test ePSF."""
    w = make_wcs(n)
    lib = library()
    k_ujy = fp.pixel_sr(w, n / 2, n / 2) * 1e12
    yy, xx = np.mgrid[0:n, 0:n]
    sci = np.full((n, n), 0.01)
    from app import psf

    for s, f in sources_ujy:
        x, y = (float(v) for v in w.world_to_pixel_values(*s.at(mjd)))
        sci += f / k_ujy * psf.render(lib.at(x, y), x, y, xx, yy, 5)
    rng = np.random.default_rng(seed)
    sci += rng.normal(0, noise, (n, n))
    flags = np.zeros((n, n), np.int64)
    if masked_core:
        x, y = (float(v) for v in w.world_to_pixel_values(*sources_ujy[0][0].at(mjd)))
        core = np.hypot(xx - x, yy - y) <= 1.0
        flags[core] = BITS["OVERFLOW"]
        sci[core] = 1e6
    var = np.full((n, n), noise**2)
    usable = (flags & BITS["OVERFLOW"]) == 0
    cut = fp.NativeCutout(sci=sci, var=var, flags=flags, bits=BITS, usable=usable, wcs=w,
                          wcs_spec=None, active_off=(0.0, 0.0))
    return cut, lib


def meta(i=0, mjd=61000.0, season=0, det=6):
    return {"id": f"f{i}", "mjd": mjd, "date": None, "season": season, "detector": det}


TARGET = fp.Source("target", "target", RA, DEC, -8000.0, 600.0, 61000.0)
NEIGH = fp.Source("n1", "neighbour", RA - 1.2 * 6.15 / 3600 / np.cos(np.radians(DEC)), DEC + 0.3 * 6.15 / 3600,
                  0.0, 0.0, 61000.0, gaia_id="1", g_mag=16.0, sep_arcsec=7.6)


def test_measure_frame_deblends_where_aperture_is_contaminated():
    flux_t, flux_n = 50.0, 1200.0
    pulls, ap = [], []
    for seed in range(25):
        cut, lib = scene([(TARGET, flux_t), (NEIGH, flux_n)], seed=seed)
        row = fp.measure_frame(cut, lib, [TARGET, NEIGH], meta(seed))
        assert row["quality"] == "good"
        pulls.append((row["flux_ujy"] - flux_t) / row["flux_err_ujy"])
        ap.append(row["aperture_flux_ujy"])
        assert abs(row["neighbours"]["n1"]["flux_ujy"] - flux_n) < 5 * row["neighbours"]["n1"]["flux_err_ujy"]
    assert abs(np.mean(pulls)) < 0.6
    assert np.mean(ap) > 3 * flux_t  # naive aperture swallows the neighbour


def test_measure_frame_without_neighbours_matches_aperture():
    cut, lib = scene([(TARGET, 800.0)], seed=3)
    row = fp.measure_frame(cut, lib, [TARGET], meta())
    assert row["neighbours"] == {}
    assert row["flux_ujy"] == pytest.approx(800, rel=0.03)
    assert row["aperture_flux_ujy"] == pytest.approx(800, rel=0.05)
    assert row["shift_px"] is not None  # bright enough to recentre
    assert abs(row["shift_px"][0]) < 0.05 and abs(row["shift_px"][1]) < 0.05


def test_measure_frame_masked_core_is_flagged_suspect():
    cut, lib = scene([(TARGET, 5000.0)], seed=4, masked_core=True)
    row = fp.measure_frame(cut, lib, [TARGET], meta())
    assert "SATURATED_CORE" in row["flags"] and "WINGS_ONLY" in row["flags"]
    assert row["quality"] == "suspect"
    assert row["flux_ujy"] == pytest.approx(5000, rel=0.1)
    assert row["aperture_flux_ujy"] is None  # aperture mostly masked


def test_measure_frame_off_image_is_bad():
    far = fp.Source("target", "target", RA + 1.0, DEC, 0.0, 0.0, 61000.0)
    cut, lib = scene([(TARGET, 100.0)])
    row = fp.measure_frame(cut, lib, [far], meta())
    assert row["quality"] == "bad" and row["flags"] == ["OFF_IMAGE"] and row["flux_ujy"] is None


def test_combine_rejects_outliers_and_scales_error():
    f = [10.0, 10.5, 9.5, 10.2, 60.0]
    e = [0.5] * 5
    m, err, n = fp.combine(f, e)
    assert n == 4 and m == pytest.approx(10.05, abs=0.01)
    assert fp.combine([], []) is None
    m1, e1, n1 = fp.combine([5.0], [2.0])
    assert (m1, e1, n1) == (5.0, 2.0, 1)


def _pt(ch, flux, err, season=0, q="good", mjd=61000.0, det=None, i=[0]):
    i[0] += 1
    return {"frame_id": f"p{i[0]}", "mjd": mjd, "season": season, "detector": det or ch // 17 + 1,
            "channel": ch, "wavelength_um": float(BAND_CENTERS_UM[ch]), "flux_ujy": flux,
            "flux_err_ujy": err, "aperture_flux_ujy": flux * 1.5, "aperture_err_ujy": err,
            "quality": q, "neighbours": {"n1": {"flux_ujy": 2 * flux, "flux_err_ujy": err}}}


def test_spectrum_binning_never_invents_channels():
    pts = [_pt(90, 100.0, 10.0, 0), _pt(90, 110.0, 10.0, 1), _pt(91, 50.0, 5.0, 0),
           _pt(92, 70.0, 5.0, 0, q="bad"), _pt(93, 80.0, 5.0, 0, q="suspect")]
    spec = fp.build_spectrum(pts)
    chans = [s["channel"] for s in spec]
    assert chans == [90, 91, 93]  # 92 has only a bad point; nothing else is filled in
    s90 = spec[0]
    assert s90["n"] == 2 and s90["flux_ujy"] == pytest.approx(105.0)
    assert set(s90["seasons"]) == {"0", "1"} and s90["seasons"]["1"]["flux_ujy"] == pytest.approx(110.0)
    assert s90["aperture_flux_ujy"] == pytest.approx(157.5)
    assert spec[2]["quality"] == "suspect"
    nspec = fp.build_neighbour_spectra(pts, [TARGET, NEIGH])
    assert [r["channel"] for r in nspec["n1"]] == [90, 91, 93]


def test_light_curve_matched_channels_ignore_spectral_slope():
    # Steep but constant spectrum; season 0 samples the blue end, season 1 both ends.
    flux = {ch: 100.0 * 2 ** (ch - 85) for ch in range(85, 90)}
    rng = np.random.default_rng(0)
    pts = []
    for s, chans in [(0, [85, 85, 86, 86, 87]), (1, [85, 86, 88, 89, 89]), (2, [85, 86, 87, 88, 89])]:
        for ch in chans:
            pts.append(_pt(ch, flux[ch] * (1 + 0.01 * rng.normal()), 0.01 * flux[ch], s, mjd=60800 + 180 * s))
    lcs = fp.build_light_curves(pts)
    assert lcs and all(lc["matched"] for lc in lcs)
    assert not any(lc["variable"] for lc in lcs)
    # Now make season 2 30% brighter: must be flagged.
    for p in pts:
        if p["season"] == 2:
            p["flux_ujy"] *= 1.3
    assert any(lc["variable"] for lc in fp.build_light_curves(pts))


def test_select_sources_paths():
    mjds = [60800.0, 61200.0]
    tgt = fp.Target(RA, DEC, -8000.0, 600.0, 61000.0)
    # failed catalogue: target only, honest note
    sel = fp.select_sources(tgt, CatalogResult(ok=False, note="down"), mjds)
    assert [s.id for s in sel.sources] == ["target"] and not sel.blend_warning
    assert "unavailable" in sel.blend_note
    # empty catalogue
    sel = fp.select_sources(tgt, CatalogResult(ok=True), mjds)
    assert len(sel.sources) == 1 and "No Gaia source" in sel.blend_note

    def star(sid, dra_arcsec, ddec_arcsec, g, pm=(0.0, 0.0), epoch=fp.GAIA_DR3_EPOCH_MJD):
        return Star(sid, RA + dra_arcsec / 3600 / np.cos(np.radians(DEC)), DEC + ddec_arcsec / 3600,
                    pm[0], pm[1], epoch, g, None)

    ra16, dec16 = tgt.at(fp.GAIA_DR3_EPOCH_MJD)
    me = Star("self", ra16, dec16, -8000.0, 600.0, fp.GAIA_DR3_EPOCH_MJD, 15.0, None)
    stars = [me, star("close", 5, 0, 16), star("far", 0, 25, 14)] + [star(f"x{i}", -20, i, 18) for i in range(8)]
    sel = fp.select_sources(tgt, CatalogResult(ok=True, stars=stars), mjds)
    ids = [s.gaia_id for s in sel.sources]
    assert sel.sources[0].gaia_id == "self" and "self" not in ids[1:]
    assert "close" in ids and sel.blend_warning
    assert len(sel.sources) == 1 + fp.settings.max_neighbours
    assert any("not fitted (cap" in n for n in sel.notes)


def test_assemble_is_strict_json():
    cut, lib = scene([(TARGET, 300.0), (NEIGH, 900.0)])
    pts = [fp.measure_frame(cut, lib, [TARGET, NEIGH], meta())]
    pts[0]["chi2_red"] = float("nan")  # must be sanitised to null
    sel = fp.Selection(sources=[TARGET, NEIGH], blend_warning=True, blend_note="x")
    out = fp.assemble("key", fp.Target(RA, DEC), sel, CatalogResult(ok=True), pts, [])
    text = json.dumps(out, allow_nan=False)
    assert '"status": "complete"' in text
    assert out["points"][0]["chi2_red"] is None
    assert {"spectrum", "neighbour_spectra", "light_curves", "method", "units"} <= set(out)


@pytest.mark.network
def test_gaia_cone_live(tmp_path, monkeypatch):
    from app.catalogs import gaia_cone

    monkeypatch.setattr(fp.settings, "cache_dir", tmp_path)
    res = gaia_cone(133.7582, -7.2412, 10 / 3600)  # the static star next to WISE 0855
    assert res.ok and any(s.source_id == "5756956644387831296" for s in res.stars)
