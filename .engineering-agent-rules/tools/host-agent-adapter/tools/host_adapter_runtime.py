#!/usr/bin/env python3
"""Thin host-adapter runtime: spawn, timeout kill, receipts, anti-recursion."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import subprocess
from collections.abc import Mapping, Sequence
from typing import Any, Callable, Protocol, TextIO
import time


ADAPTER_DEPTH_ENV = "ENGINEERING_AGENT_RULES_ADAPTER_DEPTH"
STDERR_LIMIT = 1200
DEFAULT_TIMEOUT_SECONDS = 3600.0
CODEX_SANDBOX_MODES = ("read-only", "workspace-write", "danger-full-access")
_SAFE_PROGRESS_TOKEN = re.compile(r"^[a-z0-9_]+$")
_SAFE_PROGRESS_PROVIDER = re.compile(r"^[a-z0-9]+$")


class ProcessProgress(Protocol):
    """Optional, best-effort observer for long-running child processes."""

    def started(self, attempt: int) -> None: ...

    def feed_stdout(self, chunk: bytes, attempt: int) -> bool: ...

    def heartbeat_if_due(self, attempt: int) -> None: ...

    def process_cleanup(self, reason: str, attempt: int) -> None: ...

    def finish(self, attempt: int) -> None: ...


class JsonlProgressReporter:
    """Emit safe lifecycle progress while treating valid JSONL as activity."""

    def __init__(
        self, *, provider: str, stream: TextIO | None,
        event_stage: Callable[[dict[str, Any]], str | None],
        heartbeat_interval_seconds: float,
        min_heartbeat_interval_seconds: float,
    ) -> None:
        if not _SAFE_PROGRESS_PROVIDER.fullmatch(provider):
            raise ValueError("unsafe progress provider")
        self._provider = provider
        self._stream = stream
        self._event_stage = event_stage
        self._heartbeat_interval_seconds = heartbeat_interval_seconds
        self._min_heartbeat_interval_seconds = min_heartbeat_interval_seconds
        self._started_at = time.monotonic()
        self._next_heartbeat = self._started_at + heartbeat_interval_seconds
        self._event_count = 0
        self._phase = "starting"
        self._emitted: set[str] = set()
        self._enabled = stream is not None
        self._pending = bytearray()

    def _write(self, stage: str, **fields: int | bool | str) -> None:
        if not self._enabled:
            return
        stream = self._stream
        if stream is None:
            self._enabled = False
            return
        if not _SAFE_PROGRESS_TOKEN.fullmatch(stage):  # pragma: no cover
            raise ValueError("unsafe progress stage")
        parts = [
            f"{self._provider}-adapter progress", f"stage={stage}",
            f"elapsed={time.monotonic() - self._started_at:.1f}s",
            f"events={self._event_count}",
        ]
        for key, value in fields.items():
            if not _SAFE_PROGRESS_TOKEN.fullmatch(key):  # pragma: no cover
                raise ValueError("unsafe progress field")
            if isinstance(value, bool):
                rendered = "true" if value else "false"
            elif isinstance(value, int):
                rendered = str(value)
            elif _SAFE_PROGRESS_TOKEN.fullmatch(value):
                rendered = value
            else:  # pragma: no cover
                raise ValueError("unsafe progress value")
            parts.append(f"{key}={rendered}")
        try:
            print(" ".join(parts), file=stream, flush=True)
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

    def feed_stdout(self, chunk: bytes, attempt: int) -> bool:
        self._pending.extend(chunk)
        activity = False
        while True:
            newline = self._pending.find(b"\n")
            if newline < 0:
                return activity
            line = bytes(self._pending[:newline])
            del self._pending[:newline + 1]
            activity = self._consume(line, attempt) or activity

    def _consume(self, line: bytes, attempt: int) -> bool:
        if not line.strip():
            return False
        try:
            event = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return False
        if type(event) is not dict:
            return False
        event_type = event.get("type")
        if type(event_type) is not str or not event_type:
            return False
        self._event_count += 1
        try:
            stage = self._event_stage(event)
            if stage is not None:
                if not _SAFE_PROGRESS_TOKEN.fullmatch(stage):
                    raise ValueError("unsafe progress stage")
                self._once(stage, attempt=attempt)
        except Exception:
            # Provider-specific stage hints are optional observer data. A malformed
            # event must not escape into process waiting, cleanup, or final receipts.
            self._event_stage = lambda _event: None
            self._phase = "events"
        return True

    def heartbeat_if_due(self, attempt: int) -> None:
        now = time.monotonic()
        if now < self._next_heartbeat:
            return
        self._write("heartbeat", attempt=attempt, phase=self._phase)
        self._next_heartbeat = now + max(
            self._heartbeat_interval_seconds,
            self._min_heartbeat_interval_seconds,
        )

    def process_cleanup(self, reason: str, attempt: int) -> None:
        self._once("process_cleanup", attempt=attempt, reason=reason)

    def finish(self, attempt: int) -> None:
        if self._pending:
            self._consume(bytes(self._pending), attempt)
            self._pending.clear()

    def completed(self, status: str, attempts: int) -> None:
        self._once("completed", status=status, attempts=attempts)


def load_sibling_module(
    start: Path, pack: str, filename: str, module_name: str,
) -> Any:
    tool_path = start.resolve().parents[2] / pack / "tools" / filename
    specification = importlib.util.spec_from_file_location(module_name, tool_path)
    if specification is None or specification.loader is None:
        raise RuntimeError(f"{pack} tool {filename} is unavailable")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value


def nested_adapter(*, environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    value = env.get(ADAPTER_DEPTH_ENV)
    return type(value) is str and value.strip() not in {"", "0", "false", "False"}


def adapter_environ(base: Mapping[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if base is None else base)
    env.pop("CURSOR_AGENT", None)
    env[ADAPTER_DEPTH_ENV] = "1"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    return env


def resolve_executable(
    command: str | None, names: Sequence[str],
) -> str | None:
    candidates = [command] if command else list(names)
    for candidate in candidates:
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


def parse_jsonl(output: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in output.splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if type(value) is dict:
            events.append(value)
    return events


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


def _stop_group(process: subprocess.Popen[str]) -> None:
    if os.name != "posix":  # pragma: no cover
        process.kill()
        process.wait()
        return
    for action in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, action)
        except ProcessLookupError:
            return
        except PermissionError:
            try:
                process.kill()
                process.wait(timeout=0.5)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                return
            return
        deadline = time.monotonic() + 0.5
        while _process_group_alive(process.pid) and time.monotonic() < deadline:
            time.sleep(0.01)
        if not _process_group_alive(process.pid):
            return


def run_process(
    command: list[str], *, cwd: Path | None, input_text: str | None,
    timeout_seconds: float, env: Mapping[str, str] | None,
    progress: ProcessProgress | None = None, attempt: int = 1,
) -> dict[str, Any]:
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    if attempt <= 0:
        raise ValueError("attempt must be positive")
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
            "timed_out": False, "launch_error": True, "lingering": False,
        }
    selector = selectors.DefaultSelector()
    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
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
    deadline = time.monotonic() + timeout_seconds
    try:
        while selector.get_map() or process.poll() is None:
            now = time.monotonic()
            events = selector.select(min(0.1, max(0.0, deadline - now)))
            if not events and time.monotonic() >= deadline:
                timed_out = True
                if progress is not None:
                    progress.process_cleanup("inactivity_timeout", attempt)
                _stop_group(process)
                try:
                    process.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    lingering = True
                break
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
                    if progress is not None and progress.feed_stdout(chunk, attempt):
                        deadline = time.monotonic() + timeout_seconds
                else:
                    stderr_chunks.append(chunk)
            if progress is not None:
                progress.heartbeat_if_due(attempt)
    finally:
        for key in list(selector.get_map().values()):
            close_stream(key.fileobj)
        selector.close()
    if process.poll() is None:
        if progress is not None:
            progress.process_cleanup("incomplete", attempt)
        _stop_group(process)
        try:
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            lingering = True
    if progress is not None:
        progress.finish(attempt)
    return {
        "returncode": process.returncode,
        "stdout": text(b"".join(stdout_chunks)),
        "stderr": text(b"".join(stderr_chunks)),
        "timed_out": timed_out, "launch_error": False, "lingering": lingering,
    }


def git_status_facts(repo: Path) -> dict[str, Any] | None:
    """Best-effort worktree facts. Never a fail-closed gate."""

    try:
        root = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        if root.returncode != 0:
            return None
        head = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        branch = subprocess.run(
            ["git", "symbolic-ref", "-q", "HEAD"],
            cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        status = subprocess.run(
            ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
            cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        if status.returncode != 0:
            return None
        dirty: list[str] = []
        entries = status.stdout.split(b"\0")
        index = 0
        while index < len(entries):
            entry = entries[index]
            index += 1
            if not entry or len(entry) < 4:
                continue
            dirty.append(entry[3:].decode("utf-8", errors="surrogateescape"))
            if b"R" in entry[:2] or b"C" in entry[:2]:
                if index < len(entries) and entries[index]:
                    dirty.append(
                        entries[index].decode("utf-8", errors="surrogateescape")
                    )
                    index += 1
        return {
            "head": text(head.stdout).strip() or None,
            "branch": text(branch.stdout).strip() or "detached",
            "dirty_paths": sorted(set(dirty)),
        }
    except OSError:
        return None
