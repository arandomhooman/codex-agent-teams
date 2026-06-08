import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PLUGIN_ROOT / "scripts" / "agent_team.py"
SPEC = importlib.util.spec_from_file_location("agent_team_under_test", SCRIPT)
AGENT_TEAM = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(AGENT_TEAM)


def run_cli(workspace, *args, check=True):
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--workspace", str(workspace), *args],
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"Command failed with {result.returncode}: {result.stderr}\nSTDOUT: {result.stdout}"
        )
    return result


def run_cli_with_prefix(*args, check=True):
    result = subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"Command failed with {result.returncode}: {result.stderr}\nSTDOUT: {result.stdout}"
        )
    return result


def run_cli_inprocess(workspace, *args, check=True, env=None):
    argv = ["--workspace", str(workspace), *args]
    stdout = io.StringIO()
    stderr = io.StringIO()
    previous_env = {}
    if env:
        for key, value in env.items():
            previous_env[key] = os.environ.get(key)
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    try:
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                returncode = AGENT_TEAM.main(argv)
            except SystemExit as exc:
                returncode = int(exc.code or 0)
    finally:
        for key, value in previous_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    result = subprocess.CompletedProcess(
        [sys.executable, str(SCRIPT), *argv],
        returncode,
        stdout.getvalue(),
        stderr.getvalue(),
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"Command failed with {result.returncode}: {result.stderr}\nSTDOUT: {result.stdout}"
        )
    return result


def read_state(workspace):
    state_path = Path(workspace) / ".codex-agent-teams" / "state.json"
    return json.loads(state_path.read_text(encoding="utf-8"))


