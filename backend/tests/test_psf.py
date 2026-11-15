"""ePSF rendering and multi-source linear PSF fitting on synthetic data (no network)."""

import numpy as np
import pytest
from astropy.io import fits
from scipy.special import erf

from app import psf

OS = 5


def gaussian_epsf(sigma=0.6, m=33, os=OS, dx=0.0):
    """Pixel-integrated Gaussian sampled on a 5x oversampled grid, unit sum (like IRSA's)."""
    u = (np.arange(m) - (m - 1) / 2) / os

    def integ(c):
        return 0.5 * (erf((c + 0.5) / (np.sqrt(2) * sigma)) - erf((c - 0.5) / (np.sqrt(2) * sigma)))

    p = np.outer(integ(u), integ(u - dx))
    return p / p.sum()


def library(epsf=None):
    e = gaussian_epsf() if epsf is None else epsf
    return psf.PSFLibrary(detector=1, xc=np.array([0.0, 2040.0]), yc=np.array([0.0, 2040.0]),
                          epsf=np.broadcast_to(e, (2, 2) + e.shape).copy(), cwave=np.ones((2, 2)))


def template(e, x, y, shape):
    yy, xx = np.mgrid[0:shape[0], 0:shape[1]]
    return psf.render(e, x, y, xx, yy, OS)


@pytest.mark.parametrize("phase", [(0.0, 0.0), (0.3, -0.2), (0.5, 0.5)])
def test_render_conserves_flux_at_any_phase(phase):
    t = template(gaussian_epsf(), 10 + phase[0], 10 + phase[1], (21, 21))
    assert t.sum() == pytest.approx(1.0, abs=0.01)
    # centroid follows the requested sub-pixel position
    yy, xx = np.mgrid[0:21, 0:21]
    assert (t * xx).sum() / t.sum() == pytest.approx(10 + phase[0], abs=0.02)


def test_library_interpolation_and_normalisation():
    a, b = gaussian_epsf(0.5), gaussian_epsf(0.9)
    lib = psf.PSFLibrary(detector=1, xc=np.array([0.0, 100.0]), yc=np.array([0.0]),
                         epsf=np.stack([a, b])[None], cwave=np.ones((1, 2)))
    mid = lib.at(50.0, 0.0)
    assert mid.sum() == pytest.approx(1.0)
    np.testing.assert_allclose(mid, (a + b) / 2 / ((a + b) / 2).sum(), atol=1e-12)
    np.testing.assert_allclose(lib.at(-10, 5), a / a.sum(), atol=1e-12)  # clamped outside


def test_library_from_table_roundtrip(tmp_path):
    rows = np.zeros(4, dtype=psf.EPSF_DTYPE)
    for i, (bx, by) in enumerate([(0, 0), (1, 0), (0, 1), (1, 1)]):
        rows[i]["BINX"], rows[i]["BINY"] = bx, by
        rows[i]["XCENTER"], rows[i]["YCENTER"] = 500 + 1000 * bx, 500 + 1000 * by
        rows[i]["EPSF"] = gaussian_epsf(0.5 + 0.1 * i)
    lib = psf.library_from_table(rows, detector=3)
    assert lib.epsf.shape == (2, 2, 33, 33)
    np.testing.assert_allclose(lib.epsf[1, 0], gaussian_epsf(0.7), atol=1e-7)  # BINX=0, BINY=1
    psf.save_library(lib, tmp_path / "D3.npz")
    back = psf._load(str(tmp_path / "D3.npz"), 0.0)
    np.testing.assert_allclose(back.at(700, 900), lib.at(700, 900))


def test_active_offset_and_pipeline_tag():
    h = fits.Header({"CRPIX1A": -352.0, "CRPIX2A": -124.0, "CRVAL1A": 0.0, "CRVAL2A": 0.0,
                     "CDELT1A": 1.0, "CDELT2A": 1.0})
    assert psf.active_offset(h) == (353.0, 125.0)  # cutout pixel 0 -> active pixel 353
    assert psf.pipeline_tag(".../l2b-v27-2026-197/3/level2_x.fits") == "l2b-v27"
    assert psf.pipeline_tag(".../l2b_retry-v27-2026-207/6/level2_x.fits") == "l2b-v27"
    assert psf.s3_url("https://irsa.ipac.caltech.edu/ibe/data/spherex/dr1/level2/a.fits").endswith(
        ".amazonaws.com/dr1/level2/a.fits")


def blended_scene(seed=1, sep=1.2, f1=1000.0, f2=3000.0, bg=5.0, sigma_noise=2.0, n=25):
    rng = np.random.default_rng(seed)
    e = gaussian_epsf()
    x1, y1 = 12.2, 11.9
    x2, y2 = x1 + sep, y1 + 0.3
    t1, t2 = template(e, x1, y1, (n, n)), template(e, x2, y2, (n, n))
    sci = f1 * t1 + f2 * t2 + bg + rng.normal(0, sigma_noise, (n, n))
    var = np.full((n, n), sigma_noise**2)
    region = np.zeros((n, n), bool)
    region[8:17, 8:18] = True
    return sci, var, region, (t1, t2), (x1, y1)


