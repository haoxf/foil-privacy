#!/usr/bin/env python3
"""Select the lowest sufficient execution target from scores and quota pools."""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tomllib
from typing import Any, Callable

import codex_usage
import cursor_model_selection
import dispatch_policy
from model_identity import (
    canonical_model_key, effort_from_model_identity, receipt_model_key,
)
import model_score_cache
import quota_cache
from reasoning_policy import (
    REASONING_DEPTH_LEVELS, REASONING_DEPTH_NAMES,
    reasoning_depth_at_least, reasoning_depth_excess, reasoning_depth_for_effort,
)
from tier_policy import TIER_LEVELS, TIER_NAMES, tier_at_least


SCHEMA_VERSION = 1
TIER_SCORE = {"weak": 0.0, "medium": 60.0, "strong": 67.0}
TASK_CLASSES = {
    "exploration",
    "micro_edit",
    "bounded_implementation",
    "prototype",
    "complex_implementation",
    "root_cause",
    "high_risk_review",
}
PURPOSES = {"reviewer", "writer"}
PREFERRED_PROVIDERS = {"auto", "codex", "cursor"}
SCORE_DIMENSIONS = {
    "overall_score",
    "task_score",
    "cursorbench",
    "aa_intelligence",
    "gdpval_aa",
    "deep_swe",
    "frontier_code",
    "apex_agents",
    "terminal_bench",
    "apex_swe",
    "aa_briefcase",
    "harvey_lab",
}
MAX_MODELS_OUTPUT_BYTES = 512 * 1024


def parse_cursor_models(output: str) -> list[dict[str, str]]:
    models: list[dict[str, str]] = []
    seen: set[str] = set()
    for line in output.splitlines():
        if " - " not in line:
            continue
        model_id, display_name = (part.strip() for part in line.split(" - ", 1))
        if (
            not model_id
            or not display_name
            or model_id in seen
            or not re.fullmatch(r"[a-zA-Z0-9_.-]+", model_id)
        ):
            continue
        seen.add(model_id)
        source = (
            "auto" if model_id == "auto"
            else "cursor_native" if model_id.startswith(("cursor-", "composer-"))
            else "third_party"
        )
        try:
            receipt_key = receipt_model_key(model_id, display_name)
        except ValueError:
            continue
        models.append(
            {
                "model_id": model_id,
                "display_name": display_name,
                "canonical_key": canonical_model_key(model_id, display_name),
                "receipt_key": receipt_key,
                "model_source": source,
            }
        )
    return models


