"""Repository snapshots and candidate fingerprints."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat
import subprocess
from typing import Any, Iterable

from . import model


def _git(repo: Path, arguments: list[str]) -> bytes:
    result = subprocess.run(
        ["git", *arguments], cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise model.ValidationError(result.stderr.decode("utf-8", errors="replace").strip())
    return result.stdout


def verify_repo(repo: Path) -> None:
    root = _git(repo, ["rev-parse", "--show-toplevel"]).decode().strip()
    if Path(root).resolve() != repo.resolve():
        raise model.ValidationError("--repo must be the exact Git worktree root")


def _entry(repo: Path, relative: str) -> dict[str, str]:
    path = repo / relative
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {"path": relative, "sha256": "deleted"}
    if stat.S_ISLNK(info.st_mode):
        target = os.readlink(path).encode("utf-8", errors="surrogateescape")
        digest = hashlib.sha256(b"symlink\0" + target).hexdigest()
        return {"path": relative, "sha256": digest}
    if not stat.S_ISREG(info.st_mode):
        raise model.ValidationError(f"candidate path is not a regular file: {relative}")
    return {"path": relative, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def snapshot(repo: Path, roots: Iterable[str]) -> list[dict[str, str]]:
    selected = sorted(set(roots))
    output = _git(
        repo,
        ["ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", *selected],
    )
    paths = sorted(
        {
            item.decode("utf-8", errors="surrogateescape")
            for item in output.split(b"\0")
            if item
        }
    )
    return [_entry(repo, path) for path in paths]


def fingerprint(files: list[dict[str, str]], *, scope: str) -> str:
    return model.digest_value(f"agent-task-runtime:{scope}-candidate:v1", files)


def changes(
    before: list[dict[str, str]],
    after: list[dict[str, str]],
    *,
    roots: Iterable[str] | None = None,
) -> list[dict[str, str]]:
    before_map = {entry["path"]: entry["sha256"] for entry in before}
    after_map = {entry["path"]: entry["sha256"] for entry in after}
    selected_roots = list(roots or [])
    result: list[dict[str, str]] = []
    for path in sorted(set(before_map) | set(after_map)):
        if selected_roots and not any(model.path_is_within(path, root) for root in selected_roots):
            continue
        old = before_map.get(path, "absent")
        new = after_map.get(path, "deleted")
        if old != new:
            result.append({"path": path, "sha256": new})
    return result


def stale_paths(expected: list[dict[str, str]], current: list[dict[str, str]]) -> list[str]:
    return [entry["path"] for entry in changes(expected, current)]


def preview(files: list[dict[str, str]], *, scope: str) -> dict[str, Any]:
    return {"fingerprint": fingerprint(files, scope=scope), "files": files}
