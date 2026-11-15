"""Build SPHEREx cutouts from the AWS open-data copy of the Level-2 files with HTTP range
requests, instead of asking the (slow, 10-16 s per request, often 503) IRSA cutout service.

A Level-2 file is ~66 MB of FITS: PRIMARY, IMAGE, VARIANCE, ZODI (2040 x 2040 float32, plain),
FLAGS (int32, RICE_1 tile-compressed with one tile per image row), EPSF and the small WCS-WAVE
table. Image rows are contiguous on disk, so the pixel rows covering the cutout box are ONE byte
range per image HDU (for FLAGS: the per-row tile descriptors, then one range of the heap holding
those tiles, decompressed by astropy); the columns are cut in memory. The result is a FITS byte
string shaped like ``strip_cutout()`` output for an IRSA cutout (same HDUs, plain-image FLAGS,
headers with the reference pixels of the primary and the 'A'/'W' alternate WCSs shifted by the
cutout origin), so everything downstream is unchanged.

Per exposure: the HDU headers are located by walking the file (a few small sequential ranges);
the layout is remembered per (pipeline, detector) directory, so later exposures fetch all headers
in one parallel round trip at the predicted offsets (validated, else walked again) and then the
pixel rows in a second one.
"""

from __future__ import annotations

import asyncio
import io
import logging
import math
import random
import re
import warnings
from dataclasses import dataclass
from typing import Protocol

import httpx
import numpy as np
from astropy.io import fits
from astropy.wcs import WCS, FITSFixedWarning

from .config import settings

log = logging.getLogger(__name__)

BLOCK = 2880
IMAGE_HDUS = ("IMAGE", "FLAGS", "VARIANCE", "ZODI")
TABLE_HDUS = ("WCS-WAVE",)
NEEDED = IMAGE_HDUS + TABLE_HDUS
_IRSA_PREFIX = "/ibe/data/spherex/"
# Box rule (see cutout_box) reproduces IRSA's cutout box exactly for all 258 exposures of the
# WISE 0855 field: per axis n = round(size / axis pixel scale), first pixel
# floor(c - (n - 1)/2 + 0.5) around the SIP-projected centre c, clipped to the detector.
_DROP = ("CHECKSUM", "DATASUM", "ZHECKSUM", "ZDATASUM")
SOURCE_KEY = "CUTSRC"  # primary-header marker of an S3-built cutout


def cached_source(data: bytes) -> str:
    """'s3' or 'irsa' for a cached (stripped) cutout, from the primary-header marker."""
    mark = f"{SOURCE_KEY:<8}= 's3".encode()
    for i in range(0, min(len(data), 1024 * BLOCK), 80):
        card = data[i:i + 80]
        if card.startswith(mark):
            return "s3"
        if card.startswith(b"END     "):
            break
    return "irsa"


class S3CutoutError(RuntimeError):
    """The S3 path cannot serve this cutout (caller falls back to IRSA)."""


class RangeReader(Protocol):
    async def read(self, url: str, start: int, stop: int) -> bytes:
        """Bytes [start, stop) of ``url`` (fewer only at end of file)."""


def s3_url(access_url: str) -> str:
    i = access_url.find(_IRSA_PREFIX)
    if i < 0:
        raise S3CutoutError(f"not an IRSA SPHEREx URL: {access_url}")
    return settings.spherex_s3_base.rstrip("/") + "/" + access_url[i + len(_IRSA_PREFIX):]


