---
name: graph-orchestrator
description: Drive a graph-based flow defined in a Graphviz DOT file — spawn workers, validate outputs, advance transitions — by calling the flowstate + agentctl CLIs.
---

# Graph Orchestrator (minimal)

You ARE the orchestrator. The flow's structure lives in a DOT file; the two
CLIs translate it into state and actions; the decisions between actions
belong to you.

> Started from the kit's minimal skill. Extended for the freight
> reconciliation with a spawn helper (2e), a mandatory failure policy ("When
> things fail"), and a verified procedure for driving `dynamic_fanout`
> children. The changes are listed in DESIGN.md.

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

Use the helper. It reads `node-config` and `render-prompt` with the venv's
YAML parser, refuses an empty or unrendered prompt, calls `agentctl spawn`
with the node's harness, model and autonomy, and appends the spawn to
`<run-dir>/spawn-ledger.jsonl`:

```bash
orchestrator/bin/spawn-node "<target>" --run-dir "$run_dir" [--session "<tmux_session>"]
```

Pass `--session` with the `tmux_session` from your first spawn when the
worker belongs to a subflow child run, so every worker of the run shares one
tmux session. The output is the usual agentctl envelope.

Do not pipe envelopes through the system `python3` to extract the prompt
yourself: it may lack PyYAML, the extraction then fails silently and the
worker starts with an empty prompt (this happened during exploration).

The underlying call, for reference or when the helper is unavailable:

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

## When things fail — the failure policy

Follow this policy; do not improvise around it. Record **every** decision
below with `flowstate event --kind <kind> --node <node> --message "<what and
why>"` (repeatable `-d key=value` for data), so the run log explains the run.

**Important:** a worker is ended by its wrapper the moment its
`completion.yml` appears. After `finish` reports `validate.passed: false`
the worker is already gone. Do not `agentctl send` to it; retry by
respawning, as below.

| Situation | Action | Limit, then |
|---|---|---|
| `finish` → `validate.passed: false` | Write `validate.feedback` (plus any gate or check output that explains it) to `<temp_dir>/feedback-<n>.txt`, log `--kind validation_retry`, then `orchestrator/bin/spawn-node <node> --run-dir <dir> --retry-feedback <file> [--session …]`, `wait`, `finish` again | 2 retries per node. Then log `validation_exhausted`, stop the run and show the human the feedback history. Never `advance --force` past a failed validation. |
| `wait` → `stalled` | `tmux capture-pane -p -t <session>:<window-index>` (resolve the index with `tmux list-windows`). A dialog (for example workspace trust) → answer it with `agentctl send`, log `dialog_answered`, `wait` again. A worker that is still thinking (spinner, recent transcript writes) → `wait` again. | Nothing on screen and no transcript growth for two more waits → `agentctl kill`, log `stall_respawn`, respawn with `--prompt-file <temp_dir>/prompt.raw.txt`. One respawn; a second stall stops the run. |
| `wait` → `died` | `tail -40` the worker transcript (`evidence.agent_session_id`). `spawn_failed: true` or a transient error → log `died_respawn` and respawn once from `prompt.raw.txt`. | A second death stops the run with the transcript tail shown to the human. |
| `wait` → `awaiting_human` | Relay the question to the human verbatim; send the answer with `agentctl send`; log `human_answer`. | — |
| `advance` → `blocked` (gate failed) | Read `payload.reason`. Gates in these flows are deterministic: re-running without a change cannot help. Log `gate_blocked` and show the human the reason. | Stop. |
| `advance` → `error` from a script node | Scripts are deterministic too. Show the human stderr (`payload.reason`), log `script_error`. | Stop; never hand-write the script's outputs. |
| `advance` → `choice_needed` | Every condition in these flows is resolvable; this means a variable is missing. Check `flowstate vars`, log `choice_needed`, and surface it. | Stop rather than guess a branch. |
| A `runner=orchestrator` hold node | Execute its rendered prompt yourself. It tells you what to show the human and which command records their decision. Never approve on the human's behalf. | — |

"Stop the run" means: leave the state as it is (the CLIs can resume it),
kill the run's tmux sessions, and report to the human what failed, what you
tried, and where the evidence is.

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

### Driving `dynamic_fanout` children, step by step

This loop was verified end to end on this engine:

1. `flowstate advance --run-dir "$run_dir" --node <fanout>` registers the
   branches and returns `next_startable_branches` (already capped by
   `max_concurrent`).
2. For each id: `flowstate start-branch --run-dir "$run_dir" --node <fanout>
   --branch <id>` → `payload.subflow_run_dir` is the child's run dir.
3. Drive each child with the normal loop against the **child's** run dir:
   `flowstate advance --run-dir <child>`, `spawn-node <node> --run-dir
   <child> --session <tmux_session>` for its agent nodes (for a `fork`
   inside the child, use the envelope's `active_phases` and
   `advance --node <arm>`), and `finish <node> --run-dir <child>
   --agent-id <id>`. Round-robin `agentctl wait <id> --max-seconds 60`
   across live workers.
4. When a child reaches `kind: end`, flowstate pushes its declared outputs
   into the parent and fires the join's reducer. Nothing to call.
5. Re-run step 1. It returns more `next_startable_branches` (a rolling
   window) or `fanout '<name>' complete; advanced to '<template>'`.
6. Then `flowstate advance --run-dir "$run_dir"` passes through the
   template and join into the next node.

Engine limitation: a `join` wired straight into the end node crashes the
advance envelope (it builds a node config for the end node). The flows here
always put a script node after a join; keep it that way in new flows.

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

