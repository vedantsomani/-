"""Validation report helpers (offline, pure functions only)."""

import math

import pytest

from app import report as rp
from app.catalogs import Star


def test_compare_pm_percentage_and_angle():
    c = rp.compare_pm((-7661, 1192), (-8123.7, 673.2))
    assert c["total_meas"] == pytest.approx(7753, abs=1)
    assert c["pct_diff"] == pytest.approx(-4.9, abs=0.1)
    assert c["angle_deg"] == pytest.approx(4.1, abs=0.1)
    # identical vectors, opposite vectors, zero vector
    assert rp.compare_pm((3, 4), (3, 4))["angle_deg"] == pytest.approx(0, abs=1e-6)
    assert rp.angle_between_deg((1, 0), (-1, 0)) == pytest.approx(180)
    assert rp.angle_between_deg((0, 0), (1, 0)) is None
    assert rp.compare_pm((1, 0), (0, 0))["pct_diff"] is None


def test_classify_gaia_rules():
    meas = (-800.0, 10000.0)
    star = lambda **kw: {"pmra": -800.0, "pmdec": 10300.0, "has_pm": True, "variable": None, **kw}
    assert rp.classify_gaia(meas, None) == rp.CLS_NONE
    assert rp.classify_gaia(meas, star()) == rp.CLS_HIGH_PM
    assert rp.classify_gaia(meas, star(pmdec=21000.0)) == rp.CLS_PM_MISMATCH       # speed off by more than 2x
    assert rp.classify_gaia(meas, star(pmra=9000.0, pmdec=0.0)) == rp.CLS_PM_MISMATCH  # direction off by about 85 deg
    assert rp.classify_gaia(meas, star(pmra=-5.0, pmdec=3.0, variable=True)) == rp.CLS_VARIABLE
    assert rp.classify_gaia(None, star(pmra=2.0, pmdec=1.0)) == rp.CLS_STATIC
    assert rp.classify_gaia(None, star(variable=True)) == rp.CLS_VARIABLE
    assert rp.classify_gaia(None, star(has_pm=False)) == rp.CLS_STATIC
    # a consistent catalogue motion wins over a variable flag
    assert rp.classify_gaia(meas, star(variable=True)) == rp.CLS_HIGH_PM
    # a slow Gaia star cannot be called mismatched: it is simply non-moving
    assert rp.classify_gaia(meas, star(pmra=-5.0, pmdec=3.0)) == rp.CLS_STATIC


def test_nearest_star_moves_catalogue_positions():
    # Gaia epoch 2016.0 (MJD 57388.5); 10 arcsec/yr north: at MJD 61000 (~9.9 yr) it has moved ~98 arcsec
    s = Star("1", 10.0, 0.0, 0.0, 10000.0, 57388.5, 12.0, None)
    moved_dec = 10000.0 * (rp.SPHEREX_REF_MJD - 57388.5) / 365.25 / 3.6e6
    hit = rp.nearest_star(10.0, moved_dec + 1.0 / 3600, [s])
    assert hit is not None and hit[1] == pytest.approx(1.0, abs=0.05)
    assert rp.nearest_star(10.0, 0.0, [s]) is None            # the 2016 position is 98" away now
    assert rp.nearest_star(10.0, 0.0, []) is None


def test_md_table_is_valid_github_markdown():
    t = rp.md_table(["A", "B|x"], [[1, None], ["p|q", "line\nbreak"]], ["l", "r"])
    lines = t.splitlines()
    assert lines[1] == "| :--- | ---: |"
    assert lines[0] == "| A | B\|x |"
    assert lines[2] == "| 1 | - |"
    assert lines[3] == "| p\\|q | line break |"
    with pytest.raises(ValueError):
        rp.md_table(["A", "B"], [[1]])
    with pytest.raises(ValueError):
        rp.md_table(["A"], [], ["l", "r"])


def test_md_table_column_counts_match():
    t = rp.md_table(["a", "b", "c"], [[1, 2, 3], [4, 5, 6]])
    counts = {ln.count("|") for ln in t.splitlines()}
    assert counts == {4}


def test_merge_intervals_and_formatting():
    assert rp.merge_intervals([(2, 3), (0.5, 1), (0.9, 1.5), (3, 4)]) == [(0.5, 1.5), (2, 4)]
    assert rp.fmt_intervals([(0.744, 1.1155)]) == "0.74-1.12 µm"
    assert rp.fmt_intervals([]) == "-"
    assert rp.fnum(None) == "-" and rp.fnum(float("nan")) == "-"
    assert rp.fnum(1234.6, 0, comma=True) == "1,235"
    assert rp.fnum(2.0, 1, plus=True) == "+2.0"


