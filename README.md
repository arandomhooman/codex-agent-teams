# Codex Agent Teams

Codex Agent Teams is a local Codex plugin that recreates the useful parts of Claude Code-style agent teams: a lead coordinator, named teammates, shared task state, dependency-aware assignment, direct messages, verification gates, cleanup gates, dashboard output, and hook-compatible stop/idle helpers.

The plugin is intentionally subagent-first. It does not create forked chat teams as the main path; instead, it tracks the team in local state and emits host-tool action plans for real Codex subagents.

## What It Provides

| Team feature | Codex Agent Teams implementation |
| --- | --- |
| Lead coordinator | The current Codex session acts as lead. |
| Named teammates | `init` / `launch` with `--member name=role`. |
| Shared task state | `.codex-agent-teams/state.json` in the target workspace. |
| Dependencies | `--depends task:dependency` and readiness-aware `claim`. |
| Direct messages | `message`, `inbox`, `ack`, `ack-all`, and host delivery recording. |
| Subagent backend | `launch`, `orchestrate`, `wake-plan`, and `close-plan` emit `multi_agent_v1` host-tool plans. |
| Runtime bookkeeping | `bind-subagent`, `replace-subagent`, `record-wait(-batch)`, `record-close(-batch)`, and `record-delivery(-batch)`. |
| Human dashboard | `dashboard` with ready, blocked, active, timed-out, pending-input, artifact, and verification sections. |
| Gates | `gate`, `stop-check`, and cleanup refusal while work/runtimes remain open. |
| Hooks | `hooks/agent_team_stop.py`, `hooks/agent_team_idle.py`, and `hook-config`. |

## Installation

Clone the plugin somewhere stable, then install it through a local Codex plugin marketplace entry that points at this repository.

For command examples, replace `path/to/codex-agent-teams` with your local clone:

```powershell
$AgentTeam = "path\to\codex-agent-teams\scripts\agent_team.py"
$env:CODEX_AGENT_TEAM_COMMAND = "python $AgentTeam"
```

```bash
export CODEX_AGENT_TEAM_COMMAND='python /path/to/codex-agent-teams/scripts/agent_team.py'
```

`CODEX_AGENT_TEAM_COMMAND` is optional, but recommended. Generated prompts and record commands use it instead of an installed cache path, which avoids path drift after plugin reinstalls.

## Quick Start

Run team commands from the workspace the team should manage.

```powershell
python $AgentTeam --workspace . launch `
  --spawn-policy ready-only `
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

`launch` creates `.codex-agent-teams/state.json` and emits spawn actions for the lead to execute. Use the emitted spawn args exactly. Do not add unsupported `agent_type`, `model`, or reasoning overrides when `fork_context=true` unless the host tool schema supports them.

After each spawn returns, bind the runtime handle:

```powershell
python $AgentTeam --workspace . bind-subagent --agent researcher --agent-id <agent_id> --nickname <nickname>
```

Then use the normal loop:

```powershell
python $AgentTeam --workspace . dashboard
python $AgentTeam --workspace . orchestrate
python $AgentTeam --workspace . record-wait-batch --result-file .\wait-agent-result.json
python $AgentTeam --workspace . record-close-batch --result-file .\close-agent-results.json
python $AgentTeam --workspace . gate --verification-passed true --verification-command "python -m unittest discover -s tests -v" --verification-exit-code 0 --verification-summary "All tests passed."
python $AgentTeam --workspace . stop-check
python $AgentTeam --workspace . cleanup --ack-closed
```

## Shared workspace semantics

Subagents and the lead operate in the same filesystem workspace. Forked context is conversation context, not a separate checkout or isolated file tree. Assign clear write scopes, keep generated artifacts inside the team workspace, and use `--allow-outside-workspace` only when crossing that boundary is intentional.

## When To Use This Instead Of Plain Subagents

Use plain Codex subagents for disposable one-shot packets: a bounded review, research task, test run, or implementation slice that can report back once and disappear.

Use Codex Agent Teams when coordination itself matters:

- multiple named roles need shared state or handoffs
- tasks have dependencies
- teammates need direct messages
- work should survive across turns
- the lead needs approval, verification, stop, or cleanup gates
- output quality depends on a reviewer comparing multiple workers' files

Inside Agent Teams, prefer real Codex subagents first. Use thread delegation only as a fallback when subagents are unavailable, and simulated packet passes only when no delegation backend exists.

## Core Commands

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

Use `launch --force` or `init --force` only when you intentionally want to archive and replace an existing team state.

## Common Recovery Flows

| Dashboard says | What to do |
| --- | --- |
| Unbound members | Run `orchestrate --spawn-policy ready-only`, execute spawn actions, then `bind-subagent`. |
| Blocked future workers skipped | This is expected with `ready-only`; dependencies will wake them later. |
| Runtime already bound | Use `replace-subagent`; `bind-subagent` is for first binding only. |
| Pending deliveries | Execute `send_input`, save results, then run `record-delivery-batch --result-file <json>`. |
| Waiting members | Keep `wait_agent` looping until every target is final, save raw output, then run `record-wait-batch --result-file <json>`. |
| Close-ready members | Execute close/archive actions, save results, then run `record-close-batch --result-file <json>`. |
| Timed-out agent | Run `orchestrate` for finalize nudges; if recovery fails, use `replace-subagent` or `record-wait --status errored`. |
| Blocked writer/reviewer | Use `reassign` or `cancel` with a reason. |
| Unread messages after cleanup | Cleanup may proceed; optionally run `ack-all` or `ack-closed`. |
| Stale lock or corrupt state | Run `repair`, then `repair --unlock-stale --clean-temps`, or `repair --restore-backup`. |

Batch result files use simple JSON:

```json
{"results":[{"message":"m1","agent":"builder","status":"sent"}]}
```

```json
{"results":[{"target":"agent-builder","status":"completed","summary":"Builder finished."}]}
```

```json
{"results":[{"target":"agent-builder","status":"closed","summary":"Closed."}]}
```

## Limitations

- This is not a native Codex team UI. It is a plugin-backed coordination layer.
- Plugin scripts do not directly execute `multi_agent_v1`. They emit host-tool action plans, and the lead Codex session executes those actions.
- The lifecycle loop still has explicit bookkeeping: spawn, bind, wait, record wait, close, record close. Batch recording reduces the friction but does not remove host-tool ownership.
- Runtime state is only as accurate as the host-tool results that get recorded back into team state.
- CLI actor guardrails prevent common mistakes, but they are not a security boundary.
- Subagents and the lead share the same filesystem workspace. Forked context is conversation context, not a separate checkout.
- Hook discovery is host-specific, so this repo ships hook wrapper commands instead of declaring unsupported manifest hook fields.

## Hooks

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

## Development

Run tests:

```powershell
python -m unittest discover -s tests -v
```

Validate the plugin with Codex's plugin validator:

```powershell
python path\to\plugin-creator\scripts\validate_plugin.py path\to\codex-agent-teams
```

During local plugin iteration, update the cachebuster and reinstall from your local marketplace:

```powershell
python path\to\plugin-creator\scripts\update_plugin_cachebuster.py path\to\codex-agent-teams
codex plugin add codex-agent-teams@personal
```

## Research Sources

- Claude agent teams: https://code.claude.com/docs/en/agent-teams
- Claude subagents: https://docs.anthropic.com/en/docs/claude-code/sub-agents
- Claude hooks: https://docs.anthropic.com/en/docs/claude-code/hooks
- Context7 Claude Code docs ID used during development: `/anthropics/claude-code`
