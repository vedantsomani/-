"""Joint per-group position fits on synthetic exposures (no network, no cache)."""

import math

import numpy as np
import pytest

from app import astrometry as am
from app import fingerprint as fp
from tests.test_fingerprint import DEC, RA, meta, scene


def star(ra, dec, pmra=0.0, pmdec=0.0):
    return fp.Source("target", "target", ra, dec, pmra, pmdec, 61000.0)


def group(src, flux, n=5, seed=0, extra=()):
    out = []
    for i in range(n):
        mjd = 61000.0 + i
        cut, lib = scene([(src, flux), *extra], mjd=mjd, seed=seed + i)
        out.append((cut, lib, meta(i, mjd=mjd)))
    return out


def test_tangent_plane_round_trip():
    xi, eta = am.to_tp(RA + 0.001, DEC - 0.002, RA, DEC)
    ra, dec = am.from_tp(xi, eta, RA, DEC)
    assert ra == pytest.approx(RA + 0.001, abs=1e-10)
    assert dec == pytest.approx(DEC - 0.002, abs=1e-10)
    assert float(eta) == pytest.approx(-7.2, abs=1e-3)


def test_recovers_offset_position_not_the_start():
    """Start 5" away from the true position: the fit must find the source, with honest errors."""
    true = star(RA, DEC)
    start_ra, start_dec = am.from_tp(4.0, -3.0, RA, DEC)
    pulls = []
    for seed in range(4):
        gm = am.measure_group(group(true, 300.0, seed=10 * seed), [], float(start_ra), float(start_dec),
                              group="g", season=0, detector=6)
        assert gm.status == "measured", gm.reason
        assert gm.offset_from_start_arcsec == pytest.approx(5.0, abs=0.5)
        assert gm.delta_chi2_vs_start > 25
        xi, eta = am.to_tp(gm.ra, gm.dec, RA, DEC)
        d = np.array([float(xi), float(eta)])
        assert np.hypot(*d) < 0.5
        pulls.append(float(d @ np.linalg.solve(gm.cov, d)))
        assert gm.mjd == pytest.approx(61002.0, abs=0.3)
    assert np.mean(pulls) < 6  # chi2 with 2 dof averages 2


def test_faint_source_not_detected():
    gm = am.measure_group(group(star(RA, DEC), 0.0), [], RA, DEC, group="g", season=0, detector=6)
    assert gm.status == "not_detected"
    assert gm.ra is None and gm.cov is None


def test_source_outside_search_hits_bound():
    true = star(RA, DEC)
    sra, sdec = am.from_tp(-16.0, 0.0, RA, DEC)  # 16" away, search radius 12"
    gm = am.measure_group(group(true, 300.0), [], float(sra), float(sdec), group="g", season=0, detector=6)
    assert gm.status == "at_bound"
    assert not gm.usable


def test_neighbour_is_held_fixed_and_target_found():
    nb = fp.Source("n1", "neighbour", *am.from_tp(-9.0, 4.0, RA, DEC), 0.0, 0.0, 61000.0, gaia_id="1")
    true = star(RA, DEC)
    gm = am.measure_group(group(true, 200.0, extra=[(nb, 2000.0)]), [nb], *am.from_tp(2.0, 2.0, RA, DEC),
                          group="g", season=0, detector=6)
    assert gm.status == "measured"
    xi, eta = am.to_tp(gm.ra, gm.dec, RA, DEC)
    assert math.hypot(float(xi), float(eta)) < 0.6


def test_registration_floor_recovers_injected_scatter():
    rng = np.random.default_rng(1)
    offs = []
    for _ in range(400):
        C = np.eye(2) * 0.1**2
        offs.append((rng.normal(0, math.hypot(0.1, 0.3), 2), C))
    s, info = am.registration_floor(offs)
    assert s == pytest.approx(0.3, abs=0.04)
    s0, _ = am.registration_floor([(rng.normal(0, 0.1, 2), np.eye(2) * 0.01) for _ in range(200)])
    assert s0 < 0.05


def test_sigma_equivalent_and_ellipse():
    _, sig, p = am.sigma_equivalent(np.array([2.0, 0.0]), np.eye(2))
    assert p == pytest.approx(math.exp(-2), rel=1e-9)
    assert 1.4 < sig < 1.7
    _, big, _ = am.sigma_equivalent(np.array([100.0, 0.0]), np.eye(2))
    assert 95 < big < 101
    e = am.ellipse(np.array([[4.0, 0.0], [0.0, 1.0]]))  # elongated east-west
    assert e["a_arcsec"] == pytest.approx(2.0) and e["b_arcsec"] == pytest.approx(1.0)
    assert e["pa_deg"] == pytest.approx(90.0)
