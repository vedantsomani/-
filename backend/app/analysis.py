"""Field analysis run after the exposures are in (worker thread):

1. season x detector stacks              (Milestone 1)
2. adaptive wavelength bins + bin stacks (wavelength-matched blink)
3. season differences per bin, combined per detector
4. change events -> merged candidates (mover / variable / appeared / vanished)
5. a verdict for the requested target (field centre)
6. fast movers: exposures vs their bin stack, linked within each visit
"""

from __future__ import annotations

import itertools
import logging
import math
from collections import defaultdict

import numpy as np
from astropy.time import Time

from . import changes as ch
from .bins import build_bins
from .processing import Grid
from .stacks import coadd, group_frames, season_detector_key, stack_meta, visit_detector_key
from .store import field_dir, frame_arrays, load_arrays, save_arrays, stack_arrays, stack_path

log = logging.getLogger(__name__)

ANALYSIS_VERSION = "8"  # 7: visit stacks, zl maps; 8: coherent-subset motion check
KIND_RANK = {"mover": 0, "variable": 1, "appeared": 2, "vanished": 2, "uncertain": 3, "static": 4}
EVIDENCE_RANK = {"strong": 0, "moderate": 1, "weak": 2}


def diff_path(key: str, diff_id: str):
    return field_dir(key) / "diffs" / f"{diff_id}.npz"


def diff_arrays(key: str, diff_id: str) -> dict[str, np.ndarray]:
    return load_arrays(diff_path(key, diff_id))


