#!/usr/bin/env python3
"""Synchronous Codex CLI adapter over the host-agent-adapter runtime."""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import re
import sys
import time
from collections.abc import Mapping, Sequence
from typing import Any


SCHEMA_VERSION = 1
DEFAULT_TIMEOUT_SECONDS = 3600.0
EXECUTABLE_NAMES = ("codex",)
POLICY_KEY = "codex_adapter_enabled"
HEARTBEAT_INTERVAL_SECONDS = 25.0
MIN_HEARTBEAT_INTERVAL_SECONDS = 0.25
USAGE_LIMIT_PHRASES = ("you've hit your usage limit", "usage limit")
MODEL_UNSUPPORTED_PHRASES = (
    "not supported when using codex with a chatgpt account",
)
_UPPER_REVIEW_MARKER = re.compile(
    r"(?m)^AGENT_MODEL_REVIEW:\s*(PASS|BLOCKED|UNAVAILABLE)\s*$",
)
_SAFE_PROGRESS_TOKEN = re.compile(r"^[a-z0-9_]+$")
QUOTA_POOLS = {"codex_main", "codex_spark"}


_RUNTIME = None
_DISPATCH = None
_TIER = None
_REASONING = None


class _ProgressReporter:
    """Emit fixed-shape progress without forwarding Codex-owned content."""

    def __init__(self) -> None:
        self._stream = sys.stderr
        self._started_at = time.monotonic()
        self._next_heartbeat = self._started_at + HEARTBEAT_INTERVAL_SECONDS
        self._event_count = 0
        self._phase = "starting"
        self._emitted: set[str] = set()
        self._enabled = self._stream is not None
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
            "codex-adapter progress", f"stage={stage}",
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
        if event_type == "thread.started":
            self._once("thread_started", attempt=attempt)
        return True

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

    def finish(self, attempt: int) -> None:
        if self._pending:
            self._consume(bytes(self._pending), attempt)
            self._pending.clear()

    def completed(self, status: str, attempts: int) -> None:
        self._once("completed", status=status, attempts=attempts)


