"""Find SPHEREx Level-2 images covering a position via IRSA's SIA2 service."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import time
import warnings
from dataclasses import asdict, dataclass

import httpx
from astropy.io.votable import parse_single_table

from .config import settings

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ImageRecord:
    collection: str
    obs_id: str          # e.g. 2025W19_1B_0116_1  ({year}W{week}_{segment}_{visit}_{exposure})
    detector: int        # 1..6
    access_url: str      # full L2 image; append ?center=..&size=.. for a cutout
    t_min: float         # MJD
    t_max: float         # MJD
    em_min_um: float     # detector band coverage (whole detector, not the target pixel)
    em_max_um: float

    @property
    def frame_id(self) -> str:
        return f"{self.obs_id}_D{self.detector}"

    @property
    def visit(self) -> str:
        return self.obs_id.rsplit("_", 1)[0]


def _sia_cache_path(collection: str, ra: float, dec: float, r: float):
    key = hashlib.sha1(f"{collection}|{ra:.6f}|{dec:.6f}|{r:.6f}".encode()).hexdigest()[:16]
    d = settings.cache_dir / "sia"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{collection}_{key}.json"


def _parse_votable(content: bytes, collection: str) -> list[ImageRecord]:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        table = parse_single_table(io.BytesIO(content)).to_table(use_names_over_ids=True)
    out: list[ImageRecord] = []
    for row in table:
        if str(row["dataproduct_subtype"]) != "science":
            continue
        band = str(row["energy_bandpassname"])  # "SPHEREx-D3"
        try:
            det = int(band.rsplit("D", 1)[-1])
        except ValueError:
            log.warning("unexpected bandpass name %r", band)
            continue
        out.append(
            ImageRecord(
                collection=collection,
                obs_id=str(row["obs_id"]),
                detector=det,
                access_url=str(row["access_url"]),
                t_min=float(row["t_min"]),
                t_max=float(row["t_max"]),
                em_min_um=float(row["em_min"]) * 1e6,
                em_max_um=float(row["em_max"]) * 1e6,
            )
        )
    return out


async def query_collection(
    client: httpx.AsyncClient, collection: str, ra: float, dec: float, radius: float, fresh: bool = False
) -> list[ImageRecord]:
    cache = _sia_cache_path(collection, ra, dec, radius)
    if not fresh and cache.exists() and time.time() - cache.stat().st_mtime < settings.sia_cache_ttl_s:
        return [ImageRecord(**d) for d in json.loads(cache.read_text())]

    params = {
        "COLLECTION": collection,
        "POS": f"CIRCLE {ra:.7f} {dec:.7f} {radius:.6f}",
        "RESPONSEFORMAT": "VOTABLE",
    }
    for attempt in range(settings.download_retries):
        try:
            resp = await client.get(settings.sia_url, params=params)
            resp.raise_for_status()
            break
        except httpx.HTTPError as exc:
            if attempt == settings.download_retries - 1:
                raise
            log.warning("SIA %s attempt %d failed: %s", collection, attempt + 1, exc)
            await asyncio.sleep(2**attempt)
    records = _parse_votable(resp.content, collection)
    cache.write_text(json.dumps([asdict(r) for r in records]))
    return records


async def find_images(
    client: httpx.AsyncClient, ra: float, dec: float, fresh: bool = False
) -> list[ImageRecord]:
    """Query every configured collection; de-duplicate exposures (first collection wins).
    ``fresh`` skips the short-lived result cache (and refreshes it) so new releases show up."""
    r = settings.sia_radius_deg
    results = await asyncio.gather(
        *(query_collection(client, c, ra, dec, r, fresh) for c in settings.collections),
        return_exceptions=True,
    )
    seen: dict[str, ImageRecord] = {}
    errors = []
    for coll, res in zip(settings.collections, results):
        if isinstance(res, BaseException):
            log.error("SIA query for %s failed: %s", coll, res)
            errors.append(res)
            continue
        for rec in res:
            seen.setdefault(rec.frame_id, rec)
    if errors and len(errors) == len(results):
        raise errors[0]
    return sorted(seen.values(), key=lambda r: (r.t_min, r.detector))
