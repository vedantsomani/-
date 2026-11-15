import pytest

from app.coords import CoordError, parse_dec, parse_position, parse_ra

WISE0855 = (133.795129, -7.245147)


@pytest.mark.parametrize("ra", ["08h55m10.831s", "08:55:10.831", "08 55 10.831", "133.795129"])
def test_parse_ra(ra):
    assert parse_ra(ra) == pytest.approx(WISE0855[0], abs=1e-5)


@pytest.mark.parametrize("dec", ["-07d14m42.53s", "-07:14:42.53", "-7 14 42.53", "-7.245147"])
def test_parse_dec(dec):
    assert parse_dec(dec) == pytest.approx(WISE0855[1], abs=1e-5)


@pytest.mark.parametrize(
    "text",
    ["08h55m10.831s -07d14m42.53s", "08:55:10.831 -07:14:42.53", "133.795129 -7.245147", "133.795129, -7.245147"],
)
def test_parse_position(text):
    ra, dec = parse_position(text)
    assert (ra, dec) == pytest.approx(WISE0855, abs=1e-5)


@pytest.mark.parametrize("text", ["Barnard's Star", "RW Aur", "eps Ind B", "WISE 0855-0714"])
def test_names_are_not_coordinates(text):
    assert parse_position(text) is None


def test_out_of_range():
    with pytest.raises(CoordError):
        parse_ra("400")
    with pytest.raises(CoordError):
        parse_dec("-91")