def _load_tools() -> None:
    global _RUNTIME, _DISPATCH, _TIER, _REASONING
    if _RUNTIME is not None:
        return
    start = Path(__file__)
    runtime_path = start.resolve().parent / "host_adapter_runtime.py"
    spec = importlib.util.spec_from_file_location(
        "_engineering_host_adapter_runtime", runtime_path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("host-agent-adapter runtime is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _RUNTIME = module
    _DISPATCH = module.load_sibling_module(
        start, "agent-model-router", "dispatch_policy.py",
        "_engineering_dispatch_policy",
    )
    _TIER = module.load_sibling_module(
        start, "agent-model-router", "tier_policy.py",
        "_engineering_tier_policy",
    )
    _REASONING = module.load_sibling_module(
        start, "agent-model-router", "reasoning_policy.py",
        "_engineering_reasoning_policy",
    )


def _review_prompt(prompt: str, required_tier: str, required_reasoning_depth: str) -> str:
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


def build_argv(
    *, executable: str, model: str, effort: str, sandbox: str,
) -> list[str]:
    _load_tools()
    if sandbox not in _RUNTIME.CODEX_SANDBOX_MODES:
        raise ValueError("sandbox must be a Codex CLI sandbox mode")
    return [
        executable, "exec", "-", "--json", "--ephemeral",
        "-s", sandbox, "-m", model,
        "-c", f"model_reasoning_effort={effort}",
    ]


def parse_codex_output(stdout: str, stderr: str) -> dict[str, Any]:
    _load_tools()
    events = _RUNTIME.parse_jsonl(stdout)
    last_message = None
    usage = None
    failed = False
    failed_text = ""
    for event in events:
        kind = event.get("type")
        if kind == "item.completed":
            item = event.get("item")
            if type(item) is dict and item.get("type") == "agent_message":
                text = item.get("text")
                content = item.get("content")
                if type(text) is str and text.strip():
                    last_message = text
                elif type(content) is str and content.strip():
                    last_message = content
        elif kind == "turn.completed":
            if type(event.get("usage")) is dict:
                usage = event["usage"]
        elif kind == "turn.failed":
            failed = True
            error = event.get("error")
            if type(error) is dict and type(error.get("message")) is str:
                failed_text = error["message"]
            elif type(error) is str:
                failed_text = error
    combined = f"{stderr}\n{failed_text}".casefold()
    usage_limit = any(phrase in combined for phrase in USAGE_LIMIT_PHRASES)
    model_unsupported = any(
        phrase in combined for phrase in MODEL_UNSUPPORTED_PHRASES
    )
    review_matches = list(_UPPER_REVIEW_MARKER.finditer(last_message or ""))
    review_status = None
    if (
        last_message is not None
        and len(review_matches) == 1
        and not last_message[review_matches[0].end():].strip()
    ):
        review_status = review_matches[0].group(1).casefold()
    completed = any(event.get("type") == "turn.completed" for event in events)
    return {
        "success": completed and not failed and not usage_limit and not model_unsupported,
        "last_agent_message": last_message,
        "usage": usage,
        "usage_limit": usage_limit,
        "model_unsupported": model_unsupported,
        "review_status": review_status,
        "error_text": failed_text,
    }


def _report_runtime_limit(quota_pool_id: str, cache_path: Path | None = None) -> bool:
    tool_path = (
        Path(__file__).resolve().parents[2]
        / "agent-model-router/tools/quota_cache.py"
    )
    tool_directory = str(tool_path.parent)
    inserted = tool_directory not in sys.path
    try:
        if inserted:
            sys.path.insert(0, tool_directory)
        spec = importlib.util.spec_from_file_location(
            "_engineering_quota_cache", tool_path,
        )
        if spec is None or spec.loader is None:
            return False
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        result = module.invalidate_pool(quota_pool_id, codex_cache_path=cache_path)
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
        "requested_model": None, "requested_effort": None,
        "last_agent_message": None, "usage": None, "git_status": None,
        "fallback_eligible": False, "fallback_reason": None, "stderr_summary": "",
        "quota_pool_id": None, "quota_cache_invalidated": False,
        "receipt_key": None, "assessed_tier": None, "assessed_reasoning_depth": None,
    }
    base.update(values)
    return base


def preflight(
    executable: str | None = None, *, timeout_seconds: float = 15.0,
    env: Mapping[str, str] | None = None,
    dispatch_policy_path: Path | None = None,
) -> dict[str, Any]:
    _load_tools()
    policy = _DISPATCH.load_policy(dispatch_policy_path)
    resolved = _RUNTIME.resolve_executable(executable, EXECUTABLE_NAMES)
    result = {
        "schema_version": 1, "command": "preflight", "status": "missing_executable",
        "executable": resolved, "executable_available": resolved is not None,
        "authenticated": None, "ready": False, "runtime_ready": False,
        "dispatch_policy": policy,
    }
    if resolved is None:
        return result
    version = _RUNTIME.run_process(
        [resolved, "--version"], cwd=None, input_text=None,
        timeout_seconds=timeout_seconds, env=env,
    )
    if version["launch_error"] or version["timed_out"] or version["returncode"] != 0:
        result["status"] = "status_failed"
        return result
    result["status"] = "ready"
    result["runtime_ready"] = True
    result["ready"] = True
    if not policy["policy"][POLICY_KEY]:
        result["status"] = (
            "dispatch_policy_invalid"
            if policy["status"] == "invalid"
            else "globally_disabled"
        )
        result["ready"] = False
    return result


def run_codex(
    *, executable: str | None, repo: Path | str,
    required_tier: str, assessed_tier: str,
    required_reasoning_depth: str, assessed_reasoning_depth: str,
    receipt_key: str, prompt: str,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    model: str, reasoning_effort: str,
    quota_pool_id: str = "codex_main",
    allowed_write_paths: Sequence[str] = (),
    env: Mapping[str, str] | None = None,
    usage_cache_path: Path | None = None,
    operation: str = "write",
    dispatch_policy_path: Path | None = None,
) -> dict[str, Any]:
    _load_tools()
    if operation not in {"write", "review"}:
        raise ValueError("operation must be write or review")
    policy = _DISPATCH.load_policy(dispatch_policy_path)
    if not policy["policy"][POLICY_KEY]:
        return _receipt(
            command=operation,
            status=(
                "dispatch_policy_invalid"
                if policy["status"] == "invalid"
                else "adapter_disabled"
            ),
            fallback_eligible=True,
            fallback_reason="codex_adapter_disabled",
            dispatch_policy=policy,
            requested_model=model, requested_effort=reasoning_effort,
        )
    if _RUNTIME.nested_adapter(environ=env):
        return _receipt(
            command=operation, status="nested_adapter_forbidden",
            fallback_eligible=True, fallback_reason="nested_adapter",
            dispatch_policy=policy,
            requested_model=model, requested_effort=reasoning_effort,
        )
    if required_tier not in _TIER.TIER_NAMES:
        raise ValueError("required_tier must be weak, medium, or strong")
    if assessed_tier not in _TIER.TIER_NAMES:
        raise ValueError("assessed_tier must be weak, medium, or strong")
    if required_reasoning_depth not in _REASONING.REASONING_DEPTH_NAMES:
        raise ValueError("required_reasoning_depth is invalid")
    if assessed_reasoning_depth not in _REASONING.REASONING_DEPTH_NAMES:
        raise ValueError("assessed_reasoning_depth is invalid")
    if type(receipt_key) is not str or not receipt_key:
        raise ValueError("receipt_key must be the non-empty opaque key from the router")
    if not prompt.strip():
        raise ValueError("prompt must not be empty")
    if type(model) is not str or not model.strip() or "\x00" in model:
        raise ValueError("model must be a trimmed non-empty string")
    if type(reasoning_effort) is not str or not reasoning_effort.strip():
        raise ValueError("reasoning_effort must be a non-empty string")
    if quota_pool_id not in QUOTA_POOLS:
        raise ValueError("quota_pool_id must come from a Codex router candidate")
    repo_path = Path(repo).expanduser().resolve()
    sandbox = "read-only" if operation == "review" else "workspace-write"
    brief = prompt
    if allowed_write_paths:
        roots = ", ".join(allowed_write_paths)
        brief = brief.rstrip() + f"\n\nWrite only within these repository-relative paths: {roots}\n"
    if operation == "review":
        brief = _review_prompt(brief, required_tier, required_reasoning_depth)
    resolved = _RUNTIME.resolve_executable(executable, EXECUTABLE_NAMES)
    if resolved is None:
        return _receipt(
            command=operation, status="fallback_before_write",
            fallback_eligible=True, fallback_reason="missing_executable",
            requested_model=model, requested_effort=reasoning_effort,
            dispatch_policy=policy, quota_pool_id=quota_pool_id,
        )
    command = build_argv(
        executable=resolved, model=model, effort=reasoning_effort, sandbox=sandbox,
    )
    progress = _ProgressReporter()
    process = _RUNTIME.run_process(
        command, cwd=repo_path, input_text=brief,
        timeout_seconds=timeout_seconds, env=_RUNTIME.adapter_environ(env),
        progress=progress, attempt=1,
    )
    parsed = parse_codex_output(process["stdout"], process["stderr"])
    quota_cache_invalidated = False
    if parsed["usage_limit"]:
        quota_cache_invalidated = _report_runtime_limit(quota_pool_id, usage_cache_path)
    if process["timed_out"]:
        status = "timed_out"
        reason = "timeout"
    elif parsed["usage_limit"]:
        status = "fallback_before_write"
        reason = "quota"
    elif parsed["model_unsupported"]:
        status = "fallback_before_write"
        reason = "model_unavailable"
    elif process["launch_error"]:
        status = "fallback_before_write"
        reason = "launch_error"
    elif parsed["success"] and operation == "review" and parsed["review_status"] == "pass":
        status = "review_passed"
        reason = None
    elif parsed["success"] and operation == "review" and parsed["review_status"] == "blocked":
        status = "review_blocked"
        reason = None
    elif parsed["success"] and operation == "review":
        status = "review_unavailable"
        reason = None
    elif parsed["success"] and operation == "write":
        status = "candidate_ready"
        reason = None
    elif process["returncode"] == 0:
        status = "protocol_error"
        reason = None
    else:
        status = "failed"
        reason = None
    receipt = _receipt(
        command=operation, status=status,
        candidate_ready=status == "candidate_ready",
        review_complete=status == "review_passed",
        upper_review_required=operation == "write" and status == "candidate_ready",
        returncode=process["returncode"], timed_out=process["timed_out"],
        requested_model=model, requested_effort=reasoning_effort,
        last_agent_message=parsed["last_agent_message"], usage=parsed["usage"],
        git_status=_RUNTIME.git_status_facts(repo_path),
        fallback_eligible=status == "fallback_before_write",
        fallback_reason=reason,
        quota_pool_id=quota_pool_id,
        quota_cache_invalidated=quota_cache_invalidated,
        dispatch_policy=policy,
        stderr_summary=(process["stderr"] or parsed["error_text"])[:_RUNTIME.STDERR_LIMIT],
        receipt_key=receipt_key,
        assessed_tier=assessed_tier,
        assessed_reasoning_depth=assessed_reasoning_depth,
    )
    progress.completed(status, 1)
    return receipt


def _parser() -> argparse.ArgumentParser:
    _load_tools()
    parser = argparse.ArgumentParser(description="Preflight or run Codex Agent synchronously.")
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("preflight")
    check.add_argument("--codex-command")
    check.add_argument("--timeout-seconds", type=float, default=15.0)
    check.add_argument("--dispatch-policy-path", type=Path)
    run = commands.add_parser("run")
    review = commands.add_parser("review")
    for command in (run, review):
        command.add_argument("--repo", required=True, type=Path)
        command.add_argument("--codex-command")
        command.add_argument("--required-tier", required=True, choices=_TIER.TIER_NAMES)
        command.add_argument("--assessed-tier", required=True, choices=_TIER.TIER_NAMES)
        command.add_argument(
            "--required-reasoning-depth", required=True,
            choices=_REASONING.REASONING_DEPTH_NAMES,
        )
        command.add_argument(
            "--assessed-reasoning-depth", required=True,
            choices=_REASONING.REASONING_DEPTH_NAMES,
        )
        command.add_argument("--receipt-key", required=True)
        command.add_argument("--quota-pool", required=True, choices=sorted(QUOTA_POOLS))
        command.add_argument("--model", required=True)
        command.add_argument("--reasoning-effort", required=True)
        command.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
        command.add_argument("--dispatch-policy-path", type=Path)
    run.add_argument("--allowed-write-path", action="append", default=[], dest="paths")
    return parser


def main(argv: list[str] | None = None) -> int:
    _load_tools()
    arguments = _parser().parse_args(argv)
    if arguments.command == "preflight":
        result = preflight(
            arguments.codex_command,
            timeout_seconds=arguments.timeout_seconds,
            dispatch_policy_path=arguments.dispatch_policy_path,
        )
        code = 0 if result["ready"] else 2
    else:
        try:
            result = run_codex(
                executable=arguments.codex_command, repo=arguments.repo,
                allowed_write_paths=getattr(arguments, "paths", []),
                required_tier=arguments.required_tier,
                assessed_tier=arguments.assessed_tier,
                required_reasoning_depth=arguments.required_reasoning_depth,
                assessed_reasoning_depth=arguments.assessed_reasoning_depth,
                receipt_key=arguments.receipt_key,
                quota_pool_id=arguments.quota_pool,
                prompt=sys.stdin.read(), timeout_seconds=arguments.timeout_seconds,
                model=arguments.model, reasoning_effort=arguments.reasoning_effort,
                operation="write" if arguments.command == "run" else "review",
                dispatch_policy_path=arguments.dispatch_policy_path,
            )
        except ValueError as error:
            result = {
                "schema_version": 1, "command": arguments.command,
                "status": "invalid_request", "error": str(error),
            }
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
