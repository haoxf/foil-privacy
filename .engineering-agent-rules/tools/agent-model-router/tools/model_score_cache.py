#!/usr/bin/env python3
"""Two-layer authoritative benchmark cache with stale-while-revalidate reads."""

from __future__ import annotations

import argparse
import hashlib
from html.parser import HTMLParser
import json
import math
from datetime import datetime, timezone
from pathlib import Path
import re
import sys
import time
from typing import Any, Callable
from urllib.request import HTTPRedirectHandler, Request, build_opener

import cache_store
from model_identity import normalise_model_name


SCHEMA_VERSION = 1
BUNDLE_CACHE_NAME = "model-benchmark-bundle-v1.json"
DEFAULT_TTL_SECONDS = 24 * 60 * 60
DEFAULT_TIMEOUT_SECONDS = 12.0
REFRESH_RETRY_SECONDS = 2 * 60
MAX_RESPONSE_BYTES = 6 * 1024 * 1024
CURSORBENCH_URL = "https://cursor.com/cursorbench"
MODEL_COMPARISON_URL = "https://cursor.com/grok"
USER_AGENT = "engineering-agent-rules-model-score-cache/1"


class ScoreRefreshError(RuntimeError):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def _open_fixed(request: Request, *, timeout: float) -> Any:
    return build_opener(_NoRedirect()).open(request, timeout=timeout)


