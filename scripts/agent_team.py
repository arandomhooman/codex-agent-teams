#!/usr/bin/env python3
"""Local state engine for Claude-style agent teams in Codex.

The script intentionally uses plain JSON files so a Codex skill, hook, or human
can inspect and repair the team state without needing a daemon.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


STATE_DIR = ".codex-agent-teams"
STATE_FILE = "state.json"
BACKUP_FILE = "state.json.bak"
LOCK_FILE = "state.lock"
MAX_FINALIZE_ATTEMPTS = 2
LOCK_WAIT_SECONDS = 15.0
LOCK_STALE_SECONDS = 30.0
STATE_READ_ATTEMPTS = 12
STATE_REPLACE_ATTEMPTS = 12
LEAD_ONLY_COMMANDS = {
    "ack-closed",
    "bind-subagent",
    "bind-thread",
    "cancel",
    "cleanup",
    "close-plan",
    "gate",
    "hook-config",
    "launch-plan",
    "orchestrate",
    "reassign",
    "record-delivery-batch",
    "record-wait-batch",
    "record-close-batch",
    "record-close",
    "record-delivery",
    "record-wait",
    "repair",
    "replace-subagent",
    "wake-plan",
}
AGENT_SCOPED_COMMANDS = {"ack", "ack-all", "claim", "complete", "inbox", "prompt"}


class TeamError(Exception):
    def __init__(self, code: str, message: str, exit_code: int = 1):
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "team"


def state_dir(workspace: str | Path) -> Path:
    return Path(workspace).resolve() / STATE_DIR


def state_path(workspace: str | Path) -> Path:
    return state_dir(workspace) / STATE_FILE


def backup_path(workspace: str | Path) -> Path:
    return state_dir(workspace) / BACKUP_FILE


def workspace_root(workspace: str | Path) -> Path:
    return Path(workspace).resolve()


def resolve_workspace_path(
    workspace: str | Path,
    raw_path: str | Path,
    option_name: str,
    allow_outside: bool = False,
) -> Path:
    root = workspace_root(workspace)
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    if allow_outside:
        return resolved
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise TeamError(
            "path_outside_workspace",
            f"{option_name} must stay inside workspace {root}; got {resolved}",
            2,
        ) from exc
    return resolved


@contextmanager
def state_lock(workspace: str | Path):
    directory = state_dir(workspace)
    directory.mkdir(parents=True, exist_ok=True)
    lock_path = directory / LOCK_FILE
    deadline = time.time() + LOCK_WAIT_SECONDS
    fd = None
    recovered_stale = False
    attempt = 0
    while fd is None:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
            metadata = {
                "pid": os.getpid(),
                "created_epoch": time.time(),
                "created_at": now_iso(),
                "command": " ".join(sys.argv[1:3]),
            }
            os.write(fd, (json.dumps(metadata, sort_keys=True) + "\n").encode("utf-8"))
        except FileExistsError:
            if is_stale_lock(lock_path):
                recovered_stale = recover_stale_lock(lock_path) or recovered_stale
                if recovered_stale:
                    continue
            if time.time() > deadline:
                raise TeamError("state_locked", f"Timed out waiting for {lock_path}", 2)
            sleep_with_backoff(attempt)
            attempt += 1
    try:
        yield {"recovered_stale": recovered_stale}
    finally:
        if fd is not None:
            os.close(fd)
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def sleep_with_backoff(attempt: int, base: float = 0.04, cap: float = 0.35) -> None:
    delay = min(cap, base * (attempt + 1))
    time.sleep(delay + random.uniform(0, base))


def is_stale_lock(lock_path: Path) -> bool:
    try:
        return time.time() - lock_path.stat().st_mtime > LOCK_STALE_SECONDS
    except FileNotFoundError:
        return False


def recover_stale_lock(lock_path: Path) -> bool:
    stale_path = lock_path.with_name(f"{LOCK_FILE}.stale.{os.getpid()}.{time.time_ns()}")
    try:
        lock_path.replace(stale_path)
        return True
    except FileNotFoundError:
        return True
    except PermissionError:
        return False


def require_no_fresh_lock(lock_path: Path, action: str) -> None:
    if not lock_path.exists():
        return
    if is_stale_lock(lock_path):
        return
    raise TeamError("state_locked", f"Refusing {action} while fresh lock exists at {lock_path}", 2)


def serialized_state(state: dict[str, Any]) -> str:
    return json.dumps(state, indent=2, sort_keys=True) + "\n"


def read_state(workspace: str | Path) -> dict[str, Any]:
    path = state_path(workspace)
    if not path.exists():
        raise TeamError("no_team", f"No agent team state exists at {path}", 1)
    last_error: Exception | None = None
    for attempt in range(STATE_READ_ATTEMPTS):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except PermissionError as exc:
            last_error = exc
        except json.JSONDecodeError as exc:
            last_error = exc
        if attempt < STATE_READ_ATTEMPTS - 1:
            sleep_with_backoff(attempt, base=0.03, cap=0.2)
    if isinstance(last_error, json.JSONDecodeError):
        raise TeamError(
            "state_corrupt",
            f"Could not parse {path}; run repair --restore-backup if a backup is valid",
            2,
        ) from last_error
    raise TeamError("state_read_failed", f"Could not read {path}: {last_error}", 2) from last_error


def read_state_locked(workspace: str | Path) -> dict[str, Any]:
    with state_lock(workspace):
        return read_state(workspace)


def write_state(workspace: str | Path, state: dict[str, Any]) -> None:
    directory = state_dir(workspace)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / STATE_FILE
    serialized = serialized_state(state)
    replace_text(target, serialized, "state_write_failed")
    write_state_backup(directory, serialized)


@contextmanager
def state_transaction(workspace: str | Path):
    with state_lock(workspace):
        state = read_state(workspace)
        yield state
        write_state(workspace, state)


def replace_text(target: Path, text: str, error_code: str) -> None:
    temp = target.with_name(f"{target.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        with temp.open("w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(STATE_REPLACE_ATTEMPTS):
            try:
                os.replace(str(temp), str(target))
                return
            except PermissionError:
                if attempt == STATE_REPLACE_ATTEMPTS - 1:
                    raise TeamError(error_code, f"Could not replace {target}; another process may be reading it", 2)
                sleep_with_backoff(attempt)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def write_state_backup(directory: Path, serialized: str) -> None:
    try:
        replace_text(directory / BACKUP_FILE, serialized, "state_backup_failed")
    except TeamError:
        # Backup is a recovery aid. Do not report the state mutation as failed
        # after the primary state file has already been replaced successfully.
        pass


def inspect_json_file(path: Path) -> dict[str, Any]:
    info: dict[str, Any] = {"path": str(path), "exists": path.exists(), "valid": False}
    if not path.exists():
        return info
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
        info.update(
            {
                "valid": True,
                "team_id": parsed.get("team_id"),
                "updated_at": parsed.get("updated_at"),
            }
        )
    except Exception as exc:  # noqa: BLE001 - diagnostics should report parse/read failures.
        info["error"] = f"{type(exc).__name__}: {exc}"
    return info


def inspect_lock(lock_path: Path) -> dict[str, Any]:
    info: dict[str, Any] = {"path": str(lock_path), "exists": lock_path.exists(), "stale": False}
    if not lock_path.exists():
        return info
    try:
        stat = lock_path.stat()
        info["age_seconds"] = max(0, round(time.time() - stat.st_mtime, 3))
        info["stale"] = bool(info["age_seconds"] > LOCK_STALE_SECONDS)
        try:
            info["metadata"] = json.loads(lock_path.read_text(encoding="utf-8"))
        except Exception:
            info["metadata"] = None
    except FileNotFoundError:
        info["exists"] = False
    return info


def repair_report(workspace: str | Path) -> dict[str, Any]:
    directory = state_dir(workspace)
    directory.mkdir(parents=True, exist_ok=True)
    orphan_temps = sorted(path.name for path in directory.glob(f"{STATE_FILE}.*.tmp"))
    archived_locks = sorted(path.name for path in directory.glob(f"{LOCK_FILE}.stale.*"))
    return {
        "ok": True,
        "workspace": str(workspace_root(workspace)),
        "state": inspect_json_file(directory / STATE_FILE),
        "backup": inspect_json_file(directory / BACKUP_FILE),
        "lock": inspect_lock(directory / LOCK_FILE),
        "orphan_temps": orphan_temps,
        "archived_stale_locks": archived_locks,
    }


def archive_corrupt_state(target: Path) -> str | None:
    if not target.exists():
        return None
    archive = target.with_name(f"{target.name}.corrupt.{os.getpid()}.{time.time_ns()}")
    target.replace(archive)
    return str(archive)


def archive_existing_state(workspace: str | Path, reason: str) -> dict[str, str | None]:
    directory = state_dir(workspace)
    stamp = f"{slugify(reason)}.{os.getpid()}.{time.time_ns()}"
    archived: dict[str, str | None] = {"state": None, "backup": None}
    for key, filename in (("state", STATE_FILE), ("backup", BACKUP_FILE)):
        target = directory / filename
        if not target.exists():
            continue
        archive = target.with_name(f"{target.name}.archived.{stamp}")
        target.replace(archive)
        archived[key] = str(archive)
    return archived


def cmd_repair(args: argparse.Namespace) -> dict[str, Any]:
    directory = state_dir(args.workspace)
    directory.mkdir(parents=True, exist_ok=True)
    state_file = directory / STATE_FILE
    backup_file = directory / BACKUP_FILE
    lock_file = directory / LOCK_FILE
    actions: list[str] = []

    if args.unlock_stale and is_stale_lock(lock_file):
        if recover_stale_lock(lock_file):
            actions.append("archived stale state.lock")
        else:
            raise TeamError("state_lock_busy", f"Could not archive stale lock {lock_file}", 2)

    if args.clean_temps or args.restore_backup:
        require_no_fresh_lock(lock_file, "mutating repair")

    if args.clean_temps:
        for temp in directory.glob(f"{STATE_FILE}.*.tmp"):
            try:
                temp.unlink()
                actions.append(f"removed {temp.name}")
            except FileNotFoundError:
                pass

    restored_from_backup = False
    corrupt_archive = None
    state_info = inspect_json_file(state_file)
    backup_info = inspect_json_file(backup_file)
    if args.restore_backup:
        if state_info.get("valid"):
            actions.append("state already valid; backup restore skipped")
        elif not backup_info.get("valid"):
            raise TeamError("backup_invalid", f"No valid backup exists at {backup_file}", 2)
        else:
            backup_text = backup_file.read_text(encoding="utf-8")
            corrupt_archive = archive_corrupt_state(state_file)
            replace_text(state_file, backup_text, "state_restore_failed")
            restored_from_backup = True
            actions.append("restored state.json from state.json.bak")

    report = repair_report(args.workspace)
    report["actions"] = actions
    report["state"]["restored_from_backup"] = restored_from_backup
    if corrupt_archive:
        report["state"]["corrupt_archive"] = corrupt_archive
    return report


def event(event_type: str, **data: Any) -> dict[str, Any]:
    payload = {"type": event_type, "at": now_iso()}
    payload.update(data)
    return payload


def parse_member(raw: str) -> dict[str, Any]:
    if "=" not in raw:
        raise TeamError("invalid_member", f"Member must use name=role format: {raw}")
    name, role = raw.split("=", 1)
    name = name.strip()
    role = role.strip()
    if not name or not role:
        raise TeamError("invalid_member", f"Member must include name and role: {raw}")
    return {
        "name": name,
        "role": role,
        "status": "idle",
        "current_task": None,
        "runtime": default_runtime(),
        "runtime_history": [],
        "prompt_context": [],
    }


def default_runtime() -> dict[str, Any]:
    return {
        "backend": "unbound",
        "agent_id": None,
        "nickname": None,
        "thread_id": None,
        "bound_at": None,
        "status": "unbound",
        "pending_inputs": 0,
        "last_wait_at": None,
        "last_result": None,
        "wait_timeouts": 0,
        "finalize_attempts": 0,
        "closed_at": None,
        "close_status": None,
        "close_result": None,
    }


def parse_task(raw: str) -> dict[str, Any]:
    pieces = raw.split(":", 2)
    if len(pieces) != 3:
        raise TeamError("invalid_task", f"Task must use id:owner:title format: {raw}")
    task_id, owner, title = [piece.strip() for piece in pieces]
    if not task_id or not owner or not title:
        raise TeamError("invalid_task", f"Task must include id, owner, and title: {raw}")
    return {
        "id": task_id,
        "owner": owner,
        "title": title,
        "depends_on": [],
        "status": "todo",
        "claimed_by": None,
        "claimed_at": None,
        "completed_at": None,
        "ready_notified_at": None,
        "summary": "",
        "outputs": [],
    }


def parse_dependency(raw: str) -> tuple[str, str]:
    pieces = raw.split(":", 1)
    if len(pieces) != 2:
        raise TeamError("invalid_dependency", f"Dependency must use task:depends_on format: {raw}")
    task_id, dependency_id = [piece.strip() for piece in pieces]
    if not task_id or not dependency_id:
        raise TeamError("invalid_dependency", f"Dependency must include both task ids: {raw}")
    return task_id, dependency_id


def split_assignment(raw: str, option_name: str) -> tuple[str, str]:
    if "=" not in raw:
        raise TeamError("invalid_assignment", f"{option_name} must use agent=value format: {raw}")
    name, value = raw.split("=", 1)
    name = name.strip()
    value = value.strip()
    if not name or not value:
        raise TeamError("invalid_assignment", f"{option_name} must include agent and value: {raw}")
    return name, value


def read_workspace_text(
    workspace: str | Path,
    raw_path: str | Path,
    option_name: str,
    allow_outside: bool = False,
) -> tuple[str, str]:
    path = resolve_workspace_path(workspace, raw_path, option_name, allow_outside)
    return str(path), path.read_text(encoding="utf-8")


def collect_prompt_options(args: argparse.Namespace, member_map: dict[str, dict[str, Any]]) -> dict[str, Any]:
    allow_outside = bool(getattr(args, "allow_outside_workspace", False))
    brief_parts: list[str] = []
    brief_parts.extend(getattr(args, "brief", []) or [])
    for raw_path in getattr(args, "brief_file", []) or []:
        _, text = read_workspace_text(args.workspace, raw_path, "--brief-file", allow_outside)
        brief_parts.append(text)

    context_files = []
    for raw_path in getattr(args, "context_file", []) or []:
        path, text = read_workspace_text(args.workspace, raw_path, "--context-file", allow_outside)
        context_files.append({"path": path, "content": text})

    for raw in getattr(args, "member_context", []) or []:
        name, value = split_assignment(raw, "--member-context")
        if name not in member_map:
            raise TeamError("unknown_agent", f"{name} is not part of this team")
        member_map[name].setdefault("prompt_context", []).append(value)

    for raw in getattr(args, "member_context_file", []) or []:
        name, raw_path = split_assignment(raw, "--member-context-file")
        if name not in member_map:
            raise TeamError("unknown_agent", f"{name} is not part of this team")
        path, text = read_workspace_text(args.workspace, raw_path, "--member-context-file", allow_outside)
        member_map[name].setdefault("prompt_context", []).append(f"{path}\n{text}")

    return {
        "brief": "\n\n".join(part.strip() for part in brief_parts if part.strip()),
        "context_files": context_files,
        "quality": {
            "require_citations": bool(getattr(args, "require_citations", False)),
            "verification_checks": getattr(args, "verification_check", []) or [],
        },
    }


def member_names(state: dict[str, Any]) -> set[str]:
    return {state["lead"]["name"], *(member["name"] for member in state["members"])}


def find_member(state: dict[str, Any], name: str) -> dict[str, Any] | None:
    if state["lead"]["name"] == name:
        return state["lead"]
    for member in state["members"]:
        if member["name"] == name:
            return member
    return None


def require_member(state: dict[str, Any], name: str) -> dict[str, Any]:
    member = find_member(state, name)
    if member is None:
        raise TeamError("unknown_agent", f"{name} is not part of this team")
    return member


def require_worker_member(state: dict[str, Any], name: str) -> dict[str, Any]:
    member = require_member(state, name)
    if member is state["lead"]:
        raise TeamError("lead_not_worker", f"{name} is the lead, not a worker teammate", 2)
    return member


def require_task(state: dict[str, Any], task_id: str) -> dict[str, Any]:
    task = state["tasks"].get(task_id)
    if task is None:
        raise TeamError("unknown_task", f"Task {task_id} does not exist")
    return task


def require_bound_runtime(member: dict[str, Any]) -> dict[str, Any]:
    runtime = member_runtime(member)
    handle = runtime.get("agent_id") or runtime.get("thread_id")
    if runtime.get("backend") == "unbound" or not handle:
        raise TeamError("runtime_unbound", f"{member['name']} has no bound runtime", 2)
    return runtime


def validate_task_references(members: set[str], tasks: dict[str, Any]) -> None:
    for task in tasks.values():
        if task["owner"] not in members:
            raise TeamError("unknown_owner", f"Task {task['id']} owner {task['owner']} is not a team member")
        for dependency in task["depends_on"]:
            if dependency not in tasks:
                raise TeamError("unknown_dependency", f"Task {task['id']} depends on unknown task {dependency}")


def cmd_init(args: argparse.Namespace) -> dict[str, Any]:
    members = [parse_member(raw) for raw in args.member]
    member_map = {member["name"]: member for member in members}
    tasks = {task["id"]: task for task in (parse_task(raw) for raw in args.task)}
    if len(tasks) != len(args.task):
        raise TeamError("duplicate_task", "Task ids must be unique")
    for raw_dependency in args.depends:
        task_id, dependency_id = parse_dependency(raw_dependency)
        if task_id not in tasks:
            raise TeamError("unknown_dependency_target", f"Dependency target task {task_id} is not defined")
        tasks[task_id]["depends_on"].append(dependency_id)

    lead = {"name": args.lead, "role": "Lead coordinator", "status": "active", "current_task": None}
    names = {lead["name"], *(member["name"] for member in members)}
    if len(names) != len(members) + 1:
        raise TeamError("duplicate_member", "Lead and member names must be unique")
    prompt_options = collect_prompt_options(args, member_map)
    validate_task_references(names, tasks)

    timestamp = now_iso()
    team_id = f"{slugify(args.title)}-{timestamp.replace(':', '').replace('-', '')}"
    state = {
        "schema_version": 1,
        "team_id": team_id,
        "title": args.title,
        "brief": prompt_options["brief"],
        "context_files": prompt_options["context_files"],
        "status": "active",
        "created_at": timestamp,
        "updated_at": timestamp,
        "lead": lead,
        "members": members,
        "tasks": tasks,
        "runtime": {
            "preferred_backend": "subagent",
            "fallback_backends": ["thread", "simulated"],
        },
        "messages": [],
        "gates": {
            "plan_required": bool(args.plan_required),
            "plan_approved": not bool(args.plan_required),
            "verification_passed": False,
            "verification_evidence": [],
            "open_items": [],
        },
        "quality": prompt_options["quality"],
        "events": [event("team_created", title=args.title, lead=args.lead, members=sorted(names))],
    }
    archived = {"state": None, "backup": None}
    with state_lock(args.workspace):
        existing_state = state_path(args.workspace)
        if existing_state.exists():
            if not getattr(args, "force", False):
                raise TeamError("team_exists", f"Agent team state already exists at {existing_state}; pass --force to archive and replace it", 2)
            archived = archive_existing_state(args.workspace, "force-init")
        write_state(args.workspace, state)
    payload = {"ok": True, "team_id": team_id, "state_path": str(state_path(args.workspace))}
    if archived["state"]:
        payload["archived_state"] = archived["state"]
    if archived["backup"]:
        payload["archived_backup"] = archived["backup"]
    return payload


def dependencies_done(state: dict[str, Any], task: dict[str, Any]) -> bool:
    return all(state["tasks"][dependency]["status"] == "done" for dependency in task["depends_on"])


def ready_tasks_for(state: dict[str, Any], agent: str) -> list[dict[str, Any]]:
    ready = []
    for task in state["tasks"].values():
        if task["owner"] == agent and task["status"] == "todo" and dependencies_done(state, task):
            ready.append(task)
    return sorted(ready, key=lambda task: task["id"])


def cmd_claim(args: argparse.Namespace) -> dict[str, Any]:
    with state_transaction(args.workspace) as state:
        member = require_member(state, args.agent)
        if member.get("current_task"):
            raise TeamError("active_task", f"{args.agent} already has active task {member['current_task']}", 2)
        if getattr(args, "task", None):
            task = require_task(state, args.task)
            if task["owner"] != args.agent:
                raise TeamError("wrong_agent", f"Task {args.task} is not assigned to {args.agent}", 2)
            if not dependencies_done(state, task):
                raise TeamError("dependencies_open", f"Task {args.task} still has incomplete dependencies", 2)
            if task["status"] != "todo":
                raise TeamError("task_not_ready", f"Task {args.task} is {task['status']}, not todo", 2)
        else:
            ready = ready_tasks_for(state, args.agent)
            if not ready:
                raise TeamError("no_ready_task", f"No ready task is assigned to {args.agent}", 1)
            task = ready[0]
        timestamp = now_iso()
        task["status"] = "in_progress"
        task["claimed_by"] = args.agent
        task["claimed_at"] = timestamp
        member["status"] = "working"
        member["current_task"] = task["id"]
        state["updated_at"] = timestamp
        state["events"].append(event("task_claimed", task=task["id"], agent=args.agent))
    return {"ok": True, "task": task}


def cmd_complete(args: argparse.Namespace) -> dict[str, Any]:
    with state_transaction(args.workspace) as state:
        task = require_task(state, args.task)
        if task["owner"] != args.agent:
            raise TeamError("wrong_agent", f"Task {args.task} is not assigned to {args.agent}", 2)
        if not dependencies_done(state, task):
            raise TeamError("dependencies_open", f"Task {args.task} still has incomplete dependencies", 2)
        if task["status"] != "in_progress" or task.get("claimed_by") != args.agent:
            raise TeamError("task_not_claimed", f"Task {args.task} must be claimed by {args.agent} before completion", 2)
        if state["gates"]["plan_required"] and not state["gates"]["plan_approved"]:
            raise TeamError("plan_approval_required", "Plan approval is required before completing implementation work", 2)
        timestamp = now_iso()
        task["status"] = "done"
        task["completed_at"] = timestamp
        task["summary"] = args.summary
        outputs = task.setdefault("outputs", [])
        for raw_output in getattr(args, "output", []) or []:
            output_path = resolve_workspace_path(
                args.workspace,
                raw_output,
                "--output",
                getattr(args, "allow_outside_workspace", False),
            )
            resolved_output = str(output_path)
            if resolved_output not in outputs:
                outputs.append(resolved_output)
        member = find_member(state, args.agent)
        if member is not None and member.get("current_task") == task["id"]:
            member["current_task"] = None
            member["status"] = "idle"
        state["updated_at"] = timestamp
        state["events"].append(event("task_completed", task=task["id"], agent=args.agent, summary=args.summary))
    return {"ok": True, "task": task}


def clear_current_task_for(state: dict[str, Any], agent: str, task_id: str) -> None:
    member = find_member(state, agent)
    if member is None:
        return
    if member.get("current_task") == task_id:
        member["current_task"] = None
        member["status"] = "idle"


def cmd_cancel(args: argparse.Namespace) -> dict[str, Any]:
    with state_transaction(args.workspace) as state:
        task = require_task(state, args.task)
        if task["status"] in {"done", "canceled"}:
            raise TeamError("task_closed", f"Task {args.task} is already {task['status']}", 2)
        timestamp = now_iso()
        previous_owner = task["owner"]
        previous_claimant = task.get("claimed_by")
        clear_current_task_for(state, previous_claimant or previous_owner, task["id"])
        task["status"] = "canceled"
        task["canceled_at"] = timestamp
        task["cancel_reason"] = args.reason
        task["summary"] = args.reason
        task["claimed_by"] = None
        state["updated_at"] = timestamp
        state["events"].append(
            event("task_canceled", task=task["id"], agent=previous_owner, reason=args.reason)
        )
    return {"ok": True, "task": task}


def cmd_reassign(args: argparse.Namespace) -> dict[str, Any]:
    with state_transaction(args.workspace) as state:
        task = require_task(state, args.task)
        new_owner = require_member(state, args.agent)
        if task["status"] in {"done", "canceled"}:
            raise TeamError("task_closed", f"Task {args.task} is already {task['status']}", 2)
        timestamp = now_iso()
        previous_owner = task["owner"]
        previous_claimant = task.get("claimed_by")
        clear_current_task_for(state, previous_claimant or previous_owner, task["id"])
        task["owner"] = new_owner["name"]
        task["status"] = "todo"
        task["claimed_by"] = None
        task["claimed_at"] = None
        task["ready_notified_at"] = None
        task["reassigned_at"] = timestamp
        task["reassign_reason"] = args.reason
        state["updated_at"] = timestamp
        state["events"].append(
            event(
                "task_reassigned",
                task=task["id"],
                from_agent=previous_owner,
                to_agent=new_owner["name"],
                reason=args.reason,
            )
        )
    return {"ok": True, "task": task}


def message_body_from_args(args: argparse.Namespace) -> str:
    if args.body and args.body_file:
        raise TeamError("message_body_conflict", "Use either --body or --body-file, not both", 2)
    if args.body_file:
        path = resolve_workspace_path(args.workspace, args.body_file, "--body-file", args.allow_outside_workspace)
        return path.read_text(encoding="utf-8")
    if args.body is None:
        raise TeamError("message_body_required", "Message requires --body or --body-file", 2)
    return args.body


def cmd_message(args: argparse.Namespace) -> dict[str, Any]:
    body = message_body_from_args(args)
    with state_lock(args.workspace):
        state = read_state(args.workspace)
        names = member_names(state)
        if args.sender not in names:
            raise TeamError("unknown_sender", f"{args.sender} is not part of this team")
        recipient_target = args.to.strip()
        if recipient_target.lower() in {"all", "team", "teammates"}:
            recipients = sorted(name for name in names if name != args.sender)
        else:
            recipients = [part.strip() for part in recipient_target.split(",") if part.strip()]
        unknown = [recipient for recipient in recipients if recipient not in names]
        if unknown:
            raise TeamError("unknown_recipient", f"Unknown recipient(s): {', '.join(unknown)}")
        message = {
            "id": f"m{len(state['messages']) + 1}",
            "from": args.sender,
            "to": recipients,
            "body": body,
            "at": now_iso(),
            "read_by": [],
        }
        deliveries = add_action_ids(make_delivery_actions(state, message), "deliver_messages")
        message["delivery_status"] = {delivery["agent"]: "pending" for delivery in deliveries}
        state["messages"].append(message)
        state["updated_at"] = message["at"]
        state["events"].append(event("message_sent", message=message["id"], sender=args.sender, to=recipients))
        mark_delivery_pending(state, deliveries)
        write_state(args.workspace, state)
    return {
        "ok": True,
        "message": message,
        "deliveries": deliveries,
        "lead_action_queue": lead_action_queue(args.workspace, "message_delivery", deliveries),
    }


def parse_bool(raw: str | None) -> bool | None:
    if raw is None:
        return None
    value = raw.strip().lower()
    if value in {"true", "1", "yes", "y"}:
        return True
    if value in {"false", "0", "no", "n"}:
        return False
    raise TeamError("invalid_bool", f"Expected true or false, got {raw}")


def verification_summary_from_args(args: argparse.Namespace) -> str | None:
    summary = getattr(args, "verification_summary", None)
    summary_file = getattr(args, "verification_summary_file", None)
    if summary and summary_file:
        raise TeamError("verification_summary_conflict", "Use either --verification-summary or --verification-summary-file, not both", 2)
    if summary_file:
        path = resolve_workspace_path(
            args.workspace,
            summary_file,
            "--verification-summary-file",
            getattr(args, "allow_outside_workspace", False),
        )
        return path.read_text(encoding="utf-8")
    return summary


def cmd_gate(args: argparse.Namespace) -> dict[str, Any]:
    with state_transaction(args.workspace) as state:
        gates = state["gates"]
        plan_approved = parse_bool(args.plan_approved)
        verification_passed = parse_bool(args.verification_passed)
        if plan_approved is not None:
            gates["plan_approved"] = plan_approved
        if verification_passed is not None:
            gates["verification_passed"] = verification_passed
        if args.clear_open_items:
            gates["open_items"] = []
        for item in args.open_item:
            gates["open_items"].append({"body": item, "at": now_iso()})
        timestamp = now_iso()
        summary = verification_summary_from_args(args)
        if (
            getattr(args, "verification_command", None)
            or getattr(args, "verification_exit_code", None) is not None
            or summary is not None
        ):
            gates.setdefault("verification_evidence", []).append(
                {
                    "at": timestamp,
                    "command": getattr(args, "verification_command", None),
                    "exit_code": getattr(args, "verification_exit_code", None),
                    "summary": summary,
                }
            )
        state["updated_at"] = timestamp
        state["events"].append(event("gates_updated", gates=gates.copy()))
    return {"ok": True, "gates": gates}


def open_task_ids(state: dict[str, Any]) -> list[str]:
    return [
        task_id
        for task_id, task in state["tasks"].items()
        if task["status"] not in {"done", "canceled"}
    ]


def active_work_items(state: dict[str, Any]) -> list[str]:
    items = [task_id for task_id, task in state["tasks"].items() if task["status"] == "in_progress"]
    items.extend(member["name"] for member in state["members"] if member["status"] == "working")
    return sorted(set(items))


def runtime_cleanup_blockers(state: dict[str, Any]) -> list[str]:
    blockers = []
    for member in state["members"]:
        runtime = member_runtime(member)
        handle = runtime.get("agent_id") or runtime.get("thread_id")
        if runtime.get("backend") == "unbound" or not handle:
            continue
        if runtime.get("close_status") not in CLOSED_RUNTIME_STATUSES:
            blockers.append(member["name"])
    return sorted(blockers)


def stop_report(state: dict[str, Any]) -> dict[str, Any]:
    blocking = []
    next_actions = []
    if state.get("status") == "cleaned":
        return {"ok": True, "blocking": [], "next_actions": ["Team already cleaned up."]}

    tasks_open = open_task_ids(state)
    if tasks_open:
        blocking.append({"code": "tasks_open", "tasks": tasks_open})
        next_actions.append("Claim and complete all open tasks, or cancel tasks that are no longer needed.")
    if state["gates"]["plan_required"] and not state["gates"]["plan_approved"]:
        blocking.append({"code": "plan_approval_pending"})
        next_actions.append("Ask the lead/user to approve the implementation plan, then run gate --plan-approved true.")
    if not state["gates"]["verification_passed"]:
        blocking.append({"code": "verification_pending"})
        next_actions.append("Run the verification checklist, then run gate --verification-passed true.")
    if state["gates"]["open_items"]:
        blocking.append({"code": "open_items", "items": state["gates"]["open_items"]})
        next_actions.append("Resolve open items or clear them with gate --clear-open-items.")
    return {"ok": not blocking, "blocking": blocking, "next_actions": next_actions}


def cmd_stop_check(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    try:
        state = read_state_locked(args.workspace)
    except TeamError as exc:
        if exc.code == "no_team":
            return {"ok": True, "blocking": [], "next_actions": ["No active team state found."]}, 0
        raise
    report = stop_report(state)
    return report, 0 if report["ok"] else 2


def cmd_cleanup(args: argparse.Namespace) -> dict[str, Any]:
    with state_transaction(args.workspace) as state:
        active = active_work_items(state)
        runtime_open = runtime_cleanup_blockers(state)
        stop = stop_report(state)
        if (active or runtime_open or not stop["ok"]) and not args.force:
            details = []
            if active:
                details.append(f"active work: {', '.join(active)}")
            if runtime_open:
                details.append(f"unclosed runtimes: {', '.join(runtime_open)}")
            if not stop["ok"]:
                details.append(f"stop blockers: {json.dumps(stop['blocking'], sort_keys=True)}")
            code = "active_work" if active else "runtime_open" if runtime_open else "stop_blocked"
            raise TeamError(code, f"Refusing cleanup while {'; '.join(details)}", 2)
        ack_closed_at_cleanup = {"agents": [], "acknowledged": []}
        if getattr(args, "ack_closed", False):
            agents = closed_runtime_agents(state)
            acknowledged = []
            for agent in agents:
                acknowledged.extend(ack_messages_for_agent(state, agent))
            ack_closed_at_cleanup = {"agents": agents, "acknowledged": sorted(set(acknowledged))}
            state["events"].append(
                event(
                    "messages_acknowledged_closed",
                    agents=agents,
                    messages=ack_closed_at_cleanup["acknowledged"],
                    reason="cleanup --ack-closed",
                )
            )
        unread_at_cleanup = unread_recipient_count(state)
        timestamp = now_iso()
        state["status"] = "cleaned"
        state["cleaned_at"] = timestamp
        state["updated_at"] = timestamp
        for member in state["members"]:
            member["status"] = "idle"
            member["current_task"] = None
        state["lead"]["status"] = "idle"
        state["lead"]["current_task"] = None
        state["events"].append(
            event(
                "team_cleaned",
                forced=bool(args.force),
                active_at_cleanup=active,
                runtime_open_at_cleanup=runtime_open,
                unread_at_cleanup=unread_at_cleanup,
                ack_closed_at_cleanup=ack_closed_at_cleanup,
            )
        )
    return {
        "ok": True,
        "status": "cleaned",
        "active_at_cleanup": active,
        "runtime_open_at_cleanup": runtime_open,
        "unread_at_cleanup": unread_at_cleanup,
        "ack_closed_at_cleanup": ack_closed_at_cleanup,
    }


def cmd_status(args: argparse.Namespace) -> dict[str, Any]:
    state = read_state_locked(args.workspace)
    return {"ok": True, "state": state, "stop": stop_report(state)}


def inbox_for(state: dict[str, Any], agent: str) -> list[dict[str, Any]]:
    return [message for message in state["messages"] if agent in message["to"] and agent not in message["read_by"]]


def append_block(lines: list[str], heading: str, body: str) -> None:
    lines.extend(["", heading])
    content = body.splitlines() or [""]
    lines.extend(content)


def is_reviewer_role(member: dict[str, Any]) -> bool:
    haystack = f"{member.get('name', '')} {member.get('role', '')}".lower()
    return any(word in haystack for word in ("review", "verify", "audit", "qa", "integration"))


def build_agent_prompt(state: dict[str, Any], agent: str, workspace: str | Path = ".") -> str:
    member = find_member(state, agent)
    if member is None:
        raise TeamError("unknown_agent", f"{agent} is not part of this team")
    ready = ready_tasks_for(state, agent)
    inbox = inbox_for(state, agent)
    runtime = member.get("runtime", default_runtime())
    runtime_history = member.get("runtime_history") or []
    workspace_path = Path(workspace).resolve()
    state_file = state_path(workspace_path).resolve()
    invocation = script_invocation(workspace_path)
    current_task = member.get("current_task")
    if current_task and current_task in state.get("tasks", {}):
        current_task_label = f"{current_task} - {state['tasks'][current_task]['title']}"
    else:
        current_task_label = current_task or "-"
    prompt_lines = [
        f"You are {agent}, a teammate on Codex agent team {state['team_id']}.",
        f"Team goal: {state['title']}",
        f"Your role: {member['role']}",
        f"Runtime backend: {runtime.get('backend', 'unbound')}",
        f"Current task: {current_task_label}",
        f"Team workspace: {workspace_path}",
        f"State file: {state_file}",
        f"Team command: {invocation}",
        "Shared workspace semantics: you and the lead operate in the same filesystem workspace. Forked context is conversation context, not a separate file tree. Write outputs inside the team workspace unless the lead says otherwise.",
    ]
    if state.get("brief"):
        append_block(prompt_lines, "Team brief:", str(state["brief"]))
    context_files = state.get("context_files") or []
    if context_files:
        prompt_lines.extend(["", "Shared context files:"])
        for context in context_files:
            prompt_lines.append(f"- {context.get('path', '<unknown>')}:")
            for line in str(context.get("content", "")).splitlines() or [""]:
                prompt_lines.append(f"  {line}")
    if member.get("prompt_context"):
        prompt_lines.extend(["", "Agent-specific context:"])
        for item in member.get("prompt_context", []):
            for index, line in enumerate(str(item).splitlines() or [""]):
                prefix = "- " if index == 0 else "  "
                prompt_lines.append(f"{prefix}{line}")
    if runtime_history:
        latest = runtime_history[-1]
        append_block(
            prompt_lines,
            "Replacement context:",
            "\n".join(
                [
                    f"Previous runtime status: {latest.get('replacement_old_status') or latest.get('close_status') or latest.get('status') or 'unknown'}",
                    f"Previous runtime summary: {latest.get('replacement_old_summary') or latest.get('close_result') or latest.get('last_result') or '-'}",
                    f"Previous handle: {latest.get('agent_id') or latest.get('thread_id') or '-'}",
                    "Continue from existing state, messages, summaries, and files already present in the workspace.",
                ]
            ),
        )
    quality = state.get("quality") or {}
    checks = quality.get("verification_checks") or []
    if quality.get("require_citations") or checks or is_reviewer_role(member):
        prompt_lines.extend(["", "Citation quality:"])
        if quality.get("require_citations"):
            prompt_lines.append("- Cite concrete files, commands, source URLs, or state entries for non-obvious claims.")
        for check in checks:
            prompt_lines.append(f"- {check}")
        if is_reviewer_role(member):
            prompt_lines.extend(
                [
                    "- Reviewer checklist: verify cited paths exist, flag malformed links, and separate evidence from inference.",
                    "- Do not mark verification passed; report exact checks for the lead to run or confirm.",
                ]
            )
    prompt_lines.extend(
        [
            "",
            "Shared rules:",
            "- Read the exact state file above before acting.",
            "- Do not edit state.json directly; use the team command instead.",
            "- Run team commands with the exact --workspace path above.",
            "- Claim one ready task before doing implementation work.",
            "- Worker-safe commands are claim, complete, message, inbox, ack, ack-all, status, dashboard, and prompt.",
            "- Send concise messages for blockers, decisions, and handoffs.",
            "- Complete the task with a summary when done.",
            "- Do not run orchestrate, record-wait, record-delivery, record-close, gate, close-plan, or cleanup unless the lead explicitly asks you to.",
            "- Do not mark verification passed unless you actually ran the checks.",
            "",
            "Ready tasks:",
        ]
    )
    prompt_lines.extend(f"- {task['id']}: {task['title']}" for task in ready)
    if not ready:
        prompt_lines.append("- No ready tasks right now.")
    prompt_lines.append("")
    prompt_lines.append("Unread messages:")
    prompt_lines.extend(f"- From {message['from']}: {message['body']}" for message in inbox)
    if not inbox:
        prompt_lines.append("- None.")
    return "\n".join(prompt_lines)


def cmd_prompt(args: argparse.Namespace) -> dict[str, Any]:
    state = read_state_locked(args.workspace)
    member = find_member(state, args.agent)
    if member is None:
        raise TeamError("unknown_agent", f"{args.agent} is not part of this team")
    return {"ok": True, "agent": args.agent, "prompt": build_agent_prompt(state, args.agent, args.workspace)}


def member_runtime(member: dict[str, Any]) -> dict[str, Any]:
    member.setdefault("runtime_history", [])
    runtime = member.get("runtime")
    if not isinstance(runtime, dict):
        runtime = default_runtime()
        member["runtime"] = runtime
    else:
        for key, value in default_runtime().items():
            runtime.setdefault(key, value)
    return runtime


def bind_member_runtime(
    state: dict[str, Any],
    agent: str,
    runtime: dict[str, Any],
) -> dict[str, Any]:
    member = require_worker_member(state, agent)
    member["runtime"] = runtime
    return member


def runtime_bound(runtime: dict[str, Any]) -> bool:
    return bool(
        runtime.get("backend") != "unbound"
        and (runtime.get("agent_id") or runtime.get("thread_id"))
    )


def subagent_runtime(agent_id: str, nickname: str | None) -> dict[str, Any]:
    runtime = default_runtime()
    runtime.update(
        {
            "backend": "subagent",
            "agent_id": agent_id,
            "nickname": nickname,
            "thread_id": None,
            "bound_at": now_iso(),
            "status": "running",
        }
    )
    return runtime


def thread_runtime(thread_id: str, nickname: str | None) -> dict[str, Any]:
    runtime = default_runtime()
    runtime.update(
        {
            "backend": "thread",
            "agent_id": None,
            "nickname": nickname,
            "thread_id": thread_id,
            "bound_at": now_iso(),
            "status": "running",
        }
    )
    return runtime


def script_invocation(workspace: str | Path) -> str:
    workspace_arg = f'"{Path(workspace).resolve()}"'
    override = os.environ.get("CODEX_AGENT_TEAM_COMMAND", "").strip()
    if override:
        if "{workspace}" in override:
            return override.replace("{workspace}", workspace_arg)
        return f"{override} --workspace {workspace_arg}"
    return f'python "{Path(__file__).resolve()}" --workspace {workspace_arg}'


def make_delivery_actions(state: dict[str, Any], message: dict[str, Any]) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for recipient in message["to"]:
        member = find_member(state, recipient)
        if member is None:
            continue
        if member is state["lead"]:
            continue
        runtime = member_runtime(member)
        if runtime.get("status") in FINAL_RUNTIME_STATUSES or runtime.get("close_status") in CLOSED_RUNTIME_STATUSES:
            continue
        if runtime.get("backend") == "subagent" and runtime.get("agent_id"):
            actions.append(
                {
                    "agent": recipient,
                    "message_id": message["id"],
                    "tool": "multi_agent_v1.send_input",
                    "send_input_args": {
                        "target": runtime["agent_id"],
                        "message": f"Message from {message['from']}: {message['body']}",
                    },
                }
            )
        elif runtime.get("backend") == "thread" and runtime.get("thread_id"):
            actions.append(
                {
                    "agent": recipient,
                    "message_id": message["id"],
                    "tool": "codex_app.send_message_to_thread",
                    "send_message_to_thread_args": {
                        "threadId": runtime["thread_id"],
                        "prompt": f"Message from {message['from']}: {message['body']}",
                    },
                }
            )
    return actions


def delivery_result_skeleton(deliveries: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "results": [
            {
                "message": delivery.get("message_id", "<message_id>"),
                "agent": delivery.get("agent", "<agent>"),
                "status": "sent",
            }
            for delivery in deliveries
        ]
    }


def wait_result_skeleton(actions: list[dict[str, Any]]) -> dict[str, Any]:
    targets: list[dict[str, Any]] = []
    for action in actions:
        for target in action.get("wait_policy", {}).get("targets", []):
            targets.append(
                {
                    "target": target.get("agent_id", "<agent_id>"),
                    "status": "completed",
                    "summary": f"{target.get('agent', '<agent>')} final result",
                }
            )
    return {"results": targets}


def close_result_skeleton(actions: list[dict[str, Any]]) -> dict[str, Any]:
    results = []
    for action in actions:
        target = (
            action.get("close_agent_args", {}).get("target")
            or action.get("set_thread_archived_args", {}).get("threadId")
            or "<target>"
        )
        results.append({"target": target, "status": "closed", "summary": "closed"})
    return {"results": results}


def lead_action_queue(workspace: str | Path, phase: str, actions: list[dict[str, Any]]) -> dict[str, Any]:
    invocation = script_invocation(workspace)
    queue: dict[str, Any] = {
        "phase": phase,
        "host_tool_actions": actions,
        "expected_dashboard_state": "Run the matching record command, then rerun dashboard or orchestrate.",
    }
    if phase in {"deliver_messages", "message_delivery"}:
        queue["matching_record_command"] = f"{invocation} record-delivery-batch --result-file <delivery_results.json>"
        queue["result_file_skeleton"] = delivery_result_skeleton(actions)
    elif phase == "wait_agents":
        queue["matching_record_command"] = f"{invocation} record-wait-batch --result-file <wait_results.json>"
        queue["result_file_skeleton"] = wait_result_skeleton(actions)
    elif phase == "close_ready":
        queue["matching_record_command"] = f"{invocation} record-close-batch --result-file <close_results.json>"
        queue["result_file_skeleton"] = close_result_skeleton(actions)
    elif phase == "spawn_unbound":
        queue["matching_record_commands"] = [action.get("record_command") for action in actions if action.get("record_command")]
        queue["expected_dashboard_state"] = "Bound teammates should no longer appear as unbound after bind-subagent/bind-thread recording."
    elif phase == "wake_ready":
        queue["matching_record_command"] = f"{invocation} wake-plan --mark"
        queue["expected_dashboard_state"] = "Ready tasks should have ready_notified_at set after marking the wake plan."
    elif phase == "finalize_overdue":
        queue["matching_record_command"] = f"{invocation} record-wait-batch --result-file <wait_results.json>"
        queue["result_file_skeleton"] = {"results": [{"target": "<agent_id>", "status": "completed", "summary": "<final response>"}]}
    return queue


def mark_delivery_pending(state: dict[str, Any], deliveries: list[dict[str, Any]]) -> None:
    for delivery in deliveries:
        agent = delivery.get("agent")
        if not agent:
            continue
        member = find_member(state, agent)
        if member is None:
            continue
        runtime = member_runtime(member)
        runtime["pending_inputs"] = int(runtime.get("pending_inputs") or 0) + 1
        if runtime.get("status") not in {"completed", "shutdown", "errored"}:
            runtime["status"] = "input_sent"


def reconcile_message_ack(state: dict[str, Any], message: dict[str, Any], agent: str) -> None:
    delivery_status = message.setdefault("delivery_status", {})
    previous_status = delivery_status.get(agent)
    if previous_status is not None:
        delivery_status[agent] = "read"
    if previous_status not in {"pending", "sent"}:
        return
    member = find_member(state, agent)
    if member is None:
        return
    runtime = member_runtime(member)
    runtime["pending_inputs"] = max(0, int(runtime.get("pending_inputs") or 0) - 1)
    if runtime["pending_inputs"] == 0 and runtime.get("status") == "input_sent":
        runtime["status"] = "running"


def pending_delivery_actions(state: dict[str, Any]) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for message in state.get("messages", []):
        delivery_status = message.get("delivery_status") or {}
        read_by = set(message.get("read_by") or [])
        pending = [agent for agent, status in delivery_status.items() if status == "pending" and agent not in read_by]
        if not pending:
            continue
        pending_message = message.copy()
        pending_message["to"] = pending
        actions.extend(make_delivery_actions(state, pending_message))
    return actions


def launch_actions_for_state(
    state: dict[str, Any],
    workspace: str | Path,
    backend: str,
    agents: set[str] | None = None,
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for member in state["members"]:
        if agents is not None and member["name"] not in agents:
            continue
        runtime = member_runtime(member)
        if runtime.get("backend") != "unbound":
            continue
        agent = member["name"]
        prompt = build_agent_prompt(state, agent, workspace)
        if backend == "subagent":
            record_command = (
                f"{script_invocation(workspace)} bind-subagent --agent {agent} "
                "--agent-id <agent_id> --nickname <nickname>"
            )
            actions.append(
                {
                    "agent": agent,
                    "tool": "multi_agent_v1.spawn_agent",
                    "spawn_args": {
                        "fork_context": True,
                        "message": (
                            prompt
                            + "\n\nYou are running as a Codex subagent child. "
                            "Use the shared team state through the script and return when your task is complete."
                        ),
                    },
                    "spawn_warning": (
                        "Use spawn_args exactly as emitted. Do not add agent_type, model, or reasoning overrides "
                        "when fork_context=true unless the host tool schema explicitly supports them."
                    ),
                    "record_command": record_command,
                }
            )
        elif backend == "thread":
            actions.append(
                {
                    "agent": agent,
                    "tool": "codex_app.fork_thread",
                    "fork_thread_args": {"environment": {"type": "same-directory"}},
                    "follow_up_prompt": prompt,
                }
            )
        else:
            actions.append({"agent": agent, "tool": "manual_packet", "prompt": prompt})
    return actions


def open_tasks_for_member(state: dict[str, Any], agent: str) -> list[dict[str, Any]]:
    return [
        task
        for task in state["tasks"].values()
        if task["owner"] == agent and task["status"] not in {"done", "canceled"}
    ]


def spawn_agents_for_policy(state: dict[str, Any], policy: str) -> tuple[set[str], list[str]]:
    selected: set[str] = set()
    skipped_blocked = []
    for member in state["members"]:
        runtime = member_runtime(member)
        if runtime.get("backend") != "unbound":
            continue
        open_tasks = open_tasks_for_member(state, member["name"])
        if policy == "all":
            selected.add(member["name"])
        elif policy == "open":
            if open_tasks:
                selected.add(member["name"])
        elif policy == "ready-only":
            has_ready = any(task["status"] == "todo" and dependencies_done(state, task) for task in open_tasks)
            if has_ready:
                selected.add(member["name"])
            elif open_tasks:
                skipped_blocked.append(member["name"])
        else:
            raise TeamError("invalid_spawn_policy", f"Unknown spawn policy {policy}", 2)
    return selected, skipped_blocked


def make_launch_plan(workspace: str | Path, backend: str, spawn_policy: str = "all") -> dict[str, Any]:
    state = read_state_locked(workspace)
    selected, skipped_blocked_spawns = spawn_agents_for_policy(state, spawn_policy)
    actions = launch_actions_for_state(state, workspace, backend, selected)
    return {
        "ok": True,
        "backend": backend,
        "spawn_policy": spawn_policy,
        "skipped_blocked_spawns": skipped_blocked_spawns,
        "actions": actions,
    }


def cmd_launch_plan(args: argparse.Namespace) -> dict[str, Any]:
    return make_launch_plan(args.workspace, args.backend, args.spawn_policy)


def cmd_launch(args: argparse.Namespace) -> dict[str, Any]:
    init_payload = cmd_init(args)
    return {**init_payload, "launch": make_launch_plan(args.workspace, args.backend, args.spawn_policy)}


def cmd_bind_subagent(args: argparse.Namespace) -> dict[str, Any]:
    with state_lock(args.workspace):
        state = read_state(args.workspace)
        existing_member = require_worker_member(state, args.agent)
        existing_runtime = member_runtime(existing_member)
        if runtime_bound(existing_runtime):
            raise TeamError(
                "runtime_already_bound",
                f"{args.agent} already has a runtime; use replace-subagent to archive and replace it",
                2,
            )
        runtime = subagent_runtime(args.agent_id, args.nickname)
        member = bind_member_runtime(state, args.agent, runtime)
        state["updated_at"] = runtime["bound_at"]
        state["events"].append(event("subagent_bound", agent=args.agent, agent_id=args.agent_id, nickname=args.nickname))
        write_state(args.workspace, state)
    return {"ok": True, "member": member}


def cmd_replace_subagent(args: argparse.Namespace) -> dict[str, Any]:
    with state_lock(args.workspace):
        state = read_state(args.workspace)
        member = require_worker_member(state, args.agent)
        old_runtime = member_runtime(member)
        if not runtime_bound(old_runtime):
            raise TeamError("runtime_unbound", f"{args.agent} has no runtime to replace", 2)
        if not args.old_status or not args.old_summary:
            raise TeamError(
                "old_runtime_status_required",
                "Replacement requires --old-status and --old-summary for the runtime being replaced",
                2,
            )
        timestamp = now_iso()
        snapshot = dict(old_runtime)
        snapshot.update(
            {
                "replaced_at": timestamp,
                "replacement_old_status": args.old_status,
                "replacement_old_summary": args.old_summary,
            }
        )
        member.setdefault("runtime_history", []).append(snapshot)
        runtime = subagent_runtime(args.agent_id, args.nickname)
        runtime["bound_at"] = timestamp
        member["runtime"] = runtime
        state["updated_at"] = timestamp
        state["events"].append(
            event(
                "runtime_replaced",
                agent=args.agent,
                old_agent_id=old_runtime.get("agent_id"),
                new_agent_id=args.agent_id,
                old_status=args.old_status,
                old_summary=args.old_summary,
            )
        )
        write_state(args.workspace, state)
    return {"ok": True, "member": member}


def cmd_bind_thread(args: argparse.Namespace) -> dict[str, Any]:
    with state_lock(args.workspace):
        state = read_state(args.workspace)
        existing_member = require_worker_member(state, args.agent)
        existing_runtime = member_runtime(existing_member)
        if runtime_bound(existing_runtime):
            raise TeamError(
                "runtime_already_bound",
                f"{args.agent} already has a runtime; use replace-subagent for subagent replacement or close/archive first",
                2,
            )
        runtime = thread_runtime(args.thread_id, args.nickname)
        member = bind_member_runtime(state, args.agent, runtime)
        state["updated_at"] = runtime["bound_at"]
        state["events"].append(event("thread_bound", agent=args.agent, thread_id=args.thread_id, nickname=args.nickname))
        write_state(args.workspace, state)
    return {"ok": True, "member": member}


FINAL_RUNTIME_STATUSES = {"completed", "shutdown", "errored"}
ACTIVE_RUNTIME_STATUSES = {"running", "input_sent"}
CLOSED_RUNTIME_STATUSES = {"closed", "not_found", "archived"}


def wait_summary_from_args(args: argparse.Namespace) -> str | None:
    if args.summary and args.summary_file:
        raise TeamError("wait_summary_conflict", "Use either --summary or --summary-file, not both", 2)
    if args.summary_file:
        path = resolve_workspace_path(args.workspace, args.summary_file, "--summary-file", args.allow_outside_workspace)
        return path.read_text(encoding="utf-8")
    return args.summary


def close_summary_from_args(args: argparse.Namespace) -> str | None:
    if args.summary and args.summary_file:
        raise TeamError("close_summary_conflict", "Use either --summary or --summary-file, not both", 2)
    if args.summary_file:
        path = resolve_workspace_path(args.workspace, args.summary_file, "--summary-file", args.allow_outside_workspace)
        return path.read_text(encoding="utf-8")
    return args.summary


def apply_wait_record(
    state: dict[str, Any],
    member: dict[str, Any],
    agent: str,
    status: str,
    summary: str | None,
    timestamp: str,
) -> dict[str, Any]:
    runtime = member_runtime(member)
    previous_status = runtime.get("status")
    ignored_status = None
    effective_status = status
    if previous_status in FINAL_RUNTIME_STATUSES and status not in FINAL_RUNTIME_STATUSES:
        ignored_status = status
        effective_status = previous_status
    runtime["last_wait_at"] = timestamp
    runtime["status"] = effective_status
    if summary is not None and ignored_status is None:
        runtime["last_result"] = summary
    if effective_status in FINAL_RUNTIME_STATUSES:
        runtime["pending_inputs"] = 0
        runtime["wait_timeouts"] = 0
        runtime["finalize_attempts"] = 0
    elif effective_status == "timed_out":
        runtime["wait_timeouts"] = int(runtime.get("wait_timeouts") or 0) + 1
    wait_event = event(
        "wait_recorded",
        agent=agent,
        status=effective_status,
        pending_inputs=runtime.get("pending_inputs", 0),
        wait_timeouts=runtime.get("wait_timeouts", 0),
    )
    if ignored_status is not None:
        wait_event["ignored_status"] = ignored_status
    state["events"].append(wait_event)
    return {"agent": agent, "status": effective_status, "summary": summary, "ignored_status": ignored_status}


def cmd_record_wait(args: argparse.Namespace) -> dict[str, Any]:
    with state_transaction(args.workspace) as state:
        member = require_worker_member(state, args.agent)
        timestamp = now_iso()
        summary = wait_summary_from_args(args)
        record = apply_wait_record(state, member, args.agent, args.status, summary, timestamp)
        state["updated_at"] = timestamp
    return {"ok": True, "member": member, "ignored_status": record["ignored_status"]}


def normalize_wait_status(raw: Any) -> str:
    value = str(raw or "").strip().lower().replace("-", "_")
    aliases = {
        "completed": {"completed", "succeeded", "success", "done", "finished"},
        "running": {"running", "active", "in_progress", "pending"},
        "timed_out": {"timed_out", "timeout", "timedout"},
        "shutdown": {"shutdown", "closed", "cancelled", "canceled", "stopped"},
        "errored": {"errored", "error", "failed", "failure"},
    }
    for canonical, values in aliases.items():
        if value in values:
            return canonical
    raise TeamError("unknown_wait_status", f"Could not map wait status {raw!r}", 2)


def wait_summary_from_item(item: dict[str, Any]) -> str | None:
    for key in ("summary", "result", "output", "message", "text"):
        if key in item and item[key] is not None:
            value = item[key]
            return value if isinstance(value, str) else json.dumps(value, sort_keys=True)
    return None


def wait_target_from_item(item: dict[str, Any]) -> str | None:
    for key in ("target", "agent_id", "id", "target_id"):
        if item.get(key):
            return str(item[key])
    return None


def wait_items_from_payload(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        raise TeamError("invalid_wait_result", "Wait result JSON must be an object or list", 2)
    for key in ("results", "agents", "targets", "statuses"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            return [
                {"target": target, **record}
                for target, record in value.items()
                if isinstance(record, dict)
            ]
    return [
        {"target": target, **record}
        for target, record in payload.items()
        if isinstance(record, dict)
    ]


def cmd_record_wait_batch(args: argparse.Namespace) -> dict[str, Any]:
    result_path = resolve_workspace_path(args.workspace, args.result_file, "--result-file", args.allow_outside_workspace)
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    with state_transaction(args.workspace) as state:
        runtime_by_target = {}
        for member in state["members"]:
            runtime = member_runtime(member)
            if runtime.get("agent_id"):
                runtime_by_target[str(runtime["agent_id"])] = member
        records = []
        unmatched = []
        timestamp = now_iso()
        for item in wait_items_from_payload(payload):
            target = wait_target_from_item(item)
            if not target or target not in runtime_by_target:
                unmatched.append(item)
                continue
            member = runtime_by_target[target]
            status = normalize_wait_status(item.get("status", item.get("state")))
            summary = wait_summary_from_item(item)
            records.append(apply_wait_record(state, member, member["name"], status, summary, timestamp) | {"target": target})
        if records:
            state["updated_at"] = timestamp
            state["events"].append(event("wait_batch_recorded", records=len(records), unmatched=len(unmatched)))
    return {"ok": True, "records": records, "unmatched": unmatched}


def normalize_close_status(raw: Any) -> str:
    value = str(raw or "").strip().lower().replace("-", "_")
    aliases = {
        "closed": {"closed", "close", "shutdown", "shut_down", "success", "succeeded", "done"},
        "not_found": {"not_found", "notfound", "missing", "gone", "already_closed"},
        "archived": {"archived", "archive"},
        "failed": {"failed", "failure", "errored", "error"},
    }
    for canonical, values in aliases.items():
        if value in values:
            return canonical
    raise TeamError("unknown_close_status", f"Could not map close status {raw!r}", 2)


def close_summary_from_item(item: dict[str, Any]) -> str | None:
    for key in ("summary", "result", "output", "message", "text"):
        if key in item and item[key] is not None:
            value = item[key]
            return value if isinstance(value, str) else json.dumps(value, sort_keys=True)
    return None


def close_target_from_item(item: dict[str, Any]) -> str | None:
    for key in ("target", "agent_id", "thread_id", "id", "target_id"):
        if item.get(key):
            return str(item[key])
    return None


def close_items_from_payload(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        raise TeamError("invalid_close_result", "Close result JSON must be an object or list", 2)
    for key in ("results", "agents", "targets", "statuses", "closed"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            return [
                {"target": target, **record}
                for target, record in value.items()
                if isinstance(record, dict)
            ]
    return [
        {"target": target, **record}
        for target, record in payload.items()
        if isinstance(record, dict)
    ]


def find_message(state: dict[str, Any], message_id: str) -> dict[str, Any] | None:
    for message in state.get("messages", []):
        if message.get("id") == message_id:
            return message
    return None


def normalize_delivery_status(raw: Any) -> str:
    value = str(raw or "").strip().lower().replace("-", "_")
    aliases = {
        "sent": {"sent", "delivered", "success", "succeeded", "ok"},
        "failed": {"failed", "failure", "errored", "error"},
        "read": {"read", "acked", "acknowledged"},
    }
    for canonical, values in aliases.items():
        if value in values:
            return canonical
    raise TeamError("unknown_delivery_status", f"Could not map delivery status {raw!r}", 2)


def apply_delivery_record(
    state: dict[str, Any],
    message_id: str,
    agent: str,
    status: str,
    timestamp: str,
) -> dict[str, Any]:
    target_message = find_message(state, message_id)
    if target_message is None:
        raise TeamError("unknown_message", f"{message_id} is not a message in this team")
    if agent not in target_message.get("to", []):
        raise TeamError("unknown_delivery_recipient", f"{agent} is not a recipient of {message_id}")
    delivery_status = target_message.setdefault("delivery_status", {})
    if agent not in delivery_status:
        raise TeamError("delivery_not_tracked", f"{message_id} has no host-tool delivery for {agent}")
    member = require_worker_member(state, agent)
    runtime = member_runtime(member)
    previous_status = delivery_status.get(agent)
    read_by = set(target_message.get("read_by") or [])
    ignored_status = None
    effective_status = normalize_delivery_status(status)
    if effective_status == "failed" and (previous_status in {"sent", "read"} or agent in read_by):
        ignored_status = effective_status
        effective_status = "read" if agent in read_by or previous_status == "read" else "sent"
    if effective_status == "read" and agent not in target_message["read_by"]:
        target_message["read_by"].append(agent)
        target_message["read_by"].sort()
    delivery_status[agent] = effective_status
    if effective_status == "sent":
        if int(runtime.get("pending_inputs") or 0) == 0:
            runtime["pending_inputs"] = 1
        if runtime.get("status") not in {"shutdown", "errored"}:
            runtime["status"] = "input_sent"
    elif effective_status == "failed":
        runtime["pending_inputs"] = max(0, int(runtime.get("pending_inputs") or 0) - 1)
    elif effective_status == "read":
        runtime["pending_inputs"] = max(0, int(runtime.get("pending_inputs") or 0) - 1)
        if runtime["pending_inputs"] == 0 and runtime.get("status") == "input_sent":
            runtime["status"] = "running"
    delivery_event = event(
        "delivery_recorded",
        message=message_id,
        agent=agent,
        status=effective_status,
        pending_inputs=runtime.get("pending_inputs", 0),
    )
    if ignored_status is not None:
        delivery_event["ignored_status"] = ignored_status
    state["events"].append(delivery_event)
    return {
        "message": target_message,
        "member": member,
        "agent": agent,
        "message_id": message_id,
        "status": effective_status,
        "ignored_status": ignored_status,
    }


def cmd_record_delivery(args: argparse.Namespace) -> dict[str, Any]:
    with state_transaction(args.workspace) as state:
        timestamp = now_iso()
        record = apply_delivery_record(state, args.message, args.agent, args.status, timestamp)
        state["updated_at"] = timestamp
    return {
        "ok": True,
        "message": record["message"],
        "member": record["member"],
        "ignored_status": record["ignored_status"],
    }


def delivery_items_from_payload(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        raise TeamError("invalid_delivery_result", "Delivery result JSON must be an object or list", 2)
    for key in ("results", "deliveries", "messages", "statuses"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            return [
                {"message": message_id, **record}
                for message_id, record in value.items()
                if isinstance(record, dict)
            ]
    return [
        {"message": message_id, **record}
        for message_id, record in payload.items()
        if isinstance(record, dict)
    ]


def delivery_message_from_item(item: dict[str, Any]) -> str | None:
    for key in ("message", "message_id", "id"):
        if item.get(key):
            return str(item[key])
    return None


def cmd_record_delivery_batch(args: argparse.Namespace) -> dict[str, Any]:
    result_path = resolve_workspace_path(args.workspace, args.result_file, "--result-file", args.allow_outside_workspace)
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    with state_transaction(args.workspace) as state:
        records = []
        unmatched = []
        timestamp = now_iso()
        for item in delivery_items_from_payload(payload):
            message_id = delivery_message_from_item(item)
            agent = str(item.get("agent") or item.get("recipient") or "").strip()
            status = item.get("status", item.get("state"))
            if not message_id or not agent or status is None:
                unmatched.append(item)
                continue
            try:
                records.append(apply_delivery_record(state, message_id, agent, str(status), timestamp))
            except TeamError as exc:
                if exc.code in {"unknown_message", "unknown_delivery_recipient", "delivery_not_tracked", "unknown_agent", "lead_not_worker"}:
                    unmatched.append(item)
                    continue
                raise
        if records:
            state["updated_at"] = timestamp
            state["events"].append(event("delivery_batch_recorded", records=len(records), unmatched=len(unmatched)))
    return {"ok": True, "records": records, "unmatched": unmatched}


def apply_close_record(
    state: dict[str, Any],
    member: dict[str, Any],
    agent: str,
    status: str,
    summary: str | None,
    timestamp: str,
) -> dict[str, Any]:
    runtime = member_runtime(member)
    runtime["close_status"] = status
    runtime["close_result"] = summary
    if status in CLOSED_RUNTIME_STATUSES:
        runtime["closed_at"] = timestamp
        runtime["status"] = "shutdown"
        runtime["pending_inputs"] = 0
    elif status == "failed":
        runtime["status"] = "errored"
    state["events"].append(
        event(
            "close_recorded",
            agent=agent,
            status=status,
            runtime_status=runtime.get("status"),
            result=summary,
        )
    )
    return {"agent": agent, "status": status, "summary": summary}


def cmd_record_close(args: argparse.Namespace) -> dict[str, Any]:
    with state_transaction(args.workspace) as state:
        member = require_worker_member(state, args.agent)
        runtime = require_bound_runtime(member)
        timestamp = now_iso()
        summary = close_summary_from_args(args)
        state["updated_at"] = timestamp
        apply_close_record(state, member, args.agent, args.status, summary, timestamp)
    return {"ok": True, "member": member}


def cmd_record_close_batch(args: argparse.Namespace) -> dict[str, Any]:
    result_path = resolve_workspace_path(args.workspace, args.result_file, "--result-file", args.allow_outside_workspace)
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    with state_transaction(args.workspace) as state:
        runtime_by_target = {}
        for member in state["members"]:
            runtime = member_runtime(member)
            if runtime.get("agent_id"):
                runtime_by_target[str(runtime["agent_id"])] = member
            if runtime.get("thread_id"):
                runtime_by_target[str(runtime["thread_id"])] = member
        records = []
        unmatched = []
        timestamp = now_iso()
        for item in close_items_from_payload(payload):
            target = close_target_from_item(item)
            if not target or target not in runtime_by_target:
                unmatched.append(item)
                continue
            member = runtime_by_target[target]
            status = normalize_close_status(item.get("status", item.get("state")))
            summary = close_summary_from_item(item)
            records.append(apply_close_record(state, member, member["name"], status, summary, timestamp) | {"target": target})
        if records:
            state["updated_at"] = timestamp
            state["events"].append(event("close_batch_recorded", records=len(records), unmatched=len(unmatched)))
    return {"ok": True, "records": records, "unmatched": unmatched}


def close_actions_for_state(state: dict[str, Any], workspace: str | Path) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for member in state["members"]:
        runtime = member_runtime(member)
        if runtime.get("close_status") in CLOSED_RUNTIME_STATUSES:
            continue
        record_command = (
            f"{script_invocation(workspace)} record-close --agent {member['name']} "
            "--status closed --summary <close_result>"
        )
        if runtime.get("backend") == "subagent" and runtime.get("agent_id"):
            actions.append(
                {
                    "agent": member["name"],
                    "tool": "multi_agent_v1.close_agent",
                    "close_agent_args": {"target": runtime["agent_id"]},
                    "record_command": record_command,
                }
            )
        elif runtime.get("backend") == "thread" and runtime.get("thread_id"):
            actions.append(
                {
                    "agent": member["name"],
                    "tool": "codex_app.set_thread_archived",
                    "set_thread_archived_args": {"threadId": runtime["thread_id"], "archived": True},
                    "record_command": (
                        f"{script_invocation(workspace)} record-close --agent {member['name']} "
                        "--status archived --summary <archive_result>"
                    ),
                }
            )
    return actions


def cmd_close_plan(args: argparse.Namespace) -> dict[str, Any]:
    state = read_state_locked(args.workspace)
    actions = close_actions_for_state(state, args.workspace)
    return {
        "ok": True,
        "actions": actions,
        "lead_action_queue": lead_action_queue(args.workspace, "close_ready", actions),
    }


def add_action_ids(actions: list[dict[str, Any]], phase: str) -> list[dict[str, Any]]:
    with_ids = []
    for index, action in enumerate(actions, start=1):
        action_copy = action.copy()
        agent = action_copy.get("agent", "team")
        task = action_copy.get("task")
        if phase == "spawn_unbound":
            action_id = f"spawn:{agent}"
        elif phase == "wake_ready" and task:
            action_id = f"wake:{task}:{agent}"
        elif phase == "deliver_messages":
            action_id = f"deliver:{action_copy.get('message_id', 'message')}:{agent}"
        elif phase == "close_ready":
            action_id = f"close:{agent}"
        elif phase == "finalize":
            action_id = f"finalize:{agent}"
        elif phase == "wait":
            action_id = "wait:subagents"
        else:
            action_id = f"{phase}:{index}"
        action_copy["action_id"] = action_id
        with_ids.append(action_copy)
    return with_ids


def bound_subagent_members(state: dict[str, Any]) -> list[dict[str, Any]]:
    members = []
    for member in state["members"]:
        runtime = member_runtime(member)
        if runtime.get("backend") == "subagent" and runtime.get("agent_id"):
            members.append(member)
    return members


def waiting_members(state: dict[str, Any]) -> list[dict[str, Any]]:
    waiting = []
    for member in bound_subagent_members(state):
        runtime = member_runtime(member)
        pending = int(runtime.get("pending_inputs") or 0)
        status = runtime.get("status", "unknown")
        if status in ACTIVE_RUNTIME_STATUSES or status == "timed_out" or pending > 0:
            waiting.append(member)
    return waiting


def task_label_for_member(state: dict[str, Any], member: dict[str, Any]) -> str:
    current_task = member.get("current_task")
    if current_task and current_task in state.get("tasks", {}):
        task = state["tasks"][current_task]
        return f"{task['id']} - {task['title']}"
    for task in state.get("tasks", {}).values():
        if task["owner"] == member["name"] and task["status"] in {"in_progress", "todo"}:
            return f"{task['id']} - {task['title']}"
    return "-"


def finalize_actions_for(state: dict[str, Any], members: list[dict[str, Any]]) -> list[dict[str, Any]]:
    actions = []
    for member in members:
        runtime = member_runtime(member)
        if runtime.get("status") != "timed_out":
            continue
        attempts = int(runtime.get("finalize_attempts") or 0)
        if attempts >= MAX_FINALIZE_ATTEMPTS:
            continue
        next_attempt = attempts + 1
        task_label = task_label_for_member(state, member)
        last_status = runtime.get("status", "unknown")
        last_result = runtime.get("last_result") or "-"
        actions.append(
            {
                "agent": member["name"],
                "tool": "multi_agent_v1.send_input",
                "finalize_attempt": next_attempt,
                "send_input_args": {
                    "target": runtime["agent_id"],
                    "message": (
                        f"Finalization attempt {next_attempt}/{MAX_FINALIZE_ATTEMPTS}: "
                        f"Current task: {task_label}. "
                        f"Last wait result/status: {last_status} - {last_result}. "
                        "Please provide your final response now with this exact concise shape: "
                        "Completed; Blockers; Remaining recommendation. Keep it under 200 words."
                    ),
                },
            }
        )
    return actions


def record_finalize_attempts(workspace: str | Path, actions: list[dict[str, Any]]) -> None:
    if not actions:
        return
    with state_lock(workspace):
        state = read_state(workspace)
        for action in actions:
            member = find_member(state, action["agent"])
            if member is None:
                continue
            runtime = member_runtime(member)
            runtime["finalize_attempts"] = max(
                int(runtime.get("finalize_attempts") or 0),
                int(action.get("finalize_attempt") or 0),
            )
            state["events"].append(
                event("finalize_attempted", agent=action["agent"], attempts=runtime["finalize_attempts"])
            )
        state["updated_at"] = now_iso()
        write_state(workspace, state)


def finalize_escalation(workspace: str | Path, members: list[dict[str, Any]]) -> dict[str, Any] | None:
    exhausted = []
    for member in members:
        runtime = member_runtime(member)
        if runtime.get("status") == "timed_out" and int(runtime.get("finalize_attempts") or 0) >= MAX_FINALIZE_ATTEMPTS:
            exhausted.append(member["name"])
    if not exhausted:
        return None
    with state_lock(workspace):
        state = read_state(workspace)
        existing = {item.get("body") for item in state["gates"].get("open_items", [])}
        for agent in exhausted:
            body = f"finalize attempts exhausted for {agent}; lead must inspect or close manually"
            if body not in existing:
                state["gates"]["open_items"].append({"body": body, "at": now_iso()})
        state["updated_at"] = now_iso()
        state["events"].append(event("finalize_escalated", agents=exhausted))
        write_state(workspace, state)
    return {
        "ok": True,
        "phase": "finalize_escalated",
        "actions": [],
        "escalated_agents": exhausted,
        "next": "Finalize attempts are exhausted. Inspect the listed agents, record-wait a final status, or close manually.",
    }


def wait_actions_for(members: list[dict[str, Any]], timeout_ms: int) -> list[dict[str, Any]]:
    target_records = [
        {
            "agent": member["name"],
            "agent_id": member_runtime(member)["agent_id"],
            "runtime_status": member_runtime(member).get("status", "unknown"),
            "pending_inputs": int(member_runtime(member).get("pending_inputs") or 0),
        }
        for member in members
    ]
    targets = [record["agent_id"] for record in target_records]
    if not targets:
        return []
    return [
        {
            "agent": "team",
            "tool": "multi_agent_v1.wait_agent",
            "wait_agent_args": {"targets": targets, "timeout_ms": timeout_ms},
            "wait_policy": {
                "mode": "all_targets",
                "targets": target_records,
                "require_final_statuses": True,
                "final_statuses": sorted(FINAL_RUNTIME_STATUSES),
                "instruction": (
                    "Loop wait_agent until every listed target has a final status, "
                    "then save the raw wait_agent output to JSON and run record-wait-batch. "
                    "Do not advance after a single partial result."
                ),
            },
            "result_file_example": {
                "results": [
                    {"target": record["agent_id"], "status": "completed", "summary": f"{record['agent']} final result"}
                    for record in target_records
                ]
            },
        }
    ]


def cmd_orchestrate(args: argparse.Namespace) -> dict[str, Any]:
    state = read_state_locked(args.workspace)
    spawn_agents, skipped_blocked_spawns = spawn_agents_for_policy(state, args.spawn_policy)
    spawn_actions = launch_actions_for_state(state, args.workspace, args.backend, spawn_agents)
    if spawn_actions:
        actions = add_action_ids(spawn_actions, "spawn_unbound")
        return {
            "ok": True,
            "phase": "spawn_unbound",
            "spawn_policy": args.spawn_policy,
            "skipped_blocked_spawns": skipped_blocked_spawns,
            "actions": actions,
            "lead_action_queue": lead_action_queue(args.workspace, "spawn_unbound", actions),
            "next": "Execute spawn_agent actions, then bind returned agent_id values with each record_command.",
        }

    delivery_actions = pending_delivery_actions(state)
    if delivery_actions:
        actions = add_action_ids(delivery_actions, "deliver_messages")
        return {
            "ok": True,
            "phase": "deliver_messages",
            "actions": actions,
            "lead_action_queue": lead_action_queue(args.workspace, "deliver_messages", actions),
            "next": "Execute send_input delivery actions, record them with record-delivery-batch, then run orchestrate again to wait for responses.",
        }

    finalize_actions = finalize_actions_for(state, waiting_members(state))
    if finalize_actions:
        actions = add_action_ids(finalize_actions, "finalize")
        record_finalize_attempts(args.workspace, finalize_actions)
        return {
            "ok": True,
            "phase": "finalize_overdue",
            "actions": actions,
            "lead_action_queue": lead_action_queue(args.workspace, "finalize_overdue", actions),
            "next": "Execute send_input finalization prompts, then run wait_agent and record results with record-wait-batch.",
        }
    escalation = finalize_escalation(args.workspace, waiting_members(state))
    if escalation:
        return escalation

    wake_actions = wake_actions_for_state(state)
    if wake_actions:
        if args.mark_wake:
            with state_transaction(args.workspace) as marked_state:
                marked_actions = wake_actions_for_state(marked_state)
                timestamp = now_iso()
                mark_wake_actions(marked_state, marked_actions, timestamp)
                marked_state["updated_at"] = timestamp
                marked_state["events"].append(event("wake_plan_marked", actions=len(marked_actions)))
            wake_actions = marked_actions
        actions = add_action_ids(wake_actions, "wake_ready")
        return {
            "ok": True,
            "phase": "wake_ready",
            "actions": actions,
            "lead_action_queue": lead_action_queue(args.workspace, "wake_ready", actions),
            "next": "Execute send_input actions. Use --mark-wake after delivery if you did not pass --mark-wake here.",
        }

    waiting = waiting_members(state)
    finalize_actions = finalize_actions_for(state, waiting)
    if finalize_actions:
        actions = add_action_ids(finalize_actions, "finalize")
        record_finalize_attempts(args.workspace, finalize_actions)
        return {
            "ok": True,
            "phase": "finalize_overdue",
            "actions": actions,
            "lead_action_queue": lead_action_queue(args.workspace, "finalize_overdue", actions),
            "next": "Execute send_input finalization prompts, then run wait_agent and record results with record-wait-batch.",
        }
    escalation = finalize_escalation(args.workspace, waiting)
    if escalation:
        return escalation
    if waiting:
        waiting_names = [member["name"] for member in waiting]
        actions = add_action_ids(wait_actions_for(waiting, args.timeout_ms), "wait")
        return {
            "ok": True,
            "phase": "wait_agents",
            "wait_all_required": True,
            "waiting_agents": waiting_names,
            "actions": actions,
            "lead_action_queue": lead_action_queue(args.workspace, "wait_agents", actions),
            "next": (
                "Execute wait_agent in a loop until every waiting agent reaches a final status, "
                "then record the result file with record-wait-batch before running orchestrate again."
            ),
        }

    stop = stop_report(state)
    if stop["ok"]:
        close_actions = close_actions_for_state(state, args.workspace)
        if not close_actions:
            return {
                "ok": True,
                "phase": "cleanup_ready",
                "actions": [],
                "stop": stop,
                "next": "All runtimes are recorded closed. Run cleanup to mark the team state cleaned.",
            }
        actions = add_action_ids(close_actions, "close_ready")
        return {
            "ok": True,
            "phase": "close_ready",
            "actions": actions,
            "lead_action_queue": lead_action_queue(args.workspace, "close_ready", actions),
            "stop": stop,
            "next": "Execute close_agent actions, record them with record-close-batch, then run cleanup when no agents remain open.",
        }

    return {
        "ok": True,
        "phase": "work_incomplete",
        "actions": [],
        "spawn_policy": args.spawn_policy,
        "skipped_blocked_spawns": skipped_blocked_spawns,
        "stop": stop,
        "dashboard": dashboard_text(state, args.workspace),
        "next": "Continue worker tasks, route messages, resolve gates, then run orchestrate again.",
    }


def runtime_warning(runtime: dict[str, Any]) -> str | None:
    status = runtime.get("status", "unknown")
    pending = int(runtime.get("pending_inputs") or 0)
    if status == "timed_out":
        return "timed-out-finalize-required"
    if pending > 0:
        return "pending-inputs-wait-all-required"
    if status in ACTIVE_RUNTIME_STATUSES:
        return "runtime-active-wait-all-required"
    return None


def unread_recipient_count(state: dict[str, Any]) -> int:
    total = 0
    for message in state["messages"]:
        read_by = set(message.get("read_by") or [])
        total += sum(1 for recipient in message.get("to", []) if recipient not in read_by)
    return total


def attention_summary(state: dict[str, Any]) -> dict[str, Any]:
    ready = []
    blocked = []
    active = []
    for task in state["tasks"].values():
        task_id = task["id"]
        if task["status"] == "todo":
            if dependencies_done(state, task):
                ready.append(task_id)
            else:
                blocked.append(task_id)
        elif task["status"] == "in_progress":
            active.append(task_id)
    pending_inputs = []
    timed_out = []
    for member in state["members"]:
        runtime = member_runtime(member)
        if int(runtime.get("pending_inputs") or 0) > 0:
            pending_inputs.append(member["name"])
        if runtime.get("status") == "timed_out":
            timed_out.append(member["name"])
    recent = [
        f"{item.get('type', 'event')}:{item.get('agent') or item.get('task') or item.get('message') or item.get('title') or '-'}"
        for item in state.get("events", [])[-5:]
    ]
    return {
        "ready_tasks": ready,
        "blocked_tasks": blocked,
        "active_tasks": active,
        "pending_inputs": pending_inputs,
        "timed_out": timed_out,
        "unread_recipients": unread_recipient_count(state),
        "recent_events": recent,
    }


def csv_or_dash(values: list[str]) -> str:
    return ",".join(values) if values else "-"


def artifact_records(state: dict[str, Any]) -> list[dict[str, Any]]:
    records = []
    for task in state.get("tasks", {}).values():
        for output in task.get("outputs") or []:
            records.append(
                {
                    "task": task["id"],
                    "owner": task["owner"],
                    "path": output,
                }
            )
    return records


def verification_evidence_records(state: dict[str, Any]) -> list[dict[str, Any]]:
    gates = state.get("gates") or {}
    evidence = gates.get("verification_evidence") or []
    return [record for record in evidence if isinstance(record, dict)]


def next_actions_for_state(state: dict[str, Any], workspace: str | Path = ".") -> list[str]:
    invocation = script_invocation(workspace)
    if state.get("status") == "cleaned":
        actions = ["Team cleaned. No further action required."]
        if unread_recipient_count(state):
            actions.append(
                f"Optional unread cleanup: {invocation} ack-all --agent <agent> --reason <reason> "
                f"or {invocation} ack-closed --reason <reason>"
            )
        return actions
    actions: list[str] = []
    for task in sorted(state["tasks"].values(), key=lambda item: item["id"]):
        owner = task["owner"]
        member = find_member(state, owner)
        if task["status"] == "todo" and dependencies_done(state, task):
            if member is state["lead"]:
                actions.append(f"{invocation} claim --agent {owner} --task {task['id']}")
            elif member is not None and runtime_bound(member_runtime(member)):
                actions.append(f"{invocation} claim --agent {owner} --task {task['id']}")
        elif task["status"] == "in_progress":
            agent = task.get("claimed_by") or owner
            actions.append(
                f"{invocation} complete --task {task['id']} --agent {agent} "
                "--summary <summary> --output <path>"
            )
    unbound_members = []
    waiting_runtime_members = []
    close_ready_members = []
    for member in state["members"]:
        runtime = member_runtime(member)
        owned_open = [
            task["id"]
            for task in state["tasks"].values()
            if task["owner"] == member["name"] and task["status"] not in {"done", "canceled"}
        ]
        if runtime.get("backend") == "unbound" and owned_open:
            unbound_members.append(member["name"])
        if runtime.get("backend") == "subagent" and runtime.get("agent_id"):
            status = runtime.get("status")
            pending = int(runtime.get("pending_inputs") or 0)
            if status in ACTIVE_RUNTIME_STATUSES or status == "timed_out" or pending > 0:
                waiting_runtime_members.append(member["name"])
            if status in FINAL_RUNTIME_STATUSES and runtime.get("close_status") not in CLOSED_RUNTIME_STATUSES:
                close_ready_members.append(member["name"])
    if unbound_members:
        if len(unbound_members) == 1:
            agent = unbound_members[0]
            actions.append(
                f"{invocation} launch-plan --backend subagent, then {invocation} "
                f"bind-subagent --agent {agent} --agent-id <agent_id> --nickname <nickname> "
                f"if this teammate was already spawned"
            )
        else:
            actions.append(
                f"{invocation} launch-plan --backend subagent, then bind returned agent ids "
                f"for unbound members: {csv_or_dash(unbound_members)}"
            )
    skipped_ready_only = []
    for member in state["members"]:
        runtime = member_runtime(member)
        if runtime.get("backend") != "unbound":
            continue
        open_tasks = open_tasks_for_member(state, member["name"])
        if open_tasks and not any(task["status"] == "todo" and dependencies_done(state, task) for task in open_tasks):
            skipped_ready_only.append(member["name"])
    if skipped_ready_only:
        actions.append(
            f"{invocation} orchestrate --spawn-policy ready-only intentionally skips blocked future workers: "
            f"{csv_or_dash(skipped_ready_only)}"
        )
    if len(waiting_runtime_members) > 1:
        actions.append(
            f"{invocation} orchestrate, then {invocation} record-wait-batch --result-file <wait_results.json> "
            f"# waiting members: {csv_or_dash(waiting_runtime_members)}"
        )
    for member in state["members"]:
        runtime = member_runtime(member)
        for task in state["tasks"].values():
            if task["owner"] != member["name"]:
                continue
            if task["status"] == "todo" and dependencies_done(state, task) and task.get("ready_notified_at") is None:
                if runtime.get("backend") in {"subagent", "thread"} and (runtime.get("agent_id") or runtime.get("thread_id")):
                    actions.append(f"{invocation} wake-plan --mark  # wake {member['name']} for {task['id']}")
        pending = int(runtime.get("pending_inputs") or 0)
        status = runtime.get("status")
        if runtime.get("backend") == "subagent" and runtime.get("agent_id"):
            if len(waiting_runtime_members) > 1:
                pass
            elif status == "timed_out":
                actions.append(
                    f"{invocation} orchestrate, then {invocation} record-wait --agent {member['name']} "
                    "--status completed|timed_out|shutdown|errored --summary <wait_result>"
                )
            elif status in ACTIVE_RUNTIME_STATUSES or pending > 0:
                actions.append(
                    f"{invocation} record-wait --agent {member['name']} "
                    "--status completed|running|timed_out|shutdown|errored --summary <wait_result>"
                )
            if len(close_ready_members) <= 1 and status in FINAL_RUNTIME_STATUSES and runtime.get("close_status") not in CLOSED_RUNTIME_STATUSES:
                actions.append(
                    f"{invocation} close-plan, then {invocation} record-close --agent {member['name']} "
                    "--status closed --summary <close_result>"
                )
    if len(close_ready_members) > 1:
        actions.append(
            f"{invocation} close-plan, then {invocation} record-close-batch --result-file <close_results.json> "
            f"# close-ready members: {csv_or_dash(close_ready_members)}"
        )
    stop = stop_report(state)
    if not state["gates"]["verification_passed"] and not open_task_ids(state):
        actions.append(f"{invocation} gate --verification-passed true  # only after checks pass")
    if stop["ok"] and not runtime_cleanup_blockers(state) and state.get("status") != "cleaned":
        actions.append(f"{invocation} cleanup")
    if not actions:
        actions.append("No immediate team action; continue task work or run orchestrate for the next host-tool phase.")
    return actions


def dashboard_text(state: dict[str, Any], workspace: str | Path = ".") -> str:
    attention = attention_summary(state)
    next_actions = next_actions_for_state(state, workspace)
    lines = [
        f"Team: {state['title']} ({state['team_id']})",
        f"Status: {state['status']}",
        f"Verification: {'passed' if state['gates']['verification_passed'] else 'pending'}",
        "",
        "Attention:",
        f"- ready_tasks={csv_or_dash(attention['ready_tasks'])}",
        f"- blocked_tasks={csv_or_dash(attention['blocked_tasks'])}",
        f"- active_tasks={csv_or_dash(attention['active_tasks'])}",
        f"- pending_inputs={csv_or_dash(attention['pending_inputs'])}",
        f"- timed_out={csv_or_dash(attention['timed_out'])}",
        f"- unread_recipients={attention['unread_recipients']}",
        f"- recent_events={csv_or_dash(attention['recent_events'])}",
        "",
        "Next actions:",
    ]
    lines.extend(f"- {action}" for action in next_actions)
    lines.extend(
        [
            "",
            "Members:",
        ]
    )
    for member in state["members"]:
        runtime = member_runtime(member)
        backend = runtime.get("backend", "unbound")
        handle = runtime.get("agent_id") or runtime.get("thread_id") or "-"
        runtime_status = runtime.get("status", "unknown")
        close_status = runtime.get("close_status") or "open"
        pending = int(runtime.get("pending_inputs") or 0)
        warning = runtime_warning(runtime)
        warning_text = f" warning={warning}" if warning else ""
        history = member.get("runtime_history") or []
        history_text = ""
        if history:
            latest = history[-1]
            history_text = (
                f" replacement_history={len(history)} "
                f"recent_replacement={latest.get('replacement_old_status') or latest.get('status') or 'unknown'}"
            )
        lines.append(
            f"- {member['name']} [{member['status']}] backend={backend} "
            f"runtime={runtime_status} close={close_status} pending_inputs={pending} handle={handle}{warning_text}{history_text}"
        )
    lines.append("")
    lines.append("Tasks:")
    for task in state["tasks"].values():
        deps = ",".join(task["depends_on"]) or "-"
        lines.append(f"- {task['id']} [{task['status']}] owner={task['owner']} deps={deps}: {task['title']}")
    artifacts = artifact_records(state)
    if artifacts:
        lines.append("")
        lines.append("Artifacts:")
        for artifact in artifacts:
            lines.append(f"- {artifact['task']} owner={artifact['owner']}: {artifact['path']}")
    evidence = verification_evidence_records(state)
    if evidence:
        lines.append("")
        lines.append("Verification evidence:")
        for record in evidence:
            command = record.get("command") or "-"
            exit_code = record.get("exit_code")
            summary = record.get("summary") or "-"
            lines.append(f"- exit={exit_code} command={command}: {summary}")
    unread = unread_recipient_count(state)
    lines.append("")
    lines.append(f"Messages: {len(state['messages'])} total, {unread} unread recipient deliveries")
    return "\n".join(lines)


def dashboard_markdown(state: dict[str, Any], workspace: str | Path = ".") -> str:
    unread = unread_recipient_count(state)
    attention = attention_summary(state)
    next_actions = next_actions_for_state(state, workspace)
    lines = [
        f"# {state['title']}",
        "",
        f"- Team id: `{state['team_id']}`",
        f"- Status: `{state['status']}`",
        f"- Verification: `{'passed' if state['gates']['verification_passed'] else 'pending'}`",
        f"- Messages: `{len(state['messages'])}` total, `{unread}` unread recipient deliveries",
        "",
        "## Attention",
        "",
        f"- Ready tasks: `{csv_or_dash(attention['ready_tasks'])}`",
        f"- Blocked tasks: `{csv_or_dash(attention['blocked_tasks'])}`",
        f"- Active tasks: `{csv_or_dash(attention['active_tasks'])}`",
        f"- Pending inputs: `{csv_or_dash(attention['pending_inputs'])}`",
        f"- Timed out: `{csv_or_dash(attention['timed_out'])}`",
        f"- Unread recipients: `{attention['unread_recipients']}`",
        f"- Recent events: `{csv_or_dash(attention['recent_events'])}`",
        "",
        "## Next Actions",
        "",
    ]
    lines.extend(f"- `{action}`" for action in next_actions)
    lines.extend(
        [
            "",
            "## Members",
            "",
        ]
    )
    for member in state["members"]:
        runtime = member_runtime(member)
        backend = runtime.get("backend", "unbound")
        handle = runtime.get("agent_id") or runtime.get("thread_id") or "-"
        runtime_status = runtime.get("status", "unknown")
        close_status = runtime.get("close_status") or "open"
        pending = int(runtime.get("pending_inputs") or 0)
        warning = runtime_warning(runtime)
        warning_text = f" warning=`{warning}`" if warning else ""
        history = member.get("runtime_history") or []
        history_text = ""
        if history:
            latest = history[-1]
            history_text = (
                f" replacement_history=`{len(history)}` "
                f"recent_replacement=`{latest.get('replacement_old_status') or latest.get('status') or 'unknown'}`"
            )
        lines.append(
            f"- `{member['name']}`: `{member['status']}` backend=`{backend}` "
            f"runtime=`{runtime_status}` close=`{close_status}` pending_inputs=`{pending}` handle=`{handle}`{warning_text}{history_text}"
        )
    lines.extend(["", "## Tasks", ""])
    for task in state["tasks"].values():
        deps = ", ".join(task["depends_on"]) or "-"
        lines.append(f"- `{task['id']}`: `{task['status']}` owner=`{task['owner']}` deps=`{deps}` - {task['title']}")
    artifacts = artifact_records(state)
    if artifacts:
        lines.extend(["", "## Artifacts", ""])
        for artifact in artifacts:
            lines.append(f"- `{artifact['task']}` owner=`{artifact['owner']}`: `{artifact['path']}`")
    evidence = verification_evidence_records(state)
    if evidence:
        lines.extend(["", "## Verification Evidence", ""])
        for record in evidence:
            command = record.get("command") or "-"
            exit_code = record.get("exit_code")
            summary = record.get("summary") or "-"
            lines.append(f"- exit=`{exit_code}` command=`{command}` - {summary}")
    lines.append("")
    return "\n".join(lines)


def cmd_dashboard(args: argparse.Namespace) -> dict[str, Any]:
    state = read_state_locked(args.workspace)
    rendered = dashboard_markdown(state, args.workspace) if args.format == "markdown" else dashboard_text(state, args.workspace)
    output_path = None
    if args.output:
        output = resolve_workspace_path(args.workspace, args.output, "--output", args.allow_outside_workspace)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
        output_path = str(output.resolve())
    return {
        "ok": True,
        "format": args.format,
        "dashboard": rendered,
        "output": output_path,
        "lead_action_queue": {"next_actions": next_actions_for_state(state, args.workspace)},
    }


def inbox_text(state: dict[str, Any], agent: str) -> str:
    messages = inbox_for(state, agent)
    if not messages:
        return f"Inbox for {agent}: no unread messages."
    lines = [f"Inbox for {agent}:"]
    for message in messages:
        lines.append(f"- {message['id']} from {message['from']}: {message['body']}")
    return "\n".join(lines)


def cmd_inbox(args: argparse.Namespace) -> dict[str, Any]:
    state = read_state_locked(args.workspace)
    require_member(state, args.agent)
    return {"ok": True, "agent": args.agent, "inbox": inbox_text(state, args.agent)}


def cmd_ack(args: argparse.Namespace) -> dict[str, Any]:
    with state_transaction(args.workspace) as state:
        require_member(state, args.agent)
        target = None
        for message in state["messages"]:
            if message["id"] == args.message:
                target = message
                break
        if target is None:
            raise TeamError("unknown_message", f"Message {args.message} does not exist")
        if args.agent not in target["to"]:
            raise TeamError("not_recipient", f"{args.agent} is not a recipient of {args.message}", 2)
        if args.agent not in target["read_by"]:
            target["read_by"].append(args.agent)
            target["read_by"].sort()
        reconcile_message_ack(state, target, args.agent)
        state["updated_at"] = now_iso()
        ack_event = event("message_acknowledged", agent=args.agent, message=args.message)
        if getattr(args, "reason", None):
            ack_event["reason"] = args.reason
        state["events"].append(ack_event)
    return {"ok": True, "message": target}


def ack_messages_for_agent(state: dict[str, Any], agent: str) -> list[str]:
    acknowledged = []
    for message in state["messages"]:
        if agent not in message.get("to", []):
            continue
        if agent in message.get("read_by", []):
            continue
        message.setdefault("read_by", []).append(agent)
        message["read_by"].sort()
        reconcile_message_ack(state, message, agent)
        acknowledged.append(message["id"])
    return acknowledged


def cmd_ack_all(args: argparse.Namespace) -> dict[str, Any]:
    with state_transaction(args.workspace) as state:
        require_member(state, args.agent)
        acknowledged = ack_messages_for_agent(state, args.agent)
        state["updated_at"] = now_iso()
        state["events"].append(
            event("messages_acknowledged_all", agent=args.agent, messages=acknowledged, reason=args.reason)
        )
    return {"ok": True, "agent": args.agent, "acknowledged": acknowledged}


def closed_runtime_agents(state: dict[str, Any]) -> list[str]:
    agents = []
    for member in state["members"]:
        runtime = member_runtime(member)
        if runtime.get("status") == "shutdown" or runtime.get("close_status") in CLOSED_RUNTIME_STATUSES:
            agents.append(member["name"])
    return agents


def cmd_ack_closed(args: argparse.Namespace) -> dict[str, Any]:
    with state_transaction(args.workspace) as state:
        agents = closed_runtime_agents(state)
        acknowledged = []
        for agent in agents:
            acknowledged.extend(ack_messages_for_agent(state, agent))
        acknowledged = sorted(set(acknowledged))
        state["updated_at"] = now_iso()
        state["events"].append(
            event("messages_acknowledged_closed", agents=agents, messages=acknowledged, reason=args.reason)
        )
    return {"ok": True, "agents": agents, "acknowledged": acknowledged}


def wake_actions_for_state(state: dict[str, Any]) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for task in state["tasks"].values():
        if task["status"] != "todo" or task.get("ready_notified_at") is not None:
            continue
        if not dependencies_done(state, task):
            continue
        member = find_member(state, task["owner"])
        if member is None:
            continue
        if member is state["lead"]:
            continue
        runtime = member_runtime(member)
        body = f"Task {task['id']} is now ready: {task['title']}. Claim it through the team state before working."
        if runtime.get("backend") == "subagent" and runtime.get("agent_id"):
            actions.append(
                {
                    "agent": member["name"],
                    "task": task["id"],
                    "tool": "multi_agent_v1.send_input",
                    "send_input_args": {"target": runtime["agent_id"], "message": body},
                }
            )
        elif runtime.get("backend") == "thread" and runtime.get("thread_id"):
            actions.append(
                {
                    "agent": member["name"],
                    "task": task["id"],
                    "tool": "codex_app.send_message_to_thread",
                    "send_message_to_thread_args": {"threadId": runtime["thread_id"], "prompt": body},
                }
            )
    return actions


def mark_wake_actions(state: dict[str, Any], actions: list[dict[str, Any]], timestamp: str) -> None:
    ready_task_ids = {action.get("task") for action in actions if action.get("task")}
    for task_id in ready_task_ids:
        task = state["tasks"].get(task_id)
        if task is not None:
            task["ready_notified_at"] = timestamp


def cmd_wake_plan(args: argparse.Namespace) -> dict[str, Any]:
    if args.mark:
        with state_transaction(args.workspace) as state:
            actions = wake_actions_for_state(state)
            timestamp = now_iso()
            mark_wake_actions(state, actions, timestamp)
            state["updated_at"] = timestamp
            state["events"].append(event("wake_plan_marked", actions=len(actions)))
    else:
        state = read_state_locked(args.workspace)
        actions = wake_actions_for_state(state)
    actions = add_action_ids(actions, "wake_ready")
    return {
        "ok": True,
        "actions": actions,
        "lead_action_queue": lead_action_queue(args.workspace, "wake_ready", actions),
    }


def cmd_hook_config(args: argparse.Namespace) -> dict[str, Any]:
    plugin_root = Path(__file__).resolve().parents[1]
    stop_hook = plugin_root / "hooks" / "agent_team_stop.py"
    idle_hook = plugin_root / "hooks" / "agent_team_idle.py"
    workspace = str(workspace_root(args.workspace))
    powershell = "\n".join(
        [
            f"$env:CODEX_AGENT_TEAM_WORKSPACE={ps_single_quote(workspace)}",
            f"& {ps_single_quote(sys.executable)} {ps_single_quote(stop_hook)}",
            f"$env:CODEX_AGENT_TEAM_AGENT={ps_single_quote(args.agent)}",
            f"& {ps_single_quote(sys.executable)} {ps_single_quote(idle_hook)}",
        ]
    )
    return {
        "ok": True,
        "powershell": powershell,
        "json": {
            "stop": {
                "env": {"CODEX_AGENT_TEAM_WORKSPACE": workspace},
                "command": [sys.executable, str(stop_hook)],
            },
            "idle": {
                "env": {"CODEX_AGENT_TEAM_WORKSPACE": workspace, "CODEX_AGENT_TEAM_AGENT": args.agent},
                "command": [sys.executable, str(idle_hook)],
            },
        },
    }


def print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def actor_from_args(args: argparse.Namespace) -> str | None:
    actor = getattr(args, "actor", None) or os.environ.get("CODEX_AGENT_TEAM_ACTOR")
    if actor is None:
        return None
    actor = actor.strip()
    return actor or None


def ps_single_quote(value: Any) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def enforce_actor_permissions(args: argparse.Namespace) -> None:
    actor = actor_from_args(args)
    if actor is None or args.command == "init":
        return
    if args.command == "repair":
        try:
            state = read_state_locked(args.workspace)
        except TeamError as exc:
            if exc.code in {"no_team", "state_corrupt", "state_read_failed"}:
                return
            raise
    else:
        state = read_state_locked(args.workspace)
    lead = state["lead"]["name"]
    names = member_names(state)
    if actor not in names:
        raise TeamError("unknown_actor", f"{actor} is not part of this team", 2)
    if args.command in LEAD_ONLY_COMMANDS and actor != lead:
        raise TeamError("permission_denied", f"{args.command} is reserved for lead {lead}", 2)
    if args.command == "message" and actor != lead and args.sender != actor:
        raise TeamError("sender_spoof", f"{actor} cannot send as {args.sender}", 2)
    if args.command in AGENT_SCOPED_COMMANDS and actor != lead:
        target_agent = getattr(args, "agent", None)
        if target_agent is not None and target_agent != actor:
            raise TeamError("permission_denied", f"{actor} cannot run {args.command} for {target_agent}", 2)


def add_prompt_context_arguments(command: argparse.ArgumentParser) -> None:
    command.add_argument("--brief", action="append", default=[], help="Extra team brief text to include in teammate prompts")
    command.add_argument("--brief-file", action="append", default=[], help="Workspace-contained file with team brief text")
    command.add_argument("--context-file", action="append", default=[], help="Workspace-contained shared context file to embed in prompts")
    command.add_argument("--member-context", action="append", default=[], help="Per-agent prompt context as agent=text")
    command.add_argument("--member-context-file", action="append", default=[], help="Per-agent prompt context file as agent=path")
    command.add_argument("--verification-check", action="append", default=[], help="Verification checklist item to include in prompts")
    command.add_argument("--require-citations", action="store_true", help="Require citation-quality guidance in teammate prompts")
    command.add_argument("--allow-outside-workspace", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage a local Codex agent team state file.")
    parser.add_argument("--workspace", default=".", help="Workspace that owns .codex-agent-teams/state.json")
    parser.add_argument("--actor", help="Optional guardrail identity for CLI permission checks")
    subcommands = parser.add_subparsers(dest="command", required=True)

    init = subcommands.add_parser("init", help="Create a new team state")
    init.add_argument("--title", required=True)
    init.add_argument("--lead", required=True)
    init.add_argument("--member", action="append", default=[], required=True)
    init.add_argument("--task", action="append", default=[], required=True)
    init.add_argument("--depends", action="append", default=[])
    init.add_argument("--plan-required", action="store_true")
    init.add_argument("--force", action="store_true", help="Archive existing team state before creating a new team")
    add_prompt_context_arguments(init)
    init.set_defaults(func=cmd_init)

    launch = subcommands.add_parser("launch", help="Create a team and emit a host-tool launch plan")
    launch.add_argument("--title", required=True)
    launch.add_argument("--lead", required=True)
    launch.add_argument("--member", action="append", default=[], required=True)
    launch.add_argument("--task", action="append", default=[], required=True)
    launch.add_argument("--depends", action="append", default=[])
    launch.add_argument("--plan-required", action="store_true")
    launch.add_argument("--backend", choices=["subagent", "thread", "simulated"], default="subagent")
    launch.add_argument("--spawn-policy", choices=["ready-only", "open", "all"], default="all")
    launch.add_argument("--force", action="store_true", help="Archive existing team state before creating a new team")
    add_prompt_context_arguments(launch)
    launch.set_defaults(func=cmd_launch)

    launch_plan = subcommands.add_parser("launch-plan", help="Emit host-tool calls for unbound teammates")
    launch_plan.add_argument("--backend", choices=["subagent", "thread", "simulated"], default="subagent")
    launch_plan.add_argument("--spawn-policy", choices=["ready-only", "open", "all"], default="all")
    launch_plan.set_defaults(func=cmd_launch_plan)

    claim = subcommands.add_parser("claim", help="Claim the next ready task for an agent")
    claim.add_argument("--agent", required=True)
    claim.add_argument("--task", help="Claim a specific ready task instead of the first ready task")
    claim.set_defaults(func=cmd_claim)

    complete = subcommands.add_parser("complete", help="Complete a task")
    complete.add_argument("--task", required=True)
    complete.add_argument("--agent", required=True)
    complete.add_argument("--summary", required=True)
    complete.add_argument("--output", action="append", default=[], help="Workspace-contained output artifact path produced by this task")
    complete.add_argument("--allow-outside-workspace", action="store_true")
    complete.set_defaults(func=cmd_complete)

    cancel = subcommands.add_parser("cancel", help="Cancel an open task")
    cancel.add_argument("--task", required=True)
    cancel.add_argument("--reason", required=True)
    cancel.set_defaults(func=cmd_cancel)

    reassign = subcommands.add_parser("reassign", help="Reassign an open task to another team member")
    reassign.add_argument("--task", required=True)
    reassign.add_argument("--agent", required=True)
    reassign.add_argument("--reason", required=True)
    reassign.set_defaults(func=cmd_reassign)

    message = subcommands.add_parser("message", help="Send a direct team message")
    message.add_argument("--from", dest="sender", required=True)
    message.add_argument("--to", required=True)
    message.add_argument("--body")
    message.add_argument("--body-file")
    message.add_argument("--allow-outside-workspace", action="store_true")
    message.set_defaults(func=cmd_message)

    bind_subagent = subcommands.add_parser("bind-subagent", help="Record a spawned subagent id for a teammate")
    bind_subagent.add_argument("--agent", required=True)
    bind_subagent.add_argument("--agent-id", required=True)
    bind_subagent.add_argument("--nickname")
    bind_subagent.set_defaults(func=cmd_bind_subagent)

    replace_subagent = subcommands.add_parser("replace-subagent", help="Archive an old subagent runtime and bind a replacement")
    replace_subagent.add_argument("--agent", required=True)
    replace_subagent.add_argument("--agent-id", required=True)
    replace_subagent.add_argument("--nickname")
    replace_subagent.add_argument("--old-status", choices=["closed", "not_found", "archived", "failed"])
    replace_subagent.add_argument("--old-summary")
    replace_subagent.set_defaults(func=cmd_replace_subagent)

    bind_thread = subcommands.add_parser("bind-thread", help="Record a fallback thread id for a teammate")
    bind_thread.add_argument("--agent", required=True)
    bind_thread.add_argument("--thread-id", required=True)
    bind_thread.add_argument("--nickname")
    bind_thread.set_defaults(func=cmd_bind_thread)

    gate = subcommands.add_parser("gate", help="Update stop/approval gates")
    gate.add_argument("--plan-approved")
    gate.add_argument("--verification-passed")
    gate.add_argument("--verification-command")
    gate.add_argument("--verification-exit-code", type=int)
    gate.add_argument("--verification-summary")
    gate.add_argument("--verification-summary-file")
    gate.add_argument("--allow-outside-workspace", action="store_true")
    gate.add_argument("--open-item", action="append", default=[])
    gate.add_argument("--clear-open-items", action="store_true")
    gate.set_defaults(func=cmd_gate)

    stop_check = subcommands.add_parser("stop-check", help="Return non-zero while team work is blocked or incomplete")
    stop_check.set_defaults(func=cmd_stop_check)

    cleanup = subcommands.add_parser("cleanup", help="Mark the team state cleaned")
    cleanup.add_argument("--force", action="store_true")
    cleanup.add_argument("--ack-closed", action="store_true", help="Acknowledge unread messages addressed to closed/shutdown runtimes during cleanup")
    cleanup.set_defaults(func=cmd_cleanup)

    close_plan = subcommands.add_parser("close-plan", help="Emit host-tool calls to close bound subagents or archive fallback threads")
    close_plan.set_defaults(func=cmd_close_plan)

    orchestrate = subcommands.add_parser("orchestrate", help="Emit the next lead-orchestrator host-tool phase")
    orchestrate.add_argument("--backend", choices=["subagent", "thread", "simulated"], default="subagent")
    orchestrate.add_argument("--mark-wake", action="store_true")
    orchestrate.add_argument("--spawn-policy", choices=["ready-only", "open", "all"], default="ready-only")
    orchestrate.add_argument("--timeout-ms", type=int, default=180000)
    orchestrate.set_defaults(func=cmd_orchestrate)

    record_wait = subcommands.add_parser("record-wait", help="Record a wait_agent result for a bound teammate")
    record_wait.add_argument("--agent", required=True)
    record_wait.add_argument("--status", choices=["running", "completed", "timed_out", "shutdown", "errored"], required=True)
    record_wait.add_argument("--summary")
    record_wait.add_argument("--summary-file")
    record_wait.add_argument("--allow-outside-workspace", action="store_true")
    record_wait.set_defaults(func=cmd_record_wait)

    record_wait_batch = subcommands.add_parser("record-wait-batch", help="Map a host wait_agent JSON result file into record-wait updates")
    record_wait_batch.add_argument("--result-file", required=True)
    record_wait_batch.add_argument("--allow-outside-workspace", action="store_true")
    record_wait_batch.set_defaults(func=cmd_record_wait_batch)

    record_delivery = subcommands.add_parser("record-delivery", help="Record that a host-tool message delivery was sent or failed")
    record_delivery.add_argument("--message", required=True)
    record_delivery.add_argument("--agent", required=True)
    record_delivery.add_argument("--status", choices=["sent", "failed"], required=True)
    record_delivery.set_defaults(func=cmd_record_delivery)

    record_delivery_batch = subcommands.add_parser("record-delivery-batch", help="Map a host send_input JSON result file into delivery updates")
    record_delivery_batch.add_argument("--result-file", required=True)
    record_delivery_batch.add_argument("--allow-outside-workspace", action="store_true")
    record_delivery_batch.set_defaults(func=cmd_record_delivery_batch)

    record_close = subcommands.add_parser("record-close", help="Record that a bound subagent or fallback thread was closed")
    record_close.add_argument("--agent", required=True)
    record_close.add_argument("--status", choices=["closed", "not_found", "archived", "failed"], required=True)
    record_close.add_argument("--summary")
    record_close.add_argument("--summary-file")
    record_close.add_argument("--allow-outside-workspace", action="store_true")
    record_close.set_defaults(func=cmd_record_close)

    record_close_batch = subcommands.add_parser("record-close-batch", help="Map a host close_agent JSON result file into record-close updates")
    record_close_batch.add_argument("--result-file", required=True)
    record_close_batch.add_argument("--allow-outside-workspace", action="store_true")
    record_close_batch.set_defaults(func=cmd_record_close_batch)

    status = subcommands.add_parser("status", help="Print full team status")
    status.set_defaults(func=cmd_status)

    repair = subcommands.add_parser("repair", help="Inspect and repair local team state files")
    repair.add_argument("--unlock-stale", action="store_true")
    repair.add_argument("--restore-backup", action="store_true")
    repair.add_argument("--clean-temps", action="store_true")
    repair.set_defaults(func=cmd_repair)

    dashboard = subcommands.add_parser("dashboard", help="Print a human-readable team dashboard")
    dashboard.add_argument("--format", choices=["text", "markdown"], default="text")
    dashboard.add_argument("--output")
    dashboard.add_argument("--allow-outside-workspace", action="store_true")
    dashboard.set_defaults(func=cmd_dashboard)

    inbox = subcommands.add_parser("inbox", help="Print unread messages for a teammate")
    inbox.add_argument("--agent", required=True)
    inbox.set_defaults(func=cmd_inbox)

    ack = subcommands.add_parser("ack", help="Mark a message read for a teammate")
    ack.add_argument("--agent", required=True)
    ack.add_argument("--message", required=True)
    ack.add_argument("--reason")
    ack.set_defaults(func=cmd_ack)

    ack_all = subcommands.add_parser("ack-all", help="Mark all unread messages read for a teammate")
    ack_all.add_argument("--agent", required=True)
    ack_all.add_argument("--reason", required=True)
    ack_all.set_defaults(func=cmd_ack_all)

    ack_closed = subcommands.add_parser("ack-closed", help="Mark unread messages read for closed or shutdown runtimes")
    ack_closed.add_argument("--reason", required=True)
    ack_closed.set_defaults(func=cmd_ack_closed)

    wake_plan = subcommands.add_parser("wake-plan", help="Emit host-tool calls for ready tasks whose dependencies unblocked")
    wake_plan.add_argument("--mark", action="store_true")
    wake_plan.set_defaults(func=cmd_wake_plan)

    hook_config = subcommands.add_parser("hook-config", help="Emit host hook snippets for stop and idle wrappers")
    hook_config.add_argument("--agent", default="teammate")
    hook_config.set_defaults(func=cmd_hook_config)

    prompt = subcommands.add_parser("prompt", help="Generate a teammate prompt from current state")
    prompt.add_argument("--agent", required=True)
    prompt.set_defaults(func=cmd_prompt)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        enforce_actor_permissions(args)
        result = args.func(args)
        if isinstance(result, tuple):
            payload, exit_code = result
        else:
            payload, exit_code = result, 0
        print_json(payload)
        return exit_code
    except TeamError as exc:
        print_json({"ok": False, "error": {"code": exc.code, "message": str(exc)}})
        return exc.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
