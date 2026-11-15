import numpy as np
import pytest

from app.stacks import coadd, robust_noise

N = 41


def frame(rng, sigma=0.01, star=0.0, var_scale=1.0, cr=None, excluded=None):
    yy, xx = np.mgrid[0:N, 0:N]
    psf = np.exp(-((xx - 20) ** 2 + (yy - 20) ** 2) / (2 * 0.9**2))
    sci = star * psf + rng.normal(0, sigma * np.sqrt(var_scale), (N, N))
    exc = np.zeros((N, N), bool)
    if cr:
        sci[cr[1], cr[0]] += 50.0
    if excluded:
        sci[excluded[1], excluded[0]] = 1e4  # garbage that must be ignored
        exc[excluded[1], excluded[0]] = True
    return {
        "sci": sci.astype(np.float32),
        "var": np.full((N, N), (sigma**2) * var_scale, np.float32),
        "footprint": np.ones((N, N), np.float32),
        "excluded": exc,
        "wave": np.full((N, N), 4.5, np.float32),
    }


def test_noise_drops_as_sqrt_n():
    rng = np.random.default_rng(0)
    single = robust_noise(frame(rng)["sci"])
    stack = coadd([frame(rng) for _ in range(16)])
    assert robust_noise(stack["sci"], stack["n"]) == pytest.approx(single / 4, rel=0.15)
    assert stack["n"].max() == 16


def test_cosmic_ray_rejected():
    rng = np.random.default_rng(1)
    frames = [frame(rng) for _ in range(8)]
    frames[3] = frame(rng, cr=(5, 7))
    stack = coadd(frames)
    assert abs(stack["sci"][7, 5]) < 0.05
    assert stack["n"][7, 5] == 7


def test_excluded_pixels_ignored():
    rng = np.random.default_rng(2)
    stack = coadd([frame(rng, excluded=(10, 10)) for _ in range(2)] + [frame(rng) for _ in range(2)])
    assert abs(stack["sci"][10, 10]) < 0.05
    assert stack["n"][10, 10] == 2


def test_bright_star_with_varying_flux_is_not_clipped():
    # Each exposure samples a different wavelength, so a star's surface
    # brightness changes between frames; the core must survive the clip.
    rng = np.random.default_rng(3)
    fluxes = [3.0, 4.0, 5.0, 3.5, 4.5]
    stack = coadd([frame(rng, star=f) for f in fluxes])
    assert stack["n"][20, 20] == len(fluxes)
    assert stack["sci"][20, 20] == pytest.approx(np.mean(fluxes), rel=0.05)


def test_inverse_variance_weighting():
    rng = np.random.default_rng(4)
    good = frame(rng, star=1.0, sigma=0.001)
    noisy = frame(rng, star=2.0, sigma=0.001, var_scale=100.0)
    stack = coadd([good, noisy])  # < 3 frames: no clipping, pure weighting
    # weights 1 : 0.01 -> mean ~ (1 + 0.02) / 1.01
    assert stack["sci"][20, 20] == pytest.approx(1.0101, abs=0.02)


def test_source_bright_at_some_wavelengths_is_not_clipped():
    # T dwarf-like: 20x brighter in 3 of 8 frames (its bright wavelengths). Those frames
    # support each other, so the core is kept; a cosmic ray in one frame still goes.
    rng = np.random.default_rng(3)
    frames = [frame(rng, star=5.0 if i in (1, 4, 6) else 0.25) for i in range(8)]
    frames[2] = {**frames[2], "sci": frames[2]["sci"].copy()}
    frames[2]["sci"][7, 5] += 50.0
    stack = coadd(frames)
    assert stack["n"][20, 20] == 8
    assert stack["n"][7, 5] == 7
    assert stack["sci"][20, 20] == pytest.approx((3 * 5.0 + 5 * 0.25) / 8, rel=0.02)