class _Tables(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self._table: list[list[str]] | None = None
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, _attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript"}:
            self._ignored_depth += 1
        elif self._ignored_depth:
            return
        elif tag == "table":
            self._table = []
        elif tag == "tr" and self._table is not None:
            self._row = []
        elif tag in {"th", "td"} and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"} and self._ignored_depth:
            self._ignored_depth -= 1
        elif self._ignored_depth:
            return
        elif tag in {"th", "td"} and self._cell is not None:
            assert self._row is not None
            self._row.append(_clean(" ".join(self._cell)))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if any(self._row):
                assert self._table is not None
                self._table.append(self._row)
            self._row = None
        elif tag == "table" and self._table is not None:
            if self._table:
                self.tables.append(self._table)
            self._table = None

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth and self._cell is not None:
            self._cell.append(data)


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _fetch(url: str, *, timeout_seconds: float, opener: Callable[..., Any]) -> str:
    request = Request(
        url,
        headers={"Accept": "text/html", "User-Agent": USER_AGENT},
        method="GET",
    )
    try:
        response = opener(request, timeout=timeout_seconds)
        final_url = response.geturl()
        if final_url != url:
            raise ScoreRefreshError("benchmark source redirected")
        content_type = response.headers.get_content_type()
        if content_type not in {"text/html", "application/xhtml+xml"}:
            raise ScoreRefreshError("benchmark source is not HTML")
        payload = response.read(MAX_RESPONSE_BYTES + 1)
    except ScoreRefreshError:
        raise
    except Exception as error:
        raise ScoreRefreshError("benchmark fetch failed") from error
    if len(payload) > MAX_RESPONSE_BYTES:
        raise ScoreRefreshError("benchmark response too large")
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ScoreRefreshError("benchmark source is not UTF-8") from error


def _number(value: str, *, percent: bool = False, money: bool = False) -> float | None:
    cleaned = value.replace(",", "").strip()
    if cleaned in {"", "-", "—", "n/a", "N/A"}:
        return None
    if percent:
        cleaned = cleaned.removesuffix("%")
    if money:
        cleaned = cleaned.removeprefix("$")
    try:
        result = float(cleaned)
    except ValueError:
        return None
    return result if math.isfinite(result) else None


def _parse_cursorbench(html: str) -> dict[str, Any]:
    parser = _Tables()
    parser.feed(html)
    selected: list[list[str]] | None = None
    model_column = -1
    score_column = -1
    for table in parser.tables:
        header = [cell.casefold() for cell in table[0]]
        if "model" in header and any(cell == "score" or "cursorbench" in cell for cell in header):
            selected = table
            model_column = header.index("model")
            score_column = next(
                index for index, cell in enumerate(header)
                if cell == "score" or "cursorbench" in cell
            )
            break
    if selected is None:
        raise ScoreRefreshError("CursorBench table not found")
    models: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in selected[1:]:
        if len(row) <= max(model_column, score_column):
            continue
        score = _number(row[score_column], percent=True)
        name = _clean(row[model_column])
        if not name or score is None or not 0 <= score <= 100:
            continue
        key = normalise_model_name(name)
        if not key or key in seen:
            continue
        seen.add(key)
        record: dict[str, Any] = {"name": name, "cursorbench": score}
        trailing = row[score_column + 1:]
        if len(trailing) > 0 and (cost := _number(trailing[0], money=True)) is not None:
            record["average_cost_usd"] = cost
        if len(trailing) > 1 and (tokens := _number(trailing[1])) is not None:
            record["average_tokens"] = int(tokens)
        if len(trailing) > 2 and (steps := _number(trailing[2])) is not None:
            record["average_steps"] = int(steps)
        models.append(record)
    if len(models) < 10:
        raise ScoreRefreshError("CursorBench table is unexpectedly small")
    version_match = re.search(r"CursorBench\s+v(\d+(?:\.\d+)*)", html, re.I)
    return {
        "url": CURSORBENCH_URL,
        "benchmark": f"CursorBench v{version_match.group(1)}" if version_match else "CursorBench",
        "models": models,
    }


_DIMENSIONS = {
    "aaintelligenceindex": "aa_intelligence",
    "gdpvalaa": "gdpval_aa",
    "gdpvalaa2": "gdpval_aa",
    "cursorbench": "cursorbench",
    "cursorbench32": "cursorbench",
    "deepswe": "deep_swe",
    "deepswe11": "deep_swe",
    "frontiercode": "frontier_code",
    "frontiercode11extended": "frontier_code",
    "apexagents": "apex_agents",
    "terminalbench": "terminal_bench",
    "terminalbench30": "terminal_bench",
    "apexswe": "apex_swe",
    "aabriefcase": "aa_briefcase",
    "harveylabvals": "harvey_lab",
}


def _normalise_dimension(value: str) -> str | None:
    key = re.sub(r"\bv(?=\d)", "", value.casefold())
    key = re.sub(r"[^a-z0-9]+", "", key)
    for pattern, dimension in (
        (r"cursorbench\d*", "cursorbench"),
        (r"deepswe\d*", "deep_swe"),
        (r"frontiercode\d*extended?", "frontier_code"),
        (r"terminalbench\d*", "terminal_bench"),
        (r"gdpvalaa\d*", "gdpval_aa"),
    ):
        if re.fullmatch(pattern, key):
            return dimension
    return _DIMENSIONS.get(key)


def _parse_comparison(html: str) -> dict[str, Any]:
    parser = _Tables()
    parser.feed(html)
    selected: list[list[str]] | None = None
    for table in parser.tables:
        if len(table) < 5 or len(table[0]) < 4:
            continue
        dimensions = {_normalise_dimension(row[0]) for row in table[1:] if row}
        if {"cursorbench", "deep_swe", "terminal_bench"} <= dimensions:
            selected = table
            break
    if selected is None:
        raise ScoreRefreshError("multi-benchmark comparison table not found")
    names = [_clean(value) for value in selected[0][1:]]
    if not names or any(not name for name in names) or len(names) != len(set(names)):
        raise ScoreRefreshError("comparison model header is incomplete")
    models = {name: {} for name in names}
    for row in selected[1:]:
        if not row or (dimension := _normalise_dimension(row[0])) is None:
            continue
        if len(row) < len(names) + 1:
            raise ScoreRefreshError("comparison row is incomplete")
        values = row[1:len(names) + 1]
        for name, raw in zip(names, values):
            value = _number(raw, percent="%" in raw)
            if value is not None:
                models[name][dimension] = value
    if any(len(dimensions) < 6 for dimensions in models.values()):
        raise ScoreRefreshError("comparison table is incomplete")
    return {"url": MODEL_COMPARISON_URL, "models": models}


_EFFORTS = {
    "none": 0,
    "minimal": 0,
    "low": 1,
    "medium": 2,
    "high": 3,
    "extra high": 4,
    "max": 5,
}


def _name_parts(value: str) -> tuple[str, int]:
    cleaned = re.sub(r"\b(?:cursor|fast|no\s*zdr|1m)\b", " ", value, flags=re.I)
    cleaned = re.sub(r"\bxhigh\b", "extra high", cleaned, flags=re.I)
    effort = 2
    family = cleaned
    for label, rank in sorted(_EFFORTS.items(), key=lambda item: -len(item[0])):
        pattern = rf"\b{re.escape(label)}\b"
        if re.search(pattern, cleaned, re.I):
            effort = rank
            family = re.sub(pattern, " ", cleaned, flags=re.I)
            break
    return normalise_model_name(family), effort


def _weighted(dimensions: dict[str, float], weights: dict[str, float]) -> float | None:
    if not all(key in dimensions for key in weights):
        return None
    return round(sum(dimensions[key] * weight for key, weight in weights.items()), 2)


def _source_digest(source: dict[str, Any]) -> str:
    payload = json.dumps(source, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def derive_scorecard(source: dict[str, Any]) -> dict[str, Any]:
    cursorbench = source["sources"]["cursorbench"]["models"]
    comparison = source["sources"]["model_comparison"]["models"]
    comparison_entries: list[tuple[str, str, int, dict[str, float]]] = []
    for name, dimensions in comparison.items():
        family, effort = _name_parts(name)
        comparison_entries.append((name, family, effort, dimensions))
    models: list[dict[str, Any]] = []
    for raw in cursorbench:
        name = raw["name"]
        family, effort = _name_parts(name)
        exact = comparison.get(name)
        evidence_name: str | None = name if exact is not None else None
        dimensions = dict(exact or {})
        if not dimensions:
            compatible = [
                entry for entry in comparison_entries
                if entry[1] == family and entry[2] <= effort
            ]
            if compatible:
                evidence_name, _, _, inherited = max(compatible, key=lambda entry: entry[2])
                dimensions = dict(inherited)
        dimensions["cursorbench"] = float(raw["cursorbench"])
        tasks: dict[str, float | None] = {
            "exploration": dimensions["cursorbench"],
            "micro_edit": dimensions["cursorbench"],
            "bounded_implementation": dimensions["cursorbench"],
            "prototype": _weighted(dimensions, {"cursorbench": 0.7, "apex_agents": 0.3}),
            "complex_implementation": _weighted(
                dimensions, {"cursorbench": 0.5, "deep_swe": 0.3, "frontier_code": 0.2}
            ),
            "root_cause": _weighted(
                dimensions, {"cursorbench": 0.35, "deep_swe": 0.35, "terminal_bench": 0.3}
            ),
            "high_risk_review": _weighted(
                dimensions, {"cursorbench": 0.4, "deep_swe": 0.35, "frontier_code": 0.25}
            ),
        }
        model: dict[str, Any] = {
            "canonical_key": normalise_model_name(name),
            "display_name": name,
            "overall_score": dimensions["cursorbench"],
            "task_scores": tasks,
            "dimensions": dimensions,
            "evidence": {
                "cursorbench": source["sources"]["cursorbench"]["benchmark"],
                "multi_benchmark_model": evidence_name,
                "multi_benchmark_inherited_from_lower_effort": evidence_name not in {None, name},
            },
        }
        for field in ("average_cost_usd", "average_tokens", "average_steps"):
            if field in raw:
                model[field] = raw[field]
        models.append(model)
    models.sort(key=lambda model: (-model["overall_score"], model["canonical_key"]))
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "derived_model_scorecard",
        "generated_at": source["fetched_at"],
        "source_digest": _source_digest(source),
        "models": models,
    }


def fetch_source_snapshot(
    *, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    opener: Callable[..., Any] = _open_fixed,
    now: datetime | None = None,
) -> dict[str, Any]:
    sampled = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    cursorbench_html = _fetch(CURSORBENCH_URL, timeout_seconds=timeout_seconds, opener=opener)
    comparison_html = _fetch(MODEL_COMPARISON_URL, timeout_seconds=timeout_seconds, opener=opener)
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "authoritative_benchmark_source_snapshot",
        "fetched_at": sampled.isoformat().replace("+00:00", "Z"),
        "sources": {
            "cursorbench": _parse_cursorbench(cursorbench_html),
            "model_comparison": _parse_comparison(comparison_html),
        },
    }


