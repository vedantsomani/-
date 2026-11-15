"""Gaia DR3 cone searches through IRSA TAP (CDS is too flaky here), cached on disk.

A failed query never raises: it returns ``CatalogResult(ok=False, note=...)`` so the
caller degrades to "no neighbours known".
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from dataclasses import asdict, dataclass, field

from io import BytesIO
from pathlib import Path
import httpx

import numpy as np

from .config import settings

log = logging.getLogger(__name__)

GAIA_DR3_EPOCH_MJD = 57388.5  # J2016.0
CATALOG_NAME = "Gaia DR3"


@dataclass
class Star:
    source_id: str
    ra: float              # deg at ref epoch
    dec: float
    pmra: float            # mas/yr, includes cos(dec); 0 when Gaia has no PM
    pmdec: float
    epoch_mjd: float
    g_mag: float | None
    variable: bool | None  # phot_variable_flag: VARIABLE -> True, CONSTANT -> False, else None
    has_pm: bool = True
    parallax: float | None = None         # mas; None when Gaia has none (or an older cache lacks it)
    parallax_error: float | None = None   # mas


@dataclass
class CatalogResult:
    ok: bool
    stars: list[Star] = field(default_factory=list)
    note: str | None = None
    name: str = CATALOG_NAME


def _cache_path(ra: float, dec: float, radius_deg: float):
    key = hashlib.sha1(f"{settings.gaia_table}|{ra:.5f}|{dec:.5f}|{radius_deg:.5f}".encode()).hexdigest()[:16]
    d = settings.cache_dir / "catalogs" / "gaia_dr3"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{key}.json"


def _num(v) -> float | None:
    try:
        if v is None or np.ma.is_masked(v):
            return None
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def gaia_cone(ra: float, dec: float, radius_deg: float, need_parallax: bool = False) -> CatalogResult:
    """Blocking. Gaia DR3 sources within ``radius_deg`` of (ra, dec) at epoch J2016.
    Results cached before parallax was queried load with ``parallax=None``; with
    ``need_parallax`` such a cache is refreshed (falling back to it if the query fails)."""
    path = _cache_path(ra, dec, radius_deg)
    stale: CatalogResult | None = None
    if path.exists():
        try:
            data = json.loads(path.read_text())
            cached = CatalogResult(ok=True, stars=[Star(**s) for s in data["stars"]], note=data.get("note"))
            if not need_parallax or "parallax" in str(data.get("query", "")):
                return cached
            stale = cached
        except Exception:  # noqa: BLE001 — corrupt cache: re-query
            pass
    q = (
        "SELECT source_id, ra, dec, pmra, pmdec, ref_epoch, phot_g_mean_mag, phot_variable_flag, "
        "parallax, parallax_error "
        f"FROM {settings.gaia_table} WHERE CONTAINS(POINT('ICRS', ra, dec), "
        f"CIRCLE('ICRS', {ra:.7f}, {dec:.7f}, {radius_deg:.6f})) = 1"
    )
    try:
        import pyvo
        import requests

        class _Session(requests.Session):  # pyvo has no timeout knob; inject one
            def request(self, *a, **kw):
                kw.setdefault("timeout", settings.catalog_timeout_s)
                return super().request(*a, **kw)

        sess = _Session()
        sess.headers["User-Agent"] = "SPHEREx-Time-Machine/0.1 (NASA Space Apps 2026)"
        svc = pyvo.dal.TAPService(settings.tap_url, session=sess)
        table = svc.run_sync(q).to_table()
    except Exception as exc:  # noqa: BLE001
        log.warning("Gaia query failed: %s", exc)
        if stale is not None:
            return stale
        return CatalogResult(ok=False, note=f"Gaia DR3 query failed ({type(exc).__name__}); "
                                            "no neighbours known, the fit is target-only.")
    stars = []
    for r in table:
        ra_s, dec_s = _num(r["ra"]), _num(r["dec"])
        if ra_s is None or dec_s is None:
            continue
        pmra, pmdec = _num(r["pmra"]), _num(r["pmdec"])
        ep = _num(r["ref_epoch"]) or 2016.0
        flag = str(r["phot_variable_flag"]).strip().upper() if r["phot_variable_flag"] is not None else ""
        stars.append(Star(
            source_id=str(r["source_id"]), ra=ra_s, dec=dec_s,
            pmra=pmra or 0.0, pmdec=pmdec or 0.0,
            epoch_mjd=51544.5 + (ep - 2000.0) * 365.25,
            g_mag=_num(r["phot_g_mean_mag"]),
            variable=True if flag == "VARIABLE" else False if flag == "CONSTANT" else None,
            has_pm=pmra is not None and pmdec is not None,
            parallax=_num(r["parallax"]), parallax_error=_num(r["parallax_error"]),
        ))
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"query": q, "stars": [asdict(s) for s in stars]}))
    tmp.replace(path)
    return CatalogResult(ok=True, stars=stars)


# ---------------------------------------------------------------------------------- CatWISE2020
# IRSA table ``catwise_2020`` (columns checked against IRSA's catwise_colDescriptions): ra/dec in deg,
# pmra/pmdec/sigpmra/sigpmdec in ARCSEC/yr (x1000 -> mas/yr), w1mpro/w2mpro Vega mag. ``ra_pm``/``dec_pm``
# are the positions of the motion fit at MJD 57170; ``ra``/``dec`` are the no-motion positions at
# ``meanobsmjd``. We use ra_pm/dec_pm at 57170 together with pmra/pmdec, so position and motion belong
# to the same fit, and fall back to ra/dec at meanobsmjd when ra_pm is empty.
CATWISE_NAME = "CatWISE2020"
CATWISE_TABLE = "catwise_2020"
CATWISE_PM_EPOCH_MJD = 57170.0
CATWISE_PM_SNR_MIN = 5.0     # motion is used (drawn, matched, propagated) only above this S/N
CATWISE_RED_W1W2 = 0.8       # W1-W2 above this is "red": brown-dwarf-like


@dataclass
class CatwiseSource:
    name: str
    ra: float              # deg at epoch_mjd
    dec: float
    epoch_mjd: float
    pmra: float            # mas/yr (pmra x cos dec, as published), 0 when absent
    pmdec: float
    pm_snr: float | None   # total-motion S/N; None when the uncertainties are missing
    w1: float | None       # Vega mag
    w2: float | None

    @property
    def pm_total(self) -> float:
        return math.hypot(self.pmra, self.pmdec)

    @property
    def moving(self) -> bool:
        """Motion significant enough to use (S/N > CATWISE_PM_SNR_MIN)."""
        return self.pm_snr is not None and self.pm_snr > CATWISE_PM_SNR_MIN

    @property
    def w1_w2(self) -> float | None:
        return None if self.w1 is None or self.w2 is None else self.w1 - self.w2


@dataclass
class CatwiseResult:
    ok: bool
    sources: list[CatwiseSource] = field(default_factory=list)
    note: str | None = None
    name: str = CATWISE_NAME


def pm_snr_of(pmra: float, pmdec: float, sig_pmra: float | None, sig_pmdec: float | None) -> float | None:
    """S/N of the total motion: |mu| over the uncertainty projected on the motion direction."""
    mu = math.hypot(pmra, pmdec)
    if sig_pmra is None or sig_pmdec is None or mu <= 0:
        return None
    sig = math.hypot(pmra * sig_pmra, pmdec * sig_pmdec) / mu
    return mu / sig if sig > 0 else None


def catwise_source_from_row(r) -> CatwiseSource | None:
    """One TAP row (arcsec/yr motions) -> CatwiseSource (mas/yr), or None without a usable position."""
    ra, dec = _num(r["ra_pm"]), _num(r["dec_pm"])
    epoch = CATWISE_PM_EPOCH_MJD
    if ra is None or dec is None:
        ra, dec, epoch = _num(r["ra"]), _num(r["dec"]), _num(r["meanobsmjd"]) or CATWISE_PM_EPOCH_MJD
    if ra is None or dec is None:
        return None
    pmra, pmdec = _num(r["pmra"]), _num(r["pmdec"])
    sa, sd = _num(r["sigpmra"]), _num(r["sigpmdec"])
    pmra_m = pmra * 1000.0 if pmra is not None else 0.0
    pmdec_m = pmdec * 1000.0 if pmdec is not None else 0.0
    snr = pm_snr_of(pmra_m, pmdec_m, sa * 1000.0 if sa is not None else None, sd * 1000.0 if sd is not None else None)
    return CatwiseSource(
        name=str(r["source_name"]).strip(), ra=ra, dec=dec, epoch_mjd=float(epoch),
        pmra=pmra_m, pmdec=pmdec_m, pm_snr=snr, w1=_num(r["w1mpro"]), w2=_num(r["w2mpro"]),
    )


def _catwise_cache_path(ra: float, dec: float, radius_deg: float):
    key = hashlib.sha1(f"{CATWISE_TABLE}|{ra:.5f}|{dec:.5f}|{radius_deg:.5f}".encode()).hexdigest()[:16]
    d = settings.cache_dir / "catalogs" / "catwise2020"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{key}.json"


def catwise_cone(ra: float, dec: float, radius_deg: float) -> CatwiseResult:
    """Blocking. CatWISE2020 sources within ``radius_deg`` of (ra, dec), disk-cached. Never raises:
    a failed query gives ``CatwiseResult(ok=False, note=...)``."""
    path = _catwise_cache_path(ra, dec, radius_deg)
    if path.exists():
        try:
            data = json.loads(path.read_text())
            return CatwiseResult(ok=True, sources=[CatwiseSource(**s) for s in data["sources"]])
        except Exception:  # noqa: BLE001 — corrupt cache: re-query
            pass
    q = (
        "SELECT source_name, ra, dec, ra_pm, dec_pm, pmra, pmdec, sigpmra, sigpmdec, w1mpro, w2mpro, meanobsmjd "
        f"FROM {CATWISE_TABLE} WHERE CONTAINS(POINT('ICRS', ra, dec), "
        f"CIRCLE('ICRS', {ra:.7f}, {dec:.7f}, {radius_deg:.6f})) = 1"
    )
    try:
        import pyvo
        import requests

        class _Session(requests.Session):
            def request(self, *a, **kw):
                kw.setdefault("timeout", settings.catalog_timeout_s)
                return super().request(*a, **kw)

        sess = _Session()
        sess.headers["User-Agent"] = "SPHEREx-Time-Machine/0.1 (NASA Space Apps 2026)"
        table = pyvo.dal.TAPService(settings.tap_url, session=sess).run_sync(q).to_table()
        table.rename_columns(table.colnames, [c.lower() for c in table.colnames])
        sources = [s for s in (catwise_source_from_row(r) for r in table) if s is not None]
    except Exception as exc:  # noqa: BLE001
        log.warning("CatWISE2020 query failed: %s", exc)
        return CatwiseResult(ok=False, note=f"CatWISE2020 query failed ({type(exc).__name__}); "
                                            "red and moving sources from CatWISE are not shown.")
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"query": q, "sources": [asdict(s) for s in sources]}))
    tmp.replace(path)
    return CatwiseResult(ok=True, sources=sources)

# AllWISE Source Catalog (allwise_p3as_psd): positions at the mean epoch of the WISE
# observations; motion-fit positions use MJD 55400.0. Four-band photometry, apparent
# motion fits and a per-band variability flag.
WISE_CATALOG_NAME = "AllWISE"
WISE_EPOCH_MJD = 55400.0       # AllWISE motion-fit reference epoch (2010.5589)
WISE_PM_SIGMA_MAX = 300.0
WISE_CACHE_VERSION = 2
WISE_COLUMNS = (
    "designation,ra,dec,ra_pm,dec_pm,pmra,pmdec,sigpmra,sigpmdec,"
    "w1mpro,w2mpro,w3mpro,w4mpro,var_flg,ph_qual,w1mjdmean,w2mjdmean"
)
WISE_VAR_FLAG_VARIABLE = 6      # per-band char flag; >= 6 is a high-probability variable



@dataclass
class WiseSource:
    designation: str
    ra: float              # deg at epoch_mjd; motion fit or mean observation epoch
    dec: float
    epoch_mjd: float = WISE_EPOCH_MJD
    pmra: float | None = None           # mas/yr (east / mu_alpha cos(dec)); None when no fit
    pmdec: float | None = None
    sigpmra: float | None = None
    sigpmdec: float | None = None
    w1: float | None = None             # 3.4 um
    w2: float | None = None             # 4.6 um
    w3: float | None = None             # 12.0 um
    w4: float | None = None             # 22.0 um
    var_flg: str | None = None          # 4-char string, one band digit 0-9 ('n' = unknown)
    ph_qual: str | None = None         # 'A'.. or upper limit marker


@dataclass
class WiseResult:
    ok: bool
    sources: list[WiseSource] = field(default_factory=list)
    note: str | None = None
    name: str = WISE_CATALOG_NAME



def _wise_cache_path(ra: float, dec: float, radius_deg: float) -> Path:
    key = hashlib.sha1(f"{WISE_CACHE_VERSION}|{settings.wise_table}|{ra:.5f}|{dec:.5f}|{radius_deg:.5f}".encode()).hexdigest()[:16]
    d = settings.cache_dir / "catalogs" / settings.wise_table
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{key}.json"


def _col(row, name: str):
    try:
        return _num(row[name])
    except KeyError:
        return None


def _parse_wise_sources(text: str) -> list[WiseSource]:
    """Parse the IPAC-fixed-width table that IRSA Gator returns (outfmt=1, Cone search)."""
    from astropy.io import ascii as astro_ascii

    table = astro_ascii.read(BytesIO(text.encode("utf-8")), format="ipac")
    sources: list[WiseSource] = []
    for row in table:
        ra, dec = _col(row, "ra"), _col(row, "dec")
        if ra is None or dec is None:
            continue
        pmra, pmdec = _col(row, "pmra"), _col(row, "pmdec")
        sr, sd = _col(row, "sigpmra"), _col(row, "sigpmdec")
        fit_ra, fit_dec = _col(row, "ra_pm"), _col(row, "dec_pm")
        reliable = all(x is not None for x in (fit_ra, fit_dec, pmra, pmdec, sr, sd))
        reliable = reliable and 0 < sr <= WISE_PM_SIGMA_MAX and 0 < sd <= WISE_PM_SIGMA_MAX
        if reliable:
            ra, dec, epoch = fit_ra, fit_dec, WISE_EPOCH_MJD
        else:
            # Keep the stationary fit at its observed epoch. Unknown motion is not a valid fit.
            epoch = _col(row, "w2mjdmean") or _col(row, "w1mjdmean") or WISE_EPOCH_MJD
            pmra, pmdec = None, None
        sources.append(WiseSource(
            designation=str(row["designation"]),
            ra=ra, dec=dec, epoch_mjd=epoch,
            pmra=pmra, pmdec=pmdec, sigpmra=sr, sigpmdec=sd,
            w1=_col(row, "w1mpro"),
            w2=_col(row, "w2mpro"),
            w3=_col(row, "w3mpro"),
            w4=_col(row, "w4mpro"),
            var_flg=str(row["var_flg"]) if row["var_flg"] is not None and not np.ma.is_masked(row["var_flg"]) else None,
            ph_qual=str(row["ph_qual"]) if row["ph_qual"] is not None and not np.ma.is_masked(row["ph_qual"]) else None,
        ))
    return sources


def wise_cone(ra: float, dec: float, radius_deg: float, margin_arcsec: float = 0.0) -> WiseResult:
    """Blocking. AllWISE sources within ``radius_deg`` of (ra, dec) (plus a mover margin).
    Results cached before; a failed query never raises (ok=False with a note)."""
    radius_deg = radius_deg + margin_arcsec / 3600.0
    path = _wise_cache_path(ra, dec, radius_deg)
    sources: list[WiseSource] | None = None
    if path.exists():
        try:
            data = json.loads(path.read_text())
            sources = [WiseSource(**s) for s in data["sources"]]
        except Exception:  # noqa: BLE001 — corrupt cache: re-query
            sources = None
    if sources is None:
        url = (
            f"{settings.gator_url}?catalog={settings.wise_table}&spatial=Cone&radius={radius_deg * 3600:.1f}"
            f"&radunits=arcsec&objstr={ra:.7f},{dec:.7f}&coordsys=Equatorial&outfmt=1"
            f"&selcols={WISE_COLUMNS}"
        )
        try:
            resp = httpx.get(url, timeout=settings.catalog_timeout_s,
                             headers={"User-Agent": "SPHEREx-Time-Machine/0.1 (NASA Space Apps 2026)"})
            resp.raise_for_status()
            sources = _parse_wise_sources(resp.text)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"query": url, "sources": [asdict(s) for s in sources]}))
            tmp.replace(path)
        except Exception as exc:  # noqa: BLE001
            log.warning("AllWISE query failed: %s", exc)
            return WiseResult(ok=False, note=f"AllWISE source query failed ({type(exc).__name__}); "
                                           "no AllWISE sources drawn on the image.")
    return WiseResult(ok=True, sources=sources)