def run(key: str, grid: Grid, frames: list[dict]) -> tuple[dict[str, dict], dict]:
    """Returns (stacks meta by id, analysis dict)."""
    w = grid.wcs()
    px = grid.pixscale_arcsec
    stacks: dict[str, dict] = {}

    def add_stack(stack_id: str, members: list[dict], bin_id: str | None, visit: str | None = None) -> dict:
        arrays = coadd([frame_arrays(key, m["id"]) for m in members])
        save_arrays(stack_path(key, stack_id), arrays)
        meta = stack_meta(stack_id, members, arrays, grid.n, visit=visit)
        meta.update(bin=bin_id, png=f"/api/fields/{key}/stacks/{stack_id}.png")
        stacks[stack_id] = meta
        return meta

    # 1. season x detector stacks
    for (season, det), members in sorted(group_frames(frames, season_detector_key).items()):
        add_stack(f"s{season}_d{det}", members, None)

    # 1b. per-visit stacks: every fly-over of one season and detector co-added, so the
    # blink sequence can show changes WITHIN a season (a pulsating variable, a mover
    # creeping past a star) as well as between the 2-4 seasons. Visit numbering v0,
    # v1, ... is chronological within each season and shared across detectors.
    visit_groups = group_frames(frames, visit_detector_key)
    visit_start: dict[tuple[int, str], float] = {}
    for (season, visit, _det), members in visit_groups.items():
        start = min(m["mjd"] for m in members)
        visit_start[(season, visit)] = min(visit_start.get((season, visit), start), start)
    visit_no = {}
    for season in sorted({k[0] for k in visit_start}):
        keys = sorted((k for k in visit_start if k[0] == season), key=visit_start.get)
        visit_no.update({k: i for i, k in enumerate(keys)})
    for (season, visit, det), members in sorted(visit_groups.items()):
        add_stack(f"s{season}v{visit_no[(season, visit)]}_d{det}", members, None, visit=visit)

    # 2. wavelength bins and their per-season stacks
    bins, assignment = build_bins(frames)
    by_bin_season: dict[tuple[str, int], list[dict]] = defaultdict(list)
    for f in frames:
        if f["id"] in assignment:
            by_bin_season[(assignment[f["id"]], f["season"])].append(f)
    for b in bins:
        for s in b.seasons:
            add_stack(f"s{s}_{b.id}", by_bin_season[(b.id, s)], b.id)

    # 3. differences
    diffs: dict[str, dict] = {}
    per_det_pair: dict[tuple[int, int, int], list[dict]] = defaultdict(list)
    for b in bins:
        for sa, sb in itertools.combinations(b.seasons, 2):
            A, B = stack_arrays(key, f"s{sa}_{b.id}"), stack_arrays(key, f"s{sb}_{b.id}")
            m = ch.diff_maps(A, B)
            z, k = ch.zmap(m)
            did = f"{b.id}_s{sa}-s{sb}"
            save_arrays(diff_path(key, did), {"diff": m["diff"].astype(np.float32), "z": z.astype(np.float32),
                                              "zl": ch.zmap_local(z).astype(np.float32)})
            diffs[did] = _diff_meta(key, did, b.detector, b.id, sa, sb, stacks[f"s{sa}_{b.id}"], stacks[f"s{sb}_{b.id}"], k)
            per_det_pair[(b.detector, sa, sb)].append(m)

    events: list[dict] = []
    for (det, sa, sb), maps in sorted(per_det_pair.items()):
        m = ch.combine(maps)
        z, k = ch.zmap(m)
        zs = ch.matched_filter(z)
        did = f"d{det}_s{sa}-s{sb}"
        save_arrays(diff_path(key, did), {"diff": m["diff"].astype(np.float32), "z": z.astype(np.float32),
                                          "zl": ch.zmap_local(z).astype(np.float32),
                                          "zs": zs.astype(np.float32)})
        sa_meta, sb_meta = stacks.get(f"s{sa}_d{det}"), stacks.get(f"s{sb}_d{det}")
        if not (sa_meta and sb_meta):
            continue
        diffs[did] = _diff_meta(key, did, det, None, sa, sb, sa_meta, sb_meta, k, n_bins=len(maps))
        # 4. events in this detector/pair, measured at comparable wavelengths
        pair = matched_pair(key, bins, stacks, det, sa, sb)
        if pair is None:
            continue
        A, B, info = pair
        events += pair_events(A, B, zs, det, sa, sb, did, info, px)

    candidates = _merge_events(events, w)
    _demote_saturated_halos(_saturation_holes(key, grid, stacks), candidates)

    # 5. the requested target (field centre)
    target = _target_verdict(key, grid, stacks, bins)
    _sync_target_candidate(candidates, target, grid, w)
    vet_candidates(candidates)

    # 6. fast movers within visits
    fast = _fast_movers(key, grid, frames, assignment, stacks)

    analysis = {
        "version": ANALYSIS_VERSION,
        "bins": [b.to_dict() for b in bins],
        "diffs": sorted(diffs.values(), key=lambda d: (d["detector"], d["bin"] or "", d["season_a"], d["season_b"])),
        "candidates": candidates,
        "target": target,
        "fast_movers": fast,
    }
    log.info("field %s: %d bins, %d stacks, %d diffs, %d candidates, %d fast-mover tracks",
             key, len(bins), len(stacks), len(diffs), len(candidates), len(fast))
    return stacks, analysis


def pair_events(A: dict, B: dict, zs: np.ndarray, det: int, sa: int, sb: int, did: str,
                info: dict, px: float) -> list[dict]:
    """Change events in one detector / season pair: lobes of the matched-filtered z-map,
    measured in the wavelength-balanced stacks A and B."""
    events = []
    for group in ch.group_lobes(ch.find_lobes(zs)):
        wz = np.array([abs(l["z"]) for l in group])
        x0 = float(np.sum([l["x"] for l in group] * wz) / wz.sum())
        y0 = float(np.sum([l["y"] for l in group] * wz) / wz.sum())
        meas = ch.measure_pair(A, B, x0, y0, px)
        verdict = ch.classify(meas)
        local_rms, local_z = ch.local_significance(zs, group, x0, y0)
        ev = {
            "detector": det, "season_a": sa, "season_b": sb, "diff": did,
            "x": round(x0, 2), "y": round(y0, 2),
            "max_z": round(float(wz.max()), 1),
            "local_rms": None if local_rms is None else round(local_rms, 2),
            "local_z": None if local_z is None else round(local_z, 1),
            "brightest_nearby": ch.brightest_nearby(A, B, x0, y0),
            "pos_lobe": any(l["sign"] > 0 for l in group),
            "neg_lobe": any(l["sign"] < 0 for l in group),
            "lobes": [{k2: (round(v, 2) if isinstance(v, float) else v) for k2, v in l.items()} for l in group],
            "measure": meas, **verdict, **_wavelength_support(info, A, B, x0, y0),
        }
        if verdict["kind"] == "mover":
            pmra, pmdec = ch.pm_from_shift(verdict["dx_px"], verdict["dy_px"], info["mjd_b"] - info["mjd_a"], px)
            ev.update(pmra_masyr=round(pmra), pmdec_masyr=round(pmdec))
        else:
            off = ch.change_offset(zs, group, meas)
            ev["change_offset_px"] = None if off is None else round(off, 2)
        events.append(ev)
    return events


