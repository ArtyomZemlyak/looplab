# Pre-chewed evidence — where a role is handed a slice instead of being allowed to look

**A survey with a per-site verdict.** Two sites were converted (§2); the other four are named with a
reason for or against (§3). The rule applied to every row is
[`36-agent-driven-decisions-2026-08-13.md`](36-agent-driven-decisions-2026-08-13.md): a role may be
allowed to LOOK wherever what it decides is what happens **next**, and what it looks at never becomes
a fact the **record** rests on. Widening a look is not widening authority, and a row that would blur
those two is refused here rather than shipped and walked back.

The operator's framing, verbatim:

> «давай делать тулзы адекватными. пусть при анализе трейн лога можно будет читать и сам лог (как
> обычный файл) и метрики (тоже как захочет, хоть последние 10 секунд, хоть последний день с
> периодичностью 1 час). в том числе и по другим моментам так же.»

---

## 1 · The measurement

The training monitor decides whether to KILL a node. Measured on the live run
`runs/rubertlite-dr-unified-v7` (2026-08-14):

* `train_monitor.training_log_digest` keeps 40 records and then bounds them to 4000 chars.
* A tqdm bar record on the real logs is ~330 characters, so the 40 became **about ten**.
* Those ten span roughly **30 seconds of a five-hour run**, and at the default
  `train_monitor_interval_s = 600` consecutive tails **do not overlap at all** — so no amount of
  re-checking accumulates coverage.
* Replayed on node 1 the judge saw `22.8906 22.9009 22.8881 …` and answered `broken` 0.82, "pinned at
  ~23.0 … no learning trend from its initialization value", about a node that had gone 27.69 → 22.92.
  Node 0 drew six `watch` rows the same way.

The verdict was a correct reading of what it was shown. **The question was unanswerable from the
evidence**, which is worse than an ambiguous answer because it produces a confident wrong one: inside
any short window a decelerating curve is below the step-to-step noise floor, so "converged", "stuck at
initialization" and "still descending slowly" are observationally identical.

Driven against the same file with `metric_series`, at the granularity the question actually needs:

| bucket (run clock) | n | median | IQR | min | max |
|---|---:|---:|---:|---:|---:|
| +0:02 | 238 | 24.7864 | 2.2399 | 22.0264 | 45.7692 |
| +1:02 | 232 | 23.2005 | 0.3372 | 21.1963 | 23.7247 |
| +2:02 | 237 | 22.7685 | 0.1511 | 20.9521 | 22.9752 |
| +3:02 | 219 | 22.5505 | 0.0821 | 20.7669 | 22.7063 |

The medians descend monotonically and the within-bucket spread collapses by 27x. Neither fact is
visible in any ten consecutive samples.

---

## 2 · Converted

### 2.1 The training monitor's judge — `engine/train_monitor.py`

`_training_verdict` was a single `parse_structured` completion over a spliced digest. It now routes
through `trust/judge.py::structured_judge(tools=…)` — the same contract both trust verifiers use —
with `tools/log_tools.py`'s two surfaces:

* `read_log` — the named stage log as a FILE: tail, head, a record range, a regex search with
  context. A "record" is split on `\n` **or** `\r`, because a tqdm bar writes a whole multi-hour run
  into one newline-delimited line and a line-oriented reader answers "the last 60 lines" with one
  line. Bar fill is squeezed (`45%|█…▍ …| 8004/17650 [3:24:37<4:01:19, 1.50s/it]`) so every number
  survives and only the fill is lost.
* `metric_series` — the numeric series over a time window at a granularity: `last_s=10`,
  `bucket_s=3600`, `whole_run=true`. Per bucket: count, median, spread, min, max, first, last. **The
  reduction never drops a sample** — a coarser bucket aggregates more, and `bucket_series` is total
  over its window with the bucket-count cap living at the call site as a stated refusal naming a
  width that fits. A clamp inside the aggregator would cover the window's head and silently drop its
  tail, which is the identical defect one layer down.

**What did not change.** `should_monitor_kill`'s conjuncts are untouched and the deterministic
`LossTrajectoryTracker` veto is still what stands between a `broken` verdict and a dead node. That is
doc 36's second corollary applied literally: the tool widens what a role may LOOK at, never what it
may ASSERT. Everything it returns is text the candidate's own training script wrote.

### 2.2 The ASHA judge — `engine/asha_monitor.py`

