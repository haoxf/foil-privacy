#!/usr/bin/env python3
"""Single cache-invalidation entry point for router quota pools."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import codex_usage
import cursor_usage


def invalidate_pool(
    pool_id: str, *, cursor_cache_path: Path | None = None,
    codex_cache_path: Path | None = None,
) -> dict[str, Any]:
    if pool_id in {"cursor_first_party", "cursor_api"}:
        result = cursor_usage.invalidate_cache(cache_path=cursor_cache_path)
    elif pool_id in {"codex_main", "codex_spark"} or pool_id.startswith(
        "codex_additional_"
    ):
        result = codex_usage.invalidate(cache_path=codex_cache_path)
    else:
        return {"state": "invalid_pool", "pool_id": pool_id, "removed": False}
    return {**result, "pool_id": pool_id}
