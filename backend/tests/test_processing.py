"""Processing tests on a synthetic SPHEREx-like cutout (no network)."""

import io

import numpy as np
import pytest
from astropy.io import fits
from astropy.wcs import WCS

from app.fields import assign_seasons, interleave
from app.processing import Grid, process_cutout, strip_cutout
from app.sia import ImageRecord

RA, DEC = 133.795129, -7.245147
FLAG_BITS = {"TRANSIENT": 1, "NONFUNC": 64, "OUTLIER": 524288, "SOURCE": 2097152, "HALO": 268435456}


def make_cutout(n=80, zodi=0.13, rot_deg=17.0, star_offset_px=(0.0, 0.0), dead=None) -> bytes:
    w = WCS(naxis=2)
    w.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    w.wcs.crval = [RA, DEC]
    w.wcs.crpix = [n / 2 + 0.5, n / 2 + 0.5]
    s = 6.15 / 3600
    t = np.radians(rot_deg)
    w.wcs.cd = [[-s * np.cos(t), s * np.sin(t)], [s * np.sin(t), s * np.cos(t)]]
    hdr = w.to_header()

    yy, xx = np.mgrid[0:n, 0:n]
    cx, cy = w.world_to_pixel_values(RA, DEC)
    cx += star_offset_px[0]
    cy += star_offset_px[1]
    star = 5.0 * np.exp(-((xx - cx) ** 2 + (yy - cy) ** 2) / (2 * 0.8**2))
    rng = np.random.default_rng(1)
    image = (zodi + 0.02 + star + rng.normal(0, 0.01, (n, n))).astype(np.float32)
    flags = np.zeros((n, n), np.int32)
    flags[np.hypot(xx - cx, yy - cy) < 2] |= FLAG_BITS["SOURCE"]
    if dead:
        for x, y in dead:
            flags[y, x] |= FLAG_BITS["NONFUNC"]
            image[y, x] = 1e4  # garbage value that must not leak through

    img_hdr = hdr.copy()
    img_hdr["MJD-AVG"] = 60800.7
    img_hdr["XPOSURE"] = 113.6
    flag_hdr = hdr.copy()
    for i, (name, mask) in enumerate(FLAG_BITS.items()):
        flag_hdr[f"MSKN{i:04d}"] = name
        flag_hdr[f"MSKM{i:04d}"] = mask
    hdul = fits.HDUList(
        [
            fits.PrimaryHDU(),
            fits.ImageHDU(image, img_hdr, name="IMAGE"),
            fits.ImageHDU(flags, flag_hdr, name="FLAGS"),
            fits.ImageHDU(np.full((n, n), 1e-4, np.float32), hdr, name="VARIANCE"),
            fits.ImageHDU(np.full((n, n), zodi, np.float32), hdr, name="ZODI"),
            fits.BinTableHDU.from_columns([fits.Column("X", "J", array=np.arange(10))], name="EPSF"),
        ]
    )
    buf = io.BytesIO()
    hdul.writeto(buf)
    return buf.getvalue()


def test_grid_is_odd_and_centred():
    g = Grid.for_fov(RA, DEC, 5, 6.15)
    assert g.n % 2 == 1
    x, y = g.wcs().world_to_pixel_values(RA, DEC)
    assert (float(x), float(y)) == pytest.approx((g.center_pix, g.center_pix), abs=1e-6)


def test_strip_drops_epsf():
    raw = make_cutout()
    with fits.open(io.BytesIO(strip_cutout(raw))) as h:
        assert "EPSF" not in h and "IMAGE" in h


def test_reprojects_rotated_frame_onto_target_grid():
    g = Grid.for_fov(RA, DEC, 3, 6.15)
    arrays, meta = process_cutout(make_cutout(rot_deg=33), g)
    sci = arrays["sci"]
    c = g.center_pix
    peak = np.unravel_index(np.nanargmax(sci), sci.shape)
    assert peak == (c, c), "star should land on the centre pixel regardless of detector rotation"
    # zodi + residual background removed -> sky ~ 0
    assert abs(np.nanmedian(sci)) < 0.005
    assert meta["coverage"] == pytest.approx(1.0)
    assert meta["target_flags"] == ["SOURCE"]
    assert meta["target_quality"] == "good"
    assert meta["mjd"] == pytest.approx(60800.7)


def test_dead_pixels_are_filled_and_excluded():
    g = Grid.for_fov(RA, DEC, 3, 6.15)
    arrays, meta = process_cutout(make_cutout(rot_deg=0, dead=[(34, 37), (46, 44)]), g)
    assert np.nanmax(arrays["sci"]) < 10, "garbage in dead pixels must not reach the image"
    assert arrays["excluded"].any()
    assert np.isfinite(arrays["sci"]).all()


def _rec(obs, det, t):
    return ImageRecord("c", obs, det, "u", t, t + 0.001, 1.0, 2.0)


def test_seasons_and_interleave():
    recs = [_rec(f"A_{i}", 1 + i % 2, 60800 + i * 0.1) for i in range(4)] + [
        _rec(f"B_{i}", 1 + i % 2, 61000 + i * 0.1) for i in range(4)
    ]
    seasons = assign_seasons(recs)
    assert {seasons[r.frame_id] for r in recs[:4]} == {0}
    assert {seasons[r.frame_id] for r in recs[4:]} == {1}
    order = interleave(recs, seasons)
    # first four downloads already cover both seasons and both detectors
    assert {(seasons[r.frame_id], r.detector) for r in order[:4]} == {(0, 1), (0, 2), (1, 1), (1, 2)}
