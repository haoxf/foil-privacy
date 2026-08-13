#!/usr/bin/env python3
"""Single authority for reasoning-depth ordering and runtime effort mapping."""

from __future__ import annotations


REASONING_DEPTH_LEVELS = {
    "economy": 0,
    "standard": 1,
    "deep": 2,
    "maximum": 3,
}
REASONING_DEPTH_NAMES = tuple(REASONING_DEPTH_LEVELS)

_EFFORT_DEPTH = {
    "none": "economy",
    "minimal": "economy",
    "low": "economy",
    "medium": "economy",
    "high": "standard",
    "extra high": "deep",
    "xhigh": "deep",
    "max": "maximum",
}


def is_reasoning_depth(value: object) -> bool:
    return type(value) is str and value in REASONING_DEPTH_LEVELS


def reasoning_depth_at_least(actual: object, required: object) -> bool:
    return (
        is_reasoning_depth(actual)
        and is_reasoning_depth(required)
        and REASONING_DEPTH_LEVELS[actual] >= REASONING_DEPTH_LEVELS[required]
    )


def reasoning_depth_excess(actual: object, required: object) -> int:
    if not reasoning_depth_at_least(actual, required):
        raise ValueError("reasoning depth is below the required depth")
    return REASONING_DEPTH_LEVELS[actual] - REASONING_DEPTH_LEVELS[required]


def reasoning_depth_for_effort(effort: object) -> str | None:
    if type(effort) is not str:
        return None
    return _EFFORT_DEPTH.get(effort.casefold().strip())
