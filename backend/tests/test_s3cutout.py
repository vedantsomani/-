"""S3 range-request cutouts (s3cutout.py): offline arithmetic tests on a synthetic L2-like file,
plus a live equivalence test against cached IRSA cutouts of the WISE 0855 field."""

from __future__ import annotations

import asyncio
import io

import numpy as np
import pytest
from astropy.io import fits

from app import s3cutout as s3

NY, NX = 400, 300


class MemReader:
    """In-memory RangeReader over a FITS byte string; counts requests."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.requests: list[tuple[int, int]] = []

    async def read(self, url: str, start: int, stop: int) -> bytes:
        self.requests.append((start, stop))
        return self.data[start:stop]


def _wcs_header(crpix1: float = 50.5, crpix2: float = 60.5) -> fits.Header:
    h = fits.Header()
    h["CTYPE1"], h["CTYPE2"] = "RA---TAN", "DEC--TAN"
    h["CRVAL1"], h["CRVAL2"] = 150.0, 2.0
    h["CRPIX1"], h["CRPIX2"] = crpix1, crpix2
    s = 6.15 / 3600
    h["CD1_1"], h["CD1_2"], h["CD2_1"], h["CD2_2"] = -s, 0.0, 0.0, s
    # alternate WCSs whose reference pixels must shift with the cutout origin
    h["CTYPE1A"], h["CTYPE2A"] = "LINEAR", "LINEAR"
    h["CRPIX1A"], h["CRPIX2A"], h["CRVAL1A"], h["CRVAL2A"] = 1.0, 1.0, 0.0, 0.0
    h["CRPIX1W"], h["CRPIX2W"] = 3.0, 4.0
    h["BUNIT"] = "MJy/sr"
    return h


def synthetic_l2(compress_flags: bool = True, seed: int = 1, flag_max: int = 2**20) -> tuple[bytes, dict[str, np.ndarray]]:
    rng = np.random.default_rng(seed)
    arrays = {
        "IMAGE": rng.normal(10, 1, (NY, NX)).astype(np.float32),
        "FLAGS": rng.integers(0, flag_max, (NY, NX)).astype(np.int32),
        "VARIANCE": rng.uniform(0.5, 2, (NY, NX)).astype(np.float32),
        "ZODI": rng.normal(3, 0.1, (NY, NX)).astype(np.float32),
    }
    prim = fits.PrimaryHDU()
    prim.header["MJD-AVG"] = 60800.5
    for i in range(300):  # a long primary header, as in real files
        prim.header[f"HIST{i:04d}"] = i
    hdus: list = [prim]
    for name in ("IMAGE", "FLAGS", "VARIANCE", "ZODI"):
        hdr = _wcs_header()
        if name == "FLAGS":
            hdr["MSKN0001"], hdr["MSKM0001"] = "SATURATED", 1
        if name == "FLAGS" and compress_flags:
            hdus.append(fits.CompImageHDU(arrays[name], header=hdr, name=name,
                                          compression_type="RICE_1", tile_shape=(1, NX)))
        else:
            hdus.append(fits.ImageHDU(arrays[name], header=hdr, name=name))
    hdus.append(fits.BinTableHDU.from_columns(
        [fits.Column("PSF", "100E", array=np.ones((5, 100), np.float32))], name="EPSF"))
    hdus.append(fits.BinTableHDU.from_columns(
        [fits.Column("X", "E", array=np.arange(4, dtype=np.float32)),
         fits.Column("VALUES", "8E", array=np.ones((4, 8), np.float32))], name="WCS-WAVE"))
    buf = io.BytesIO()
    fits.HDUList(hdus).writeto(buf)
    return buf.getvalue(), arrays


URL = "https://example.org/dr1/level2/2025W19_1B/l2b-v27-2026-197/3/level2_x_1D3_spx.fits"


def _build(data: bytes, ra: float, dec: float, size: float) -> tuple[bytes, MemReader, s3.S3CutoutFetcher]:
    reader = MemReader(data)
    fetcher = s3.S3CutoutFetcher(reader)

    async def run() -> bytes:
        layout = await fetcher.layout(URL)
        out, _ = await s3.build_from_layout(reader, URL, layout, ra, dec, size)
        s3.validate(out)
        return out

    return asyncio.run(run()), reader, fetcher


@pytest.mark.parametrize("compress", [True, False])
def test_cutout_pixels_and_headers(compress):
    data, arrays = synthetic_l2(compress)
    # centre at 0-based pixel (40.3, 70.8): 21 x 21 px box
    from astropy.wcs import WCS

    w = WCS(_wcs_header())
    ra, dec = (float(v) for v in w.all_pix2world(40.3, 70.8, 0))
    size = 21 * 6.15 / 3600
    out, reader, _ = _build(data, ra, dec, size)
    x0, y0 = 40 - 10, 71 - 10  # floor(c - (n-1)/2 + 0.5)
    with fits.open(io.BytesIO(out)) as h:
        assert [x.name for x in h] == ["PRIMARY", "IMAGE", "FLAGS", "VARIANCE", "ZODI", "WCS-WAVE"]
        assert h[0].header[s3.SOURCE_KEY] == "s3"
        assert h[0].header["MJD-AVG"] == 60800.5
        for n, arr in arrays.items():
            assert not isinstance(h[n], fits.CompImageHDU)
            np.testing.assert_array_equal(h[n].data, arr[y0:y0 + 21, x0:x0 + 21])
            hdr = h[n].header
            assert hdr["CRPIX1"] == 50.5 - x0 and hdr["CRPIX2"] == 60.5 - y0
            assert hdr["CRPIX1A"] == 1.0 - x0 and hdr["CRPIX2A"] == 1.0 - y0
            assert hdr["CRPIX1W"] == 3.0 - x0 and hdr["CRPIX2W"] == 4.0 - y0
        assert h["FLAGS"].header["MSKN0001"] == "SATURATED"
        assert h["WCS-WAVE"].data["VALUES"].shape == (4, 8)
        # the pixel centre of the target maps back to the same sky position
        from app.processing import _celestial_header

        wc = WCS(_celestial_header(h["IMAGE"].header))
        px, py = (float(v) for v in wc.all_world2pix(ra, dec, 0))
        assert abs(px - (40.3 - x0)) < 1e-6 and abs(py - (70.8 - y0)) < 1e-6
    assert s3.cached_source(out) == "s3"
    # only a small part of the file was read
    assert sum(b - a for a, b in reader.requests) < len(data) / 3


def test_box_rule_even_size_and_edge_clipping():
    hdr = _wcs_header()
    hdr["NAXIS1"], hdr["NAXIS2"] = NX, NY
    from astropy.wcs import WCS

    w = WCS(_wcs_header())
    ra, dec = (float(v) for v in w.all_pix2world(40.3, 2.2, 0))
    # 20 px (even): first pixel floor(40.3 - 9.5 + 0.5) = 31; y from -7, clipped at 0
    assert s3.cutout_box(hdr, ra, dec, 20 * 6.15 / 3600, (NY, NX)) == (31, 51, 0, 13)
    ra, dec = (float(v) for v in w.all_pix2world(298.0, 397.6, 0))
    assert s3.cutout_box(hdr, ra, dec, 11 * 6.15 / 3600, (NY, NX)) == (293, 300, 393, 400)


def test_off_detector_raises():
    data, _ = synthetic_l2()
    from astropy.wcs import WCS

    ra, dec = (float(v) for v in WCS(_wcs_header()).all_pix2world(400, 400, 0))
    with pytest.raises(s3.S3CutoutError):
        _build(data, ra, dec, 21 * 6.15 / 3600)


class TwoFiles(MemReader):
    def __init__(self, files: dict[str, bytes]) -> None:
        super().__init__(b"")
        self.files = files

    async def read(self, url: str, start: int, stop: int) -> bytes:
        self.requests.append((start, stop))
        return self.files[url][start:stop]


def test_layout_reused_for_same_detector_with_different_heap_size():
    first, _ = synthetic_l2(seed=1)
    second, arrays = synthetic_l2(seed=2, flag_max=4)  # compresses much better: smaller FLAGS heap
    other = URL.replace("2025W19_1B/l2b-v27-2026-197", "2026W20_1A/l2b-v27-2026-246")
    reader = TwoFiles({URL: first, other: second})
    fetcher = s3.S3CutoutFetcher(reader)
    assert fetcher.layout_key(URL) == fetcher.layout_key(other) == "l2b-v27/3"
    from astropy.wcs import WCS

    ra, dec = (float(v) for v in WCS(_wcs_header()).all_pix2world(150.0, 200.0, 0))

    async def run() -> bytes:
        await fetcher.layout(URL)
        return await fetcher.cutout_from_url(other, ra, dec, 21 * 6.15 / 3600)

    out = asyncio.run(run())
    assert fetcher.stats["walked"] == 1 and fetcher.stats["guessed"] == 1
    with fits.open(io.BytesIO(out)) as h:
        for n, arr in arrays.items():
            np.testing.assert_array_equal(h[n].data, arr[190:211, 140:161])


def test_cached_source_and_s3_url():
    buf = io.BytesIO()
    fits.PrimaryHDU().writeto(buf)
    assert s3.cached_source(buf.getvalue()) == "irsa"
    assert s3.s3_url("https://irsa.ipac.caltech.edu/ibe/data/spherex/dr1/level2/a/b.fits").endswith(
        "nasa-irsa-spherex.s3.us-east-1.amazonaws.com/dr1/level2/a/b.fits")
    with pytest.raises(s3.S3CutoutError):
        s3.s3_url("https://example.org/other.fits")


# --------------------------------------------------------------------------- live equivalence

@pytest.mark.network
def test_s3_matches_cached_irsa_cutouts():
    from app.fields import FieldJob, cutout_file, field_key
    from app.processing import Grid, process_cutout

    g = Grid.for_fov(133.76002, -7.24221, 5, 6.15)
    job = FieldJob.load(field_key(g), g)
    if job is None:
        pytest.skip("WISE 0855 field not cached")
    by_det: dict[int, dict] = {}
    for m in job.frames.values():
        if cutout_file(m["collection"], m["id"], m["source_url"], g).exists():
            by_det.setdefault(m["detector"], m)
    picks = [by_det[d] for d in sorted(by_det)][::2][:3]
    if len(picks) < 3:
        pytest.skip("cached IRSA cutouts missing")

    async def build_all() -> list[bytes]:
        client = s3.make_client(16)
        try:
            f = s3.S3CutoutFetcher(s3.HttpRangeReader(client, 16))
            return await asyncio.gather(*(
                f.cutout(m["source_url"], float(f"{g.ra:.7f}"), float(f"{g.dec:.7f}"),
                         float(f"{g.cutout_size_deg():.5f}")) for m in picks))
        finally:
            await client.aclose()

    built = asyncio.run(build_all())
    for m, mine in zip(picks, built):
        irsa = cutout_file(m["collection"], m["id"], m["source_url"], g).read_bytes()
        with fits.open(io.BytesIO(irsa)) as a, fits.open(io.BytesIO(mine)) as b:
            for n in s3.IMAGE_HDUS:
                ha, hb = a[n].header, b[n].header
                dx, dy = round(ha["CRPIX1"] - hb["CRPIX1"]), round(ha["CRPIX2"] - hb["CRPIX2"])
                assert (dx, dy) == (0, 0), (m["id"], n, dx, dy)  # same box as IRSA
                np.testing.assert_array_equal(a[n].data, b[n].data)
                for k in ("CRPIX1", "CRPIX2", "CRPIX1A", "CRPIX2A", "CRPIX1W", "CRPIX2W"):
                    assert ha.get(k) == hb.get(k), (m["id"], n, k)
        pa, ma = process_cutout(irsa, g)
        pb, mb = process_cutout(mine, g)
        for k, x in pa.items():
            if x.dtype.kind == "f":
                np.testing.assert_allclose(pb[k], x, rtol=1e-5, atol=0, equal_nan=True)
            else:
                np.testing.assert_array_equal(pb[k], x)
        assert ma == mb
