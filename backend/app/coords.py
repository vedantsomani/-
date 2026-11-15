"""Coordinate parsing (decimal or sexagesimal) and name resolution via CDS Sesame."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from urllib.parse import quote
from dataclasses import asdict, dataclass

import astropy.units as u
import httpx
from astropy.coordinates import Angle, SkyCoord

from .config import settings

_DECIMAL = re.compile(r"^[+-]?\d+(\.\d*)?([eE][+-]?\d+)?$")


class CoordError(ValueError):
    pass


def parse_ra(text: str) -> float:
    """RA in decimal degrees, or sexagesimal hours ('08h55m10.8s', '08:55:10.8', '08 55 10.8')."""
    s = text.strip()
    try:
        if _DECIMAL.match(s):
            val = float(s)
        else:
            val = Angle(s, unit=u.hourangle).degree
    except Exception as exc:  # astropy raises several types
        raise CoordError(f"Cannot parse RA {text!r}") from exc
    if not 0 <= val < 360:
        raise CoordError(f"RA out of range: {val}")
    return float(val)


def parse_dec(text: str) -> float:
    """Dec in decimal degrees, or sexagesimal degrees ('-07d14m42.5s', '-07:14:42.5')."""
    s = text.strip()
    try:
        val = float(s) if _DECIMAL.match(s) else Angle(s, unit=u.deg).degree
    except Exception as exc:
        raise CoordError(f"Cannot parse Dec {text!r}") from exc
    if not -90 <= val <= 90:
        raise CoordError(f"Dec out of range: {val}")
    return float(val)


def parse_position(text: str) -> tuple[float, float] | None:
    """Parse a free-form 'RA Dec' string. Returns None if it isn't coordinates."""
    s = text.strip().replace(",", " ")
    tokens = s.split()
    if len(tokens) == 2 and all(_DECIMAL.match(t) for t in tokens):
        try:
            return parse_ra(tokens[0]), parse_dec(tokens[1])
        except CoordError:
            return None
    if not re.search(r"\d", s) or not re.search(r"[+-]?\d", s):
        return None
    try:
        c = SkyCoord(s, unit=(u.hourangle, u.deg), frame="icrs")
        return float(c.ra.deg), float(c.dec.deg)
    except Exception:
        return None


@dataclass
class Resolved:
    name: str
    ra: float
    dec: float
    source: str  # which resolver answered (Simbad, NED, VizieR) or "coordinates"
    otype: str | None = None
    pm_ra_masyr: float | None = None
    pm_dec_masyr: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _f(el: ET.Element | None) -> float | None:
    try:
        return float(el.text) if el is not None and el.text else None
    except ValueError:
        return None


async def resolve_name(name: str, client: httpx.AsyncClient) -> Resolved:
    pos = parse_position(name)
    if pos is not None:
        return Resolved(name=name, ra=pos[0], dec=pos[1], source="coordinates")

    resp = await client.get(f"{settings.sesame_url}?{quote(name.strip())}")
    resp.raise_for_status()
    root = ET.fromstring(resp.text)
    for resolver in root.iter("Resolver"):
        ra, dec = _f(resolver.find("jradeg")), _f(resolver.find("jdedeg"))
        if ra is None or dec is None:
            continue
        pm = resolver.find("pm")
        oname = resolver.find("oname")
        otype = resolver.find("otype")
        return Resolved(
            name=oname.text if oname is not None and oname.text else name,
            ra=ra,
            dec=dec,
            source=(resolver.get("name") or "Sesame").split("=")[-1].split(",")[0].strip(),
            otype=otype.text if otype is not None else None,
            pm_ra_masyr=_f(pm.find("pmRA")) if pm is not None else None,
            pm_dec_masyr=_f(pm.find("pmDE")) if pm is not None else None,
        )
    raise CoordError(f"Sesame could not resolve {name!r}")


# --- proper motion -----------------------------------------------------------
# Must match frontend/src/lib/coords.ts so pre-warmed fields share the browser's cache key.
J2000_MJD = 51544.5
SPHEREX_REF_MJD = 61000.0  # mid-survey epoch used to centre fields on fast movers
PM_THRESHOLD_MASYR = 200.0


def propagate(ra: float, dec: float, pmra: float, pmdec: float, from_mjd: float, to_mjd: float) -> tuple[float, float]:
    """Linear proper-motion propagation (pmra includes cos dec; mas/yr). Ignores parallax."""
    import math

    dt = (to_mjd - from_mjd) / 365.25
    d_dec = pmdec * dt / 3.6e6
    d_ra = pmra * dt / 3.6e6 / math.cos(math.radians(dec))
    return (ra + d_ra) % 360.0, dec + d_dec


def field_centre(r: "Resolved") -> tuple[float, float]:
    """Field centre the web app uses for a resolved object (SPHEREx-epoch position for fast movers)."""
    pmra, pmdec = r.pm_ra_masyr or 0.0, r.pm_dec_masyr or 0.0
    if (pmra**2 + pmdec**2) ** 0.5 > PM_THRESHOLD_MASYR:
        return propagate(r.ra, r.dec, pmra, pmdec, J2000_MJD, SPHEREX_REF_MJD)
    return r.ra, r.dec
