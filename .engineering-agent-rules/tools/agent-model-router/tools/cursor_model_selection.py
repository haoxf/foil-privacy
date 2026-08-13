#!/usr/bin/env python3
"""Read Cursor's local user-enabled Agent model families without exposing other state."""

from __future__ import annotations

import json
from pathlib import Path
import re
import sqlite3
from typing import Any
from urllib.parse import quote


APPLICATION_USER_KEY = (
    "src.vs.platform.reactivestorage.browser.reactiveStorageServiceImpl."
    "persistentStorage.applicationUser"
)
MAX_STATE_BYTES = 2 * 1024 * 1024
FAMILY_PATTERN = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}")


def default_database_path() -> Path:
    return (
        Path.home()
        / "Library/Application Support/Cursor/User/globalStorage/state.vscdb"
    )


def _unavailable(reason: str) -> dict[str, Any]:
    return {
        "status": "unavailable",
        "source": "cursor_local_settings",
        "reason": reason,
        "enabled_families": [],
        "known_family_count": 0,
    }


def read_enabled_model_families(
    database_path: Path | None = None,
) -> dict[str, Any]:
    """Return only the model allowlist derived from Cursor's local settings.

    Cursor stores unrelated account and UI data in the same JSON value.  This
    function deliberately emits only validated model-family identifiers and
    aggregate counts.
    """

    path = (database_path or default_database_path()).expanduser()
    try:
        if not path.is_file() or path.is_symlink():
            return _unavailable("database_missing")
        uri = "file:" + quote(str(path.resolve()), safe="/") + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=1.0)
        try:
            connection.execute("PRAGMA query_only = ON")
            row = connection.execute(
                "SELECT value FROM ItemTable WHERE key = ? LIMIT 1",
                (APPLICATION_USER_KEY,),
            ).fetchone()
        finally:
            connection.close()
    except (OSError, sqlite3.Error):
        return _unavailable("database_unreadable")
    if row is None or type(row[0]) is not str:
        return _unavailable("settings_missing")
    if len(row[0].encode("utf-8")) > MAX_STATE_BYTES:
        return _unavailable("settings_too_large")
    try:
        root = json.loads(row[0])
    except json.JSONDecodeError:
        return _unavailable("settings_invalid")
    if type(root) is not dict:
        return _unavailable("settings_invalid")

    definitions = root.get("availableDefaultModels2")
    settings = root.get("aiSettings")
    if type(definitions) is not list or type(settings) is not dict:
        return _unavailable("model_settings_missing")

    def valid_family(value: object) -> bool:
        return type(value) is str and FAMILY_PATTERN.fullmatch(value) is not None

    enabled_overrides = {
        value for value in settings.get("modelOverrideEnabled", [])
        if valid_family(value)
    } if type(settings.get("modelOverrideEnabled", [])) is list else set()
    disabled_overrides = {
        value for value in settings.get("modelOverrideDisabled", [])
        if valid_family(value)
    } if type(settings.get("modelOverrideDisabled", [])) is list else set()

    known: set[str] = set()
    enabled: set[str] = set()
    for definition in definitions:
        if type(definition) is not dict:
            continue
        family = definition.get("serverModelName")
        if not valid_family(family) or definition.get("supportsAgent") is not True:
            continue
        known.add(family)
        if (
            (definition.get("defaultOn") is True or family in enabled_overrides)
            and family not in disabled_overrides
        ):
            enabled.add(family)

    if not known:
        return _unavailable("model_definitions_empty")
    return {
        "status": "ok",
        "source": "cursor_local_settings",
        "enabled_families": sorted(enabled),
        "known_families": sorted(known),
        "known_family_count": len(known),
    }


def family_for_cli_model(model_id: str, known_families: set[str]) -> str | None:
    """Map a concrete CLI variant to the longest matching UI model family."""

    if model_id == "auto":
        return "default" if "default" in known_families else None
    comparable = model_id.removeprefix("cursor-")
    matches = [
        family for family in known_families
        if comparable == family or comparable.startswith(family + "-")
    ]
    return max(matches, key=len) if matches else None


def filter_enabled_cli_models(
    models: list[dict[str, str]], selection: dict[str, Any],
) -> list[dict[str, str]]:
    """Fail closed unless a CLI model maps to an enabled local UI family."""

    if selection.get("status") != "ok":
        return []
    raw_enabled = selection.get("enabled_families")
    raw_known = selection.get("known_families")
    if type(raw_enabled) is not list or type(raw_known) is not list:
        return []
    enabled = {
        value for value in raw_enabled
        if type(value) is str and FAMILY_PATTERN.fullmatch(value) is not None
    }
    known = {
        value for value in raw_known
        if type(value) is str and FAMILY_PATTERN.fullmatch(value) is not None
    }
    result: list[dict[str, str]] = []
    for model in models:
        model_id = model.get("model_id")
        if type(model_id) is not str:
            continue
        family = family_for_cli_model(model_id, known)
        if family is not None and family in enabled:
            result.append({**model, "cursor_family": family})
    return result
