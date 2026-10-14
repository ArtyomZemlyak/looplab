# 78. External-agent orchestration: waking, GPU order, failure forensics, routine checks, canary

Date: 2026-10-14. Origin: the audit of Codex as an external LoopLab agent
(`looplab-reviews/20261014-codex-external-audit/report.md`, items L-01, L-02, L-05, L-07 and
incident G1). The agent had written ~11 k lines of its own wrappers — GPU queues, handoff scripts,
a `codex queue` notification bridge, regex bots answering monitor questions, supervisors and
watchdogs — and every incident of the night happened inside them. This document describes what
LoopLab now does itself, the API an agent uses instead, and examples for Codex and Claude Code.

Everything here is additive: an agent that ignores it keeps working exactly as before.

| Need | Before (agent scripts) | Now (LoopLab) |
|---|---|---|
| Wake the agent on events | `codex queue` from 33 call sites, hand-kept ledger | outbox + `wait_for_next` long poll + operator hook with retries and a ledger (§1) |
| Run B after run A on the GPUs | pause/resume handoff scripts, ack files (raced by ~2 s) | `gpu_gate` `start_after` / `hold`, FIFO GPU queue (§2) |
| Why did it fail? | grep the child log on S3 | `failure_forensics` on `node_failed`, earlier attempts' logs kept (§3) |
| Routine monitor answers | regex bot posting `continue` | declared rules answered by LoopLab, `answerer: rule` (§4) |
| Catch a broken CLI flag early | nothing (failed after data prep) | `eval.canary` on by default for external runs, `smoke` mode (§5) |

## 1. Waking the agent

### 1.1 What LoopLab records

The UI server (`looplab ui`) runs one background pump (`looplab/serve/agent_events.py`). For every
external-harness run under its run root it derives **actionable events** from LoopLab's own record
and appends them to a durable outbox, `<run-root>/.agent-events/outbox.jsonl`:

| kind | derived from | details |
|---|---|---|
| `node_evaluated` | `node_evaluated` row | metric, generation |
| `node_failed` | `node_failed` row | engine reason, error head, forensics headline / signals / exit codes (§3) |
| `stage_failed` | `stage_finished` with a failure status | stage, status, exit code |
| `monitor_question` | a pending checkpoint the rules did not answer (§4) | checkpoint_id, phase, stage |
| `obligation_due` | harness-progress gates (research, report, reviews, …) | phase_id |
| `run_idle` | engine running, waiting for the next experiment, node budget left | budget_left |
| `run_paused` / `run_finished` | `pause` / `run_finished` rows | reason |
| `gpu_free` | host GPU pool free with nobody queued for 60 s | — |

Each row has a monotonically increasing `id` (the cursor) and a dedupe `key`, so a restarted
server, two servers on one root, or a condition seen on every scan never produce a duplicate. On
first sight of a run the pump starts at the end of its log (no history flood) but still reports
current conditions. The pump only runs while the UI server runs; its watermarks are durable, so after
a restart it catches up on the rows it missed.

### 1.2 Long poll: `GET /api/agent-events` / MCP `wait_for_next`

```
GET /api/agent-events?after=<last id>&timeout=<s ≤ 600>[&run_id=…][&kinds=a,b]
→ {"events": [{"id", "ts", "run_id", "kind", "node_id", "seq", "summary", "details"}, …],
   "next_after": <id>, "timed_out": bool, "has_more": bool, "gap": bool, "stale_cursor": bool}
```

* Omit `after` once to subscribe "from now" (`next_after` = newest id); `-1` reads the retained
  history (the outbox keeps ~4 MB plus one rotated file — `gap: true` means older rows rotated away).
* The call returns as soon as an event past the cursor exists, else after `timeout` with
  `timed_out: true`. `stale_cursor: true` means the outbox was rebuilt; continue from `next_after`.
* An event is a nudge, never a permission: read `run_progress` for that run before acting.

MCP: `wait_for_next(after=None, timeout_s=120, run_id="", kinds="", limit=50)` performs exactly
this GET (the HTTP client timeout is extended accordingly). A typical agent loop:

```python
cursor = wait_for_next()["body"]["next_after"]          # subscribe from now
while True:
    page = wait_for_next(after=cursor, timeout_s=600)["body"]
    for event in page["events"]:
        handle(event)       # read run_progress(event["run_id"], …) and decide
    cursor = page["next_after"]
```

### 1.3 Notification hook (operator)

