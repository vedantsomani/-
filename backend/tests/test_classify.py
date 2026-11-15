from __future__ import annotations

import math

import numpy as np
import pytest

from app import classify as C
from app.bands import BAND_CENTERS_UM


def planck_nu(wl_um: np.ndarray, t_k: float) -> np.ndarray:
    """Blackbody F_nu shape (arbitrary scale): analytic reference, not a template."""
    x = 14387.77 / (wl_um * t_k)
    return wl_um ** -3 / np.expm1(x)


def spectrum(flux: np.ndarray, snr: float = 50.0, floor_err: float = 0.0) -> list[dict]:
    flux = np.asarray(flux, float)
    err = np.abs(flux) / snr + floor_err
    return [{"channel": i, "wavelength_um": float(w), "flux_ujy": float(f), "flux_err_ujy": float(e),
             "quality": "good"} for i, (w, f, e) in enumerate(zip(BAND_CENTERS_UM, flux, err))]


def dip(wl: np.ndarray, centre: float, width: float, depth: float) -> np.ndarray:
    return 1 - depth * np.exp(-0.5 * ((wl - centre) / width) ** 2)


WL = np.asarray(BAND_CENTERS_UM, float)


@pytest.mark.parametrize("t_k", [5000, 5800, 10000])
def test_blackbody_star_is_star_with_flat_indices(t_k):
    f = planck_nu(WL, t_k)
    res = C.classify(spectrum(1e4 * f / f.max(), snr=100))
    assert res["label"] == C.LABEL_STAR
    for k, ix in res["indices"].items():
        if ix["kind"] == "absorption":
            assert abs(1 - ix["value"]) < 0.06, (k, ix)
    assert res["confidence_note"] == "rule-based, not a probability"


def test_methane_and_water_give_t_dwarf():
    f = planck_nu(WL, 1200) * dip(WL, 3.3, 0.12, 0.7) * dip(WL, 1.4, 0.05, 0.7) \
        * dip(WL, 1.88, 0.07, 0.6) * dip(WL, 2.7, 0.12, 0.6)
    res = C.classify(spectrum(1e4 * f / f.max(), snr=40))
    assert res["label"] == C.LABEL_T
    assert res["confidence"] == "high"
    ix = res["indices"]["CH4_3.3"]
    assert ix["value"] < 0.6 and ix["sigma"] >= 5
    assert any("3.3 um CH4" in r for r in res["reasons"])


def test_water_without_methane_is_m_dwarf():
    f = planck_nu(WL, 3200) * dip(WL, 1.4, 0.05, 0.3) * dip(WL, 1.88, 0.07, 0.3)
    res = C.classify(spectrum(1e4 * f / f.max(), snr=100))
    assert res["label"] == C.LABEL_M


def test_flux_only_at_4_5_um_is_y_dwarf():
    flux = np.where(WL > 4.0, 500.0, 0.0)
    rng = np.random.default_rng(1)
    sp = spectrum(flux, floor_err=25.0)
    for r in sp:
        r["flux_ujy"] += float(rng.normal(0, 25.0))
    res = C.classify(sp, target_pm_masyr=8000.0)
    assert res["label"] == C.LABEL_Y
    assert any("Proper motion" in r for r in res["reasons"])


def test_too_faint_is_not_classifiable():
    rng = np.random.default_rng(2)
    sp = spectrum(np.zeros(len(WL)), floor_err=30.0)
    for r in sp:
        r["flux_ujy"] = float(rng.normal(0, 30.0))
    res = C.classify(sp)
    assert res["label"] == C.LABEL_NONE
    assert "faint" in res["reasons"][0].lower()


def test_saturated_points_refuse():
    sp = spectrum(1e5 * planck_nu(WL, 3000) / planck_nu(WL, 3000).max())
    pts = [{"channel": i % 102, "quality": "suspect", "flags": ["SATURATED_CORE", "WINGS_ONLY"]} for i in range(200)]
    pts += [{"channel": 0, "quality": "good", "flags": []} for _ in range(20)]
    res = C.classify(sp, points=pts)
    assert res["label"] == C.LABEL_NONE
    assert any("Saturated" in c for c in res["caveats"])


def test_few_channels_refuse():
    sp = spectrum(np.full(len(WL), 1000.0))[:8]
    res = C.classify(sp, points=[{"channel": 0, "quality": "bad", "flags": []}] * 50)
    assert res["label"] == C.LABEL_NONE
    assert "8 of 102" in res["reasons"][0]


def test_blend_floor_dominated_target_refused():
    # target at 1% of a close neighbour everywhere: detected statistically, but below the 2% floor
    nb = 1e5 * planck_nu(WL, 5800) / planck_nu(WL, 5800).max()
    tgt = spectrum(0.01 * nb, snr=200)
    nspec = {"n1": spectrum(nb)}
    sources = [{"id": "target", "role": "target", "sep_arcsec": 0.0},
               {"id": "n1", "role": "neighbour", "sep_arcsec": 4.4}]
    res = C.classify(tgt, neighbour_spectra=nspec, sources=sources)
    assert res["label"] == C.LABEL_NONE
    assert "blend floor" in res["reasons"][0]
    # a distant neighbour adds no floor
    sources[1]["sep_arcsec"] = 40.0
    assert C.classify(tgt, neighbour_spectra=nspec, sources=sources)["label"] == C.LABEL_STAR


def test_index_errors_propagate_and_missing_window_is_null():
    f = np.full(len(WL), 1000.0)
    ix = C.compute_index(C.INDEX_DEFS[0], C._rows(spectrum(f, snr=20)))
    assert ix["value"] == pytest.approx(1.0, abs=1e-6)
    assert 0 < ix["err"] < 0.1
    rows = [r for r in C._rows(spectrum(f)) if not 3.15 <= r[0] <= 3.55]
    ix = C.compute_index(C.INDEX_DEFS[0], rows)
    assert ix["value"] is None and ix["note"]
    assert math.isfinite(C.classify([])["indices"]["CH4_3.3"]["band_um"][0])
