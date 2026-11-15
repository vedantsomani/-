import numpy as np
import pytest

from app.bands import BAND_CENTERS_UM, BAND_DETECTOR, BAND_EDGES_UM, band_index


def test_layout():
    assert BAND_CENTERS_UM.size == 102
    assert np.all(np.diff(BAND_CENTERS_UM) > 0)
    for d in range(1, 7):
        assert (BAND_DETECTOR == d).sum() == 17
    # every centre lies inside its own edges
    assert np.all((BAND_EDGES_UM[:, 0] < BAND_CENTERS_UM) & (BAND_CENTERS_UM < BAND_EDGES_UM[:, 1]))


@pytest.mark.parametrize("i", [0, 16, 17, 50, 51, 84, 85, 101])
def test_centres_map_to_themselves(i):
    assert band_index(float(BAND_CENTERS_UM[i]), int(BAND_DETECTOR[i])) == i


def test_detector_disambiguates_overlap():
    # 1.105 um is at the D1/D2 boundary; the measuring detector decides.
    assert BAND_DETECTOR[band_index(1.105, 1)] == 1
    assert BAND_DETECTOR[band_index(1.108, 2)] == 2


def test_far_outside_is_none():
    assert band_index(2.0, 6) is None
    assert band_index(float("nan")) is None
