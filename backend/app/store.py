"""On-disk layout of processed products and cached loaders.

cache/fields/{key}/manifest.json
cache/fields/{key}/frames/{frame_id}.npz|.json   per-exposure arrays + meta
cache/fields/{key}/stacks/{stack_id}.npz          co-adds
cache/fields/{key}/.accessed                      touched on use; its mtime is the LRU clock

The cache is bounded (`settings.cache_max_bytes`). `plan_eviction` / `enforce_cache_limit`
remove whole fields least-recently-used: processed products (fields/, fingerprints/) first,
raw cutouts next. cache/psf, cache/sia and cache/catalogs are never evicted.
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable

import numpy as np

from .config import settings

log = logging.getLogger(__name__)


def field_dir(key: str) -> Path:
    return settings.cache_dir / "fields" / key


def frame_path(key: str, frame_id: str) -> Path:
    return field_dir(key) / "frames" / f"{frame_id}.npz"


def stack_path(key: str, stack_id: str) -> Path:
    return field_dir(key) / "stacks" / f"{stack_id}.npz"


@lru_cache(maxsize=4096)
def _load(path: str, mtime: float) -> dict[str, np.ndarray]:  # mtime busts the cache on rewrite
    with np.load(path) as z:
        return {k: z[k] for k in z.files}


def load_arrays(path: Path) -> dict[str, np.ndarray]:
    if not path.exists():
        raise FileNotFoundError(path.name)
    return _load(str(path), path.stat().st_mtime)


def frame_arrays(key: str, frame_id: str) -> dict[str, np.ndarray]:
    return load_arrays(frame_path(key, frame_id))


def stack_arrays(key: str, stack_id: str) -> dict[str, np.ndarray]:
    return load_arrays(stack_path(key, stack_id))


def save_arrays(path: Path, arrays: dict[str, np.ndarray]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp.npz")
    np.savez_compressed(tmp, **arrays)
    tmp.replace(path)


# ----------------------------------------------------------------------------- cache limit
ACCESS_FILE = ".accessed"
_touched: dict[str, float] = {}


def touch_field(key: str, min_interval: float = 30.0) -> None:
    """Record that a field was used (cheap: one utime, at most every ``min_interval`` s)."""
    now = time.time()
    if now - _touched.get(key, 0.0) < min_interval:
        return
    _touched[key] = now
    d = field_dir(key)
    try:
        if d.is_dir():
            (d / ACCESS_FILE).touch()
    except OSError:
        pass


def _mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def field_last_access(key: str) -> float:
    d = field_dir(key)
    return max(_mtime(d / ACCESS_FILE), _mtime(d / "manifest.json"), _mtime(d))


def tree_size(path: Path) -> int:
    """Bytes under ``path`` (a file or a directory). Missing paths count as 0."""
    total = 0
    stack = [str(path)]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        else:
                            total += e.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except NotADirectoryError:
            try:
                total += os.stat(cur).st_size
            except OSError:
                pass
        except OSError:
            continue
    return total


def cache_usage() -> dict:
    """Total bytes of the cache and of each top-level directory."""
    root = settings.cache_dir
    by: dict[str, int] = {}
    if root.is_dir():
        for child in root.iterdir():
            by[child.name] = tree_size(child)
    total = sum(by.values())
    limit = settings.cache_max_bytes
    return {
        "bytes": total,
        "limit_bytes": limit,
        "fraction": round(total / limit, 4) if limit else None,
        "by_dir": by,
    }


@dataclass
class Unit:
    """One evictable item: a field's processed products, or its raw cutouts."""

    kind: str                  # "processed" | "raw"
    key: str                   # field key (raw orphans: "orphan:<file name>")
    paths: list[Path]
    size: int
    last_access: float
    orphan: bool = False


KIND_ORDER = {"processed": 0, "raw": 1}


def is_protected(u: Unit, now: float, protect_s: float, active_keys, active_paths) -> bool:
    if u.key in active_keys or now - u.last_access < protect_s:
        return True
    return any(p in active_paths for p in u.paths)


