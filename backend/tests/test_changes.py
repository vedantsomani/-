"""Change detection on synthetic stacks (no network)."""

import numpy as np
import pytest

from app import changes as ch
from app.bins import build_bins

N = 41
PIX = 6.15


def star_field(rng, sources, sigma=0.01, var_scale=1.0):
    """sources: list of (x, y, amplitude). Returns a stack-like dict."""
    yy, xx = np.mgrid[0:N, 0:N]
    img = rng.normal(0, sigma, (N, N))
    for x, y, amp in sources:
        img += amp * np.exp(-((xx - x) ** 2 + (yy - y) ** 2) / (2 * ch.PSF_SIGMA_PX**2))
    return {
        "sci": img.astype(np.float32),
        "var": np.full((N, N), sigma**2 * var_scale, np.float32),
        "n": np.full((N, N), 10, np.int16),
        "footprint": np.ones((N, N), np.float32),
        "excluded": np.zeros((N, N), bool),
        "wave": np.full((N, N), 4.5, np.float32),
    }


FIELD = [(8, 8, 2.0), (30, 12, 1.0), (12, 30, 0.6), (33, 33, 3.0)]


def test_static_pair_has_unit_noise_and_no_events():
    rng = np.random.default_rng(0)
    a, b = star_field(rng, FIELD), star_field(rng, FIELD)
    z, _ = ch.zmap(ch.diff_maps(a, b))
    good = np.isfinite(z)
    assert np.std(z[good]) == pytest.approx(1.0, rel=0.15)
    assert ch.find_lobes(ch.matched_filter(z)) == []
    assert ch.classify(ch.measure_pair(a, b, 33, 33, PIX))["kind"] == "static"


def test_sub_pixel_mover_is_detected_with_correct_pm_sign():
    rng = np.random.default_rng(1)
    a = star_field(rng, FIELD + [(20.0, 20.0, 0.25)])
    b = star_field(rng, FIELD + [(20.7, 20.1, 0.25)])  # moved +x (WEST) by 0.7 px
    zs = ch.matched_filter(ch.zmap(ch.diff_maps(a, b))[0])
    groups = ch.group_lobes(ch.find_lobes(zs))
    assert len(groups) == 1 and {l["sign"] for l in groups[0]} == {1, -1}, "expected one +/- dipole"
    v = ch.classify(ch.measure_pair(a, b, 20.3, 20.0, PIX))
    assert v["kind"] == "mover"
    pmra, pmdec = ch.pm_from_shift(v["dx_px"], v["dy_px"], 365.25, PIX)
    assert pmra < 0, "moving west means negative pmra"
    assert pmra == pytest.approx(-0.7 * PIX * 1000, rel=0.2)


def test_variable_not_mover():
    rng = np.random.default_rng(2)
    a = star_field(rng, FIELD + [(20, 20, 0.3)])
    b = star_field(rng, FIELD + [(20, 20, 0.6)])  # doubles in brightness, same place
    zs = ch.matched_filter(ch.zmap(ch.diff_maps(a, b))[0])
    groups = ch.group_lobes(ch.find_lobes(zs))
    assert len(groups) == 1 and {l["sign"] for l in groups[0]} == {1}, "variable: single-sign residual"
    v = ch.classify(ch.measure_pair(a, b, 20, 20, PIX))
    assert v["kind"] == "variable"
    assert v["flux_ratio"] == pytest.approx(2.0, rel=0.15)


def test_appeared_needs_significant_change():
    rng = np.random.default_rng(3)
    a = star_field(rng, FIELD)
    b = star_field(rng, FIELD + [(20, 20, 0.4)])
    assert ch.classify(ch.measure_pair(a, b, 20, 20, PIX))["kind"] == "appeared"
    shallow = star_field(rng, FIELD, sigma=0.01, var_scale=400)  # 20x noisier first epoch
    assert ch.classify(ch.measure_pair(shallow, b, 20, 20, PIX))["kind"] == "uncertain"


def test_edges_are_ignored():
    z = np.zeros((N, N))
    z[1, 20] = 50.0
    assert ch.find_lobes(z) == []
    z[20, 20] = 50.0
    assert len(ch.find_lobes(z)) == 1


