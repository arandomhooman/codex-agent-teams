#!/usr/bin/env python3
"""Stop-hook wrapper for Codex Agent Teams.

Exit code 2 means the team has unfinished work or an open gate. Hosts that
support stop hooks can use that as a nudge to continue rather than finalize.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
import os


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
AGENT_TEAM = PLUGIN_ROOT / "scripts" / "agent_team.py"


def main() -> int:
    workspace = os.environ.get("CODEX_AGENT_TEAM_WORKSPACE", ".")
    result = subprocess.run(
        [sys.executable, str(AGENT_TEAM), "--workspace", workspace, "stop-check"],
        capture_output=True,
        text=True,
    )
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    if result.returncode == 2 and result.stdout:
        sys.stderr.write(result.stdout)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
