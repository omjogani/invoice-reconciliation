---
name: graph-orchestrator
description: Drive a graph-based flow defined in a Graphviz DOT file — spawn workers, validate outputs, advance transitions — by calling the flowstate + agentctl CLIs.
---

# Graph Orchestrator (minimal)

You ARE the orchestrator. The flow's structure lives in a DOT file; the two
CLIs translate it into state and actions; the decisions between actions
belong to you.

> **This is a minimal skill** — just enough machinery to progress a
> flowstate graph. It specifies the CLI protocol faithfully but not much
> beyond it, and likely needs additional improvements; extend it as you
> see fit.

## Tools

Two CLIs emit YAML envelopes to stdout (run `setup.sh` in `orchestrator/`
once first):

- `orchestrator/bin/flowstate` — graph traversal, run state, validation
- `orchestrator/bin/agentctl` — worker lifecycle (spawn, wait, send, status, kill)

Every call returns one of two shapes:

```yaml
status: ok
payload: { ... }
```
```yaml
status: error
intent: "<what the tool was trying to do>"
failure: "<what went wrong>"
suggestion: "<a concrete manual workaround>"
```

## Concepts

- **Flow**: `factory/flows/<stem>/<stem>.dot` (nodes + edges) plus a
  companion `<stem>.flow.yml` (output schemas, variables). Agent nodes
  declare a `prompt_template`, a `working_dir`, an `output_schema`; edges
  may declare `gates` (shell scripts that must exit 0) and `condition`
  expressions over variables.
- **Run**: one execution of a flow. All state lives in
  `<run-dir>/graph_run_state.yml` — read it via `flowstate status` /
  `flowstate vars`, never directly.
- **Workers**: each agent node is executed by a fresh Claude Code session
  spawned in a tmux window. Workers see ONLY their rendered prompt —
  variables reach them through `{name}` placeholder substitution, and
  results come back as output files validated against the node's schema.
- **Your session id**: read it from the `CLAUDE_CODE_SESSION_ID=...` line
  the SessionStart hook injected at the top of this conversation. Do not
  read the env var from shell — it can carry a stale value.

## Step 1 — Bootstrap

One call resolves prefs, initialises the run, and performs the first
advance:

```bash
orchestrator/bin/flowstate bootstrap \
  --run-descriptor "<short-path-safe-name>" \
  --orchestrator-session-id "<your session id>" \
  --seed-var <name>="<value>"        # repeatable; --seed-var-json for lists/dicts
```

Seed every variable the first node's prompt template interpolates, or the
first render fails. The envelope carries everything needed to start:

- `payload.resolved.run_dir` → your `$run_dir` for every later call
- `payload.summary.graph` → node/edge digest; narrate from this, don't re-read the DOT
- `payload.advance` → the first transition, including `node_config` and
  `rendered_prompt` for the first agent node — go straight to Step 2c

Pass `--flow-dot <path>` or `--supervision afk|low|medium|high` only to
override the defaults in `factory/factory-prefs.yml`.

**Trust the envelope.** After a successful bootstrap, do not re-call
`summary`, `node-config`, `render-prompt`, or `status` to "check" — the data
is already in the payload. One exception: when bootstrap lands ON a
`dynamic_fanout` node, the branches are not yet registered — the next
`advance` performs the registration and returns `next_startable_branches`.

## Step 2 — Main loop

Repeat until the flow reaches its end node.

### 2a. Advance

```bash
orchestrator/bin/flowstate advance --run-dir "$run_dir"
```

Advance auto-chases: script nodes execute inline, resolvable conditions
route automatically, and the call returns only when something needs YOU.
`payload.chased[]` lists the hops taken. Branch on `payload.kind`:

| kind | Meaning | What you do |
|---|---|---|
| `moved` | Landed on `payload.target`. Agent node → 2c. `runner: orchestrator` node → execute its rendered prompt yourself in this session, then `flowstate complete "<node>" --run-dir "$run_dir"` (no spawn). | |
| `end` | Reached the end node | Step 3 |
| `choice_needed` | Fan-out flowstate couldn't auto-resolve; `payload.options[]` lists the edges | Reason over labels/conditions, re-call `advance --target <chosen>` |
| `blocked` | A gate failed, or current phase isn't done; `payload.reason` explains | Your failure policy |
| `infra_error` / `error` | Transition script failed / invalid call | Surface; do not blind-retry |

### 2c. Extract the spawn inputs from the envelope

When `kind: moved` lands on an agent node, the payload already includes
`node_config` (`working_dir`, `model`, `harness`, `autonomy`, `outputs[]`,
`temp_dir`) and `rendered_prompt`. If `rendered_prompt_error` is present
instead, a placeholder variable is unset — `flowstate set-var` it, then
`flowstate render-prompt <node> --run-dir "$run_dir"`.

### 2e. Spawn the worker

```bash
orchestrator/bin/agentctl spawn \
  --harness "<node_config.harness or claude-code>" \
  --working-dir "<node_config.working_dir>" \
  --phase "<target>" \
  --prompt "$RENDERED_PROMPT" \
  --run-dir "$run_dir" \
  --temp-dir "<node_config.temp_dir>" \
  --run-descriptor "<the run descriptor>" \
  --repo-root "$(git rev-parse --show-toplevel)" \
  [--model "<node_config.model>"] [--autonomy "<node_config.autonomy>"]
```

Save `payload.agent_id` and (from the first spawn) `payload.tmux_session`.
The worker runs in a tmux window; `payload.attach_hint` tells the human how
to watch it.

### 2f. Wait — one blocking call, not a poll loop

```bash
orchestrator/bin/agentctl wait "$agent_id" --max-seconds 240   # loop on `timeout`
```