`_asha_verdict` already called `structured_judge`, whose signature has always carried `tools` — it was
simply never given any, so it took the `tools is None` branch and degraded to a plain parse. It now
passes the same provider, built by the same `monitor_log_tools`, so a run cannot end up with a looking
training monitor and a blind ASHA judge. That builder is a FREE FUNCTION taking the engine
(`engine/speculation_gate.py`'s shape, for its stated reason) rather than a mixin method: as a
method it must live on a mixin one of the two watchdogs does not inherit, and an ASHA-only
object then raises an AttributeError that `_monitor_asha`'s own per-tick containment `except`
swallows into a watchdog that silently stops producing verdicts for the rest of a multi-hour
eval. `tests/test_asha_monitor.py::_AshaStub` is exactly that object and is what caught it.

Its slice is a **different** one and worth naming: this judge never sees a byte of log text at all.
`asha_judge_context` hands it 12 curve points mined from a 128 KiB tail (`max_shown=12` over
`extract_resource_curve`'s `max_points=32`), ≤8 extra metrics, ≤12 sibling values, and a 300-char echo
of the training monitor's reason. So "is this run behind but still improving" — the exact case its own
system prompt tells it to spare — is asked of twelve numbers whose spacing it cannot see.

---

## 3 · Named and NOT converted, with the reason

### 3.1 Crash triage — `engine/crash_repair.py` / `agents/unified_agent.py` · **the biggest one, and a tool is not the fix yet**

The cut is `engine/evaluate.py::_eval_failure_text`: `res.stderr[-500:]`. That 500-char tail is the
whole crash diagnosis, and it is simultaneously the repair prompt, `node_repaired.error_in`, the
judge's history rows and the node terminal's `error`. Beside it, `unified_agent.py` splices
`node.code[-1500:]` — the model is asked to fix code it can only see the bottom of.

The call site is already agentic (`_pilot_emit` → `drive_tool_loop` with `_pilot_tools`), so a
provider would drop straight in. **It is not converted here for a reason that a tool does not fix**:
the bottleneck is what gets **persisted**, not what a role may read. `RunTools.read_logs` already
exists and already bottoms out on the same 500 bytes, because that is all `evaluate.py` kept. Pointing
a log tool at the node's workdir instead would widen it genuinely — the workdir is still on disk at
triage time — but that is a different change with its own lifetime question (a reset or a workspace
cleanup between the failure and the triage), and it should be measured before it is shipped. Recorded
as the next row, not done blind.

### 3.2 Repair history — `engine/evaluate.py::_JUDGE_HISTORY_ROWS` / `_JUDGE_ERROR_CHARS` · **a log tool is the wrong tool**

12 rows, each with a 300-char error (a chew of the already-500-char tail) and a 200-char claimed fix;
`ancestral_repair_chain` keeps 4 ancestors at 80 chars. The slice is real, and the code says so
itself — keeping the NEWEST rows "is the lossy direction for 'we already tried this'".

But this is a window over **events**, not over a file. A `read_log`/`metric_series` pair answers
nothing here. If this is widened it wants a *query over the durable repair ledger* — "have I tried
this before", "what did attempt 3 change" — which is a different tool over `events.jsonl` and
`spans.jsonl`, both of which have bounded readers already (`core/trace_files.py`,
`events/span_index.py`). Named, not converted, and deliberately not forced onto this tool's shape.

### 3.3 The Researcher's live-log cues — `engine/proposal_cues.py` · **no live log to read**

`_cue_failure_reflection` shows 3 failed nodes at 90 chars each; `_cue_watchdog_reflection` (via
`events/digest.py::watchdog_reflection`) shows 2 lifecycles at 120 chars. These are the tightest cuts
in the survey by ratio.

They are also **not** a slice of a log — they are one-line summaries of durable diagnostic rows,
spliced into a proposal prompt that runs when no eval is in flight. There is no live log, and the
node workdirs a proposal might want are an unbounded set rather than the one eval's plan. The
agentic Researcher can already reach the run through `RunTools`; what it lacks is the same thing
§3.1 lacks, and it is fixed in the same place (what `evaluate.py` persists), not here.

### 3.4 `RunTools.read_logs` — `tools/run_tools.py` · **the one to fix next, and it is the persistence fix**

Named explicitly because it looks like the counter-example: the repo *does* have a "read the logs"
tool. It serves `res.stdout[-500:]` and `n.error` (the same 500-char stderr) clipped to
`RESULT_CAP - 400`. So the one existing log query returns the same pre-chewed 500 bytes as the prompt
splice it was meant to escape. Widening it is worth doing and is the same work as §3.1.

---

## 4 · The boundary, stated once

`tools/log_tools.py` is bounded by rules a new case inherits, not by a table (`tools/dev_probe.py`'s
precedent, and doc 36's rejection of the allow-list shape):

1. **It names a LOG, never a path.** The `log` argument is matched against a `LogSource` map the
   CALLER supplies. No path is ever constructed from model input, so there is no `..` to reject and no
   symlink to resolve — the traversal question does not arise, one rung earlier than where
   `runtime/read_fence.py` would answer it.
2. **The map is the engine's own resolved stage plan.** `monitor_log_sources` over `eval_log_plan`:
   exactly the `<stage>.log` files this eval writes in its own workdir, with a filename the plan
   cannot attribute to a phase (`LOG_ROLE_AMBIGUOUS`) left out — the same rule `resolve_stage_log`
   already applies to what may be JUDGED, applied to what may be READ. That region is the one place on
   the filesystem that provably holds only what this node produced, and it is one
   `runtime/read_allowlist.py` already grants and `read_fence.fence_inputs` never fences.
3. **Every read is a bounded seek whose answer states its own bounds** — the byte range covered, and
   the call that continues past it. The ceiling is a parameter with a maximum, never a fixed
   truncation (`core/trace_files.py::iter_bounded_trace_jsonl_lines`'s discipline).
4. **It reads, and only reads.** Nothing to gate under engine invariant #3, so a call is an ordinary
   `tool` span like `run_probe`'s — not a domain event.

`LogSource.floor` carries the attempt boundary, and `train_monitor.attempt_byte_floor` was EXTRACTED
out of `read_training_tail_raw` rather than copied: one boundary, two readers. A role that could seek
past a floor the digest respects would read a dead attempt's curve as the live one's.

---

## 5 · The clock, because "the last 10 seconds" needs one

The real logs do not carry a timestamp per line. On v7 node 1 there are 22 absolute timestamps, **all
inside the first 34 KB**, followed by 3.5 hours of pure tqdm. So the series clock is the best available
LOWER BOUND from two independent sources, and it is monotone by construction:

* the log's own `YYYY-MM-DD HH:MM:SS` stamps, as a delta from the first seen; and
* the progress bars' elapsed field, tracked **per bar total**.

The per-lane part is the whole subtlety, and both directions were measured wrong first. Summing every
restart reported v7 node 1 as **12.5 hours** (real 3.5) because a nested concurrent dataloader bar
restarts constantly and finishes nothing. Ignoring restarts reported `rubertlite-dense-retrieval` node
36 as **108 seconds** (real ~1.2 h) because its per-epoch bar resets 327 times and each reset really
did finish an epoch. Keying the accumulator on the bar's `total` gets both right — 3.56 h and 1.15 h —
and the derived end-of-run wall clock lands within 8 minutes of the file's own mtime on a run that is
still appending. Every answer names which clock is carrying it.

---

## 6 · Cost, measured

Mean of 5, page-cache warm, on the two live v7 logs (5.9 MB and 15.7 MB):

| call | bytes touched | node 1 | node 0 |
|---|---|---:|---:|
| `read_log` tail/head (default) | 256 KiB | 2.4 ms | 2.6 ms |
| `read_log` search (default window) | 256 KiB | 3.4 ms | 3.3 ms |
| `metric_series` default, and `last_s=10` | 256 KiB | 14 ms | 13 ms |
| `metric_series` whole run, hourly | the whole file | 703 ms | 1,269 ms |

The default is a tail, so the common call is the cheap one — about what a monitor tick already pays to
build its digest. A whole-run scan is what the caller explicitly asked for and its receipt states the
bytes it cost. Scan time is dominated by the record split and regex over tqdm fill rather than by I/O.

The model-side cost is the real one: `_MONITOR_LOOK_TURNS = 6`, deliberately below
`trust/judge.py::JUDGE_MAX_TURNS = 15`, because this judge fires on a TIMER up to
`_MAX_MONITOR_LLM_CALLS = 200` times per node in a way the two one-shot verifiers never do.
`Settings.train_monitor_tools` is ON; `LEGACY_CONFIG_SNAPSHOT_DEFAULTS` pins it OFF so a resumed old
run gains no paid calls, and OFF reproduces the historical single completion byte for byte — the
`_LOOK_INVITATION` is spliced at the same position pattern as `stage_context`/`trajectory_text`.
