#!/usr/bin/env python3
"""CLI for the lean repository-local task runtime."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

from task_runtime import model
from task_runtime.store import RuntimeStore, RuntimeStoreError


def _task(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--task-id", required=True)


def _owner(parser: argparse.ArgumentParser) -> None:
    _task(parser)
    parser.add_argument("--owner", required=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a small, local complex-task workflow.")
    parser.add_argument("--repo", default=".")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list")

    create = commands.add_parser("create")
    create.add_argument("--definition", required=True, type=Path)
    create.add_argument("--owner", required=True)
    _task(commands.add_parser("status"))
    _owner(commands.add_parser("arm"))

    start = commands.add_parser("start")
    _owner(start)
    start.add_argument("--authorization", required=True)

    for name in ("close-ticket", "close-run"):
        close = commands.add_parser(name)
        _owner(close)
        close.add_argument("--review", required=True, type=Path)

    _owner(commands.add_parser("release"))
    resume = commands.add_parser("resume")
    _owner(resume)
    resume.add_argument("--authorization")
    return parser


def _json_file(path: Path) -> dict[str, Any]:
    value = model.strict_json_loads(path.read_bytes())
    if type(value) is not dict:
        raise model.ValidationError(f"{path} must contain a JSON object")
    return value


def execute(arguments: argparse.Namespace) -> dict[str, Any]:
    runtime = RuntimeStore(arguments.repo)
    command = arguments.command
    if command == "list":
        return runtime.list_tasks()
    if command == "create":
        return runtime.create(_json_file(arguments.definition), owner=arguments.owner)
    if command == "status":
        return runtime.status(arguments.task_id)
    if command == "arm":
        return runtime.arm(arguments.task_id, owner=arguments.owner)
    if command == "start":
        return runtime.start(
            arguments.task_id, owner=arguments.owner, authorization=arguments.authorization
        )
    if command == "close-ticket":
        return runtime.close_ticket(
            arguments.task_id, owner=arguments.owner, review=_json_file(arguments.review)
        )
    if command == "close-run":
        return runtime.close_run(
            arguments.task_id, owner=arguments.owner, review=_json_file(arguments.review)
        )
    if command == "release":
        return runtime.release(arguments.task_id, owner=arguments.owner)
    if command == "resume":
        return runtime.resume(
            arguments.task_id, owner=arguments.owner, authorization=arguments.authorization
        )
    raise model.ValidationError(f"unsupported command: {command}")


def main() -> int:
    parser = build_parser()
    try:
        result = execute(parser.parse_args())
    except (OSError, RuntimeStoreError, model.ValidationError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
