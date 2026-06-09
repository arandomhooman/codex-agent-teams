# Codex Agent Teams

Codex Agent Teams is a Codex plugin for agents that need Claude-style team coordination: named teammates, shared task state, dependency-aware work, teammate messages, dashboards, gates, cleanup, and subagent-first execution plans.

Most operational detail lives in the bundled `agent-teams` skill. This README only covers what a human or installing agent needs before using it.

## Ask Your Agent To Install It

Give your Codex agent this prompt:

```text
Please install Codex Agent Teams for me from https://github.com/arandomhooman/codex-agent-teams.
```

Install notes for the agent:

- Clone the repo to a stable local plugin path such as `~/plugins/codex-agent-teams`.
- Add or update the personal Codex plugin marketplace so it points at that local clone.
- Validate the plugin, install it with `codex plugin add codex-agent-teams@personal`, and verify it appears in `codex plugin list`.
- If the plugin source changes during install, update the Codex cachebuster first.
- Report the local source path, installed version, and recommended `CODEX_AGENT_TEAM_COMMAND`.

For stable generated commands, set:

```powershell
$AgentTeam = "path\to\codex-agent-teams\scripts\agent_team.py"
$env:CODEX_AGENT_TEAM_COMMAND = "python $AgentTeam"
```

```bash
export CODEX_AGENT_TEAM_COMMAND='python /path/to/codex-agent-teams/scripts/agent_team.py'
```

## When Agents Should Use It

Use ordinary Codex subagents for one-shot packets.

Use Codex Agent Teams when the task needs persistent coordination: named roles, shared state, dependencies, messages, plan or verification gates, cleanup, or a reviewer comparing multiple workers' outputs.

The expected backend order is:

1. Real Codex subagents.
2. Thread delegation if subagents are unavailable.
3. Simulated packet passes only as a last fallback.

## Minimal Agent Loop

The skill explains full usage. The essential lead loop is:

```powershell
python $AgentTeam --workspace . launch --spawn-policy ready-only ...
python $AgentTeam --workspace . bind-subagent --agent <name> --agent-id <id> --nickname <nickname>
python $AgentTeam --workspace . dashboard
python $AgentTeam --workspace . orchestrate --spawn-policy ready-only
python $AgentTeam --workspace . record-wait-batch --result-file .\wait-agent-result.json
python $AgentTeam --workspace . record-close-batch --result-file .\close-agent-results.json
python $AgentTeam --workspace . gate --verification-passed true --verification-command "<command>" --verification-exit-code 0 --verification-summary "<summary>"
python $AgentTeam --workspace . stop-check
python $AgentTeam --workspace . cleanup --ack-closed
```

Useful names agents should recognize: `claim --task`, `message --to team`, `record-delivery-batch`, `replace-subagent`, `init --force`, `launch --force`, and `--context-mode reference`.

For large shared files, prefer `--context-file <path> --context-mode reference`. That tells teammates to read the file from the workspace without duplicating the full contents inside every spawn prompt.

## Shared workspace semantics

Subagents and the lead operate in the same filesystem workspace. Forked context is conversation context, not a separate checkout or isolated file tree. Assign clear write scopes and keep generated artifacts inside the team workspace unless crossing that boundary is intentional.

## Common recovery flows

The skill contains detailed recovery instructions. The short version:

| Situation | Use |
| --- | --- |
| Unbound ready teammate | `orchestrate --spawn-policy ready-only`, then `bind-subagent`. |
| Pending delivery | The message is already in team state; execute host `send_input`, then `record-delivery-batch`. |
| Waiting runtime | Save raw `wait_agent` output, then `record-wait-batch --result-file <json>`. |
| Close-ready runtime | Execute close/archive, then `record-close-batch --result-file <json>`. |
| Dead or replaced runtime | `replace-subagent`. |
| Wrong/open work item | `reassign` or `cancel`. |
| Stale lock or corrupt state | `repair`. |

## Hard Limits

- This is not a native Codex team UI.
- Plugin scripts do not directly call `multi_agent_v1`; they emit host-tool plans for the lead to execute.
- Runtime state is only correct after the lead records host-tool results back into team state.
- The lifecycle loop is still explicit: spawn, bind, wait, record wait, close, record close.
- CLI actor guardrails prevent mistakes, not malicious misuse.
- Hook discovery is host-specific, so hook wrappers are commands rather than manifest hook fields.

## Development Checks

```powershell
python -m unittest discover -s tests -v
python path\to\plugin-creator\scripts\validate_plugin.py path\to\codex-agent-teams
```

During local plugin iteration:

```powershell
python path\to\plugin-creator\scripts\update_plugin_cachebuster.py path\to\codex-agent-teams
codex plugin add codex-agent-teams@personal
```
