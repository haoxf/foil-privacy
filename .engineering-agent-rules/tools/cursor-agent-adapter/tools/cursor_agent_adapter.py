#!/usr/bin/env python3
"""Small synchronous bridge to the Cursor Agent CLI."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from typing import Any


SCHEMA_VERSION = 1
DEFAULT_TIMEOUT_SECONDS = 3600.0
HEARTBEAT_INTERVAL_SECONDS = 25.0
MIN_HEARTBEAT_INTERVAL_SECONDS = 0.25
PROCESS_EXIT_GRACE_SECONDS = 1.0
STDERR_LIMIT = 1200
_SAFE_PROGRESS_TOKEN = re.compile(r"^[a-z0-9_]+$")
_UPPER_REVIEW_MARKER = re.compile(
    r"(?m)^AGENT_MODEL_REVIEW:\s*(PASS|BLOCKED|UNAVAILABLE)\s*$",
)


def _review_prompt(
    prompt: str, required_tier: str, required_reasoning_depth: str
) -> str:
    return prompt.rstrip() + f"""

---
Upper scheduler read-only review (required tier: {required_tier}; required
reasoning depth: {required_reasoning_depth})

Remain read-only. Inspect the frozen acceptance criteria, actual diff, production
path and supplied verification. Findings block only for acceptance violations,
reproducible supported-environment defects, directly related production-path
errors, or clear security/data/state/lifecycle/concurrency risk. Report findings
first with precise paths and evidence; defer style and unrelated cleanup.

