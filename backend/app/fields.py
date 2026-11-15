"""A *field* = (target position, FOV, grid). Building a field means: SIA query ->
download every cutout -> process onto the field grid -> cache. It runs as a
background asyncio task so the API can return partial results while it works."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import httpx
import numpy as np

from .config import settings
from .processing import PROCESSING_VERSION, Grid, process_cutout, strip_cutout
from .sia import ImageRecord, find_images
from . import analysis, s3cutout
from .stacks import STACK_VERSION
from .store import (
    cache_usage, enforce_cache_limit, field_dir, frame_path, save_arrays, touch_field,
)

# Cached manifests whose analysis was built with another version are re-analysed on load.
ANALYSIS_TAG = f"{STACK_VERSION}.{analysis.ANALYSIS_VERSION}"

log = logging.getLogger(__name__)

SEASON_GAP_DAYS = 30.0


def _iso(ts: float | None) -> str | None:
    return None if ts is None else datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def _from_iso(s: str | None) -> float | None:
    try:
        return datetime.fromisoformat(s).timestamp() if s else None
    except (TypeError, ValueError):
        return None


def field_key(grid: Grid) -> str:
    raw = "|".join(
        [
            f"{grid.ra:.6f}", f"{grid.dec:.6f}", str(grid.n), f"{grid.pixscale_arcsec:.4f}",
            ",".join(settings.collections), PROCESSING_VERSION,
        ]
    )
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def cutout_file(collection: str, frame_id: str, access_url: str, grid: Grid) -> Path:
    """Where the raw cutout of one exposure for this grid is cached (no side effects)."""
    h = hashlib.sha1(
        f"{access_url}|{grid.ra:.6f}|{grid.dec:.6f}|{grid.cutout_size_deg():.5f}".encode()
    ).hexdigest()[:12]
    return settings.cache_dir / "cutouts" / collection / f"{frame_id}_{h}.fits"


def cutout_path(rec: ImageRecord, grid: Grid) -> Path:
    p = cutout_file(rec.collection, rec.frame_id, rec.access_url, grid)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


# ---- failed exposures ----------------------------------------------------------------
_HTTP_4XX = re.compile(r"(?:HTTP |Client error ')(4[0-9][0-9])(?![0-9])")


def classify_failure(msg: str) -> str:
    """'transient' (worth retrying: HTTP 429/5xx, timeouts, dropped connections, a non-FITS
    reply from an overloaded server) or 'permanent' (HTTP 4xx, no coverage, processing errors)."""
    if "cutout download failed" not in msg:
        return "permanent"  # e.g. "no coverage of target grid", errors while processing a cutout
    m = _HTTP_4XX.search(msg)
    if m and m.group(1) != "429":
        return "permanent"
    return "transient"


def select_retry(
    failed: dict[str, str], *, now: float, last_attempt: float, attempts: int, explicit: bool,
    cooldown_s: float, auto_max: int,
) -> list[str]:
    """Frame ids to re-attempt. Only transient failures qualify. An explicit request retries them
    now; the automatic path waits for the cool-down and gives up after ``auto_max`` attempts."""
    ids = sorted(f for f, msg in failed.items() if classify_failure(msg) == "transient")
    if not ids:
        return []
    if explicit:
        return ids
    if attempts >= auto_max or now - last_attempt < cooldown_s:
        return []
    return ids


def assign_seasons(records: list[ImageRecord]) -> dict[str, int]:
    """Cluster observations into survey 'seasons' separated by > SEASON_GAP_DAYS."""
    out: dict[str, int] = {}
    season, last = 0, None
    for r in sorted(records, key=lambda r: r.t_min):
        if last is not None and r.t_min - last > SEASON_GAP_DAYS:
            season += 1
        out[r.frame_id] = season
        last = r.t_min
    return out


def interleave(records: list[ImageRecord], seasons: dict[str, int]) -> list[ImageRecord]:
    """Download order that samples every (season, detector) early, so partial
    results already span the whole time baseline."""
    buckets: dict[tuple[int, int], list[ImageRecord]] = defaultdict(list)
    for r in sorted(records, key=lambda r: r.t_min):
        buckets[(seasons[r.frame_id], r.detector)].append(r)
    queues = [buckets[k] for k in sorted(buckets)]
    out: list[ImageRecord] = []
    while any(queues):
        for q in queues:
            if q:
                out.append(q.pop(0))
    return out


BUILDING = ("pending", "queued", "searching", "processing")  # statuses a build can be cancelled in


@dataclass
class FieldJob:
    key: str
    grid: Grid
    # pending | queued | searching | processing | stacking | complete | error
    # (queued: waiting for one of `max_concurrent_builds` slots)
    status: str = "pending"
    error: str | None = None
    total: int = 0
    frames: dict[str, dict] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)
    seasons: list[dict] = field(default_factory=list)
    stacks: dict[str, dict] = field(default_factory=dict)
    analysis: dict = field(default_factory=dict)
    stack_version: str | None = None
    started: float = field(default_factory=time.time)
    finished: float | None = None
    task: asyncio.Task | None = None
    # --- interest / retry bookkeeping (not part of the build itself) ---
    last_poll: float = field(default_factory=time.time)
    watchers: dict[str, float] = field(default_factory=dict)   # client -> last seen
    released_at: float | None = None                             # set when the last watcher let go
    last_attempt: float = field(default_factory=time.time)      # last time exposures were downloaded
    retry_attempts: int = 0
    retrying: bool = False
    updating: bool = False                                       # an "check for new observations" pass is running
    last_checked: float | None = None                            # when the exposure list was last obtained (SIA)
    new_frames: int | None = None                                # exposures found by the latest update request
    cutouts: set = field(default_factory=set)                    # raw cutout files this build uses

    # ---- interest ----------------------------------------------------------
    def touch(self, client: str | None = None, now: float | None = None) -> None:
        now = time.time() if now is None else now
        self.last_poll = now
        self.released_at = None
        if client:
            self.watchers[client] = now

    def release(self, client: str | None, now: float | None = None) -> int:
        """The client no longer needs this field. Returns the number of watchers left."""
        now = time.time() if now is None else now
        if client:
            self.watchers.pop(client, None)
        if not self.watchers:
            self.released_at = now
        return len(self.watchers)

    @property
    def maintaining(self) -> bool:
        """A retry or an update of a finished field is running (never cancelled as 'abandoned')."""
        return self.retrying or self.updating

    def cancellable(self, now: float, idle_s: float, grace_s: float) -> bool:
        """A build (not a retry, not a finished field) that nobody has polled for ``idle_s``, or
        whose last watcher let go more than ``grace_s`` ago with no poll since."""
        if self.status not in BUILDING or self.maintaining or self.task is None or self.task.done():
            return False
        if now - self.last_poll > idle_s:
            return True
        return not self.watchers and self.released_at is not None and now - self.released_at > grace_s

    # ---- serialisation ---------------------------------------------------
    def retry_summary(self) -> dict:
        kinds = [classify_failure(m) for m in self.failed.values()]
        return {
            "transient": kinds.count("transient"),
            "permanent": kinds.count("permanent"),
            "attempts": self.retry_attempts,
            "in_progress": self.retrying,
        }

    def to_dict(self, queue_position: int | None = None) -> dict:
        frames = sorted(self.frames.values(), key=lambda f: (f["mjd"] or 0, f["detector"]))
        return {
            "key": self.key,
            "status": self.status,
            "error": self.error,
            "target": {"ra": self.grid.ra, "dec": self.grid.dec},
            "grid": {
                "n": self.grid.n,
                "pixscale_arcsec": self.grid.pixscale_arcsec,
                "fov_arcmin": round(self.grid.fov_arcmin, 4),
                "wcs": self.grid.header_dict(),
            },
            "collections": settings.collections,
            "total": self.total,
            "done": len(self.frames),
            "failed": self.failed,
            "seasons": self.seasons,
            "elapsed_s": round((self.finished or time.time()) - self.started, 1),
            "frames": frames,
            "stack_version": self.stack_version,
            "stacks": sorted(self.stacks.values(), key=lambda s: (s["detector"], s.get("bin") or "", s["season"])),
            "analysis": self.analysis,
            "queue_position": queue_position,
            "interest": len(self.watchers),
            "retry": self.retry_summary(),
            "last_attempt": self.last_attempt,
            "retry_attempts": self.retry_attempts,
            "last_checked": _iso(self.last_checked),
            "new_frames": self.new_frames,
        }

    def save_manifest(self) -> None:
        d = field_dir(self.key)
        d.mkdir(parents=True, exist_ok=True)
        data = self.to_dict()
        tmp = d / "manifest.json.tmp"
        tmp.write_text(json.dumps(data))
        tmp.replace(d / "manifest.json")

    @classmethod
    def load(cls, key: str, grid: Grid) -> "FieldJob | None":
        p = field_dir(key) / "manifest.json"
        if not p.exists():
            return None
        data = json.loads(p.read_text())
        if data.get("status") != "complete":
            return None
        job = cls(key=key, grid=grid, status="complete", total=data["total"])
        job.frames = {f["id"]: f for f in data["frames"]}
        job.failed = data.get("failed", {})
        job.seasons = data.get("seasons", [])
        job.stacks = {s["id"]: s for s in data.get("stacks", [])}
        job.analysis = data.get("analysis", {})
        job.stack_version = data.get("stack_version")
        job.started = time.time() - data.get("elapsed_s", 0)
        job.finished = time.time()
        job.retry_attempts = int(data.get("retry_attempts", 0))
        job.last_attempt = float(data.get("last_attempt") or p.stat().st_mtime)
        job.last_checked = _from_iso(data.get("last_checked")) or p.stat().st_mtime  # older manifests: build time
        return job


class FieldManager:
    def __init__(self) -> None:
        self.jobs: dict[str, FieldJob] = {}
        self._download_sem = asyncio.Semaphore(settings.download_concurrency)
        self._cpu_sem = asyncio.Semaphore(settings.processing_workers)
        self._build_slots = asyncio.Semaphore(settings.max_concurrent_builds)
        self._admitted = 0  # builds holding or waiting for a slot (labels new jobs pending/queued)
        self.usage: dict | None = None
        self.usage_at = 0.0
        self.client = httpx.AsyncClient(
            timeout=settings.http_timeout_s,
            follow_redirects=True,
            headers={"User-Agent": "SPHEREx-Time-Machine/0.1 (NASA Space Apps 2026)"},
            limits=httpx.Limits(max_connections=settings.download_concurrency + 8),
        )
        self._s3: s3cutout.S3CutoutFetcher | None = None  # created on first use

    async def close(self) -> None:
        # Cancel in-flight builds first, otherwise shutdown (and uvicorn --reload)
        # hangs waiting on dozens of slow IRSA cutout requests.
        tasks = [j.task for j in self.jobs.values() if j.task and not j.task.done()]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.client.aclose()
        if self._s3 is not None:
            await self._s3.reader.client.aclose()

    def plan(self, grid: Grid, refresh: bool = False, retry: bool = False, update: bool = False) -> str:
        """What `get_or_start` would do, without doing it: 'build' (a NEW field build, charged
        to the build rate limit), 'update' (archive re-query), 'retry' (explicit retry of failed
        exposures) or 'none'."""
        key = field_key(grid)
        job = self.jobs.get(key)
        if job and job.status in BUILDING + ("stacking",):
            return "none"
        if refresh or (job and job.status == "error"):
            return "build"
        if job is None and not (field_dir(key) / "manifest.json").exists():
            return "build"
        if update:
            return "update"
        return "retry" if retry else "none"

    def get_or_start(self, grid: Grid, refresh: bool = False, retry: bool = False,
                     client: str | None = None) -> FieldJob:
        key = field_key(grid)
        job = self.jobs.get(key)
        if job and job.status in BUILDING + ("stacking",):
            refresh = False  # never start a second build of a field that is already building
        if job and not refresh and job.status != "error":
            if job.status == "complete" and job.stack_version != ANALYSIS_TAG:  # code changed under us
                job.status = "stacking"
                job.task = asyncio.create_task(self._stack_phase(job))
            else:
                self._maybe_retry(job, retry)
            job.touch(client)
            touch_field(key)
            return job
        if not refresh and (cached := FieldJob.load(key, grid)):
            self.jobs[key] = cached
            if cached.stack_version != ANALYSIS_TAG:  # stacks/analysis missing or stale
                cached.status = "stacking"
                cached.task = asyncio.create_task(self._stack_phase(cached))
            else:
                self._maybe_retry(cached, retry)
            cached.touch(client)
            touch_field(key)
            return cached
        job = FieldJob(key=key, grid=grid)
        job.status = "queued" if self._admitted >= settings.max_concurrent_builds else "pending"
        job.touch(client)
        self.jobs[key] = job
        job.task = self._spawn(self._run(job))
        return job

    def _spawn(self, coro) -> asyncio.Task:
        self._admitted += 1
        task = asyncio.create_task(coro)
        task.add_done_callback(lambda _t: setattr(self, "_admitted", self._admitted - 1))
        return task

    def _maybe_retry(self, job: FieldJob, explicit: bool) -> None:
        """Re-attempt transient failures of a complete field (see `select_retry`)."""
        if job.status != "complete" or job.maintaining or not job.failed:
            return
        ids = select_retry(
            job.failed, now=time.time(), last_attempt=job.last_attempt, attempts=job.retry_attempts,
            explicit=explicit, cooldown_s=settings.retry_cooldown_s, auto_max=settings.auto_retry_max,
        )
        if ids:
            log.info("field %s: retrying %d failed exposures (%s)", job.key, len(ids),
                     "requested" if explicit else "automatic")
            job.retrying = True
            job.status = "queued" if self._admitted >= settings.max_concurrent_builds else "processing"
            job.task = self._spawn(self._retry(job, ids))

    def queue_position(self, job: FieldJob) -> int | None:
        if job.status != "queued":
            return None
        return 1 + sum(1 for j in self.jobs.values() if j.status == "queued" and j.started < job.started)

    def describe(self, job: FieldJob) -> dict:
        return job.to_dict(self.queue_position(job))

    # ---- interest, cancellation, cache -----------------------------------------
    def release(self, key: str, client: str | None) -> dict:
        job = self.jobs.get(key)
        if job is None:
            return {"key": key, "status": "unknown", "interest": 0, "cancel_scheduled": False}
        left = job.release(client)
        building = job.status in BUILDING and not job.maintaining
        return {"key": key, "status": job.status, "interest": left,
                "cancel_scheduled": bool(building and left == 0)}

    def cancel(self, job: FieldJob) -> None:
        """Stop a build nobody wants. Finished frames and cutouts stay on disk; the next request
        for this field makes a new job that resumes from them."""
        log.info("field %s: cancelled (%d/%d frames done, nobody polling)", job.key, len(job.frames), job.total)
        job.status = "cancelled"
        if job.task and not job.task.done():
            job.task.cancel()
        if self.jobs.get(job.key) is job:
            del self.jobs[job.key]

    def reap_idle(self, now: float | None = None) -> list[str]:
        now = time.time() if now is None else now
        out = [j for j in list(self.jobs.values())
               if j.cancellable(now, settings.job_idle_cancel_s, settings.release_grace_s)]
        for j in out:
            self.cancel(j)
        return [j.key for j in out]

    def active(self) -> tuple[set[str], set[Path]]:
        """Fields being built (never evicted) and the raw cutouts they use."""
        jobs = [j for j in list(self.jobs.values())
                if j.status not in ("complete", "error", "cancelled") or j.maintaining]
        return {j.key for j in jobs}, {p for j in jobs for p in list(j.cutouts)}

    @staticmethod
    def cutouts_of(key: str) -> list[Path]:
        try:
            data = json.loads((field_dir(key) / "manifest.json").read_text())
            t, g = data["target"], data["grid"]
            grid = Grid(ra=t["ra"], dec=t["dec"], n=g["n"], pixscale_arcsec=g["pixscale_arcsec"])
            return [cutout_file(f["collection"], f["id"], f["source_url"], grid)
                    for f in data["frames"] if f.get("source_url")]
        except Exception:  # noqa: BLE001 — unreadable manifest: its cutouts count as orphans
            return []

    async def enforce_cache(self) -> dict:
        res = await asyncio.to_thread(enforce_cache_limit, self.cutouts_of, self.active)
        for key in res["processed_keys"]:  # their arrays are gone: forget the in-memory job
            job = self.jobs.get(key)
            if job and job.status in ("complete", "error"):
                del self.jobs[key]
        if res["evicted"]:
            log.info("cache eviction: %.0f -> %.0f MB, %d units", res["bytes_before"] / 1e6,
                     res["bytes_after"] / 1e6, len(res["evicted"]))
        self.usage, self.usage_at = None, 0.0
        return res

    async def cache_status(self, max_age: float = 30.0) -> dict:
        if self.usage is None or time.time() - self.usage_at > max_age:
            u = await asyncio.to_thread(cache_usage)
            self.usage, self.usage_at = u, time.time()
        return self.usage

    async def janitor(self) -> None:
        """Background upkeep: cancel abandoned builds, keep the cache under its limit."""
        last_check = 0.0
        while True:
            await asyncio.sleep(settings.janitor_interval_s)
            try:
                self.reap_idle()
                if time.time() - last_check >= settings.cache_check_interval_s:
                    last_check = time.time()
                    await self.enforce_cache()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("janitor pass failed")

    def find_job_containing(self, ra: float, dec: float) -> FieldJob | None:
        """A loaded field whose grid contains (ra, dec), preferring the smallest."""
        best = None
        for job in self.jobs.values():
            x, y = job.grid.wcs().world_to_pixel_values(ra, dec)
            if 1 <= x < job.grid.n - 1 and 1 <= y < job.grid.n - 1:
                if best is None or job.grid.n < best.grid.n:
                    best = job
        return best

    # ---- pipeline ----------------------------------------------------------
    async def _run(self, job: FieldJob) -> None:
        try:
            async with self._build_slots:
                job.status = "searching"
                records = await find_images(self.client, job.grid.ra, job.grid.dec)
                job.last_checked = time.time()
                job.total = len(records)
                season_of = assign_seasons(records)
                job.seasons = _season_summary(records, season_of)
                job.status = "processing"
                log.info("field %s: %d images", job.key, len(records))
                await asyncio.gather(
                    *(self._frame(job, r, season_of[r.frame_id]) for r in interleave(records, season_of))
                )
                job.last_attempt = time.time()
                log.info("field %s frames done: %d ok, %d failed in %.0fs", job.key,
                         len(job.frames), len(job.failed), time.time() - job.started)
                await self._stack_phase(job)
        except Exception as exc:  # noqa: BLE001 — surface any failure to the client
            log.exception("field %s failed", job.key)
            job.status = "error"
            job.error = f"{type(exc).__name__}: {exc}"
            job.finished = time.time()

    async def _retry(self, job: FieldJob, ids: list[str]) -> None:
        """Re-attempt only the given (transient) failed exposures, then re-run the analysis."""
        prior = dict(job.failed)
        wanted = set(ids)
        try:
            async with self._build_slots:
                job.status = "processing"
                records = await find_images(self.client, job.grid.ra, job.grid.dec)
                job.last_checked = time.time()
                season_of = assign_seasons(records)
                todo = [r for r in records if r.frame_id in wanted]
                for r in todo:
                    job.failed.pop(r.frame_id, None)
                before = len(job.frames)
                await asyncio.gather(*(self._frame(job, r, season_of[r.frame_id])
                                       for r in interleave(todo, season_of)))
                job.retry_attempts += 1
                job.last_attempt = time.time()
                recovered = len(job.frames) - before
                log.info("field %s retry: %d of %d failed exposures recovered", job.key, recovered, len(wanted))
                if recovered:
                    await self._stack_phase(job)
                else:
                    job.status = "complete"
                    job.save_manifest()
        except asyncio.CancelledError:
            job.failed = prior
            job.status = "complete"
            raise
        except Exception:  # noqa: BLE001 — a failed retry must not damage the finished field
            log.exception("field %s retry failed", job.key)
            job.failed = prior
            job.retry_attempts += 1
            job.last_attempt = time.time()
            job.status = "complete"
        finally:
            job.retrying = False

    async def update(self, grid: Grid, client: str | None = None) -> FieldJob:
        """'Check for new observations': re-query the archive ignoring the SIA cache and process
        only exposures this field has never seen (neither processed nor failed). Failed ones are
        the retry path's business. The analysis is re-run only if something was added."""
        job = self.get_or_start(grid, client=client)
        if job.status != "complete" or job.maintaining:
            return job  # still building / already updating: just report it, the client polls
        records = await find_images(self.client, grid.ra, grid.dec, fresh=True)
        if job.status != "complete" or job.maintaining:
            return job  # something else took the field while we were waiting on the archive
        job.last_checked = time.time()
        known = set(job.frames) | set(job.failed)
        new = [r for r in records if r.frame_id not in known]
        job.new_frames = len(new)
        log.info("field %s: update check found %d new exposures", job.key, len(new))
        if not new:
            job.save_manifest()
            return job
        job.updating = True
        job.status = "queued" if self._admitted >= settings.max_concurrent_builds else "processing"
        job.task = self._spawn(self._update(job, records, new))
        return job

    async def _update(self, job: FieldJob, records: list[ImageRecord], new: list[ImageRecord]) -> None:
        prior = (job.total, job.seasons)
        try:
            async with self._build_slots:
                job.status = "processing"
                season_of = assign_seasons(records)
                job.total, job.seasons = len(records), _season_summary(records, season_of)
                before = len(job.frames)
                await asyncio.gather(*(self._frame(job, r, season_of[r.frame_id])
                                       for r in interleave(new, season_of)))
                job.last_attempt = time.time()
                changed = len(job.frames) > before
                for fid, meta in job.frames.items():  # a new exposure can merge/split seasons
                    if fid in season_of and meta.get("season") != season_of[fid]:
                        meta["season"], changed = season_of[fid], True
                        try:
                            frame_path(job.key, fid).with_suffix(".json").write_text(json.dumps(meta))
                        except OSError:
                            log.warning("could not update season of %s", fid)
                if changed:
                    await self._stack_phase(job)
                else:
                    job.status = "complete"
                    job.save_manifest()
        except asyncio.CancelledError:
            job.total, job.seasons, job.status = *prior, "complete"
            raise
        except Exception:  # noqa: BLE001 — a failed update must not damage the finished field
            log.exception("field %s update failed", job.key)
            job.total, job.seasons, job.status = *prior, "complete"
        finally:
            job.updating = False

    async def _stack_phase(self, job: FieldJob) -> None:
        try:
            job.status = "stacking"
            t0 = time.time()
            async with self._cpu_sem:
                job.stacks, job.analysis = await asyncio.to_thread(
                    analysis.run, job.key, job.grid, list(job.frames.values())
                )
            job.stack_version = ANALYSIS_TAG
            job.status = "complete"
            job.finished = time.time()
            job.save_manifest()
            log.info("field %s: %d stacks in %.1fs", job.key, len(job.stacks), time.time() - t0)
        except Exception as exc:  # noqa: BLE001
            log.exception("field %s stacking failed", job.key)
            job.status = "error"
            job.error = f"stacking failed: {type(exc).__name__}: {exc}"
            job.finished = time.time()

    async def _frame(self, job: FieldJob, rec: ImageRecord, season: int) -> None:
        npz = frame_path(job.key, rec.frame_id)
        meta_path = npz.with_suffix(".json")
        try:
            if npz.exists() and meta_path.exists():
                meta = json.loads(meta_path.read_text())
            else:
                job.cutouts.add(cutout_file(rec.collection, rec.frame_id, rec.access_url, job.grid))
                raw, source = await self._get_cutout(rec, job.grid)
                async with self._cpu_sem:
                    arrays, meta = await asyncio.to_thread(process_cutout, raw, job.grid)
                meta["cutout_source"] = source
                npz.parent.mkdir(parents=True, exist_ok=True)
                await asyncio.to_thread(save_arrays, npz, arrays)
                meta.update(
                    id=rec.frame_id,
                    obs_id=rec.obs_id,
                    visit=rec.visit,
                    detector=rec.detector,
                    collection=rec.collection,
                    season=season,
                    source_url=rec.access_url,
                    png=f"/api/fields/{job.key}/frames/{rec.frame_id}.png",
                )
                meta_path.write_text(json.dumps(meta))
            if meta.get("coverage", 0) <= 0:
                job.failed[rec.frame_id] = "no coverage of target grid"
                return
            job.frames[rec.frame_id] = meta
        except Exception as exc:  # noqa: BLE001
            log.warning("frame %s failed: %s", rec.frame_id, exc)
            job.failed[rec.frame_id] = f"{type(exc).__name__}: {exc}"

    def _s3_fetcher(self) -> "s3cutout.S3CutoutFetcher":
        if self._s3 is None:
            client = s3cutout.make_client(settings.s3_concurrency)
            self._s3 = s3cutout.S3CutoutFetcher(s3cutout.HttpRangeReader(client, settings.s3_concurrency))
        return self._s3

    async def _get_cutout(self, rec: ImageRecord, grid: Grid) -> tuple[bytes, str]:
        """Stripped cutout bytes and where they came from ('s3' or 'irsa'). Sources are tried in
        the order of ``settings.cutout_sources``; any S3 failure falls through to the next."""
        path = cutout_path(rec, grid)
        if path.exists():
            data = path.read_bytes()
            return data, s3cutout.cached_source(data)
        errors: list[str] = []
        for source in settings.cutout_sources:
            if source == "s3":
                try:
                    # Same rounded centre/size as the IRSA request, so the box is identical.
                    data = await self._s3_fetcher().cutout(
                        rec.access_url, float(f"{grid.ra:.7f}"), float(f"{grid.dec:.7f}"),
                        float(f"{grid.cutout_size_deg():.5f}"),
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 — any S3 problem: fall back
                    log.info("S3 cutout of %s failed, falling back: %s", rec.frame_id, exc)
                    errors.append(f"s3: {exc}")
                    continue
                tmp = path.with_name(path.name + ".tmp")
                tmp.write_bytes(data)
                tmp.replace(path)
                return data, "s3"
            if source == "irsa":
                return await self._get_irsa_cutout(rec, grid, path), "irsa"
            log.warning("unknown cutout source %r ignored", source)
        raise RuntimeError(f"cutout download failed: {'; '.join(errors) or 'no cutout source configured'}")

    async def _get_irsa_cutout(self, rec: ImageRecord, grid: Grid, path: Path) -> bytes:
        url = f"{rec.access_url}?center={grid.ra:.7f},{grid.dec:.7f}&size={grid.cutout_size_deg():.5f}"
        last: Exception | None = None
        for attempt in range(settings.download_retries):
            try:
                async with self._download_sem:
                    resp = await self.client.get(url)
                if resp.status_code in (429, 500, 502, 503, 504):
                    raise httpx.HTTPStatusError(f"HTTP {resp.status_code}", request=resp.request, response=resp)
                resp.raise_for_status()
                if not resp.content.startswith(b"SIMPLE"):
                    raise ValueError(f"not a FITS file: {resp.content[:80]!r}")
                data = await asyncio.to_thread(strip_cutout, resp.content, rec.access_url, rec.detector)
                tmp = path.with_name(path.name + ".tmp")
                tmp.write_bytes(data)
                tmp.replace(path)
                return data
            except (httpx.HTTPError, ValueError) as exc:
                last = exc
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status is not None and 400 <= status < 500 and status != 429:
                    break  # permanent
                await asyncio.sleep(min(2 ** attempt + np.random.rand(), 20))
        raise RuntimeError(f"cutout download failed: {last}")


def _season_summary(records: list[ImageRecord], season_of: dict[str, int]) -> list[dict]:
    from astropy.time import Time

    by: dict[int, list[ImageRecord]] = defaultdict(list)
    for r in records:
        by[season_of[r.frame_id]].append(r)
    out = []
    for s in sorted(by):
        rs = by[s]
        t0, t1 = min(r.t_min for r in rs), max(r.t_max for r in rs)
        out.append(
            {
                "season": s,
                "mjd_start": t0,
                "mjd_end": t1,
                "start": Time(t0, format="mjd").isot[:10],
                "end": Time(t1, format="mjd").isot[:10],
                "n_frames": len(rs),
                "detectors": sorted({r.detector for r in rs}),
            }
        )
    return out