For an agent that cannot block on a long poll — a CLI agent whose turn ends, or one whose turn may
die (`server_overloaded`) — the operator configures a command LoopLab runs on new events:

```bash
looplab agent-notify --run-root /dev/shm/looplab-runs \
  --command 'codex queue --thread {thread} --message {message}' \
  --var thread=01a12f07-xxxx --debounce 20
looplab agent-notify --run-root /dev/shm/looplab-runs          # show config + delivery ledger
looplab agent-notify --run-root /dev/shm/looplab-runs --clear
```

or `PUT /api/agent-notify` with the owner token (the harness token is refused: the command is code
the server executes). Semantics:

* the command is an **argv list run without a shell**; placeholders are `{message}`, `{run_id}`,
  `{runs}`, `{kinds}`, `{count}`, `{after}`, `{first_id}`, `{last_id}` and every name in `vars`;
  unknown placeholders are refused when the hook is configured;
* events are **batched**: a delivery happens after `debounce_s` (default 15) of quiet, or at the
  latest `max_delay_s` (120) after the oldest pending event, at most `max_batch` (20) per message;
* a failed delivery (non-zero exit, timeout, missing binary) is **retried** with exponential
  backoff (`retry_base_s` 30 → 60 → …) up to `max_attempts` (5); then it is recorded as
  `abandoned`, the cursor moves on, and the next message says how many notifications were lost — the
  events stay in the outbox;
* every attempt is a row in `.agent-events/deliveries.jsonl` (`GET /api/agent-notify`, the UI's
  *Agent wake-ups* panel): time, attempt, event ids, runs, kinds, status, exit code, stderr tail;
* the message is composed by LoopLab from ids, kinds and short summaries, passed through the secret
  redactor, and ends with the cursor to read details from; the hook's environment never contains
  `LOOPLAB_UI_TOKEN`, `LOOPLAB_HARNESS_TOKEN` or any `LOOPLAB_*TOKEN*`;
* a run may override variables (e.g. its own thread) or opt out:
  `PUT /api/runs/{run_id}/agent-notify {"vars": {"thread": "…"}, "enabled": true}` — the harness
  token may do this; values are short tokens (`[A-Za-z0-9._:@/+=,-]`).

`POST /api/agent-notify/test` runs the command once with a test message (owner only).

## 2. Cross-run GPU order

### 2.1 The FIFO queue

The host GPU lease (one `flock` per OS user, `/tmp/looplab-gpu-pool-<uid>.lock`) made co-hosted
runs mutually exclusive but unordered: whichever process polled first after a release won, and the
holder could take the pool straight back between two of its own evaluations. Now
(`looplab/engine/gpu_queue.py`) an engine that needs the lease first takes a **ticket** in
`<lease>.queue/` (named by creation time, flock-held by the waiting process, heartbeat on every
admission poll). Only the **head** of the queue tries the lease; a holder that releases and needs
the pool again queues at the tail. A ticket whose process died is pruned by whoever meets it; one
whose owner stopped polling (its evaluation was cancelled, the run paused) stops counting after 15 s.
So a release hands the pool to the run that waited longest, with no window for a third process.

`GET /api/gpu-queue` / `looplab gpu-queue`: the holder (pid, run, since — from the kernel lock
table) and the waiting tickets in order, plus every run's gate. The UI shows it on the runs page
(*GPU queue*).

### 2.2 The per-run GPU gate

`<run>/gpu_gate.json`, re-read by the engine on every admission poll:

* `hold: true` — this run takes **no GPU** until released;
* `start_after: [run dirs]` — no GPU until every named run's **engine has exited** (paused,
  finished, aborted or crashed: `engine.lock` free). An inconclusive probe keeps waiting.

Only GPU admission waits: setup, CPU-only stages (`footprint {"gpus": 0}`) and the agent's own
commands (injects, memos, reviews) proceed. A malformed gate holds (it never silently starts GPUs).

```bash
# before `looplab run` creates the run: start it held, behind run A
looplab gpu-gate runs/B --hold --start-after runs/A --reason "after A's training"
looplab run b.yaml --out runs/B -s external_harness=true --backend toy
looplab gpu-gate runs/B --release        # drop the hold; B still waits for A's engine to exit
```

```
PUT /api/runs/B/gpu-gate {"hold": false, "start_after": ["A"], "reason": "…"}   # harness token OK
PUT /api/runs/B/gpu-gate {"clear": true}
GET /api/runs/B/gpu-gate → {"gate": {...}, "blockers": [{"kind": "start_after", "run_id": "A", …}]}
```

