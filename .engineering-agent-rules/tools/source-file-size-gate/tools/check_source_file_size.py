#!/usr/bin/env python3
"""源码文件物理行数门禁：基线 vs 候选，硬阈值默认失败。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


GATE_ID = "source-file-size-gate"
REPORT_SCHEMA_VERSION = 1
POLICY_VERSION = 1
MAXIMUM_THRESHOLD = 1000
BUILTIN_EXCLUDED_ROOTS = frozenset({".engineering-agent-rules", ".git"})
EMPTY_ALLOWLIST_BYTES = b'{"schema_version":1,"waivers":[]}\n'
PROFILE_REQUIRED = {
    "schema_version",
    "source_roots",
    "include_globs",
    "exclude_globs",
    "allowlist_path",
    "threshold",
}
ALLOWLIST_REQUIRED = {"schema_version", "waivers"}
WAIVER_REQUIRED = {
    "file_key",
    "threshold",
    "max_size",
    "reason",
    "approved_by",
    "approval_ref",
}


class GateError(Exception):
    """可操作的门禁失败。"""


def assert_repo_relative_path(value: str, *, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise GateError(f"{label} 必须为非空相对路径")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise GateError(f"{label} 必须是有效 UTF-8 路径：{value!r}") from exc
    path = Path(value)
    if path.is_absolute():
        raise GateError(f"{label} 禁止绝对路径：{value}")
    if ".." in path.parts:
        raise GateError(f"{label} 禁止包含 '..'：{value}")


def resolve_inside_repo(repo: Path, relative: str, *, label: str, expect: str) -> Path:
    assert_repo_relative_path(relative, label=label)
    repo_root = repo.resolve()
    cursor = repo
    for part in Path(relative).parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise GateError(f"{label} 禁止符号链接：{relative}")
    raw = repo / relative
    candidate = raw.resolve()
    try:
        candidate.relative_to(repo_root)
    except ValueError as exc:
        raise GateError(f"{label} 解析后位于仓库外：{relative} -> {candidate}") from exc
    if expect == "file":
        if not raw.is_file() or raw.is_symlink():
            raise GateError(f"{label} 必须是仓库内普通文件：{relative}")
    elif expect == "dir":
        if not raw.exists():
            raise GateError(f"{label} 不存在：{relative}")
        if not raw.is_dir() or raw.is_symlink():
            raise GateError(f"{label} 必须是仓库内普通目录：{relative}")
    return candidate


def read_regular_file_snapshot(path: Path, *, label: str) -> bytes:
    try:
        before = path.lstat()
    except OSError as exc:
        raise GateError(f"无法读取 {label} 文件类型：{path}: {exc}") from exc
    if not stat.S_ISREG(before.st_mode):
        raise GateError(f"{label} 必须是普通文件且不能是符号链接：{path}")
    try:
        contents = path.read_bytes()
        after = path.lstat()
    except OSError as exc:
        raise GateError(f"无法读取 {label}：{path}: {exc}") from exc
    before_identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if not stat.S_ISREG(after.st_mode) or before_identity != after_identity:
        raise GateError(f"{label} 在读取期间发生变化：{path}")
    return contents


def resolved_path_snapshot(path: Path, *, label: str) -> Path:
    try:
        return path.resolve(strict=True)
    except OSError as exc:
        raise GateError(f"无法解析 {label} 路径：{path}: {exc}") from exc


def assert_file_snapshot(
    path: Path,
    expected: bytes,
    *,
    label: str,
    expected_resolved_path: Path,
) -> None:
    current = read_regular_file_snapshot(path, label=label)
    if current != expected or resolved_path_snapshot(path, label=label) != expected_resolved_path:
        raise GateError(f"{label} 路径类型或内容在判定期间发生变化：{path}")


def load_json_snapshot(contents: bytes, path: Path) -> Any:
    try:
        return json.loads(contents)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise GateError(f"JSON 无效：{path}: {exc}") from exc


def require_object(data: Any, label: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise GateError(f"{label} 必须是对象")
    return data


def reject_unknown(data: dict[str, Any], allowed: set[str], label: str) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise GateError(f"{label} 含未知字段：{', '.join(unknown)}")


def load_profile_snapshot(contents: bytes, path: Path) -> dict[str, Any]:
    data = require_object(load_json_snapshot(contents, path), "profile")
    reject_unknown(data, PROFILE_REQUIRED, "profile")
    missing = sorted(PROFILE_REQUIRED - set(data))
    if missing:
        raise GateError(f"profile 缺少字段：{', '.join(missing)}")
    if data["schema_version"] != 1:
        raise GateError("profile.schema_version 必须为 1")
    roots = data["source_roots"]
    if not isinstance(roots, list) or not roots or not all(
        isinstance(item, str) and item.strip() for item in roots
    ):
        raise GateError("profile.source_roots 必须为非空字符串列表")
    for index, root in enumerate(roots):
        assert_repo_relative_path(root, label=f"profile.source_roots[{index}]")
    includes = data["include_globs"]
    if not isinstance(includes, list) or not includes or not all(
        isinstance(item, str) and item.strip() for item in includes
    ):
        raise GateError("profile.include_globs 必须为非空字符串列表")
    excludes = data["exclude_globs"]
    if not isinstance(excludes, list) or not all(isinstance(item, str) for item in excludes):
        raise GateError("profile.exclude_globs 必须为字符串列表")
    assert_repo_relative_path(data["allowlist_path"], label="profile.allowlist_path")
    threshold = data["threshold"]
    if not isinstance(threshold, int) or isinstance(threshold, bool) or threshold < 1:
        raise GateError("profile.threshold 必须为正整数")
    if threshold > MAXIMUM_THRESHOLD:
        raise GateError(
            f"profile.threshold 最大值为 {MAXIMUM_THRESHOLD}；允许更严格，不得放宽"
        )
    return data


def load_allowlist_snapshot(
    contents: bytes,
    path: Path,
    expected_threshold: int,
) -> dict[str, dict[str, Any]]:
    data = require_object(load_json_snapshot(contents, path), "allowlist")
    reject_unknown(data, ALLOWLIST_REQUIRED, "allowlist")
    if data.get("schema_version") != 1:
        raise GateError("allowlist.schema_version 必须为 1")
    waivers = data.get("waivers")
    if not isinstance(waivers, list):
        raise GateError("allowlist.waivers 必须为数组")
    by_key: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(waivers):
        label = f"allowlist.waivers[{index}]"
        entry = require_object(item, label)
        reject_unknown(entry, WAIVER_REQUIRED, label)
        missing = sorted(WAIVER_REQUIRED - set(entry))
        if missing:
            raise GateError(f"{label} 缺少字段：{', '.join(missing)}")
        file_key = entry["file_key"]
        assert_repo_relative_path(file_key, label=f"{label}.file_key")
        if file_key in by_key:
            raise GateError(f"allowlist 重复 file_key：{file_key}")
        threshold = entry["threshold"]
        if not isinstance(threshold, int) or isinstance(threshold, bool):
            raise GateError(f"{label}.threshold 必须为整数")
        if threshold != expected_threshold:
            raise GateError(
                f"{label}.threshold={threshold} 与 profile.threshold={expected_threshold} 不一致"
            )
        max_size = entry["max_size"]
        if not isinstance(max_size, int) or isinstance(max_size, bool):
            raise GateError(f"{label}.max_size 必须为整数")
        if max_size < expected_threshold:
            raise GateError(
                f"{label}.max_size={max_size} 不得小于 profile.threshold={expected_threshold}"
            )
        for field in ("reason", "approved_by", "approval_ref"):
            if not isinstance(entry[field], str) or not entry[field].strip():
                raise GateError(f"{label}.{field} 必须为非空字符串")
        by_key[file_key] = entry
    return by_key


def load_allowlist_from_repo(
    repo: Path,
    relative: str,
    threshold: int,
) -> dict[str, Any]:
    """Allowlist 文件缺失视为空表；路径上的符号链接或非普通文件失败。"""
    assert_repo_relative_path(relative, label="profile.allowlist_path")
    cursor = repo
    for part in Path(relative).parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise GateError(f"profile.allowlist_path 禁止符号链接：{relative}")
        if not cursor.exists():
            return {
                "present": False,
                "path": repo / relative,
                "resolved": None,
                "snapshot": EMPTY_ALLOWLIST_BYTES,
                "waivers": {},
            }
    path = resolve_inside_repo(
        repo, relative, label="profile.allowlist_path", expect="file"
    )
    snapshot = read_regular_file_snapshot(path, label="allowlist")
    return {
        "present": True,
        "path": path,
        "resolved": resolved_path_snapshot(path, label="allowlist"),
        "snapshot": snapshot,
        "waivers": load_allowlist_snapshot(snapshot, path, threshold),
    }


def is_builtin_excluded(relative: str) -> bool:
    root = relative.split("/", 1)[0]
    return root in BUILTIN_EXCLUDED_ROOTS


def matches(relative: str, patterns: list[str], *, directory: bool = False) -> bool:
    probes = [relative]
    if directory:
        probes.append(f"{relative.rstrip('/')}/_")
    return any(Path(probe).match(pattern) for pattern in patterns for probe in probes)


def is_excluded(relative: str, patterns: list[str], *, directory: bool = False) -> bool:
    return is_builtin_excluded(relative) or matches(relative, patterns, directory=directory)


def is_included(relative: str, patterns: list[str]) -> bool:
    return matches(relative, patterns)


def resolved_source_roots(root: Path, source_roots: list[str]) -> list[Path]:
    bases: list[Path] = []
    for index, relative in enumerate(source_roots):
        base = resolve_inside_repo(
            root, relative, label=f"source_roots[{index}]", expect="dir"
        )
        for previous in bases:
            try:
                base.relative_to(previous)
                overlaps = True
            except ValueError:
                try:
                    previous.relative_to(base)
                    overlaps = True
                except ValueError:
                    overlaps = False
            if overlaps:
                raise GateError(
                    "profile.source_roots 禁止重复或重叠："
                    f"{previous.relative_to(root.resolve())} 与 {relative}"
                )
        bases.append(base)
    return bases


def list_source_files(
    root: Path,
    source_roots: list[str],
    include_globs: list[str],
    exclude_globs: list[str],
) -> list[Path]:
    repo_root = root.resolve()
    files: dict[str, Path] = {}

    def fail_walk(error: OSError) -> None:
        raise GateError(f"无法遍历 source_roots：{error}") from error

    for base in resolved_source_roots(root, source_roots):
        for current, directory_names, file_names in os.walk(
            base, followlinks=False, onerror=fail_walk
        ):
            current_path = Path(current)
            for name in list(directory_names):
                path = current_path / name
                relative = path.relative_to(repo_root).as_posix()
                assert_repo_relative_path(relative, label="candidate directory path")
                if is_excluded(relative, exclude_globs, directory=True):
                    directory_names.remove(name)
                    continue
                if path.is_symlink():
                    raise GateError(f"source_roots 内禁止未排除的目录符号链接：{relative}")
            for name in file_names:
                path = current_path / name
                relative = path.relative_to(repo_root).as_posix()
                assert_repo_relative_path(relative, label="candidate file path")
                if is_excluded(relative, exclude_globs):
                    continue
                if path.is_symlink():
                    raise GateError(f"source_roots 内禁止未排除的文件符号链接：{relative}")
                if not path.is_file():
                    raise GateError(f"source_roots 内条目必须是普通文件：{relative}")
                if is_included(relative, include_globs):
                    files[relative] = path
    return [files[key] for key in sorted(files)]


def resolve_baseline_commit(repo: Path, baseline_ref: str) -> str:
    probe = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", f"{baseline_ref}^{{commit}}"],
        text=True,
        capture_output=True,
        check=False,
    )
    if probe.returncode != 0:
        raise GateError(
            f"无法解析显式基线 commit `{baseline_ref}`（不得猜测 HEAD~1）。"
            f"请传入存在的 commit-ish。git: {(probe.stderr or probe.stdout).strip()}"
        )
    commit = probe.stdout.strip()
    if not commit:
        raise GateError(f"显式基线 commit `{baseline_ref}` 解析结果为空")
    return commit


def export_baseline_tree(
    repo: Path,
    baseline_commit: str,
    source_roots: list[str],
    include_globs: list[str],
    exclude_globs: list[str],
    destination: Path,
) -> list[Path]:
    exported: dict[str, Path] = {}
    for rel_root in source_roots:
        (destination / rel_root).mkdir(parents=True, exist_ok=True)
        listing = subprocess.run(
            ["git", "-C", str(repo), "ls-tree", "-r", "-z", baseline_commit, "--", rel_root],
            capture_output=True,
            check=False,
        )
        if listing.returncode != 0:
            raise GateError(
                f"git ls-tree 失败（baseline={baseline_commit}）："
                f"{os.fsdecode((listing.stderr or listing.stdout).strip())}"
            )
        for record in listing.stdout.split(b"\0"):
            if not record:
                continue
            try:
                header, path_bytes = record.split(b"\t", 1)
                mode, object_type, object_id = header.split(b" ", 2)
            except ValueError as exc:
                raise GateError(f"git ls-tree 返回无法解析的 NUL 记录：{record!r}") from exc
            relative = os.fsdecode(path_bytes)
            assert_repo_relative_path(relative, label="baseline tree path")
            if is_excluded(relative, exclude_globs):
                continue
            ordinary_blob = mode in {b"100644", b"100755"} and object_type == b"blob"
            if not ordinary_blob:
                raise GateError(
                    "baseline source_roots 只允许普通 blob；"
                    f"拒绝 mode={os.fsdecode(mode)} type={os.fsdecode(object_type)} "
                    f"path={relative!r}"
                )
            if not is_included(relative, include_globs) or relative in exported:
                continue
            blob = subprocess.run(
                ["git", "-C", str(repo), "cat-file", "blob", os.fsdecode(object_id)],
                capture_output=True,
                check=False,
            )
            if blob.returncode != 0:
                raise GateError(
                    f"无法读取基线 blob {os.fsdecode(object_id)} ({relative!r})："
                    f"{os.fsdecode((blob.stderr or blob.stdout).strip())}"
                )
            output = destination / relative
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(blob.stdout)
            exported[relative] = output.resolve()
    return [exported[key] for key in sorted(exported)]


def physical_lines(contents: bytes, *, file_key: str) -> int:
    if b"\0" in contents:
        raise GateError(f"源码文件含 NUL，拒绝按物理行数度量：{file_key}")
    if not contents:
        return 0
    return contents.count(b"\n") + (0 if contents.endswith(b"\n") else 1)


def update_source_digest(digest: Any, relative: str, contents: bytes) -> None:
    relative_bytes = relative.encode("utf-8")
    digest.update(len(relative_bytes).to_bytes(8, "big"))
    digest.update(relative_bytes)
    digest.update(len(contents).to_bytes(8, "big"))
    digest.update(contents)


def capture_and_measure(
    root: Path,
    files: list[Path],
    destination: Path,
    *,
    label: str,
) -> tuple[dict[str, int], list[str], dict[str, Any]]:
    root_resolved = root.resolve()
    digest = hashlib.sha256()
    sizes: dict[str, int] = {}
    paths: list[str] = []
    try:
        destination.mkdir(parents=True, exist_ok=False)
        for path in sorted(files, key=lambda item: item.relative_to(root_resolved).as_posix()):
            relative = path.relative_to(root_resolved).as_posix()
            contents = read_regular_file_snapshot(path, label=f"{label} source {relative}")
            snapshot_path = destination / relative
            snapshot_path.parent.mkdir(parents=True, exist_ok=True)
            with snapshot_path.open("xb") as handle:
                handle.write(contents)
            snapshot_path.chmod(0o444)
            update_source_digest(digest, relative, contents)
            sizes[relative] = physical_lines(contents, file_key=relative)
            paths.append(relative)
    except OSError as exc:
        raise GateError(f"无法创建{label}源码不可变快照：{exc}") from exc
    return sizes, paths, {
        "source_file_count": len(paths),
        "source_digest": digest.hexdigest(),
    }


def source_evidence_and_sizes(
    root: Path,
    files: list[Path],
    *,
    label: str,
) -> tuple[dict[str, int], list[str], dict[str, Any]]:
    root_resolved = root.resolve()
    digest = hashlib.sha256()
    sizes: dict[str, int] = {}
    paths: list[str] = []
    for path in sorted(files, key=lambda item: item.relative_to(root_resolved).as_posix()):
        relative = path.relative_to(root_resolved).as_posix()
        contents = read_regular_file_snapshot(path, label=f"{label} source {relative}")
        update_source_digest(digest, relative, contents)
        sizes[relative] = physical_lines(contents, file_key=relative)
        paths.append(relative)
    return sizes, paths, {
        "source_file_count": len(paths),
        "source_digest": digest.hexdigest(),
    }


def waiver_status(hit: dict[str, Any], waiver: dict[str, Any] | None) -> dict[str, Any]:
    if waiver is None:
        return {"status": "not_listed", "reason": "allowlist_no_matching_file_key"}
    evidence = {
        "max_size": waiver["max_size"],
        "reason": waiver["reason"],
        "approved_by": waiver["approved_by"],
        "approval_ref": waiver["approval_ref"],
    }
    if hit["candidate_size"] <= waiver["max_size"]:
        return {"status": "within_max_size", **evidence}
    return {
        "status": "exceeds_max_size",
        "unwaived_reason": "candidate_exceeds_max_size",
        **evidence,
    }


def threshold_hits(
    baseline: dict[str, int],
    candidate: dict[str, int],
    threshold: int,
    waivers: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    for file_key, candidate_size in sorted(candidate.items()):
        baseline_size = baseline.get(file_key)
        kind: str | None = None
        if baseline_size is None and candidate_size >= threshold:
            kind = "new_file"
        elif baseline_size is not None and baseline_size < threshold <= candidate_size:
            kind = "first_cross"
        elif (
            baseline_size is not None
            and baseline_size >= threshold
            and candidate_size > baseline_size
        ):
            kind = "over_threshold_growth"
        if kind:
            hit = {
                "file_key": file_key,
                "kind": kind,
                "baseline_size": baseline_size,
                "candidate_size": candidate_size,
                "threshold": threshold,
            }
            hit["waiver"] = waiver_status(hit, waivers.get(file_key))
            hits.append(hit)
    return hits


def describe_hit(hit: dict[str, Any], threshold: int) -> str:
    if hit["kind"] == "new_file":
        return f"新建文件 {hit['file_key']} 物理行数 {hit['candidate_size']} >= {threshold}"
    if hit["kind"] == "first_cross":
        return (
            f"文件 {hit['file_key']} 首次越过阈值：基线 {hit['baseline_size']} -> "
            f"候选 {hit['candidate_size']}（阈值 {threshold}）"
        )
    return (
        f"文件 {hit['file_key']} 已超阈且继续增长：基线 {hit['baseline_size']} -> "
        f"候选 {hit['candidate_size']}（阈值 {threshold}）"
    )


def run_gate(
    *,
    repo: Path,
    profile_path: Path,
    baseline_ref: str,
    include_all_sizes: bool = False,
) -> int:
    profile_snapshot = read_regular_file_snapshot(profile_path, label="profile")
    profile_resolved = resolved_path_snapshot(profile_path, label="profile")
    profile = load_profile_snapshot(profile_snapshot, profile_path)
    roots = profile["source_roots"]
    includes = profile["include_globs"]
    excludes = profile["exclude_globs"]
    threshold = profile["threshold"]
    allowlist = load_allowlist_from_repo(repo, profile["allowlist_path"], threshold)
    waivers = allowlist["waivers"]
    candidate_files = list_source_files(repo, roots, includes, excludes)
    baseline_commit = resolve_baseline_commit(repo, baseline_ref)

    with tempfile.TemporaryDirectory(prefix="source-file-size-snapshots-") as temporary:
        snapshot_root = Path(temporary)
        candidate_sizes, candidate_paths, candidate_snapshot_evidence = capture_and_measure(
            repo, candidate_files, snapshot_root / "candidate", label="候选"
        )
        candidate_evidence = {"root": str(repo.resolve()), **candidate_snapshot_evidence}
        baseline_root = snapshot_root / "baseline"
        baseline_files = export_baseline_tree(
            repo, baseline_commit, roots, includes, excludes, baseline_root
        )
        baseline_sizes, _, baseline_source_evidence = source_evidence_and_sizes(
            baseline_root, baseline_files, label="基线"
        )
        baseline_evidence = {
            "requested_ref": baseline_ref,
            "commit": baseline_commit,
            **baseline_source_evidence,
        }

    try:
        candidate_files_after = list_source_files(repo, roots, includes, excludes)
    except (GateError, OSError) as exc:
        raise GateError(f"候选完成前重枚举失败：{exc}") from exc
    candidate_sizes_after, candidate_paths_after, candidate_evidence_after = (
        source_evidence_and_sizes(repo, candidate_files_after, label="候选复核")
    )
    if (
        candidate_paths != candidate_paths_after
        or candidate_sizes != candidate_sizes_after
        or any(
            candidate_evidence[key] != candidate_evidence_after[key]
            for key in candidate_evidence_after
        )
    ):
        raise GateError(
            "候选源码路径集合、文件类型或内容在度量期间发生变化；"
            "拒绝输出错配证据，请冻结后重跑"
        )
    assert_file_snapshot(
        profile_path,
        profile_snapshot,
        label="profile",
        expected_resolved_path=profile_resolved,
    )
    if allowlist["present"]:
        assert_file_snapshot(
            allowlist["path"],
            allowlist["snapshot"],
            label="allowlist",
            expected_resolved_path=allowlist["resolved"],
        )
    elif allowlist["path"].exists():
        raise GateError("allowlist 在判定期间被创建；拒绝输出错配证据，请冻结后重跑")

    hits = threshold_hits(baseline_sizes, candidate_sizes, threshold, waivers)
    violations = [
        describe_hit(hit, threshold)
        for hit in hits
        if hit["waiver"]["status"] != "within_max_size"
    ]
    waiver_evidence = [
        {
            **{key: value for key, value in hit.items() if key != "waiver"},
            **hit["waiver"],
        }
        for hit in hits
        if hit["waiver"]["status"] == "within_max_size"
    ]
    verdict = "block" if violations else "waived" if waiver_evidence else "pass"
    report = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "gate": {
            "id": GATE_ID,
            "policy_version": POLICY_VERSION,
            "threshold": threshold,
            "maximum_threshold": MAXIMUM_THRESHOLD,
            "profile_path": str(profile_path),
            "profile_sha256": hashlib.sha256(profile_snapshot).hexdigest(),
            "allowlist_path": profile["allowlist_path"],
            "allowlist_sha256": hashlib.sha256(allowlist["snapshot"]).hexdigest(),
            "source_roots": roots,
            "include_globs": includes,
            "exclude_globs": excludes,
        },
        "verdict": verdict,
        "threshold": threshold,
        "baseline_ref": baseline_ref,
        "baseline": baseline_evidence,
        "candidate": candidate_evidence,
        "baseline_file_count": len(baseline_sizes),
        "candidate_file_count": len(candidate_sizes),
        "waiver_count": len(waivers),
        "hits": hits,
        "violations": violations,
        "waiver_evidence": waiver_evidence,
    }
    if include_all_sizes:
        report["baseline_sizes"] = baseline_sizes
        report["candidate_sizes"] = candidate_sizes
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    if violations:
        print("结构门禁：阻塞", file=sys.stderr)
        return 1
    if waiver_evidence:
        print("结构门禁：豁免记录命中", file=sys.stderr)
        return 0
    print("结构门禁：通过", file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="源码文件物理行数硬阈值门禁")
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="使用方仓库根（默认 cwd）")
    parser.add_argument("--profile", type=Path, required=True, help="门禁 profile JSON")
    parser.add_argument(
        "--baseline-ref",
        required=True,
        help="显式 git commit-ish；缺失或无法解析则失败，绝不猜测 HEAD~1",
    )
    parser.add_argument(
        "--include-all-sizes",
        action="store_true",
        help="局部诊断时附加 baseline_sizes/candidate_sizes 全量表；默认省略",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run_gate(
            repo=args.root.resolve(),
            profile_path=args.profile.absolute(),
            baseline_ref=args.baseline_ref,
            include_all_sizes=args.include_all_sizes,
        )
    except GateError as exc:
        print(
            json.dumps(
                {
                    "schema_version": REPORT_SCHEMA_VERSION,
                    "gate": {"id": GATE_ID, "policy_version": POLICY_VERSION},
                    "verdict": "error",
                    "error": str(exc),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        print(f"结构门禁：阻塞\n{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
