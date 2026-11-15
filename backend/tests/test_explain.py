"""'Explain this candidate': facts layer, number validator, endpoint and AI layer (client mocked)."""

import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import explain

SEASONS = [
    {"season": 0, "start": "2025-05-05"},
    {"season": 1, "start": "2025-10-31"},
    {"season": 2, "start": "2026-04-21"},
]
PIX = 6.15
KEY = "abc123"


def row(det, sa, sb, kind, **kw):
    r = {"detector": det, "season_a": sa, "season_b": sb, "kind": kind, "lam_lo_um": 4.42, "lam_hi_um": 5.01,
         "bins": [f"d{det}b0", f"d{det}b1"], "bins_missing": []}
    r.update(kw)
    return r


def diffs_for(*rows):
    out = []
    for r in rows:
        pair = f"s{r['season_a']}-s{r['season_b']}"
        out.append({"id": f"d{r['detector']}_{pair}", "detector": r["detector"], "dt_days": 171.2})
        out += [{"id": f"{b}_{pair}", "detector": r["detector"], "dt_days": 171.2} for b in r.get("bins", [])]
    return out


CHECKS_OK = [
    {"label": "Seen in two or more detectors", "ok": True},
    {"label": "Two or more independent season comparisons", "ok": True},
    {"label": "Motion agrees in direction and speed across measurements", "ok": True},
]


def mover_analysis():
    rows = [
        row(6, 1, 2, "mover", shift_px=0.646, shift_sigma=7.9, flux_ratio=0.891, dflux_sigma=-1.2,
            snr_a=16.3, snr_b=16.4, pmra_masyr=-8907, pmdec_masyr=160),
        row(5, 1, 2, "mover", shift_px=0.513, shift_sigma=6.3, flux_ratio=0.898, dflux_sigma=-1.0,
            snr_a=15.6, snr_b=15.2, pmra_masyr=-6475, pmdec_masyr=131, lam_lo_um=3.82, lam_hi_um=4.42),
        row(1, 1, 2, "static", shift_px=0.037, shift_sigma=0.5, flux_ratio=1.011, dflux_sigma=0.2,
            snr_a=49.1, snr_b=50.3, lam_lo_um=0.74, lam_hi_um=1.12),
        row(6, 0, 1, "uncertain", reason="one epoch too shallow to compare", snr_a=2.6, snr_b=6.8),
    ]
    return {
        "version": "5", "diffs": diffs_for(*rows),
        "target": {
            "summary": "mover", "rows": rows, "pmra_masyr": -7661, "pmdec_masyr": 1192, "pm_consistent": True,
            "motion_check": {"n_movers": 2, "coherent": True, "coherence": 0.99, "speed_scatter": 0.14},
            "evidence": "strong", "n_independent": 2, "n_detectors_agree": 2, "checks": CHECKS_OK,
        },
        "candidates": [{"id": "c0", "kind": "mover", "is_target": True, "ra": 133.76002, "dec": -7.24221,
                        "max_z": 8.9, "detectors": [5, 6], "evidence": "strong", "events": []}],
    }


def variable_analysis():
    ev = [
        row(3, 0, 2, "variable", diff="d3_s0-s2", shift_px=0.004, shift_sigma=0.1, flux_ratio=0.337,
            dflux_sigma=-19.8, snr_a=400.0, snr_b=410.0),
        row(3, 1, 2, "variable", diff="d3_s1-s2", shift_px=0.036, shift_sigma=0.5, flux_ratio=0.428,
            dflux_sigma=-16.0, snr_a=300.0, snr_b=400.0),
        row(2, 1, 2, "variable", diff="d2_s1-s2", shift_px=0.033, shift_sigma=0.5, flux_ratio=1.801,
            dflux_sigma=11.4, snr_a=300.0, snr_b=400.0, method="shift fit (saturated core)"),
    ]
    return {
        "version": "5", "diffs": diffs_for(*ev),
        "target": {"summary": "static", "rows": [], "evidence": "weak", "n_independent": 0,
                   "n_detectors_agree": 0, "checks": [], "motion_check": {"n_movers": 0, "coherent": None}},
        "candidates": [{
            "id": "c1", "kind": "variable", "ra": 76.97, "dec": 30.37, "max_z": 13.5, "detectors": [2, 3],
            "evidence": "moderate", "n_independent": 4, "n_detectors_agree": 2,
            "checks": [{"label": "Seen in two or more detectors", "ok": True},
                       {"label": "Two or more independent season comparisons", "ok": True}],
            "motion_check": {"n_movers": 3, "coherent": False, "coherence": 0.24, "speed_scatter": 0.23},
            "flux_ratio_min": 0.337, "flux_ratio_max": 1.801, "events": ev,
        }],
    }


