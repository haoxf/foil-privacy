#!/usr/bin/env python3
"""Authoritative Cursor quota probe for the unified model router."""

from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import sys
from typing import Any, Callable
from urllib.request import HTTPRedirectHandler, Request, build_opener

import cache_store


SCHEMA_VERSION = 1
CACHE_SCHEMA_VERSION = 2
ACCESS_TOKEN_KEY = "cursorAuth/accessToken"
USAGE_ENDPOINT = "https://cursor.com/api/usage-summary"
DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_CACHE_TTL_SECONDS = 30 * 60
DEFAULT_NEGATIVE_CACHE_TTL_SECONDS = 2 * 60
MAX_RESPONSE_BYTES = 1_000_000
ROUTING_PACE_GAP = 10.0
CACHE_STATUS_VALUES = {"ok", "unknown", "signed_out", "unsupported"}
CACHE_HINT_VALUES = {"prefer_auto", "prefer_api", "balanced", "unknown"}


class _ProbeFailure(Exception):
    def __init__(self, status: str) -> None:
        super().__init__(status)
        self.status = status


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def _open_fixed(request: Request, *, timeout: float) -> Any:
    return build_opener(_NoRedirect).open(request, timeout=timeout)


def _state_db() -> Path:
    return (
        Path.home()
        / "Library/Application Support/Cursor/User/globalStorage/state.vscdb"
    )


def _cache_path() -> Path:
    return (
        Path.home()
        / "Library/Caches/engineering-agent-rules/cursor-usage-v1.json"
    )


def _read_access_token(path: Path) -> str:
    if not path.is_file():
        raise _ProbeFailure("unsupported")
    try:
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            row = connection.execute(
                "SELECT value FROM ItemTable WHERE key = ? LIMIT 1",
                (ACCESS_TOKEN_KEY,),
            ).fetchone()
        finally:
            connection.close()
    except (OSError, sqlite3.Error):
        raise _ProbeFailure("unknown") from None
    if row is None or not isinstance(row[0], str) or not row[0].strip():
        raise _ProbeFailure("signed_out")
    stored = row[0].strip()
    try:
        decoded = json.loads(stored)
    except json.JSONDecodeError:
        token = stored
    else:
        token = decoded if isinstance(decoded, str) else ""
    if not token or "\r" in token or "\n" in token:
        raise _ProbeFailure("signed_out")
    return token


def _session_cookie_and_fingerprint(access_token: str) -> tuple[str, str]:
    parts = access_token.split(".")
    if len(parts) < 2 or not parts[1]:
        raise _ProbeFailure("unknown")
    encoded = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        decoded = base64.b64decode(encoded, altchars=b"-_", validate=True)
        payload = json.loads(decoded.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        raise _ProbeFailure("unknown") from None
    subject = payload.get("sub") if type(payload) is dict else None
    if not isinstance(subject, str) or not subject:
        raise _ProbeFailure("unknown")
    user_id = subject.split("|", 1)[-1]
    if not user_id or any(character in user_id for character in "\r\n;"):
        raise _ProbeFailure("unknown")
    fingerprint = hashlib.sha256(subject.encode("utf-8")).hexdigest()
    return f"{user_id}::{access_token}", fingerprint


def _session_cookie_value(access_token: str) -> str:
    return _session_cookie_and_fingerprint(access_token)[0]


def _fetch_usage(
    cookie_value: str, *, timeout_seconds: float,
    opener: Callable[..., Any],
) -> Any:
    request = Request(
        USAGE_ENDPOINT,
        headers={
            "Accept": "application/json",
            "Cookie": f"WorkosCursorSessionToken={cookie_value}",
            "User-Agent": "engineering-agent-rules-cursor-usage/2",
        },
        method="GET",
    )
    try:
        with opener(request, timeout=timeout_seconds) as response:
            payload = response.read(MAX_RESPONSE_BYTES + 1)
    except Exception:
        raise _ProbeFailure("unknown") from None
    if len(payload) > MAX_RESPONSE_BYTES:
        raise _ProbeFailure("unknown")
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise _ProbeFailure("unknown") from None


def _first(container: dict[str, Any], names: tuple[str, ...]) -> Any:
    return next(
        (container[name] for name in names if container.get(name) is not None),
        None,
    )


def _date(value: Any) -> datetime:
    if isinstance(value, bool):
        raise _ProbeFailure("unknown")
    if isinstance(value, (int, float)) and math.isfinite(value):
        seconds = value / 1000 if value >= 1_000_000_000_000 else value
        try:
            return datetime.fromtimestamp(seconds, timezone.utc)
        except (OSError, OverflowError, ValueError):
            raise _ProbeFailure("unknown") from None
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise _ProbeFailure("unknown") from None
        return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).astimezone(timezone.utc)
    raise _ProbeFailure("unknown")


