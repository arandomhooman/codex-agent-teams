---
name: agent-teams
description: Use when a user asks for Claude-style agent teams, teammate coordination, swarms, multi-agent execution, role-based subagents, shared task lists, stop-gated work, or parallel Codex work packets.
---

# Codex Agent Teams

Use this skill to run a Claude Code agent-teams style workflow inside Codex.

Claude Code agent teams provide a lead, named teammates, a shared task list, direct teammate messaging, plan-approval gates, stop/idle hooks, and cleanup. Codex Agent Teams now recreates the workflow by wrapping Codex host subagents first, with thread and simulated packet fallbacks:

- a shared state file at `.codex-agent-teams/state.json`
- a tested state engine at `scripts/agent_team.py`
- hook-compatible wrappers in `hooks/`
- host-tool launch plans for `multi_agent_v1.spawn_agent`, `send_input`, `wait_agent`, and `close_agent`
- a recoverable local state layer with stale-lock recovery, jittered retries, `state.json.bak`, and a `repair` command
- fallback Codex thread delegation only when subagents are unavailable
- simulated packet execution only when no delegation backend is available

## Operating Contract

1. Treat the current Codex session as the team lead.
2. Initialize team state before splitting work.
3. Use named teammates with clear roles, not vague "agent 1" labels.
4. Keep every task in `.codex-agent-teams/state.json`.
5. Claim tasks before working on them.
6. Send messages for blockers, decisions, and handoffs.
7. Never edit `state.json` directly; use the team command so locking, validation, and events stay intact.
8. Reserve `orchestrate`, `record-wait`, `record-delivery`, `record-close`, `gate`, `close-plan`, and `cleanup` for the lead unless the lead explicitly delegates one of those commands.
9. Run verification before setting `verification_passed`.
10. Use `stop-check` before finalizing. If it returns exit code 2, continue or resolve the listed gates.
11. Clean up when the team is done.

## Teams vs Subagents

Codex should use this plugin when the work needs a persistent team layer. It should use ordinary subagents, thread delegation, or simulated work packets when the work is bounded and disposable.

Use Codex Agent Teams when:

- the user explicitly asks for agent teams, Claude-style teams, teammates, a swarm with a lead, or persistent multi-agent coordination
- the task has 2 or more roles that need a shared task list, dependencies, handoffs, or direct messages
- the work should survive across turns through `.codex-agent-teams/state.json`
- the lead must enforce plan approval, verification gates, stop gates, or cleanup
- multiple worker threads/subagents may be spawned but need one coordinator to track ownership and integration

Use ordinary subagents or one-off thread delegation when:

- the task is a single isolated research, review, test, or implementation packet
- the worker can return one result and then disappear
- there is no need for shared state, messages, dependency tracking, or cleanup
- speed matters more than persistent coordination
- the user says to use subagents but does not ask for a managed team

Use both together when:

- Agent Teams supplies the lead layer and shared state
- each teammate is executed by a Codex thread/subagent or by an isolated packet pass
- every teammate must claim a task before work and complete/message back into the team state

Default rule: start with subagents for disposable one-shot packets; start Codex Agent Teams when coordination itself is part of the job.

Do not start Codex Agent Teams for a tiny one-file fix, a direct answer, or a task with no handoffs. Do not use bare subagents for a multi-step team effort that needs gates, status, or integration history.

Trigger phrases that should select this plugin: `agent team`, `Claude teams`, `teammates`, `swarm with a lead`, `shared task list`, `team status`, `stop gates`, `coordinate agents`, `parallel team`, `role-based agents`.

## Quick Start

From the workspace being worked on, prefer `launch` so team state is created and the lead receives the subagent spawn plan in one step:

```powershell
$AgentTeam = "path\to\codex-agent-teams\scripts\agent_team.py"
$env:CODEX_AGENT_TEAM_COMMAND = "python $AgentTeam"
python $AgentTeam --workspace . launch `
  --title "Feature build" `
  --lead lead `
  --member "researcher=Find current docs and constraints" `
  --member "builder=Implement the feature" `
  --member "reviewer=Verify and review" `
  --task "T1:researcher:Map requirements and source evidence" `
  --task "T2:builder:Implement the requested change" `
  --task "T3:reviewer:Run verification and summarize risks" `
  --depends "T2:T1" `
  --depends "T3:T2" `
  --brief "Preserve existing repo style and keep changes tightly scoped." `
  --context-file .\team-context.md `
  --context-mode reference `
  --member-context "reviewer=Check file links, citations, and verification evidence." `
  --require-citations `
  --verification-check "Report exact files changed and commands run."
```