A `start_after` cycle is refused (409). This replaces the handoff scripts and their ack files: the
order is decided by LoopLab at GPU admission, after the predecessor's engine is gone, so the C2 race
(lease released ~2 s before the ack) cannot happen.

Note: inside ONE run, consecutive nodes already queue on the run's own evaluation lane; the gate is
for ordering separate runs. Prefer one run per campaign where the task allows it.

## 3. Failure forensics

### 3.1 `failure_forensics` on `node_failed` / `node_repaired`

`looplab/runtime/failure_forensics.py` parses what the failed attempt printed (stdout/stderr tails,
the failed stage's log tail, `eval.log`) — deterministically, no model:

```json
{"version": 1,
 "headline": "stage stage1: SIGSEGV on rank(s) 3,5 AssertionError: ('G0', 'stage1', 1)",
 "exit_code": 1, "failed_stage": "stage1",
 "root_exception": {"type": "FileNotFoundError", "message": "… compact_pieces.json", "source": "stage2.log", "rank": null},
 "exceptions": [...raise order, "chained" marks handler-side wrappers...],
 "signals": [{"signal": "SIGSEGV", "ranks": [3, 5], "source": "torchrun"}],
 "exit_codes": {"stage stage1": 1, "rank 3": -11, "rank 5": -11},
 "ranks": [{"rank": 3, "local_rank": 3, "exitcode": -11, "pid": 4240, "signal": "SIGSEGV", "root_cause": true}],
 "faulthandler": {"error": "Segmentation fault", "stack": ["File \"model.py\", line 10 in forward", ...]},
 "hints": ["nccl", "out_of_memory"],
 "rank_tails": {"rank 3": ["…last lines…"]}, "log_tails": {"stage1.log": ["…"]}}
```

The root exception is the first non-wrapper exception in raise order (`ChildFailedError`,
`CalledProcessError`, `SystemExit` and handler-side chained exceptions are wrappers). Signals come
from negative exit codes, torchrun's per-rank failure blocks, `<Signals.SIGSEGV: 11>`, shell
messages ("Segmentation fault", "Killed"). Every string goes through the engine's redaction funnel;
the record is bounded (≤ 24 000 chars). It is a record: nothing that decides reads it. The
`node_failed` agent event carries its headline/signals/exit codes; the UI shows it on the node.

A wrapper script that runs the real trainer in a subprocess should tee the child's output to its own
stdout/stderr (or a stage log): LoopLab can only parse what the stage printed.

### 3.2 Earlier attempts' logs are kept

Before a node's workdir is rebuilt for a new lifecycle (reset, retry), its top-level `*.log` files
are moved to `<run>/attempt_logs/node_<id>/before-gen<N>-<UTC>/` (≤ 20 archives per node).
`GET /api/runs/{run_id}/attempt-logs[?node_id=]` lists them; read a file with
`GET /api/runs/{run_id}/artifact?root=run&path=<listed path>&expected_generation=…`.

### 3.3 Crash diagnostics in the evaluation environment

External-harness evaluations get `PYTHONFAULTHANDLER=1` and `TORCH_SHOW_CPP_STACKTRACES=1` as the
lowest environment layer: a native crash prints every thread's Python stack (and C++ frames from
PyTorch). `Settings.eval_env`, the task's `eval.env` and a stage's `env` override it; set
`PYTHONFAULTHANDLER=` (empty) to switch it off, or add the same variables through `-s eval_env=…`
on an internal run.

## 4. Routine checkpoint answers by declared rules

A run with live monitoring opens a checkpoint per observation tick and the engine waits for each
answer. Declare which answers are routine and LoopLab answers them itself:

```
PUT /api/runs/{run_id}/harness-monitor-rules
{"train_monitor": {"routine_if": {"loss_finite": true, "progress_advances": true,
                                  "forbid_patterns": ["Traceback", "out of memory", "NCCL error"]}},
 "asha_live":     {"routine_if": {"progress_advances": true}},
 "stage_check":   {"routine_if": {"require_patterns": ["Saved checkpoint"]}, "rule_id": "saved-ckpt"}}
```

* Conditions: `loss_finite` (a loss value is visible and the last one is finite),
  `progress_advances` (a step/iteration/epoch counter is visible and larger than the last one a rule
  accepted for the same node lifecycle and phase), `forbid_patterns`, `require_patterns`
  (case-insensitive regexes).