def _percent(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _ProbeFailure("unknown")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise _ProbeFailure("unknown")
    return result


def _usage_plan(body: dict[str, Any]) -> dict[str, Any]:
    individual = body.get("individualUsage")
    candidates = (
        body.get("plan"),
        individual.get("plan") if type(individual) is dict else None,
        individual,
        body,
    )
    auto_names = ("autoPercentUsed", "autoPercentageUsed", "autoUsedPercent")
    api_names = ("apiPercentUsed", "apiPercentageUsed", "apiUsedPercent")
    for candidate in candidates:
        if (
            type(candidate) is dict
            and _first(candidate, auto_names) is not None
            and _first(candidate, api_names) is not None
        ):
            return candidate
    raise _ProbeFailure("unknown")


def _normalise(body: Any, *, now: datetime) -> dict[str, Any]:
    if type(body) is not dict:
        raise _ProbeFailure("unknown")
    start = _date(_first(body, (
        "billingCycleStart", "currentPeriodStart", "startOfMonth",
    )))
    end = _date(_first(body, (
        "billingCycleEnd", "currentPeriodEnd", "endOfMonth",
    )))
    if end <= start:
        raise _ProbeFailure("unknown")
    plan = _usage_plan(body)
    auto_used = _percent(_first(plan, (
        "autoPercentUsed", "autoPercentageUsed", "autoUsedPercent",
    )))
    api_used = _percent(_first(plan, (
        "apiPercentUsed", "apiPercentageUsed", "apiUsedPercent",
    )))
    now_utc = now.astimezone(timezone.utc)
    elapsed = min(max((now_utc - start).total_seconds(), 0.0), (end - start).total_seconds())
    elapsed_percent = elapsed / (end - start).total_seconds() * 100
    auto_delta = auto_used - elapsed_percent
    api_delta = api_used - elapsed_percent
    if auto_used >= 100 > api_used:
        hint = "prefer_api"
    elif api_used >= 100 > auto_used:
        hint = "prefer_auto"
    elif api_delta - auto_delta >= ROUTING_PACE_GAP:
        hint = "prefer_auto"
    elif auto_delta - api_delta >= ROUTING_PACE_GAP:
        hint = "prefer_api"
    else:
        hint = "balanced"
    def iso(value: datetime) -> str:
        return value.isoformat(timespec="seconds").replace("+00:00", "Z")
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "ok",
        "fetched_at": iso(now_utc),
        "cycle": {
            "start": iso(start), "end": iso(end),
            "elapsed_percent": round(elapsed_percent, 1),
        },
        "auto": {
            "used_percent": auto_used,
            "pace_delta": round(auto_delta, 1),
            "exhausted": auto_used >= 100,
        },
        "api": {
            "used_percent": api_used,
            "pace_delta": round(api_delta, 1),
            "exhausted": api_used >= 100,
        },
        "routing_hint": hint,
    }


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")


def _minimal(status: str, *, now: datetime) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "fetched_at": _iso(now),
        "routing_hint": "unknown",
    }


def _credential(
    *, state_db: Path | None, platform: str | None,
) -> tuple[str, str]:
    if state_db is None and (platform or sys.platform) != "darwin":
        raise _ProbeFailure("unsupported")
    access_token = _read_access_token(state_db or _state_db())
    try:
        return _session_cookie_and_fingerprint(access_token)
    finally:
        access_token = ""


def _credential_state(
    *, state_db: Path | None, platform: str | None,
) -> tuple[str | None, str, str]:
    try:
        cookie_value, account_fingerprint = _credential(
            state_db=state_db, platform=platform
        )
        return cookie_value, account_fingerprint, "ok"
    except _ProbeFailure as failure:
        status = failure.status if failure.status in CACHE_STATUS_VALUES else "unknown"
        return None, f"credential:{status}", status