Claim and complete work:

```powershell
python $AgentTeam --workspace . claim --agent researcher
python $AgentTeam --workspace . complete --task T1 --agent researcher --summary "Requirements mapped." --output .\outputs\requirements.md
```

Send messages:

```powershell
python $AgentTeam --workspace . message --from lead --to reviewer --body "Please verify plugin validation too."
```

Gate final completion:

```powershell
python $AgentTeam --workspace . stop-check
python $AgentTeam --workspace . gate --verification-passed true
python $AgentTeam --workspace . cleanup
```

## Subagent Backend

Prefer real Codex subagents over forked chats.

The Python state engine cannot call host tools by itself. It emits structured host-tool plans, and the lead Codex session must execute those plans with `multi_agent_v1` tools.

One-command launch:

```powershell
python $AgentTeam --workspace . launch `
  --title "Feature build" `
  --lead lead `
  --member "researcher=Find current docs and constraints" `
  --member "builder=Implement the feature" `
  --task "T1:researcher:Map requirements and source evidence" `
  --task "T2:builder:Implement the requested change" `
  --depends "T2:T1"
```

Use the returned `multi_agent_v1.spawn_agent` actions. After each spawn returns, record the handle:

```powershell
python $AgentTeam --workspace . bind-subagent --agent researcher --agent-id <agent_id> --nickname <nickname>
```

Messages to bound subagents return `multi_agent_v1.send_input` delivery actions. Execute those host-tool actions, then keep state as the source of truth.

Generated teammate prompts include the absolute workspace path, exact state file path, and exact team command. This prevents subagents from accidentally reading an older `.codex-agent-teams/state.json` in a different current directory.
Shared workspace semantics: subagents and the lead operate in the same filesystem workspace. Forked context is conversation context, not a separate file tree. Assign clear write scopes and keep outputs inside the team workspace unless the lead intentionally allows an outside path.
Use `--context-mode reference` with large `--context-file` inputs when launch output would get noisy; teammates receive the file path and must read the shared workspace file themselves.
Prefer a stable source command path or wrapper in handoffs and docs. Avoid depending on installed cache paths under `.codex\plugins\cache\...`; cachebuster reinstalls intentionally change those paths. If you maintain a wrapper command, set `CODEX_AGENT_TEAM_COMMAND` and generated prompts/record commands will use that stable command instead.
Use `orchestrate` as the best available lead-runtime loop. It emits the next host-tool phase with stable action IDs: `spawn_unbound`, `deliver_messages`, `wake_ready`, `wait_agents`, `finalize_overdue`, `close_ready`, `cleanup_ready`, or `work_incomplete`. Codex still has to execute the returned host-tool actions because plugin scripts cannot call `multi_agent_v1` directly. Record message sends with `record-delivery`, record `wait_agent` results with `record-wait`, and record close actions with `record-close` so cleanup is gated on runtime lifecycle, not just task completion. Treat `wait_agents` as a wait-all phase: keep waiting until every listed teammate reaches a final runtime status, then save the raw wait_agent output to JSON and run `record-wait-batch --result-file <json>`.

Useful lead commands:

- `dashboard` gives a human-readable team dashboard with each teammate's runtime status, pending input count, and warnings for active, timed-out, or pending-input runtimes.
- Dashboard output includes attention buckets for ready tasks, blocked tasks, active tasks, pending-input runtimes, timed-out runtimes, and recent events.
- Dashboard output also includes exact next actions for unbound teammates, dependency-ready wakeups, pending `record-wait`, pending `record-close`, verification, cleanup, artifact paths, and verification evidence.
- `dashboard --format markdown --output TEAM_DASHBOARD.md` writes a human-visible Markdown dashboard file.
- `inbox --agent <name>` shows unread messages.
- `ack --agent <name> --message <id>` marks a message read, reconciles tracked delivery status to `read`, clears one pending input, and prevents `orchestrate` from re-emitting that delivery.
- `ack-all --agent <name> --reason <text>` marks all unread messages read for one teammate; `ack-closed --reason <text>` resolves unread messages addressed to closed or shutdown runtimes.
- `claim` refuses a second active task for the same teammate, `claim --task <task_id>` claims a specific ready task, and `complete` requires the task to be dependency-clean, claimed, and in progress. Lead-owned tasks may use `claim --agent lead` and `complete --agent lead`; lead is still never spawned, waited, closed, or replaced.
- `complete --output <path>` records task artifacts in state so dashboard and final reporting can show produced files directly.
- `cancel --task <id> --reason <text>` cancels open work; `reassign --task <id> --agent <member> --reason <text>` moves open work and clears the old owner's `current_task`.
- Reviewer members should act as challengers: flag unsupported claims, separate evidence from inference, and check final report quality before the lead records the verification gate.
- Dashboard unread totals count unread recipient deliveries, not just messages, so broadcasts stay visible until every recipient acknowledges them.
- `init --force` and `launch --force` archive existing state before replacing it; without `--force`, they refuse to overwrite an active `.codex-agent-teams/state.json`.
- `--brief`, `--brief-file`, `--context-file`, `--context-mode embed|reference`, `--member-context`, `--member-context-file`, `--verification-check`, and `--require-citations` make launch prompts more task-specific and add citation/reviewer quality gates.
- `message --from <sender> --to <agent|all|team> --body-file <path>` safely routes long multiline peer notes; `team` is a broadcast alias for `all`. Messages are recorded in team state immediately; host delivery remains pending until the lead executes the emitted `send_input` action and records it with `record-delivery` or `record-delivery-batch`. `--body-file`, `record-wait --summary-file`, `record-close --summary-file`, and `dashboard --output` stay inside the team workspace unless `--allow-outside-workspace` is explicit.
- `--actor <name>` or `CODEX_AGENT_TEAM_ACTOR=<name>` enables CLI guardrails that block worker use of lead-only commands and sender spoofing. Treat this as accidental misuse protection, not hard security.
- `orchestrate --spawn-policy ready-only|open|all` emits the next lead-executable host-tool phase. It defaults to `ready-only`, so blocked future workers are not spawned until dependencies unblock. `launch --spawn-policy ready-only|open|all` and `launch-plan --spawn-policy ready-only|open|all` are also available; plain `launch` and `launch-plan` remain full-team starts for compatibility.
- `gate --verification-command <cmd> --verification-exit-code <n> --verification-summary <text>` stores verification evidence alongside pass/fail state. Use `--verification-summary-file <path>` for long output.
- Timed-out subagents receive at most two finalization nudges; after that `orchestrate` returns `finalize_escalated` and opens a lead-visible item.
- `record-delivery --message <id> --agent <name> --status sent|failed` records whether the lead executed a message delivery action emitted by `message` or `orchestrate`.
- `record-delivery-batch --result-file <json>` maps saved send-input results into message delivery updates.
- `record-wait --agent <name> --status completed|running|timed_out|shutdown|errored --summary <text>` records subagent lifecycle results. Use `--summary-file <path>` for long multiline wait results.
- `record-wait-batch --result-file <json>` maps a saved host wait result to the matching bound subagents and records multiple wait statuses in one state transaction.
- `record-close --agent <name> --status closed|not_found|archived|failed --summary <text>` records whether the lead executed a close/archive action. Use `--summary-file <path>` for long multiline close results. `cleanup` refuses stop-check blockers and unclosed bound runtimes unless forced.
- `record-close-batch --result-file <json>` maps saved host close results to matching bound subagents or threads and records multiple close statuses in one state transaction.
- `replace-subagent --agent <member> --agent-id <id> --nickname <name> --old-status closed|not_found|archived|failed --old-summary <text>` archives the previous runtime into `runtime_history`, binds a replacement, and gives the replacement prompt failure context.
- `repair`, `repair --unlock-stale --clean-temps`, and `repair --restore-backup` inspect local state health, archive stale locks, remove orphan temp files, and restore a corrupt `state.json` from `state.json.bak`.
- `wake-plan --mark` emits `send_input` actions when dependencies unblock tasks.
- `close-plan` emits `multi_agent_v1.close_agent` actions for bound subagents that have not already been recorded closed.
- `hook-config --agent <name>` emits stop/idle hook snippets.

Fallback order:

1. `multi_agent_v1` subagents.
2. Codex thread tools, only if subagents are unavailable.
3. Simulated packet passes in the current session.

## Common recovery flows

The real limit is runtime ownership: plugin scripts emit host-tool plans and do not execute `multi_agent_v1` directly. The lead executes those host-tool actions, then records the result with the matching command.

| If the dashboard says X | Run Y |
| --- | --- |
| unbound members | `orchestrate --spawn-policy ready-only`, execute spawn actions, then `bind-subagent`; use `launch-plan` for deliberate full-team launch. |
| blocked reviewer appeared in first launch | Use `launch --spawn-policy ready-only` or `launch-plan --spawn-policy ready-only`; plain `launch` stays full-team for compatibility. |
| runtime already bound | Do not rerun `bind-subagent`; use `replace-subagent --agent <name> --agent-id <new_id> --old-status failed --old-summary "<why>"`. |
| pending deliveries | Execute `send_input`, save results, then `record-delivery-batch --result-file <json>`. |
| waiting members | Keep `wait_agent` looping until all targets are final, save the raw wait_agent output to JSON, then `record-wait-batch --result-file <json>`. |
| close-ready members | Execute close/archive actions, save results, then `record-close-batch --result-file <json>`. |
| timed out agent | Run `orchestrate`; it sends a finalize prompt with task id/title and last wait result. If recovery fails, `replace-subagent` or `record-wait --status errored`. |
| blocked writer/reviewer | Run `reassign --task <id> --agent <member> --reason <text>` or `cancel --task <id> --reason <text>`. |
| unread messages after cleanup | Cleanup may proceed; optionally run `ack-all --agent <name> --reason <text>` or `ack-closed --reason <text>`. |
| unread messages are only for closed runtimes | Run `cleanup --ack-closed` at cleanup time. |
| stale lock/corrupt state | Run `repair`, `repair --unlock-stale --clean-temps`, or `repair --restore-backup`. |

Batch JSON examples:

```json
{"results":[{"message":"m1","agent":"builder","status":"sent"}]}
```

```json
{"results":[{"target":"agent-builder","status":"completed","summary":"Builder finished."}]}
```

```json
{"results":[{"target":"agent-builder","status":"closed","summary":"Closed."}]}
```

## Hook Wrappers

The hook scripts are intentionally not referenced from `plugin.json` because generated personal plugin validation currently rejects unsupported manifest hook fields.

- `hooks/agent_team_stop.py` runs `stop-check`. It exits `2` when tasks or gates remain open.
- `hooks/agent_team_stop.py` also mirrors blocking JSON to stderr for hosts that surface hook reasons there.
- `hooks/agent_team_idle.py` reads `CODEX_AGENT_TEAM_AGENT`, claims the next ready task for that agent, and exits `2` with a continue prompt when work is available.
- Both hook wrappers honor `CODEX_AGENT_TEAM_WORKSPACE`; prefer `hook-config --agent <name>` to generate snippets with that environment set.

Use these as stop/idle hook commands wherever the host exposes hook configuration.

## Claude Feature Map

| Claude agent-team capability | Codex Agent Teams replica |
| --- | --- |
| Lead agent | Current Codex session acts as lead. |
| Named teammates | `--member name=role` entries in state. |
| Shared task list | `tasks` object in `.codex-agent-teams/state.json`. |
| Dependency-aware tasking | `--depends task:dependency` and `claim` readiness checks. |
| Direct teammate messaging | `message --from --to --body` records messages in state and `orchestrate` can re-emit pending host deliveries. |
| Teammate prompts | `prompt --agent <name>` generates state-aware instructions. |
| Plan approval | `--plan-required` plus `gate --plan-approved true`. |
| Stop gates | `stop-check` and `hooks/agent_team_stop.py`. |
| Idle nudges | `hooks/agent_team_idle.py` with `CODEX_AGENT_TEAM_AGENT`. |
| Cleanup | `record-close` plus `cleanup`, refusing active work or unclosed runtimes unless `--force`. |

## Known Limits

- This plugin cannot create a native Claude-style team UI inside Codex.
- Plugin scripts cannot call hidden Codex model runners by themselves.
- Actual parallelism depends on available Codex subagent host tools; thread tools and manual/simulated packets are fallbacks.
- Hook discovery is host-specific, so the hook wrappers are provided as commands rather than unsupported manifest fields.

## Verification Rule

Before telling the user the team work is complete:

1. Run the relevant tests/build/lint for the work.
2. Run `scripts/agent_team.py --workspace . gate --verification-passed true` only after verification succeeds.
3. Run `scripts/agent_team.py --workspace . stop-check`.
4. If `stop-check` returns exit code `2`, resolve the listed blockers before final response.
