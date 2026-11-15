"""Investigation report: structure, HTML well-formedness, no NaN, with and without a fingerprint."""

import json
from datetime import datetime, timezone
from html.parser import HTMLParser
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import investigation as inv
from app.config import settings
from app.fields import FieldJob
from app.fingerprint import FINGERPRINT_VERSION, Target
from app.processing import Grid
from test_explain import SEASONS, mover_analysis

KEY = "abc123def4567890"
VOID = {"meta", "img", "br", "hr", "link", "input"}
NAN = float("nan")
TARGET = Target(ra=133.76002, dec=-7.242207, pmra=-8123.7, pmdec=673.2, epoch_mjd=61000.0)


class Balanced(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack, self.errors = [], []

    def handle_starttag(self, tag, attrs):
        if tag not in VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack.pop() != tag:
            self.errors.append(tag)


def assert_well_formed(text: str) -> None:
    assert text.startswith("<!doctype html>")
    p = Balanced()
    p.feed(text)
    p.close()
    assert not p.errors and not p.stack, (p.errors, p.stack)
    assert "NaN" not in text


def make_job() -> FieldJob:
    grid = Grid.for_fov(133.76002, -7.242207, 5.0, 6.15)
    job = FieldJob(key=KEY, grid=grid, status="complete", total=4)
    seasons = [{**s, "end": s["start"], "n_frames": 2, "detectors": [5, 6], "mjd_start": 60800.0} for s in SEASONS]
    job.seasons = seasons
    job.frames = {
        f"f{i}": {"id": f"f{i}", "mjd": 60800.0 + 60 * (i // 2), "detector": 5 + i % 2, "season": i // 2}
        for i in range(4)
    }
    job.failed = {"bad1": "RuntimeError: cutout download failed: HTTP 503", "bad2": "no coverage <b>x</b>"}
    job.analysis = mover_analysis()
    job.last_checked = 1_760_000_000.0
    return job


def fingerprint_result() -> dict:
    spec = [
        {"channel": 85, "wavelength_um": 3.9, "detector": 5, "flux_ujy": 120.5, "flux_err_ujy": 4.0,
         "aperture_flux_ujy": 150.0, "aperture_err_ujy": 6.0, "n": 4, "quality": "good", "seasons": {}},
        {"channel": 86, "wavelength_um": 4.0, "detector": 6, "flux_ujy": NAN, "flux_err_ujy": NAN,
         "aperture_flux_ujy": None, "aperture_err_ujy": None, "n": 2, "quality": "suspect", "wing_fit": True},
    ]
    lcs = [{"bin": "d6b0", "detector": 6, "lam_lo_um": 4.4, "lam_hi_um": 5.0, "chi2_const": NAN, "dof": 1,
            "variable": False, "matched": True,
            "points": [{"season": 1, "flux_ujy": 10.0, "flux_err_ujy": 1.0}, {"season": 2, "flux_ujy": NAN,
                                                                               "flux_err_ujy": 1.0}]}]
    return {
        "status": "complete", "version": FINGERPRINT_VERSION, "field": KEY,
        "target": {"ra": TARGET.ra, "dec": TARGET.dec, "pmra_masyr": TARGET.pmra, "pmdec_masyr": TARGET.pmdec,
                   "epoch_mjd": TARGET.epoch_mjd},
        "sources": [{"id": "t", "role": "target"},
                    {"id": "n0", "role": "neighbour", "gaia_id": "123", "sep_arcsec": 4.5, "g_mag": 17.2,
                     "pmra_masyr": 1.0, "pmdec_masyr": -2.0, "variable": None}],
        "blend_warning": True, "blend_note": "A star 4.5 arcsec away blends with the target.",
        "catalog": {"name": "Gaia DR3", "ok": True, "note": None},
        "spectrum": spec, "light_curves": lcs,
        "classification": {"label": "T dwarf", "confidence": "moderate", "reasons": ["methane absorption"],
                           "caveats": ["blended"], "confidence_note": "rule-based, not a probability",
                           "indices": {"CH4": {"value": 0.7, "err": 0.1, "sigma": 3.0, "note": None}}},
        "method": {"notes": ["Errors are 1-sigma."]},
    }


GEN = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)


def test_report_without_fingerprint_says_not_computed():
    doc = inv.build_report(make_job(), TARGET, name="WISE 0855-0714", page_url="https://x.test/?ra=1&dec=2",
                           generated=GEN)
    text = inv.render_html(doc)
    assert_well_formed(text)
    assert doc["fingerprint"]["computed"] is False and "Not computed yet" in text
    for needle in ("WISE 0855-0714", "https://x.test/?ra=1&amp;dec=2", "2026-11-15T12:00:00+00:00",
                   "Failed downloads (2)", "6.2 arcseconds", "not a confirmed discovery", "NASA/JPL-Caltech",
                   "Evidence checks", "Measurements per detector", "What the measurements support", "evidence:"):
        assert needle in text, needle
    assert "<b>x</b>" not in text and "&lt;b&gt;x&lt;/b&gt;" in text       # failure text is escaped
    json.dumps(doc, allow_nan=False)
    assert doc["data"]["versions"]["fingerprint"] == FINGERPRINT_VERSION
    assert {c["n"] for c in doc["data"]["per_season_detector"]} == {1}


def test_report_with_fingerprint_has_spectrum_lightcurves_and_neighbours():
    imgs = [{"caption": "D6 stack", "data_uri": "data:image/png;base64,AAAA"}]
    doc = inv.build_report(make_job(), TARGET, name="t", fingerprint=fingerprint_result(), images=imgs,
                           generated=GEN)
    text = inv.render_html(doc)
    assert_well_formed(text)
    assert doc["fingerprint"]["computed"] and "Not computed yet" not in text
    for needle in ("T dwarf", "methane absorption", "120.50 ± 4.00", "(wing fit)", "Gaia neighbours", "123",
                   "d6b0", "Errors are 1-sigma.", "data:image/png;base64,AAAA", "Spectral indices"):
        assert needle in text, needle
    s = json.dumps(doc, allow_nan=False)      # NaN anywhere would raise
    assert "NaN" not in s and doc["fingerprint"]["spectrum"][1]["flux_ujy"] is None


def test_report_without_analysis_or_page_url_still_renders():
    job = make_job()
    job.analysis = {}
    doc = inv.build_report(job, TARGET, page_url="javascript:alert(1)", generated=GEN)
    text = inv.render_html(doc)
    assert_well_formed(text)
    assert doc["page_url"] == "" and "javascript:" not in text and "No verdict is available" in text


def test_render_images_picks_latest_two_seasons_of_reddest_detector(monkeypatch):
    job = make_job()
    job.stacks = {f"s{s}_d{d}": {"id": f"s{s}_d{d}", "season": s, "detector": d, "bin": None, "n_frames": 3,
                                 "mjd_min": 60800.0, "mjd_max": 60801.0} for s in range(3) for d in (1, 6)}
    job.analysis["diffs"].append({"id": "d6_s1-s2", "detector": 6, "dt_days": 170.0})
    drawn = []
    monkeypatch.setattr(inv, "render_stack_png", lambda j, sid, **kw: drawn.append(sid) or b"\x89PNG")
    monkeypatch.setattr(inv, "diff_arrays", lambda k, d: {"z": [[0.0]]})
    monkeypatch.setattr(inv, "render_diff_png", lambda z, zmax: b"\x89PNGdiff")
    imgs = inv.render_images(job)
    assert drawn == ["s1_d6", "s2_d6"] and len(imgs) == 3 and "difference" in imgs[2]["caption"]
    assert all(i["data_uri"].startswith("data:image/png;base64,") for i in imgs)


def _client(tmp_path, monkeypatch, with_fp: bool):
    monkeypatch.setattr(settings, "cache_dir", tmp_path)
    if with_fp:
        d = tmp_path / "fingerprints" / KEY
        d.mkdir(parents=True)
        (d / "x.json").write_text(json.dumps(fingerprint_result(), allow_nan=True))
    app = FastAPI()
    app.include_router(inv.router)
    job = make_job()
    app.state.fields = SimpleNamespace(jobs={KEY: job})
    app.state.fingerprints = SimpleNamespace(jobs={})
    return TestClient(app), job


Q = "pmra=-8123.7&pmdec=673.2&epoch=61000&ra=133.76002&dec=-7.242207&name=WISE%200855&url=https://x.test/%3Fa%3D1"


def test_route_html_and_json_without_cached_fingerprint(tmp_path, monkeypatch):
    c, _ = _client(tmp_path, monkeypatch, with_fp=False)
    r = c.get(f"/api/investigation?key={KEY}&{Q}")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert_well_formed(r.text)
    assert "Not computed yet" in r.text and "WISE 0855" in r.text
    j = c.get(f"/api/investigation?key={KEY}&{Q}&format=json")
    assert j.status_code == 200 and j.json()["fingerprint"]["computed"] is False
    assert j.json()["page_url"] == "https://x.test/?a=1"
    assert not (tmp_path / "fingerprints").exists()                      # nothing was computed or cached


def test_route_uses_cached_fingerprint_for_matching_target_only(tmp_path, monkeypatch):
    c, _ = _client(tmp_path, monkeypatch, with_fp=True)
    r = c.get(f"/api/investigation?key={KEY}&{Q}")
    assert r.status_code == 200 and "T dwarf" in r.text and "Not computed yet" not in r.text
    other = Q.replace("pmra=-8123.7", "pmra=5")
    assert "Not computed yet" in c.get(f"/api/investigation?key={KEY}&{other}").text
    assert "NaN" not in c.get(f"/api/investigation?key={KEY}&{Q}&format=json").text


def test_route_errors_and_key_derivation(tmp_path, monkeypatch):
    c, job = _client(tmp_path, monkeypatch, with_fp=False)
    assert c.get("/api/investigation?key=nope").status_code == 404
    assert c.get("/api/investigation").status_code == 422
    job.status = "processing"
    assert c.get(f"/api/investigation?key={KEY}").status_code == 409
    k = inv.field_key_for(133.76002, -7.242207, -8123.7, 673.2, 61000.0, None)
    assert len(k) == 16 and k == inv.field_key_for(133.76002, -7.242207, -8123.7, 673.2, 61000.0, 5.0)
