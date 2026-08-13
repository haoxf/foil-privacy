#!/usr/bin/env python3
"""Credential-free Codex quota cache backed by the local app-server RPC."""

from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import time
from typing import Any, Callable

import cache_store


SCHEMA_VERSION = 1
CACHE_SCHEMA_VERSION = 2
CACHE_NAME = "codex-usage-v1.json"
DEFAULT_TTL_SECONDS = 30 * 60
REFRESH_RETRY_SECONDS = 2 * 60
DEFAULT_TIMEOUT_SECONDS = 8.0
MAX_RPC_BYTES = 512 * 1024
MAX_AUTH_BYTES = 256 * 1024


class UsageError(RuntimeError):
    pass


def _cache_path() -> Path:
    return cache_store.cache_path(CACHE_NAME)


def _auth_path() -> Path:
    return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "auth.json"


def _local_account_fingerprint(path: Path | None = None) -> tuple[str | None, str]:
    fingerprint, _evidence, status = _local_account_identity(path)
    return fingerprint, status


def _local_account_identity(
    path: Path | None = None,
) -> tuple[str | None, str | None, str]:
    selected = path or _auth_path()
    try:
        metadata = selected.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
            or metadata.st_size > MAX_AUTH_BYTES
        ):
            return None, None, "unsupported"
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(selected, flags)
        try:
            payload = os.read(descriptor, MAX_AUTH_BYTES + 1)
        finally:
            os.close(descriptor)
    except FileNotFoundError:
        return None, None, "signed_out"
    except OSError:
        return None, None, "unsupported"
    if len(payload) > MAX_AUTH_BYTES:
        return None, None, "unsupported"
    try:
        value = json.loads(payload.decode("utf-8"))
        tokens = value["tokens"]
        account_id = tokens["account_id"]
        id_token = tokens["id_token"]
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError):
        return None, None, "unsupported"
    if type(account_id) is not str or not account_id:
        return None, None, "signed_out"
    if type(id_token) is not str:
        return None, None, "unsupported"
    try:
        part = id_token.split(".")[1]
        payload = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        email = payload["email"]
    except (IndexError, ValueError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError):
        return None, None, "unsupported"
    if type(email) is not str or not email:
        return None, None, "unsupported"
    fingerprint = hashlib.sha256(
        ("engineering-agent-rules/codex-account/v1\0" + account_id).encode("utf-8")
    ).hexdigest()
    evidence = hashlib.sha256(
        ("engineering-agent-rules/codex-rpc-account/v1\0" + email.casefold()).encode("utf-8")
    ).hexdigest()
    return fingerprint, evidence, "ok"


