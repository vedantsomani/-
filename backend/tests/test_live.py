"""Live end-to-end checks against IRSA/CDS. Run with: pytest -m network"""

import httpx
import pytest

from app.coords import resolve_name
from app.sia import find_images

pytestmark = pytest.mark.network


@pytest.mark.anyio
async def test_sia_finds_wise0855_epochs():
    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as c:
        recs = await find_images(c, 133.795129, -7.245147)
    assert len(recs) > 100
    assert {r.detector for r in recs} == {1, 2, 3, 4, 5, 6}


@pytest.mark.anyio
async def test_sesame_returns_proper_motion():
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as c:
        r = await resolve_name("Barnard's Star", c)
    assert r.pm_dec_masyr and r.pm_dec_masyr > 10000