def _diff_meta(key, did, det, bin_id, sa, sb, a, b, k, n_bins=1) -> dict:
    return {
        "id": did, "detector": det, "bin": bin_id, "season_a": sa, "season_b": sb,
        "mjd_a": a["mjd"], "mjd_b": b["mjd"], "dt_days": round(b["mjd"] - a["mjd"], 2),
        "wavelength_a_um": a["wavelength_um"], "wavelength_b_um": b["wavelength_um"],
        "n_bins": n_bins, "noise_scale": round(k, 3),
        "png": f"/api/fields/{key}/diffs/{did}.png",
    }


def _merge_events(events: list[dict], w) -> list[dict]:
    """Merge events at the same place (any detector / season pair) into candidates."""
    events = [e for e in events if e["kind"] != "static" and not (e["kind"] == "uncertain" and e["max_z"] < 6)]
    clusters: list[list[dict]] = []
    for e in sorted(events, key=lambda e: -e["max_z"]):
        for c in clusters:
            # A bright source leaves residuals all over its PSF wings: fold weaker
            # events within its (brightness-dependent) halo into it.
            if math.hypot(c[0]["x"] - e["x"], c[0]["y"] - e["y"]) <= halo_radius(c[0]["max_z"]):
                c.append(e)
                break
        else:
            clusters.append([e])
    out = []
    for i, c in enumerate(clusters):
        motion = ch.reconcile_movers(c)
        c = [e for e in c if e["kind"] != "static"]
        if not c:
            continue
        kind = min((e["kind"] for e in c), key=lambda k: KIND_RANK[k])
        x = float(np.mean([e["x"] for e in c]))
        y = float(np.mean([e["y"] for e in c]))
        ra, dec = (float(v) for v in w.pixel_to_world_values(x, y))
        cand = {
            "id": f"c{i}", "kind": kind, "x": round(x, 2), "y": round(y, 2),
            "ra": round(ra, 6), "dec": round(dec, 6),
            "max_z": max(e["max_z"] for e in c),
            "detectors": sorted({e["detector"] for e in c}),
            "n_events": len(c),
            "motion_check": motion,
            "events": c,
        }
        movers = [e for e in c if e["kind"] == "mover"]
        if movers:
            pmra, pmdec, consistent = combine_pm(movers)
            cand.update(pmra_masyr=round(pmra), pmdec_masyr=round(pmdec),
                        pm_total_masyr=round(math.hypot(pmra, pmdec)),
                        pm_consistent=consistent,
                        n_mover_detectors=len({e["detector"] for e in movers}))
        variables = [e for e in c if e["kind"] == "variable"]
        if variables:
            ratios = [e["flux_ratio"] for e in variables]
            cand.update(flux_ratio_min=min(ratios), flux_ratio_max=max(ratios))
        cand.update(evidence(kind, c, motion))
        out.append(cand)
    out.sort(key=_rank)
    for i, c in enumerate(out):
        c["id"] = f"c{i}"
    return out


SAT_HALO_PX = 5


def _saturation_holes(key: str, grid: Grid, stacks: dict) -> np.ndarray:
    """Pixels masked (no usable exposure) inside the footprint of any season x detector stack."""
    holes = np.zeros((grid.n, grid.n), bool)
    for sid, s in stacks.items():
        # season x detector stacks only: a pixel can be masked in one VISIT yet usable
        # across the whole season, and that is not a saturated-star hole.
        if s.get("bin") is None and s.get("visit") is None:
            a = stack_arrays(key, sid)
            holes |= (a["n"] == 0) & (a["footprint"] > 0.5)
    return holes