def _scorecard_valid(value: Any) -> bool:
    if (
        type(value) is not dict
        or value.get("schema_version") != SCHEMA_VERSION
        or value.get("kind") != "derived_model_scorecard"
        or type(value.get("generated_at")) is not str
        or type(value.get("source_digest")) is not str
        or type(value.get("models")) is not list
        or len(value["models"]) < 10
    ):
        return False
    return all(
        type(model) is dict
        and type(model.get("canonical_key")) is str
        and type(model.get("overall_score")) in (int, float)
        and type(model.get("task_scores")) is dict
        for model in value["models"]
    )


def _bundle_valid(value: Any) -> bool:
    if (
        type(value) is not dict
        or value.get("schema_version") != SCHEMA_VERSION
        or value.get("kind") != "authoritative_model_benchmark_bundle"
        or type(value.get("source")) is not dict
        or not _scorecard_valid(value.get("scorecard"))
    ):
        return False
    source = value["source"]
    return (
        source.get("kind") == "authoritative_benchmark_source_snapshot"
        and type(source.get("sources")) is dict
        and value["scorecard"]["source_digest"] == _source_digest(source)
    )


def _age_seconds(value: dict[str, Any], *, now: datetime) -> float | None:
    try:
        generated = datetime.fromisoformat(value["generated_at"].replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError):
        return None
    age = (now - generated.astimezone(timezone.utc)).total_seconds()
    return age if age >= 0 else None