def uncertain_analysis():
    ev = [row(4, 1, 2, "uncertain", diff="d4_s1-s2", shift_px=0.9, shift_sigma=3.1, snr_a=9.0, snr_b=8.0,
              reason="one epoch too shallow to compare"),
          row(4, 0, 2, "uncertain", no_comparison=True,
              reason="no wavelength bin with 3 or more exposures in both seasons")]
    return {
        "version": "5", "diffs": diffs_for(*ev),
        "target": {"summary": "possible mover", "evidence": "weak", "n_independent": 1, "n_detectors_agree": 1,
                   "checks": [{"label": "Seen in two or more detectors", "ok": False},
                              {"label": "Two or more independent season comparisons", "ok": False},
                              {"label": "Motion agrees in direction and speed across measurements", "ok": False,
                               "note": "only one measurement, cannot be checked"}],
                   "motion_check": {"n_movers": 1, "coherent": None}, "pmra_masyr": -3000, "pmdec_masyr": 400,
                   "rows": [row(6, 1, 2, "mover", shift_px=0.4, shift_sigma=4.5, flux_ratio=0.95, dflux_sigma=-0.5,
                                snr_a=20.0, snr_b=22.0, pmra_masyr=-3000, pmdec_masyr=400)]},
        "candidates": [{
            "id": "c2", "kind": "uncertain", "ra": 10.0, "dec": 20.0, "max_z": 7.2, "detectors": [4],
            "evidence": "weak", "n_independent": 0, "n_detectors_agree": 0,
            "checks": [{"label": "Seen in two or more detectors", "ok": False},
                       {"label": "Two or more independent season comparisons", "ok": False},
                       {"label": "Inside the halo of a saturated star", "ok": False}],
            "reason": "inside the halo of a saturated star", "events": ev,
        }],
    }


def explain_for(analysis, cand):
    out = explain.build_explanation(analysis, KEY, cand, SEASONS, PIX)
    assert out is not None
    return out


def texts(out, section):
    return [it["text"] for it in out["sections"][section]]


def all_text(out):
    return " ".join(t for s in explain.SECTIONS for t in texts(out, s))


# ------------------------------------------------------------------------------ facts
def test_mover_facts_cite_measured_numbers():
    out = explain_for(mover_analysis(), "target")
    assert out["kind"] == "mover" and out["evidence"] == "strong"
    assert set(out["sections"]) == set(explain.SECTIONS)
    supports = " ".join(texts(out, "supports"))
    assert "0.646 pixels (3.97 arcseconds)" in supports  # shift in px and arcsec, from the row
    assert "7.9 sigma" in supports and "Oct 2025" in supports and "Apr 2026" in supports
    assert "7.75 arcseconds per year" in supports  # measured proper motion
    assert "0.99" in supports  # motion coherence
    assert "0.891" in supports  # flux ratios
    # D1 disagrees: that is a stated uncertainty and the suggested next check
    assert "D1" in " ".join(texts(out, "uncertain")) and "no significant" in " ".join(texts(out, "uncertain"))
    assert "D1" in " ".join(texts(out, "next_check"))
    assert out["facts"]["proper_motion"]["total_arcsec_yr"] == 7.75
    assert out["facts"]["seasons"] == {"0": "May 2025", "1": "Oct 2025", "2": "Apr 2026"}


