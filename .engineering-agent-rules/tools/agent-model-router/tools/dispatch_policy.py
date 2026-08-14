#!/usr/bin/env python3
"""Read and atomically update the global Agent dispatch policy."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import stat
import tempfile
from typing import Any


SCHEMA_VERSION = 2
PREFERRED_PROVIDERS = {"auto", "codex", "cursor"}
MAX_POLICY_BYTES = 4096
CONFIG_DIRECTORY = Path.home() / ".config/engineering-agent-rules"
POLICY_PATH = CONFIG_DIRECTORY / "dispatch-policy.json"
POLICY_PATH_ENVIRONMENT = "ENGINEERING_AGENT_RULES_DISPATCH_POLICY_PATH"
KNOWN_KEYS = {
    "schema_version",
    "provider_preference",
    "cursor_adapter_enabled",
    "codex_adapter_enabled",
    "updated_at",
}
DEFAULT_POLICY = {
    "provider_preference": "auto",
    "cursor_adapter_enabled": True,
    "codex_adapter_enabled": True,
}
SAFE_FALLBACK_POLICY = {
    "provider_preference": "auto",
    "cursor_adapter_enabled": False,
    "codex_adapter_enabled": False,
}


def _result(
    *, path: Path, status: str, policy: dict[str, Any], error: str | None = None,
) -> dict[str, Any]:
    value = {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "path": str(path),
        "policy": policy,
    }
    if error is not None:
        value["error"] = error
    return value


def _policy_path(path: Path | None) -> Path:
    if path is not None:
        return path
    override = os.environ.get(POLICY_PATH_ENVIRONMENT)
    return Path(override).expanduser() if override else POLICY_PATH


def _as_bool(value: Any, field: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{field} must be a boolean")
    return value


def _validated_policy(value: Any) -> dict[str, Any]:
    if type(value) is not dict:
        raise ValueError("policy must be an object")
    extra = set(value) - KNOWN_KEYS
    if extra:
        raise ValueError("policy keys do not match schema v2")
    version = value.get("schema_version")
    if version not in {1, 2}:
        raise ValueError("unsupported policy schema version")
    preference = value.get("provider_preference")
    if type(preference) is not str or preference not in PREFERRED_PROVIDERS:
        raise ValueError("provider_preference must be auto, codex, or cursor")
    updated_at = value.get("updated_at")
    if type(updated_at) is not str or not updated_at:
        raise ValueError("updated_at must be a non-empty string")
    try:
        datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("updated_at must be an ISO 8601 timestamp") from error
    cursor_enabled = value.get("cursor_adapter_enabled")
    if cursor_enabled is None:
        cursor_enabled = DEFAULT_POLICY["cursor_adapter_enabled"]
    else:
        cursor_enabled = _as_bool(cursor_enabled, "cursor_adapter_enabled")
    codex_enabled = value.get("codex_adapter_enabled")
    if codex_enabled is None:
        codex_enabled = DEFAULT_POLICY["codex_adapter_enabled"]
    else:
        codex_enabled = _as_bool(codex_enabled, "codex_adapter_enabled")
    return {
        "provider_preference": preference,
        "cursor_adapter_enabled": cursor_enabled,
        "codex_adapter_enabled": codex_enabled,
        "updated_at": updated_at,
    }


def load_policy(path: Path | None = None) -> dict[str, Any]:
    target = _policy_path(path)
    try:
        info = target.lstat()
    except FileNotFoundError:
        return _result(
            path=target, status="default", policy={**DEFAULT_POLICY, "updated_at": None},
        )
    except OSError as error:
        return _result(
            path=target, status="invalid", policy={**SAFE_FALLBACK_POLICY, "updated_at": None},
            error=f"policy metadata unavailable: {error.strerror or type(error).__name__}",
        )
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        return _result(
            path=target, status="invalid", policy={**SAFE_FALLBACK_POLICY, "updated_at": None},
            error="policy path must be a regular file and not a symbolic link",
        )
    if info.st_size > MAX_POLICY_BYTES:
        return _result(
            path=target, status="invalid", policy={**SAFE_FALLBACK_POLICY, "updated_at": None},
            error="policy file exceeds the supported size",
        )
    try:
        raw = target.read_bytes()
        if len(raw) > MAX_POLICY_BYTES:
            raise ValueError("policy file exceeds the supported size")
        parsed = json.loads(raw.decode("utf-8"))
        policy = _validated_policy(parsed)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        return _result(
            path=target, status="invalid", policy={**SAFE_FALLBACK_POLICY, "updated_at": None},
            error=str(error),
        )
    return _result(path=target, status="ok", policy=policy)


def _write_policy(target: Path, policy: dict[str, Any]) -> dict[str, Any]:
    parent = target.parent
    if parent.exists() and parent.is_symlink():
        raise ValueError("policy directory must not be a symbolic link")
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not parent.is_dir():
        raise ValueError("policy parent must be a directory")
    if target.exists() and (target.is_symlink() or not target.is_file()):
        raise ValueError("policy path must be a regular file and not a symbolic link")
    payload = {
        "schema_version": SCHEMA_VERSION,
        "provider_preference": policy["provider_preference"],
        "cursor_adapter_enabled": policy["cursor_adapter_enabled"],
        "codex_adapter_enabled": policy["codex_adapter_enabled"],
        "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    encoded = (json.dumps(
        payload, ensure_ascii=False, sort_keys=True, indent=2,
    ) + "\n").encode("utf-8")
    temporary_path: Path | None = None
    try:
        descriptor, raw_path = tempfile.mkstemp(
            dir=parent, prefix=f".{target.name}.", suffix=".tmp",
        )
        temporary_path = Path(raw_path)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, target)
        temporary_path = None
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
    return load_policy(target)


def set_policy(
    *,
    provider_preference: str | None = None,
    cursor_adapter_enabled: bool | None = None,
    codex_adapter_enabled: bool | None = None,
    path: Path | None = None,
) -> dict[str, Any]:
    if (
        provider_preference is None
        and cursor_adapter_enabled is None
        and codex_adapter_enabled is None
    ):
        raise ValueError("at least one policy field is required")
    if provider_preference is not None and provider_preference not in PREFERRED_PROVIDERS:
        raise ValueError("provider_preference must be auto, codex, or cursor")
    if cursor_adapter_enabled is not None and type(cursor_adapter_enabled) is not bool:
        raise ValueError("cursor_adapter_enabled must be a boolean")
    if codex_adapter_enabled is not None and type(codex_adapter_enabled) is not bool:
        raise ValueError("codex_adapter_enabled must be a boolean")
    target = _policy_path(path)
    current = load_policy(target)
    if current["status"] == "invalid":
        base = {**SAFE_FALLBACK_POLICY}
    elif current["status"] == "default":
        base = {**DEFAULT_POLICY}
    else:
        base = dict(current["policy"])
    if provider_preference is not None:
        base["provider_preference"] = provider_preference
    if cursor_adapter_enabled is not None:
        base["cursor_adapter_enabled"] = cursor_adapter_enabled
    if codex_adapter_enabled is not None:
        base["codex_adapter_enabled"] = codex_adapter_enabled
    return _write_policy(target, base)


def _enabled(value: str) -> bool:
    return value == "enabled"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read or update global Agent dispatch policy")
    commands = parser.add_subparsers(dest="command", required=True)
    get = commands.add_parser("get")
    get.add_argument("--path", type=Path)
    set_command = commands.add_parser("set")
    set_command.add_argument("--path", type=Path)
    set_command.add_argument(
        "--prefer-provider", required=False, choices=sorted(PREFERRED_PROVIDERS),
    )
    set_command.add_argument(
        "--cursor-adapter", required=False, choices=("disabled", "enabled"),
    )
    set_command.add_argument(
        "--codex-adapter", required=False, choices=("disabled", "enabled"),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "get":
            result = load_policy(arguments.path)
            code = 0 if result["status"] in {"default", "ok"} else 2
        else:
            result = set_policy(
                provider_preference=arguments.prefer_provider,
                cursor_adapter_enabled=(
                    None if arguments.cursor_adapter is None
                    else _enabled(arguments.cursor_adapter)
                ),
                codex_adapter_enabled=(
                    None if arguments.codex_adapter is None
                    else _enabled(arguments.codex_adapter)
                ),
                path=arguments.path,
            )
            code = 0
    except (OSError, ValueError) as error:
        result = {
            "schema_version": SCHEMA_VERSION,
            "status": "write_failed",
            "error": str(error),
        }
        code = 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