def _terminate(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        try:
            process.terminate()
        except OSError:
            pass
    try:
        process.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            try:
                process.kill()
            except OSError:
                pass
        try:
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            pass


def _rpc(
    *, executable: str = "codex", timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    popen: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
) -> dict[str, Any]:
    resolved = shutil.which(executable)
    if resolved is None:
        raise UsageError("codex executable unavailable")
    try:
        process = popen(
            [resolved, "-s", "read-only", "-a", "untrusted", "app-server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            start_new_session=True,
            close_fds=True,
        )
    except OSError as error:
        raise UsageError("codex app-server launch failed") from error
    requests = [
        {
            "id": 1,
            "method": "initialize",
            "params": {
                "clientInfo": {"name": "engineering-agent-rules", "version": "1"},
                "capabilities": {},
            },
        },
        {"method": "initialized", "params": {}},
        {"id": 2, "method": "account/read", "params": {"refreshToken": False}},
        {"id": 3, "method": "account/rateLimits/read", "params": {}},
    ]
    responses: dict[int, dict[str, Any]] = {}
    total_bytes = 0
    deadline = time.monotonic() + max(0.1, timeout_seconds)
    try:
        assert process.stdin is not None
        assert process.stdout is not None
        process.stdin.write("".join(json.dumps(item, separators=(",", ":")) + "\n" for item in requests))
        process.stdin.flush()
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        while time.monotonic() < deadline and not {1, 2, 3} <= set(responses):
            events = selector.select(max(0.0, deadline - time.monotonic()))
            if not events:
                break
            line = process.stdout.readline()
            if not line:
                break
            total_bytes += len(line.encode("utf-8"))
            if total_bytes > MAX_RPC_BYTES:
                raise UsageError("codex app-server response too large")
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            request_id = value.get("id") if type(value) is dict else None
            if request_id in {1, 2, 3}:
                responses[request_id] = value
    finally:
        _terminate(process)
    if not {1, 2, 3} <= set(responses):
        raise UsageError("codex app-server response incomplete")
    if any("error" in responses[key] for key in (1, 2, 3)):
        raise UsageError("codex app-server returned an error")
    account = responses[2].get("result", {}).get("account")
    rates = responses[3].get("result", {}).get("rateLimitsByLimitId")
    if type(account) is not dict or type(rates) is not dict:
        raise UsageError("codex app-server schema unsupported")
    email = account.get("email")
    if type(email) is not str or not email:
        raise UsageError("codex app-server account identity missing")
    account_evidence = hashlib.sha256(
        ("engineering-agent-rules/codex-rpc-account/v1\0" + email.casefold()).encode("utf-8")
    ).hexdigest()
    return {"rates": rates, "account_evidence": account_evidence}


def _iso_epoch(value: Any) -> str | None:
    if type(value) not in (int, float) or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    except (OSError, OverflowError, ValueError):
        return None


def _window(value: Any) -> dict[str, Any] | None:
    if type(value) is not dict:
        return None
    used = value.get("usedPercent")
    duration = value.get("windowDurationMins")
    if type(used) not in (int, float) or not math_is_finite(used) or not 0 <= used <= 100:
        return None
    result: dict[str, Any] = {
        "used_percent": round(float(used), 3),
        "remaining_percent": round(100.0 - float(used), 3),
    }
    reset = _iso_epoch(value.get("resetsAt"))
    if reset is not None:
        result["reset_at"] = reset
    if type(duration) is int and duration > 0:
        result["window_minutes"] = duration
    return result


def math_is_finite(value: int | float) -> bool:
    return value == value and value not in {float("inf"), float("-inf")}


def _pool_class(name: str | None, *, main: bool) -> str:
    if main:
        return "main"
    tokens = set(re.findall(r"[a-z0-9]+", (name or "").casefold()))
    return "spark" if "spark" in tokens else "additional"


def _normalise(rates: dict[str, Any], *, now: datetime) -> dict[str, Any]:
    pools: list[dict[str, Any]] = []
    for raw_id, raw in rates.items():
        if type(raw_id) is not str or type(raw) is not dict:
            continue
        main = raw_id == "codex"
        display_name = raw.get("limitName")
        if display_name is not None and type(display_name) is not str:
            display_name = None
        pool_class = _pool_class(display_name, main=main)
        window = _window(raw.get("primary"))
        if window is None:
            continue
        reached = raw.get("rateLimitReachedType") is not None
        spend_reached = raw.get("spendControlReached") is True
        exhausted = reached or spend_reached or window["used_percent"] >= 100.0
        if pool_class == "main":
            pool_id = "codex_main"
        elif pool_class == "spark":
            pool_id = "codex_spark"
        else:
            digest = hashlib.sha256(raw_id.encode("utf-8")).hexdigest()[:10]
            pool_id = f"codex_additional_{digest}"
        pool: dict[str, Any] = {
            "pool_id": pool_id,
            "pool_class": pool_class,
            "exhausted": exhausted,
            "window": window,
        }
        if display_name:
            pool["display_name"] = display_name
        pools.append(pool)
    if not any(pool["pool_class"] == "main" for pool in pools):
        raise UsageError("Codex main quota pool missing")
    pools.sort(key=lambda pool: (pool["pool_class"] != "main", pool["pool_id"]))
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "ok",
        "fetched_at": now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "pools": pools,
    }


def _result_valid(value: Any) -> bool:
    if (
        type(value) is not dict
        or value.get("schema_version") != SCHEMA_VERSION
        or value.get("status") != "ok"
        or type(value.get("fetched_at")) is not str
        or type(value.get("pools")) is not list
    ):
        return False
    return all(
        type(pool) is dict
        and type(pool.get("pool_id")) is str
        and pool.get("pool_class") in {"main", "spark", "additional"}
        and type(pool.get("exhausted")) is bool
        and type(pool.get("window")) is dict
        for pool in value["pools"]
    )


def _envelope_valid(
    value: Any, fingerprint: str, generation: int,
) -> dict[str, Any] | None:
    if (
        type(value) is not dict
        or set(value) != {
            "cache_schema_version", "generation", "account_fingerprint", "result",
        }
        or value.get("cache_schema_version") != CACHE_SCHEMA_VERSION
        or value.get("generation") != generation
        or value.get("account_fingerprint") != fingerprint
        or not _result_valid(value.get("result"))
    ):
        return None
    return value["result"]


def _age(value: dict[str, Any], *, now: datetime) -> float | None:
    try:
        fetched = datetime.fromisoformat(value["fetched_at"].replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError):
        return None
    age = (now - fetched.astimezone(timezone.utc)).total_seconds()
    return age if age >= 0 else None


def refresh(
    *, cache_path: Path | None = None, auth_path: Path | None = None,
    now: datetime | None = None, executable: str = "codex",
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    rpc: Callable[..., dict[str, Any]] = _rpc,
) -> dict[str, Any]:
    selected = cache_path or _cache_path()
    refresh_generation = cache_store.capture_generation(selected)
    fingerprint, local_evidence, credential_status = _local_account_identity(auth_path)
    if fingerprint is None or local_evidence is None:
        raise UsageError(credential_status)
    sampled = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    rpc_result = rpc(executable=executable, timeout_seconds=timeout_seconds)
    if (
        type(rpc_result) is not dict
        or type(rpc_result.get("rates")) is not dict
        or rpc_result.get("account_evidence") != local_evidence
    ):
        raise UsageError("Codex account changed during refresh")
    result = _normalise(rpc_result["rates"], now=sampled)
    descriptor: int | None = None
    try:
        descriptor = cache_store.lock(selected)
        if cache_store.current_generation_locked(selected) != refresh_generation:
            raise UsageError("Codex quota cache invalidated during refresh")
        current_fingerprint, current_evidence, current_status = _local_account_identity(auth_path)
        if (
            current_status != "ok"
            or current_fingerprint != fingerprint
            or current_evidence != local_evidence
        ):
            raise UsageError("Codex account changed during refresh")
        cache_store.write(
            selected,
            {
                "cache_schema_version": CACHE_SCHEMA_VERSION,
                "generation": refresh_generation,
                "account_fingerprint": fingerprint,
                "result": result,
            },
        )
    finally:
        cache_store.unlock(descriptor)
    return {**result, "cache": {"state": "refreshed", "fresh": True, "age_seconds": 0.0}}


def get_usage(
    *, cache_path: Path | None = None, auth_path: Path | None = None,
    now: datetime | None = None, ttl_seconds: float = DEFAULT_TTL_SECONDS,
    launch_refresh: bool = True,
) -> dict[str, Any]:
    selected = cache_path or _cache_path()
    sampled = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    fingerprint, credential_status = _local_account_fingerprint(auth_path)
    if fingerprint is None:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": credential_status,
            "pools": [],
            "cache": {"state": "unavailable", "fresh": False, "refresh_started": False},
        }
    descriptor: int | None = None
    value: dict[str, Any] | None = None
    try:
        descriptor = cache_store.lock(selected)
        generation = cache_store.current_generation_locked(selected)
        value = _envelope_valid(
            cache_store.read(selected), fingerprint, generation,
        )
    except cache_store.CacheUnavailable:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "unknown",
            "pools": [],
            "cache": {"state": "unavailable", "fresh": False, "refresh_started": False},
        }
    finally:
        cache_store.unlock(descriptor)
    age = _age(value, now=sampled) if value is not None else None
    fresh = age is not None and age <= max(0.0, ttl_seconds)
    refresh_started = False
    if not fresh and launch_refresh:
        if cache_store.mark_refresh_due(
            selected,
            minimum_interval_seconds=REFRESH_RETRY_SECONDS,
            reservation_key=fingerprint,
        ):
            refresh_started = cache_store.spawn_refresh(Path(__file__), "_refresh-worker")
    if value is not None:
        return {
            **value,
            "cache": {
                "state": "fresh" if fresh else "stale",
                "fresh": fresh,
                "age_seconds": round(age, 1) if age is not None else None,
                "refresh_started": refresh_started,
            },
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "warming",
        "pools": [],
        "cache": {"state": "warming", "fresh": False, "refresh_started": refresh_started},
    }


def invalidate(*, cache_path: Path | None = None) -> dict[str, Any]:
    return {"cache_schema_version": CACHE_SCHEMA_VERSION, **cache_store.invalidate(cache_path or _cache_path())}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="读取异步刷新的 Codex 额度池快照")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("get")
    refresh_parser = commands.add_parser("refresh")
    refresh_parser.add_argument("--wait", action="store_true", required=True)
    commands.add_parser("invalidate")
    commands.add_parser("_refresh-worker", help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    command = arguments.command or "get"
    if command == "get":
        result = get_usage()
        code = 0
    elif command == "invalidate":
        result = invalidate()
        code = 0 if result["state"] == "invalidated" else 1
    else:
        try:
            result = refresh()
            code = 0
        except (UsageError, cache_store.CacheUnavailable, KeyError, TypeError, ValueError):
            result = {"schema_version": SCHEMA_VERSION, "status": "refresh_failed"}
            code = 1
        if command == "_refresh-worker":
            return code
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