def live_cursor_models(
    *, executable: str = "agent", timeout_seconds: float = 8.0,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> list[dict[str, str]]:
    resolved = shutil.which(executable)
    if resolved is None:
        return []
    try:
        result = runner(
            [resolved, "models"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            timeout=max(0.1, timeout_seconds),
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0 or len(result.stdout.encode("utf-8")) > MAX_MODELS_OUTPUT_BYTES:
        return []
    return parse_cursor_models(result.stdout)


def _cursor_usage_tool() -> Path:
    return Path(__file__).with_name("cursor_usage.py")


def get_cursor_usage(
    *, runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    tool = _cursor_usage_tool()
    if not tool.is_file():
        return {
            "schema_version": 1,
            "status": "unsupported",
            "cache": {"state": "unavailable", "fresh": False},
        }
    try:
        result = runner(
            [sys.executable, str(tool)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            timeout=3.0,
            check=False,
        )
        value = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return {
            "schema_version": 1,
            "status": "unknown",
            "cache": {"state": "unavailable", "fresh": False},
        }
    return value if type(value) is dict else {"status": "unknown"}


def discover_codex_roles(repo: Path) -> list[dict[str, Any]]:
    roles: list[dict[str, Any]] = []
    directory = repo / ".codex/agents"
    if not directory.is_dir():
        return roles
    for path in sorted(directory.glob("*.toml")):
        try:
            value = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
            continue
        name = value.get("name")
        model = value.get("model")
        effort = value.get("model_reasoning_effort")
        if not all(type(item) is str and item for item in (name, model, effort)):
            continue
        roles.append({"role": name, "model_id": model, "reasoning_effort": effort})
    return roles


def _pool_facts(cursor: dict[str, Any], codex: dict[str, Any]) -> dict[str, dict[str, Any]]:
    facts: dict[str, dict[str, Any]] = {}
    cursor_fresh = cursor.get("cache", {}).get("fresh") is True
    if cursor.get("status") == "ok":
        for key, pool_id in (("auto", "cursor_first_party"), ("api", "cursor_api")):
            raw = cursor.get(key)
            if type(raw) is not dict:
                continue
            facts[pool_id] = {
                "pool_id": pool_id,
                "fresh": cursor_fresh,
                "exhausted": raw.get("exhausted") is True if cursor_fresh else False,
                "used_percent": raw.get("used_percent"),
            }
    codex_fresh = codex.get("cache", {}).get("fresh") is True
    if codex.get("status") == "ok":
        for raw in codex.get("pools", []):
            if type(raw) is not dict or type(raw.get("pool_id")) is not str:
                continue
            window = raw.get("window", {})
            facts[raw["pool_id"]] = {
                "pool_id": raw["pool_id"],
                "fresh": codex_fresh,
                "exhausted": raw.get("exhausted") is True if codex_fresh else False,
                "used_percent": window.get("used_percent"),
            }
    return facts


def _score_index(scorecard: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if scorecard.get("status") != "ok":
        return {}
    return {
        model["canonical_key"]: model
        for model in scorecard.get("models", [])
        if type(model) is dict and type(model.get("canonical_key")) is str
    }


def _tier_from_score(score: float | None) -> str:
    if score is None:
        return "weak"
    if score >= TIER_SCORE["strong"]:
        return "strong"
    if score >= TIER_SCORE["medium"]:
        return "medium"
    return "weak"


def _cursor_candidates(
    models: list[dict[str, str]], scorecard: dict[str, Any],
    *, task_class: str,
) -> list[dict[str, Any]]:
    scores = _score_index(scorecard)
    candidates: list[dict[str, Any]] = []
    for live in models:
        if live["model_id"] == "auto":
            if task_class not in {"exploration", "micro_edit", "bounded_implementation"}:
                continue
            candidates.append(
                {
                    "executor": "cursor_agent",
                    "model_id": "auto",
                    "display_name": live["display_name"],
                    "model_source": "auto",
                    "receipt_key": live["receipt_key"],
                    "pool_id": "cursor_first_party",
                    "tier": "medium",
                    "capability_mode": "adaptive_unscored",
                    "policy_ceiling": "medium",
                    "reasoning_depth": "economy",
                    "numeric_scores_available": False,
                    "task_score": None,
                    "score_evidence": "adaptive_policy_ceiling_not_benchmark",
                }
            )
            continue
        scored = scores.get(live["canonical_key"])
        if scored is None:
            continue
        try:
            effort = effort_from_model_identity(
                live["model_id"], live["display_name"]
            )
        except ValueError:
            continue
        reasoning_depth = reasoning_depth_for_effort(effort)
        if reasoning_depth is None:
            continue
        task_score = scored.get("task_scores", {}).get(task_class)
        if type(task_score) not in (int, float):
            continue
        pool_id = (
            "cursor_first_party"
            if live["model_source"] == "cursor_native"
            else "cursor_api"
        )
        candidates.append(
            {
                "executor": "cursor_agent",
                "model_id": live["model_id"],
                "display_name": live["display_name"],
                "model_source": live["model_source"],
                "receipt_key": live["receipt_key"],
                "pool_id": pool_id,
                "tier": _tier_from_score(float(scored["overall_score"])),
                "reasoning_depth": reasoning_depth,
                "reasoning_depth_evidence": "explicit_runtime_effort",
                "capability_mode": "benchmarked_explicit_model",
                "numeric_scores_available": True,
                "task_score": float(task_score),
                "overall_score": scored.get("overall_score"),
                "average_cost_usd": scored.get("average_cost_usd"),
                "scores": {
                    "overall_score": scored.get("overall_score"),
                    "task_score": float(task_score),
                    **scored.get("dimensions", {}),
                },
                "score_evidence": "derived_scorecard",
                "fast_variant": live["model_id"].endswith("-fast"),
            }
        )
    return candidates


_ROLE_TASKS = {
    "explorer": {"exploration"},
    "spark_worker": {"micro_edit"},
    "bounded_worker": {"bounded_implementation"},
    "strong_worker": {
        "bounded_implementation", "prototype", "complex_implementation",
    },
    "deep_worker": {
        "bounded_implementation", "prototype", "complex_implementation",
    },
    "strong_reviewer": {
        "bounded_implementation", "prototype", "complex_implementation",
        "root_cause", "high_risk_review",
    },
    "critical_reviewer": {
        "bounded_implementation", "prototype", "complex_implementation",
        "root_cause", "high_risk_review",
    },
    "max_worker": {
        "bounded_implementation", "prototype", "complex_implementation",
    },
    "max_reviewer": {
        "bounded_implementation", "prototype", "complex_implementation",
        "root_cause", "high_risk_review",
    },
}

_REVIEWER_ROLES = {"strong_reviewer", "critical_reviewer", "max_reviewer"}


def _codex_candidates(
    roles: list[dict[str, Any]], scorecard: dict[str, Any],
    *, task_class: str, purpose: str,
) -> list[dict[str, Any]]:
    scores = _score_index(scorecard)
    candidates: list[dict[str, Any]] = []
    for role in roles:
        name = role["role"]
        if task_class not in _ROLE_TASKS.get(name, set()):
            continue
        if purpose == "writer" and name in _REVIEWER_ROLES:
            continue
        if purpose == "reviewer" and name not in _REVIEWER_ROLES:
            continue
        spark = name in {"explorer", "spark_worker"}
        tier = "medium" if spark or name == "bounded_worker" else "strong"
        reasoning_depth = reasoning_depth_for_effort(role["reasoning_effort"])
        if reasoning_depth is None:
            continue
        candidate: dict[str, Any] = {
                "executor": "codex_agent",
                "role": name,
                "model_id": role["model_id"],
                "reasoning_effort": role["reasoning_effort"],
                "pool_id": "codex_spark" if spark else "codex_main",
                "tier": tier,
                "reasoning_depth": reasoning_depth,
                "reasoning_depth_evidence": "frozen_role_effort",
                "task_score": None,
                "score_evidence": "frozen_role_contract",
            }
        try:
            score_key = canonical_model_key(
                f"{role['model_id']} {role['reasoning_effort']}",
                f"{role['model_id']} {role['reasoning_effort']}",
            )
        except ValueError:
            score_key = ""
        scored = scores.get(score_key)
        task_score = scored.get("task_scores", {}).get(task_class) if scored else None
        if scored is not None and type(task_score) in (int, float):
            candidate.update(
                {
                    "capability_mode": "benchmarked_codex_role",
                    "numeric_scores_available": True,
                    "task_score": float(task_score),
                    "overall_score": scored.get("overall_score"),
                    "average_cost_usd": scored.get("average_cost_usd"),
                    "scores": {
                        "overall_score": scored.get("overall_score"),
                        "task_score": float(task_score),
                        **scored.get("dimensions", {}),
                    },
                    "score_evidence": "derived_scorecard_plus_frozen_role",
                }
            )
        candidates.append(candidate)
    return candidates


def _base_rank(candidate: dict[str, Any], *, task_class: str, purpose: str) -> float:
    executor = candidate["executor"]
    role = candidate.get("role")
    if task_class in {"exploration", "micro_edit"}:
        base = 0.0 if candidate["pool_id"] == "codex_spark" else 20.0 if executor == "cursor_agent" else 40.0
    elif purpose == "reviewer":
        base = 0.0 if executor == "cursor_agent" else 10.0 if role in _REVIEWER_ROLES else 30.0
    else:
        base = 0.0 if executor == "cursor_agent" else 20.0 if role == "bounded_worker" else 40.0
    return base


def recommend(
    *, task_class: str, required_tier: str, reasoning_depth: str, purpose: str,
    cursor_models: list[dict[str, str]], scorecard: dict[str, Any],
    cursor_quota: dict[str, Any], codex_quota: dict[str, Any],
    codex_roles: list[dict[str, Any]], writer_model: str | None = None,
    prefer_fast: bool = False,
    preferred_provider: str = "auto",
    cursor_adapter_enabled: bool = True,
    minimum_scores: dict[str, float] | None = None,
) -> dict[str, Any]:
    if (
        task_class not in TASK_CLASSES
        or required_tier not in TIER_LEVELS
        or reasoning_depth not in REASONING_DEPTH_LEVELS
        or purpose not in PURPOSES
        or preferred_provider not in PREFERRED_PROVIDERS
    ):
        raise ValueError("invalid routing request")
    pools = _pool_facts(cursor_quota, codex_quota)
    minimums = minimum_scores or {}
    if set(minimums) - SCORE_DIMENSIONS:
        raise ValueError("unknown score dimension")
    if any(type(value) not in (int, float) or not 0 <= value <= 10000 for value in minimums.values()):
        raise ValueError("score minimum must be a finite non-negative number")
    candidates: list[dict[str, Any]] = []
    if not (task_class == "root_cause" and purpose == "writer"):
        if task_class != "root_cause" and cursor_adapter_enabled:
            candidates = _cursor_candidates(
                cursor_models, scorecard, task_class=task_class
            )
        candidates += _codex_candidates(
            codex_roles, scorecard, task_class=task_class, purpose=purpose
        )
    eligible: list[dict[str, Any]] = []
    for candidate in candidates:
        pool = pools.get(candidate["pool_id"])
        if pool is not None and pool["exhausted"]:
            continue
        if not tier_at_least(candidate["tier"], required_tier):
            continue
        if not reasoning_depth_at_least(
            candidate.get("reasoning_depth"), reasoning_depth
        ):
            continue
        candidate_scores = candidate.get("scores", {})
        if any(
            type(candidate_scores.get(dimension)) not in (int, float)
            or float(candidate_scores[dimension]) < minimum
            for dimension, minimum in minimums.items()
        ):
            continue
        score = candidate.get("task_score")
        overall_score = candidate.get("overall_score")
        if candidate.get("capability_mode") == "benchmarked_explicit_model" and (
            type(overall_score) not in (int, float)
            or overall_score < TIER_SCORE[required_tier]
        ):
            continue
        rank = _base_rank(candidate, task_class=task_class, purpose=purpose)
        used = pool.get("used_percent") if pool is not None else None
        if type(used) in (int, float):
            rank += float(used) / 20.0
        if type(score) in (int, float):
            rank -= float(score) / 20.0
        cost = candidate.get("average_cost_usd")
        if type(cost) in (int, float):
            rank += float(cost) / 5.0
        if candidate.get("fast_variant") and not prefer_fast:
            rank += 4.0
        if writer_model and candidate.get("model_id") == writer_model:
            rank += 8.0
        candidate_provider = (
            "codex" if candidate["executor"] == "codex_agent" else "cursor"
        )
        eligible.append(
            {
                **candidate,
                "provider": candidate_provider,
                "reasoning_depth_excess": reasoning_depth_excess(
                    candidate["reasoning_depth"], reasoning_depth
                ),
                "rank": round(rank, 3),
            }
        )
    eligible.sort(
        key=lambda item: (
            item["reasoning_depth_excess"],
            0
            if preferred_provider == "auto"
            or item["provider"] == preferred_provider
            else 1,
            item["rank"],
            -(item.get("task_score") or -1),
            item.get("model_id", ""),
        )
    )
    recommended = eligible[0] if eligible else None
    recommended_provider = (
        "codex" if recommended and recommended["executor"] == "codex_agent"
        else "cursor" if recommended else None
    )
    preference_honored = (
        None
        if preferred_provider == "auto"
        else recommended_provider == preferred_provider
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "ok" if recommended is not None else "no_eligible_target",
        "task": {
            "class": task_class,
            "required_tier": required_tier,
            "reasoning_depth": reasoning_depth,
            "purpose": purpose,
            "minimum_scores": dict(sorted(minimums.items())),
            "preferred_provider": preferred_provider,
        },
        "recommended": recommended,
        "alternatives": eligible[1:5],
        "evidence": {
            "scorecard": scorecard.get("cache", {}),
            "cursor_quota": cursor_quota.get("cache", {}),
            "codex_quota": codex_quota.get("cache", {}),
            "live_cursor_model_count": len(cursor_models),
        },
        "pools": sorted(pools.values(), key=lambda pool: pool["pool_id"]),
        "preference": {
            "requested": preferred_provider,
            "honored": preference_honored,
            "fallback_reason": (
                None
                if preferred_provider == "auto" or preference_honored
                else (
                    "Cursor adapter is disabled by the global dispatch policy"
                    if preferred_provider == "cursor" and not cursor_adapter_enabled
                    else (
                        f"no eligible {preferred_provider} candidate at the minimum "
                        "sufficient reasoning depth satisfied capability, purpose, "
                        "score, and quota gates"
                    )
                )
            ),
        },
    }


def select(
    *, repo: Path, task_class: str, required_tier: str,
    reasoning_depth: str, purpose: str,
    writer_model: str | None = None, prefer_fast: bool = False,
    preferred_provider: str | None = None,
    minimum_scores: dict[str, float] | None = None,
    dispatch_policy_path: Path | None = None,
) -> dict[str, Any]:
    policy = dispatch_policy.load_policy(dispatch_policy_path)
    effective_preference = (
        preferred_provider
        if preferred_provider is not None
        else policy["policy"]["provider_preference"]
    )
    adapter_enabled = policy["policy"]["cursor_adapter_enabled"] is True
    account_models = live_cursor_models()
    cursor_selection = cursor_model_selection.read_enabled_model_families()
    enabled_models = cursor_model_selection.filter_enabled_cli_models(
        account_models, cursor_selection,
    )
    result = recommend(
        task_class=task_class,
        required_tier=required_tier,
        reasoning_depth=reasoning_depth,
        purpose=purpose,
        cursor_models=enabled_models,
        scorecard=model_score_cache.get_scorecard(),
        cursor_quota=get_cursor_usage(),
        codex_quota=codex_usage.get_usage(),
        codex_roles=discover_codex_roles(repo.resolve()),
        writer_model=writer_model,
        prefer_fast=prefer_fast,
        preferred_provider=effective_preference,
        cursor_adapter_enabled=adapter_enabled,
        minimum_scores=minimum_scores,
    )
    result["dispatch_policy"] = policy
    result["preference"]["source"] = (
        "explicit_task" if preferred_provider is not None else "global_default"
    )
    result["evidence"]["cursor_model_selection"] = {
        "status": cursor_selection.get("status", "unavailable"),
        "source": cursor_selection.get("source", "cursor_local_settings"),
        "reason": cursor_selection.get("reason"),
        "account_model_count": len(account_models),
        "enabled_family_count": len(cursor_selection.get("enabled_families", [])),
        "enabled_model_variant_count": len(enabled_models),
        "enabled_families": cursor_selection.get("enabled_families", []),
    }
    return result


def _score_requirement(value: str) -> tuple[str, float]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("score requirement must be DIMENSION=MINIMUM")
    dimension, raw = value.split("=", 1)
    dimension = dimension.strip()
    if dimension not in SCORE_DIMENSIONS:
        raise argparse.ArgumentTypeError(
            f"unknown score dimension: {dimension}; choose from {', '.join(sorted(SCORE_DIMENSIONS))}"
        )
    try:
        minimum = float(raw)
    except ValueError as error:
        raise argparse.ArgumentTypeError("score minimum must be numeric") from error
    if not 0 <= minimum <= 10000:
        raise argparse.ArgumentTypeError("score minimum is outside the supported range")
    return dimension, minimum


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="根据评分卡与额度池选择执行或审查模型")
    commands = parser.add_subparsers(dest="command", required=True)
    choose = commands.add_parser("select")
    choose.add_argument("--repo", type=Path, default=Path.cwd())
    choose.add_argument("--task-class", required=True, choices=sorted(TASK_CLASSES))
    choose.add_argument("--required-tier", required=True, choices=TIER_NAMES)
    choose.add_argument(
        "--reasoning-depth", required=True, choices=REASONING_DEPTH_NAMES,
        help="独立推理投入下限；economy/standard/deep/maximum",
    )
    choose.add_argument("--purpose", required=True, choices=sorted(PURPOSES))
    choose.add_argument("--writer-model")
    choose.add_argument("--prefer-fast", action="store_true")
    choose.add_argument(
        "--prefer-provider",
        choices=sorted(PREFERRED_PROVIDERS),
        default=None,
        help=(
            "用户显式 provider 软优先；省略时读取全局策略；能力、用途、评分与额度门禁仍不可绕过"
        ),
    )
    choose.add_argument(
        "--min-score",
        action="append",
        default=[],
        type=_score_requirement,
        metavar="DIMENSION=MINIMUM",
        help="模型必须满足的评分维度下限；可重复",
    )
    commands.add_parser("snapshot")
    report_limit = commands.add_parser("report-limit")
    report_limit.add_argument("--pool", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.command == "report-limit":
        result = quota_cache.invalidate_pool(arguments.pool)
        code = (
            0 if result["state"] == "invalidated"
            else 64 if result["state"] == "invalid_pool"
            else 1
        )
    elif arguments.command == "snapshot":
        account_models = live_cursor_models()
        cursor_selection = cursor_model_selection.read_enabled_model_families()
        enabled_models = cursor_model_selection.filter_enabled_cli_models(
            account_models, cursor_selection,
        )
        result = {
            "schema_version": SCHEMA_VERSION,
            "dispatch_policy": dispatch_policy.load_policy(),
            "scorecard": model_score_cache.get_scorecard(),
            "cursor_quota": get_cursor_usage(),
            "codex_quota": codex_usage.get_usage(),
            "cursor_model_selection": {
                **cursor_selection,
                "account_model_count": len(account_models),
                "enabled_model_variant_count": len(enabled_models),
            },
        }
        code = 0
    else:
        minimum_scores: dict[str, float] = {}
        duplicate_dimensions: set[str] = set()
        for dimension, minimum in arguments.min_score:
            if dimension in minimum_scores:
                duplicate_dimensions.add(dimension)
            minimum_scores[dimension] = minimum
        if duplicate_dimensions:
            result = {
                "schema_version": SCHEMA_VERSION,
                "status": "invalid_request",
                "error": "duplicate score dimensions: " + ", ".join(sorted(duplicate_dimensions)),
            }
            print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
            return 64
        result = select(
            repo=arguments.repo,
            task_class=arguments.task_class,
            required_tier=arguments.required_tier,
            reasoning_depth=arguments.reasoning_depth,
            purpose=arguments.purpose,
            writer_model=arguments.writer_model,
            prefer_fast=arguments.prefer_fast,
            preferred_provider=arguments.prefer_provider,
            minimum_scores=minimum_scores,
        )
        code = 0 if result["recommended"] is not None else 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
