"""Lean, repository-local task state and quality gates."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Any, Iterator

from . import candidate, model, workflow


class RuntimeStoreError(RuntimeError):
    pass


class CheckFailed(RuntimeStoreError):
    pass


class RuntimeStore:
    def __init__(self, repository: str | Path) -> None:
        self.repository = Path(repository).resolve()
        candidate.verify_repo(self.repository)
        self.root = self.repository / ".agent-task-runtime"
        self.tasks_root = self.root / "tasks"
        self.lock_path = self.root / ".lock"

    @contextmanager
    def _lock(self, *, exclusive: bool, create: bool) -> Iterator[None]:
        if create:
            self.tasks_root.mkdir(parents=True, exist_ok=True)
        if not self.root.exists():
            yield
            return
        if self.root.is_symlink() or (self.tasks_root.exists() and self.tasks_root.is_symlink()):
            raise RuntimeStoreError("runtime paths must not be symbolic links")
        flags = os.O_RDWR | (os.O_CREAT if create else 0)
        try:
            descriptor = os.open(self.lock_path, flags, 0o600)
        except FileNotFoundError as error:
            raise RuntimeStoreError("runtime lock is missing") from error
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _task_root(self, task_id: str) -> Path:
        if not model.IDENTIFIER.fullmatch(task_id):
            raise model.ValidationError("task_id is not a safe identifier")
        return self.tasks_root / task_id

    @staticmethod
    def _write(path: Path, value: Any) -> None:
        payload = model.canonical_json_bytes(value)
        descriptor, temporary_name = tempfile.mkstemp(prefix=".write-", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        except BaseException:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            raise

    @staticmethod
    def _read(path: Path) -> dict[str, Any]:
        value = model.strict_json_loads(path.read_bytes())
        if type(value) is not dict:
            raise model.ValidationError(f"{path.name} must be an object")
        if model.canonical_json_bytes(value) != path.read_bytes():
            raise model.ValidationError(f"{path.name} is not canonical JSON")
        return value

    def _load(self, task_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        task_root = self._task_root(task_id)
        if not task_root.is_dir() or task_root.is_symlink():
            raise RuntimeStoreError(f"task not found: {task_id}")
        definition = self._read(task_root / "task.json")
        state = self._read(task_root / "state.json")
        model.validate_definition(definition)
        model.validate_state(definition, state)
        return definition, state

    def _save_state(self, task_id: str, state: dict[str, Any]) -> None:
        self._write(self._task_root(task_id) / "state.json", state)

    def _check_context(self, definition: dict[str, Any]) -> None:
        context = definition["context"]
        if Path(context["repository"]).resolve() != self.repository:
            raise model.ValidationError("task repository does not match --repo")
        if Path(context["worktree"]).resolve() != self.repository:
            raise model.ValidationError("task worktree does not match --repo")
        head = self._git_text(["rev-parse", "HEAD"])
        if head != context["base_head"]:
            raise model.ValidationError("task base_head does not match the worktree")
        branch = self._git_text(["branch", "--show-current"]) or "detached"
        if branch != context["branch_or_detached"]:
            raise model.ValidationError("task branch does not match the worktree")

    def _git_text(self, arguments: list[str]) -> str:
        result = subprocess.run(
            ["git", *arguments], cwd=self.repository, capture_output=True, text=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeStoreError(result.stderr.strip())
        return result.stdout.strip()

    def _base_is_ancestor(self, base_head: str) -> bool:
        result = subprocess.run(
            ["git", "merge-base", "--is-ancestor", base_head, "HEAD"],
            cwd=self.repository, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            check=False,
        )
        return result.returncode == 0

    def _context_drift(self, definition: dict[str, Any], state: dict[str, Any]) -> str | None:
        context = definition["context"]
        branch = self._git_text(["branch", "--show-current"]) or "detached"
        head = self._git_text(["rev-parse", "HEAD"])
        if state["state"] in {"planned", "armed"}:
            if branch != context["branch_or_detached"] or head != context["base_head"]:
                return "branch or HEAD changed before task start"
            return None
        policy = definition["git_policy"]
        allowed = (
            {context["branch_or_detached"]}
            if policy["branch_mode"] == "reuse_current"
            else {policy["return_branch"], policy["target_branch"]}
        )
        if branch not in allowed or not self._base_is_ancestor(context["base_head"]):
            return "branch or base ancestry drifted from the frozen context"
        return None

    def _require_close_context(self, definition: dict[str, Any], state: dict[str, Any]) -> None:
        drift = self._context_drift(definition, state)
        if drift is not None:
            raise model.ValidationError(drift)
        policy = definition["git_policy"]
        if policy["branch_mode"] == "create_task_branch":
            branch = self._git_text(["branch", "--show-current"]) or "detached"
            if branch != policy["target_branch"]:
                raise model.ValidationError("ticket/run close requires the frozen target branch")

    @staticmethod
    def _require_owner(state: dict[str, Any], owner: str) -> None:
        if type(owner) is not str or not owner.strip() or owner != owner.strip():
            raise model.ValidationError("owner must be a trimmed non-empty string")
        if state["owner"] != owner:
            raise model.ValidationError("task owner does not match")

    def create(self, definition: dict[str, Any], *, owner: str) -> dict[str, Any]:
        model.validate_definition(definition)
        self._require_owner({"owner": owner}, owner)
        self._check_context(definition)
        task_id = definition["task_id"]
        state = {
            "schema_version": 1,
            "task_id": task_id,
            "task_digest": model.task_digest(definition),
            "state": "planned",
            "owner": owner,
            "active_ticket": None,
            "tickets": [
                {"id": ticket["id"], "state": "pending", "result": None}
                for ticket in definition["tickets"]
            ],
            "initial_snapshot": None,
            "baseline_snapshot": None,
            "run_result": None,
        }
        model.validate_state(definition, state)
        with self._lock(exclusive=True, create=True):
            task_root = self._task_root(task_id)
            try:
                task_root.mkdir()
            except FileExistsError as error:
                raise RuntimeStoreError(f"task already exists: {task_id}") from error
            self._write(task_root / "task.json", definition)
            self._write(task_root / "state.json", state)
        return self.status(task_id)

    def arm(self, task_id: str, *, owner: str) -> dict[str, Any]:
        with self._lock(exclusive=True, create=False):
            definition, state = self._load(task_id)
            self._require_owner(state, owner)
            if state["state"] != "planned":
                raise model.ValidationError("only a planned task can be armed")
            state["state"] = "armed"
            self._save_state(task_id, state)
            return self._status(definition, state)

    def start(self, task_id: str, *, owner: str, authorization: str) -> dict[str, Any]:
        if authorization != "开始执行":
            raise model.ValidationError("start requires exact authorization: 开始执行")
        with self._lock(exclusive=True, create=False):
            definition, state = self._load(task_id)
            self._require_owner(state, owner)
            if state["state"] != "armed":
                raise model.ValidationError("only an armed task can start")
            self._check_context(definition)
            captured = candidate.snapshot(self.repository, definition["write_paths"])
            state["initial_snapshot"] = captured
            state["baseline_snapshot"] = captured
            state["state"] = "active"
            ticket_id = workflow.next_ready_ticket(definition, state)
            state["active_ticket"] = ticket_id
            workflow.ticket_state(state, ticket_id)["state"] = "active"
            self._save_state(task_id, state)
            return self._status(definition, state)

    def _derive(self, definition: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        projection: dict[str, Any] = {
            "task_id": definition["task_id"],
            "title": definition["title"],
            "task_digest": state["task_digest"],
            "state": state["state"],
            "owner": state["owner"],
            "active_ticket": state["active_ticket"],
            "tickets": state["tickets"],
            "candidate_preview": None,
            "stale_paths": [],
        }
        if state["state"] == "planned":
            projection["next_action"] = {"code": "ARM_TASK"}
            return projection
        if state["state"] == "armed":
            projection["next_action"] = {"code": "AWAIT_START_AUTHORIZATION"}
            return projection

        if state["state"] == "active":
            drift = self._context_drift(definition, state)
            if drift is not None:
                current = candidate.snapshot(self.repository, definition["write_paths"])
                active_ticket = state["active_ticket"]
                roots = (
                    workflow.ticket_definition(definition, active_ticket)["write_paths"]
                    if active_ticket is not None else definition["write_paths"]
                )
                files = candidate.changes(state["baseline_snapshot"], current, roots=roots)
                projection["candidate_preview"] = candidate.preview(
                    files, scope=f"ticket:{active_ticket}" if active_ticket else "run"
                )
                projection["next_action"] = {"code": "CONTEXT_DRIFT", "reason": drift}
                return projection

        current = candidate.snapshot(self.repository, definition["write_paths"])
        expected = state["baseline_snapshot"]
        if state["state"] == "complete":
            stale = candidate.stale_paths(expected, current)
            projection["stale_paths"] = stale
            projection["candidate_preview"] = state["run_result"]["candidate"]
            projection["next_action"] = {
                "code": "STALE_CANDIDATE" if stale else "RUN_GATE_COMPLETE"
            }
            return projection

        active_ticket = state["active_ticket"]
        if active_ticket is not None:
            ticket = workflow.ticket_definition(definition, active_ticket)
            all_changes = candidate.changes(expected, current)
            stale = [
                entry["path"] for entry in all_changes
                if not any(model.path_is_within(entry["path"], root) for root in ticket["write_paths"])
            ]
            files = candidate.changes(expected, current, roots=ticket["write_paths"])
            projection["stale_paths"] = stale
            projection["candidate_preview"] = candidate.preview(
                files, scope=f"ticket:{active_ticket}"
            )
            projection["next_action"] = {
                "code": "STALE_CANDIDATE" if stale else "WORK_TICKET",
                "ticket_id": active_ticket,
            }
            return projection

        stale = candidate.stale_paths(expected, current)
        files = candidate.changes(state["initial_snapshot"], current)
        projection["stale_paths"] = stale
        projection["candidate_preview"] = candidate.preview(files, scope="run")
        projection["next_action"] = {"code": "STALE_CANDIDATE" if stale else "CLOSE_RUN"}
        return projection

    def _status(self, definition: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        model.validate_state(definition, state)
        return self._derive(definition, state)

    def status(self, task_id: str) -> dict[str, Any]:
        with self._lock(exclusive=False, create=False):
            definition, state = self._load(task_id)
            return self._status(definition, state)

    def _run_checks(self, commands: list[list[str]]) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for argv in commands:
            completed = subprocess.run(
                argv, cwd=self.repository, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                check=False, timeout=900,
            )
            digest = hashlib.sha256(completed.stdout).hexdigest()
            result = {"argv": argv, "returncode": completed.returncode, "output_sha256": digest}
            results.append(result)
            if completed.returncode != 0:
                raise CheckFailed(f"check failed with exit {completed.returncode}: {argv[0]}")
        return results

    def close_ticket(
        self, task_id: str, *, owner: str, review: dict[str, Any]
    ) -> dict[str, Any]:
        with self._lock(exclusive=True, create=False):
            definition, state = self._load(task_id)
            self._require_owner(state, owner)
            status = self._status(definition, state)
            if state["state"] != "active" or state["active_ticket"] is None:
                raise model.ValidationError("there is no active ticket to close")
            self._require_close_context(definition, state)
            if status["stale_paths"]:
                raise model.StaleCandidateError("candidate changed outside the active ticket")
            preview = status["candidate_preview"]
            if not preview["files"]:
                raise model.ValidationError("ticket candidate is empty")
            ticket_id = state["active_ticket"]
            ticket = workflow.ticket_definition(definition, ticket_id)
            model.validate_review(
                review, fingerprint=preview["fingerprint"],
                required_tier=ticket["risk"]["required_tier"],
            )
            checks = self._run_checks(ticket["verification"])
            current = candidate.snapshot(self.repository, definition["write_paths"])
            all_changes = candidate.changes(state["baseline_snapshot"], current)
            stale = [
                entry["path"] for entry in all_changes
                if not any(
                    model.path_is_within(entry["path"], root)
                    for root in ticket["write_paths"]
                )
            ]
            after = candidate.preview(
                candidate.changes(
                    state["baseline_snapshot"], current, roots=ticket["write_paths"]
                ),
                scope=f"ticket:{ticket_id}",
            )
            if stale or after["fingerprint"] != preview["fingerprint"]:
                raise model.StaleCandidateError("candidate changed while checks were running")
            ticket_state = workflow.ticket_state(state, ticket_id)
            ticket_state["state"] = "complete"
            ticket_state["result"] = {"candidate": preview, "checks": checks, "review": review}
            state["baseline_snapshot"] = current
            next_ticket = workflow.next_ready_ticket(definition, state)
            state["active_ticket"] = next_ticket
            if next_ticket is not None:
                workflow.ticket_state(state, next_ticket)["state"] = "active"
            self._save_state(task_id, state)
            return self._status(definition, state)

    def close_run(
        self, task_id: str, *, owner: str, review: dict[str, Any]
    ) -> dict[str, Any]:
        with self._lock(exclusive=True, create=False):
            definition, state = self._load(task_id)
            self._require_owner(state, owner)
            status = self._status(definition, state)
            if state["state"] != "active" or state["active_ticket"] is not None:
                raise model.ValidationError("run cannot close before every ticket")
            self._require_close_context(definition, state)
            if status["stale_paths"]:
                raise model.StaleCandidateError("completed ticket output drifted")
            preview = status["candidate_preview"]
            model.validate_review(
                review, fingerprint=preview["fingerprint"],
                required_tier="strong",
            )
            checks = self._run_checks(definition["run_verification"])
            current = candidate.snapshot(self.repository, definition["write_paths"])
            stale = candidate.stale_paths(state["baseline_snapshot"], current)
            after = candidate.preview(
                candidate.changes(state["initial_snapshot"], current), scope="run"
            )
            if stale or after["fingerprint"] != preview["fingerprint"]:
                raise model.StaleCandidateError("run candidate changed while checks were running")
            state["run_result"] = {"candidate": preview, "checks": checks, "review": review}
            state["state"] = "complete"
            state["baseline_snapshot"] = current
            self._save_state(task_id, state)
            return self._status(definition, state)

    def release(self, task_id: str, *, owner: str) -> dict[str, Any]:
        with self._lock(exclusive=True, create=False):
            definition, state = self._load(task_id)
            self._require_owner(state, owner)
            if state["state"] == "complete":
                raise model.ValidationError("completed tasks cannot be released")
            state["owner"] = None
            self._save_state(task_id, state)
            return self._status(definition, state)

    def resume(
        self, task_id: str, *, owner: str, authorization: str | None = None
    ) -> dict[str, Any]:
        with self._lock(exclusive=True, create=False):
            definition, state = self._load(task_id)
            if type(owner) is not str or not owner.strip() or owner != owner.strip():
                raise model.ValidationError("owner must be a trimmed non-empty string")
            previous = state["owner"]
            if previous not in {None, owner} and authorization != "重新开始任务":
                raise model.ValidationError("takeover requires exact authorization: 重新开始任务")
            state["owner"] = owner
            self._save_state(task_id, state)
            return self._status(definition, state)

    def list_tasks(self) -> dict[str, Any]:
        if not self.tasks_root.exists():
            return {"schema_version": 1, "task_count": 0, "incomplete_count": 0, "tasks": []}
        with self._lock(exclusive=False, create=False):
            tasks: list[dict[str, Any]] = []
            for path in sorted(self.tasks_root.iterdir(), key=lambda item: item.name):
                if not path.is_dir() or path.is_symlink() or not model.IDENTIFIER.fullmatch(path.name):
                    raise RuntimeStoreError("invalid task entry")
                definition, state = self._load(path.name)
                status = self._status(definition, state)
                live_gate = status["next_action"]["code"] == "RUN_GATE_COMPLETE"
                persisted_complete = state["state"] == "complete"
                completion = (
                    "run_gate_complete" if live_gate
                    else "stale_after_complete" if persisted_complete
                    else "incomplete"
                )
                tasks.append({
                    "task_id": path.name,
                    "title": definition["title"],
                    "state": state["state"],
                    "active_ticket": state["active_ticket"],
                    "ticket_count": len(state["tickets"]),
                    "completed_ticket_count": sum(
                        ticket["state"] == "complete" for ticket in state["tickets"]
                    ),
                    "completion": completion,
                    "next_action": status["next_action"],
                })
            return {
                "schema_version": 1,
                "task_count": len(tasks),
                "incomplete_count": sum(item["completion"] == "incomplete" for item in tasks),
                "tasks": tasks,
            }