Prefer bounded waits (~240s, tool timeout 300000 ms) in a loop over one
long blocking call — agent harnesses commonly kill a stream that is silent
for ~10 minutes, and the 570s default sits right on that line. With several
live workers, round-robin `--max-seconds 60`. The envelope's
`payload.outcome` is one of:

| outcome | Meaning |
|---|---|
| `completed` | Worker wrote its completion marker → 2g |
| `timeout` | Time budget elapsed, worker alive and working → just call `wait` again |
| `awaiting_human` | Worker asked a question (`evidence.awaiting_human_reason`) → relay it to the user, inject the answer with `agentctl send "$agent_id" "<answer>"`, `wait` again |
| `stalled` | No log activity and no child processes for the stall window → your failure policy |
| `died` | Process gone without completing (`evidence.spawn_failed` distinguishes never-started) → your failure policy |

`evidence.agent_session_id` locates the worker's transcript at
`~/.claude/projects/<encoded-cwd>/<session-id>.jsonl` — `tail -40` it when
you need to see what the worker actually did. `agentctl send <id>
[--interrupt] "<text>"` types into the worker's session (plain text queues
safely; `--interrupt` ends its current turn first). `agentctl status <id>`
re-probes on demand.

### 2g. Finish — validate + reap + advance in one call

```bash
orchestrator/bin/flowstate finish "<target>" --run-dir "$run_dir" --agent-id "$agent_id"
```

- `payload.validate.passed: true` → outputs conform to the node's schema,
  the worker was reaped, and `payload.advance` carries the next transition.
  **Continue the loop from that nested envelope — don't issue another
  `advance`.**
- `payload.validate.passed: false` → `payload.validate.feedback` says why,
  and the worker is deliberately left alive (it may be able to fix its own
  output) → your failure policy.

Standalone `flowstate validate`, `flowstate advance --force` (marks the
phase `done_forced`, auditable), `flowstate kill-check` + `agentctl kill`
exist for when your policy needs a step in isolation.

### 2h. Supervision pauses

Supervision levels are ordered `afk < low < medium < high`. After a
successful validation, if the node declares `pauses_at_min` and the run's
supervision is at or above it, pause: show the user the node's result and
the current variables, and wait for confirmation before advancing.

## When things fail

The protocol above stops at three junctures: **validation failed**,
**worker stalled**, **worker died** (plus `blocked` gates). This skill does
not specify what to do there — handle them as you judge best, and extend
this skill if you find that useful. (`flowstate event --kind <k> --node <n>
--message <m>` appends to the run's event log, if you want a record of what
you did.)

## Parallel branches (fan-out flows)

When a flow uses `fork` / `dynamic_fanout` / `join` nodes, an `advance` on
the fan-out node registers the branches. For static `fork`, the envelope
carries `active_phases` + `active_phase_configs`; for `dynamic_fanout` it
carries `next_startable_branches` + `max_concurrent_resolved` (flowstate
owns the concurrency cap — you don't count). Start each named branch with
`flowstate start-branch --run-dir "$run_dir" --node <fanout> --branch
<id>`, then drive it with the normal loop using `--node "<branch_node>"`
(fork arms, shared run dir) or the child's own `--run-dir` (subflow
branches). **When spawning a subflow child's workers, pass the CHILD's run
descriptor (its run-dir name / start-branch payload) as
`--run-descriptor`** — reusing the parent's collides on the tmux session
name and mints one session (and one viewer window) per worker. With several live workers, round-robin short waits
(`agentctl wait <id> --max-seconds 60`) instead of blocking 570s on one.
Inside an inline fork arm, every `set-var` MUST carry `--node
"<branch_node>"` so the write lands in branch scope. Joins fire
automatically once all branches land; `flowstate status` shows per-branch
state.

## Step 3 — Done

When `advance` returns `kind: end`:

1. Tear down every tmux session belonging to the run — each subflow child
   run has its own. Enumerate `tmux ls` and kill each matching session by
   exact name (`tmux kill-session -t "=<name>"`); don't assume a single
   session.
2. Report to the user: final variables (`flowstate vars`), phase statuses
   (`flowstate status`), and where the run's artifacts live (`$run_dir`).

## Ground rules

- As the orchestrator, NEVER edit `orchestrator/lib/**`, the CLIs, this
  skill, or the flow's definition files. If you hit what looks like a bug
  in the machinery, stop and surface it to the human — changing the
  machinery is a human decision, made outside a run.
- Never hand-edit `graph_run_state.yml` — the CLIs own it.
- Workers write output files; you don't write outputs on their behalf.

## Field notes (learned from real runs — read before your first)

- **Keep your own spawn ledger.** `flowstate status` does not carry a live
  worker's `agent_id` (it is recorded at finish). If you lose your session,
  recover worker ids from the agentctl registry (`~/.agentctl/*.yml`) or
  the per-phase temp dirs — not from run state.
- **Inspect the pane before judging a stall.** An interactive dialog (for
  example Claude Code's workspace-trust prompt on a first spawn in a fresh
  clone) reads as `stalled` with no child processes; `awaiting_human` only
  covers the marker file. `tmux capture-pane` the worker's window — resolve
  the window by index via `list-windows`, not by name — and answer dialogs
  via `agentctl send` (e.g. "1" for trust).
- **Spawning a node you already advanced onto:** when there is no fresh
  advance envelope (post-recovery, or fan-out arms), get the inputs with
  `flowstate node-config <node>` + `flowstate render-prompt <node>`.
- **Respawns use `prompt.raw.txt`**, never the substituted `prompt.txt` —
  the substituted copy embeds the previous attempt's session id.
- **`flowstate event` flags:** `--kind <k> --node <n> --message <m>`, plus
  repeatable `-d key=value` for structured payload. There is no `--note`.

