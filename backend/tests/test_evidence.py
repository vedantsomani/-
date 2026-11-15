from app.analysis import evidence, independent_count


def row(det, sa, sb, kind="mover"):
    return {"detector": det, "season_a": sa, "season_b": sb, "kind": kind}


def test_season_pairs_sharing_a_stack_are_not_independent():
    rows = [row(6, 0, 1), row(6, 0, 2), row(6, 1, 2)]  # three pairs from three seasons
    assert independent_count(rows, "mover") == (2, 1)


def test_independent_count_adds_detectors():
    rows = [row(5, 1, 2), row(6, 1, 2), row(6, 0, 1, kind="static")]
    assert independent_count(rows, "mover") == (2, 2)


def test_evidence_levels():
    coherent = {"coherent": True}
    strong = evidence("mover", [row(5, 1, 2), row(6, 1, 2)], coherent)
    assert strong["evidence"] == "strong" and all(c["ok"] for c in strong["checks"])
    one_det = evidence("mover", [row(6, 0, 1), row(6, 1, 2)], coherent)
    assert one_det["evidence"] == "moderate"
    single = evidence("mover", [row(6, 1, 2)], {"coherent": None})
    assert single["evidence"] == "weak"
    assert any("cannot be checked" in c.get("note", "") for c in single["checks"])
    incoherent = evidence("mover", [row(5, 1, 2), row(6, 1, 2)], {"coherent": False})
    assert incoherent["evidence"] == "weak"
    assert evidence("uncertain", [], None)["evidence"] == "weak"
