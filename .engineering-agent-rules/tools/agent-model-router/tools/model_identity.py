#!/usr/bin/env python3
"""Canonical Cursor model identity shared by the router and its adapters."""

from __future__ import annotations

import re


RECEIPT_KEY_PATTERN = re.compile(r"[a-z0-9]{1,160}")


def normalise_model_name(value: str) -> str:
    value = re.sub(r"\b(?:cursor|fast|no\s*zdr|1m)\b", " ", value, flags=re.I)
    value = re.sub(
        r"\bextra[ -]?high\b|\bxhigh\b", " extra high ", value, flags=re.I,
    )
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def effort_from_model_id(model_id: str) -> str | None:
    tokens = re.split(r"[-_.]+", model_id.casefold())
    efforts: set[str] = set()
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "xhigh":
            efforts.add("extra high")
        elif token == "extra" and index + 1 < len(tokens) and tokens[index + 1] == "high":
            efforts.add("extra high")
            index += 1
        elif token in {"max", "high", "medium", "low", "minimal", "none"}:
            efforts.add(token)
        index += 1
    if len(efforts) > 1:
        raise ValueError("model id contains conflicting reasoning efforts")
    return next(iter(efforts), None)


def effort_from_model_identity(model_id: str, display_name: str) -> str | None:
    """Return an explicit effort only when id/display evidence is consistent."""

    id_effort = effort_from_model_id(model_id)
    display_effort = _display_effort(display_name)
    if id_effort and display_effort and id_effort != display_effort:
        raise ValueError("model id and display name disagree on reasoning effort")
    return id_effort or display_effort


def canonical_model_key(model_id: str, display_name: str) -> str:
    """Build the benchmark key, intentionally ignoring runtime variants."""

    cleaned = re.sub(
        r"\b(?:cursor|fast|no\s*zdr|1m)\b|[()]", " ", display_name, flags=re.I,
    )
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    effort = effort_from_model_id(model_id)
    has_effort = re.search(
        r"\b(?:none|minimal|low|medium|high|extra[ -]?high|xhigh|max)\b",
        cleaned,
        re.I,
    )
    if effort and not has_effort:
        cleaned = f"{cleaned} {effort}"
    return normalise_model_name(cleaned)


def _receipt_parts(
    value: str, *, fallback_effort: str | None = None,
    extra_flags: frozenset[str] = frozenset(),
) -> str:
    folded = value.casefold()
    effort = _display_effort(value) or fallback_effort
    flags = {
        "fast": re.search(r"\bfast\b", folded) is not None or "fast" in extra_flags,
        "1m": re.search(r"\b1m\b", folded) is not None or "1m" in extra_flags,
        "nozdr": re.search(r"\bno\s*zdr\b", folded) is not None or "nozdr" in extra_flags,
    }
    base = re.sub(
        r"\b(?:cursor|fast|no\s*zdr|1m|extra[ -]?high|xhigh|max|high|medium|minimal|low|none)\b|[()]",
        " ", value, flags=re.I,
    )
    key = re.sub(r"[^a-z0-9]+", "", base.casefold())
    if effort:
        key += "extrahigh" if effort in {"extra high", "xhigh"} else effort
    return key + "".join(flag for flag in ("fast", "1m", "nozdr") if flags[flag])


def _runtime_flags(value: str) -> dict[str, bool]:
    folded = value.casefold()
    return {
        "fast": re.search(r"(?:^|[-_.\s])fast(?:$|[-_.\s])", folded) is not None,
        "1m": re.search(r"(?:^|[-_.\s])1m(?:$|[-_.\s])", folded) is not None,
        "nozdr": re.search(
            r"(?:^|[-_.\s])no[-_.\s]*zdr(?:$|[-_.\s])", folded,
        ) is not None,
    }


def _display_effort(value: str) -> str | None:
    folded = value.casefold()
    efforts = {
        ("extra high" if match.group(0).replace("-", " ") in {"extra high", "xhigh"} else match.group(0))
        for match in re.finditer(
            r"\b(?:extra[ -]high|xhigh|max|high|medium|minimal|low|none)\b",
            folded,
        )
    }
    if len(efforts) > 1:
        raise ValueError("display name contains conflicting reasoning efforts")
    return next(iter(efforts), None)


def receipt_model_key(model_id: str, display_name: str) -> str:
    """Build an execution receipt key that preserves runtime-affecting variants."""

    if model_id == "auto":
        return "auto"
    id_effort = effort_from_model_id(model_id)
    display_effort = _display_effort(display_name)
    if id_effort and display_effort and id_effort != display_effort:
        raise ValueError("model id and display name disagree on reasoning effort")
    id_flags = _runtime_flags(model_id)
    return _receipt_parts(
        display_name,
        fallback_effort=id_effort,
        extra_flags=frozenset(name for name, present in id_flags.items() if present),
    )


def receipt_key_matches(expected_key: str, reported_model: str | None) -> bool:
    """Compare an opaque router key with Cursor's system-init model receipt."""

    if RECEIPT_KEY_PATTERN.fullmatch(expected_key) is None:
        return False
    reported = (reported_model or "").strip()
    if expected_key == "auto":
        return re.fullmatch(r"auto(?:\s*\([^\r\n]*\))?", reported, re.I) is not None
    try:
        return _receipt_parts(reported) == expected_key
    except ValueError:
        return False