def test_refs_point_at_real_detector_season_pairs():
    out = explain_for(mover_analysis(), "target")
    refs = [r for s in explain.SECTIONS for it in out["sections"][s] for r in it["refs"]]
    assert refs and all(set(r) == {"detector", "season_a", "season_b", "diff_id"} for r in refs)
    top = out["sections"]["supports"][1]["refs"][0]
    assert (top["detector"], top["season_a"], top["season_b"]) == (6, 1, 2)
    assert top["diff_id"] == "d6_s1-s2"
    assert json.dumps(out)  # serialisable


def test_variable_candidate_facts():
    out = explain_for(variable_analysis(), "c1")
    assert out["kind"] == "variable"
    supports = " ".join(texts(out, "supports"))
    assert "0.337 times" in supports and "1.801" in supports and "19.8 sigma" in supports
    assert "fainter" in supports and "brighter" in supports
    unc = " ".join(texts(out, "uncertain"))
    assert "disagree with each other" in unc and "0.24" in unc  # incoherent shifts are not motion
    assert "saturated" in unc  # indirect fit for the saturated core
    nxt = texts(out, "next_check")
    assert len(nxt) == 1 and "per-wavelength" in nxt[0]  # incoherent motion -> compare the bins
    assert out["sections"]["next_check"][0]["refs"][0]["detector"] in (2, 3)
    assert "variable" in texts(out, "conclusion")[0]


def test_uncertain_candidate_lists_what_limits_it():
    out = explain_for(uncertain_analysis(), "c2")
    assert out["kind"] == "uncertain" and out["evidence"] == "weak"
    unc = " ".join(texts(out, "uncertain"))
    assert "No detector shows a clear change" in unc and "No independent season comparison" in unc
    assert "lowered for the reason given below" in unc
    assert "halo" in unc or "glow" in unc  # the engine's own reason
    assert "could not be compared at all" in unc and "at least 3" in unc  # not-comparable pair
    assert "inconclusive" in unc and "too faint" in unc
    assert "not the probability" in unc
    assert "6.15 arcseconds" in unc  # blending limit
    assert "not confirmed" in texts(out, "conclusion")[0]


def test_possible_mover_single_comparison():
    out = explain_for(uncertain_analysis(), "target")
    assert out["kind"] == "possible mover"
    unc = " ".join(texts(out, "uncertain"))
    assert "Only 1 detector shows this" in unc and "Only 1 independent season comparison supports it" in unc
    assert "Only one shift was measured" in unc
    concl = texts(out, "conclusion")[0]
    assert "possible" in concl and "not confirmed" in concl


def test_conclusions_never_overclaim():
    cases = [(mover_analysis(), "target"), (variable_analysis(), "c1"), (variable_analysis(), "target"),
             (uncertain_analysis(), "c2"), (uncertain_analysis(), "target")]
    for analysis, cand in cases:
        out = explain_for(analysis, cand)
        concl = " ".join(texts(out, "conclusion"))
        assert explain.banned_wording(concl) is None, concl
        assert 1 <= len(texts(out, "conclusion")) <= 2
        assert any(p in concl for p in ("consistent with", "candidate", "not confirmed", "do not settle"))
        low = concl.lower()
        assert "discover" not in low and "new planet" not in low


def test_banned_wording_detector():
    assert explain.banned_wording("We discovered a new planet.")
    assert explain.banned_wording("This is Planet X.")
    assert explain.banned_wording("It is confirmed.")
    assert explain.banned_wording("It is a confirmed mover.")
    assert explain.banned_wording("It is not a confirmed one.") is None
    assert explain.banned_wording("Not confirmed.") is None
    assert explain.banned_wording("It remains unconfirmed.") is None


def test_unknown_candidate_is_none():
    assert explain.build_explanation(mover_analysis(), KEY, "c9", SEASONS, PIX) is None


def test_sections_are_faithful_to_their_own_facts():
    for analysis, cand in [(mover_analysis(), "target"), (variable_analysis(), "c1"), (uncertain_analysis(), "c2")]:
        out = explain_for(analysis, cand)
        problems = explain.validate_ai_text(all_text(out), out["facts"], out["sections"])
        assert [p for p in problems if p != "too long"] == []  # (the facts text is longer than an AI summary)