* When every declared condition holds, `ask` writes the answer in the same breath: `continue`
  (monitors) or `proceed` (stage check), with `answerer: "rule"`, `rule_id` and the facts it read
  (`rule_facts`). Rules never abort, fail or extend — anything else stays open and is what the agent
  is woken for (`monitor_question`). `deadline_grace` is always the agent's.
* `{}` clears the rules; `GET` reads them. A damaged rules file answers nothing.
* Every answer now names its author: `answerer` ∈ `rule`, `agent` (harness token), `human` (owner
  token), `engine`. Diagnostic `train_monitor_alert` rows carry `source: rule|engine|operator|external_agent`.
* **A failed evaluation never waits for an advisory answer**: when the evaluated process already
  ended without a measurement, the engine closes its open live questions (`answerer: "engine"`,
  "moot") and writes the terminal at once, and no final-pass question is asked about it.

Agent activity: rule answers and long-poll reads are not agent writes. Report "the agent is alive"
from authored actions (commands, decisions, answers with `answerer: agent`), not from reads.

## 5. The eval canary for external runs

`eval_canary` (doc 69) runs the node's own stage chain on a tiny slice before its full evaluation.
For an **external-harness run it is on by default** whenever the task declares `eval.canary`
(`Settings.eval_canary` still switches it on for internal runs). Two modes:

* `slice` (default): the chain must finish on the slice and print a number. Make your scripts honour
  `LOOPLAB_CANARY=1` (or declared `env`) by running e.g. one step on a few hundred rows.
* `smoke`: for scripts that cannot run a slice — the chain only has to **survive** `timeout` seconds
  (or exit 0 sooner); a crash before that (an unknown CLI flag such as `max_steps`, an import or
  missing-file error) fails the canary in seconds. Smoke runs the real command in a scratch directory
  with the real environment: redirect absolute output paths through `canary.env`.

```json
"eval": {"stages": [...],
         "canary": {"mode": "smoke", "timeout": 120, "env": {"MR_DURABLE_ROOT": "/tmp/canary"}}}
```

A passed canary is remembered per code digest; a failed one is the attempt's crash (with forensics).

## 6. Examples

### Codex CLI (one long-lived thread, many runs)

Operator, once:

```bash
looplab agent-notify --run-root /dev/shm/looplab-runs \
  --command 'codex queue --thread {thread} --message {message}' --var thread=<codex thread id>
```

Codex, per campaign step:

1. Create run B held behind A: `looplab gpu-gate <runs>/B --hold --start-after <runs>/A`, then
   `looplab run …` (or `PUT /api/runs/B/gpu-gate` once B exists). Release the hold when B's first
   candidate is injected: `PUT /api/runs/B/gpu-gate {"hold": false, "start_after": ["A"]}`.
2. `PUT /api/runs/B/harness-monitor-rules` with the routine policy; answer only escalated questions.
3. Declare `eval.canary` (slice or smoke) in the task.
4. End the turn. LoopLab queues a message into the thread when something actionable happens; the
   message lists event ids — call `wait_for_next(after=<first id - 1>, timeout_s=0)` and
   `run_progress` before deciding.

No supervisor, watchdog, handoff or notification scripts are needed.

### Claude Code (MCP, blocking loop)

```text
mcp: connection_check(run_id) → run_progress(run_id, generation)
mcp: api_request PUT /api/runs/{run_id}/harness-monitor-rules {...}
loop:
  page = wait_for_next(after=cursor, timeout_s=600)
  for each event: run_progress(event.run_id, generation) → decide → inject / answer / finalize
  cursor = page.next_after
```

If the session may end between events, the operator's hook can wake it instead (any CLI that accepts
a message argument works as the command).

## 7. Deploy notes

* The pump, routes and GPU queue take effect when the UI server and the engines are restarted on
  the new build. Engines started before the upgrade keep the old unordered lease; mixing old and new
  engines on one box gives the old ones no queue position (they still respect the lease itself).
* Disable the pump with `LOOPLAB_AGENT_EVENTS=0` in the UI server's environment.
* Files: `<run-root>/.agent-events/{outbox.jsonl,state.json,notify.json,deliveries.jsonl}`,
  `<run>/gpu_gate.json`, `<run>/agent_notify.json`, `<run>/harness_monitor_rules.json`,
  `<run>/attempt_logs/`, `<lease>.queue/`, `<lease>.holder.json`.
