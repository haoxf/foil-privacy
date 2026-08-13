"""Strict JSON models for the lean task runtime."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any


class ValidationError(ValueError):
    pass


class StaleCandidateError(ValidationError):
    pass


IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9._-]{0,95}$")
HEX_DIGEST = re.compile(r"^[0-9a-f]{64}$")
PHASES = {"planned", "armed", "active", "complete"}
TICKET_STATES = {"pending", "active", "complete"}
RESERVED_ROOTS = {".git", ".agent-task-runtime"}


def _load_tier_policy() -> Any:
    path = (
        Path(__file__).resolve().parents[3]
        / "agent-model-router/tools/tier_policy.py"
    )
    specification = importlib.util.spec_from_file_location(
        "_engineering_agent_rules_tier_policy", path,
    )
    if specification is None or specification.loader is None:
        raise RuntimeError("agent-model-router tier policy is unavailable")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _load_reasoning_policy() -> Any:
    path = (
        Path(__file__).resolve().parents[3]
        / "agent-model-router/tools/reasoning_policy.py"
    )
    specification = importlib.util.spec_from_file_location(
        "_engineering_agent_rules_reasoning_policy", path,
    )
    if specification is None or specification.loader is None:
        raise RuntimeError("agent-model-router reasoning policy is unavailable")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


_tier_policy = _load_tier_policy()
_tier_names = _tier_policy.TIER_NAMES
_tier_at_least = _tier_policy.tier_at_least
_reasoning_policy = _load_reasoning_policy()
_reasoning_depth_names = _reasoning_policy.REASONING_DEPTH_NAMES
_reasoning_depth_at_least = _reasoning_policy.reasoning_depth_at_least


def _reject_constant(value: str) -> None:
    raise ValidationError(f"invalid JSON constant: {value}")


def strict_json_loads(payload: bytes | str) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValidationError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        return json.loads(payload, object_pairs_hook=pairs, parse_constant=_reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValidationError(f"invalid JSON: {error}") from error


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8") + b"\n"


def digest_value(domain: str, value: Any) -> str:
    return hashlib.sha256(domain.encode("utf-8") + b"\0" + canonical_json_bytes(value)).hexdigest()


def _object(
    value: Any, label: str, required: set[str], optional: set[str] | frozenset[str] = frozenset()
) -> dict[str, Any]:
    if type(value) is not dict:
        raise ValidationError(f"{label} must be an object")
    keys = set(value)
    missing = required - keys
    unknown = keys - required - optional
    if missing:
        raise ValidationError(f"{label} missing fields: {sorted(missing)}")
    if unknown:
        raise ValidationError(f"{label} unknown fields: {sorted(unknown)}")
    return value


def _string(value: Any, label: str) -> str:
    if type(value) is not str or not value.strip() or value != value.strip():
        raise ValidationError(f"{label} must be a trimmed non-empty string")
    return value


def _identifier(value: Any, label: str) -> str:
    text = _string(value, label)
    if not IDENTIFIER.fullmatch(text):
        raise ValidationError(f"{label} is not a safe identifier")
    return text


def _strings(value: Any, label: str, *, nonempty: bool) -> list[str]:
    if type(value) is not list or (nonempty and not value):
        raise ValidationError(f"{label} must be {'a non-empty ' if nonempty else ''}array")
    result = [_string(item, f"{label}[]") for item in value]
    if len(result) != len(set(result)):
        raise ValidationError(f"{label} must not contain duplicates")
    return result


def validate_path(value: Any, label: str) -> str:
    text = _string(value, label)
    if "\\" in text or text.startswith("/") or text.endswith("/"):
        raise ValidationError(f"{label} must be a normalized repo-relative path")
    path = PurePosixPath(text)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValidationError(f"{label} must be a normalized repo-relative path")
    if not path.parts or path.parts[0] in RESERVED_ROOTS:
        raise ValidationError(f"{label} uses a reserved root")
    return text


def validate_paths(value: Any, label: str, *, nonempty: bool = True) -> list[str]:
    if type(value) is not list or (nonempty and not value):
        raise ValidationError(f"{label} must be {'a non-empty ' if nonempty else ''}array")
    paths = [validate_path(item, f"{label}[]") for item in value]
    if len(paths) != len(set(paths)):
        raise ValidationError(f"{label} contains duplicates")
    return paths


def path_is_within(path: str, root: str) -> bool:
    return path == root or path.startswith(root + "/")


def _commands(value: Any, label: str) -> list[list[str]]:
    if type(value) is not list or not value:
        raise ValidationError(f"{label} must be a non-empty argv array list")
    commands: list[list[str]] = []
    for index, command in enumerate(value):
        if type(command) is not list or not command:
            raise ValidationError(f"{label}[{index}] must be a non-empty argv array")
        commands.append([_string(arg, f"{label}[{index}][]") for arg in command])
    return commands


def validate_definition(value: Any) -> dict[str, Any]:
    definition = _object(
        value,
        "task",
        {
            "schema_version", "task_id", "title", "goal", "acceptance", "non_goals",
            "write_paths", "run_verification", "git_policy", "context", "tickets",
        },
    )
    if definition["schema_version"] != 2:
        raise ValidationError("task.schema_version must equal 2")
    _identifier(definition["task_id"], "task.task_id")
    for field in ("title", "goal"):
        _string(definition[field], f"task.{field}")
    _strings(definition["acceptance"], "task.acceptance", nonempty=True)
    _strings(definition["non_goals"], "task.non_goals", nonempty=False)
    task_paths = validate_paths(definition["write_paths"], "task.write_paths")
    _commands(definition["run_verification"], "task.run_verification")

    git_policy = _object(
        definition["git_policy"], "task.git_policy",
        {
            "branch_mode", "target_branch", "return_branch",
            "commit_policy", "integration_mode",
        },
    )
    if git_policy["branch_mode"] not in {"reuse_current", "create_task_branch"}:
        raise ValidationError("unsupported branch_mode")
    if git_policy["commit_policy"] not in {"none", "verified_ticket_commits"}:
        raise ValidationError("unsupported commit_policy")
    if git_policy["integration_mode"] not in {"no_integration", "ff_only_back"}:
        raise ValidationError("unsupported integration_mode")
    if git_policy["branch_mode"] == "reuse_current":
        if git_policy["target_branch"] is not None or git_policy["return_branch"] is not None:
            raise ValidationError("reuse_current forbids target_branch and return_branch")
        if git_policy["integration_mode"] != "no_integration":
            raise ValidationError("reuse_current requires no_integration")
    else:
        target = _string(git_policy["target_branch"], "task.git_policy.target_branch")
        returned = _string(git_policy["return_branch"], "task.git_policy.return_branch")
        if target == returned:
            raise ValidationError("target_branch must differ from return_branch")
    if git_policy["integration_mode"] == "ff_only_back" and git_policy["commit_policy"] != "verified_ticket_commits":
        raise ValidationError("ff_only_back requires verified_ticket_commits")

    context = _object(
        definition["context"], "task.context",
        {
            "repository", "worktree", "branch_or_detached", "base_head",
            "dirty_included", "dirty_excluded",
        },
    )
    for field in ("repository", "worktree", "branch_or_detached", "base_head"):
        _string(context[field], f"task.context.{field}")
    if not context["repository"].startswith("/") or not context["worktree"].startswith("/"):
        raise ValidationError("repository and worktree must be absolute")
    validate_paths(context["dirty_included"], "task.context.dirty_included", nonempty=False)
    validate_paths(context["dirty_excluded"], "task.context.dirty_excluded", nonempty=False)

    tickets = definition["tickets"]
    if type(tickets) is not list or not 2 <= len(tickets) <= 6:
        raise ValidationError("task.tickets must contain 2 to 6 tickets")
    seen: set[str] = set()
    for index, ticket_value in enumerate(tickets):
        ticket = _object(
            ticket_value, f"task.tickets[{index}]",
            {
                "id", "title", "goal", "acceptance", "non_goals", "write_paths",
                "depends_on", "verification", "stop_conditions", "risk",
            },
        )
        ticket_id = _identifier(ticket["id"], f"task.tickets[{index}].id")
        if ticket_id in seen:
            raise ValidationError(f"duplicate ticket id: {ticket_id}")
        for field in ("title", "goal"):
            _string(ticket[field], f"ticket.{field}")
        _strings(ticket["acceptance"], "ticket.acceptance", nonempty=True)
        _strings(ticket["non_goals"], "ticket.non_goals", nonempty=False)
        ticket_paths = validate_paths(ticket["write_paths"], "ticket.write_paths")
        if any(not any(path_is_within(path, root) for root in task_paths) for path in ticket_paths):
            raise ValidationError(f"ticket {ticket_id} write path escapes task scope")
        dependencies = _strings(ticket["depends_on"], "ticket.depends_on", nonempty=False)
        if any(dependency not in seen for dependency in dependencies):
            raise ValidationError(f"ticket {ticket_id} dependencies must refer to earlier tickets")
        _commands(ticket["verification"], "ticket.verification")
        _strings(ticket["stop_conditions"], "ticket.stop_conditions", nonempty=True)
        risk = _object(
            ticket["risk"], "ticket.risk",
            {"required_tier", "reasoning_depth"},
        )
        if risk["required_tier"] not in _tier_names:
            raise ValidationError("unknown required tier")
        if risk["reasoning_depth"] not in _reasoning_depth_names:
            raise ValidationError("unknown reasoning depth")
        seen.add(ticket_id)
    return definition


def task_digest(definition: dict[str, Any]) -> str:
    return digest_value("agent-task-runtime:task:v2", definition)


def validate_review(
    value: Any, *, fingerprint: str, required_tier: str,
    required_reasoning_depth: str,
) -> dict[str, Any]:
    review = _object(
        value,
        "review",
        {
            "schema_version", "reviewer", "provider", "model", "tier",
            "reasoning_depth",
            "candidate_fingerprint", "conclusion", "findings", "counterexample",
        },
    )
    if review["schema_version"] != 1:
        raise ValidationError("review.schema_version must equal 1")
    _string(review["reviewer"], "review.reviewer")
    _string(review["provider"], "review.provider")
    _string(review["model"], "review.model")
    if not _tier_at_least(review["tier"], required_tier):
        raise ValidationError("review tier is below the required tier")
    if not _reasoning_depth_at_least(
        review["reasoning_depth"], required_reasoning_depth
    ):
        raise ValidationError("review reasoning depth is below the required depth")
    if review["candidate_fingerprint"] != fingerprint:
        raise StaleCandidateError("review does not bind the current candidate")
    if review["conclusion"] != "passed":
        raise ValidationError("review conclusion must be passed")
    _strings(review["findings"], "review.findings", nonempty=False)
    _string(review["counterexample"], "review.counterexample")
    return review


def _snapshot(value: Any, label: str) -> list[dict[str, str]]:
    if type(value) is not list:
        raise ValidationError(f"{label} must be an array")
    paths: list[str] = []
    for entry in value:
        _object(entry, f"{label}[]", {"path", "sha256"})
        paths.append(validate_path(entry["path"], f"{label}[].path"))
        if entry["sha256"] != "deleted" and not HEX_DIGEST.fullmatch(entry["sha256"]):
            raise ValidationError(f"{label}[].sha256 is invalid")
    if paths != sorted(set(paths)):
        raise ValidationError(f"{label} paths must be unique and sorted")
    return value


def _result(value: Any, label: str) -> dict[str, Any]:
    result = _object(value, label, {"candidate", "checks", "review"})
    preview = _object(result["candidate"], f"{label}.candidate", {"fingerprint", "files"})
    if not HEX_DIGEST.fullmatch(preview["fingerprint"]):
        raise ValidationError(f"{label}.candidate fingerprint is invalid")
    _snapshot(preview["files"], f"{label}.candidate.files")
    if type(result["checks"]) is not list or not result["checks"]:
        raise ValidationError(f"{label}.checks must be a non-empty array")
    for check in result["checks"]:
        _object(check, f"{label}.checks[]", {"argv", "returncode", "output_sha256"})
        _commands([check["argv"]], f"{label}.checks[].argv")
        if check["returncode"] != 0 or not HEX_DIGEST.fullmatch(check["output_sha256"]):
            raise ValidationError(f"{label}.checks[] is not a passing check")
    review = _object(
        result["review"], f"{label}.review",
        {
            "schema_version", "reviewer", "provider", "model", "tier",
            "reasoning_depth",
            "candidate_fingerprint", "conclusion", "findings", "counterexample",
        },
    )
    if review["schema_version"] != 1 or review["conclusion"] != "passed":
        raise ValidationError(f"{label}.review is not a passing v1 review")
    for field in ("reviewer", "provider", "model", "counterexample"):
        _string(review[field], f"{label}.review.{field}")
    if review["tier"] not in _tier_names:
        raise ValidationError(f"{label}.review tier is invalid")
    if review["reasoning_depth"] not in _reasoning_depth_names:
        raise ValidationError(f"{label}.review reasoning depth is invalid")
    if review["candidate_fingerprint"] != preview["fingerprint"]:
        raise ValidationError(f"{label}.review does not bind its candidate")
    _strings(review["findings"], f"{label}.review.findings", nonempty=False)
    return result


def validate_state(definition: dict[str, Any], value: Any) -> dict[str, Any]:
    state = _object(
        value,
        "state",
        {
            "schema_version", "task_id", "task_digest", "state", "owner",
            "active_ticket", "tickets", "initial_snapshot", "baseline_snapshot",
            "run_result",
        },
    )
    if state["schema_version"] != 2 or state["task_id"] != definition["task_id"]:
        raise ValidationError("state identity mismatch")
    if state["task_digest"] != task_digest(definition):
        raise ValidationError("state task digest mismatch")
    if state["state"] not in PHASES:
        raise ValidationError("unknown state")
    if state["owner"] is not None:
        _string(state["owner"], "state.owner")
    if state["active_ticket"] is not None:
        _identifier(state["active_ticket"], "state.active_ticket")
    if type(state["tickets"]) is not list or len(state["tickets"]) != len(definition["tickets"]):
        raise ValidationError("state ticket count mismatch")
    for expected, current in zip(definition["tickets"], state["tickets"], strict=True):
        _object(current, "state.ticket", {"id", "state", "result"})
        if current["id"] != expected["id"] or current["state"] not in TICKET_STATES:
            raise ValidationError("state ticket mismatch")
        if current["state"] == "complete":
            result = _result(current["result"], "state.ticket.result")
            validate_review(
                result["review"], fingerprint=result["candidate"]["fingerprint"],
                required_tier=expected["risk"]["required_tier"],
                required_reasoning_depth=expected["risk"]["reasoning_depth"],
            )
            expected_fingerprint = digest_value(
                f"agent-task-runtime:ticket:{current['id']}-candidate:v1",
                result["candidate"]["files"],
            )
            if result["candidate"]["fingerprint"] != expected_fingerprint:
                raise ValidationError("ticket result fingerprint mismatch")
        if current["state"] != "complete" and current["result"] is not None:
            raise ValidationError("incomplete ticket forbids result")
    for field in ("initial_snapshot", "baseline_snapshot"):
        if state[field] is not None:
            _snapshot(state[field], f"state.{field}")
    if state["state"] == "complete":
        result = _result(state["run_result"], "state.run_result")
        validate_review(
            result["review"], fingerprint=result["candidate"]["fingerprint"],
            required_tier="strong",
            required_reasoning_depth=max(
                (ticket["risk"]["reasoning_depth"] for ticket in definition["tickets"]),
                key=_reasoning_policy.REASONING_DEPTH_LEVELS.__getitem__,
            ),
        )
        expected_fingerprint = digest_value(
            "agent-task-runtime:run-candidate:v1", result["candidate"]["files"]
        )
        if result["candidate"]["fingerprint"] != expected_fingerprint:
            raise ValidationError("run result fingerprint mismatch")
    if state["state"] != "complete" and state["run_result"] is not None:
        raise ValidationError("incomplete state forbids run_result")
    return state
