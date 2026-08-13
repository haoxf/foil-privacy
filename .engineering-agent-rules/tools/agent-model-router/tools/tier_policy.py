#!/usr/bin/env python3
"""Single authority for shared agent capability tier ordering."""

from __future__ import annotations


TIER_LEVELS = {"weak": 0, "medium": 1, "strong": 2}
TIER_NAMES = tuple(TIER_LEVELS)


def is_tier(value: object) -> bool:
    return type(value) is str and value in TIER_LEVELS


def tier_at_least(actual: object, required: object) -> bool:
    return (
        is_tier(actual)
        and is_tier(required)
        and TIER_LEVELS[actual] >= TIER_LEVELS[required]
    )