# ------------------------------------------------------------------------------ validator
def test_validator_accepts_faithful_text_and_rounding():
    out = explain_for(mover_analysis(), "target")
    good = (
        "In D6 the source moved 0.646 pixels, about 3.97 arcseconds, between October 2025 and Apr 2026. "
        "That is 7.9 times its uncertainty. Together the shifts imply about 7.75 arcseconds per year. "
        "Rounded, the flux ratio was 0.89. D1 to D6 each cover their own wavelengths, and 14% speed spread is small. "
        "It is a candidate, not confirmed."
    )
    assert explain.validate_ai_text(good, out["facts"], out["sections"]) == []


def test_validator_rejects_invented_number():
    out = explain_for(mover_analysis(), "target")
    for bad in (
        "The source moved 12.5 arcseconds between seasons.",  # invented decimal
        "The source moved 0.646 pixels in 9 detectors.",  # invented whole number
        "The shift is 99 percent certain to be real.",
        "It moved 3.5 arcseconds.",  # close to real values but not a rounding of any
    ):
        problems = explain.validate_ai_text(bad, out["facts"], out["sections"])
        assert problems and "not in the measurements" in problems[0], bad
    # plain rounding to fewer decimals is fine
    assert explain.validate_ai_text("It sped along at 7.8 arcseconds per year.", out["facts"], out["sections"]) == []


def test_validator_rejects_other_inventions():
    out = explain_for(mover_analysis(), "target")
    f, s = out["facts"], out["sections"]
    assert any("D3" in p for p in explain.validate_ai_text("D3 also shows the shift.", f, s))
    assert any("Proxima" in p for p in explain.validate_ai_text("This looks like Proxima Centauri.", f, s))
    assert any("banned" in p for p in explain.validate_ai_text("This is a discovery of a new planet.", f, s))
    assert explain.validate_ai_text("", f, s)
    assert any("too long" in p for p in explain.validate_ai_text("word " * 300, f, s))


def test_number_extraction_skips_identifiers():
    assert explain._numbers_in("D6 and d6b2 moved 1,234.5 px in 2025") == [(1234.5, 1), (2025.0, 0)]


# ------------------------------------------------------------------------------ endpoint
class FakeClient:
    def __init__(self, text="", exc=None):
        self.calls = []
        self.text, self.exc = text, exc
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self.exc:
            raise self.exc
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=self.text)])


def make_app(analysis, status="complete"):
    app = FastAPI()
    app.include_router(explain.router)
    job = SimpleNamespace(status=status, analysis=analysis, seasons=SEASONS, error="boom",
                          grid=SimpleNamespace(pixscale_arcsec=PIX))
    app.state.fields = SimpleNamespace(jobs={KEY: job})
    return TestClient(app)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(explain, "field_dir", lambda key: tmp_path / key)
    monkeypatch.setattr(explain, "_locks", {})
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("SPHEREX_EXPLAIN_MODEL", raising=False)


def enable_ai(monkeypatch, client):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-secret-value")
    monkeypatch.setattr(explain, "_sdk_present", lambda: True)
    monkeypatch.setattr(explain, "make_client", lambda: client)


def test_endpoint_facts_without_ai():
    c = make_app(mover_analysis())
    r = c.get("/api/explain", params={"key": KEY, "cand": "target"})
    assert r.status_code == 200
    d = r.json()
    assert d["field"] == KEY and d["cand"] == "target" and d["kind"] == "mover" and d["evidence"] == "strong"
    assert d["ai"] is None and d["ai_available"] is False and d["ai_error"] is None
    assert set(d["sections"]) == set(explain.SECTIONS) and "facts" in d


def test_endpoint_ai_requested_without_key_returns_facts():
    d = make_app(mover_analysis()).get("/api/explain", params={"key": KEY, "cand": "target", "ai": 1}).json()
    assert d["ai_available"] is False and d["ai"] is None and d["ai_error"]
    assert d["sections"]["supports"]


def test_endpoint_errors_and_states():
    c = make_app(mover_analysis())
    assert c.get("/api/explain", params={"key": "nope"}).status_code == 404
    assert c.get("/api/explain", params={"key": KEY, "cand": "c9"}).status_code == 404
    assert c.get("/api/explain", params={"key": KEY, "cand": "../../etc"}).status_code == 404
    assert make_app(mover_analysis(), "processing").get("/api/explain", params={"key": KEY}).json() == {"status": "processing"}
    assert make_app({}, "complete").get("/api/explain", params={"key": KEY}).json() == {"status": "processing"}
    err = make_app({}, "error").get("/api/explain", params={"key": KEY}).json()
    assert err == {"status": "error", "error": "boom"}