def refresh(
    *, bundle_path: Path | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    opener: Callable[..., Any] = _open_fixed,
    now: datetime | None = None,
) -> dict[str, Any]:
    selected = bundle_path or cache_store.cache_path(BUNDLE_CACHE_NAME)
    source = fetch_source_snapshot(timeout_seconds=timeout_seconds, opener=opener, now=now)
    scorecard = derive_scorecard(source)
    bundle = {
        "schema_version": SCHEMA_VERSION,
        "kind": "authoritative_model_benchmark_bundle",
        "source": source,
        "scorecard": scorecard,
    }
    descriptor: int | None = None
    try:
        descriptor = cache_store.lock(selected)
        cache_store.write(selected, bundle)
    finally:
        cache_store.unlock(descriptor)
    return {**scorecard, "cache": {"state": "refreshed", "fresh": True, "age_seconds": 0.0}}


def get_scorecard(
    *, bundle_path: Path | None = None,
    now: datetime | None = None, ttl_seconds: float = DEFAULT_TTL_SECONDS,
    launch_refresh: bool = True,
) -> dict[str, Any]:
    selected = bundle_path or cache_store.cache_path(BUNDLE_CACHE_NAME)
    sampled = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    descriptor: int | None = None
    value: Any = None
    try:
        descriptor = cache_store.lock(selected)
        value = cache_store.read(selected)
    except cache_store.CacheUnavailable:
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "derived_model_scorecard",
            "status": "unavailable",
            "models": [],
            "cache": {"state": "unavailable", "fresh": False, "refresh_started": False},
        }
    finally:
        cache_store.unlock(descriptor)
    valid = _bundle_valid(value)
    scorecard = value["scorecard"] if valid else None
    age = _age_seconds(scorecard, now=sampled) if scorecard is not None else None
    fresh = age is not None and age <= max(0.0, ttl_seconds)
    refresh_started = False
    if not fresh and launch_refresh:
        if cache_store.mark_refresh_due(
            selected,
            minimum_interval_seconds=REFRESH_RETRY_SECONDS,
            reservation_key="authoritative-benchmarks",
        ):
            refresh_started = cache_store.spawn_refresh(Path(__file__), "_refresh-worker")
    if valid:
        return {
            **scorecard,
            "status": "ok",
            "cache": {
                "state": "fresh" if fresh else "stale",
                "fresh": fresh,
                "age_seconds": round(age, 1) if age is not None else None,
                "refresh_started": refresh_started,
            },
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "derived_model_scorecard",
        "status": "warming",
        "models": [],
        "cache": {"state": "warming", "fresh": False, "refresh_started": refresh_started},
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="读取异步刷新的全局模型评分卡")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("get")
    refresh_parser = commands.add_parser("refresh")
    refresh_parser.add_argument("--wait", action="store_true", required=True)
    commands.add_parser("_refresh-worker", help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    command = arguments.command or "get"
    if command == "get":
        result = get_scorecard()
        code = 0
    else:
        try:
            result = refresh()
            code = 0
        except (ScoreRefreshError, cache_store.CacheUnavailable, KeyError, TypeError, ValueError):
            result = {"schema_version": SCHEMA_VERSION, "status": "refresh_failed"}
            code = 1
        if command == "_refresh-worker":
            return code
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
