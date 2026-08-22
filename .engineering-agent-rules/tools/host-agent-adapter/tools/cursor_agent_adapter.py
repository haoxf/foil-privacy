#!/usr/bin/env python3
"""Synchronous Cursor CLI adapter over the host-agent-adapter runtime."""

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
EXECUTABLE_NAMES = ("cursor-agent", "agent")
POLICY_KEY = "cursor_adapter_enabled"
HEARTBEAT_INTERVAL_SECONDS = 25.0
MIN_HEARTBEAT_INTERVAL_SECONDS = 0.25
AUTO_MODEL = re.compile(r"^(?:[>*✓•-]\s*)?auto(?:\s+-\s+auto(?:\s+\(|$)|$)", re.I)
_SAFE_PROGRESS_TOKEN = re.compile(r"^[a-z0-9_]+$")
_UPPER_REVIEW_MARKER = re.compile(
    r"(?m)^AGENT_MODEL_REVIEW:\s*(PASS|BLOCKED|UNAVAILABLE)\s*$",
)
FALLBACKS = {
    "rate_limit": ("rate limit", "rate_limit", "too many requests", "status 429"),
    "quota": ("quota", "usage limit", "credit limit", "credits exhausted"),
    "model_unavailable": (
        "model unavailable", "unknown model", "not available for this account",
    ),
    "authentication": (
        "not authenticated", "authentication required", "unauthorized", "status 401",
    ),
}

_RUNTIME = None
_DISPATCH = None
_TIER = None
_REASONING = None
_IDENTITY = None