If the stable candidate passes, end with exactly: AGENT_MODEL_REVIEW: PASS
If it has blockers, end with exactly: AGENT_MODEL_REVIEW: BLOCKED
If the evidence cannot support a real review, end with exactly:
AGENT_MODEL_REVIEW: UNAVAILABLE
"""


class _ProgressReporter:
    """Emit fixed-shape progress without forwarding Cursor-owned content."""

    def __init__(self) -> None:
        self._stream = sys.stderr
        self._started_at = time.monotonic()
        self._next_heartbeat = self._started_at + HEARTBEAT_INTERVAL_SECONDS
        self._event_count = 0
        self._phase = "starting"
        self._emitted: set[str] = set()
        self._enabled = True

    def _write(self, stage: str, **fields: int | bool | str) -> None:
        if not self._enabled:
            return
        if not _SAFE_PROGRESS_TOKEN.fullmatch(stage):  # pragma: no cover - internal guard
            raise ValueError("unsafe progress stage")
        parts = [
            "cursor-adapter progress", f"stage={stage}",
            f"elapsed={time.monotonic() - self._started_at:.1f}s",
            f"events={self._event_count}",
        ]
        for key, value in fields.items():
            if not _SAFE_PROGRESS_TOKEN.fullmatch(key):  # pragma: no cover - internal guard
                raise ValueError("unsafe progress field")
            if isinstance(value, bool):
                rendered = "true" if value else "false"
            elif isinstance(value, int):
                rendered = str(value)
            elif _SAFE_PROGRESS_TOKEN.fullmatch(value):
                rendered = value
            else:  # pragma: no cover - internal guard
                raise ValueError("unsafe progress value")
            parts.append(f"{key}={rendered}")
        try:
            print(" ".join(parts), file=self._stream, flush=True)
        except (BlockingIOError, OSError, ValueError):
            self._enabled = False

    def _once(self, stage: str, **fields: int | bool | str) -> None:
        if stage in self._emitted:
            return
        self._emitted.add(stage)
        self._phase = stage
        self._write(stage, **fields)

    def started(self, attempt: int) -> None:
        self._once("started", attempt=attempt)

    def event(self, *, model_receipt: bool, attempt: int) -> None:
        self._event_count += 1
        if model_receipt:
            self._once("model_receipt", attempt=attempt)

    def heartbeat_delay(self) -> float:
        return max(0.0, self._next_heartbeat - time.monotonic())

    def heartbeat_if_due(self, attempt: int) -> None:
        now = time.monotonic()
        if now < self._next_heartbeat:
            return
        self._write("heartbeat", attempt=attempt, phase=self._phase)
        self._next_heartbeat = now + max(
            HEARTBEAT_INTERVAL_SECONDS, MIN_HEARTBEAT_INTERVAL_SECONDS,
        )

    def process_cleanup(self, reason: str, attempt: int) -> None:
        self._once("process_cleanup", attempt=attempt, reason=reason)

    def tls_retry(self, attempt: int) -> None:
        self._once("tls_retry", attempt=attempt)

    def workspace_audit(
        self, *, changed: int, outside: int, git_changed: bool,
    ) -> None:
        self._once(
            "workspace_audit", changed=changed, outside=outside,
            git_changed=git_changed,
        )

    def completed(self, status: str, attempts: int) -> None:
        self._once("completed", status=status, attempts=attempts)


class _StreamProgress:
    def __init__(self, reporter: _ProgressReporter, attempt: int) -> None:
        self._reporter = reporter
        self._attempt = attempt
        self._pending = bytearray()
        self.terminal_success = False

    def feed(self, chunk: bytes) -> None:
        self._pending.extend(chunk)
        while True:
            newline = self._pending.find(b"\n")
            if newline < 0:
                return
            line = bytes(self._pending[:newline])
            del self._pending[:newline + 1]
            self._consume(line)

    def finish(self) -> None:
        if self._pending:
            self._consume(bytes(self._pending))
            self._pending.clear()

    def _consume(self, line: bytes) -> None:
        if not line.strip():
            return
        try:
            event = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        if type(event) is not dict:
            return
        model_receipt = (
            event.get("type") == "system" and event.get("subtype") == "init"
            and type(event.get("model")) is str and bool(event["model"].strip())
        )
        if event.get("type") == "result":
            self.terminal_success = (
                event.get("subtype") == "success" and event.get("is_error") is False
            )
        self._reporter.event(model_receipt=model_receipt, attempt=self._attempt)


def _resolve_executable(command: str | None) -> str | None:
    for candidate in ([command] if command else ["cursor-agent", "agent"]):
        if not candidate:
            continue
        if os.sep in candidate:
            path = Path(candidate).expanduser()
            if path.is_file() and os.access(path, os.X_OK):
                return str(path.resolve())
        else:
            resolved = shutil.which(candidate)
            if resolved:
                return str(Path(resolved).resolve())
    return None


def _text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value


def _process_group_alive(group: int) -> bool:
    if os.name != "posix":  # pragma: no cover
        return False
    try:
        os.killpg(group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _stop_group(process: subprocess.Popen[str]) -> bool:
    if os.name != "posix":  # pragma: no cover
        process.kill()
        process.wait()
        return True
    for action in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, action)
        except ProcessLookupError:
            return True
        except PermissionError:
            try:
                process.kill()
                process.wait(timeout=0.5)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                return False
            return not _process_group_alive(process.pid)
        deadline = time.monotonic() + 0.5
        while _process_group_alive(process.pid) and time.monotonic() < deadline:
            time.sleep(0.01)
        if not _process_group_alive(process.pid):
            return True
    return False


def _run_process(
    command: list[str], *, cwd: Path | None, input_text: str | None,
    timeout_seconds: float, env: Mapping[str, str] | None,
    progress: _ProgressReporter | None = None, attempt: int = 1,
) -> dict[str, Any]:
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    if progress is not None:
        progress.started(attempt)
    try:
        process = subprocess.Popen(
            command, cwd=cwd, env=None if env is None else dict(env),
            stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=os.name == "posix",
        )
    except OSError as error:
        return {
            "returncode": None, "stdout": "", "stderr": str(error),
            "timed_out": False, "launch_error": True,
            "lingering": False, "quiet": True,
            "root_forced_cleanup": False,
        }
    selector = selectors.DefaultSelector()
    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    stream_progress = _StreamProgress(progress, attempt) if progress is not None else None
    input_bytes = input_text.encode("utf-8") if input_text is not None else b""
    input_offset = 0
    assert process.stdout is not None
    assert process.stderr is not None
    os.set_blocking(process.stdout.fileno(), False)
    os.set_blocking(process.stderr.fileno(), False)
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    if process.stdin is not None:
        os.set_blocking(process.stdin.fileno(), False)
        selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")

    def close_stream(file_object: Any) -> None:
        try:
            selector.unregister(file_object)
        except KeyError:
            pass
        file_object.close()

    timed_out = False
    lingering = False
    quiet = True
    group_finalised = False
    root_forced_cleanup = False
    deadline = time.monotonic() + timeout_seconds
    exit_grace_deadline: float | None = None
    drain_deadline: float | None = None
    try:
        while selector.get_map() or process.poll() is None or not group_finalised:
            now = time.monotonic()
            returncode = process.poll()
            terminal_success = bool(
                stream_progress is not None and stream_progress.terminal_success
            )
            group_alive = returncode is None or _process_group_alive(process.pid)
            if not group_finalised and now >= deadline:
                timed_out = True
                if progress is not None:
                    progress.process_cleanup("timeout", attempt)
                quiet = _stop_group(process) if group_alive else True
                lingering = False
                group_finalised = True
                drain_deadline = time.monotonic() + 1.0
                if group_alive:
                    try:
                        process.wait(timeout=0.5)
                    except subprocess.TimeoutExpired:
                        pass
            elif returncode is None and terminal_success:
                if exit_grace_deadline is None:
                    exit_grace_deadline = min(
                        deadline, now + PROCESS_EXIT_GRACE_SECONDS,
                    )
                elif now >= exit_grace_deadline:
                    lingering = True
                    root_forced_cleanup = True
                    if progress is not None:
                        progress.process_cleanup("terminal_result", attempt)
                    quiet = _stop_group(process)
                    try:
                        process.wait(timeout=0.5)
                    except subprocess.TimeoutExpired:
                        quiet = False
                    quiet = quiet or not _process_group_alive(process.pid)
                    group_finalised = True
                    drain_deadline = time.monotonic() + 1.0
            elif returncode is not None and not group_finalised:
                if not group_alive:
                    group_finalised = True
                    drain_deadline = time.monotonic() + 1.0
                elif exit_grace_deadline is None:
                    exit_grace_deadline = min(
                        deadline, now + PROCESS_EXIT_GRACE_SECONDS,
                    )
                elif now >= exit_grace_deadline:
                    lingering = True
                    if progress is not None:
                        progress.process_cleanup("lingering", attempt)
                    quiet = _stop_group(process)
                    group_finalised = True
                    drain_deadline = time.monotonic() + 1.0

            if progress is not None:
                progress.heartbeat_if_due(attempt)
            now = time.monotonic()
            if drain_deadline is not None and now >= drain_deadline:
                break
            waits = [0.1]
            if not group_finalised:
                waits.append(max(0.0, deadline - now))
            if exit_grace_deadline is not None:
                waits.append(max(0.0, exit_grace_deadline - now))
            if progress is not None:
                waits.append(progress.heartbeat_delay())
            events = selector.select(min(waits))
            for key, _ in events:
                file_object = key.fileobj
                if key.data == "stdin":
                    if input_offset >= len(input_bytes):
                        close_stream(file_object)
                        continue
                    try:
                        written = os.write(
                            file_object.fileno(),
                            input_bytes[input_offset:input_offset + 65536],
                        )
                    except BlockingIOError:
                        continue
                    except BrokenPipeError:
                        close_stream(file_object)
                    else:
                        input_offset += written
                        if input_offset >= len(input_bytes):
                            close_stream(file_object)
                    continue
                try:
                    chunk = os.read(file_object.fileno(), 65536)
                except BlockingIOError:
                    continue
                except OSError:
                    chunk = b""
                if not chunk:
                    close_stream(file_object)
                elif key.data == "stdout":
                    stdout_chunks.append(chunk)
                    if stream_progress is not None:
                        stream_progress.feed(chunk)
                else:
                    stderr_chunks.append(chunk)
    finally:
        for key in list(selector.get_map().values()):
            close_stream(key.fileobj)
        selector.close()
    if process.poll() is None:
        if progress is not None:
            progress.process_cleanup("incomplete", attempt)
        quiet = _stop_group(process)
        try:
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            quiet = False
    if stream_progress is not None:
        stream_progress.finish()
    return {
        "returncode": process.returncode,
        "stdout": _text(b"".join(stdout_chunks)),
        "stderr": _text(b"".join(stderr_chunks)),
        "timed_out": timed_out, "launch_error": False,
        "lingering": lingering, "quiet": quiet,
        "root_forced_cleanup": root_forced_cleanup,
    }


def _authenticated(value: Any) -> bool:
    if type(value) is not dict:
        return False
    for key in ("isAuthenticated", "authenticated", "loggedIn"):
        if type(value.get(key)) is bool:
            return value[key]
    return str(value.get("status", "")).casefold() in {
        "authenticated", "logged in", "logged_in",
    }


AUTO_MODEL = re.compile(r"^(?:[>*✓•-]\s*)?auto(?:\s+-\s+auto(?:\s+\(|$)|$)", re.I)


def preflight(
    executable: str | None = None, *, timeout_seconds: float = 15.0,
    env: Mapping[str, str] | None = None,
    dispatch_policy_path: Path | None = None,
) -> dict[str, Any]:
    policy = _dispatch_policy.load_policy(dispatch_policy_path)
    resolved = _resolve_executable(executable)
    result = {
        "schema_version": 1, "command": "preflight", "status": "missing_executable",
        "executable": resolved, "executable_available": resolved is not None,
        "authenticated": None, "auto_model_available": None, "ready": False,
        "runtime_ready": False, "dispatch_policy": policy,
    }
    if resolved is None:
        return result
    status = _run_process(
        [resolved, "status", "--format", "json"], cwd=None, input_text=None,
        timeout_seconds=timeout_seconds, env=env,
    )
    if status["launch_error"] or status["timed_out"] or status["returncode"] != 0:
        result["status"] = "status_failed"
        return result
    try:
        result["authenticated"] = _authenticated(json.loads(status["stdout"]))
    except json.JSONDecodeError:
        result["status"] = "status_invalid_json"
        return result
    if not result["authenticated"]:
        result["status"] = "not_authenticated"
        return result
    models = _run_process(
        [resolved, "models"], cwd=None, input_text=None,
        timeout_seconds=timeout_seconds, env=env,
    )
    if models["launch_error"] or models["timed_out"] or models["returncode"] != 0:
        result["status"] = "models_failed"
        return result
    result["auto_model_available"] = any(
        AUTO_MODEL.search(line.strip()) for line in models["stdout"].splitlines()
    )
    result["status"] = "ready" if result["auto_model_available"] else "auto_unavailable"
    result["runtime_ready"] = result["status"] == "ready"
    result["ready"] = result["runtime_ready"]
    if result["runtime_ready"] and not policy["policy"]["cursor_adapter_enabled"]:
        result["status"] = (
            "dispatch_policy_invalid"
            if policy["status"] == "invalid"
            else "globally_disabled"
        )
        result["ready"] = False
    return result


def _git(repo: Path, arguments: Sequence[str], *, allow_one: bool = False) -> bytes | None:
    completed = subprocess.run(
        ["git", *arguments], cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode == 0:
        return completed.stdout
    if allow_one and completed.returncode == 1:
        return None
    raise ValueError(_text(completed.stderr).strip()[:STDERR_LIMIT])


def _porcelain_paths(output: bytes) -> list[str]:
    entries = output.split(b"\0")
    paths: set[str] = set()
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if not entry:
            continue
        if len(entry) < 4:
            raise ValueError("malformed git status entry")
        paths.add(entry[3:].decode("utf-8", errors="surrogateescape"))
        if b"R" in entry[:2] or b"C" in entry[:2]:
            if index >= len(entries) or not entries[index]:
                raise ValueError("malformed git rename entry")
            paths.add(entries[index].decode("utf-8", errors="surrogateescape"))
            index += 1
    return sorted(paths)


def _fingerprint(repo: Path, relative: str) -> str:
    path = repo / relative
    try:
        info = path.lstat()
    except FileNotFoundError:
        return "deleted"
    if stat.S_ISLNK(info.st_mode):
        target = os.readlink(path).encode("utf-8", errors="surrogateescape")
        return "symlink:" + hashlib.sha256(target).hexdigest()
    if not stat.S_ISREG(info.st_mode):
        return f"special:{info.st_mode:o}"
    executable = stat.S_IMODE(info.st_mode) & 0o111
    return f"file:{executable:o}:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _workspace(repo: Path) -> dict[str, Any]:
    root = Path(_text(_git(repo, ["rev-parse", "--show-toplevel"])).strip()).resolve()
    if root != repo:
        raise ValueError("repo must be the exact Git worktree root")
    dirty = _porcelain_paths(
        _git(repo, ["status", "--porcelain=v1", "-z", "--untracked-files=all"])
    )
    staged = sorted(
        item.decode("utf-8", errors="surrogateescape")
        for item in _git(repo, ["diff", "--cached", "--name-only", "-z"]).split(b"\0")
        if item
    )
    head = _text(_git(repo, ["rev-parse", "--verify", "HEAD"])).strip()
    branch_raw = _git(repo, ["symbolic-ref", "-q", "HEAD"], allow_one=True)
    branch = _text(branch_raw).strip() if branch_raw is not None else "detached"
    staged_diff = _git(repo, ["diff", "--cached", "--binary"])
    return {
        "head": head, "branch": branch, "staged": staged,
        "staged_digest": hashlib.sha256(staged_diff).hexdigest(),
        "dirty": {path: _fingerprint(repo, path) for path in dirty},
    }


def _normalise_allowed(repo: Path, paths: Sequence[str]) -> tuple[str, ...]:
    if not paths:
        raise ValueError("at least one allowed write path is required")
    result: list[str] = []
    for raw in paths:
        candidate = PurePosixPath(raw)
        if (
            not raw or "\\" in raw or candidate.is_absolute() or raw.endswith("/")
            or candidate.as_posix() != raw
            or any(part in {"", ".", ".."} for part in candidate.parts)
            or candidate.parts[0] in {".git", ".agent-task-runtime"}
        ):
            raise ValueError(f"invalid allowed write path: {raw!r}")
        current = repo
        for part in candidate.parts:
            current /= part
            if current.is_symlink():
                raise ValueError(f"allowed write path crosses a symlink: {raw!r}")
            if not current.exists():
                break
        result.append(candidate.as_posix())
    return tuple(sorted(set(result)))


def _changed(before: dict[str, str], after: dict[str, str]) -> list[str]:
    return sorted(
        path for path in set(before) | set(after) if before.get(path) != after.get(path)
    )


def _allowed(path: str, roots: Sequence[str]) -> bool:
    return any(path == root or path.startswith(root + "/") for root in roots)


def _parse_stream(output: str) -> dict[str, Any]:
    events: list[dict[str, Any]] = []
    malformed = False
    for line in output.splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            malformed = True
            continue
        if type(value) is dict:
            events.append(value)
        else:
            malformed = True
    terminal = [event for event in events if event.get("type") == "result"]
    success = bool(
        not malformed and len(terminal) == 1 and events and terminal[0] is events[-1]
        and terminal[0].get("subtype") == "success" and terminal[0].get("is_error") is False
    )
    init_models = [
        event.get("model") for event in events
        if event.get("type") == "system" and event.get("subtype") == "init"
        and type(event.get("model")) is str and event.get("model").strip()
    ]
    model_name = init_models[0] if len(init_models) == 1 else None
    terminal_result = (
        terminal[0].get("result")
        if len(terminal) == 1 and type(terminal[0].get("result")) is str
        else ""
    )
    review_matches = list(_UPPER_REVIEW_MARKER.finditer(terminal_result))
    review_status = None
    if (
        len(review_matches) == 1
        and not terminal_result[review_matches[0].end():].strip()
    ):
        review_status = review_matches[0].group(1).casefold()
    review_report = terminal_result[-STDERR_LIMIT:]
    error_text = "\n".join(
        json.dumps(event, ensure_ascii=False, sort_keys=True)
        for event in events
        if event.get("type") == "error" or event.get("is_error") is True
    )
    return {
        "success": success, "model": model_name,
        "model_receipt_valid": len(init_models) == 1, "error_text": error_text,
        "review_status": review_status, "review_report": review_report,
    }


def _load_router_tool(filename: str, module_name: str) -> Any:
    tool_path = (
        Path(__file__).resolve().parents[2]
        / f"agent-model-router/tools/{filename}"
    )
    specification = importlib.util.spec_from_file_location(
        module_name, tool_path,
    )
    if specification is None or specification.loader is None:
        raise RuntimeError("agent-model-router model identity tool is unavailable")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


_model_identity = _load_router_tool(
    "model_identity.py", "_engineering_agent_rules_model_identity",
)
_tier_policy = _load_router_tool(
    "tier_policy.py", "_engineering_agent_rules_tier_policy",
)
_reasoning_policy = _load_router_tool(
    "reasoning_policy.py", "_engineering_agent_rules_reasoning_policy",
)
_dispatch_policy = _load_router_tool(
    "dispatch_policy.py", "_engineering_agent_rules_dispatch_policy",
)
_receipt_key_matches = _model_identity.receipt_key_matches
_tier_names = _tier_policy.TIER_NAMES
_tier_at_least = _tier_policy.tier_at_least
_reasoning_depth_names = _reasoning_policy.REASONING_DEPTH_NAMES
_reasoning_depth_at_least = _reasoning_policy.reasoning_depth_at_least


FALLBACKS = {
    "rate_limit": ("rate limit", "rate_limit", "too many requests", "status 429"),
    "quota": ("quota", "usage limit", "credit limit", "credits exhausted"),
    "model_unavailable": ("model unavailable", "unknown model", "not available for this account"),
    "authentication": ("not authenticated", "authentication required", "unauthorized", "status 401"),
    "transient_network": (
        "tls", "ssl", "handshake", "connection reset", "connection closed",
        "connection failed", "connecterror", "err_http2", "http2_error",
        "network error", "writableiterable is closed",
    ),
}


def _fallback_reason(text: str) -> str | None:
    folded = text.casefold()
    return next(
        (reason for reason, phrases in FALLBACKS.items() if any(item in folded for item in phrases)),
        None,
    )


def _session_directive(quota_pool_id: str, reason: str | None) -> str | None:
    if reason == "authentication":
        return "disable_cursor_for_session"
    if reason not in {"quota", "rate_limit"}:
        return None
    return f"disable_{quota_pool_id}_for_session"


def _report_runtime_limit(
    quota_pool_id: str, cache_path: Path | None = None,
) -> bool:
    """Best-effort cache invalidation after an authoritative runtime limit."""
    tool_path = (
        Path(__file__).resolve().parents[2]
        / "agent-model-router/tools/quota_cache.py"
    )
    tool_directory = str(tool_path.parent)
    inserted = tool_directory not in sys.path
    try:
        if inserted:
            sys.path.insert(0, tool_directory)
        specification = importlib.util.spec_from_file_location(
            "_engineering_agent_rules_quota_cache", tool_path,
        )
        if specification is None or specification.loader is None:
            return False
        module = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(module)
        result = module.invalidate_pool(
            quota_pool_id, cursor_cache_path=cache_path,
        )
    except Exception:
        return False
    finally:
        if inserted:
            try:
                sys.path.remove(tool_directory)
            except ValueError:
                pass
    return type(result) is dict and result.get("state") == "invalidated"


def _receipt(**values: Any) -> dict[str, Any]:
    base = {
        "schema_version": SCHEMA_VERSION, "command": "run", "status": "failed",
        "candidate_ready": False, "review_complete": False,
        "upper_review_required": False, "returncode": None, "timed_out": False,
        "model_receipt": None, "allowed_write_paths": [], "baseline_dirty_paths": [],
        "changed_paths": [], "out_of_scope_paths": [], "unsafe_symlink_paths": [],
        "git_state_changed": False, "lingering_processes_detected": False,
        "process_group_quiet": True, "writes_may_have_occurred": False,
        "root_forced_cleanup": False,
        "fallback_eligible": False, "fallback_reason": None, "stderr_summary": "",
        "requested_model": None, "model_source": None, "session_directive": None,
        "quota_pool_id": None, "quota_cache_invalidated": False,
        "attempt_count": 0, "retry_reason": None,
    }
    base.update(values)
    return base


def run_cursor(
    *, executable: str | None, repo: Path | str, allowed_write_paths: Sequence[str],
    required_tier: str, assessed_tier: str,
    required_reasoning_depth: str, assessed_reasoning_depth: str,
    receipt_key: str, prompt: str,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    model: str = "auto", model_source: str = "auto",
    quota_pool_id: str = "cursor_first_party",
    env: Mapping[str, str] | None = None,
    usage_cache_path: Path | None = None,
    operation: str = "write",
    dispatch_policy_path: Path | None = None,
) -> dict[str, Any]:
    if operation not in {"write", "review"}:
        raise ValueError("operation must be write or review")
    policy = _dispatch_policy.load_policy(dispatch_policy_path)
    if not policy["policy"]["cursor_adapter_enabled"]:
        return _receipt(
            command=operation,
            status=(
                "dispatch_policy_invalid"
                if policy["status"] == "invalid"
                else "adapter_disabled"
            ),
            fallback_eligible=True,
            fallback_reason="cursor_adapter_disabled",
            dispatch_policy=policy,
        )
    if required_tier not in _tier_names:
        raise ValueError("required_tier must be weak, medium, or strong")
    if assessed_tier not in _tier_names:
        raise ValueError("assessed_tier must be weak, medium, or strong")
    if required_reasoning_depth not in _reasoning_depth_names:
        raise ValueError("required_reasoning_depth is invalid")
    if assessed_reasoning_depth not in _reasoning_depth_names:
        raise ValueError("assessed_reasoning_depth is invalid")
    if type(receipt_key) is not str or not receipt_key:
        raise ValueError("receipt_key must be the non-empty opaque key from the router")
    if not prompt.strip():
        raise ValueError("prompt must not be empty")
    if type(model) is not str or not model.strip() or model != model.strip() or "\x00" in model:
        raise ValueError("model must be a trimmed non-empty string")
    if model.casefold() == "auto":
        model = "auto"
    if model_source not in {"auto", "third_party", "cursor_native"}:
        raise ValueError("model_source must be auto, third_party, or cursor_native")
    if (model == "auto") != (model_source == "auto"):
        raise ValueError("auto model requires auto source; explicit model requires explicit source")
    if quota_pool_id not in {"cursor_first_party", "cursor_api"}:
        raise ValueError("quota_pool_id must come from a Cursor router candidate")
    source = model_source
    progress = _ProgressReporter()
    progress.started(1)

    def finish(receipt: dict[str, Any]) -> dict[str, Any]:
        receipt["quota_pool_id"] = quota_pool_id
        receipt["dispatch_policy"] = policy
        progress.completed(
            str(receipt["status"]), int(receipt.get("attempt_count", 0)),
        )
        return receipt

    repo_path = Path(repo).expanduser().resolve()
    try:
        allowed = (
            _normalise_allowed(repo_path, allowed_write_paths)
            if operation == "write"
            else ()
        )
        before = _workspace(repo_path)
    except (OSError, ValueError) as error:
        return finish(_receipt(
            status="worktree_error", requested_model=model, model_source=source,
            stderr_summary=str(error)[:STDERR_LIMIT],
        ))
    if before["staged"]:
        progress.workspace_audit(changed=0, outside=0, git_changed=True)
        return finish(_receipt(
            status="worktree_error", allowed_write_paths=list(allowed),
            baseline_dirty_paths=sorted(before["dirty"]), git_state_changed=True,
            requested_model=model, model_source=source,
            stderr_summary="staged input is not allowed",
        ))
    resolved = _resolve_executable(executable)
    if resolved is None:
        return finish(_receipt(
            status="fallback_before_write", allowed_write_paths=list(allowed),
            baseline_dirty_paths=sorted(before["dirty"]), fallback_eligible=True,
            fallback_reason="missing_executable", requested_model=model,
            model_source=source,
        ))
    if operation == "write":
        command = [
            resolved, "--print", "--force", "--trust", "--sandbox", "enabled",
        ]
    else:
        command = [
            resolved, "--print", "--trust", "--sandbox", "enabled",
            "--mode", "plan",
        ]
    command.extend(("--output-format", "stream-json", "--model", model))
    cursor_env = dict(os.environ if env is None else env)
    cursor_env["GIT_OPTIONAL_LOCKS"] = "0"
    cursor_prompt = (
        _review_prompt(prompt, required_tier, required_reasoning_depth)
        if operation == "review" else prompt
    )
    process = _run_process(
        command, cwd=repo_path, input_text=cursor_prompt,
        timeout_seconds=timeout_seconds, env=cursor_env,
        progress=progress, attempt=1,
    )
    stream = _parse_stream(process["stdout"])
    reason = "launch_error" if process["launch_error"] else _fallback_reason(
        process["stderr"] + "\n" + stream["error_text"]
    )
    attempt_count = 1
    retry_reason = None
    first_succeeded = (
        process["returncode"] == 0 and not process["timed_out"]
        and not process["root_forced_cleanup"]
        and stream["success"] and stream["model_receipt_valid"] and process["quiet"]
    )
    if (
        not first_succeeded and not process["timed_out"]
        and not process["root_forced_cleanup"]
        and not process["lingering"]
        and reason == "transient_network" and process["quiet"]
    ):
        try:
            interim = _workspace(repo_path)
        except (OSError, ValueError) as error:
            progress.workspace_audit(changed=0, outside=0, git_changed=True)
            return finish(_receipt(
                status="failed_after_possible_write", returncode=process["returncode"],
                timed_out=process["timed_out"], allowed_write_paths=list(allowed),
                baseline_dirty_paths=sorted(before["dirty"]), writes_may_have_occurred=True,
                git_state_changed=True, lingering_processes_detected=process["lingering"],
                process_group_quiet=process["quiet"], fallback_reason=reason,
                root_forced_cleanup=process["root_forced_cleanup"],
                requested_model=model, model_source=source, attempt_count=attempt_count,
                stderr_summary=str(error)[:STDERR_LIMIT],
            ))
        interim_changed = _changed(before["dirty"], interim["dirty"])
        interim_git_changed = any(
            before[field] != interim[field] for field in ("head", "branch", "staged_digest")
        ) or bool(interim["staged"])
        if not interim_changed and not interim_git_changed:
            retry_reason = reason
            attempt_count = 2
            progress.tls_retry(attempt_count)
            process = _run_process(
                command, cwd=repo_path, input_text=cursor_prompt,
                timeout_seconds=timeout_seconds, env=cursor_env,
                progress=progress, attempt=attempt_count,
            )
            stream = _parse_stream(process["stdout"])
            reason = "launch_error" if process["launch_error"] else _fallback_reason(
                process["stderr"] + "\n" + stream["error_text"]
            )
    session_directive = _session_directive(quota_pool_id, reason)
    quota_cache_invalidated = (
        _report_runtime_limit(quota_pool_id, usage_cache_path)
        if reason in {"quota", "rate_limit"}
        else False
    )
    try:
        after = _workspace(repo_path)
    except (OSError, ValueError) as error:
        progress.workspace_audit(changed=0, outside=0, git_changed=True)
        return finish(_receipt(
            status="failed_after_possible_write", returncode=process["returncode"],
            timed_out=process["timed_out"], allowed_write_paths=list(allowed),
            baseline_dirty_paths=sorted(before["dirty"]), writes_may_have_occurred=True,
            git_state_changed=True, lingering_processes_detected=process["lingering"],
            process_group_quiet=process["quiet"], fallback_reason=reason,
            root_forced_cleanup=process["root_forced_cleanup"],
            requested_model=model, model_source=source,
            session_directive=session_directive,
            quota_cache_invalidated=quota_cache_invalidated,
            attempt_count=attempt_count, retry_reason=retry_reason,
            stderr_summary=str(error)[:STDERR_LIMIT],
        ))
    changed = _changed(before["dirty"], after["dirty"])
    outside = (
        [path for path in changed if not _allowed(path, allowed)]
        if operation == "write"
        else []
    )
    symlinks = [
        path for path in changed if after["dirty"].get(path, "").startswith("symlink:")
    ]
    git_changed = any(
        before[field] != after[field] for field in ("head", "branch", "staged_digest")
    ) or bool(after["staged"])
    progress.workspace_audit(
        changed=len(changed), outside=len(outside), git_changed=git_changed,
    )
    identity_matches = (
        stream["model_receipt_valid"]
        and _receipt_key_matches(receipt_key, stream["model"])
    )
    tier_receipt = {
        "reported_model": stream["model"], "assessed_tier": assessed_tier,
        "requested_model": model, "model_source": source,
        "receipt_key": receipt_key,
        "required_tier": required_tier,
        "assessed_reasoning_depth": assessed_reasoning_depth,
        "required_reasoning_depth": required_reasoning_depth,
        "identity_matches": identity_matches,
        "valid": stream["model_receipt_valid"] and identity_matches,
        "sufficient": (
            stream["model_receipt_valid"] and identity_matches
            and _tier_at_least(assessed_tier, required_tier)
            and _reasoning_depth_at_least(
                assessed_reasoning_depth, required_reasoning_depth
            )
        ),
    }
    writes = bool(
        changed or git_changed or process["timed_out"]
        or process["root_forced_cleanup"] or process["lingering"]
        or not process["quiet"]
    )
    process_success = (
        process["returncode"] == 0 and not process["timed_out"]
        and not process["root_forced_cleanup"]
        and stream["success"] and stream["model_receipt_valid"]
        and process["quiet"]
    )
    if not process["quiet"]:
        status = "process_group_not_quiet"
    elif git_changed:
        status = "git_state_changed"
    elif symlinks:
        status = "unsafe_symlink_changes"
    elif outside:
        status = "out_of_scope_changes"
    elif process_success and not identity_matches:
        status = "model_receipt_mismatch"
    elif process_success and not tier_receipt["sufficient"]:
        status = "insufficient_model_capability"
    elif operation == "review" and changed:
        status = "review_mutated_worktree"
    elif operation == "review" and process_success and stream["review_status"] == "pass":
        status = "review_passed"
    elif operation == "review" and process_success and stream["review_status"] == "blocked":
        status = "review_blocked"
    elif operation == "review" and process_success:
        status = "review_unavailable"
    elif operation == "write" and process_success and changed:
        status = "candidate_ready"
    elif process_success:
        status = "no_changes"
    elif writes:
        status = "failed_after_possible_write"
    elif reason is not None:
        status = "fallback_before_write"
    else:
        status = "protocol_error" if process["returncode"] == 0 else "failed"
    return finish(_receipt(
        command=operation, status=status, candidate_ready=status == "candidate_ready",
        review_complete=status == "review_passed",
        upper_review_required=operation == "write" and status == "candidate_ready",
        returncode=process["returncode"], timed_out=process["timed_out"],
        model_receipt=tier_receipt, allowed_write_paths=list(allowed),
        baseline_dirty_paths=sorted(before["dirty"]), changed_paths=changed,
        out_of_scope_paths=outside, unsafe_symlink_paths=symlinks,
        git_state_changed=git_changed, lingering_processes_detected=process["lingering"],
        process_group_quiet=process["quiet"], writes_may_have_occurred=writes,
        root_forced_cleanup=process["root_forced_cleanup"],
        fallback_eligible=status == "fallback_before_write", fallback_reason=reason,
        requested_model=model, model_source=source,
        session_directive=session_directive,
        quota_cache_invalidated=quota_cache_invalidated,
        attempt_count=attempt_count, retry_reason=retry_reason,
        stderr_summary=(
            stream["review_report"] or "upper review did not report pass"
            if status in {"review_blocked", "review_unavailable"}
            else process["stderr"].strip()[:STDERR_LIMIT]
        ),
    ))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Preflight or run Cursor Agent synchronously.")
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("preflight")
    check.add_argument("--cursor-command")
    check.add_argument("--timeout-seconds", type=float, default=15.0)
    check.add_argument("--dispatch-policy-path", type=Path)
    run = commands.add_parser("run")
    run.add_argument("--repo", required=True, type=Path)
    run.add_argument("--cursor-command")
    run.add_argument("--allowed-write-path", action="append", required=True, dest="paths")
    run.add_argument("--required-tier", required=True, choices=_tier_names)
    run.add_argument("--assessed-tier", required=True, choices=_tier_names)
    run.add_argument(
        "--required-reasoning-depth", required=True,
        choices=_reasoning_depth_names,
    )
    run.add_argument(
        "--assessed-reasoning-depth", required=True,
        choices=_reasoning_depth_names,
    )
    run.add_argument("--receipt-key", required=True)
    run.add_argument("--quota-pool", required=True, choices=("cursor_first_party", "cursor_api"))
    run.add_argument("--model", default="auto")
    run.add_argument(
        "--model-source", default="auto",
        choices=("auto", "third_party", "cursor_native"),
    )
    run.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    run.add_argument("--dispatch-policy-path", type=Path)
    review = commands.add_parser("review")
    review.add_argument("--repo", required=True, type=Path)
    review.add_argument("--cursor-command")
    review.add_argument("--required-tier", required=True, choices=_tier_names)
    review.add_argument("--assessed-tier", required=True, choices=_tier_names)
    review.add_argument(
        "--required-reasoning-depth", required=True,
        choices=_reasoning_depth_names,
    )
    review.add_argument(
        "--assessed-reasoning-depth", required=True,
        choices=_reasoning_depth_names,
    )
    review.add_argument("--receipt-key", required=True)
    review.add_argument("--quota-pool", required=True, choices=("cursor_first_party", "cursor_api"))
    review.add_argument("--model", default="auto")
    review.add_argument(
        "--model-source", default="auto",
        choices=("auto", "third_party", "cursor_native"),
    )
    review.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    review.add_argument("--dispatch-policy-path", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.command == "preflight":
        result = preflight(
            arguments.cursor_command,
            timeout_seconds=arguments.timeout_seconds,
            dispatch_policy_path=arguments.dispatch_policy_path,
        )
        code = 0 if result["ready"] else 2
    else:
        try:
            result = run_cursor(
                executable=arguments.cursor_command, repo=arguments.repo,
                allowed_write_paths=(arguments.paths if arguments.command == "run" else []),
                required_tier=arguments.required_tier,
                assessed_tier=arguments.assessed_tier,
                required_reasoning_depth=arguments.required_reasoning_depth,
                assessed_reasoning_depth=arguments.assessed_reasoning_depth,
                receipt_key=arguments.receipt_key,
                quota_pool_id=arguments.quota_pool,
                prompt=sys.stdin.read(), timeout_seconds=arguments.timeout_seconds,
                model=arguments.model, model_source=arguments.model_source,
                operation="write" if arguments.command == "run" else "review",
                dispatch_policy_path=arguments.dispatch_policy_path,
            )
        except ValueError as error:
            result = {"schema_version": 1, "command": "run", "status": "invalid_request", "error": str(error)}
            code = 64
        else:
            success = (
                result["candidate_ready"]
                if arguments.command == "run"
                else result["review_complete"]
            )
            code = 0 if success else 2 if result["fallback_eligible"] else 4
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
