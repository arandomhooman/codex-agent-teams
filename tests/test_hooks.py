import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
AGENT_TEAM = PLUGIN_ROOT / "scripts" / "agent_team.py"
STOP_HOOK = PLUGIN_ROOT / "hooks" / "agent_team_stop.py"
IDLE_HOOK = PLUGIN_ROOT / "hooks" / "agent_team_idle.py"


def run_agent_team(workspace, *args):
    return subprocess.run(
        [sys.executable, str(AGENT_TEAM), "--workspace", str(workspace), *args],
        capture_output=True,
        text=True,
        check=True,
    )


class AgentTeamHookTests(unittest.TestCase):
    def test_stop_hook_returns_blocking_code_for_active_team(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_agent_team(
                tmp,
                "init",
                "--title",
                "Hook stop test",
                "--lead",
                "lead",
                "--member",
                "builder=Build",
                "--task",
                "T1:builder:Implement",
            )

            result = subprocess.run(
                [sys.executable, str(STOP_HOOK)],
                cwd=tmp,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 2)
            self.assertIn("tasks_open", result.stdout)
            self.assertIn("tasks_open", result.stderr)

    def test_idle_hook_claims_ready_task_for_configured_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_agent_team(
                tmp,
                "init",
                "--title",
                "Hook idle test",
                "--lead",
                "lead",
                "--member",
                "builder=Build",
                "--task",
                "T1:builder:Implement",
            )

            env = os.environ.copy()
            env["CODEX_AGENT_TEAM_AGENT"] = "builder"
            result = subprocess.run(
                [sys.executable, str(IDLE_HOOK)],
                cwd=tmp,
                env=env,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 2)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["task"]["id"], "T1")
            self.assertIn("continue_prompt", payload)

    def test_hooks_use_explicit_workspace_environment(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as other:
            run_agent_team(
                tmp,
                "init",
                "--title",
                "Hook workspace env test",
                "--lead",
                "lead",
                "--member",
                "builder=Build",
                "--task",
                "T1:builder:Implement",
            )

            env = os.environ.copy()
            env["CODEX_AGENT_TEAM_WORKSPACE"] = tmp
            stop = subprocess.run(
                [sys.executable, str(STOP_HOOK)],
                cwd=other,
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(stop.returncode, 2)
            self.assertIn("tasks_open", stop.stderr)

            env["CODEX_AGENT_TEAM_AGENT"] = "builder"
            idle = subprocess.run(
                [sys.executable, str(IDLE_HOOK)],
                cwd=other,
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(idle.returncode, 2)
            payload = json.loads(idle.stdout)
            self.assertEqual(payload["task"]["id"], "T1")


if __name__ == "__main__":
    unittest.main()