def _cached_result(value: Any) -> dict[str, Any] | None:
    if type(value) is not dict:
        return None
    status = value.get("status")
    hint = value.get("routing_hint")
    if (
        value.get("schema_version") != SCHEMA_VERSION
        or status not in CACHE_STATUS_VALUES
        or hint not in CACHE_HINT_VALUES
        or not isinstance(value.get("fetched_at"), str)
    ):
        return None
    try:
        _date(value["fetched_at"])
    except _ProbeFailure:
        return None
    if status != "ok":
        if set(value) != {
            "schema_version", "status", "fetched_at", "routing_hint",
        } or hint != "unknown":
            return None
        return {
            "schema_version": SCHEMA_VERSION,
            "status": status,
            "fetched_at": value["fetched_at"],
            "routing_hint": "unknown",
        }
    if set(value) != {
        "schema_version", "status", "fetched_at", "cycle", "auto", "api",
        "routing_hint",
    } or hint == "unknown":
        return None
    cycle = value.get("cycle")
    auto = value.get("auto")
    api = value.get("api")
    if (
        type(cycle) is not dict
        or set(cycle) != {"start", "end", "elapsed_percent"}
        or type(auto) is not dict
        or set(auto) != {"used_percent", "pace_delta", "exhausted"}
        or type(api) is not dict
        or set(api) != {"used_percent", "pace_delta", "exhausted"}
    ):
        return None
    try:
        start = _date(cycle["start"])
        end = _date(cycle["end"])
    except (KeyError, _ProbeFailure):
        return None
    if end <= start:
        return None
    for numeric in (
        cycle.get("elapsed_percent"), auto.get("used_percent"),
        auto.get("pace_delta"), api.get("used_percent"), api.get("pace_delta"),
    ):
        if (
            isinstance(numeric, bool)
            or not isinstance(numeric, (int, float))
            or not math.isfinite(float(numeric))
        ):
            return None
    if type(auto.get("exhausted")) is not bool or type(api.get("exhausted")) is not bool:
        return None
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "ok",
        "fetched_at": value["fetched_at"],
        "cycle": {
            "start": cycle["start"],
            "end": cycle["end"],
            "elapsed_percent": cycle["elapsed_percent"],
        },
        "auto": {
            "used_percent": auto["used_percent"],
            "pace_delta": auto["pace_delta"],
            "exhausted": auto["exhausted"],
        },
        "api": {
            "used_percent": api["used_percent"],
            "pace_delta": api["pace_delta"],
            "exhausted": api["exhausted"],
        },
        "routing_hint": hint,
    }


def _cache_envelope(
    value: Any, *, account_fingerprint: str, generation: int,
) -> dict[str, dict[str, Any] | None] | None:
    base_fields = {
        "cache_schema_version", "generation", "account_fingerprint", "result",
    }
    if (
        type(value) is not dict
        or set(value) not in (base_fields, base_fields | {"refresh_failure"})
        or value.get("cache_schema_version") != CACHE_SCHEMA_VERSION
        or value.get("generation") != generation
        or value.get("account_fingerprint") != account_fingerprint
    ):
        return None
    result = _cached_result(value.get("result"))
    if result is None:
        return None
    raw_failure = value.get("refresh_failure")
    refresh_failure = (
        _cached_result(raw_failure) if raw_failure is not None else None
    )
    if raw_failure is not None and (
        refresh_failure is None or refresh_failure["status"] == "ok"
    ):
        return None
    return {"result": result, "refresh_failure": refresh_failure}


def _cache_age(result: dict[str, Any], *, now: datetime) -> float | None:
    try:
        fetched = _date(result["fetched_at"])
    except (KeyError, _ProbeFailure):
        return None
    age = (now.astimezone(timezone.utc) - fetched).total_seconds()
    if age < 0:
        return None
    return age