def _demote_saturated_halos(holes: np.ndarray, candidates: list[dict]) -> None:
    """Saturated stars leave masked holes whose rims and wings produce spurious dipoles.
    Within each hole's halo keep the candidate nearest the hole (the star itself) and
    demote the others to 'uncertain'."""
    from scipy import ndimage

    if not holes.any():
        return
    labels, nlab = ndimage.label(holes)
    for lab in range(1, nlab + 1):
        region = labels == lab
        if region.sum() < 2:  # single dead pixels are not saturated stars
            continue
        cy, cx = ndimage.center_of_mass(region)
        halo = ndimage.binary_dilation(region, iterations=SAT_HALO_PX)
        inside = [c for c in candidates if halo[int(round(c["y"])), int(round(c["x"]))]]
        if len(inside) < 2:
            continue
        star = min(inside, key=lambda c: math.hypot(c["x"] - cx, c["y"] - cy))
        for c in inside:
            if c is not star:
                _demote(c, "inside the halo of a saturated star")
    candidates.sort(key=_rank)
    for i, c in enumerate(candidates):
        c["id"] = f"c{i}"


# Per-measurement tests applied to every candidate other than the requested target (whose
# verdict is measured at the known position with all detectors and season pairs).
# Each entry: (check label, demotion reason if NO measurement passes).
VET_LOCAL = ("Stands out above the residuals around it",
             "not significant above the residuals around it (bright-star wings, spikes or crowding)")
VET_FLUX = ("Brightness stays the same while it moves",
            "brightness changes by more than a factor 2 while it moves (blend or residual, not motion)")
VET_CENTRE = ("Change is centred on a detected source",
              "change is not centred on a detected source (PSF-edge residual of a nearby star)")


def _local_ok(e: dict) -> bool:
    # Not applicable when the source itself is the brightest thing around (the ring holds
    # its own wings), or when the ring is unusable (field edge, masks): not penalised.
    return e.get("brightest_nearby") or e.get("local_z") is None or e["local_z"] >= ch.DETECT_SIGMA


def _flux_ok(e: dict) -> bool:
    r = e.get("flux_ratio")
    return r is None or 1 / ch.MOVER_MAX_FLUX_RATIO <= r <= ch.MOVER_MAX_FLUX_RATIO


def _centre_ok(e: dict) -> bool:
    off = e.get("change_offset_px")
    return off is not None and off <= ch.CENTRE_TOL_PX


def vet_candidates(cands: list[dict]) -> None:
    """Keep only the measurements that look like a real source changing: significant
    against the LOCAL residual level, and centred on the source (brightness changes) or
    with constant brightness (motion). Evidence is recomputed from the measurements that
    pass; a candidate with none left is demoted to 'uncertain' with the reason. Mutates cands."""
    for c in cands:
        kind = c["kind"]
        if c.get("is_target") or kind not in ("mover", "variable", "appeared", "vanished"):
            continue
        rows = [e for e in c["events"] if e["kind"] == kind]
        tests = [(VET_LOCAL, _local_ok), (VET_FLUX, _flux_ok) if kind == "mover" else (VET_CENTRE, _centre_ok)]
        passing = [e for e in rows if all(t(e) for _, t in tests)]
        vchecks = [{"label": lbl, "ok": any(t(e) for e in rows),
                    "note": f"{sum(bool(t(e)) for e in rows)} of {len(rows)} measurements pass"}
                   for (lbl, _), t in tests]
        if not passing:
            failed = [why for (_, why), t in tests if not any(t(e) for e in rows)]
            c["checks"] = c.get("checks", []) + vchecks
            _demote(c, failed[0] if failed else "no single measurement passes all checks")
            continue
        c.update(evidence(kind, passing, c.get("motion_check")))
        c["checks"] += vchecks
        c["n_events_vetted"] = len(passing)
        if kind == "mover":
            pmra, pmdec, consistent = combine_pm(passing)
            c.update(pmra_masyr=round(pmra), pmdec_masyr=round(pmdec),
                     pm_total_masyr=round(math.hypot(pmra, pmdec)), pm_consistent=consistent)
    cands.sort(key=lambda c: (not c.get("is_target", False), *_rank(c)))
    for i, c in enumerate(cands):
        c["id"] = f"c{i}"


