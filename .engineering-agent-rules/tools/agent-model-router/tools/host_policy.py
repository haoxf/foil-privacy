#!/usr/bin/env python3
"""Detect the current agent host and which other runtimes may be dispatched."""

from __future__ import annotations

import os
import shutil
from typing import Any, Callable


HOSTS = ("cursor", "codex", "unknown")
CURSOR_HOST_FLAGS = ("CURSOR_AGENT",)
CODEX_HOST_FLAGS = ("CODEX_SANDBOX", "CODEX_CI")
CODEX_EXECUTORS = {"codex_agent", "codex_adapter"}
CURSOR_EXECUTORS = {"cursor_agent", "cursor_session"}


def is_host(value: object) -> bool:
    return type(value) is str and value in HOSTS


def _flag_on(environ: dict[str, str], name: str) -> bool:
    value = environ.get(name)
    return type(value) is str and value.strip() not in {"", "0", "false", "False"}


def detect_host(*, environ: dict[str, str] | None = None) -> dict[str, Any]:
    # Cursor 用正证据 CURSOR_AGENT（子进程会继承）。Codex 只用
    # CODEX_SANDBOX / CODEX_CI，不用 CODEX_HOME。冲突或全无则 unknown，禁止 adapter。
    env = os.environ if environ is None else environ
    cursor_evidence = [name for name in CURSOR_HOST_FLAGS if _flag_on(env, name)]
    codex_evidence = [name for name in CODEX_HOST_FLAGS if _flag_on(env, name)]
    if cursor_evidence and not codex_evidence:
        return {
            "host": "cursor",
            "source": "environment",
            "evidence": cursor_evidence,
            "reason": None,
        }
    if codex_evidence and not cursor_evidence:
        return {
            "host": "codex",
            "source": "environment",
            "evidence": codex_evidence,
            "reason": None,
        }
    if cursor_evidence and codex_evidence:
        return {
            "host": "unknown",
            "source": "environment",
            "evidence": cursor_evidence + codex_evidence,
            "reason": "conflicting_host_signals",
        }
    return {
        "host": "unknown",
        "source": "environment",
        "evidence": [],
        "reason": "no_host_signal",
    }


def resolve_host(
    explicit: str | None, *, environ: dict[str, str] | None = None,
) -> dict[str, Any]:
    if explicit is None:
        return detect_host(environ=environ)
    if not is_host(explicit):
        raise ValueError("invalid host")
    return {
        "host": explicit,
        "source": "explicit",
        "evidence": [explicit],
        "reason": None,
    }


def _adapter_probe(
    *, host: str, self_host: str, enabled: bool, cli_available: bool,
) -> dict[str, Any]:
    if host == self_host:
        return {"available": False, "reason": "self_dispatch_forbidden"}
    if host == "unknown":
        return {"available": False, "reason": "host_unknown_adapter_forbidden"}
    if enabled is not True:
        return {"available": False, "reason": "adapter_disabled"}
    if not cli_available:
        return {"available": False, "reason": "cli_missing"}
    return {"available": True, "reason": None}


def probe_dispatch(
    *, host: str, cursor_adapter_enabled: bool,
    codex_adapter_enabled: bool,
    which: Callable[[str], str | None] = shutil.which,
) -> dict[str, Any]:
    if not is_host(host):
        raise ValueError("invalid host")
    agent_cli = which("agent") is not None
    codex_cli = which("codex") is not None
    session_available = host == "cursor"
    if session_available:
        session_reason = None
    else:
        session_reason = "not_cursor_host"

    if host == "codex":
        native_codex = {
            "available": True, "reason": None, "source": "current_host",
        }
    elif host == "unknown" and codex_cli:
        native_codex = {
            "available": True, "reason": None, "source": "cli_available",
        }
    elif host == "unknown":
        native_codex = {
            "available": False, "reason": "cli_missing", "source": "unavailable",
        }
    else:
        native_codex = {
            "available": False, "reason": "not_codex_host", "source": "unavailable",
        }

    return {
        "host": host,
        "cursor_session": {
            "available": session_available,
            "reason": session_reason,
        },
        "cursor_adapter": _adapter_probe(
            host=host, self_host="cursor", enabled=cursor_adapter_enabled,
            cli_available=agent_cli,
        ),
        "codex_agent": native_codex,
        "codex_adapter": _adapter_probe(
            host=host, self_host="codex", enabled=codex_adapter_enabled,
            cli_available=codex_cli,
        ),
    }


def cursor_executor(dispatch: dict[str, Any]) -> str | None:
    if dispatch.get("cursor_session", {}).get("available") is True:
        return "cursor_session"
    if dispatch.get("cursor_adapter", {}).get("available") is True:
        return "cursor_agent"
    return None


def codex_executor(dispatch: dict[str, Any]) -> str | None:
    if dispatch.get("codex_agent", {}).get("available") is True:
        return "codex_agent"
    if dispatch.get("codex_adapter", {}).get("available") is True:
        return "codex_adapter"
    return None


def include_codex(dispatch: dict[str, Any]) -> bool:
    return codex_executor(dispatch) is not None


def include_cursor(dispatch: dict[str, Any]) -> bool:
    return cursor_executor(dispatch) is not None