class _ProgressReporter:
    """Emit fixed-shape progress without forwarding Cursor-owned content."""

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
            "cursor-adapter progress", f"stage={stage}",
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

    def feed_stdout(self, chunk: bytes, attempt: int) -> None:
        self._pending.extend(chunk)
        while True:
            newline = self._pending.find(b"\n")
            if newline < 0:
                return
            line = bytes(self._pending[:newline])
            del self._pending[:newline + 1]
            self._consume(line, attempt)

    def _consume(self, line: bytes, attempt: int) -> None:
        if not line.strip():
            return
        try:
            event = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        if type(event) is not dict:
            return
        self._event_count += 1
        if (
            event.get("type") == "system" and event.get("subtype") == "init"
            and type(event.get("model")) is str and bool(event["model"].strip())
        ):
            self._once("model_receipt", attempt=attempt)

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
    global _RUNTIME, _DISPATCH, _TIER, _REASONING, _IDENTITY
    if _RUNTIME is not None:
        return
    start = Path(__file__)
    runtime_path = start.resolve().parent / "host_adapter_runtime.py"
    spec = importlib.util.spec_from_file_location(
        "_engineering_host_adapter_runtime_cursor", runtime_path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("host-agent-adapter runtime is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _RUNTIME = module
    _DISPATCH = module.load_sibling_module(
        start, "agent-model-router", "dispatch_policy.py",
        "_engineering_dispatch_policy_cursor",
    )
    _TIER = module.load_sibling_module(
        start, "agent-model-router", "tier_policy.py",
        "_engineering_tier_policy_cursor",
    )
    _REASONING = module.load_sibling_module(
        start, "agent-model-router", "reasoning_policy.py",
        "_engineering_reasoning_policy_cursor",
    )
    _IDENTITY = module.load_sibling_module(
        start, "agent-model-router", "model_identity.py",
        "_engineering_model_identity_cursor",
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


def build_argv(*, executable: str, model: str, operation: str) -> list[str]:
    if operation == "write":
        command = [
            executable, "--print", "--force", "--trust", "--sandbox", "enabled",
        ]
    else:
        command = [
            executable, "--print", "--trust", "--sandbox", "enabled",
            "--mode", "plan",
        ]
    command.extend(("--output-format", "stream-json", "--model", model))
    return command


def parse_cursor_output(stdout: str, stderr: str) -> dict[str, Any]:
    _load_tools()
    events = _RUNTIME.parse_jsonl(stdout)
    last_message = None
    terminal = [event for event in events if event.get("type") == "result"]
    success = bool(
        len(terminal) == 1 and events and terminal[0] is events[-1]
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
    for event in events:
        if event.get("type") == "assistant" and type(event.get("message")) is str:
            last_message = event["message"]
    if not last_message and terminal_result:
        last_message = terminal_result
    review_source = last_message or terminal_result or ""
    review_matches = list(_UPPER_REVIEW_MARKER.finditer(review_source))
    review_status = None
    if (
        len(review_matches) == 1
        and not review_source[review_matches[0].end():].strip()
    ):
        review_status = review_matches[0].group(1).casefold()
    error_text = "\n".join(
        json.dumps(event, ensure_ascii=False, sort_keys=True)
        for event in events
        if event.get("type") == "error" or event.get("is_error") is True
    )
    combined = f"{stderr}\n{error_text}".casefold()
    usage_limit = any(
        phrase in combined for phrase in FALLBACKS["quota"] + FALLBACKS["rate_limit"]
    )
    model_unsupported = any(
        phrase in combined for phrase in FALLBACKS["model_unavailable"]
    )
    fallback = None
    for reason, phrases in FALLBACKS.items():
        if any(item in combined for item in phrases):
            fallback = reason
            break
    return {
        "success": success and not usage_limit and not model_unsupported,
        "model": model_name,
        "model_receipt_valid": len(init_models) == 1,
        "last_agent_message": last_message,
        "usage_limit": usage_limit,
        "model_unsupported": model_unsupported,
        "fallback_reason": fallback,
        "review_status": review_status,
        "error_text": error_text,
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
            "_engineering_quota_cache_cursor", tool_path,
        )
        if spec is None or spec.loader is None:
            return False
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        result = module.invalidate_pool(quota_pool_id, cursor_cache_path=cache_path)
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
        "requested_model": None, "model_source": None, "model_receipt": None,
        "last_agent_message": None, "git_status": None,
        "fallback_eligible": False, "fallback_reason": None, "stderr_summary": "",
        "quota_pool_id": None, "quota_cache_invalidated": False,
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
        "authenticated": None, "auto_model_available": None, "ready": False,
        "runtime_ready": False, "dispatch_policy": policy,
    }
    if resolved is None:
        return result
    status = _RUNTIME.run_process(
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
    models = _RUNTIME.run_process(
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
    if result["runtime_ready"] and not policy["policy"][POLICY_KEY]:
        result["status"] = (
            "dispatch_policy_invalid"
            if policy["status"] == "invalid"
            else "globally_disabled"
        )
        result["ready"] = False
    return result


def run_cursor(
    *, executable: str | None, repo: Path | str,
    required_tier: str, assessed_tier: str,
    required_reasoning_depth: str, assessed_reasoning_depth: str,
    receipt_key: str, prompt: str,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    model: str = "auto", model_source: str = "auto",
    quota_pool_id: str = "cursor_first_party",
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
            fallback_reason="cursor_adapter_disabled",
            dispatch_policy=policy,
            requested_model=model, model_source=model_source,
        )
    if _RUNTIME.nested_adapter(environ=env):
        return _receipt(
            command=operation, status="nested_adapter_forbidden",
            fallback_eligible=True, fallback_reason="nested_adapter",
            dispatch_policy=policy,
            requested_model=model, model_source=model_source,
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
    repo_path = Path(repo).expanduser().resolve()
    brief = prompt
    if allowed_write_paths:
        roots = ", ".join(allowed_write_paths)
        brief = brief.rstrip() + (
            f"\n\nWrite only within these repository-relative paths: {roots}\n"
        )
    if operation == "review":
        brief = _review_prompt(brief, required_tier, required_reasoning_depth)
    resolved = _RUNTIME.resolve_executable(executable, EXECUTABLE_NAMES)
    if resolved is None:
        return _receipt(
            command=operation, status="fallback_before_write",
            fallback_eligible=True, fallback_reason="missing_executable",
            requested_model=model, model_source=model_source,
            dispatch_policy=policy, quota_pool_id=quota_pool_id,
        )
    command = build_argv(executable=resolved, model=model, operation=operation)
    progress = _ProgressReporter()
    process = _RUNTIME.run_process(
        command, cwd=repo_path, input_text=brief,
        timeout_seconds=timeout_seconds, env=_RUNTIME.adapter_environ(env),
        progress=progress, attempt=1,
    )
    parsed = parse_cursor_output(process["stdout"], process["stderr"])
    identity_matches = (
        parsed["model_receipt_valid"]
        and _IDENTITY.receipt_key_matches(receipt_key, parsed["model"])
    )
    model_receipt = {
        "reported_model": parsed["model"], "assessed_tier": assessed_tier,
        "requested_model": model, "model_source": model_source,
        "receipt_key": receipt_key,
        "required_tier": required_tier,
        "assessed_reasoning_depth": assessed_reasoning_depth,
        "required_reasoning_depth": required_reasoning_depth,
        "identity_matches": identity_matches,
        "valid": parsed["model_receipt_valid"] and identity_matches,
        "sufficient": (
            parsed["model_receipt_valid"] and identity_matches
            and _TIER.tier_at_least(assessed_tier, required_tier)
            and _REASONING.reasoning_depth_at_least(
                assessed_reasoning_depth, required_reasoning_depth
            )
        ),
    }
    quota_cache_invalidated = False
    reason = parsed["fallback_reason"]
    if process["launch_error"]:
        reason = "launch_error"
    if reason in {"quota", "rate_limit"}:
        quota_cache_invalidated = _report_runtime_limit(quota_pool_id, usage_cache_path)
    if process["timed_out"]:
        status = "timed_out"
    elif parsed["usage_limit"] or parsed["model_unsupported"]:
        status = "fallback_before_write"
    elif process["launch_error"]:
        status = "fallback_before_write"
    elif parsed["success"] and not identity_matches:
        status = "model_receipt_mismatch"
        reason = None
    elif parsed["success"] and not model_receipt["sufficient"]:
        status = "insufficient_model_capability"
        reason = None
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
    elif reason is not None:
        status = "fallback_before_write"
    elif process["returncode"] == 0:
        status = "protocol_error"
    else:
        status = "failed"
    receipt = _receipt(
        command=operation, status=status,
        candidate_ready=status == "candidate_ready",
        review_complete=status == "review_passed",
        upper_review_required=operation == "write" and status == "candidate_ready",
        returncode=process["returncode"], timed_out=process["timed_out"],
        requested_model=model, model_source=model_source,
        model_receipt=model_receipt,
        last_agent_message=parsed["last_agent_message"],
        git_status=_RUNTIME.git_status_facts(repo_path),
        fallback_eligible=status == "fallback_before_write",
        fallback_reason=reason,
        quota_pool_id=quota_pool_id,
        quota_cache_invalidated=quota_cache_invalidated,
        dispatch_policy=policy,
        stderr_summary=(process["stderr"] or parsed["error_text"])[:_RUNTIME.STDERR_LIMIT],
    )
    progress.completed(status, 1)
    return receipt


def _parser() -> argparse.ArgumentParser:
    _load_tools()
    parser = argparse.ArgumentParser(description="Preflight or run Cursor Agent synchronously.")
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("preflight")
    check.add_argument("--cursor-command")
    check.add_argument("--timeout-seconds", type=float, default=15.0)
    check.add_argument("--dispatch-policy-path", type=Path)
    run = commands.add_parser("run")
    review = commands.add_parser("review")
    for command in (run, review):
        command.add_argument("--repo", required=True, type=Path)
        command.add_argument("--cursor-command")
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
        command.add_argument(
            "--quota-pool", required=True,
            choices=("cursor_first_party", "cursor_api"),
        )
        command.add_argument("--model", default="auto")
        command.add_argument(
            "--model-source", default="auto",
            choices=("auto", "third_party", "cursor_native"),
        )
        command.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
        command.add_argument("--dispatch-policy-path", type=Path)
    run.add_argument("--allowed-write-path", action="append", default=[], dest="paths")
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
                allowed_write_paths=getattr(arguments, "paths", []),
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
