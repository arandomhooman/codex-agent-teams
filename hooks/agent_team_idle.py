#!/usr/bin/env python3
"""Idle-hook wrapper that claims the next ready task for a named teammate."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
AGENT_TEAM = PLUGIN_ROOT / "scripts" / "agent_team.py"


def main() -> int:
    agent = os.environ.get("CODEX_AGENT_TEAM_AGENT", "").strip()
    workspace = os.environ.get("CODEX_AGENT_TEAM_WORKSPACE", ".")
    if not agent:
        print(json.dumps({"ok": True, "claimed": False, "reason": "CODEX_AGENT_TEAM_AGENT is not set"}))
        return 0

    result = subprocess.run(
        [sys.executable, str(AGENT_TEAM), "--workspace", workspace, "claim", "--agent", agent],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        payload = json.loads(result.stdout)
        task = payload["task"]
        payload["continue_prompt"] = (
            f"You are {agent}. Continue with claimed task {task['id']}: {task['title']}. "
            f"When finished, run {sys.executable} {AGENT_TEAM} --workspace {workspace!r} "
            "complete with a concise summary."
        )
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 2

    # No ready task is not a hook failure; it just means this teammate can idle.
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        sys.stdout.write(result.stdout)
        sys.stderr.write(result.stderr)
        return result.returncode
    if payload.get("error", {}).get("code") == "no_ready_task":
        print(json.dumps({"ok": True, "claimed": False, "reason": "no_ready_task", "agent": agent}))
        return 0
    print(json.dumps(payload, indent=2, sort_keys=True))
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
