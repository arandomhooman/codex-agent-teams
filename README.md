# Codex Agent Teams

Codex Agent Teams is a Codex plugin for agents who need Claude Code-style team coordination: a lead, named teammates, shared task state, dependency-aware work claiming, teammate messages, dashboards, gates, cleanup, and hook-compatible stop/idle helpers.

This README is written for agents first. If you are a human, the easiest path is to tell your Codex agent to install and use the plugin for you.

## Tell Your Agent To Install It

Give your Codex agent this prompt:ory:

```text
Please install Codex Agent Teams for me from <rehttps://github.com/arandomhooman/codex-agent-teams/edit/main/README.md.
```

If your agent is doing the install manually, the stable command should look like one of these:

```powershell
$AgentTeam = "path\to\codex-agent-teams\scripts\agent_team.py"
$env:CODEX_AGENT_TEAM_COMMAND = "python $AgentTeam"
```

```bash
export CODEX_AGENT_TEAM_COMMAND='python /path/to/codex-agent-teams/scripts/agent_team.py'
```

`CODEX_AGENT_TEAM_COMMAND` is recommended because generated teammate prompts and record commands can use that stable source command instead of a changing installed-cache path.

## Agent Decision Rule

Use ordinary Codex subagents for one-shot packets: a bounded review, research task, test run, or implementation slice that can report once and disappear.

Use Codex Agent Teams when coordination itself is part of the job:

- the user asks for an agent team, teammates, a swarm, Claude teams, shared task tracking, or a lead-managed workflow
- multiple roles need dependencies, handoffs, or messages
- work should survive across turns in `.codex-agent-teams/state.json`
- the lead must enforce plan approval, verification, stop gates, or cleanup
- a reviewer needs to compare files produced by multiple workers

Inside an Agent Team, prefer real Codex subagents first. Use thread delegation only as a fallback when subagents are unavailable, and simulated packet passes only when no delegation backend is available.

## Agent Operating Model

| Team concept | What the agent should use |
| --- | --- |
| Lead | The current Codex session. |
| Teammates | Named `--member name=role` entries. |
| Team state | `.codex-agent-teams/state.json` in the target workspace. |
| Task ownership | `claim`, `claim --task`, `complete`, `cancel`, and `reassign`. |
| Dependencies | `--depends task:dependency`; blocked work should not be spawned with `ready-only`. |
| Messages | `message`, `inbox`, `ack`, `ack-all`, `ack-closed`, and delivery recording. |
| Runtime handles | `bind-subagent` for first binding, `replace-subagent` for recovery. |
| Host actions | `launch`, `orchestrate`, `wake-plan`, and `close-plan` emit action plans. |
| Recording | `record-delivery-batch`, `record-wait-batch`, and `record-close-batch`. |
| Human visibility | `dashboard` and optional Markdown dashboard output. |
| Completion gates | `gate`, `stop-check`, `close-plan`, and `cleanup`. |

Important: plugin scripts do not directly execute `multi_agent_v1`. They emit host-tool action plans. The lead Codex session executes those host-tool calls, then records results back into team state.

## Quick Start For Agents

Run commands from the workspace the team should manage.

```powershell
python $AgentTeam --workspace . launch --spawn-policy ready-only `
  --title "Feature build" `
  --lead lead `
  --member "researcher=Map requirements and constraints" `
  --member "builder=Implement the change" `
  --member "reviewer=Verify and challenge the result" `
  --task "T1:researcher:Research requirements" `
  --task "T2:builder:Implement changes" `
  --task "T3:reviewer:Review and verify" `
  --depends "T2:T1" `
  --depends "T3:T2" `
  --brief "Keep changes scoped and preserve the existing project style." `
  --verification-check "Report exact files changed and commands run."
```

`launch --spawn-policy ready-only` starts only dependency-ready teammates. Plain `launch` remains a full-team start for compatibility, so agents should prefer `ready-only` when dependencies exist.

When `launch` emits spawn actions, execute the emitted spawn args exactly. Do not add unsupported `agent_type`, `model`, or reasoning overrides when `fork_context=true` unless the host tool schema explicitly supports them.

After each spawn returns, bind the runtime handle:

```powershell
python $AgentTeam --workspace . bind-subagent --agent researcher --agent-id <agent_id> --nickname <nickname>
```

Then run the lead loop:

```powershell
python $AgentTeam --workspace . dashboard
python $AgentTeam --workspace . orchestrate --spawn-policy ready-only
python $AgentTeam --workspace . record-wait-batch --result-file .\wait-agent-result.json
python $AgentTeam --workspace . record-close-batch --result-file .\close-agent-results.json
python $AgentTeam --workspace . gate --verification-passed true --verification-command "<command>" --verification-exit-code 0 --verification-summary "<summary>"
python $AgentTeam --workspace . stop-check
python $AgentTeam --workspace . cleanup --ack-closed
```

## Shared workspace semantics

Subagents and the lead operate in the same filesystem workspace. Forked context is conversation context, not a separate checkout or isolated file tree. Assign clear write scopes, keep generated artifacts inside the team workspace, and use `--allow-outside-workspace` only when crossing that boundary is intentional.