def _sync_target_candidate(cands: list[dict], target: dict, grid: Grid, w) -> None:
    """The target verdict (measured at the known position, with every detector and season
    pair) is more reliable than event-level measurements at difference-image lobes. Make the
    candidate at the target agree with it, and fold residual blips around it into it."""
    c0 = grid.center_pix
    near = [c for c in cands if math.hypot(c["x"] - c0, c["y"] - c0) <= max(3.0, halo_radius(c["max_z"]))]
    summary = target["summary"]
    if summary not in ("mover", "variable"):
        return
    if near:
        t = min(near, key=lambda c: math.hypot(c["x"] - c0, c["y"] - c0))
    else:
        ra, dec = (float(v) for v in w.pixel_to_world_values(c0, c0))
        t = {"x": float(c0), "y": float(c0), "ra": round(ra, 6), "dec": round(dec, 6), "max_z": 0.0,
             "detectors": [], "n_events": 0, "events": [], "motion_check": target.get("motion_check", {})}
        cands.append(t)
    agree = [r for r in target["rows"] if r["kind"] == summary]
    ra, dec = (float(v) for v in w.pixel_to_world_values(c0, c0))
    t.update(kind=summary, is_target=True,
             x=float(c0), y=float(c0), ra=round(ra, 6), dec=round(dec, 6),  # the known target position
             detectors=sorted({r["detector"] for r in agree} | set(t["detectors"])),
             motion_check=target.get("motion_check", t.get("motion_check")),
             **evidence(summary, target["rows"], target.get("motion_check")))
    for k in ("pmra_masyr", "pmdec_masyr", "pm_total_masyr", "flux_ratio_min", "flux_ratio_max"):
        t.pop(k, None)
    if summary == "mover":
        t.update(pmra_masyr=target["pmra_masyr"], pmdec_masyr=target["pmdec_masyr"],
                 pm_total_masyr=round(math.hypot(target["pmra_masyr"], target["pmdec_masyr"])),
                 pm_consistent=bool(target.get("pm_consistent", True)))
    else:
        ratios = [r["flux_ratio"] for r in agree if r.get("flux_ratio")]
        if ratios:
            t.update(flux_ratio_min=min(ratios), flux_ratio_max=max(ratios))
    for c in near:
        if c is not t:
            _demote(c, "residual of the target's PSF")
    cands.sort(key=lambda c: (not c.get("is_target", False), *_rank(c)))
    for i, c in enumerate(cands):
        c["id"] = f"c{i}"


def independent_count(rows: list[dict], kind: str) -> tuple[int, int]:
    """(independent season comparisons, detectors) agreeing on `kind`.

    Season pairs that share a stack are not independent: three seasons give three pairs
    but only two independent comparisons. Per detector we therefore count
    (seasons involved in agreeing pairs) - 1.
    """
    by_det: dict[int, set[int]] = defaultdict(set)
    for r in rows:
        if r["kind"] == kind:
            by_det[r["detector"]].update((r["season_a"], r["season_b"]))
    return sum(len(s) - 1 for s in by_det.values()), len(by_det)


def evidence(kind: str, rows: list[dict], motion: dict | None = None) -> dict:
    """Evidence level from explicit checks. This is a ranking aid, NOT a calibrated
    probability: it has not been measured against a labelled sample."""
    n_ind, n_det = independent_count(rows, kind)
    checks = [
        {"label": "Seen in two or more detectors", "ok": n_det >= 2},
        {"label": "Two or more independent season comparisons", "ok": n_ind >= 2},
    ]
    if kind == "mover":
        coherent = (motion or {}).get("coherent")
        checks.append({
            "label": "Motion agrees in direction and speed across measurements",
            "ok": bool(coherent),
            **({"note": "only one measurement, cannot be checked"} if coherent is None else {}),
        })
    if kind in ("uncertain", "static"):
        level = "weak"
    elif all(c["ok"] for c in checks):
        level = "strong"
    elif n_ind >= 2 and all(c["ok"] for c in checks[2:]):
        level = "moderate"
    else:
        level = "weak"
    return {"evidence": level, "n_independent": n_ind, "n_detectors_agree": n_det, "checks": checks}