def test_two_blended_sources_recovered_where_aperture_is_biased():
    pulls1, pulls2, ap_bias = [], [], []
    for seed in range(40):
        sci, var, region, (t1, t2), (x1, y1) = blended_scene(seed)
        usable = np.ones_like(region)
        r = psf.fit_fluxes(sci, var, usable, region, [t1, t2])
        pulls1.append((r.flux[0] - 1000) / r.err[0])
        pulls2.append((r.flux[1] - 3000) / r.err[1])
        ap = psf.aperture(sci, var, usable, x1, y1, 1.5, 4.0, 7.0)
        apc = psf.aperture(t1, np.zeros_like(var), usable, x1, y1, 1.5, 4.0, 7.0)[0]
        ap_bias.append(ap[0] / apc / 1000 - 1)
    # unbiased and correctly scaled errors
    assert abs(np.mean(pulls1)) < 0.5 and abs(np.mean(pulls2)) < 0.5
    assert 0.6 < np.std(pulls1) < 1.4
    # the neighbour leaks into the aperture: grossly biased
    assert np.mean(ap_bias) > 0.5


def test_masked_core_fit_uses_wings():
    sci, var, region, (t1, t2), (x1, y1) = blended_scene(3, sep=4.0, f1=20000.0, f2=500.0, sigma_noise=3.0)
    yy, xx = np.mgrid[0:sci.shape[0], 0:sci.shape[1]]
    usable = np.hypot(xx - x1, yy - y1) > 1.0  # saturated core masked
    sci = np.where(usable, sci, 1e9)          # garbage in masked pixels must not matter
    r = psf.fit_fluxes(sci, var, usable, region, [t1, t2])
    assert r is not None
    assert abs(r.flux[0] - 20000) < 4 * r.err[0]
    assert r.n_pix == int((region & usable).sum())


def test_clipping_removes_uncatalogued_contaminant():
    sci, var, region, (t1, t2), (x1, y1) = blended_scene(5, sep=3.0)
    e = gaussian_epsf()
    sci = sci + 4000 * template(e, 16.6, 8.4, sci.shape)  # unknown star at the box corner
    usable = np.ones_like(region)
    yy, xx = np.mgrid[0:sci.shape[0], 0:sci.shape[1]]
    protect = (np.hypot(xx - x1, yy - y1) <= 2) | (np.hypot(xx - x1 - 3, yy - y1 - 0.3) <= 2)
    plain = psf.fit_fluxes(sci, var, usable, region, [t1, t2])
    clipped = psf.fit_fluxes(sci, var, usable, region, [t1, t2], clip=5.0, protect=protect)
    assert clipped.chi2_red < plain.chi2_red
    assert clipped.chi2_red < 2
    assert abs(clipped.flux[0] - 1000) < 4 * clipped.err[0]


def test_underdetermined_fit_returns_none():
    sci, var, region, (t1, t2), _ = blended_scene()
    usable = np.zeros_like(region)
    usable[10, 10:14] = True
    assert psf.fit_fluxes(sci, var, usable, region, [t1, t2]) is None


def test_strip_cutout_captures_epsf_library(tmp_path, monkeypatch):
    import io

    from app.config import settings
    from app.processing import strip_cutout

    monkeypatch.setattr(settings, "cache_dir", tmp_path)
    rows = np.zeros(1, dtype=psf.EPSF_DTYPE)
    rows[0]["XCENTER"], rows[0]["YCENTER"] = 1020, 1020
    rows[0]["EPSF"] = gaussian_epsf()
    epsf_hdu = fits.BinTableHDU(rows, name="EPSF")
    epsf_hdu.header["OVSMPX"] = 5
    hdul = fits.HDUList([fits.PrimaryHDU(), fits.ImageHDU(np.zeros((5, 5), np.float32), name="IMAGE"), epsf_hdu])
    buf = io.BytesIO()
    hdul.writeto(buf)
    url = "https://irsa.ipac.caltech.edu/ibe/data/spherex/dr1/level2/X/l2b-v99-2026-001/4/f.fits"
    out = strip_cutout(buf.getvalue(), url, 4)
    with fits.open(io.BytesIO(out)) as h:
        assert "EPSF" not in h
    lib = psf.load_library(url, 4)
    assert lib is not None and lib.detector == 4
    np.testing.assert_allclose(lib.at(0, 0), gaussian_epsf(), atol=1e-7)
    with fits.open(io.BytesIO(strip_cutout(buf.getvalue()))) as h:  # plain call keeps working
        assert "EPSF" not in h


@pytest.mark.network
def test_fetch_library_from_s3(tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "cache_dir", tmp_path)
    url = ("https://irsa.ipac.caltech.edu/ibe/data/spherex/dr1/level2/2025W19_1B/l2b-v27-2026-197/3/"
           "level2_2025W19_1B_0116_1D3_spx_l2b-v27-2026-197.fits")
    lib = psf.fetch_library(url, 3)
    assert lib.epsf.shape == (41, 11, 33, 33)
    assert lib.at(1000, 1000).sum() == pytest.approx(1.0)
