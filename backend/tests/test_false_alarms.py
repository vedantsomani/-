"""False alarms around a bright saturated star (synthetic, no network).

A bright, saturated high-proper-motion star with broad wings and diffraction spikes
(which rotate with the spacecraft roll between seasons) leaves residuals all over the
season differences. Only the star itself may come out as a strong/moderate candidate;
a faint isolated mover elsewhere in the same field must still be found.
"""

import itertools
import math

import numpy as np

from app import analysis
from app import changes as ch
from app.processing import Grid

PIX = 6.15
GRID = Grid.for_fov(150.0, 2.0, 5.0, PIX)
N = GRID.n
SEASONS = (0, 1, 2)
MJD = {0: 60500.0, 1: 60683.0, 2: 60866.0}
DETECTORS = (1, 2, 3)
NOISE = 0.01

BRIGHT = {0: (18.0, 20.0), 1: (18.0, 20.9), 2: (18.0, 21.8)}  # ~0.9 px per half year, north
BRIGHT_AMP = 400.0
ROLL_DEG = {0: 0.0, 1: 25.0, 2: 50.0}                          # spikes rotate between seasons
FAINT = {0: (36.0, 38.0), 1: (35.4, 38.0), 2: (34.8, 38.0)}    # 0.6 px per half year, east
FAINT_AMP = 0.3
STATIC = [(10.0, 12.0, 0.5), (26.0, 14.0, 0.4), (11.0, 28.0, 0.6), (40.0, 10.0, 1.0), (8.0, 40.0, 0.8)]


def stack(rng, season: int, det: int) -> dict:
    yy, xx = np.mgrid[0:N, 0:N].astype(float)
    img = rng.normal(0, NOISE, (N, N))

    def psf(x, y, amp):
        return amp * np.exp(-((xx - x) ** 2 + (yy - y) ** 2) / (2 * ch.PSF_SIGMA_PX**2))

    for x, y, a in STATIC + [(*FAINT[season], FAINT_AMP)]:
        img += psf(x, y, a)
    bx, by = BRIGHT[season]
    r = np.hypot(xx - bx, yy - by)
    img += psf(bx, by, BRIGHT_AMP)
    img += BRIGHT_AMP * 2e-3 * (1 + (r / 2.0) ** 2) ** -1.5                 # halo
    for k in range(4):                                                     # four spikes
        th = math.radians(ROLL_DEG[season] + 90 * k + 7 * det)             # slightly chromatic
        along = (xx - bx) * math.cos(th) + (yy - by) * math.sin(th)
        perp = -(xx - bx) * math.sin(th) + (yy - by) * math.cos(th)
        img += np.where(along > 0, BRIGHT_AMP * 4e-3 * np.exp(-perp**2 / (2 * 0.6**2)) * (1 + along / 2) ** -2, 0)
    n = np.full((N, N), 10, np.int16)
    var = np.full((N, N), NOISE**2, np.float32)
    core = r <= 1.6                                                        # saturated: masked
    n[core] = 0
    var[core] = np.nan
    return {"sci": img.astype(np.float32), "var": var, "n": n, "footprint": np.ones((N, N), np.float32),
            "excluded": core, "wave": np.full((N, N), 1.0 + det, np.float32)}


def field_candidates(vet: bool) -> list[dict]:
    rng = np.random.default_rng(42)
    stacks = {(s, d): stack(rng, s, d) for s in SEASONS for d in DETECTORS}
    events = []
    for d in DETECTORS:
        for sa, sb in itertools.combinations(SEASONS, 2):
            A, B = stacks[(sa, d)], stacks[(sb, d)]
            zs = ch.matched_filter(ch.zmap(ch.diff_maps(A, B))[0])
            info = {"bins": [], "bins_missing": [], "lam_lo_um": 1.0, "lam_hi_um": 2.0,
                    "mjd_a": MJD[sa], "mjd_b": MJD[sb]}
            events += analysis.pair_events(A, B, zs, d, sa, sb, f"d{d}_s{sa}-s{sb}", info, PIX)
    cands = analysis._merge_events(events, GRID.wcs())
    holes = np.any([s["n"] == 0 for s in stacks.values()], axis=0)
    analysis._demote_saturated_halos(holes, cands)
    if vet:
        analysis.vet_candidates(cands)
    return cands


def near(c: dict, track: dict, tol: float) -> bool:
    return min(math.hypot(c["x"] - x, c["y"] - y) for x, y in track.values()) <= tol


def confident(cands):
    return [c for c in cands if c["evidence"] in ("strong", "moderate")]


def test_synthetic_field_has_false_alarms_without_vetting():
    """Guard that the scene is hard: the residuals do produce false alarms before vetting."""
    false = [c for c in confident(field_candidates(vet=False))
             if not near(c, BRIGHT, 2.5) and not near(c, FAINT, 1.5)]
    assert false, "synthetic bright star should produce residual candidates without vetting"


def test_bright_saturated_mover_leaves_no_confident_false_alarm_and_faint_mover_survives():
    cands = field_candidates(vet=True)
    false = [c for c in confident(cands) if not near(c, BRIGHT, 2.5) and not near(c, FAINT, 1.5)]
    assert not false, [(c["kind"], c["evidence"], c["x"], c["y"]) for c in false]

    faint = [c for c in cands if near(c, FAINT, 1.5)]
    assert faint and faint[0]["kind"] == "mover" and faint[0]["evidence"] == "strong"
    assert faint[0]["pmra_masyr"] > 0, "moving east (-x) means positive pmra"
    expected = 0.6 * PIX * 1000 / ((MJD[1] - MJD[0]) / 365.25)
    assert math.isclose(faint[0]["pm_total_masyr"], expected, rel_tol=0.2)

    # demoted residuals stay listed, explained, with a failed check
    demoted = [c for c in cands if c["kind"] == "uncertain"]
    assert demoted and all(c.get("reason") for c in demoted)
    assert all(any(not k["ok"] for k in c["checks"]) for c in demoted)


def test_vetting_rules_on_single_measurements():
    ok_mover = {"kind": "mover", "local_z": 9.0, "flux_ratio": 1.1, "brightest_nearby": False}
    assert analysis._local_ok(ok_mover) and analysis._flux_ok(ok_mover)
    assert not analysis._flux_ok({**ok_mover, "flux_ratio": 0.25})          # fading "mover" = blend
    assert not analysis._local_ok({**ok_mover, "local_z": 3.0})             # lost in a bright star's wings
    assert analysis._local_ok({**ok_mover, "local_z": 3.0, "brightest_nearby": True})  # its own wings
    assert analysis._centre_ok({"change_offset_px": 0.4})
    assert not analysis._centre_ok({"change_offset_px": 1.8})               # PSF-edge residual
    assert not analysis._centre_ok({"change_offset_px": None})              # no source at all