## Lead Workflow Checklist

1. Initialize with `launch --spawn-policy ready-only` when dependencies exist.
2. Execute emitted host-tool actions exactly.
3. Bind new subagent handles with `bind-subagent`.
4. Use `dashboard` to decide the next correct action.
5. Use `claim --task <id>` when the exact task matters.
6. Use `message --to team` for broadcast handoffs.
7. Use `orchestrate` to emit the next spawn, delivery, wake, wait, finalize, close, or cleanup phase.
8. Save raw host-tool results to JSON and batch-record them.
9. Record verification evidence with `gate`.
10. Run `stop-check` before final response.
11. Close runtimes, record closes, and run `cleanup`.

Use `init --force` or `launch --force` only when intentionally archiving and replacing an existing team state.

## Common recovery flows

| Dashboard says | Agent action |
| --- | --- |
| Unbound members | Run `orchestrate --spawn-policy ready-only`, execute spawn actions, then `bind-subagent`. |
| Blocked future workers skipped | Expected with `ready-only`; dependencies will wake them later. |
| Runtime already bound | Use `replace-subagent`; `bind-subagent` is for first binding only. |
| Pending deliveries | Execute `send_input`, save results, then run `record-delivery-batch --result-file <json>`. |
| Waiting members | Keep `wait_agent` looping until every target is final, save raw output, then run `record-wait-batch --result-file <json>`. |
| Close-ready members | Execute close/archive actions, save results, then run `record-close-batch --result-file <json>`. |
| Timed-out agent | Run `orchestrate` for finalize nudges; if recovery fails, use `replace-subagent` or `record-wait --status errored`. |
| Blocked writer or reviewer | Use `reassign` or `cancel` with a reason. |
| Unread messages after cleanup | Cleanup may proceed; optionally run `ack-all` or `ack-closed`. |
| Stale lock or corrupt state | Run `repair`, then `repair --unlock-stale --clean-temps`, or `repair --restore-backup`. |

Batch result examples:

```json
{"results":[{"message":"m1","agent":"builder","status":"sent"}]}
```

```json
{"results":[{"target":"agent-builder","status":"completed","summary":"Builder finished."}]}
```

```json
{"results":[{"target":"agent-builder","status":"closed","summary":"Closed."}]}
```

## Core Command Map

```powershell
python $AgentTeam --workspace . dashboard
python $AgentTeam --workspace . orchestrate --spawn-policy ready-only
python $AgentTeam --workspace . claim --agent researcher
python $AgentTeam --workspace . claim --agent researcher --task T1
python $AgentTeam --workspace . complete --task T1 --agent researcher --summary "Requirements mapped." --output .\outputs\requirements.md
python $AgentTeam --workspace . message --from lead --to team --body "T1 is ready."
python $AgentTeam --workspace . inbox --agent reviewer
python $AgentTeam --workspace . ack-all --agent reviewer --reason "Read and handled."
python $AgentTeam --workspace . cancel --task T4 --reason "No longer needed."
python $AgentTeam --workspace . reassign --task T2 --agent reviewer --reason "Reviewer owns the follow-up."
python $AgentTeam --workspace . replace-subagent --agent builder --agent-id <new_agent_id> --nickname <nickname> --old-status failed --old-summary "Stream disconnected."
python $AgentTeam --workspace . repair
```

## Limitations Agents Must Preserve

- This is not a native Codex team UI. It is a plugin-backed coordination layer.
- The plugin does not directly call `multi_agent_v1`; the lead executes emitted host-tool plans.
- The lifecycle loop is still explicit: spawn, bind, wait, record wait, close, record close. Batch recording reduces friction but does not remove host-tool ownership.
- Runtime state is only as accurate as the host-tool results recorded back into state.
- CLI actor guardrails prevent common mistakes, but they are not a security boundary.
- Hook discovery is host-specific, so hook wrappers are commands rather than manifest hook fields.

## Hook Commands

Stop gate:

```powershell
python path\to\codex-agent-teams\hooks\agent_team_stop.py
```

Idle nudge:

```powershell
$env:CODEX_AGENT_TEAM_AGENT = "builder"
python path\to\codex-agent-teams\hooks\agent_team_idle.py
```

Generate host hook snippets:

```powershell
python $AgentTeam --workspace . hook-config --agent builder
```

## Development Checks

Run tests:

```powershell
python -m unittest discover -s tests -v
```

Validate the plugin:

```powershell
python path\to\plugin-creator\scripts\validate_plugin.py path\to\codex-agent-teams
```

During local plugin iteration, update the cachebuster and reinstall:

```powershell
python path\to\plugin-creator\scripts\update_plugin_cachebuster.py path\to\codex-agent-teams
codex plugin add codex-agent-teams@personal
```

## Research Sources

- Claude agent teams: https://code.claude.com/docs/en/agent-teams
- Claude subagents: https://docs.anthropic.com/en/docs/claude-code/sub-agents
- Claude hooks: https://docs.anthropic.com/en/docs/claude-code/hooks
- Context7 Claude Code docs ID used during development: `/anthropics/claude-code`