class AgentTeamCliTests(unittest.TestCase):
    def test_init_creates_team_state_with_members_tasks_and_dependencies(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = run_cli(
                tmp,
                "init",
                "--title",
                "Parallel plugin build",
                "--lead",
                "lead",
                "--member",
                "researcher=Map Claude agent-team docs",
                "--member",
                "builder=Implement Codex replica",
                "--task",
                "T1:researcher:Research Claude features",
                "--task",
                "T2:builder:Build Codex plugin",
                "--depends",
                "T2:T1",
            )

            payload = json.loads(result.stdout)
            state = read_state(tmp)

            self.assertEqual(payload["team_id"], state["team_id"])
            self.assertEqual(state["title"], "Parallel plugin build")
            self.assertEqual(state["lead"]["name"], "lead")
            self.assertEqual([member["name"] for member in state["members"]], ["researcher", "builder"])
            self.assertEqual(state["tasks"]["T1"]["owner"], "researcher")
            self.assertEqual(state["tasks"]["T2"]["depends_on"], ["T1"])
            self.assertTrue(state["gates"]["plan_approved"])
            self.assertFalse(state["gates"]["verification_passed"])
            self.assertEqual(state["events"][0]["type"], "team_created")

    def test_init_and_launch_force_archive_existing_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_cli_inprocess(
                tmp,
                "init",
                "--title",
                "Original team",
                "--lead",
                "lead",
                "--member",
                "builder=Build",
                "--task",
                "T1:builder:Build original",
            )

            refused = run_cli_inprocess(
                tmp,
                "init",
                "--title",
                "Replacement team",
                "--lead",
                "lead",
                "--member",
                "builder=Build",
                "--task",
                "T1:builder:Build replacement",
                check=False,
            )
            self.assertEqual(refused.returncode, 2)
            self.assertIn("team_exists", refused.stdout)

            forced = json.loads(
                run_cli_inprocess(
                    tmp,
                    "init",
                    "--force",
                    "--title",
                    "Forced team",
                    "--lead",
                    "lead",
                    "--member",
                    "builder=Build",
                    "--task",
                    "T1:builder:Build forced",
                ).stdout
            )
            self.assertTrue(Path(forced["archived_state"]).exists())
            self.assertEqual(read_state(tmp)["title"], "Forced team")

            launched = json.loads(
                run_cli_inprocess(
                    tmp,
                    "launch",
                    "--force",
                    "--title",
                    "Launch forced team",
                    "--lead",
                    "lead",
                    "--member",
                    "researcher=Research",
                    "--task",
                    "T1:researcher:Research",
                ).stdout
            )

            self.assertTrue(Path(launched["archived_state"]).exists())
            self.assertEqual(read_state(tmp)["title"], "Launch forced team")
            self.assertEqual(launched["launch"]["actions"][0]["agent"], "researcher")

    def test_claim_respects_dependencies_and_assignment(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_cli(
                tmp,
                "init",
                "--title",
                "Dependency test",
                "--lead",
                "lead",
                "--member",
                "researcher=Research",
                "--member",
                "builder=Build",
                "--task",
                "T1:researcher:Research docs",
                "--task",
                "T2:builder:Implement result",
                "--depends",
                "T2:T1",
            )

            blocked = run_cli(tmp, "claim", "--agent", "builder", check=False)
            self.assertEqual(blocked.returncode, 1)
            self.assertIn("no_ready_task", blocked.stdout)

            claimed = json.loads(run_cli(tmp, "claim", "--agent", "researcher").stdout)
            self.assertEqual(claimed["task"]["id"], "T1")

            run_cli(tmp, "complete", "--task", "T1", "--agent", "researcher", "--summary", "Feature map done")

            builder_claim = json.loads(run_cli(tmp, "claim", "--agent", "builder").stdout)
            self.assertEqual(builder_claim["task"]["id"], "T2")
            state = read_state(tmp)
            self.assertEqual(state["tasks"]["T1"]["status"], "done")
            self.assertEqual(state["tasks"]["T2"]["status"], "in_progress")

    def test_claim_can_target_an_exact_ready_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_cli_inprocess(
                tmp,
                "init",
                "--title",
                "Exact claim test",
                "--lead",
                "lead",
                "--member",
                "builder=Build",
                "--member",
                "reviewer=Review",
                "--task",
                "T1:builder:First builder task",
                "--task",
                "T2:builder:Second builder task",
                "--task",
                "T3:reviewer:Review task",
                "--task",
                "T4:builder:Blocked builder task",
                "--depends",
                "T4:T1",
            )

            wrong_owner = run_cli_inprocess(tmp, "claim", "--agent", "builder", "--task", "T3", check=False)
            self.assertEqual(wrong_owner.returncode, 2)
            self.assertIn("wrong_agent", wrong_owner.stdout)

            blocked = run_cli_inprocess(tmp, "claim", "--agent", "builder", "--task", "T4", check=False)
            self.assertEqual(blocked.returncode, 2)
            self.assertIn("dependencies_open", blocked.stdout)

            claimed = json.loads(run_cli_inprocess(tmp, "claim", "--agent", "builder", "--task", "T2").stdout)
            self.assertEqual(claimed["task"]["id"], "T2")

            active = run_cli_inprocess(tmp, "claim", "--agent", "builder", "--task", "T1", check=False)
            self.assertEqual(active.returncode, 2)
            self.assertIn("active_task", active.stdout)

    def test_claim_refuses_when_agent_already_has_active_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_cli(
                tmp,
                "init",
                "--title",
                "Active claim test",
                "--lead",
                "lead",
                "--member",
                "builder=Build",
                "--task",
                "T1:builder:First task",
                "--task",
                "T2:builder:Second task",
            )

            first = json.loads(run_cli(tmp, "claim", "--agent", "builder").stdout)
            self.assertEqual(first["task"]["id"], "T1")

            blocked = run_cli(tmp, "claim", "--agent", "builder", check=False)
            self.assertEqual(blocked.returncode, 2)
            self.assertIn("active_task", blocked.stdout)
            state = read_state(tmp)
            self.assertEqual(state["tasks"]["T1"]["status"], "in_progress")
            self.assertEqual(state["tasks"]["T2"]["status"], "todo")

    def test_complete_requires_claimed_in_progress_task_and_completed_dependencies(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_cli(
                tmp,
                "init",
                "--title",
                "Complete invariant test",
                "--lead",
                "lead",
                "--member",
                "researcher=Research",
                "--member",
                "builder=Build",
                "--task",
                "T1:researcher:Research",
                "--task",
                "T2:builder:Build",
                "--depends",
                "T2:T1",
            )

            deps_blocked = run_cli(
                tmp,
                "complete",
                "--task",
                "T2",
                "--agent",
                "builder",
                "--summary",
                "Built without deps",
                check=False,
            )
            self.assertEqual(deps_blocked.returncode, 2)
            self.assertIn("dependencies_open", deps_blocked.stdout)

            run_cli(tmp, "claim", "--agent", "researcher")
            run_cli(tmp, "complete", "--task", "T1", "--agent", "researcher", "--summary", "Research done")

            unclaimed = run_cli(
                tmp,
                "complete",
                "--task",
                "T2",
                "--agent",
                "builder",
                "--summary",
                "Built without claim",
                check=False,
            )
            self.assertEqual(unclaimed.returncode, 2)
            self.assertIn("task_not_claimed", unclaimed.stdout)

            run_cli(tmp, "claim", "--agent", "builder")
            done = json.loads(
                run_cli(tmp, "complete", "--task", "T2", "--agent", "builder", "--summary", "Built").stdout
            )
            self.assertEqual(done["task"]["status"], "done")

    def test_messages_gates_and_stop_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_cli(
                tmp,
                "init",
                "--title",
                "Stop gate test",
                "--lead",
                "lead",
                "--member",
                "reviewer=Review",
                "--task",
                "T1:reviewer:Review implementation",
            )
            message = json.loads(
                run_cli(
                    tmp,
                    "message",
                    "--from",
                    "lead",
                    "--to",
                    "reviewer",
                    "--body",
                    "Check the stop gate behavior.",
                ).stdout
            )
            self.assertEqual(message["message"]["to"], ["reviewer"])

            blocked = run_cli(tmp, "stop-check", check=False)
            self.assertEqual(blocked.returncode, 2)
            self.assertIn("tasks_open", blocked.stdout)

            run_cli(tmp, "claim", "--agent", "reviewer")
            run_cli(tmp, "complete", "--task", "T1", "--agent", "reviewer", "--summary", "Reviewed")

            verify_blocked = run_cli(tmp, "stop-check", check=False)
            self.assertEqual(verify_blocked.returncode, 2)
            self.assertIn("verification_pending", verify_blocked.stdout)

            run_cli(tmp, "gate", "--verification-passed", "true")
            ok = json.loads(run_cli(tmp, "stop-check").stdout)
            self.assertTrue(ok["ok"])
            self.assertEqual(ok["blocking"], [])

    def test_actor_guardrails_block_worker_lead_commands_and_spoofing(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_cli(
                tmp,
                "init",
                "--title",
                "Actor guard test",
                "--lead",
                "lead",
                "--member",
                "builder=Build",
                "--task",
                "T1:builder:Build",
            )

            worker_gate = run_cli_with_prefix(
                "--workspace",
                tmp,
                "--actor",
                "builder",
                "gate",
                "--verification-passed",
                "true",
                check=False,
            )
            self.assertEqual(worker_gate.returncode, 2)
            self.assertIn("permission_denied", worker_gate.stdout)

            spoofed_message = run_cli_with_prefix(
                "--workspace",
                tmp,
                "--actor",
                "builder",
                "message",
                "--from",
                "lead",
                "--to",
                "builder",
                "--body",
                "spoof",
                check=False,
            )
            self.assertEqual(spoofed_message.returncode, 2)
            self.assertIn("sender_spoof", spoofed_message.stdout)

            lead_gate = json.loads(
                run_cli_with_prefix(
                    "--workspace",
                    tmp,
                    "--actor",
                    "lead",
                    "gate",
                    "--verification-passed",
                    "true",
                ).stdout
            )
            self.assertTrue(lead_gate["gates"]["verification_passed"])

    def test_actor_guardrails_require_lead_for_repair_when_state_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_cli(
                tmp,
                "init",
                "--title",
                "Repair actor guard test",
                "--lead",
                "lead",
                "--member",
                "builder=Build",
                "--task",
                "T1:builder:Build",
            )

            worker_repair = run_cli_with_prefix(
                "--workspace",
                tmp,
                "--actor",
                "builder",
                "repair",
                "--restore-backup",
                check=False,
            )
            self.assertEqual(worker_repair.returncode, 2)
            self.assertIn("permission_denied", worker_repair.stdout)

            lead_repair = json.loads(
                run_cli_with_prefix("--workspace", tmp, "--actor", "lead", "repair").stdout
            )
            self.assertTrue(lead_repair["ok"])

    def test_repair_refuses_mutation_when_fresh_lock_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_cli(
                tmp,
                "init",
                "--title",
                "Repair lock guard test",
                "--lead",
                "lead",
                "--member",
                "builder=Build",
                "--task",
                "T1:builder:Build",
            )
            lock_path = Path(tmp) / ".codex-agent-teams" / "state.lock"
            lock_path.write_text('{"pid": 999999, "command": "still-active"}', encoding="utf-8")

            blocked = run_cli(tmp, "repair", "--clean-temps", check=False)

            self.assertEqual(blocked.returncode, 2)
            self.assertIn("state_locked", blocked.stdout)

    def test_stale_state_lock_is_recovered(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_cli(
                tmp,
                "init",
                "--title",
                "Stale lock test",
                "--lead",
                "lead",
                "--member",
                "builder=Build",
                "--task",
                "T1:builder:Build",
            )
            lock_path = Path(tmp) / ".codex-agent-teams" / "state.lock"
            lock_path.write_text('{"pid": 999999, "command": "dead-test"}', encoding="utf-8")
            old = time.time() - 120
            os.utime(lock_path, (old, old))

            message = json.loads(
                run_cli(tmp, "message", "--from", "lead", "--to", "builder", "--body", "Recovered.").stdout
            )

            self.assertTrue(message["ok"])
            self.assertFalse(lock_path.exists())
            stale_locks = list(lock_path.parent.glob("state.lock.stale.*"))
            self.assertEqual(len(stale_locks), 1)

    def test_state_backup_and_repair_restore_corrupt_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_cli(
                tmp,
                "init",
                "--title",
                "Repair test",
                "--lead",
                "lead",
                "--member",
                "builder=Build",
                "--task",
                "T1:builder:Build",
            )
            run_cli(tmp, "message", "--from", "lead", "--to", "builder", "--body", "Keep me.")
            state_file = Path(tmp) / ".codex-agent-teams" / "state.json"
            backup = state_file.with_name("state.json.bak")
            self.assertTrue(backup.exists())

            state_file.write_text("{broken json", encoding="utf-8")
            repair = json.loads(run_cli(tmp, "repair", "--restore-backup").stdout)

            self.assertTrue(repair["state"]["restored_from_backup"])
            restored = read_state(tmp)
            self.assertEqual(restored["messages"][0]["body"], "Keep me.")
            corrupt_archives = list(state_file.parent.glob("state.json.corrupt.*"))
            self.assertEqual(len(corrupt_archives), 1)

    def test_team_alias_broadcasts_to_everyone_except_sender(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_cli(
                tmp,
                "init",
                "--title",
                "Team alias test",
                "--lead",
                "lead",
                "--member",
                "researcher=Research",
                "--member",
                "builder=Build",
                "--task",
                "T1:researcher:Research",
            )

            lead_message = json.loads(
                run_cli(tmp, "message", "--from", "lead", "--to", "team", "--body", "Hello team").stdout
            )
            self.assertEqual(lead_message["message"]["to"], ["builder", "researcher"])

            member_message = json.loads(
                run_cli(tmp, "message", "--from", "researcher", "--to", "team", "--body", "Peer note").stdout
            )
            self.assertEqual(member_message["message"]["to"], ["builder", "lead"])

    def test_plan_required_gate_blocks_completion_until_approved(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_cli(
                tmp,
                "init",
                "--title",
                "Plan gate test",
                "--lead",
                "lead",
                "--member",
                "builder=Build",
                "--task",
                "T1:builder:Implement",
                "--plan-required",
            )
            run_cli(tmp, "claim", "--agent", "builder")

            blocked = run_cli(
                tmp,
                "complete",
                "--task",
                "T1",
                "--agent",
                "builder",
                "--summary",
                "Built",
                check=False,
            )
            self.assertEqual(blocked.returncode, 2)
            self.assertIn("plan_approval_required", blocked.stdout)

            run_cli(tmp, "gate", "--plan-approved", "true")
            done = json.loads(
                run_cli(tmp, "complete", "--task", "T1", "--agent", "builder", "--summary", "Built").stdout
            )
            self.assertEqual(done["task"]["status"], "done")

    def test_cleanup_refuses_active_team_without_force(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_cli(
                tmp,
                "init",
                "--title",
                "Cleanup test",
                "--lead",
                "lead",
                "--member",
                "builder=Build",
                "--task",
                "T1:builder:Implement",
            )
            run_cli(tmp, "claim", "--agent", "builder")

            refused = run_cli(tmp, "cleanup", check=False)
            self.assertEqual(refused.returncode, 2)
            self.assertIn("active_work", refused.stdout)

            cleaned = json.loads(run_cli(tmp, "cleanup", "--force").stdout)
            self.assertEqual(cleaned["status"], "cleaned")
            state = read_state(tmp)
            self.assertEqual(state["status"], "cleaned")

    def test_cleanup_refuses_stop_report_blockers_without_force(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_cli(
                tmp,
                "init",
                "--title",
                "Cleanup stop report test",
                "--lead",
                "lead",
                "--member",
                "builder=Build",
                "--task",
                "T1:builder:Implement",
            )

            tasks_open = run_cli(tmp, "cleanup", check=False)
            self.assertEqual(tasks_open.returncode, 2)
            self.assertIn("stop_blocked", tasks_open.stdout)
            self.assertIn("tasks_open", tasks_open.stdout)

            run_cli(tmp, "claim", "--agent", "builder")
            run_cli(tmp, "complete", "--task", "T1", "--agent", "builder", "--summary", "Built")

            verification_open = run_cli(tmp, "cleanup", check=False)
            self.assertEqual(verification_open.returncode, 2)
            self.assertIn("verification_pending", verification_open.stdout)

            run_cli(tmp, "gate", "--verification-passed", "true")
            cleaned = json.loads(run_cli(tmp, "cleanup").stdout)
            self.assertEqual(cleaned["status"], "cleaned")

    def test_launch_plan_prefers_subagents_over_thread_forks(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_cli(
                tmp,
                "init",
                "--title",
                "Subagent launch test",
                "--lead",
                "lead",
                "--member",
                "researcher=Research",
                "--member",
                "builder=Build",
                "--task",
                "T1:researcher:Research docs",
                "--task",
                "T2:builder:Build result",
            )

            plan = json.loads(run_cli(tmp, "launch-plan", "--backend", "subagent").stdout)

            self.assertEqual(plan["backend"], "subagent")
            self.assertEqual([action["tool"] for action in plan["actions"]], ["multi_agent_v1.spawn_agent", "multi_agent_v1.spawn_agent"])
            self.assertEqual([action["agent"] for action in plan["actions"]], ["researcher", "builder"])
            self.assertNotIn("fork_thread", json.dumps(plan))
            self.assertIn("bind-subagent --agent researcher --agent-id <agent_id>", plan["actions"][0]["record_command"])
            self.assertIn("Claim one ready task", plan["actions"][0]["spawn_args"]["message"])
            self.assertTrue(plan["actions"][0]["spawn_args"]["fork_context"])
            self.assertNotIn("agent_type", plan["actions"][0]["spawn_args"])

    def test_launch_plan_prompts_include_absolute_workspace_and_state_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_cli(
                tmp,
                "init",
                "--title",
                "Prompt path test",
                "--lead",
                "lead",
                "--member",
                "researcher=Research",
                "--task",
                "T1:researcher:Research docs",
            )

            plan = json.loads(run_cli(tmp, "launch-plan", "--backend", "subagent").stdout)
            prompt = plan["actions"][0]["spawn_args"]["message"]
            workspace = str(Path(tmp).resolve())
            state_file = str((Path(tmp) / ".codex-agent-teams" / "state.json").resolve())

            self.assertIn(f"Team workspace: {workspace}", prompt)
            self.assertIn(f"State file: {state_file}", prompt)
            self.assertIn(f"--workspace \"{workspace}\"", prompt)
            self.assertIn("Do not run orchestrate, record-wait, record-delivery", prompt)
            self.assertIn("record-close", prompt)

    def test_script_invocation_uses_stable_command_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"CODEX_AGENT_TEAM_COMMAND": "codex-agent-team"}
            run_cli_inprocess(
                tmp,
                "init",
                "--title",
                "Command override test",
                "--lead",
                "lead",
                "--member",
                "builder=Build",
                "--task",
                "T1:builder:Build",
                env=env,
            )

            plan = json.loads(run_cli_inprocess(tmp, "launch-plan", "--backend", "subagent", env=env).stdout)
            command_prefix = f'codex-agent-team --workspace "{Path(tmp).resolve()}"'
            self.assertIn(command_prefix, plan["actions"][0]["record_command"])
            self.assertIn(f"Team command: {command_prefix}", plan["actions"][0]["spawn_args"]["message"])

            run_cli_inprocess(tmp, "bind-subagent", "--agent", "builder", "--agent-id", "agent-builder", env=env)
            close = json.loads(run_cli_inprocess(tmp, "close-plan", env=env).stdout)
            self.assertIn(command_prefix, close["actions"][0]["record_command"])

    def test_prompts_show_current_task_title_and_avoid_duplicate_claim_instruction(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_cli(
                tmp,
                "init",
                "--title",
                "Prompt duplication test",
                "--lead",
                "lead",
                "--member",
                "builder=Build",
                "--task",
                "T1:builder:Build the thing",
            )
            launch_prompt = json.loads(run_cli(tmp, "launch-plan", "--backend", "subagent").stdout)["actions"][0][
                "spawn_args"
            ]["message"]
            self.assertEqual(launch_prompt.count("Claim one ready task"), 1)

            run_cli(tmp, "claim", "--agent", "builder")
            prompt = json.loads(run_cli(tmp, "prompt", "--agent", "builder").stdout)["prompt"]
            self.assertIn("Current task: T1 - Build the thing", prompt)

    def test_launch_prompts_include_brief_context_member_context_and_quality_rules(self):
        with tempfile.TemporaryDirectory() as tmp:
            context_file = Path(tmp) / "audit-context.md"
            context_file.write_text("Focus on state durability and dashboard next actions.", encoding="utf-8")
            member_file = Path(tmp) / "builder-context.md"
            member_file.write_text("Builder should inspect scripts/agent_team.py first.", encoding="utf-8")
            run_cli(
                tmp,
                "init",
                "--title",
                "Prompt context test",
                "--lead",
                "lead",
                "--member",
                "builder=Build",
                "--task",
                "T1:builder:Build",
                "--brief",
                "Harden the agent team plugin.",
                "--context-file",
                "audit-context.md",
                "--member-context",
                "builder=Prefer small, verified patches.",
                "--member-context-file",
                "builder=builder-context.md",
                "--require-citations",
                "--verification-check",
                "Cite changed files and commands run.",
            )

            plan = json.loads(run_cli(tmp, "launch-plan", "--backend", "subagent").stdout)
            prompt = plan["actions"][0]["spawn_args"]["message"]

            self.assertIn("Team brief:", prompt)
            self.assertIn("Harden the agent team plugin.", prompt)
            self.assertIn("Shared context files:", prompt)
            self.assertIn("Focus on state durability", prompt)
            self.assertIn("Agent-specific context:", prompt)
            self.assertIn("Prefer small, verified patches.", prompt)
            self.assertIn("Builder should inspect scripts/agent_team.py first.", prompt)
            self.assertIn("Citation quality:", prompt)
            self.assertIn("Cite changed files and commands run.", prompt)

    def test_bind_subagent_and_message_delivery_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_cli(
                tmp,
                "init",
                "--title",
                "Subagent bind test",
                "--lead",
                "lead",
                "--member",
                "researcher=Research",
                "--task",
                "T1:researcher:Research docs",
            )

            bound = json.loads(
                run_cli(
                    tmp,
                    "bind-subagent",
                    "--agent",
                    "researcher",
                    "--agent-id",
                    "agent-123",
                    "--nickname",
                    "Ptolemy",
                ).stdout
            )
            self.assertEqual(bound["member"]["runtime"]["backend"], "subagent")
            self.assertEqual(bound["member"]["runtime"]["agent_id"], "agent-123")

            message = json.loads(
                run_cli(
                    tmp,
                    "message",
                    "--from",
                    "lead",
                    "--to",
                    "researcher",
                    "--body",
                    "Please continue.",
                ).stdout
            )
            self.assertEqual(message["deliveries"][0]["tool"], "multi_agent_v1.send_input")
            self.assertEqual(message["deliveries"][0]["send_input_args"]["target"], "agent-123")
            self.assertIn("Please continue.", message["deliveries"][0]["send_input_args"]["message"])

    def test_close_plan_emits_subagent_close_actions(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_cli(
                tmp,
                "init",
                "--title",
                "Subagent close test",
                "--lead",
                "lead",
                "--member",
                "builder=Build",
                "--task",
                "T1:builder:Build",
            )
            run_cli(tmp, "bind-subagent", "--agent", "builder", "--agent-id", "agent-456")

            plan = json.loads(run_cli(tmp, "close-plan").stdout)

            self.assertEqual(plan["actions"], [
                {
                    "agent": "builder",
                    "tool": "multi_agent_v1.close_agent",
                    "close_agent_args": {"target": "agent-456"},
                    "record_command": f'python "{SCRIPT}" --workspace "{Path(tmp).resolve()}" record-close --agent builder --status closed --summary <close_result>',
                }
            ])

    def test_cleanup_requires_recorded_runtime_close_before_state_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_cli(
                tmp,
                "init",
                "--title",
                "Runtime close cleanup test",
                "--lead",
                "lead",
                "--member",
                "builder=Build",
                "--task",
                "T1:builder:Build",
            )
            run_cli(tmp, "bind-subagent", "--agent", "builder", "--agent-id", "agent-builder")
            run_cli(tmp, "claim", "--agent", "builder")
            run_cli(tmp, "complete", "--task", "T1", "--agent", "builder", "--summary", "Built")
            run_cli(tmp, "gate", "--verification-passed", "true")
            run_cli(tmp, "record-wait", "--agent", "builder", "--status", "completed", "--summary", "Done.")

            refused = run_cli(tmp, "cleanup", check=False)
            self.assertEqual(refused.returncode, 2)
            self.assertIn("runtime_open", refused.stdout)
            self.assertIn("builder", refused.stdout)

            run_cli(tmp, "record-close", "--agent", "builder", "--status", "closed", "--summary", "completed")
            plan = json.loads(run_cli(tmp, "close-plan").stdout)
            self.assertEqual(plan["actions"], [])

            ready = json.loads(run_cli(tmp, "orchestrate").stdout)
            self.assertEqual(ready["phase"], "cleanup_ready")

            cleaned = json.loads(run_cli(tmp, "cleanup").stdout)
            self.assertEqual(cleaned["status"], "cleaned")

    def test_failed_runtime_close_blocks_cleanup_until_successfully_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_cli(
                tmp,
                "init",
                "--title",
                "Failed close test",
                "--lead",
                "lead",
                "--member",
                "builder=Build",
                "--task",
                "T1:builder:Build",
            )
            run_cli(tmp, "bind-subagent", "--agent", "builder", "--agent-id", "agent-builder")
            run_cli(tmp, "claim", "--agent", "builder")
            run_cli(tmp, "complete", "--task", "T1", "--agent", "builder", "--summary", "Built")
            run_cli(tmp, "gate", "--verification-passed", "true")
            run_cli(tmp, "record-wait", "--agent", "builder", "--status", "completed", "--summary", "Done.")

            failed = json.loads(
                run_cli(tmp, "record-close", "--agent", "builder", "--status", "failed", "--summary", "close failed").stdout
            )
            self.assertEqual(failed["member"]["runtime"]["close_status"], "failed")

            refused = run_cli(tmp, "cleanup", check=False)
            self.assertEqual(refused.returncode, 2)
            self.assertIn("runtime_open", refused.stdout)

            run_cli(tmp, "record-close", "--agent", "builder", "--status", "closed", "--summary", "retry ok")
            cleaned = json.loads(run_cli(tmp, "cleanup").stdout)
            self.assertEqual(cleaned["status"], "cleaned")

    def test_record_close_summary_file_handles_multiline_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_cli(
                tmp,
                "init",
                "--title",
                "Close summary file test",
                "--lead",
                "lead",
                "--member",
                "builder=Build",
                "--task",
                "T1:builder:Build",
            )
            run_cli(tmp, "bind-subagent", "--agent", "builder", "--agent-id", "agent-builder")
            summary = "Closed cleanly.\nNo leaked handles."
            summary_path = Path(tmp) / "close-summary.txt"
            summary_path.write_text(summary, encoding="utf-8")

            closed = json.loads(
                run_cli(
                    tmp,
                    "record-close",
                    "--agent",
                    "builder",
                    "--status",
                    "closed",
                    "--summary-file",
                    "close-summary.txt",
                ).stdout
            )

            self.assertEqual(closed["member"]["runtime"]["close_result"], summary)

    def test_record_close_batch_maps_host_close_result_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_cli_inprocess(
                tmp,
                "init",
                "--title",
                "Close batch test",
                "--lead",
                "lead",
                "--member",
                "alpha=Alpha",
                "--member",
                "beta=Beta",
                "--task",
                "T1:alpha:Alpha work",
                "--task",
                "T2:beta:Beta work",
            )
            run_cli_inprocess(tmp, "bind-subagent", "--agent", "alpha", "--agent-id", "agent-alpha")
            run_cli_inprocess(tmp, "bind-subagent", "--agent", "beta", "--agent-id", "agent-beta")
            result_path = Path(tmp) / "close-results.json"
            result_path.write_text(
                json.dumps(
                    {
                        "results": [
                            {"target": "agent-alpha", "status": "closed", "summary": "Alpha closed."},
                            {"target": "agent-beta", "status": "not_found", "message": "Beta was already gone."},
                            {"target": "agent-missing", "status": "closed", "summary": "Unknown handle."},
                        ]
                    }
                ),
                encoding="utf-8",
            )

            recorded = json.loads(
                run_cli_inprocess(tmp, "record-close-batch", "--result-file", "close-results.json").stdout
            )

            self.assertEqual([record["agent"] for record in recorded["records"]], ["alpha", "beta"])
            self.assertEqual(len(recorded["unmatched"]), 1)
            state = read_state(tmp)
            self.assertEqual(state["members"][0]["runtime"]["close_status"], "closed")
            self.assertEqual(state["members"][1]["runtime"]["close_status"], "not_found")
            self.assertEqual(state["members"][1]["runtime"]["close_result"], "Beta was already gone.")

    def test_orchestrate_emits_spawn_phase_for_unbound_members(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_cli(
                tmp,
                "init",
                "--title",
                "Orchestrate spawn test",
                "--lead",
                "lead",
                "--member",
                "researcher=Research",
                "--task",
                "T1:researcher:Research",
            )

            plan = json.loads(run_cli(tmp, "orchestrate").stdout)

            self.assertEqual(plan["phase"], "spawn_unbound")
            self.assertEqual(plan["actions"][0]["action_id"], "spawn:researcher")
            self.assertEqual(plan["actions"][0]["tool"], "multi_agent_v1.spawn_agent")
            self.assertIn("bind-subagent", plan["actions"][0]["record_command"])

    def test_orchestrate_emits_close_phase_after_stop_check_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_cli(
                tmp,
                "init",
                "--title",
                "Orchestrate close test",
                "--lead",
                "lead",
                "--member",
                "builder=Build",
                "--task",
                "T1:builder:Build",
            )
            run_cli(tmp, "bind-subagent", "--agent", "builder", "--agent-id", "agent-builder")
            run_cli(tmp, "claim", "--agent", "builder")
            run_cli(tmp, "complete", "--task", "T1", "--agent", "builder", "--summary", "Built")
            run_cli(tmp, "gate", "--verification-passed", "true")

            waiting = json.loads(run_cli(tmp, "orchestrate").stdout)
            self.assertEqual(waiting["phase"], "wait_agents")
            self.assertTrue(waiting["wait_all_required"])
            self.assertEqual(waiting["waiting_agents"], ["builder"])
            self.assertEqual(waiting["actions"][0]["tool"], "multi_agent_v1.wait_agent")
            self.assertEqual(waiting["actions"][0]["wait_policy"]["mode"], "all_targets")
            self.assertTrue(waiting["actions"][0]["wait_policy"]["require_final_statuses"])
            self.assertIn("partial result", waiting["actions"][0]["wait_policy"]["instruction"])

            run_cli(
                tmp,
                "record-wait",
                "--agent",
                "builder",
                "--status",
                "completed",
                "--summary",
                "Builder finished cleanly.",
            )
            plan = json.loads(run_cli(tmp, "orchestrate").stdout)

            self.assertEqual(plan["phase"], "close_ready")
            self.assertEqual(plan["actions"][0]["action_id"], "close:builder")
            self.assertEqual(plan["actions"][0]["tool"], "multi_agent_v1.close_agent")

    def test_record_wait_stores_runtime_result_and_clears_pending_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_cli(
                tmp,
                "init",
                "--title",
                "Record wait test",
                "--lead",
                "lead",
                "--member",
                "builder=Build",
                "--task",
                "T1:builder:Build",
            )
            run_cli(tmp, "bind-subagent", "--agent", "builder", "--agent-id", "agent-builder")
            message = json.loads(
                run_cli(tmp, "message", "--from", "lead", "--to", "builder", "--body", "Please finish.").stdout
            )

            self.assertEqual(message["message"]["delivery_status"], {"builder": "pending"})
            self.assertEqual(message["deliveries"][0]["message_id"], "m1")

            state = read_state(tmp)
            runtime = state["members"][0]["runtime"]
            self.assertEqual(runtime["status"], "input_sent")
            self.assertEqual(runtime["pending_inputs"], 1)

            run_cli(tmp, "record-wait", "--agent", "builder", "--status", "completed", "--summary", "Done.")
            state = read_state(tmp)
            runtime = state["members"][0]["runtime"]
            self.assertEqual(runtime["status"], "completed")
            self.assertEqual(runtime["pending_inputs"], 0)
            self.assertEqual(runtime["last_result"], "Done.")
            self.assertEqual(state["messages"][0]["delivery_status"]["builder"], "pending")

    def test_record_wait_batch_maps_host_wait_result_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_cli(
                tmp,
                "init",
                "--title",
                "Wait batch test",
                "--lead",
                "lead",
                "--member",
                "builder=Build",
                "--member",
                "reviewer=Review",
                "--task",
                "T1:builder:Build",
                "--task",
                "T2:reviewer:Review",
            )
            run_cli(tmp, "bind-subagent", "--agent", "builder", "--agent-id", "agent-builder")
            run_cli(tmp, "bind-subagent", "--agent", "reviewer", "--agent-id", "agent-reviewer")
            result_file = Path(tmp) / "wait-result.json"
            result_file.write_text(
                json.dumps(
                    {
                        "results": [
                            {"target": "agent-builder", "status": "succeeded", "summary": "Builder done."},
                            {"agent_id": "agent-reviewer", "state": "timeout", "output": "Reviewer still running."},
                        ]
                    }
                ),
                encoding="utf-8",
            )

            batch = json.loads(run_cli(tmp, "record-wait-batch", "--result-file", "wait-result.json").stdout)

            self.assertEqual([record["agent"] for record in batch["records"]], ["builder", "reviewer"])
            self.assertEqual([record["status"] for record in batch["records"]], ["completed", "timed_out"])
            state = read_state(tmp)
            runtimes = {member["name"]: member["runtime"] for member in state["members"]}
            self.assertEqual(runtimes["builder"]["status"], "completed")
            self.assertEqual(runtimes["builder"]["last_result"], "Builder done.")
            self.assertEqual(runtimes["reviewer"]["status"], "timed_out")
            self.assertEqual(runtimes["reviewer"]["wait_timeouts"], 1)

    def test_message_to_completed_runtime_does_not_queue_host_delivery(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_cli(
                tmp,
                "init",
                "--title",
                "Completed delivery test",
                "--lead",
                "lead",
                "--member",
                "builder=Build",
                "--task",
                "T1:builder:Build",
            )
            run_cli(tmp, "bind-subagent", "--agent", "builder", "--agent-id", "agent-builder")
            run_cli(tmp, "record-wait", "--agent", "builder", "--status", "completed", "--summary", "Done.")

            message = json.loads(
                run_cli(tmp, "message", "--from", "lead", "--to", "builder", "--body", "Post-run note.").stdout
            )
            state = read_state(tmp)

            self.assertEqual(message["deliveries"], [])
            self.assertEqual(message["message"]["delivery_status"], {})
            self.assertEqual(state["members"][0]["runtime"]["pending_inputs"], 0)

    def test_record_wait_does_not_downgrade_final_runtime_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_cli(
                tmp,
                "init",
                "--title",
                "Record wait monotonic test",
                "--lead",
                "lead",
                "--member",
                "builder=Build",
                "--task",
                "T1:builder:Build",
            )
            run_cli(tmp, "bind-subagent", "--agent", "builder", "--agent-id", "agent-builder")
            run_cli(tmp, "record-wait", "--agent", "builder", "--status", "completed", "--summary", "Final.")

            result = json.loads(
                run_cli(
                    tmp,
                    "record-wait",
                    "--agent",
                    "builder",
                    "--status",
                    "timed_out",
                    "--summary",
                    "Stale timeout.",
                ).stdout
            )
            state = read_state(tmp)
            runtime = state["members"][0]["runtime"]

            self.assertEqual(result["ignored_status"], "timed_out")
            self.assertEqual(runtime["status"], "completed")
            self.assertEqual(runtime["wait_timeouts"], 0)
            self.assertEqual(runtime["last_result"], "Final.")
            self.assertEqual(state["events"][-1]["status"], "completed")
            self.assertEqual(state["events"][-1]["ignored_status"], "timed_out")

    def test_orchestrate_reemits_pending_message_deliveries_until_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_cli(
                tmp,
                "init",
                "--title",
                "Pending delivery test",
                "--lead",
                "lead",
                "--member",
                "builder=Build",
                "--task",
                "T1:builder:Build",
            )
            run_cli(tmp, "bind-subagent", "--agent", "builder", "--agent-id", "agent-builder")
            run_cli(tmp, "claim", "--agent", "builder")
            run_cli(tmp, "complete", "--task", "T1", "--agent", "builder", "--summary", "Built")
            run_cli(tmp, "message", "--from", "lead", "--to", "builder", "--body", "Review peer note.")

            plan = json.loads(run_cli(tmp, "orchestrate").stdout)
            self.assertEqual(plan["phase"], "deliver_messages")
            self.assertEqual(plan["actions"][0]["action_id"], "deliver:m1:builder")
            self.assertEqual(plan["actions"][0]["tool"], "multi_agent_v1.send_input")
            self.assertEqual(plan["actions"][0]["message_id"], "m1")

            run_cli(tmp, "record-delivery", "--message", "m1", "--agent", "builder", "--status", "sent")
            state = read_state(tmp)
            self.assertEqual(state["messages"][0]["delivery_status"]["builder"], "sent")
            self.assertEqual(state["members"][0]["runtime"]["status"], "input_sent")
            self.assertEqual(state["members"][0]["runtime"]["pending_inputs"], 1)

            plan = json.loads(run_cli(tmp, "orchestrate").stdout)
            self.assertEqual(plan["phase"], "wait_agents")

    def test_record_delivery_does_not_downgrade_read_or_sent_delivery(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_cli(
                tmp,
                "init",
                "--title",
                "Delivery monotonic test",
                "--lead",
                "lead",
                "--member",
                "builder=Build",
                "--task",
                "T1:builder:Build",
            )
            run_cli(tmp, "bind-subagent", "--agent", "builder", "--agent-id", "agent-builder")
            run_cli(tmp, "message", "--from", "lead", "--to", "builder", "--body", "Please review.")
            run_cli(tmp, "ack", "--agent", "builder", "--message", "m1")

            result = json.loads(
                run_cli(tmp, "record-delivery", "--message", "m1", "--agent", "builder", "--status", "failed").stdout
            )
            state = read_state(tmp)
            runtime = state["members"][0]["runtime"]

            self.assertEqual(result["ignored_status"], "failed")
            self.assertEqual(state["messages"][0]["delivery_status"]["builder"], "read")
            self.assertEqual(runtime["pending_inputs"], 0)
            self.assertEqual(state["events"][-1]["status"], "read")
            self.assertEqual(state["events"][-1]["ignored_status"], "failed")

    def test_record_wait_summary_file_handles_multiline_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_cli(
                tmp,
                "init",
                "--title",
                "Record wait file test",
                "--lead",
                "lead",
                "--member",
                "builder=Build",
                "--task",
                "T1:builder:Build",
            )
            run_cli(tmp, "bind-subagent", "--agent", "builder", "--agent-id", "agent-builder")
            summary = "Final result with quotes, apostrophes, and newlines:\n\"ok\"\nIt isn't tiny."
            summary_path = Path(tmp) / "wait-summary.txt"
            summary_path.write_text(summary, encoding="utf-8")

            run_cli(
                tmp,
                "record-wait",
                "--agent",
                "builder",
                "--status",
                "completed",
                "--summary-file",
                str(summary_path),
            )

            state = read_state(tmp)
            self.assertEqual(state["members"][0]["runtime"]["last_result"], summary)

    def test_orchestrate_emits_finalize_prompt_after_wait_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_cli(
                tmp,
                "init",
                "--title",
                "Finalize timeout test",
                "--lead",
                "lead",
                "--member",
                "builder=Build",
                "--task",
                "T1:builder:Build",
            )
            run_cli(tmp, "bind-subagent", "--agent", "builder", "--agent-id", "agent-builder")
            run_cli(tmp, "record-wait", "--agent", "builder", "--status", "timed_out")

            plan = json.loads(run_cli(tmp, "orchestrate").stdout)

            self.assertEqual(plan["phase"], "finalize_overdue")
            self.assertEqual(plan["actions"][0]["action_id"], "finalize:builder")
            self.assertEqual(plan["actions"][0]["tool"], "multi_agent_v1.send_input")
            self.assertIn("final", plan["actions"][0]["send_input_args"]["message"].lower())

    def test_launch_initializes_team_and_outputs_subagent_actions(self):
        with tempfile.TemporaryDirectory() as tmp:
            launched = json.loads(
                run_cli(
                    tmp,
                    "launch",
                    "--title",
                    "One command team",
                    "--lead",
                    "lead",
                    "--member",
                    "researcher=Research",
                    "--task",
                    "T1:researcher:Research",
                ).stdout
            )

            state = read_state(tmp)
            self.assertEqual(launched["team_id"], state["team_id"])
            self.assertEqual(launched["launch"]["backend"], "subagent")
            self.assertEqual(launched["launch"]["actions"][0]["tool"], "multi_agent_v1.spawn_agent")
            self.assertEqual(state["runtime"]["preferred_backend"], "subagent")

    def test_dashboard_inbox_and_ack_are_human_readable(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_cli(
                tmp,
                "init",
                "--title",
                "Dashboard test",
                "--lead",
                "lead",
                "--member",
                "researcher=Research",
                "--task",
                "T1:researcher:Research",
            )
            run_cli(tmp, "message", "--from", "lead", "--to", "researcher", "--body", "Read this.")

            dashboard = json.loads(run_cli(tmp, "dashboard").stdout)
            self.assertIn("Dashboard test", dashboard["dashboard"])
            self.assertIn("T1", dashboard["dashboard"])
            self.assertIn("verification", dashboard["dashboard"].lower())
            self.assertIn("runtime=", dashboard["dashboard"])
            self.assertIn("pending_inputs=", dashboard["dashboard"])

            inbox = json.loads(run_cli(tmp, "inbox", "--agent", "researcher").stdout)
            self.assertIn("m1", inbox["inbox"])
            self.assertIn("Read this.", inbox["inbox"])

            ack = json.loads(run_cli(tmp, "ack", "--agent", "researcher", "--message", "m1").stdout)
            self.assertTrue(ack["ok"])
            state = read_state(tmp)
            self.assertEqual(state["messages"][0]["read_by"], ["researcher"])

    def test_dashboard_counts_unread_messages_per_recipient(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_cli(
                tmp,
                "init",
                "--title",
                "Unread count test",
                "--lead",
                "lead",
                "--member",
                "researcher=Research",
                "--member",
                "builder=Build",
                "--task",
                "T1:researcher:Research",
            )
            run_cli(tmp, "message", "--from", "lead", "--to", "team", "--body", "Broadcast")

            before = json.loads(run_cli(tmp, "dashboard").stdout)
            self.assertIn("2 unread recipient", before["dashboard"])

            run_cli(tmp, "ack", "--agent", "researcher", "--message", "m1")
            after = json.loads(run_cli(tmp, "dashboard").stdout)
            self.assertIn("1 unread recipient", after["dashboard"])

    def test_dashboard_warns_when_runtime_wait_state_is_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_cli(
                tmp,
                "init",
                "--title",
                "Runtime warning test",
                "--lead",
                "lead",
                "--member",
                "builder=Build",
                "--task",
                "T1:builder:Build",
            )
            run_cli(tmp, "bind-subagent", "--agent", "builder", "--agent-id", "agent-builder")

            active_dashboard = json.loads(run_cli(tmp, "dashboard").stdout)
            self.assertIn("warning=runtime-active-wait-all-required", active_dashboard["dashboard"])

            run_cli(tmp, "message", "--from", "lead", "--to", "builder", "--body", "Please finish.")
            pending_dashboard = json.loads(run_cli(tmp, "dashboard").stdout)
            self.assertIn("warning=pending-inputs-wait-all-required", pending_dashboard["dashboard"])

    def test_ack_reconciles_pending_delivery_and_skips_redelivery(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_cli(
                tmp,
                "init",
                "--title",
                "Ack delivery test",
                "--lead",
                "lead",
                "--member",
                "researcher=Research",
                "--task",
                "T1:researcher:Research",
            )
            run_cli(tmp, "bind-subagent", "--agent", "researcher", "--agent-id", "agent-researcher")
            run_cli(tmp, "message", "--from", "lead", "--to", "researcher", "--body", "Read this.")

            state = read_state(tmp)
            self.assertEqual(state["messages"][0]["delivery_status"]["researcher"], "pending")
            self.assertEqual(state["members"][0]["runtime"]["pending_inputs"], 1)
            self.assertEqual(state["members"][0]["runtime"]["status"], "input_sent")
            before_ack = json.loads(run_cli(tmp, "orchestrate").stdout)
            self.assertEqual(before_ack["phase"], "deliver_messages")
            self.assertEqual(before_ack["actions"][0]["action_id"], "deliver:m1:researcher")

            run_cli(tmp, "ack", "--agent", "researcher", "--message", "m1")

            state = read_state(tmp)
            self.assertEqual(state["messages"][0]["read_by"], ["researcher"])
            self.assertEqual(state["messages"][0]["delivery_status"]["researcher"], "read")
            self.assertEqual(state["members"][0]["runtime"]["pending_inputs"], 0)
            self.assertEqual(state["members"][0]["runtime"]["status"], "running")
            after_ack = json.loads(run_cli(tmp, "orchestrate").stdout)
            self.assertNotEqual(after_ack["phase"], "deliver_messages")
            self.assertNotIn("deliver:m1:researcher", json.dumps(after_ack))

    def test_message_body_file_handles_long_multiline_peer_notes(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_cli(
                tmp,
                "init",
                "--title",
                "Body file test",
                "--lead",
                "lead",
                "--member",
                "researcher=Research",
                "--task",
                "T1:researcher:Research",
            )
            body = "Peer note with quotes, apostrophes, and newlines:\n\"quoted\" line\nIt isn't short. " + ("x" * 2000)
            body_path = Path(tmp) / "message.txt"
            body_path.write_text(body, encoding="utf-8")

            message = json.loads(
                run_cli(
                    tmp,
                    "message",
                    "--from",
                    "lead",
                    "--to",
                    "researcher",
                    "--body-file",
                    str(body_path),
                ).stdout
            )

            self.assertEqual(message["message"]["body"], body)

    def test_file_options_are_contained_to_workspace_by_default(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside:
            run_cli(
                tmp,
                "init",
                "--title",
                "Containment test",
                "--lead",
                "lead",
                "--member",
                "researcher=Research",
                "--task",
                "T1:researcher:Research",
            )
            outside_body = Path(outside) / "message.txt"
            outside_body.write_text("outside", encoding="utf-8")

            blocked_body = run_cli(
                tmp,
                "message",
                "--from",
                "lead",
                "--to",
                "researcher",
                "--body-file",
                str(outside_body),
                check=False,
            )
            self.assertEqual(blocked_body.returncode, 2)
            self.assertIn("path_outside_workspace", blocked_body.stdout)

            output_path = Path(outside) / "dashboard.md"
            blocked_output = run_cli(
                tmp,
                "dashboard",
                "--format",
                "markdown",
                "--output",
                str(output_path),
                check=False,
            )
            self.assertEqual(blocked_output.returncode, 2)
            self.assertIn("path_outside_workspace", blocked_output.stdout)

            allowed = json.loads(
                run_cli(
                    tmp,
                    "dashboard",
                    "--format",
                    "markdown",
                    "--output",
                    str(output_path),
                    "--allow-outside-workspace",
                ).stdout
            )
            self.assertEqual(Path(allowed["output"]), output_path.resolve())

            run_cli(tmp, "bind-subagent", "--agent", "researcher", "--agent-id", "agent-researcher")
            outside_summary = Path(outside) / "summary.txt"
            outside_summary.write_text("outside summary", encoding="utf-8")
            blocked_summary = run_cli(
                tmp,
                "record-wait",
                "--agent",
                "researcher",
                "--status",
                "completed",
                "--summary-file",
                str(outside_summary),
                check=False,
            )
            self.assertEqual(blocked_summary.returncode, 2)
            self.assertIn("path_outside_workspace", blocked_summary.stdout)

    def test_dashboard_can_emit_markdown_and_write_output_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_cli(
                tmp,
                "init",
                "--title",
                "Markdown dashboard test",
                "--lead",
                "lead",
                "--member",
                "builder=Build",
                "--task",
                "T1:builder:Build",
            )
            output = Path(tmp) / "TEAM_DASHBOARD.md"
            run_cli(tmp, "message", "--from", "lead", "--to", "builder", "--body", "Unread markdown note.")

            dashboard = json.loads(
                run_cli(
                    tmp,
                    "dashboard",
                    "--format",
                    "markdown",
                    "--output",
                    str(output),
                ).stdout
            )

            self.assertIn("# Markdown dashboard test", dashboard["dashboard"])
            self.assertIn("pending_inputs=`0`", dashboard["dashboard"])
            self.assertIn("- Unread recipients: `1`", dashboard["dashboard"])
            self.assertEqual(output.read_text(encoding="utf-8"), dashboard["dashboard"])

    def test_dashboard_includes_attention_buckets(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_cli(
                tmp,
                "init",
                "--title",
                "Attention dashboard test",
                "--lead",
                "lead",
                "--member",
                "researcher=Research",
                "--member",
                "builder=Build",
                "--task",
                "T1:researcher:Research",
                "--task",
                "T2:builder:Build",
                "--depends",
                "T2:T1",
            )
            run_cli(tmp, "bind-subagent", "--agent", "builder", "--agent-id", "agent-builder")
            run_cli(tmp, "message", "--from", "lead", "--to", "builder", "--body", "Please watch T2.")

            dashboard = json.loads(run_cli(tmp, "dashboard").stdout)

            self.assertIn("Attention:", dashboard["dashboard"])
            self.assertIn("ready_tasks=T1", dashboard["dashboard"])
            self.assertIn("blocked_tasks=T2", dashboard["dashboard"])
            self.assertIn("pending_inputs=builder", dashboard["dashboard"])
            self.assertIn("recent_events=", dashboard["dashboard"])

    def test_dashboard_includes_exact_next_actions_for_manual_runtime_steps(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_cli(
                tmp,
                "init",
                "--title",
                "Next actions dashboard test",
                "--lead",
                "lead",
                "--member",
                "researcher=Research",
                "--member",
                "builder=Build",
                "--task",
                "T1:researcher:Research",
                "--task",
                "T2:builder:Build",
            )
            run_cli(tmp, "bind-subagent", "--agent", "builder", "--agent-id", "agent-builder")

            dashboard = json.loads(run_cli(tmp, "dashboard").stdout)["dashboard"]

            self.assertIn("Next actions:", dashboard)
            self.assertIn("bind-subagent --agent researcher", dashboard)
            self.assertIn("wake-plan --mark", dashboard)
            self.assertIn("record-wait --agent builder", dashboard)

            run_cli(tmp, "record-wait", "--agent", "builder", "--status", "completed", "--summary", "Done.")
            run_cli(tmp, "claim", "--agent", "researcher")
            run_cli(tmp, "complete", "--task", "T1", "--agent", "researcher", "--summary", "Done.")
            run_cli(tmp, "claim", "--agent", "builder")
            run_cli(tmp, "complete", "--task", "T2", "--agent", "builder", "--summary", "Built.")
            run_cli(tmp, "gate", "--verification-passed", "true")
            dashboard = json.loads(run_cli(tmp, "dashboard").stdout)["dashboard"]

            self.assertIn("close-plan", dashboard)
            self.assertIn("record-close --agent builder", dashboard)

    def test_dashboard_compresses_unbound_next_actions_and_surfaces_unread_attention(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_cli(
                tmp,
                "init",
                "--title",
                "Dashboard compression test",
                "--lead",
                "lead",
                "--member",
                "alpha=Alpha",
                "--member",
                "beta=Beta",
                "--member",
                "gamma=Gamma",
                "--task",
                "T1:alpha:Alpha work",
                "--task",
                "T2:beta:Beta work",
                "--task",
                "T3:gamma:Gamma work",
            )
            run_cli(tmp, "message", "--from", "lead", "--to", "alpha", "--body", "Unread note.")

            dashboard = json.loads(run_cli(tmp, "dashboard").stdout)["dashboard"]

            self.assertIn("unread_recipients=1", dashboard)
            self.assertEqual(dashboard.count("launch-plan --backend subagent"), 1)
            self.assertIn("unbound members: alpha,beta,gamma", dashboard)

    def test_dashboard_groups_wait_and_close_next_actions_for_large_teams(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_cli_inprocess(
                tmp,
                "init",
                "--title",
                "Dashboard wait close grouping test",
                "--lead",
                "lead",
                "--member",
                "alpha=Alpha",
                "--member",
                "beta=Beta",
                "--member",
                "gamma=Gamma",
                "--task",
                "T1:alpha:Alpha work",
                "--task",
                "T2:beta:Beta work",
                "--task",
                "T3:gamma:Gamma work",
            )
            for agent in ("alpha", "beta", "gamma"):
                run_cli_inprocess(tmp, "bind-subagent", "--agent", agent, "--agent-id", f"agent-{agent}")

            waiting_dashboard = json.loads(run_cli_inprocess(tmp, "dashboard").stdout)["dashboard"]

            self.assertIn("waiting members: alpha,beta,gamma", waiting_dashboard)
            self.assertIn("record-wait-batch", waiting_dashboard)
            self.assertEqual(waiting_dashboard.count("record-wait --agent"), 0)

            for agent in ("alpha", "beta", "gamma"):
                run_cli_inprocess(tmp, "record-wait", "--agent", agent, "--status", "completed", "--summary", "Done.")
            close_dashboard = json.loads(run_cli_inprocess(tmp, "dashboard").stdout)["dashboard"]

            self.assertIn("close-ready members: alpha,beta,gamma", close_dashboard)
            self.assertIn("record-close-batch", close_dashboard)
            self.assertEqual(close_dashboard.count("record-close --agent"), 0)

    def test_dashboard_timed_out_next_action_mentions_finalize_orchestrate(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_cli(
                tmp,
                "init",
                "--title",
                "Timed out action test",
                "--lead",
                "lead",
                "--member",
                "builder=Build",
                "--task",
                "T1:builder:Build",
            )
            run_cli(tmp, "bind-subagent", "--agent", "builder", "--agent-id", "agent-builder")
            run_cli(tmp, "record-wait", "--agent", "builder", "--status", "timed_out")

            dashboard = json.loads(run_cli(tmp, "dashboard").stdout)["dashboard"]

            self.assertIn("orchestrate", dashboard)
            self.assertIn("record-wait --agent builder --status completed|timed_out|shutdown|errored", dashboard)

    def test_concurrent_message_writes_preserve_valid_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_cli(
                tmp,
                "init",
                "--title",
                "Concurrent write test",
                "--lead",
                "lead",
                "--member",
                "builder=Build",
                "--task",
                "T1:builder:Build",
            )

            def send_note(index):
                return run_cli(
                    tmp,
                    "message",
                    "--from",
                    "lead",
                    "--to",
                    "builder",
                    "--body",
                    f"note {index}",
                    check=False,
                )

            with ThreadPoolExecutor(max_workers=12) as executor:
                futures = [executor.submit(send_note, index) for index in range(24)]
                results = [future.result() for future in as_completed(futures)]

            failures = [result.stdout + result.stderr for result in results if result.returncode != 0]
            self.assertEqual(failures, [])
            state = read_state(tmp)
            self.assertEqual(len(state["messages"]), 24)
            self.assertEqual(len({message["id"] for message in state["messages"]}), 24)

    def test_finalize_attempts_escalate_after_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_cli(
                tmp,
                "init",
                "--title",
                "Finalize cap test",
                "--lead",
                "lead",
                "--member",
                "builder=Build",
                "--task",
                "T1:builder:Build",
            )
            run_cli(tmp, "bind-subagent", "--agent", "builder", "--agent-id", "agent-builder")
            run_cli(tmp, "record-wait", "--agent", "builder", "--status", "timed_out")

            first = json.loads(run_cli(tmp, "orchestrate").stdout)
            self.assertEqual(first["phase"], "finalize_overdue")
            self.assertEqual(first["actions"][0]["finalize_attempt"], 1)

            second = json.loads(run_cli(tmp, "orchestrate").stdout)
            self.assertEqual(second["phase"], "finalize_overdue")
            self.assertEqual(second["actions"][0]["finalize_attempt"], 2)

            escalated = json.loads(run_cli(tmp, "orchestrate").stdout)
            self.assertEqual(escalated["phase"], "finalize_escalated")
            self.assertEqual(escalated["actions"], [])
            self.assertIn("builder", json.dumps(escalated))
            state = read_state(tmp)
            self.assertIn("finalize attempts exhausted", state["gates"]["open_items"][0]["body"])

    def test_launch_plan_covers_thread_and_simulated_fallbacks(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_cli(
                tmp,
                "init",
                "--title",
                "Fallback launch test",
                "--lead",
                "lead",
                "--member",
                "fallback=Fallback worker",
                "--task",
                "T1:fallback:Fallback work",
            )

            thread_plan = json.loads(run_cli(tmp, "launch-plan", "--backend", "thread").stdout)
            self.assertEqual(thread_plan["actions"][0]["tool"], "codex_app.fork_thread")

            simulated_plan = json.loads(run_cli(tmp, "launch-plan", "--backend", "simulated").stdout)
            self.assertEqual(simulated_plan["actions"][0]["tool"], "manual_packet")

    def test_thread_fallback_message_and_close_plan_are_recordable(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_cli(
                tmp,
                "init",
                "--title",
                "Thread fallback test",
                "--lead",
                "lead",
                "--member",
                "fallback=Fallback worker",
                "--task",
                "T1:fallback:Fallback work",
            )
            run_cli(tmp, "bind-thread", "--agent", "fallback", "--thread-id", "thread-123", "--nickname", "Threadie")

            message = json.loads(
                run_cli(tmp, "message", "--from", "lead", "--to", "fallback", "--body", "Thread note").stdout
            )
            self.assertEqual(message["deliveries"][0]["tool"], "codex_app.send_message_to_thread")

            close = json.loads(run_cli(tmp, "close-plan").stdout)
            self.assertEqual(close["actions"][0]["tool"], "codex_app.set_thread_archived")
            self.assertIn("record-close --agent fallback", close["actions"][0]["record_command"])

    def test_wake_plan_emits_dependency_unblocked_subagent_delivery(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_cli(
                tmp,
                "init",
                "--title",
                "Wake test",
                "--lead",
                "lead",
                "--member",
                "researcher=Research",
                "--member",
                "builder=Build",
                "--task",
                "T1:researcher:Research",
                "--task",
                "T2:builder:Build",
                "--depends",
                "T2:T1",
            )
            run_cli(tmp, "bind-subagent", "--agent", "builder", "--agent-id", "agent-builder")
            run_cli(tmp, "claim", "--agent", "researcher")
            run_cli(tmp, "complete", "--task", "T1", "--agent", "researcher", "--summary", "Ready")

            wake = json.loads(run_cli(tmp, "wake-plan", "--mark").stdout)

            self.assertEqual(wake["actions"][0]["tool"], "multi_agent_v1.send_input")
            self.assertEqual(wake["actions"][0]["agent"], "builder")
            self.assertEqual(wake["actions"][0]["send_input_args"]["target"], "agent-builder")
            self.assertIn("T2", wake["actions"][0]["send_input_args"]["message"])
            state = read_state(tmp)
            self.assertIsNotNone(state["tasks"]["T2"]["ready_notified_at"])

    def test_hook_config_generates_stop_and_idle_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = json.loads(run_cli(tmp, "hook-config", "--agent", "builder").stdout)

            self.assertIn("agent_team_stop.py", config["powershell"])
            self.assertIn("agent_team_idle.py", config["powershell"])
            self.assertIn("CODEX_AGENT_TEAM_AGENT", config["powershell"])

    def test_hook_config_escapes_powershell_single_quotes(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "team's space"
            workspace.mkdir()

            config = json.loads(run_cli(workspace, "hook-config", "--agent", "builder's helper").stdout)

            self.assertIn("team''s space", config["powershell"])
            self.assertIn("builder''s helper", config["powershell"])

    def test_lead_owned_tasks_are_claimable_but_never_spawned(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_cli_inprocess(
                tmp,
                "init",
                "--title",
                "Lead task test",
                "--lead",
                "lead",
                "--member",
                "builder=Build",
                "--task",
                "T0:lead:Integrate final report",
                "--task",
                "T1:builder:Build section",
            )

            claimed = json.loads(run_cli_inprocess(tmp, "claim", "--agent", "lead", "--task", "T0").stdout)
            self.assertEqual(claimed["task"]["id"], "T0")
            completed = json.loads(
                run_cli_inprocess(tmp, "complete", "--agent", "lead", "--task", "T0", "--summary", "Integrated.").stdout
            )
            self.assertEqual(completed["task"]["status"], "done")

            launch = json.loads(run_cli_inprocess(tmp, "launch-plan", "--backend", "subagent").stdout)
            self.assertEqual([action["agent"] for action in launch["actions"]], ["builder"])

    def test_cancel_and_reassign_manage_open_task_lifecycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_cli_inprocess(
                tmp,
                "init",
                "--title",
                "Cancel reassign test",
                "--lead",
                "lead",
                "--member",
                "builder=Build",
                "--member",
                "reviewer=Review",
                "--task",
                "T1:builder:Build first",
                "--task",
                "T2:builder:Build second",
            )
            run_cli_inprocess(tmp, "claim", "--agent", "builder", "--task", "T1")

            worker_cancel = run_cli_with_prefix(
                "--workspace",
                tmp,
                "--actor",
                "builder",
                "cancel",
                "--task",
                "T1",
                "--reason",
                "No longer needed",
                check=False,
            )
            self.assertEqual(worker_cancel.returncode, 2)
            self.assertIn("permission_denied", worker_cancel.stdout)

            canceled = json.loads(
                run_cli_inprocess(tmp, "cancel", "--task", "T1", "--reason", "No longer needed").stdout
            )
            self.assertEqual(canceled["task"]["status"], "canceled")
            state = read_state(tmp)
            self.assertIsNone(state["members"][0]["current_task"])
            self.assertEqual(state["members"][0]["status"], "idle")

            reassigned = json.loads(
                run_cli_inprocess(tmp, "reassign", "--task", "T2", "--agent", "reviewer", "--reason", "Review owns it").stdout
            )
            self.assertEqual(reassigned["task"]["owner"], "reviewer")
            self.assertIn("Review owns it", json.dumps(read_state(tmp)["events"]))

    def test_replace_subagent_archives_old_runtime_and_prevents_accidental_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_cli_inprocess(
                tmp,
                "init",
                "--title",
                "Replace runtime test",
                "--lead",
                "lead",
                "--member",
                "builder=Build",
                "--task",
                "T1:builder:Build replacement",
            )
            run_cli_inprocess(tmp, "bind-subagent", "--agent", "builder", "--agent-id", "agent-old", "--nickname", "Old")

            accidental = run_cli_inprocess(
                tmp,
                "bind-subagent",
                "--agent",
                "builder",
                "--agent-id",
                "agent-overwrite",
                check=False,
            )
            self.assertEqual(accidental.returncode, 2)
            self.assertIn("runtime_already_bound", accidental.stdout)

            refused = run_cli_inprocess(
                tmp,
                "replace-subagent",
                "--agent",
                "builder",
                "--agent-id",
                "agent-new",
                "--nickname",
                "New",
                check=False,
            )
            self.assertEqual(refused.returncode, 2)
            self.assertIn("old_runtime_status_required", refused.stdout)

            replaced = json.loads(
                run_cli_inprocess(
                    tmp,
                    "replace-subagent",
                    "--agent",
                    "builder",
                    "--agent-id",
                    "agent-new",
                    "--nickname",
                    "New",
                    "--old-status",
                    "failed",
                    "--old-summary",
                    "stream disconnect",
                ).stdout
            )
            self.assertEqual(replaced["member"]["runtime"]["agent_id"], "agent-new")
            self.assertEqual(replaced["member"]["runtime_history"][0]["agent_id"], "agent-old")
            prompt = json.loads(run_cli_inprocess(tmp, "prompt", "--agent", "builder").stdout)["prompt"]
            self.assertIn("Replacement context", prompt)
            self.assertIn("stream disconnect", prompt)

    def test_orchestrate_spawn_policy_defaults_to_ready_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_cli_inprocess(
                tmp,
                "init",
                "--title",
                "Spawn policy test",
                "--lead",
                "lead",
                "--member",
                "researcher=Research",
                "--member",
                "writer=Write",
                "--member",
                "reviewer=Review",
                "--task",
                "T1:researcher:Research",
                "--task",
                "T2:writer:Write",
                "--task",
                "T3:reviewer:Review",
                "--depends",
                "T2:T1",
                "--depends",
                "T3:T2",
            )

            default = json.loads(run_cli_inprocess(tmp, "orchestrate").stdout)
            self.assertEqual([action["agent"] for action in default["actions"]], ["researcher"])
            self.assertEqual(default["spawn_policy"], "ready-only")
            self.assertEqual(default["skipped_blocked_spawns"], ["writer", "reviewer"])

            open_policy = json.loads(run_cli_inprocess(tmp, "orchestrate", "--spawn-policy", "open").stdout)
            self.assertEqual([action["agent"] for action in open_policy["actions"]], ["researcher", "writer", "reviewer"])

            launch = json.loads(run_cli_inprocess(tmp, "launch-plan", "--backend", "subagent").stdout)
            self.assertEqual([action["agent"] for action in launch["actions"]], ["researcher", "writer", "reviewer"])

    def test_record_delivery_batch_and_action_queue_make_delivery_steps_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_cli_inprocess(
                tmp,
                "init",
                "--title",
                "Delivery batch test",
                "--lead",
                "lead",
                "--member",
                "builder=Build",
                "--task",
                "T1:builder:Build",
            )
            run_cli_inprocess(tmp, "bind-subagent", "--agent", "builder", "--agent-id", "agent-builder")
            message = json.loads(
                run_cli_inprocess(tmp, "message", "--from", "lead", "--to", "builder", "--body", "Please handle this.").stdout
            )
            self.assertIn("lead_action_queue", message)
            self.assertIn("record-delivery-batch", json.dumps(message["lead_action_queue"]))

            result_file = Path(tmp) / "delivery-results.json"
            result_file.write_text(
                json.dumps(
                    {
                        "results": [
                            {"message": "m1", "agent": "builder", "status": "sent"},
                            {"message": "missing", "agent": "builder", "status": "sent"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            recorded = json.loads(
                run_cli_inprocess(tmp, "record-delivery-batch", "--result-file", "delivery-results.json").stdout
            )
            self.assertEqual(len(recorded["records"]), 1)
            self.assertEqual(len(recorded["unmatched"]), 1)
            self.assertEqual(read_state(tmp)["messages"][0]["delivery_status"]["builder"], "sent")

    def test_ack_all_ack_closed_and_cleanup_unread_reporting(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_cli_inprocess(
                tmp,
                "init",
                "--title",
                "Ack cleanup test",
                "--lead",
                "lead",
                "--member",
                "builder=Build",
                "--member",
                "reviewer=Review",
                "--task",
                "T1:builder:Build",
            )
            run_cli_inprocess(tmp, "message", "--from", "lead", "--to", "team", "--body", "Broadcast.")
            acked = json.loads(run_cli_inprocess(tmp, "ack-all", "--agent", "builder", "--reason", "Handled.").stdout)
            self.assertEqual(acked["acknowledged"], ["m1"])

            run_cli_inprocess(tmp, "bind-subagent", "--agent", "reviewer", "--agent-id", "agent-reviewer")
            run_cli_inprocess(tmp, "record-wait", "--agent", "reviewer", "--status", "completed", "--summary", "Done.")
            run_cli_inprocess(tmp, "record-close", "--agent", "reviewer", "--status", "closed", "--summary", "Closed.")
            closed_ack = json.loads(run_cli_inprocess(tmp, "ack-closed", "--reason", "Closed runtime cannot read.").stdout)
            self.assertIn("reviewer", closed_ack["agents"])

            run_cli_inprocess(tmp, "claim", "--agent", "builder", "--task", "T1")
            run_cli_inprocess(tmp, "complete", "--agent", "builder", "--task", "T1", "--summary", "Built.")
            run_cli_inprocess(tmp, "gate", "--verification-passed", "true")
            cleaned = json.loads(run_cli_inprocess(tmp, "cleanup").stdout)
            self.assertIn("unread_at_cleanup", cleaned)

            dashboard = json.loads(run_cli_inprocess(tmp, "dashboard").stdout)["dashboard"]
            self.assertIn("Team cleaned. No further action required.", dashboard)

    def test_finalize_prompt_includes_task_context_and_batch_record_hints(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_cli_inprocess(
                tmp,
                "init",
                "--title",
                "Finalize prompt context test",
                "--lead",
                "lead",
                "--member",
                "builder=Build",
                "--task",
                "T1:builder:Build final thing",
            )
            run_cli_inprocess(tmp, "bind-subagent", "--agent", "builder", "--agent-id", "agent-builder")
            run_cli_inprocess(tmp, "claim", "--agent", "builder", "--task", "T1")
            run_cli_inprocess(tmp, "record-wait", "--agent", "builder", "--status", "timed_out", "--summary", "No response.")

            plan = json.loads(run_cli_inprocess(tmp, "orchestrate").stdout)

            self.assertEqual(plan["phase"], "finalize_overdue")
            message = plan["actions"][0]["send_input_args"]["message"]
            self.assertIn("T1 - Build final thing", message)
            self.assertIn("No response.", message)
            self.assertIn("record-wait-batch", json.dumps(plan["lead_action_queue"]))

    def test_docs_explain_recovery_flows_and_command_discovery(self):
        readme = (PLUGIN_ROOT / "README.md").read_text(encoding="utf-8")
        skill = (PLUGIN_ROOT / "skills" / "agent-teams" / "SKILL.md").read_text(encoding="utf-8")
        combined = readme + "\n" + skill

        self.assertIn("Common recovery flows", combined)
        for command in (
            "record-delivery-batch",
            "record-wait-batch",
            "record-close-batch",
            "replace-subagent",
            "cancel --task",
            "reassign --task",
            "ack-all",
            "ack-closed",
            "CODEX_AGENT_TEAM_COMMAND",
        ):
            self.assertIn(command, combined)

    def test_launch_spawn_policy_can_start_ready_only_without_blocked_agents(self):
        with tempfile.TemporaryDirectory() as tmp:
            launched = json.loads(
                run_cli_inprocess(
                    tmp,
                    "launch",
                    "--title",
                    "Launch spawn policy test",
                    "--lead",
                    "lead",
                    "--member",
                    "researcher=Research",
                    "--member",
                    "writer=Write",
                    "--member",
                    "reviewer=Review",
                    "--task",
                    "T1:researcher:Research",
                    "--task",
                    "T2:writer:Write",
                    "--task",
                    "T3:reviewer:Review",
                    "--depends",
                    "T2:T1",
                    "--depends",
                    "T3:T2",
                    "--spawn-policy",
                    "ready-only",
                ).stdout
            )

            self.assertEqual([action["agent"] for action in launched["launch"]["actions"]], ["researcher"])
            self.assertEqual(launched["launch"]["spawn_policy"], "ready-only")
            self.assertEqual(launched["launch"]["skipped_blocked_spawns"], ["writer", "reviewer"])
            for action in launched["launch"]["actions"]:
                self.assertNotIn("agent_type", action.get("spawn_args", {}))

            open_plan = json.loads(run_cli_inprocess(tmp, "launch-plan", "--spawn-policy", "open").stdout)
            self.assertEqual([action["agent"] for action in open_plan["actions"]], ["researcher", "writer", "reviewer"])

    def test_complete_records_outputs_and_dashboard_lists_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "outputs" / "homepage.md"
            output.parent.mkdir()
            output.write_text("# Homepage copy\n", encoding="utf-8")
            run_cli_inprocess(
                tmp,
                "init",
                "--title",
                "Artifact registry test",
                "--lead",
                "lead",
                "--member",
                "writer=Write",
                "--task",
                "T1:writer:Draft homepage",
            )
            run_cli_inprocess(tmp, "claim", "--agent", "writer", "--task", "T1")
            completed = json.loads(
                run_cli_inprocess(
                    tmp,
                    "complete",
                    "--agent",
                    "writer",
                    "--task",
                    "T1",
                    "--summary",
                    "Drafted.",
                    "--output",
                    "outputs/homepage.md",
                ).stdout
            )

            self.assertEqual(completed["task"]["outputs"], [str(output.resolve())])
            dashboard = json.loads(run_cli_inprocess(tmp, "dashboard").stdout)["dashboard"]
            self.assertIn("Artifacts:", dashboard)
            self.assertIn("T1", dashboard)
            self.assertIn(str(output.resolve()), dashboard)

    def test_gate_records_verification_evidence_and_dashboard_shows_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_cli_inprocess(
                tmp,
                "init",
                "--title",
                "Verification evidence test",
                "--lead",
                "lead",
                "--member",
                "builder=Build",
                "--task",
                "T1:builder:Build",
            )
            gate = json.loads(
                run_cli_inprocess(
                    tmp,
                    "gate",
                    "--verification-passed",
                    "true",
                    "--verification-command",
                    "python -m unittest discover -s tests -v",
                    "--verification-exit-code",
                    "0",
                    "--verification-summary",
                    "68 tests passed.",
                ).stdout
            )

            evidence = gate["gates"]["verification_evidence"][-1]
            self.assertEqual(evidence["exit_code"], 0)
            self.assertIn("68 tests passed.", evidence["summary"])
            dashboard = json.loads(run_cli_inprocess(tmp, "dashboard").stdout)["dashboard"]
            self.assertIn("Verification evidence:", dashboard)
            self.assertIn("python -m unittest discover -s tests -v", dashboard)

    def test_cleanup_ack_closed_marks_closed_runtime_messages_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_cli_inprocess(
                tmp,
                "init",
                "--title",
                "Cleanup ack closed test",
                "--lead",
                "lead",
                "--member",
                "reviewer=Review",
                "--task",
                "T1:reviewer:Review",
            )
            run_cli_inprocess(tmp, "bind-subagent", "--agent", "reviewer", "--agent-id", "agent-reviewer")
            run_cli_inprocess(tmp, "claim", "--agent", "reviewer", "--task", "T1")
            run_cli_inprocess(tmp, "complete", "--agent", "reviewer", "--task", "T1", "--summary", "Reviewed.")
            run_cli_inprocess(tmp, "record-wait", "--agent", "reviewer", "--status", "completed", "--summary", "Done.")
            run_cli_inprocess(tmp, "record-close", "--agent", "reviewer", "--status", "closed", "--summary", "Closed.")
            run_cli_inprocess(tmp, "message", "--from", "lead", "--to", "reviewer", "--body", "Incorporated.")
            run_cli_inprocess(tmp, "gate", "--verification-passed", "true")

            cleaned = json.loads(run_cli_inprocess(tmp, "cleanup", "--ack-closed").stdout)

            self.assertEqual(cleaned["unread_at_cleanup"], 0)
            self.assertEqual(cleaned["ack_closed_at_cleanup"]["agents"], ["reviewer"])
            state = read_state(tmp)
            self.assertEqual(state["messages"][0]["read_by"], ["reviewer"])

    def test_prompts_and_docs_explain_shared_workspace_semantics(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_cli_inprocess(
                tmp,
                "init",
                "--title",
                "Workspace semantics test",
                "--lead",
                "lead",
                "--member",
                "builder=Build",
                "--task",
                "T1:builder:Build",
            )
            prompt = json.loads(run_cli_inprocess(tmp, "prompt", "--agent", "builder").stdout)["prompt"]
            readme = (PLUGIN_ROOT / "README.md").read_text(encoding="utf-8")
            skill = (PLUGIN_ROOT / "skills" / "agent-teams" / "SKILL.md").read_text(encoding="utf-8")

            self.assertIn("same filesystem workspace", prompt)
            self.assertIn("Shared workspace semantics", readme)
            self.assertIn("same filesystem workspace", skill)

    def test_dashboard_prioritizes_ready_tasks_before_runtime_close(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_cli_inprocess(
                tmp,
                "init",
                "--title",
                "Dashboard priority test",
                "--lead",
                "lead",
                "--member",
                "writer=Write",
                "--task",
                "T1:writer:Draft",
                "--task",
                "T2:lead:Integrate final docs",
                "--depends",
                "T2:T1",
            )
            run_cli_inprocess(tmp, "bind-subagent", "--agent", "writer", "--agent-id", "agent-writer")
            run_cli_inprocess(tmp, "claim", "--agent", "writer", "--task", "T1")
            run_cli_inprocess(tmp, "complete", "--agent", "writer", "--task", "T1", "--summary", "Drafted.")
            run_cli_inprocess(tmp, "record-wait", "--agent", "writer", "--status", "completed", "--summary", "Done.")

            dashboard = json.loads(run_cli_inprocess(tmp, "dashboard").stdout)["dashboard"]

            claim_index = dashboard.index("claim --agent lead --task T2")
            close_index = dashboard.index("close-plan")
            self.assertLess(claim_index, close_index)

    def test_spawn_actions_warn_against_adding_agent_type_with_fork_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_cli_inprocess(
                tmp,
                "init",
                "--title",
                "Spawn warning test",
                "--lead",
                "lead",
                "--member",
                "writer=Write",
                "--task",
                "T1:writer:Draft",
            )

            plan = json.loads(run_cli_inprocess(tmp, "launch-plan").stdout)
            action = plan["actions"][0]

            self.assertTrue(action["spawn_args"]["fork_context"])
            self.assertIn("Do not add agent_type", action["spawn_warning"])
            self.assertNotIn("agent_type", action["spawn_args"])

    def test_docs_recommend_ready_only_launch_and_raw_wait_batch_recording(self):
        readme = (PLUGIN_ROOT / "README.md").read_text(encoding="utf-8")
        skill = (PLUGIN_ROOT / "skills" / "agent-teams" / "SKILL.md").read_text(encoding="utf-8")
        combined = readme + "\n" + skill

        self.assertIn("launch --spawn-policy ready-only", combined)
        self.assertIn("raw wait_agent output", combined)
        self.assertIn("record-wait-batch --result-file", combined)


if __name__ == "__main__":
    unittest.main()