def test_new_flux_rejects_colour_residual_on_bright_star():
    rng = np.random.default_rng(4)
    ref = star_field(rng, FIELD, sigma=0.002)
    frame = star_field(rng, [(x, y, a * (1.25 if (x, y) == (33, 33) else 1.0)) for x, y, a in FIELD], sigma=0.01)
    assert ch.new_flux_detections(frame, ref) == []
    frame2 = star_field(rng, FIELD + [(20, 15, 0.5)], sigma=0.01)
    dets = ch.new_flux_detections(frame2, ref)
    assert len(dets) == 1 and (dets[0]["x"], dets[0]["y"]) == (20, 15)


def _det(t_min, x, y, obs, det=1, z=8.0):
    return {"t": 60000 + t_min / 1440, "x": x, "y": y, "z": z, "frame_id": f"{obs}_D{det}", "detector": det, "obs_id": obs}


def test_link_three_collinear_detections():
    dets = [_det(0, 10, 10, "v_1"), _det(2, 10.6, 10.2, "v_2"), _det(4, 11.2, 10.4, "v_3"), _det(2, 30, 5, "v_2")]
    tracks = ch.link_visit(dets, PIX)
    assert len(tracks) == 1 and tracks[0]["kind"] == "fast_mover"
    assert tracks[0]["rate"] == pytest.approx(np.hypot(0.3, 0.1) * PIX, rel=0.01)


def test_two_unconfirmed_points_are_not_a_track():
    assert ch.link_visit([_det(0, 10, 10, "v_1"), _det(2, 11, 10, "v_2")], PIX) == []


def test_twin_confirmed_single_epoch_is_transient():
    tracks = ch.link_visit([_det(0, 10, 10, "v_1", det=1), _det(0, 10.2, 10, "v_1", det=4)], PIX)
    assert [t["kind"] for t in tracks] == ["transient"]


def test_bins_have_min_exposures_and_stay_in_detector():
    rng = np.random.default_rng(5)
    from app.bands import BAND_CENTERS_UM

    frames = []
    for det in (3, 6):
        lam = BAND_CENTERS_UM[(det - 1) * 17 : det * 17]
        for season, n in ((0, 4), (1, 20), (2, 18)):
            for i in range(n):
                frames.append({"id": f"{det}_{season}_{i}", "detector": det, "season": season,
                               "wavelength_um": float(rng.uniform(lam[0], lam[-1]))})
    bins, assign = build_bins(frames)
    assert len(assign) == len(frames)
    for b in bins:
        assert b.ch_lo // 17 == b.ch_hi // 17 == b.detector - 1
        assert len(b.seasons) >= 2
        for s in b.seasons:
            assert b.counts[s] >= 3


def _mover_event(pmra, pmdec, sig=10.0, ratio=1.0, dsig=0.0):
    return {"kind": "mover", "pmra_masyr": pmra, "pmdec_masyr": pmdec, "shift_sigma": sig,
            "flux_ratio": ratio, "dflux_sigma": dsig}


def test_coherent_motion_stays_mover():
    evs = [_mover_event(4000, -2400), _mover_event(3600, -2100), _mover_event(4300, -2700)]
    out = ch.reconcile_movers(evs)
    assert out["coherent"] and all(e["kind"] == "mover" for e in evs)


def test_incoherent_shifts_of_a_variable_are_not_motion():
    # RW Aur-like: "shifts" in random directions while the flux changes a lot
    evs = [_mover_event(-5000, -4700, ratio=1.35, dsig=6), _mover_event(9500, -10000, ratio=2.1, dsig=14),
           _mover_event(-23000, 13000, ratio=2.0, dsig=13), _mover_event(11000, -6800, ratio=0.76, dsig=-5.4)]
    out = ch.reconcile_movers(evs)
    assert out["coherent"] is False
    assert {e["kind"] for e in evs} == {"variable"}
    assert all("pmra_masyr" not in e for e in evs)


