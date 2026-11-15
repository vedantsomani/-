"""False-colour RGB and flag-overlay rendering + routes (offline, synthetic arrays)."""

from __future__ import annotations

import io
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from app import colour
from app.config import settings
from app.store import save_arrays, stack_path, frame_path

N = 31
C = N // 2
KEY = "testkey"
BITS = {"OVERFLOW": 2, "HOT": 1024, "OUTLIER": 524288, "GHOST": 4194304, "SOURCE": 2097152}


def _stack(star: float, rng) -> dict:
    sci = rng.normal(0, 1.0, (N, N)).astype(np.float32)
    sci[C, C] += star
    return {
        "sci": sci, "var": np.ones((N, N), np.float32), "n": np.full((N, N), 3, np.int16),
        "wave": np.full((N, N), 2.0, np.float32), "footprint": np.ones((N, N), np.float32),
    }


def _rgba(png: bytes) -> np.ndarray:
    return np.array(Image.open(io.BytesIO(png)).convert("RGBA"))


@pytest.fixture
def job(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "cache_dir", tmp_path)
    rng = np.random.default_rng(1)
    stacks = {}
    # season 0: all six detectors, object bright only in D5/D6 (a cold brown dwarf).
    # season 1: only D3 and D4 exist.
    plan = {0: {1: 0, 2: 0, 3: 3, 4: 3, 5: 80, 6: 80}, 1: {3: 50, 4: 50}}
    for season, dets in plan.items():
        for d, amp in dets.items():
            sid = f"s{season}_d{d}"
            arr = _stack(amp, rng)
            if season == 1 and d == 3:
                arr["n"][2:5, 2:5] = 0  # a coverage hole
            stack_path(KEY, sid).parent.mkdir(parents=True, exist_ok=True)
            save_arrays(stack_path(KEY, sid), arr)
            stacks[sid] = {"id": sid, "season": season, "detector": d, "bin": None}
    flags = np.zeros((N, N), np.int32)
    flags[C, C] = BITS["OVERFLOW"] | BITS["SOURCE"]  # masked (+ informational)
    flags[3, 3] = BITS["OUTLIER"]  # suspect
    flags[5, 5] = BITS["GHOST"]  # artefact
    flags[7, 7] = BITS["SOURCE"]  # informational only
    flags[9, 9] = BITS["OVERFLOW"] | BITS["GHOST"]  # masked wins
    foot = np.ones((N, N), np.float32)
    foot[0, :] = 0
    fr = {
        "sci": np.zeros((N, N), np.float32), "var": np.ones((N, N), np.float32), "flags": flags,
        "footprint": foot, "excluded": np.zeros((N, N), bool), "wave": np.ones((N, N), np.float32),
    }
    frame_path(KEY, "f1").parent.mkdir(parents=True, exist_ok=True)
    save_arrays(frame_path(KEY, "f1"), fr)
    return SimpleNamespace(
        key=KEY, stacks=stacks, frames={"f1": {"flag_bits": BITS}}, touch=lambda: None,
    )


@pytest.fixture
def client(job):
    app = FastAPI()
    app.state.fields = SimpleNamespace(jobs={KEY: job})
    app.include_router(colour.router)
    return TestClient(app)


def test_rgb_cold_object_is_red_and_sky_is_dark(client):
    r = client.get(f"/api/fields/{KEY}/rgb/s0.png")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert r.headers["X-RGB-Detectors"] == "1,2,3,4,5,6" and r.headers["X-RGB-Missing"] == ""
    px = _rgba(r.content)
    assert px.shape == (N, N, 4) and (px[..., 3] == 255).all()
    cr, cg, cb = (int(v) for v in px[C, C, :3])
    assert cr > 150 and cr > 2 * cg and cr > 2 * cb  # deep red at the centre
    sky = px[2:8, 2:8, :3].astype(float)
    assert sky.mean() < 40  # neutral dark sky
    assert abs(sky[..., 0].mean() - sky[..., 2].mean()) < 25  # no colour cast in the sky