def _demote(c: dict, reason: str) -> None:
    c.update(kind="uncertain", evidence="weak", reason=reason)
    c.setdefault("checks", []).append({"label": reason.capitalize(), "ok": False})


def _rank(c: dict) -> tuple:
    return (KIND_RANK[c["kind"]], EVIDENCE_RANK[c["evidence"]], -c.get("n_independent", 0), -c["max_z"])


def matched_pair(key: str, bins: list, stacks: dict, det: int, sa: int, sb: int):
    """Wavelength-balanced stacks of seasons sa and sb for one detector, built from the
    wavelength bins that have enough exposures in BOTH seasons. Returns (A, B, info) or None."""
    use = [b for b in bins if b.detector == det and sa in b.seasons and sb in b.seasons]
    if not use:
        return None
    out = ch.wavelength_balanced(
        [stack_arrays(key, f"s{sa}_{b.id}") for b in use], [stack_arrays(key, f"s{sb}_{b.id}") for b in use]
    )
    if out is None:
        return None
    A, B, wts = out
    all_bins = [b for b in bins if b.detector == det]
    info = {
        "bins": [b.id for b in use],
        "bins_missing": [b.id for b in all_bins if b not in use],
        "lam_lo_um": min(b.lam_lo_um for b in use),
        "lam_hi_um": max(b.lam_hi_um for b in use),
        "mjd_a": float(sum(wi * stacks[f"s{sa}_{b.id}"]["mjd"] for wi, b in zip(wts, use))),
        "mjd_b": float(sum(wi * stacks[f"s{sb}_{b.id}"]["mjd"] for wi, b in zip(wts, use))),
    }
    return A, B, info


def _wavelength_support(info: dict, A: dict, B: dict, x: float, y: float) -> dict:
    """What wavelengths a two-season comparison actually used, for the evidence trail."""
    iy, ix = int(round(y)), int(round(x))

    def wave_at(m: dict) -> float:
        v = float(m["wave"][iy, ix])
        if math.isfinite(v):
            return v
        # masked (saturated) centre: use the usable pixels around it
        box = m["wave"][max(iy - 10, 0) : iy + 11, max(ix - 10, 0) : ix + 11]
        return float(np.nanmedian(box)) if np.isfinite(box).any() else float("nan")

    wa, wb = wave_at(A), wave_at(B)
    return {
        "bins": info["bins"], "bins_missing": info["bins_missing"],
        "lam_lo_um": info["lam_lo_um"], "lam_hi_um": info["lam_hi_um"],
        "wavelength_a_um": round(wa, 4) if math.isfinite(wa) else None,
        "wavelength_b_um": round(wb, 4) if math.isfinite(wb) else None,
    }


def halo_radius(max_z: float) -> float:
    """Merge radius (px) around a change of significance max_z."""
    return float(np.clip(2.0 + 2.0 * math.log10(max(max_z, 5.0) / 5.0), ch.MERGE_RADIUS_PX, 6.0))


def combine_pm(movers: list[dict]) -> tuple[float, float, bool]:
    """Inverse-variance mean proper motion of mover events (error ~ |pm| / shift_sigma),
    and whether all events point within 60 deg of the mean direction."""
    wts = [(e["shift_sigma"] / max(math.hypot(e["pmra_masyr"], e["pmdec_masyr"]), 1.0)) ** 2 for e in movers]
    pmra = sum(wi * e["pmra_masyr"] for wi, e in zip(wts, movers)) / sum(wts)
    pmdec = sum(wi * e["pmdec_masyr"] for wi, e in zip(wts, movers)) / sum(wts)
    ang0 = math.atan2(pmdec, pmra)
    consistent = all(
        abs((math.atan2(e["pmdec_masyr"], e["pmra_masyr"]) - ang0 + math.pi) % (2 * math.pi) - math.pi)
        < math.radians(60)
        for e in movers
    )
    return pmra, pmdec, consistent