def _pad(n: int) -> int:
    return -(-n // BLOCK) * BLOCK


# --------------------------------------------------------------------------- headers / layout

@dataclass
class HduInfo:
    index: int
    name: str
    header_offset: int
    header_len: int          # padded to 2880
    data_len: int            # unpadded (incl. heap)
    header: fits.Header
    tail: bytes = b""        # bytes read past the header (start of the data), if any

    @property
    def data_offset(self) -> int:
        return self.header_offset + self.header_len

    @property
    def next_offset(self) -> int:
        return self.data_offset + _pad(self.data_len)

    @property
    def compressed(self) -> bool:
        return bool(self.header.get("ZIMAGE", False))

    def prefix_len(self) -> int:
        """Data bytes worth reading together with the header: whole small tables, the tile
        descriptor table of a compressed image."""
        if self.name in TABLE_HDUS:
            return self.data_len
        if self.compressed:
            return int(self.header["NAXIS1"]) * int(self.header["NAXIS2"])
        return 0


def parse_header(buf: bytes, pos: int = 0) -> tuple[fits.Header, int] | None:
    """Header starting at ``buf[pos]`` and its padded length, or None if END is not in ``buf``."""
    i = pos
    while i + 80 <= len(buf):
        if buf[i:i + 8] == b"END     ":
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                hdr = fits.Header.fromstring(buf[pos:i + 80].decode("ascii", "replace"))
            return hdr, _pad(i + 80 - pos)
        i += 80
    return None


def data_length(hdr: fits.Header) -> int:
    naxis = int(hdr.get("NAXIS", 0))
    if naxis == 0:
        return 0
    n = 1
    for k in range(1, naxis + 1):
        n *= int(hdr[f"NAXIS{k}"])
    return abs(int(hdr["BITPIX"])) // 8 * int(hdr.get("GCOUNT", 1)) * (int(hdr.get("PCOUNT", 0)) + n)


def _hdu_name(hdr: fits.Header, index: int) -> str:
    return "PRIMARY" if index == 0 else str(hdr.get("EXTNAME", "")).strip().upper()


def _info(index: int, off: int, buf: bytes) -> HduInfo | None:
    got = parse_header(buf)
    if got is None:
        return None
    hdr, hlen = got
    return HduInfo(index, _hdu_name(hdr, index), off, hlen, data_length(hdr), hdr, buf[hlen:])


async def _hdu_at(reader: RangeReader, url: str, index: int, off: int, guess: int) -> HduInfo:
    """The HDU whose header starts at ``off`` (reads ``guess`` bytes, more until END appears)."""
    buf = b""
    size = max(guess, BLOCK)
    while True:
        chunk = await reader.read(url, off + len(buf), off + size)
        if not chunk:
            raise S3CutoutError(f"unexpected end of file at byte {off + len(buf)}")
        buf += chunk
        info = _info(index, off, buf)
        if info is not None:
            return info
        if len(buf) < size:
            raise S3CutoutError("header without END card")
        size *= 2
        if size > 4 * 1024 * 1024:
            raise S3CutoutError("header too large")


async def walk_layout(reader: RangeReader, url: str) -> dict[str, HduInfo]:
    """Locate PRIMARY and every needed HDU by walking headers (one small range per HDU)."""
    out: dict[str, HduInfo] = {}
    off, index, guess = 0, 0, 64 * 1024
    while not all(n in out for n in NEEDED):
        info = await _hdu_at(reader, url, index, off, guess)
        if index == 0 and str(info.header.cards[0].keyword) != "SIMPLE":
            raise S3CutoutError("not a FITS file")
        out.setdefault(info.name, info)
        off, index, guess = info.next_offset, index + 1, 48 * 1024
        if index > 64:
            raise S3CutoutError("needed HDUs not found")
    return out


async def guessed_layout(reader: RangeReader, url: str, template: dict[str, HduInfo]) -> dict[str, HduInfo] | None:
    """Read the headers at the offsets of a previously walked file with the same layout key, in
    parallel batches. A tile-compressed HDU (FLAGS) has a file-specific heap size, so it ends a
    batch: later HDUs are read at the template offsets shifted by the measured size difference
    (two round trips for a real L2 file). None if anything differs (header length, name, size)."""
    names = sorted(["PRIMARY", *NEEDED], key=lambda n: template[n].index)
    out: dict[str, HduInfo] = {}
    delta = 0

    async def one(name: str, shift: int) -> HduInfo | None:
        t = template[name]
        off = t.header_offset + shift
        buf = await reader.read(url, off, off + t.header_len + t.prefix_len())
        info = _info(t.index, off, buf)
        if info is None or info.header_len != t.header_len or info.name != name:
            return None
        if not _variable(t) and info.data_len != t.data_len:
            return None
        if t.index == 0 and str(info.header.cards[0].keyword) != "SIMPLE":
            return None
        return info

    i = 0
    while i < len(names):
        batch = []
        for n in names[i:]:
            batch.append(n)
            if _variable(template[n]):
                break
        res = await asyncio.gather(*(one(n, delta) for n in batch))
        if any(r is None for r in res):
            return None
        for n, r in zip(batch, res):
            out[n] = r
            delta += _pad(r.data_len) - _pad(template[n].data_len)
        i += len(batch)
    return out


def _variable(info: HduInfo) -> bool:
    """Data size may differ between files of the same layout (heap of a compressed image)."""
    return info.compressed or int(info.header.get("PCOUNT", 0)) > 0 and info.name not in TABLE_HDUS


# --------------------------------------------------------------------------- geometry

def _celestial(hdr: fits.Header) -> WCS:
    from .processing import _celestial_header

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", FITSFixedWarning)
        return WCS(_celestial_header(hdr))


def cutout_box(hdr: fits.Header, ra: float, dec: float, size_deg: float,
               shape: tuple[int, int] | None = None) -> tuple[int, int, int, int]:
    """(x0, x1, y0, y1), 0-based, end-exclusive, clipped to the image (``shape`` = (ny, nx),
    default from NAXIS1/2). Empty if no overlap."""
    ny, nx = shape if shape is not None else (int(hdr["NAXIS2"]), int(hdr["NAXIS1"]))
    w = _celestial(hdr)
    try:
        cx, cy = (float(v) for v in w.all_world2pix(ra, dec, 0))
    except Exception as exc:  # noqa: BLE001 — e.g. SIP inversion does not converge far off-chip
        raise S3CutoutError(f"target does not project onto the detector: {exc}") from exc
    if not (math.isfinite(cx) and math.isfinite(cy)):
        raise S3CutoutError("target does not project onto the detector plane")
    m = w.wcs.cd if w.wcs.has_cd() else w.wcs.get_pc() * w.wcs.cdelt[:, None]

    def axis(c: float, j: int, n_max: int) -> tuple[int, int]:
        n = round(size_deg / math.hypot(*m[:, j]))  # pixels along this axis
        a = math.floor(c - (n - 1) / 2 + 0.5)
        lo, hi = max(a, 0), min(a + n, n_max)
        return lo, max(hi, lo)

    x0, x1 = axis(cx, 0, nx)
    y0, y1 = axis(cy, 1, ny)
    return x0, x1, y0, y1


def shift_header(hdr: fits.Header, x0: int, y0: int) -> fits.Header:
    """Cutout header: reference pixels of the primary and alternate WCSs shifted by the origin."""
    h = hdr.copy()
    for suffix in ("", "A", "W"):
        for axis, d in ((1, x0), (2, y0)):
            k = f"CRPIX{axis}{suffix}"
            if k in h:
                h[k] = float(h[k]) - d
    for k in _DROP:
        h.remove(k, ignore_missing=True)
    return h


# --------------------------------------------------------------------------- pixel reads

_DTYPES = {8: ">u1", 16: ">i2", 32: ">i4", 64: ">i8", -32: ">f4", -64: ">f8"}


def image_shape(info: HduInfo) -> tuple[int, int]:
    """(ny, nx) of a plain or tile-compressed image HDU; raises for anything unsupported."""
    h = info.header
    if info.compressed:
        if int(h.get("ZNAXIS", 0)) != 2 or int(h.get("TFIELDS", 0)) != 1:
            raise S3CutoutError(f"unsupported compressed layout in {info.name}")
        if int(h.get("ZTILE1", -1)) != int(h["ZNAXIS1"]):
            raise S3CutoutError(f"{info.name}: tiles narrower than a row")
        if not str(h.get("TFORM1", "")).strip().upper().startswith(("1P", "1Q")):
            raise S3CutoutError(f"{info.name}: unexpected TFORM1")
        return int(h["ZNAXIS2"]), int(h["ZNAXIS1"])
    if str(h.get("XTENSION", "")).strip() != "IMAGE":
        raise S3CutoutError(f"{info.name} is not an image HDU")
    if int(h.get("NAXIS", 0)) != 2 or int(h.get("PCOUNT", 0)) != 0 or int(h.get("GCOUNT", 1)) != 1:
        raise S3CutoutError(f"unexpected {info.name} layout")
    if float(h.get("BZERO", 0)) != 0 or float(h.get("BSCALE", 1)) != 1:
        raise S3CutoutError(f"{info.name} is scaled (BZERO/BSCALE)")
    if int(h["BITPIX"]) not in _DTYPES:
        raise S3CutoutError(f"unsupported BITPIX {h['BITPIX']}")
    return int(h["NAXIS2"]), int(h["NAXIS1"])


async def _prefix(reader: RangeReader, url: str, info: HduInfo) -> bytes:
    n = info.prefix_len()
    if len(info.tail) >= n:
        return info.tail[:n]
    buf = await reader.read(url, info.data_offset, info.data_offset + n)
    if len(buf) != n:
        raise S3CutoutError(f"short read for {info.name}")
    return buf


async def read_plain_rows(reader: RangeReader, url: str, info: HduInfo, y0: int, y1: int) -> tuple[np.ndarray, fits.Header]:
    ny, nx = image_shape(info)
    dt = np.dtype(_DTYPES[int(info.header["BITPIX"])])
    rb = nx * dt.itemsize
    start = info.data_offset + y0 * rb
    buf = await reader.read(url, start, start + (y1 - y0) * rb)
    if len(buf) != (y1 - y0) * rb:
        raise S3CutoutError(f"short read for {info.name}")
    return np.frombuffer(buf, dtype=dt).reshape(y1 - y0, nx), info.header


async def read_compressed_rows(reader: RangeReader, url: str, info: HduInfo, y0: int, y1: int) -> tuple[np.ndarray, fits.Header]:
    """Rows [y0, y1) of a tile-compressed image whose tiles are whole rows: fetch the tile
    descriptors, ONE heap range covering the needed tiles, and let astropy decompress a
    re-based miniature of the HDU. Returns (rows, image-style header)."""
    h = info.header
    ny, nx = image_shape(info)
    tile_h = int(h.get("ZTILE2", 1))
    t0, t1 = y0 // tile_h, (y1 - 1) // tile_h + 1
    wide = str(h["TFORM1"]).strip().upper().startswith("1Q")
    desc = np.frombuffer(await _prefix(reader, url, info), dtype=">i8" if wide else ">i4").reshape(-1, 2)
    if desc.shape[0] != int(h["NAXIS2"]):
        raise S3CutoutError(f"{info.name}: bad descriptor table")
    d = desc[t0:t1].astype(np.int64)
    lo, hi = int(d[:, 1].min()), int((d[:, 1] + d[:, 0]).max())
    heap0 = info.data_offset + int(h.get("THEAP", int(h["NAXIS1"]) * int(h["NAXIS2"])))
    heap = await reader.read(url, heap0 + lo, heap0 + hi)
    if len(heap) != hi - lo:
        raise S3CutoutError(f"short heap read for {info.name}")
    rows_in = min(t1 * tile_h, ny) - t0 * tile_h
    mh = h.copy()
    for k in (*_DROP, "THEAP"):
        mh.remove(k, ignore_missing=True)
    mh["NAXIS2"] = t1 - t0
    mh["PCOUNT"] = hi - lo
    mh["ZNAXIS2"] = rows_in
    table = np.column_stack([d[:, 0], d[:, 1] - lo]).astype(">i8" if wide else ">i4").tobytes()
    blob = mh.tostring().encode("ascii") + table + heap
    prim = fits.PrimaryHDU().header.tostring().encode("ascii")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with fits.open(io.BytesIO(prim + blob + bytes(_pad(len(blob)) - len(blob))),
                       disable_image_compression=False) as hl:
            hdu = hl[1]
            if not isinstance(hdu, fits.CompImageHDU):
                raise S3CutoutError(f"{info.name}: astropy did not recognise the compressed image")
            data = np.array(hdu.data)
            img_hdr = hdu.header.copy()
    r0 = y0 - t0 * tile_h
    return data[r0:r0 + (y1 - y0)], img_hdr


async def read_rows(reader: RangeReader, url: str, info: HduInfo, y0: int, y1: int) -> tuple[np.ndarray, fits.Header]:
    if info.compressed:
        return await read_compressed_rows(reader, url, info, y0, y1)
    return await read_plain_rows(reader, url, info, y0, y1)


# --------------------------------------------------------------------------- build

async def build_from_layout(reader: RangeReader, url: str, layout: dict[str, HduInfo],
                            ra: float, dec: float, size_deg: float) -> tuple[bytes, dict]:
    shapes = {n: image_shape(layout[n]) for n in IMAGE_HDUS}
    shape = shapes["IMAGE"]
    if any(s != shape for s in shapes.values()):
        raise S3CutoutError("image HDUs differ in shape")
    x0, x1, y0, y1 = cutout_box(layout["IMAGE"].header, ra, dec, size_deg, shape)
    if x1 - x0 < 2 or y1 - y0 < 2:
        raise S3CutoutError("cutout box does not overlap the detector")

    async def table(name: str) -> fits.BinTableHDU:
        info = layout[name]
        blob = info.header.tostring().encode("ascii") + await _prefix(reader, url, info)
        prim = fits.PrimaryHDU().header.tostring().encode("ascii")
        with fits.open(io.BytesIO(prim + blob + bytes(_pad(len(blob)) - len(blob)))) as th:
            t = th[1]
            return fits.BinTableHDU(data=t.data.copy(), header=t.header.copy())

    res = await asyncio.gather(*(read_rows(reader, url, layout[n], y0, y1) for n in IMAGE_HDUS),
                               *(table(n) for n in TABLE_HDUS))
    got, tables = res[:len(IMAGE_HDUS)], res[len(IMAGE_HDUS):]
    data = await asyncio.to_thread(_assemble, layout["PRIMARY"].header, got, tables, x0, x1, y0)
    return data, {"box": [x0, x1, y0, y1]}


def _assemble(primary: fits.Header, got: list, tables: list, x0: int, x1: int, y0: int) -> bytes:
    prim_hdr = primary.copy()
    for k in _DROP:
        prim_hdr.remove(k, ignore_missing=True)
    prim_hdr[SOURCE_KEY] = ("s3", "cutout built from AWS open-data L2 file (s3cutout.py)")
    hdus: list = [fits.PrimaryHDU(header=prim_hdr)]
    for (rows, hdr) in got:
        hdus.append(fits.ImageHDU(data=np.ascontiguousarray(rows[:, x0:x1]), header=shift_header(hdr, x0, y0)))
    hdus.extend(tables)
    buf = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        fits.HDUList(hdus).writeto(buf, output_verify="silentfix")
    return buf.getvalue()


def validate(data: bytes) -> None:
    """Sanity checks on a built cutout before it stands in for an IRSA one."""
    with fits.open(io.BytesIO(data)) as h:
        names = [x.name for x in h]
        for n in NEEDED:
            if n not in names:
                raise S3CutoutError(f"built cutout lacks {n}")
        shape = h["IMAGE"].data.shape
        if min(shape) < 2 or any(h[n].data.shape != shape for n in IMAGE_HDUS):
            raise S3CutoutError("built cutout has inconsistent shapes")
        if not np.isfinite(h["IMAGE"].data).any():
            raise S3CutoutError("built cutout has no finite pixels")


class S3CutoutFetcher:
    """Builds cutouts; remembers HDU layouts per directory (pipeline version + detector)."""

    def __init__(self, reader: RangeReader) -> None:
        self.reader = reader
        self._layouts: dict[str, dict[str, HduInfo]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self.stats = {"built": 0, "walked": 0, "guessed": 0}

    @staticmethod
    def layout_key(url: str) -> str:
        """Files of one pipeline version and detector share their HDU layout (validated on use):
        ``.../level2/<week>/l2b-v27-2026-197/<detector>/file.fits`` -> ``l2b-v27/<detector>``."""
        parts = url.split("/")
        if len(parts) >= 3:
            m = re.match(r"(l2b-v\d+)", parts[-3])
            if m:
                return f"{m.group(1)}/{parts[-2]}"
        return url.rsplit("/", 1)[0]

    async def layout(self, url: str) -> dict[str, HduInfo]:
        key = self.layout_key(url)
        tmpl = self._layouts.get(key)
        if tmpl is None:
            # Concurrent first requests for one key: one walks, the others then reuse its layout.
            async with self._locks.setdefault(key, asyncio.Lock()):
                tmpl = self._layouts.get(key)
                if tmpl is None:
                    return await self._walk(key, url)
        got = await guessed_layout(self.reader, url, tmpl)
        if got is not None:
            self.stats["guessed"] += 1
            return got
        return await self._walk(key, url)

    async def _walk(self, key: str, url: str) -> dict[str, HduInfo]:
        got = await walk_layout(self.reader, url)
        self.stats["walked"] += 1
        self._layouts[key] = {k: HduInfo(v.index, v.name, v.header_offset, v.header_len, v.data_len, v.header)
                              for k, v in got.items()}
        return got

    async def cutout(self, access_url: str, ra: float, dec: float, size_deg: float) -> bytes:
        """Stripped cutout for an IRSA ``access_url`` (read from its AWS open-data copy)."""
        return await self.cutout_from_url(s3_url(access_url), ra, dec, size_deg)

    async def cutout_from_url(self, url: str, ra: float, dec: float, size_deg: float) -> bytes:
        layout = await self.layout(url)
        data, _ = await build_from_layout(self.reader, url, layout, ra, dec, size_deg)
        await asyncio.to_thread(validate, data)
        self.stats["built"] += 1
        return data


class HttpRangeReader:
    """Range reads over a shared httpx connection pool, bounded concurrency, short retries."""

    def __init__(self, client: httpx.AsyncClient, concurrency: int, retries: int = 3) -> None:
        self.client = client
        self.sem = asyncio.Semaphore(concurrency)
        self.retries = retries
        self.bytes = 0
        self.requests = 0

    async def read(self, url: str, start: int, stop: int) -> bytes:
        if stop <= start:
            return b""
        last: Exception | None = None
        for attempt in range(self.retries):
            try:
                async with self.sem:
                    r = await self.client.get(url, headers={"Range": f"bytes={start}-{stop - 1}"})
                self.requests += 1
                if r.status_code == 416:
                    return b""
                if r.status_code in (429, 500, 502, 503, 504):
                    raise httpx.HTTPStatusError(f"HTTP {r.status_code}", request=r.request, response=r)
                if r.status_code == 206:
                    content = r.content
                elif r.status_code == 200:  # Range ignored: whole object came back
                    content = r.content[start:stop]
                else:
                    raise S3CutoutError(f"HTTP {r.status_code} for {url}")
                self.bytes += len(content)
                return content
            except (httpx.HTTPStatusError, httpx.TransportError) as exc:
                last = exc
            await asyncio.sleep(0.5 * 2 ** attempt + random.random() * 0.5)
        raise S3CutoutError(f"S3 range read failed: {last}")


def make_client(concurrency: int) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(settings.s3_timeout_s, connect=10.0),
        follow_redirects=True,
        headers={"User-Agent": "SPHEREx-Time-Machine/0.1 (NASA Space Apps 2026)"},
        limits=httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency),
    )