def test_rgb_missing_detectors_reported_and_used(client):
    r = client.get(f"/api/fields/{KEY}/rgb/s1.png")
    assert r.status_code == 200
    assert r.headers["X-RGB-Detectors"] == "3,4" and r.headers["X-RGB-Missing"] == "1,2,5,6"
    info = client.get(f"/api/fields/{KEY}/rgb/s1.json").json()
    assert info["missing"] == [1, 2, 5, 6] and info["channels"]["green"]["detectors"] == [3, 4]
    assert info["channels"]["red"]["detectors"] == [] and info["channels"]["red"]["noise_mjysr"] is None
    px = _rgba(r.content)
    assert px[C, C, 1] > 150 and px[C, C, 0] < 30  # green only: nothing in the missing channels


def test_rgb_unknown_season_and_field_404(client):
    assert client.get(f"/api/fields/{KEY}/rgb/s9.png").status_code == 404
    assert client.get("/api/fields/nope/rgb/s0.png").status_code == 404


def test_lupton_preserves_colour_when_saturated():
    img = np.zeros((9, 9))
    rng = np.random.default_rng(0)
    r = rng.normal(0, 1, img.shape); g = rng.normal(0, 1, img.shape); b = rng.normal(0, 1, img.shape)
    r[4, 4], g[4, 4], b[4, 4] = 4000.0, 2000.0, 1000.0  # far above white
    rgb, covered, _ = colour.lupton_rgb({"red": r, "green": g, "blue": b})
    assert covered.all()
    r8, g8, b8 = (int(v) for v in rgb[4, 4])
    assert r8 == 255 and g8 / r8 == pytest.approx(0.5, abs=0.05) and b8 / r8 == pytest.approx(0.25, abs=0.06)


def test_nan_pixels_are_not_covered():
    rng = np.random.default_rng(3)
    chans = {k: rng.normal(0, 1, (9, 9)) for k in ("red", "green", "blue")}
    chans["red"][:, 0] = np.nan
    chans["green"][:, 0] = np.nan
    chans["blue"][:, 0] = np.nan
    chans["blue"][:, 1] = np.nan  # other channels still cover column 1
    rgb, covered, _ = colour.lupton_rgb(chans)
    assert not covered[:, 0].any() and covered[:, 1:].all()
    assert (rgb[:, 0] == 0).all()


def test_flag_overlay_classes(client):
    r = client.get(f"/api/fields/{KEY}/frames/f1/flags.png")
    assert r.status_code == 200
    px = _rgba(r.content)  # north up: array row y is image row N-1-y
    at = lambda y, x: tuple(int(v) for v in px[N - 1 - y, x])  # noqa: E731
    assert at(C, C) == colour.FLAG_COLOURS["masked"]
    assert at(3, 3) == colour.FLAG_COLOURS["suspect"]
    assert at(5, 5) == colour.FLAG_COLOURS["artifact"]
    assert at(7, 7)[3] == 0  # informational flags are not drawn
    assert at(9, 9) == colour.FLAG_COLOURS["masked"]  # most severe class wins
    assert (px[N - 1, :, 3] == 0).all()  # outside the footprint
    assert 0 < colour.FLAG_COLOURS["masked"][3] < 255  # semi-transparent


def test_flag_overlay_unknown_frame_404(client):
    assert client.get(f"/api/fields/{KEY}/frames/zzz/flags.png").status_code == 404


def test_coverage_overlay_marks_only_empty_pixels(client):
    r = client.get(f"/api/fields/{KEY}/stacks/s1_d3/coverage.png")
    assert r.status_code == 200
    px = _rgba(r.content)
    red = px[..., 3] > 0
    assert red.sum() == 9 and red[N - 1 - 3, 3]
    assert tuple(px[N - 1 - 3, 3]) == colour.FLAG_COLOURS["masked"]
    assert not _rgba(client.get(f"/api/fields/{KEY}/stacks/s0_d1/coverage.png").content)[..., 3].any()
    assert client.get(f"/api/fields/{KEY}/stacks/nope/coverage.png").status_code == 404
