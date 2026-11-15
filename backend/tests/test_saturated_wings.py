"""Saturated stars: extended-PSF wing fits on synthetic data (no network)."""

from __future__ import annotations

import numpy as np
import pytest

from app import classify as C
from app import fingerprint as fp
from app import psf
from tests.test_classify import WL, dip, planck_nu, spectrum
from tests.test_fingerprint import BITS, RA, DEC, make_wcs, meta
from tests.test_psf import library

N = 61


@pytest.fixture(autouse=True)
def wings_on(monkeypatch):
    monkeypatch.setattr(fp.settings, "saturated_wings", True)
STAR = fp.Source("target", "target", RA, DEC, 0.0, 0.0, 61000.0)


def ext_profile(ratio=1.0, sys=0.05) -> psf.ExtendedProfile:
    r = psf.EXT_R_MIN + psf.EXT_DR * (np.arange(int((psf.EXT_R_MAX - psf.EXT_R_MIN) / psf.EXT_DR)) + 0.5)
    w = 2e-3 * (r / 2.0) ** -2.5          # a power-law halo: ~10% of the flux beyond the ePSF
    return psf.ExtendedProfile(detector=3, r=r, w=w, w_err=0.05 * w, n_star=100,
                               cal_r=np.array([2.0, 6.0]), cal_ratio=np.array([ratio, ratio]),
                               cal_sys=np.array([sys, sys]))


def saturated_scene(flux_ujy: float, r_mask: float, ext: psf.ExtendedProfile, seed=0, noise=1e-3,
                    true_ext: psf.ExtendedProfile | None = None):
    w = make_wcs(N)
    lib = library()
    k = fp.pixel_sr(w, N / 2, N / 2) * 1e12
    yy, xx = np.mgrid[0:N, 0:N]
    x, y = (float(v) for v in w.world_to_pixel_values(*STAR.at(61000.0)))
    sci = 0.02 + flux_ujy / k * psf.render_extended(lib.at(x, y), true_ext or ext, x, y, xx, yy, 5)
    sci += np.random.default_rng(seed).normal(0, noise, (N, N))
    rr = np.hypot(xx - x, yy - y)
    flags = np.zeros((N, N), np.int64)
    flags[rr <= r_mask] = BITS["OVERFLOW"]
    sci[rr <= r_mask] = 1e6
    usable = flags == 0
    cut = fp.NativeCutout(sci=sci, var=np.full((N, N), noise**2), flags=flags, bits=BITS, usable=usable,
                          wcs=w, wcs_spec=None, active_off=(0.0, 0.0))
    return cut, lib


@pytest.mark.parametrize("r_mask", [2.5, 4.0, 4.8])
def test_saturated_star_recovered_from_wings_within_systematic(r_mask):
    ext = ext_profile(sys=0.05)
    flux = 3e6
    pulls = []
    for seed in range(5):
        cut, lib = saturated_scene(flux, r_mask, ext, seed=seed)
        row = fp.measure_frame(cut, lib, [STAR], meta(seed, det=3), ext)
        assert fp.WING_FLAG in row["flags"] and "SATURATED_CORE" in row["flags"]
        assert row["quality"] == "suspect"
        wf = row["wing_fit"]
        assert wf["r_in_px"] > r_mask                # masked blob + non-linear rim excluded
        assert row["flux_err_ujy"] >= 0.05 * row["flux_ujy"] * 0.99   # error carries the systematic
        pulls.append((row["flux_ujy"] - flux) / row["flux_err_ujy"])
    assert np.all(np.abs(pulls) < 2.0)


def test_calibration_ratio_is_applied_and_halo_mismatch_is_within_quoted_error():
    flux = 2e6
    # the true halo is 15% brighter than the profile: a calibration ratio of 1.15 undoes it
    true = ext_profile()
    true.w = true.w * 1.15
    ext = ext_profile(ratio=1.15, sys=0.08)
    cut, lib = saturated_scene(flux, 4.5, ext, true_ext=true)
    row = fp.measure_frame(cut, lib, [STAR], meta(det=3), ext)
    assert row["wing_fit"]["ratio"] == pytest.approx(1.15)
    assert abs(row["flux_ujy"] - flux) < 2 * row["flux_err_ujy"]
    assert row["wing_fit"]["raw_flux_ujy"] > row["flux_ujy"]


def test_unsaturated_path_unchanged_by_extended_profile():
    from tests.test_fingerprint import TARGET, scene

    cut, lib = scene([(TARGET, 800.0)], seed=3)
    a = fp.measure_frame(cut, lib, [TARGET], meta())
    b = fp.measure_frame(cut, lib, [TARGET], meta(), ext_profile())
    assert a == b
    assert fp.WING_FLAG not in b["flags"]


def test_feature_switch_off_keeps_old_behaviour(monkeypatch):
    ext = ext_profile()
    cut, lib = saturated_scene(3e6, 4.0, ext)
    monkeypatch.setattr(fp.settings, "saturated_wings", False)
    row = fp.measure_frame(cut, lib, [STAR], meta(det=3), ext)
    assert fp.WING_FLAG not in row["flags"]


def test_spectrum_and_light_curve_mark_wing_points():
    pts = []
    for i, (ch, s) in enumerate([(40, 0), (40, 0), (40, 1), (40, 1), (41, 0), (41, 1)]):
        pts.append({"frame_id": f"f{i}", "mjd": 61000.0 + 180 * s, "season": s, "detector": 3,
                    "channel": ch, "wavelength_um": 2.0 + 0.01 * (ch - 40), "flux_ujy": 1e6 * (1 + 0.8 * s),
                    "flux_err_ujy": 5e4, "aperture_flux_ujy": None, "aperture_err_ujy": None,
                    "quality": "suspect", "flags": ["SATURATED_CORE", "WINGS_ONLY", fp.WING_FLAG],
                    "neighbours": {}})
    sp = fp.build_spectrum(pts)
    assert all(r["wing_fit"] and r["n_wing"] == r["n"] for r in sp)
    lc = fp.build_light_curves(pts)
    assert lc and all(b["wing_fit"] for b in lc)
    assert all(b["sys_frac"] == fp.LC_SYS_FRAC_WINGS for b in lc)
    assert any(b["variable"] for b in lc)   # 80% change >> 10% systematic + errors


def test_classify_accepts_wing_spectra_with_capped_confidence():
    f = planck_nu(WL, 3200) * dip(WL, 1.4, 0.05, 0.3) * dip(WL, 1.88, 0.07, 0.3)
    sp = spectrum(1e6 * f / f.max(), snr=100)
    for r in sp:
        r["quality"], r["wing_fit"] = "suspect", True
    pts = [{"channel": i % 102, "quality": "suspect",
            "flags": ["SATURATED_CORE", "WINGS_ONLY", C.WING_FLAG], "wing_fit": {"sys_frac": 0.07}}
           for i in range(204)]
    res = C.classify(sp, points=pts)
    assert res["label"] == C.LABEL_M
    assert res["confidence"] == "medium"
    assert any("wings" in c for c in res["caveats"])
    for p in pts:
        p["wing_fit"]["sys_frac"] = 0.2
    assert C.classify(sp, points=pts)["confidence"] == "low"
    # too few channels: still refused
    assert C.classify(sp[:30], points=pts)["label"] == C.LABEL_NONE
