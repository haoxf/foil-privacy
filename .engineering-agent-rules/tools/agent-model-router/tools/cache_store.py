#!/usr/bin/env python3
"""Small hardened JSON cache primitives shared by model-routing tools."""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any


DEFAULT_CACHE_DIRECTORY = Path.home() / "Library/Caches/engineering-agent-rules"
MAX_CACHE_BYTES = 2 * 1024 * 1024


class CacheUnavailable(RuntimeError):
    """The cache path cannot be used without weakening its ownership boundary."""


def cache_path(name: str) -> Path:
    if not name or "/" in name or name in {".", ".."}:
        raise ValueError("cache name must be one plain path component")
    return DEFAULT_CACHE_DIRECTORY / name


def _safe_directory(path: Path) -> None:
    directory = path.parent
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        metadata = directory.lstat()
    except OSError as error:
        raise CacheUnavailable("cache directory unavailable") from error
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
    ):
        raise CacheUnavailable("unsafe cache directory")
    try:
        os.chmod(directory, 0o700)
    except OSError as error:
        raise CacheUnavailable("cache directory unavailable") from error


def lock(path: Path) -> int:
    _safe_directory(path)
    lock_path = path.with_name(path.name + ".lock")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(lock_path, flags, 0o600)
        opened = os.fstat(descriptor)
        current = os.stat(lock_path, follow_symlinks=False)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
            or (hasattr(os, "getuid") and opened.st_uid != os.getuid())
        ):
            raise OSError("unsafe cache lock")
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        return descriptor
    except OSError as error:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise CacheUnavailable("cache lock unavailable") from error


def unlock(descriptor: int | None) -> None:
    if descriptor is None:
        return
    try:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def read(path: Path, *, max_bytes: int = MAX_CACHE_BYTES) -> Any | None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise CacheUnavailable("cache file unavailable") from error
    if (
        not stat.S_ISREG(metadata.st_mode)
        or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
        or metadata.st_mode & 0o077
    ):
        raise CacheUnavailable("unsafe cache file")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        opened_before = os.fstat(descriptor)
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        opened_after = os.fstat(descriptor)
        current = os.stat(path, follow_symlinks=False)
    except OSError as error:
        raise CacheUnavailable("cache file unavailable") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if (
        len(payload) > max_bytes
        or not stat.S_ISREG(opened_before.st_mode)
        or (opened_before.st_dev, opened_before.st_ino)
        != (opened_after.st_dev, opened_after.st_ino)
        or (opened_after.st_dev, opened_after.st_ino)
        != (current.st_dev, current.st_ino)
        or opened_before.st_size != opened_after.st_size
        or opened_before.st_mtime_ns != opened_after.st_mtime_ns
    ):
        raise CacheUnavailable("cache changed while reading")
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def write(path: Path, value: Any, *, max_bytes: int = MAX_CACHE_BYTES) -> None:
    _safe_directory(path)
    payload = (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    if len(payload) > max_bytes:
        raise CacheUnavailable("cache payload too large")
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "wb", delete=False, dir=path.parent, prefix=f".{path.name}-", suffix=".tmp"
        ) as temporary:
            temporary.write(payload)
            temporary.flush()
            os.fchmod(temporary.fileno(), 0o600)
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        try:
            existing = path.lstat()
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISREG(existing.st_mode):
                raise OSError("unsafe cache file")
        os.replace(temporary_path, path)
        temporary_path = None
    except OSError as error:
        raise CacheUnavailable("cache write unavailable") from error
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except OSError:
                pass


def current_generation_locked(path: Path) -> int:
    """Read the invalidation generation while the caller holds ``lock(path)``."""

    generation_path = path.with_name(path.name + ".generation.json")
    value = read(generation_path, max_bytes=4096)
    generation = value.get("generation", 0) if type(value) is dict else 0
    return generation if type(generation) is int and generation >= 0 else 0


def capture_generation(path: Path) -> int:
    """Capture a quota-write fence before starting external refresh work."""

    descriptor: int | None = None
    try:
        descriptor = lock(path)
        return current_generation_locked(path)
    finally:
        unlock(descriptor)


def invalidate(path: Path) -> dict[str, Any]:
    descriptor: int | None = None
    generation_path = path.with_name(path.name + ".generation.json")
    try:
        descriptor = lock(path)
        generation = current_generation_locked(path)
        write(
            generation_path,
            {"schema_version": 1, "generation": generation + 1},
            max_bytes=4096,
        )
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            removed = False
        else:
            if (
                not stat.S_ISREG(metadata.st_mode)
                or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
            ):
                raise CacheUnavailable("unsafe cache file")
            path.unlink()
            removed = True
        return {"state": "invalidated", "removed": removed}
    except (CacheUnavailable, OSError):
        return {"state": "unavailable", "removed": False}
    finally:
        unlock(descriptor)


def mark_refresh_due(
    path: Path, *, minimum_interval_seconds: float = 120.0,
    now_epoch: float | None = None, reservation_key: str = "global",
) -> bool:
    """Atomically reserve one asynchronous refresh launch across processes."""
    descriptor: int | None = None
    marker = path.with_name(path.name + ".refresh.json")
    if not reservation_key or len(reservation_key) > 256 or "\n" in reservation_key:
        return False
    try:
        descriptor = lock(path)
        sampled = time.time() if now_epoch is None else now_epoch
        generation = current_generation_locked(path)
        value = read(marker, max_bytes=4096)
        previous = value.get("started_at") if type(value) is dict else None
        if (
            type(previous) in (int, float)
            and value.get("generation") == generation
            and value.get("reservation_key") == reservation_key
            and sampled - float(previous) < max(0.0, minimum_interval_seconds)
        ):
            return False
        write(marker, {
            "schema_version": 1,
            "generation": generation,
            "reservation_key": reservation_key,
            "started_at": sampled,
        }, max_bytes=4096)
        return True
    except CacheUnavailable:
        return False
    finally:
        unlock(descriptor)


def spawn_refresh(script: Path, *arguments: str) -> bool:
    """Launch a detached refresh worker without inheriting caller I/O."""
    try:
        subprocess.Popen(
            [sys.executable, str(script), *arguments],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
        return True
    except OSError:
        return False