GOOD_AI = (
    "A source in D6 shifted by 0.646 pixels, which is 3.97 arcseconds, between October 2025 and Apr 2026. "
    "Together the measurements are consistent with a drift of about 7.75 arcseconds per year, so it is a candidate "
    "mover, not confirmed. D1 sees no change, which could be a neighbouring star."
)


def test_ai_summary_is_returned_and_cached(monkeypatch, tmp_path):
    client = FakeClient(GOOD_AI)
    enable_ai(monkeypatch, client)
    c = make_app(mover_analysis())
    d = c.get("/api/explain", params={"key": KEY, "cand": "target", "ai": 1}).json()
    assert d["ai_available"] is True and d["ai_error"] is None
    assert d["ai"] == {"text": GOOD_AI, "model": explain.DEFAULT_MODEL}
    assert (tmp_path / KEY / "explain" / "target.json").exists()
    d2 = c.get("/api/explain", params={"key": KEY, "cand": "target", "ai": 1}).json()
    assert d2["ai"]["text"] == GOOD_AI and len(client.calls) == 1  # second click is free
    sent = client.calls[0]
    assert sent["model"] == explain.DEFAULT_MODEL and "FACTS:" in sent["messages"][0]["content"]
    assert "discovery" in sent["system"] and "Never add a number" in sent["system"]
    assert "sk-ant" not in json.dumps(d) and "sk-ant" not in json.dumps(sent)


def test_ai_cache_is_keyed_by_analysis_version(monkeypatch):
    client = FakeClient(GOOD_AI)
    enable_ai(monkeypatch, client)
    a = mover_analysis()
    make_app(a).get("/api/explain", params={"key": KEY, "ai": 1})
    a2 = mover_analysis()
    a2["version"] = "6"
    make_app(a2).get("/api/explain", params={"key": KEY, "ai": 1})
    assert len(client.calls) == 2


def test_model_can_be_overridden(monkeypatch):
    client = FakeClient(GOOD_AI)
    enable_ai(monkeypatch, client)
    monkeypatch.setenv("SPHEREX_EXPLAIN_MODEL", "claude-sonnet-5-5")
    d = make_app(mover_analysis()).get("/api/explain", params={"key": KEY, "ai": 1}).json()
    assert d["ai"]["model"] == "claude-sonnet-5-5" and client.calls[0]["model"] == "claude-sonnet-5-5"


def test_ai_text_with_invented_number_is_discarded(monkeypatch, tmp_path):
    client = FakeClient("The source moved 42.5 arcseconds, a candidate not confirmed.")
    enable_ai(monkeypatch, client)
    r = make_app(mover_analysis()).get("/api/explain", params={"key": KEY, "ai": 1})
    d = r.json()
    assert r.status_code == 200 and d["ai"] is None and "did not match" in d["ai_error"]
    assert d["sections"]["supports"]  # facts stay
    assert not (tmp_path / KEY / "explain" / "target.json").exists()  # bad text is never cached


@pytest.mark.parametrize("exc_name,fragment", [
    ("APITimeoutError", "too long"),
    ("RateLimitError", "busy"),
    ("AuthenticationError", "credentials"),
    ("APIConnectionError", "could not be reached"),
    ("ValueError", "could not be produced"),
])
def test_api_failures_degrade_to_facts(monkeypatch, exc_name, fragment):
    exc = type(exc_name, (Exception,), {})("request failed for key sk-ant-test-secret-value")
    enable_ai(monkeypatch, FakeClient(exc=exc))
    r = make_app(mover_analysis()).get("/api/explain", params={"key": KEY, "ai": 1})
    d = r.json()
    assert r.status_code == 200 and d["ai"] is None and fragment in d["ai_error"]
    assert "sk-ant" not in json.dumps(d)  # exception text (which could echo a key) is never returned
