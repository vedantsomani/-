"""Adaptive wavelength bins for wavelength-matched comparison.

SPHEREx's linear variable filters mean every exposure samples the target at a
different wavelength. Comparing two epochs is only meaningful at (nearly) the
same wavelength, otherwise a red star "changes brightness" just because it was
seen at 4.9 um once and 4.5 um the next time.

Bins start from the 102 official channels (17 per detector) and contiguous
channels are merged, per detector, until every season that *can* contribute has
at least ``min_per_season`` exposures in the bin. A season "can" contribute if it
has that many exposures in the whole detector. Bins never cross detectors.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass

from .bands import BAND_EDGES_UM, N_PER_DETECTOR, band_index

MIN_PER_SEASON = 3
# Stop growing a bin once it is this many channels wide AND at least two seasons
# qualify, rather than widening it just to pull in a sparsely observed season.
SOFT_MAX_CHANNELS = 5


@dataclass
class WaveBin:
    id: str            # e.g. "d6b2"
    detector: int
    index: int         # bin number within the detector
    ch_lo: int         # first channel (0..101), inclusive
    ch_hi: int         # last channel, inclusive
    lam_lo_um: float
    lam_hi_um: float
    counts: dict[int, int]   # season -> exposures in the bin
    seasons: list[int]       # seasons with >= MIN_PER_SEASON exposures (comparable)

    @property
    def n_channels(self) -> int:
        return self.ch_hi - self.ch_lo + 1

    def to_dict(self) -> dict:
        lam_c = (self.lam_lo_um + self.lam_hi_um) / 2
        d = asdict(self)
        d.update(
            n_channels=self.n_channels,
            lam_center_um=round(lam_c, 4),
            width_um=round(self.lam_hi_um - self.lam_lo_um, 4),
            resolving_power=round(lam_c / (self.lam_hi_um - self.lam_lo_um), 1),
            counts={str(k): v for k, v in sorted(self.counts.items())},
        )
        return d


def frame_channel(meta: dict) -> int | None:
    return band_index(meta.get("wavelength_um"), meta["detector"])


def build_bins(frames: list[dict], min_per_season: int = MIN_PER_SEASON) -> tuple[list[WaveBin], dict[str, str]]:
    """Returns (bins, frame_id -> bin_id). Frames whose wavelength is unknown get no bin."""
    bins: list[WaveBin] = []
    assignment: dict[str, str] = {}
    for det in range(1, 7):
        dframes = [(f, frame_channel(f)) for f in frames if f["detector"] == det]
        dframes = [(f, c) for f, c in dframes if c is not None]
        if not dframes:
            continue
        season_tot = Counter(f["season"] for f, _ in dframes)
        eligible = {s for s, n in season_tot.items() if n >= min_per_season}
        per_ch: dict[int, Counter] = {}
        for f, c in dframes:
            per_ch.setdefault(c, Counter())[f["season"]] += 1

        first = (det - 1) * N_PER_DETECTOR
        channels = list(range(first, first + N_PER_DETECTOR))
        groups: list[list[int]] = []
        cur: list[int] = []
        acc: Counter = Counter()
        for ch in channels:
            cur.append(ch)
            acc.update(per_ch.get(ch, Counter()))
            ok = {s for s in eligible if acc[s] >= min_per_season}
            if ok == eligible or (len(cur) >= SOFT_MAX_CHANNELS and len(ok) >= 2):
                groups.append(cur)
                cur, acc = [], Counter()
        if cur:  # leftover red-end channels: fold into the previous bin if they can't stand alone
            ok = {s for s in eligible if acc[s] >= min_per_season}
            if groups and len(ok) < max(2, len(eligible)):
                groups[-1].extend(cur)
            else:
                groups.append(cur)

        # Drop empty channels at the edges of each bin so reported ranges are honest.
        spans = []
        for g in groups:
            used = [c for c in g if per_ch.get(c)]
            if used:
                spans.append((min(used), max(used)))
        for i, (lo, hi) in enumerate(spans):
            counts: Counter = Counter()
            for c in range(lo, hi + 1):
                counts.update(per_ch.get(c, Counter()))
            b = WaveBin(
                id=f"d{det}b{i}",
                detector=det,
                index=i,
                ch_lo=lo,
                ch_hi=hi,
                lam_lo_um=round(float(BAND_EDGES_UM[lo, 0]), 4),
                lam_hi_um=round(float(BAND_EDGES_UM[hi, 1]), 4),
                counts=dict(counts),
                seasons=sorted(s for s, n in counts.items() if n >= min_per_season),
            )
            bins.append(b)
            for f, c in dframes:
                if lo <= c <= hi:
                    assignment[f["id"]] = b.id
    return bins, assignment


def describe(bins: list[WaveBin]) -> str:
    """Human-readable bin table (used in logs and the milestone report)."""
    lines = []
    for b in bins:
        d = b.to_dict()
        lines.append(
            f"{b.id:6s} ch{b.ch_lo:3d}-{b.ch_hi:3d} ({b.n_channels} ch) {b.lam_lo_um:.3f}-{b.lam_hi_um:.3f} um "
            f"dlam={d['width_um']:.3f} R={d['resolving_power']:5.1f} counts={dict(sorted(b.counts.items()))} "
            f"comparable={b.seasons}"
        )
    return "\n".join(lines)
