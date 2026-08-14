#!/usr/bin/env python3
"""Detect the current agent host and which other runtimes may be dispatched."""

from __future__ import annotations

import os
import shutil
from typing import Any, Callable


HOSTS = ("cursor", "codex", "unknown")
CURSOR_HOST_FLAGS = ("CURSOR_AGENT",)
CODEX_HOST_FLAGS = ("CODEX_SANDBOX", "CODEX_CI")


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


def probe_dispatch(
    *, host: str, cursor_adapter_enabled: bool,
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

    adapter_available = False
    if host == "cursor":
        adapter_reason = "self_dispatch_forbidden"
    elif host == "unknown":
        adapter_reason = "host_unknown_adapter_forbidden"
    elif cursor_adapter_enabled is not True:
        adapter_reason = "adapter_disabled"
    elif not agent_cli:
        adapter_reason = "cli_missing"
    else:
        adapter_available = True
        adapter_reason = None

    if host == "codex":
        codex_available = True
        codex_reason = None
        codex_source = "current_host"
    elif codex_cli:
        codex_available = True
        codex_reason = None
        codex_source = "cli_available"
    else:
        codex_available = False
        codex_reason = "cli_missing"
        codex_source = "unavailable"

    return {
        "host": host,
        "cursor_session": {
            "available": session_available,
            "reason": session_reason,
        },
        "cursor_adapter": {
            "available": adapter_available,
            "reason": adapter_reason,
        },
        "codex_agent": {
            "available": codex_available,
            "reason": codex_reason,
            "source": codex_source,
        },
    }


def cursor_executor(dispatch: dict[str, Any]) -> str | None:
    if dispatch.get("cursor_session", {}).get("available") is True:
        return "cursor_session"
    if dispatch.get("cursor_adapter", {}).get("available") is True:
        return "cursor_agent"
    return None


def include_codex(dispatch: dict[str, Any]) -> bool:
    return dispatch.get("codex_agent", {}).get("available") is True


def include_cursor(dispatch: dict[str, Any]) -> bool:
    return cursor_executor(dispatch) is not None
