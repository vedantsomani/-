"""The 102 SPHEREx spectral channels (17 per detector).

Band centres (um) are taken verbatim from the IRSA SPHEREx DR1 all-sky HiPS cube
(`data_cube_crval3_arr` in
https://irsa.ipac.caltech.edu/data/hips/SPHEREx/spherex_allsky_cube/properties).
Channel edges are midpoints between neighbouring centres; the outer edges of each
detector are extrapolated by half a channel. Used to bin per-exposure photometry
into a 102-point spectrum and to define narrow wavelength bins for blinking.
"""

from __future__ import annotations

import numpy as np

BAND_CENTERS_UM = np.array(
    [
        0.753, 0.771, 0.790, 0.809, 0.828, 0.848, 0.868, 0.889, 0.910, 0.932, 0.955, 0.978, 1.001, 1.025,
        1.050, 1.075, 1.102, 1.113, 1.139, 1.167, 1.195, 1.224, 1.254, 1.284, 1.315, 1.346, 1.378, 1.412,
        1.446, 1.481, 1.517, 1.554, 1.592, 1.631, 1.656, 1.695, 1.735, 1.775, 1.818, 1.861, 1.905, 1.949,
        1.995, 2.041, 2.089, 2.139, 2.189, 2.240, 2.292, 2.345, 2.399, 2.445, 2.506, 2.575, 2.646, 2.719,
        2.794, 2.871, 2.951, 3.033, 3.118, 3.204, 3.293, 3.383, 3.476, 3.570, 3.667, 3.768, 3.833, 3.867,
        3.901, 3.935, 3.970, 4.005, 4.040, 4.076, 4.112, 4.148, 4.184, 4.220, 4.257, 4.294, 4.331, 4.368,
        4.405, 4.435, 4.467, 4.500, 4.533, 4.566, 4.600, 4.635, 4.669, 4.704, 4.739, 4.773, 4.808, 4.843,
        4.879, 4.915, 4.951, 4.988,
    ]
)
assert BAND_CENTERS_UM.size == 102

N_PER_DETECTOR = 17
# Detector of each channel: channels 0-16 -> D1, 17-33 -> D2, ...
BAND_DETECTOR = np.repeat(np.arange(1, 7), N_PER_DETECTOR)


def _edges() -> np.ndarray:
    """(102, 2) array of [lo, hi) edges, computed within each detector."""
    edges = np.empty((102, 2))
    for d in range(6):
        c = BAND_CENTERS_UM[d * 17 : (d + 1) * 17]
        mid = (c[:-1] + c[1:]) / 2
        lo = np.concatenate([[c[0] - (mid[0] - c[0])], mid])
        hi = np.concatenate([mid, [c[-1] + (c[-1] - mid[-1])]])
        edges[d * 17 : (d + 1) * 17] = np.column_stack([lo, hi])
    return edges


BAND_EDGES_UM = _edges()


def band_index(wavelength_um: float, detector: int | None = None) -> int | None:
    """Index (0..101) of the channel containing `wavelength_um`.

    Detector bands overlap slightly at their ends; pass the detector to pick the
    channel belonging to the detector that actually made the measurement.
    """
    if wavelength_um is None or not np.isfinite(wavelength_um):
        return None
    idx = range(102) if detector is None else range((detector - 1) * 17, detector * 17)
    best, best_d = None, np.inf
    for i in idx:
        lo, hi = BAND_EDGES_UM[i]
        if lo <= wavelength_um < hi:
            return i
        d = min(abs(wavelength_um - lo), abs(wavelength_um - hi))
        if d < best_d:
            best, best_d = i, d
    # Slightly outside this detector's nominal range: snap to the nearest channel
    # only if within half a channel width.
    lo, hi = BAND_EDGES_UM[best]
    return best if best_d <= (hi - lo) / 2 else None