def test_shift_fit_recovers_motion_of_saturated_star():
    rng = np.random.default_rng(6)
    yy, xx = np.mgrid[0:N, 0:N]

    def bright(x, y, amp):  # real PSFs have broad wings; that's what the fit uses
        return star_field(rng, FIELD + [(x, y, amp)], sigma=0.01), amp * 0.05 * np.exp(-((xx - x) ** 2 + (yy - y) ** 2) / (2 * 2.0**2))

    a, wa = bright(20.0, 20.0, 50.0)
    b, wb = bright(21.0, 20.4, 40.0)  # moved +1 px W, +0.4 N, 20% fainter
    a["sci"] = (a["sci"] + wa).astype(np.float32)
    b["sci"] = (b["sci"] + wb).astype(np.float32)
    for s, (x, y) in ((a, (20, 20)), (b, (21, 20))):  # saturated core: masked & interpolated
        s["n"][y - 1 : y + 2, x - 1 : x + 2] = 0
        s["var"][y - 1 : y + 2, x - 1 : x + 2] = np.nan
    m = ch.measure_pair(a, b, 20.5, 20.2, PIX)
    assert "saturated" in m["aperture"] and m["fit"] is not None
    v = ch.classify(m)
    assert v["kind"] == "mover" and v["method"].startswith("shift fit")
    assert v["dx_px"] == pytest.approx(1.0, abs=0.1) and v["dy_px"] == pytest.approx(0.4, abs=0.1)
    assert v["flux_ratio"] == pytest.approx(0.8, abs=0.05)


def test_wavelength_balanced_pair_ignores_changed_wavelength_mixture():
    # A red source: 1.0 in the blue bin, 2.0 in the red bin, constant in time. Season A has
    # a deep blue bin and a shallow red bin; season B the opposite. Broad per-season stacks
    # (inverse-variance weighted) would see it "brighten"; the balanced pair must not.
    rng = np.random.default_rng(7)
    src = lambda amp, vs: star_field(rng, [(20, 20, amp)], sigma=0.002, var_scale=vs)  # noqa: E731
    As = [src(1.0, 1.0), src(2.0, 25.0)]
    Bs = [src(1.0, 25.0), src(2.0, 1.0)]
    A, B, w = ch.wavelength_balanced(As, Bs)
    assert w.sum() == pytest.approx(1.0)
    assert B["sci"][20, 20] / A["sci"][20, 20] == pytest.approx(1.0, abs=0.03)

    def broad(parts):  # what a whole-detector stack does
        wt = [1 / float(np.median(p["var"])) for p in parts]
        return sum(wi * p["sci"][20, 20] for wi, p in zip(wt, parts)) / sum(wt)

    assert broad(Bs) / broad(As) > 1.5, "the broad stacks really are biased in this setup"


def test_wavelength_balanced_marks_pixels_missing_in_any_bin():
    rng = np.random.default_rng(8)
    As = [star_field(rng, []), star_field(rng, [])]
    Bs = [star_field(rng, []), star_field(rng, [])]
    As[1]["n"][5, 5] = 0
    A, B, _ = ch.wavelength_balanced(As, Bs)
    assert A["n"][5, 5] == 0 and B["n"][5, 5] > 0


def test_one_outlying_pair_does_not_veto_coherent_motion():
    evs = [_mover_event(4000, -2400), _mover_event(3600, -2100), _mover_event(4300, -2700),
           _mover_event(3900, -2500), _mover_event(-9000, 7000)]
    out = ch.reconcile_movers(evs)
    assert out["coherent"] and out["n_dropped"] == 1
    assert [e["kind"] == "mover" for e in evs] == [True, True, True, True, False]
    assert evs[4]["reason"] == ch.OUTLYING_PAIR and "pmra_masyr" not in evs[4]


def test_no_coherent_majority_still_demotes_everything():
    evs = [_mover_event(4000, -2400), _mover_event(3600, -2100),
           _mover_event(-9000, 7000), _mover_event(-200, 8000)]
    out = ch.reconcile_movers(evs)
    assert out["coherent"] is False and "n_dropped" not in out
    assert all(e["kind"] != "mover" for e in evs)