def _cache_metadata(
    state: str, *, age_seconds: float | None, fresh: bool,
    refresh_status: str | None = None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {"state": state, "fresh": fresh}
    if age_seconds is not None:
        metadata["age_seconds"] = round(age_seconds, 1)
    if refresh_status is not None:
        metadata["refresh_status"] = refresh_status
    return metadata


def _with_cache(
    result: dict[str, Any], state: str, *, age_seconds: float | None,
    fresh: bool, refresh_status: str | None = None,
) -> dict[str, Any]:
    return {
        **result,
        "cache": _cache_metadata(
            state,
            age_seconds=age_seconds,
            fresh=fresh,
            refresh_status=refresh_status,
        ),
    }


def _lock_cache(cache_path: Path) -> int:
    try:
        return cache_store.lock(cache_path)
    except cache_store.CacheUnavailable as error:
        raise _ProbeFailure("cache_unavailable") from error


def _read_cache(
    cache_path: Path, *, account_fingerprint: str, generation: int,
) -> dict[str, dict[str, Any] | None] | None:
    try:
        envelope = cache_store.read(cache_path, max_bytes=MAX_RESPONSE_BYTES)
    except cache_store.CacheUnavailable as error:
        raise _ProbeFailure("cache_unavailable") from error
    return _cache_envelope(
        envelope,
        account_fingerprint=account_fingerprint,
        generation=generation,
    )


def _write_cache(
    cache_path: Path, *, account_fingerprint: str, result: dict[str, Any],
    generation: int, refresh_failure: dict[str, Any] | None = None,
) -> None:
    envelope = {
        "cache_schema_version": CACHE_SCHEMA_VERSION,
        "generation": generation,
        "account_fingerprint": account_fingerprint,
        "result": result,
        "refresh_failure": refresh_failure,
    }
    try:
        cache_store.write(cache_path, envelope, max_bytes=MAX_RESPONSE_BYTES)
    except cache_store.CacheUnavailable as error:
        raise _ProbeFailure("cache_unavailable") from error


def invalidate_cache(*, cache_path: Path | None = None) -> dict[str, Any]:
    """Safely invalidate the shared cache without reading Cursor credentials."""
    selected_cache_path = cache_path or _cache_path()
    result = cache_store.invalidate(selected_cache_path)
    return {"cache_schema_version": CACHE_SCHEMA_VERSION, **result}


def cached_usage(
    *, state_db: Path | None = None, cache_path: Path | None = None,
    now: datetime | None = None, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    cache_ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
    negative_cache_ttl_seconds: float = DEFAULT_NEGATIVE_CACHE_TTL_SECONDS,
    force_refresh: bool = False,
    opener: Callable[..., Any] = _open_fixed, platform: str | None = None,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    def sampled_time() -> datetime:
        value = now or (
            clock() if clock is not None else datetime.now(timezone.utc)
        )
        return value.astimezone(timezone.utc)

    fetched = sampled_time()
    selected_cache_path = cache_path or _cache_path()
    cookie_value: str | None = None
    descriptor: int | None = None
    live: dict[str, Any] | None = None
    try:
        descriptor = _lock_cache(selected_cache_path)
    except _ProbeFailure:
        cookie_value, _, credential_status = _credential_state(
            state_db=state_db, platform=platform
        )
        live = _refresh_usage(
            cookie_value=cookie_value,
            credential_status=credential_status,
            now=fetched,
            timeout_seconds=timeout_seconds,
            opener=opener,
        )
        cookie_value = None
        return _with_cache(
            live,
            "unavailable",
            age_seconds=0.0 if live["status"] == "ok" else None,
            fresh=live["status"] == "ok",
        )

    if now is None:
        fetched = sampled_time()
    generation = cache_store.current_generation_locked(selected_cache_path)
    cookie_value, account_fingerprint, credential_status = _credential_state(
        state_db=state_db, platform=platform
    )
    try:
        cache_record = _read_cache(
            selected_cache_path,
            account_fingerprint=account_fingerprint,
            generation=generation,
        )
    except _ProbeFailure:
        live = _refresh_usage(
            cookie_value=cookie_value,
            credential_status=credential_status,
            now=fetched,
            timeout_seconds=timeout_seconds,
            opener=opener,
        )
        cookie_value = None
        cache_store.unlock(descriptor)
        return _with_cache(
            live,
            "unavailable",
            age_seconds=0.0 if live["status"] == "ok" else None,
            fresh=live["status"] == "ok",
        )

    try:
        cached = cache_record["result"] if cache_record is not None else None
        refresh_failure = (
            cache_record["refresh_failure"] if cache_record is not None else None
        )
        cached_age = _cache_age(cached, now=fetched) if cached is not None else None
        if cached is not None and cached_age is not None and not force_refresh:
            ttl = (
                max(0.0, cache_ttl_seconds)
                if cached["status"] == "ok"
                else max(0.0, negative_cache_ttl_seconds)
            )
            if cached_age <= ttl:
                return _with_cache(
                    cached, "fresh", age_seconds=cached_age, fresh=True
                )
        failure_age = (
            _cache_age(refresh_failure, now=fetched)
            if refresh_failure is not None else None
        )
        if (
            not force_refresh
            and cached is not None
            and cached["status"] == "ok"
            and cached_age is not None
            and refresh_failure is not None
            and failure_age is not None
            and failure_age <= max(0.0, negative_cache_ttl_seconds)
        ):
            return _with_cache(
                cached,
                "stale",
                age_seconds=cached_age,
                fresh=False,
                refresh_status=refresh_failure["status"],
            )

        live = _refresh_usage(
            cookie_value=cookie_value,
            credential_status=credential_status,
            now=fetched,
            timeout_seconds=timeout_seconds,
            opener=opener,
        )
        if live["status"] == "ok":
            _write_cache(
                selected_cache_path,
                account_fingerprint=account_fingerprint,
                result=live,
                generation=generation,
            )
            return _with_cache(
                live, "refreshed", age_seconds=0.0, fresh=True
            )
        if cached is not None and cached["status"] == "ok" and cached_age is not None:
            _write_cache(
                selected_cache_path,
                account_fingerprint=account_fingerprint,
                result=cached,
                generation=generation,
                refresh_failure=live,
            )
            return _with_cache(
                cached,
                "stale",
                age_seconds=cached_age,
                fresh=False,
                refresh_status=live["status"],
            )
        _write_cache(
            selected_cache_path,
            account_fingerprint=account_fingerprint,
            result=live,
            generation=generation,
        )
        return _with_cache(
            live, "refreshed", age_seconds=0.0, fresh=True
        )
    except _ProbeFailure:
        if live is None:
            live = _refresh_usage(
                cookie_value=cookie_value,
                credential_status=credential_status,
                now=fetched,
                timeout_seconds=timeout_seconds,
                opener=opener,
            )
        return _with_cache(
            live,
            "unavailable",
            age_seconds=0.0 if live["status"] == "ok" else None,
            fresh=live["status"] == "ok",
        )
    finally:
        cookie_value = None
        cache_store.unlock(descriptor)


def _refresh_usage(
    *, cookie_value: str | None, credential_status: str, now: datetime,
    timeout_seconds: float, opener: Callable[..., Any],
) -> dict[str, Any]:
    if cookie_value is None:
        return _minimal(credential_status, now=now)
    try:
        body = _fetch_usage(
            cookie_value,
            timeout_seconds=max(1.0, min(timeout_seconds, 30.0)),
            opener=opener,
        )
        return _normalise(body, now=now)
    except _ProbeFailure as failure:
        status = failure.status if failure.status in CACHE_STATUS_VALUES else "unknown"
        return _minimal(status, now=now)
    except Exception:
        return _minimal("unknown", now=now)


def probe(
    *, state_db: Path | None = None, now: datetime | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    opener: Callable[..., Any] = _open_fixed, platform: str | None = None,
) -> dict[str, Any]:
    fetched = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    try:
        cookie_value, _ = _credential(state_db=state_db, platform=platform)
        try:
            body = _fetch_usage(
                cookie_value,
                timeout_seconds=max(1.0, min(timeout_seconds, 30.0)),
                opener=opener,
            )
        finally:
            cookie_value = ""
        return _normalise(body, now=fetched)
    except _ProbeFailure as failure:
        status = failure.status if failure.status in CACHE_STATUS_VALUES else "unknown"
        return _minimal(status, now=fetched)
    except Exception:
        return _minimal("unknown", now=fetched)


def _reserve_async_refresh(cache_path: Path, *, reservation_key: str) -> bool:
    return cache_store.mark_refresh_due(
        cache_path,
        minimum_interval_seconds=DEFAULT_NEGATIVE_CACHE_TTL_SECONDS,
        reservation_key=reservation_key,
    )


def _spawn_async_refresh() -> bool:
    return cache_store.spawn_refresh(Path(__file__), "--refresh-worker")


def refresh_success_only(
    *, state_db: Path | None = None, cache_path: Path | None = None,
    now: datetime | None = None, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    opener: Callable[..., Any] = _open_fixed, platform: str | None = None,
) -> dict[str, Any]:
    """Refresh atomically; a failed fetch never mutates last-known-good data."""
    sampled = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    selected = cache_path or _cache_path()
    try:
        refresh_generation = cache_store.capture_generation(selected)
    except cache_store.CacheUnavailable:
        return _with_cache(
            _minimal("unknown", now=sampled),
            "unavailable",
            age_seconds=None,
            fresh=False,
        )
    cookie_value, fingerprint, credential_status = _credential_state(
        state_db=state_db, platform=platform
    )
    live = _refresh_usage(
        cookie_value=cookie_value,
        credential_status=credential_status,
        now=sampled,
        timeout_seconds=timeout_seconds,
        opener=opener,
    )
    cookie_value = None
    if live["status"] != "ok":
        return _with_cache(live, "refresh_failed", age_seconds=None, fresh=False)
    descriptor: int | None = None
    try:
        descriptor = _lock_cache(selected)
        if cache_store.current_generation_locked(selected) != refresh_generation:
            return _with_cache(
                _minimal("unknown", now=sampled),
                "refresh_failed",
                age_seconds=None,
                fresh=False,
            )
        _, current_fingerprint, current_status = _credential_state(
            state_db=state_db, platform=platform
        )
        if current_status != "ok" or current_fingerprint != fingerprint:
            return _with_cache(
                _minimal("unknown", now=sampled),
                "refresh_failed",
                age_seconds=None,
                fresh=False,
            )
        _write_cache(
            selected,
            account_fingerprint=fingerprint,
            result=live,
            generation=refresh_generation,
        )
        return _with_cache(live, "refreshed", age_seconds=0.0, fresh=True)
    except _ProbeFailure:
        return _with_cache(live, "unavailable", age_seconds=0.0, fresh=True)
    finally:
        cache_store.unlock(descriptor)


def advisory_usage(
    *, state_db: Path | None = None, cache_path: Path | None = None,
    now: datetime | None = None,
    cache_ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
    platform: str | None = None, launch_refresh: bool = True,
) -> dict[str, Any]:
    """Return local facts immediately and refresh expired data out of band."""
    sampled = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    selected = cache_path or _cache_path()
    cookie_value: str | None = None
    descriptor: int | None = None
    try:
        descriptor = _lock_cache(selected)
        cookie_value, fingerprint, credential_status = _credential_state(
            state_db=state_db, platform=platform
        )
        cookie_value = None
        generation = cache_store.current_generation_locked(selected)
        record = _read_cache(
            selected,
            account_fingerprint=fingerprint,
            generation=generation,
        )
    except _ProbeFailure:
        return _with_cache(
            _minimal("unknown", now=sampled),
            "unavailable",
            age_seconds=None,
            fresh=False,
        )
    finally:
        cache_store.unlock(descriptor)
    cached = record["result"] if record is not None else None
    age = _cache_age(cached, now=sampled) if cached is not None else None
    fresh = age is not None and age <= max(0.0, cache_ttl_seconds)
    refresh_started = False
    if not fresh and launch_refresh and credential_status == "ok":
        if _reserve_async_refresh(selected, reservation_key=fingerprint):
            refresh_started = _spawn_async_refresh()
    if cached is not None:
        result = _with_cache(
            cached,
            "fresh" if fresh else "stale",
            age_seconds=age,
            fresh=fresh,
        )
    else:
        status = credential_status if credential_status != "ok" else "unknown"
        result = _with_cache(
            _minimal(status, now=sampled),
            "warming" if credential_status == "ok" else "unavailable",
            age_seconds=None,
            fresh=False,
        )
    result["cache"]["refresh_started"] = refresh_started
    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="立即读取 Cursor 额度缓存；过期时在后台刷新"
    )
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument(
        "--refresh",
        action="store_true",
        help="显式同步刷新（必须同时传 --wait）；失败不修改 last-known-good",
    )
    actions.add_argument(
        "--invalidate",
        action="store_true",
        help="安全失效共享缓存；供 adapter 在真实 quota/rate-limit 后自动调用",
    )
    actions.add_argument("--refresh-worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--wait", action="store_true", help="等待显式刷新完成")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.refresh and not args.wait:
        print("--refresh requires --wait", file=sys.stderr)
        return 64
    if args.invalidate:
        result = invalidate_cache()
    elif args.refresh or args.refresh_worker:
        result = refresh_success_only()
    else:
        result = advisory_usage()
    if args.refresh_worker:
        return 0 if result["status"] == "ok" else 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