def plan_eviction(
    units: list[Unit],
    total: int,
    limit: int,
    now: float,
    protect_s: float,
    active_keys=frozenset(),
    active_paths=frozenset(),
    target: float = 0.85,
) -> list[Unit]:
    """Units to delete, in order. Nothing is planned while ``total <= limit``; once over, units
    are removed (processed before raw, least recently used first) until ``total`` would be
    ``<= limit * target``. Active (building) and recently used units are never selected."""
    if total <= limit:
        return []
    goal = limit * target
    cands = [u for u in units if not is_protected(u, now, protect_s, active_keys, active_paths)]
    cands.sort(key=lambda u: (KIND_ORDER[u.kind], u.last_access))
    out: list[Unit] = []
    freed = 0
    for u in cands:
        if total - freed <= goal:
            break
        out.append(u)
        freed += u.size
    return out


def build_units(cutouts_of: Callable[[str], list[Path]]) -> list[Unit]:
    """Scan the cache. ``cutouts_of(key)`` lists the raw cutout files that field's manifest
    refers to (supplied by fields.py, which knows how cutout names are derived)."""
    root = settings.cache_dir
    fields_root, fp_root, cut_root = root / "fields", root / "fingerprints", root / "cutouts"
    keys = {p.name for p in fields_root.iterdir() if p.is_dir()} if fields_root.is_dir() else set()
    fp_keys = {p.name for p in fp_root.iterdir() if p.is_dir()} if fp_root.is_dir() else set()
    units: list[Unit] = []
    access: dict[str, float] = {}
    for key in keys | fp_keys:
        paths = [p for p in (fields_root / key, fp_root / key) if p.exists()]
        access[key] = field_last_access(key) if key in keys else max(_mtime(p) for p in paths)
        units.append(Unit("processed", key, paths, sum(tree_size(p) for p in paths), access[key]))
    # Raw cutouts belong to the most recently used field whose manifest lists them.
    owner: dict[Path, str] = {}
    for key in keys:
        for p in cutouts_of(key):
            if p not in owner or access[key] > access[owner[p]]:
                owner[p] = key
    by_owner: dict[str, list[Path]] = {}
    orphans: list[Unit] = []
    if cut_root.is_dir():
        for coll in cut_root.iterdir():
            if not coll.is_dir():
                continue
            for f in coll.iterdir():
                if not f.is_file() or f.name.endswith(".tmp"):
                    continue
                if f in owner:
                    by_owner.setdefault(owner[f], []).append(f)
                    continue
                try:
                    st = f.stat()
                except OSError:
                    continue
                orphans.append(Unit("raw", f"orphan:{f.name}", [f], st.st_size, st.st_mtime, orphan=True))
    for key, files in by_owner.items():
        size = 0
        for f in files:
            try:
                size += f.stat().st_size
            except OSError:
                pass
        units.append(Unit("raw", key, files, size, access[key]))
    return units + orphans


def delete_unit(u: Unit) -> None:
    for p in u.paths:
        try:
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("cache eviction: could not remove %s: %s", p, exc)
    if u.kind == "processed":
        _touched.pop(u.key, None)


def enforce_cache_limit(
    cutouts_of: Callable[[str], list[Path]],
    active: Callable[[], tuple[set[str], set[Path]]],
    *,
    limit: int | None = None,
    now: float | None = None,
) -> dict:
    """Blocking. Evict least-recently-used fields until the cache is under its limit.
    ``active()`` returns (keys of fields being built, cutout files they use), evaluated again
    just before each deletion. Returns bytes before/after, what was evicted and which
    processed-product keys went (so the caller can drop their in-memory jobs)."""
    limit = settings.cache_max_bytes if limit is None else limit
    now = time.time() if now is None else now
    total = cache_usage()["bytes"]
    result: dict = {"bytes_before": total, "bytes_after": total, "evicted": [], "processed_keys": []}
    if total <= limit:
        return result
    units = build_units(cutouts_of)
    plan = plan_eviction(units, total, limit, now, settings.cache_protect_s, *active(),
                         target=settings.cache_evict_target)
    freed = 0
    for u in plan:
        keys, paths = active()  # the field may have been requested since the plan was made
        if u.kind == "processed" and not u.orphan:
            u.last_access = max(u.last_access, field_last_access(u.key))
        if is_protected(u, time.time(), settings.cache_protect_s, keys, paths):
            continue
        delete_unit(u)
        freed += u.size
        result["evicted"].append((u.kind, u.key, u.size))
        if u.kind == "processed":
            result["processed_keys"].append(u.key)
    if freed:
        _load.cache_clear()
    result["bytes_after"] = max(total - freed, 0)
    if result["bytes_after"] > limit:
        log.warning("cache still over its limit after eviction: %d > %d bytes (the rest is protected)",
                    result["bytes_after"], limit)
    return result