def test_weighted_mean_and_band_depth():
    m = rp.wmean([10.0, 20.0, None], [1.0, 1.0, 1.0])
    assert m[0] == pytest.approx(15.0) and m[1] == pytest.approx(1 / math.sqrt(2)) and m[2] == 2
    assert rp.wmean([None], [1.0]) is None and rp.wmean([1.0], [0.0]) is None

    def spec(f):
        return [{"wavelength_um": w, "flux_ujy": f(w), "flux_err_ujy": 1.0}
                for w in (2.92, 3.0, 3.15, 3.25, 3.4, 3.85, 3.95, 4.05)]
    flat = rp.band_depth(spec(lambda w: 100.0))
    assert flat["ratio"] == pytest.approx(1.0)
    dip = rp.band_depth(spec(lambda w: 50.0 if 3.1 <= w < 3.6 else 100.0))
    assert dip["ratio"] == pytest.approx(0.5) and dip["err"] > 0
    assert rp.band_depth(spec(lambda w: 100.0)[:3]) is None   # windows without enough channels


def test_lc_judgement_never_overclaims():
    pts = [{"season": 0, "n": 3}, {"season": 1, "n": 3}]
    base = {"points": pts, "matched": True, "chi2_const": 1.0, "dof": 1, "variable": False, "quality": "good"}
    assert rp.lc_judgement(base)[0] == "constant within errors"
    assert rp.lc_judgement({**base, "variable": True})[0] == "variable"
    assert rp.lc_judgement({**base, "quality": "suspect"})[0] == "inconclusive"
    assert rp.lc_judgement({**base, "quality": "suspect", "variable": True})[0] == "variable (indicative)"
    assert rp.lc_judgement({**base, "chi2_const": None})[0] == "inconclusive"
    assert rp.lc_judgement({**base, "points": [{"season": 0, "n": 1}, {"season": 1, "n": 3}]})[0] == "inconclusive"


def test_lc_outliers_flags_isolated_bin():
    def lc(i, f):
        return {"bin": f"d6b{i}", "detector": 6, "lam_lo_um": 4.4 + 0.1 * i, "lam_hi_um": 4.5 + 0.1 * i,
                "points": [{"season": 0, "flux_ujy": f}]}
    out = rp.lc_outliers([lc(0, 100.0), lc(1, 10.0), lc(2, 90.0)])
    assert [o["bin"] for o in out] == ["d6b1"]
    assert rp.lc_outliers([lc(0, 100.0), lc(1, 95.0), lc(2, 90.0)]) == []


def test_lc_vs_rows_flags_opposite_directions():
    rows = [{"detector": 4, "season_a": 0, "season_b": 2, "flux_ratio": 0.7}]
    lcs = [{"detector": 4, "points": [{"season": 0, "flux_ujy": 100.0}, {"season": 2, "flux_ujy": 150.0}]},
           {"detector": 4, "points": [{"season": 0, "flux_ujy": 100.0}, {"season": 2, "flux_ujy": 160.0}]}]
    assert rp.lc_vs_rows(rows, lcs)[0]["verdict"] == "DISAGREE"
    assert rp.lc_vs_rows(rows, lcs[:1])[0]["verdict"] == "too few bins"
    lcs2 = [{"detector": 4, "points": [{"season": 0, "flux_ujy": 100.0}, {"season": 2, "flux_ujy": 60.0}]}] * 2
    assert rp.lc_vs_rows(rows, lcs2)[0]["verdict"] == "agree"


def test_svg_plot_and_ticks():
    t = rp.nice_ticks(0, 65000)
    assert t[0] == 0 and t[-1] <= 65000 and len(t) >= 3
    svg = rp.svg_plot([{"label": "a <b>", "x": [1, 2], "y": [1, 2], "err": [0.1, 0.1], "color": "#000", "kind": "points"}],
                      "t", (0, 3), (0, 3))
    assert svg.startswith("<svg") and svg.rstrip().endswith("</svg>") and "&lt;b&gt;" in svg


def test_variable_summary():
    rows = [{"kind": "variable", "flux_ratio": 0.5, "detector": 5, "lam_lo_um": 3.8, "lam_hi_um": 4.4},
            {"kind": "variable", "flux_ratio": 0.7, "detector": 6, "lam_lo_um": 4.4, "lam_hi_um": 5.0},
            {"kind": "static", "flux_ratio": 1.0, "detector": 2, "lam_lo_um": 1.1, "lam_hi_um": 1.6}]
    v = rp.variable_summary(rows)
    assert (v["n"], v["rmin"], v["rmax"], v["dets"]) == (2, 0.5, 0.7, [5, 6])
    assert v["spans"] == [(3.8, 5.0)]