def _target_verdict(key: str, grid: Grid, stacks: dict[str, dict], bins: list) -> dict:
    """Measure the source at the field centre in every detector, for every season pair,
    always in wavelength-balanced stacks. Pairs with no shared wavelength bin are
    reported as such: missing coverage is not evidence of anything."""
    c = grid.center_pix
    rows = []
    for det in range(1, 7):
        seasons = sorted(s["season"] for s in stacks.values()
                         if s["detector"] == det and s["bin"] is None and s.get("visit") is None)
        for sa, sb in itertools.combinations(seasons, 2):
            row = {"detector": det, "season_a": sa, "season_b": sb}
            pair = matched_pair(key, bins, stacks, det, sa, sb)
            if pair is None:
                row.update(kind="uncertain", no_comparison=True,
                           reason="no wavelength bin with 3 or more exposures in both seasons")
                rows.append(row)
                continue
            A, B, info = pair
            meas = ch.measure_pair(A, B, c, c, grid.pixscale_arcsec)
            v = ch.classify(meas)
            row.update(measure=meas, **v, **_wavelength_support(info, A, B, c, c))
            if v["kind"] == "mover":
                pmra, pmdec = ch.pm_from_shift(v["dx_px"], v["dy_px"], info["mjd_b"] - info["mjd_a"],
                                               grid.pixscale_arcsec)
                row.update(pmra_masyr=round(pmra), pmdec_masyr=round(pmdec))
            rows.append(row)
    motion = ch.reconcile_movers(rows)
    counts = {k: sum(r["kind"] == k for r in rows) for k in KIND_RANK}
    n_mover, _ = independent_count(rows, "mover")
    n_var, _ = independent_count(rows, "variable")
    # Mover/variable need two INDEPENDENT comparisons; a single one is only "possible".
    if n_mover >= 2:
        summary = "mover"
    elif n_var >= 2:
        summary = "variable"
    elif n_mover or n_var:
        summary = "possible " + ("mover" if n_mover else "variable")
    elif counts["static"]:
        summary = "static"
    else:
        summary = "no data"
    out = {"summary": summary, "rows": rows, "motion_check": motion,
           **evidence(summary.replace("possible ", ""), rows, motion)}
    movers = [r for r in rows if r["kind"] == "mover"]
    if movers:
        pmra, pmdec, consistent = combine_pm(movers)
        out.update(pmra_masyr=round(pmra), pmdec_masyr=round(pmdec), pm_consistent=consistent)
    return out


def _fast_movers(key: str, grid: Grid, frames: list[dict], assignment: dict[str, str], stacks: dict) -> list[dict]:
    w = grid.wcs()
    by_visit: dict[str, list[dict]] = defaultdict(list)
    for f in frames:
        ref_id = f"s{f['season']}_{assignment.get(f['id'])}"
        if ref_id not in stacks:
            ref_id = f"s{f['season']}_d{f['detector']}"
        if ref_id not in stacks:
            continue
        ref = stack_arrays(key, ref_id)
        arr = frame_arrays(key, f["id"])
        for lobe in ch.new_flux_detections(arr, ref):
            by_visit[f["visit"]].append({
                "t": f["mjd"], "x": lobe["x"], "y": lobe["y"], "z": lobe["z"],
                "frame_id": f["id"], "detector": f["detector"], "obs_id": f["obs_id"],
            })
    tracks = []
    for visit, dets in by_visit.items():
        for t in ch.link_visit(dets, grid.pixscale_arcsec):
            pts = []
            for p in t["members"]:
                ra, dec = (float(v) for v in w.pixel_to_world_values(p["x"], p["y"]))
                pts.append({**p, "ra": round(ra, 6), "dec": round(dec, 6), "z": round(p["z"], 1)})
            mjd = float(np.mean([p["t"] for p in pts]))
            tracks.append({
                "kind": t["kind"], "visit": visit, "mjd": mjd, "date": Time(mjd, format="mjd").isot[:19],
                "rate_arcsec_per_min": None if t["rate"] is None else round(t["rate"], 3),
                "pa_deg": None if t.get("pa") is None else round(t["pa"], 1),
                "max_z": max(p["z"] for p in pts), "points": pts,
            })
    tracks.sort(key=lambda t: ({"fast_mover": 0, "transient": 1, "stationary_transient": 2}[t["kind"]], -t["max_z"]))
    return tracks
