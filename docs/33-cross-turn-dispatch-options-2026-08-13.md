# 32 · Cross-turn eval dispatch — analysis and options (F1f)

**Status:** IMPLEMENTED 2026-08-13 — see §9 for what shipped, where it departed from §6, and what is
still open. §§0–8 are the original analysis, written 2026-08-13 against
`5b7010c7`, with `runs/rubertlite-dr-unified-v6` live on the box.

This is the deep read of backlog item
[F1f](29-operator-backlog-2026-08-11.md) — *"the eval batch is a barrier, so one slow node
idles a GPU for hours"*. It corrects the diagnosis, quantifies the cost against every run on
this box, and lays out the options.

---

## 0 · Executive summary

Three findings, in order of how much they change the decision.

1. **The diagnosis in F1f names the wrong code.** `Engine._dispatch_evals`'s task-group join is
   a real barrier, but **it is not the one v6 hit and never has been.** `run_started` for v6
   pins `card_driven_selection: true` and `speculation_depth: 2`, so `_speculation_enabled()`
   is true and every eval goes through `Engine._run_card_session`
   (`looplab/engine/speculation.py:2339`), which delegates to `_dispatch_evals` **only** when
   speculation is off. There are two different barriers in two different dispatchers.

2. **The continuous cross-turn dispatcher F1f asks to be built already exists** — it is the
   Card session. It admits from `state.pending_nodes()` (the whole board, not a per-turn
   batch), it refills a freed slot on the next poll, it runs its own producer, and it already
   commits `node_created` from the main task inside the dispatch loop. It is **switched off by
   two boolean latches**: `CardSession.consumer_completed`, set in the `finally` of *every* eval
   child, and `CardSession.yield_outer`. Either one makes `open_for_new_work()` false, which
   closes admission for **all** slots — and the session then cannot actually return to the outer
   loop until the **last** eval drains. So the run stops starting work at the *first* terminal
   and does not reach the outer boundary any sooner than it would have anyway.

3. **The cost is the largest single number in the backlog.** Across the six runs on this box
   that ran at width 2, **115.6 GPU-hours** were spent with a free slot while another eval was
   in flight — against **164.4 GPU-hours** of work actually done. **82.6 % of all the
   second-slot time that was available while the box was busy went unused.** The worst single
   window is `rubert-dr-0807`: 41.8 hours at occupancy 1 after having been at occupancy 2.

There is a **second, larger and separate** cost sitting next to it: **167.7 GPU-hours** of
"serial gap" — time when *no* eval was running at all because the loop was proposing and
building. In v6 that is the 15–37 minute hole between every pair of consecutive nodes. The two
have the same root (`yield_outer` latches the producer off during a long eval), but they are
distinct defects and a fix for one does not automatically fix the other. §5 covers it.

**Recommendation: Option 1 (hoist the eval task group to run scope).** Options 2 and 3 are
cheaper and honest; 4 is a misdiagnosis and is written up to close it out; 5–7 are mitigations
and are labelled as such.

---

## 1 · Which dispatcher runs, and when

`_run_with_llm_broker` (`engine/orchestrator.py:1452`) ends each turn with:

```python
if self._speculation_enabled():
    await self._run_card_session(evals, state, max_es, ...)
else:
    await self._dispatch_evals(evals, state, max_es)
```

`_speculation_enabled()` (`speculation.py:198`) requires all four of `card_driven_selection`,
`speculation_depth > 0`, `_speculation_gate_admitted`, and a non-empty gate receipt digest.
v6 has all four (`run_started`: `card_driven_selection=True`, `speculation_depth=2`,
`speculation_gate_receipt_digest=sha256:688d094c…`, `speculation_policy_scope=greedy`), and no
`speculation_depth_settled` row ever landed to ratchet it to 0. **v6 has never executed a line
of `_dispatch_evals`.**

### 1a · `_dispatch_evals` — the batch barrier (speculation OFF)

`orchestrator.py:3735`. Its parallel branch is **already** continuous *within a batch* — the
code says so in its own comment:

```
# G3 distributed/parallel eval: CONTINUOUS dispatch. A pool of `max_parallel` slots
# is kept FULL — the instant any eval finishes … the producer admits the NEXT queued eval …
# STILL A BARRIER: the inner task group joins the WHOLE batch before returning …
```

The batch is `evals`, produced by one `_select_actions` turn. In the non-Card path
`GreedyTree.next_actions` (`search/policy.py:249`) returns an evaluate action for **every**
pending node, so the batch is exactly the set of nodes the previous `creates` turn built — i.e.
whatever the `llm_parallel` build fan-out produced. Once `pending` drains, the group joins and
the loop can create the next batch. F1f's description ("the fast slot idles until the slowest
member of its own batch lands") is **exactly right for this path**. It also carries a bounded
aging rule (`_HEAD_BYPASS_LIMIT`, `orchestrator.py:175`) so a GPU-wide head is not starved by a
stream of small jobs — worth knowing, because any redesign has to keep that property.

### 1b · `_run_card_session` — the latch barrier (speculation ON, v6)

`speculation.py:2339`. A `while True` turn loop over six phases, each re-deriving its own
snapshot through `_fold_current`:

| phase | what it does |
|---|---|
| `_close_developer_sentinel_once` | crash-prefix cleanup |
| `_card_phase_serve_raw_stage` | commit a prepared raw proposal → `card_added`, elect it |
| `_card_phase_drop_stale` | freshness drain of the speculative prefix |
| `_card_phase_serve_head` | claim a finished producer result → `node_created` |
| `_card_phase_admit_evals` | **fill every free slot from `state.pending_nodes()`** |
| `_card_phase_request_build` | elect the next Card, or run the paid raw proposal lane |
| `_card_phase_decide_exit` | the one exit decision |

Note what `_card_phase_admit_evals` reads: `current.pending_nodes()` — **the folded board**, not
a per-turn list. This dispatcher is not batch-scoped at all. It is the producer/consumer design
F1f says "is not a small change and it is not mine to make unilaterally". It shipped in
`bb176cb9`/`8d9952a1` and was decomposed in `dde1ff49` (doc 25 EC-02).

---

## 2 · The real mechanism, verified

### 2a · The two latches

`CardSession.open_for_new_work` (`speculation.py:183`) is the single gate on both
`_card_phase_admit_evals` (outer entry) and `_card_phase_request_build`:

```python
return not (gates.stopping or self.consumer_completed or self.yield_outer)
```

`consumer_completed` is set in the `finally` of **every** eval child
(`_card_eval_one`, `speculation.py:1963`) — the code carries its own open TODO:

```python
# CODEX AGENT: this session-wide first-completion fence prevents the Card path from
# refilling a freed GPU while unrelated long-running siblings finish. Preserve the outer
# cadence boundary without turning one terminal child into head-of-line blocking for
# every remaining slot; add an unequal-duration refill regression.
session.consumer_completed = True
```

`yield_outer` is set by `_card_phase_serve_raw_stage` (nothing staged / permanent attach
refusal) and by `_card_phase_request_build` when `_request_card_build()` declines **and** the
raw lane yields no action.

And `_card_phase_decide_exit` (`speculation.py:2312`), when closing, returns
`not inflight` — where `inflight` includes `session.eval_inflight`. **So the session cannot
leave until the last eval terminates.** The latch does not bring the outer boundary any closer;
it only stops work from starting in the meantime. That asymmetry is the whole defect.

This is corroborated independently inside the suite. `tests/test_card_budget_refund.py:488`
already documents it as a *known* behaviour of the system:

> "**8 of 8** `stale` closes were `allow_commit=False` — the session's own gate, which goes shut
> the instant `consumer_completed` is set by the first eval terminal in the admitted batch."

### 2b · Bounded experiment (scratchpad, toy backend)

Harness: `_engine(depth=2)` from `tests/test_card_speculation_engine.py`, `_eval_parallel = 2`,
three ready Cards, `_evaluate` replaced by a sleeper — node 0 long (held open), node 1 short
(50 ms). Probe wrapped around `_card_phase_admit_evals`. Observation window: 3 s of wall clock,
~6 session poll ticks, with one slot free and the long sibling still burning.

```
 0.039  eval-start n0
 0.064  eval-start n1
 0.064  both slots busy: [0, 1]
 0.117  eval-end n1
 0.118  short terminated; {'requests': 3, 'nodes': 3, 'started': 2}
 3.121  after 3s idle slot; {'requests': 3, 'nodes': 3, 'started': 2}
 3.121  PROBE {'inflight': [(0, 0)], 'width': 2,
                'terminal': False, 'budget': False, 'outer_rebuild': False,
                'consumer_completed': True, 'yield_outer': True,
                'open_for_new_work': False,
                'admissible_pending': [2], 'pending': [0, 2]}
```

Read the probe line: **every fold-derived stop condition is false**, the width is 2, one slot is
free, and **node 2 is already built, pending, and judged admissible by the engine's own
`_session_admissible`** — and it is not started, because `open_for_new_work` is false. That is
the defect, isolated, with the inventory sitting right there.

Controls:

* neutralising **only** `consumer_completed` → still no refill (`yield_outer` was also set).
  Both latches must be reasoned about; fixing one is not enough.
* neutralising **both** → `open_for_new_work` becomes true, the session re-opens, and
  `_card_phase_drop_stale` immediately **discards node 2 as stale** (node 1's terminal moved
  `best`, so the card it was scored against superseded). That is correct behaviour, and it is
  worth stating plainly: **un-latching does not mean dispatching stale work** — the freshness
  machinery is downstream of the gate and still runs.

*Honest limit of the experiment.* The both-latches-off control does not demonstrate a
**successful** refill, because the toy harness had no fresh card left after the drain and its
raw lane is a stub that refuses to re-propose. It demonstrates that the gate re-opens and the
correct next decision (drop the stale prefetch) is taken. A refill demonstration needs a
harness with a live proposal lane; I did not build one.

Scripts: `<scratchpad>/exp/test_f1f_{barrier,v2,probe,both,release}.py`. Nothing was run inside
the repo or inside `runs/`.

### 2c · The live run, read against the mechanism

`runs/rubertlite-dr-unified-v6`, 08-12 15:17:48 → 08-13 08:36:13 (17.31 h), width 2:

```
16:07:20 -> 20:07:29   4.002h  occ=1  free=1     node 0
20:07:29 -> 20:22:35   0.252h  occ=0  free=2     <- serial gap (build)
20:22:35 -> 21:45:38   1.384h  occ=1  free=1     node 1
21:45:38 -> 22:07:51   0.370h  occ=0  free=2
22:07:51 -> 23:38:23   1.509h  occ=1  free=1     node 2
23:38:23 -> 00:15:00   0.610h  occ=0  free=2
00:15:00 -> 01:41:01   1.434h  occ=1  free=1     node 3
01:41:01 -> 02:17:28   0.607h  occ=0  free=2
02:17:28 -> 03:45:54   1.474h  occ=1  free=1     node 4
03:45:54 -> 04:01:08   0.254h  occ=0  free=2
04:01:08 -> 04:46:13   0.751h  occ=1  free=1     node 5 alone
04:46:13 -> 06:19:23   1.553h  occ=2  free=0     <- the ONLY 2-wide window in the whole run
06:19:23 -> 08:36:13   2.281h  occ=1  free=1     <- the window F1f reports, still open
```

The one 2-wide window is itself instructive. Node 5's `node_created` is at 04:01:08; a
`card_added` + `card_build_requested card-7` follows **three seconds later** at 04:01:11, and
node 6 is created at 04:46:12 and admitted at 04:46:13. So nodes 5 and 6 were **not** "dispatched
from the same turn" as F1f states — node 6 was produced by the session's own raw proposal lane
and prefetch *while node 5 was already training*, then admitted by
`_card_phase_admit_evals` into the second slot. **The continuous dispatcher demonstrably works
when the latches happen not to be set.** That is the strongest evidence that the fix belongs at
the latch and not in a rewrite.

Then node 6 terminates at 06:19:23 → its `finally` sets `consumer_completed = True` → the
session is sterile for every remaining slot, and cannot exit because node 5 is inflight. From
06:19:23 the log contains only `llm_usage`, `train_monitor_alert`, and four
`research_completed`/`hint` pairs (the repeating deep research in `bg_tg`, on its own timer —
F1f is right that this is why the run *looks* alive). No `card_build_requested`, no
`node_building`, no `card_added`. At 08:11 `card-3` folds to `selection_ready=True` with an
empty blocker list, so `_request_card_build` would very likely have succeeded — the phase that
calls it was gated off.

Node 5 then hit its declared 4-hour stage timeout: `train` span 04:22:32 +240 min,
`exit=-9 timeout` at 08:22:32, `triage` attempt 4 `reason=timeout` 08:22:34, `inline_repair`
attempt 4 at 08:24:05, new `train` at 08:26:10. The idle window is still open.

*Not verified:* whether `yield_outer` was **also** set before 06:19. It is the likeliest reason
no prefetch was requested during the 04:46→06:19 double-occupancy window (depth 2 allowed two
units of prefetch inventory and none was requested), but `yield_outer` is process-local and
leaves no row in the log. I could not confirm it from `events.jsonl` or `spans.jsonl`. It does
not change the conclusion for the 06:19→08:36 window, where `consumer_completed` alone is
sufficient.

---

## 3 · Why the latch exists — what the outer boundary owes

The latch is blunt, but it is not arbitrary. The session is a **narrow** executor: it can serve
Card builds, admit Card-owned pending nodes, drain stale speculation, and run one paid raw
proposal lane. Everything else in the run lives in `_run_with_llm_broker`, and none of it can
run while a session is open.

The session **does** observe, live, on every turn:

* operator pause / stop / finish — `_terminal_intent(state)` reads folded `state.paused` /
  `finished` / `stop_requested`, which the UI appends directly. **A stop is honoured.**
* the eval-second budget and the invocation wall deadline — `CardSession.budget_exhausted`.
* an operator `node_reset` crossing the propose/implement boundary — `needs_outer_rebuild`.
* the developer-crash sentinel, and the freshness drain.

The session **does not** run, and cannot:

* `_run_cadences` — coverage snapshot, concept coverage, run-base concepts, verifier tie-break,
  **the Strategist**, deep research, report refresh, hypothesis-board consolidation, lesson
  distill/refresh/reconcile. Several of these are node-count-paced (`engine/cadence.py::cadence_due`
  is a *since-last node-count* gate), so an endless session starves them **permanently**.
* `_ack_commands` — the durable acknowledgement of server command intents.
* `_apply_control_overrides` — live changes to `max_s` / `max_es`.
* `_serve_forced_requests` — operator forks, injects, confirms, ablations.
* `_settle_speculation_depth` / `_refresh_speculation_budget`.
* `CreationRunawayCounters.charge` and `systemic_failure_stop_reason`.
* `_select_actions`' non-Card `creates` branch, and `ablates`.

So the requirement the latch is (over-)serving is real: **the outer loop must remain
reachable.** What makes the current design cost 115 GPU-hours is that it enforces that by
*stopping the world* at the first terminal, while the boundary it is protecting does not
actually arrive until the *last* terminal.

**Every option below is a different answer to one question: how do you keep the outer boundary
reachable without draining the pipeline to get there?**

---

## 4 · What the status quo costs

Measured read-only over all 52 run directories with an `events.jsonl`. Occupancy is
reconstructed from `node_eval_started` → terminal where present, and from `evaluate` spans
otherwise (neither source is complete on its own: v4 emits no `node_eval_started` at all;
`evaluate` spans are written only at completion, so in-flight evals have none). Where both
exist they agree to <0.001 h on 5 of v6's 6 finished nodes. `eval_seconds` is **not** a valid
occupancy measure — it excludes repair/triage/score and is systematically short (v6 node 0:
3.888 h `eval_seconds` vs 4.002 h of slot held). `looplab timings` independently confirms the
v6 per-node totals.

Only **8** runs carry `eval_parallel` in `run_started`; `max_parallel` appears in no
`run_started` payload at all (it exists only in `config.snapshot.json`, where it is stale — every
snapshot says `max_parallel=1, eval_parallel=0` even for runs that really ran at 2). Five runs
declare 2. `rubertlite-dr-unified-v4` declares nothing but empirically ran two evals
concurrently for 9.73 h, and is included as inferred. A sweep over all 44 key-less runs found no
other run that ever exceeded occupancy 1.

| run | W | span h | evals | slot-h used | max occ | **barrier idle GPU-h** | % of W·span | % of free-slot-while-busy | serial gap GPU-h | util % |
|---|---|---|---|---|---|---|---|---|---|---|
| rubert-dr-0805 | 2 | 0.62 | 1 | 0.13 | 1 | 0.13 | 10.7 % | 100 % | 0.98 | 10.7 % |
| rubert-dr-0807 | 2 | 69.64 | 12 | 75.59 | 2 | **56.53** | 40.6 % | 85.6 % | 7.16 | 54.3 % |
| rubertlite-dr-unified-v2 | 2 | 42.67 | 8 | 23.15 | 2 | 16.04 | 18.8 % | 81.9 % | 46.14 | 27.1 % |
| rubertlite-dr-unified-v4 † | 2 | 87.01 | 10 | 46.18 | 2 | 26.71 | 15.3 % | 73.3 % | 101.13 | 26.5 % |
| rubertlite-dr-unified-v5 | 2 | 6.64 | 3 | 3.40 | **1** | 3.40 | 25.6 % | 100 % | 6.49 | 25.6 % |
| **rubertlite-dr-unified-v6** (live) | 2 | 17.31 | 7 | 15.94 | 2 | **12.84** | 37.1 % | 89.2 % | 5.84 | 46.1 % |
| **corpus total** | | 223.9 | 41 | **164.39** | | **115.64** | **25.8 %** | **82.6 %** | **167.74** | 36.7 % |

† v4's width is inferred from behaviour, not config, and its span is clipped to its active
window (its raw first→last is 567 h because seven stray `card_merged`/`workspace_changed` rows
were appended on 08-12 after a 528 h dormancy). Reject v4 and the declared-width-2 total is
**88.93 GPU-h barrier idle out of 273.75 available = 32.5 %**.

**The headline: barrier idle (115.6 GPU-h) is of the same order as all the work the box has ever
done at width 2 (164.4 GPU-h). 82.6 % of the second-slot time that was available while the box
was busy went unused.**

Longest windows where occupancy dropped below W and stayed there >10 min with ≥1 eval running:

| run | start | end | h | entered from full width |
|---|---|---|---|---|
| rubert-dr-0807 | 08-07 06:29:13 | 08-09 00:15:44 | **41.78** | yes |
| rubert-dr-0807 | 08-09 06:53:56 | 08-09 15:36:26 | 8.71 | yes |
| rubertlite-dr-unified-v2 | 08-11 20:41:46 | 08-12 05:55:22 | 9.23 | yes |
| rubertlite-dr-unified-v4 | 07-20 15:58:09 | 07-21 00:34:30 | 8.61 | no |
| rubertlite-dr-unified-v6 | 08-13 06:19:23 | *(open)* | 2.28+ | **yes** |

Two runs at width 2 **never ran two evals at once at all**: `rubert-dr-0805` (37 min, one eval —
too thin to be evidence) and `rubertlite-dr-unified-v5` (6.6 h, three evals, maximum
concurrency 1 for the entire run — half the box was never touched). v6 reached width 2 for
1.55 h out of 17.31.

One caveat that cuts the *other* way: barrier idle counts **slot** occupancy, not GPU
utilisation. A node in LLM triage/inline-repair holds its slot with the GPU idle (v6 node 5:
14.4 min, node 0: 7.7 min). True GPU waste is slightly **higher** than these figures.

---

## 5 · The other half — why speculation did not fill the window

F1f asks whether the fix belongs in the speculation lane. The direct answer is **no for the
barrier, yes for the serial gap**, and the two need separating.

**For the barrier (§2), the speculation lane is not the problem — the inventory was already
there.** In the experiment, `speculation_depth=2` had already built node 2 and it sat pending
and admissible while the slot stayed empty. The prefetch did its job; the consumer refused it.
"Let speculation dispatch its own eval" is therefore **not a smaller change with the same
effect** — it would mean giving the producer a second, ungated path around
`_card_phase_admit_evals`, i.e. a second admission authority that does not consult
`gates.stopping` or the resource reservation ladder. That is strictly worse than fixing the
gate: `_record_eval_start_boundary` would move off the dispatch decision, and invariant #1
records exactly what that costs (writing it from the eval worker made every prefetch election
lose its CAS — depth-1 speculation silently went serial, 17 builds / 5 discards became 12 / 0).

**For the serial gap (167.7 GPU-h), the speculation lane *is* the site.** v6's nodes 0→4 are
strictly serial with 15–37 minute holes between them, and during node 0's four-hour eval the
session requested **no** build at all. The reason is structural: new Cards are produced by
outer-loop cadences (`card_merged` / `card_enriched` land at 20:09:30, two minutes *after* node
0's terminal), so while a long eval is running the board frequently has nothing selectable;
`_request_card_build` declines, `speculative_raw_actions` returns `[]`
(`card_selection.py:1764` — it returns nothing when `selected` is non-empty *or* `fallback` is
empty), and `_card_phase_request_build` sets `yield_outer = True`. From that moment the session
is sterile for the rest of the eval, and the run pays the full build latency serially after the
terminal instead of hiding it behind the eval.

That is a genuine speculation-lane finding and it is worth its own backlog item. Median v6
build is ~28 min against 1.4–4.6 h evals, so `_ADAPTIVE_DEPTH_MIN_EVAL_FRACTION` (0.1) is
comfortably satisfied — the prefetch pays here, it just never fires. Options 1 and 2 both help
(they stop `yield_outer` from being permanent), but neither *causes* the board to be refilled
during a long eval; that needs the card-production cadences to be reachable mid-eval, which is
Option 1's other payoff.

---

## 6 · Options

### Option 1 — Hoist the eval task group to run scope ("adopting sessions") · **recommended**

**Design.** Today `session.task_group` is created inside `_run_card_session` and joined on
return. Move the eval children into a task group owned by the `run` spine, alive for the whole
run. `_run_card_session` returns when its *own* decision work is done — after a terminal, as
today — **without** waiting for evals it started. The outer loop turns: cadences, `_ack_commands`,
`_apply_control_overrides`, `_serve_forced_requests`, budget refresh, runaway charge. Then the
next session is entered and **adopts** the still-running evals.

**Turn structure.** The turn becomes what it already claims to be: a *decision* boundary, not a
*quiescence* boundary. `eval_inflight` stops being session-local and becomes engine-level. The
outer loop must gain one gate it does not have: `_select_actions`' `creates` branch and the
ablate branch must not run while evals are inflight (they assume a quiet log today), or must be
made tolerant. The finalization ladder (`_settle_terminal_gate`,
`_finish_with_report_if_quiescent`) must count inflight evals as in-flight work — it already
counts Card build heads, so the shape exists.

**Invariants.**

* **#1 (sole writer).** *No new exception needed.* Eval children are `anyio` tasks on the
  engine's own event loop, not background threads, and they already append every node terminal
  under `_write_lock`. Verified by AST rather than by eye, because it is the load-bearing claim
  of this option: all eight `store.append(EV_NODE_EVALUATED | EV_NODE_FAILED, …)` call sites in
  `engine/evaluate.py` (in `_evaluate` and `_record_superseded`) are lexically inside an
  `async with self._write_lock`. `node_created` is still committed
  by the main task in `_card_phase_serve_head`. `_record_eval_start_boundary` stays exactly
  where invariant #1 says to keep it — "on the main task, at the dispatch decision" — because
  the dispatch decision is still `_card_phase_admit_evals`. This is the single strongest
  argument for this option: the engine already runs evals concurrently with main-task appends
  and has done since `bb176cb9`; widening the *lifetime* of that task group changes no writer.
* **`_proposal_authority_seq`.** This is the real cost, and it must be stated. Today the outer
  loop's `_handle_create_actions` → `_prepare_node_idea` runs with **no eval in flight**, so its
  equality fence over a stable window is guaranteed. Under Option 1 a `node_evaluated` (FOLDED,
  *not* diagnostic, therefore *not* excluded by `speculation.py:398`) can land inside that
  window and discard a paid Developer call. Two honest answers: (a) accept it — the session's
  own raw lane **already** runs a paid proposal concurrently with evals and its docstring says
  it "may safely decline a stale proposal if an eval changes the search state during the paid
  call", so this is a lane that already exists and is already calibrated; (b) refuse it — keep
  the outer `creates` branch gated on eval quiescence, which costs nothing here because in Card
  mode the `creates` branch is a fallback the Card selector rarely reaches. **Take (b) first.**
  Do not widen the fence's exclusion set to cover `node_evaluated`: a node terminal genuinely
  *does* carry selection authority, which is precisely what the fence is for.
* **#3 (side effects gated on events).** Crash/resume is the load-bearing half. On resume the
  adopted set must be rebuilt from `EV_NODE_EVAL_STARTED`, which exists for exactly this reason
  (see the comment at `speculation.py`'s `_record_eval_start_boundary` call site: "a process that
  resumed after a kill starts with an empty one and cannot tell a prefetch that never ran from
  one whose sandbox burned GPU minutes; this row can"). `_drop_stale_speculation` already reads
  that durable boundary. The resume path is therefore *already written*; it needs to become the
  in-process path too.
* **#4 (fold, never cached state).** Unchanged — `_fold_current` already re-folds on any tail
  move, from any writer.

**Cost to build.** The largest of the options. Realistically: move the task group and its
lifetime; promote `eval_inflight` to engine state with a durable rebuild; teach
`_settle_terminal_gate` / `_finish_with_report_if_quiescent` / `finalize_run` about inflight
evals; gate the `creates`/`ablate` branches on quiescence; decide the cancellation semantics on
pause/stop (today the session's join gives you "let them finish"); rework the `bg_tg` research
lifetime, which is currently cancelled in `_dispatch_evals`/`_run_card_session`'s `finally`.
Plus the width-settling interaction: a live Strategist `eval_parallel` change would now land
while evals run.

**How it is tested.** Tier 1, driving the property, per CLAUDE.md's ladder — a source pin is
worthless here. The regression the code's own TODO asks for: *"add an unequal-duration refill
regression."* Concretely: a real engine, two evals of 10× unequal duration, assert the short
one's slot is refilled with a third node **before** the long one terminates, and assert the
Strategist/cadence at_node gate fired in between. Plus a kill-and-resume test that adopts an
eval from `node_eval_started` with no in-memory state, and a replay-idempotence test over the
resulting log.

**How it fails.** (a) A cadence that assumed quiescence now sees a moving log and mis-gates —
mitigated by the fact that every cadence is already at_node-idempotent
(`search/coverage.py::already_covered_at`). (b) Finalization races a running eval and finishes
the run over live work — this is the dangerous one, and it is why the quiescence ladder must be
extended in the *same* change, not after. (c) The paid-proposal fence loses proposals if answer
(a) above is taken instead of (b).

---

### Option 2 — Serve the outer boundary *inside* the session ("inline settlement")

**Design.** Keep one task group and the join. Replace `consumer_completed`'s meaning: on a
terminal, instead of closing the session, run a bounded **inter-experiment settlement** inside
the session — `_ack_commands`, `_apply_control_overrides`, `_refresh_speculation_budget`,
`_settle_speculation_depth`, `runaway.charge`, `systemic_failure_stop_reason`, and the
node-count-paced `_run_cadences` — then clear the flag and keep admitting. `yield_outer` keeps
its current meaning but becomes **one-shot**: it forces exactly one settlement pass and is then
cleared, rather than latching for the life of the session.

**Turn structure.** The outer loop becomes almost vestigial in Card mode; the session is the
loop. That is honest about what has actually happened to this codebase.

**Invariants.** Lighter than Option 1 in one respect and heavier in another. Lighter: nothing
moves, evals are still joined by the session, finalization still runs over a quiet log.
Heavier: **the cadences now execute with evals in flight**, which is the same
`_proposal_authority_seq` exposure as Option 1 plus the Strategist's `_apply_strategy`
(which can change `eval_parallel` live — `engine/widths.py`'s settling rule) landing mid-batch.
Invariant #1 is untouched (all of this is main-task work). Invariant #3 is untouched (every
cadence is already event-gated and at_node-idempotent — that is what makes this option possible
at all).

**Cost to build.** Medium. No lifetime surgery. The work is auditing each cadence for
"is it safe with a moving log?", and the answer is *mostly yes by construction*. The real work
is the settlement's ordering contract and one new bounded-recursion risk: a cadence that appends
makes `_fold_current` re-fold, which is fine, but a cadence that *creates a node* would re-enter
admission from inside a settlement.

**How it is tested.** The same unequal-duration refill regression as Option 1, plus a test that
the Strategist's at_node cadence actually fires between two evals of one session (today it
provably cannot), plus a test that a live width change applied mid-session settles through
`widths.py` rather than being clamped.

**How it fails.** The outer loop stops being exercised in Card mode, so its gates rot; a bug
that only the outer loop catches (systemic failure stop, runaway guard) now has two
implementations. If the settlement's list drifts from `_run_with_llm_broker`'s, you get the
classic "two copies of the same rule that must keep agreeing" — the exact failure
`CardSessionGates`' docstring was written to prevent.

---

### Option 3 — Bounded latch: replace the boolean with a budget

**Design.** The smallest change that is not purely a mitigation. `consumer_completed: bool`
becomes `terminals_since_entry: int`, and `open_for_new_work` closes when
`terminals_since_entry >= session_terminal_budget` (a new setting; **default 1 reproduces today's
behaviour byte-identically**). Likewise `yield_outer` gains a bounded retry rather than latching.
With the budget at, say, 4, a session admits through up to four terminals before handing back.

**Turn structure.** Unchanged in shape. The cadence starvation risk is *bounded* rather than
eliminated: the outer loop is reached after at most `budget` terminals, but still not until the
last eval of that session drains — so a session that keeps admitting can still hold a very long
tail. In the v6 shape (one 6 h node, several 1.5 h nodes) a budget of 4 would have filled the
06:19 window and the outer loop would have been reached at node 5's terminal either way.

**Invariants.** Identical exposure to Option 1's, minus the lifetime change: cadences still do
not run mid-session, so `_proposal_authority_seq`'s window stays quiet, and finalization still
runs over a joined group. This is the option with the **smallest invariant surface** of the
three real fixes.

**Cost to build.** Small — a counter, a setting, a docs row, a diagram number. Days, not weeks.

**How it is tested.** The unequal-duration refill regression, parameterised over the budget; a
byte-identity test that budget=1 produces the same event log as today (the codebase already
uses this idiom for `llm_parallel=1`).

**How it fails.** It trades one starvation for another: raise the budget and the Strategist /
report / lessons cadences fire later and later; leave it low and the barrier comes back. It has
**no principled setting** — the right value depends on the ratio of eval durations, which is
exactly what nobody knows in advance. And it does nothing for the serial gap (§5), which is the
larger number.

---

### Option 4 — Fix it in the speculation lane instead · **not viable as stated**

Written up to close it out, because F1f explicitly asks. **The prefetch is not the missing
piece — §2b shows a prefetched, committed, admissible node sitting unstarted next to a free
slot.** Giving speculation its own dispatch path means a second admission authority that does
not consult `gates.stopping`, does not go through `_try_reserve_node_resources`/the GPU pool
ladder, and moves `_record_eval_start_boundary` off the main-task dispatch decision — the change
whose measured cost is in invariant #1 (17 builds / 5 discards → 12 / 0). It is a larger change
than Option 3 and a worse one than Option 1.

**What is real in this direction** is the §5 serial-gap finding: `yield_outer` latching because
the board is empty mid-eval. That is a speculation-lane defect and deserves its own item —
"the Card board is only refilled by outer-loop cadences, so there is nothing to prefetch during
a long eval". Options 1 and 2 make it *possible* to fix; neither fixes it by itself.

---

### Option 5 — Refilling per-turn batch in `_dispatch_evals` · **wrong target**

F1f's own suggested shape: keep the join, top up `pending` with newly-selectable cards while the
group is still open. Three problems. (a) It is in the dispatcher **v6 does not use** — and it
would change little of the measured 115 GPU-h, because all five runs that *declare* width 2
(`rubert-dr-0805`, `rubert-dr-0807`, v2, v5, v6) also declare `card_driven_selection: true` with
`speculation_depth: 2`, i.e. 88.93 of the 115.64 GPU-h is in the Card dispatcher. Only
`rubertlite-dr-unified-v4` (07-19, inferred width, 26.71 GPU-h) declares neither key, so its
dispatcher is genuinely unknown and predates these settings. (b) Topping up `pending` requires calling `_select_actions` and
then *creating* nodes from inside the open group, which is the invariant-#1 question F1f
correctly flags — and unlike Option 1, here it would be genuinely new, because `_dispatch_evals`
has no main-task commit phase to hang it on. (c) It duplicates, in the legacy dispatcher, the
producer/consumer design the Card session already has.

Worth doing **only** if the speculation-off path is going to remain a supported production
configuration. It is not what any run on this box uses.

---

### Option 6 — Bound the damage (mitigations, no design change)

**These are mitigations. They reduce the number; none removes the barrier.**

* **Tighter `eval_timeout` / stage timeouts.** Caps the worst-case idle at the timeout. v6's
  node 5 declared 4 h and is on its fourth repair attempt at that ceiling — the cap worked and
  the run still idled a GPU for 2 h. Cheap, available today, no code.
* **Cost-homogeneous batching.** Refuse to co-schedule a node whose predicted cost differs from
  its sibling's by more than some factor. There is machinery for the prediction
  (`search/foresight.py`, `search/proxy.py`, the surrogate), so this is not fantasy — but it
  *reduces* utilisation by construction (it leaves slots empty on purpose to avoid leaving them
  empty later), and it fails exactly when the prediction is wrong, which is the v6 case: node 5
  was proposed at batch size 8192 and ran at 256 after three repair rounds, turning a ~1.5 h
  training into ~6 h. Nothing could have predicted that at admission.
* **Refuse to co-schedule a long node with a short one.** Same as above, stated as a rule.
  Strictly worse than the barrier in the common case where durations are similar.

The first is worth doing today regardless of which option is chosen. The other two should not be
built.

---

### Option 7 — Do nothing; set `eval_parallel: 1`

**What it buys.** Honesty. The run stops advertising a width it cannot sustain, the GPU pool
lease stops being held for a second device that is never used, and the operator's mental model
matches the log. On the corpus this costs almost nothing that is not already being lost: total
utilisation across the six width-2 runs is **36.7 %**, and two of the six never achieved
concurrency 2 at all.

**What is lost.** The 1.55 h that v6 *did* run two-wide, and the 41.8 h window in `rubert-dr-0807`
represents capacity that a working dispatcher would have used. More seriously: it forecloses the
`speculation_depth`/prefetch design, whose whole justification is overlapping the Developer's
provider latency with a running evaluation — at width 1 with a serial consumer, the calibrated
speculation envelope (`engine/speculation_gate.py`) is buying much less than it was calibrated
to buy. And it makes the second H200 dead weight, which on this box is the entire question.

Recorded as the honest floor, not as a recommendation.

---

## 7 · Ranking

| | option | fixes barrier | fixes serial gap | invariant surface | build cost |
|---|---|---|---|---|---|
| **1** | hoist eval group to run scope | **yes** | enables | largest (but **no new #1 exception**) | large |
| **2** | inline settlement in the session | **yes** | enables | medium; two copies of the boundary | medium |
| **3** | bounded latch (terminal budget) | partly, unprincipled | no | **smallest** | small |
| 4 | speculation dispatches its own eval | no | — | bad (#1) | medium |
| 5 | refilling batch in `_dispatch_evals` | not for any real run | no | new #1 question | medium |
| 6 | mitigations | no | no | none | ~none |
| 7 | `eval_parallel: 1` | n/a (removes the width) | no | none | none |

**Recommend Option 1.** The reasons, in order:

1. The dispatcher already exists and is already calibrated; this is a **lifetime** change to an
   existing design, not a new concurrency design. §2c proves the machinery works when the
   latches are not set — nodes 5 and 6 really did overlap under exactly this code path.
2. It requires **no new exception to invariant #1**, which is the constraint F1f was most
   worried about. Eval children are already engine-loop tasks appending under `_write_lock`;
   node creation stays on the main task at `_card_phase_serve_head`;
   `_record_eval_start_boundary` stays at the dispatch decision where the invariant text says to
   keep it.
3. The resume story is already written. `EV_NODE_EVAL_STARTED` exists specifically so a
   restarted process can tell an unrun prefetch from one that burned GPU minutes, and
   `_drop_stale_speculation` already reads it.
4. It is the only option that also makes §5's 167.7 GPU-h serial gap **addressable**, by letting
   card-producing cadences run while an eval is in flight.

**If Option 1 is too much to take on now, take Option 3 as a deliberate stopgap** — it is a
counter and a setting, it defaults to today's behaviour byte-identically, and it converts an
unbounded idle into a bounded one. Do not take it as the answer: it has no principled setting
and does nothing for the larger number.

**Do first, regardless:** the regression the code's own TODO asks for — *"add an unequal-duration
refill regression"* — written as a tier-1 test that drives a real engine and observes the effect.
Right now nothing in an 8,900-test suite fails when the second GPU goes dark for two hours, and
that is the reason this survived to be found by an operator watching `nvidia-smi`.

---

## 8 · What I could not verify

* Whether `yield_outer` was set in v6 before 06:19. It is process-local and leaves no row. It is
  the most likely explanation for the missing prefetch during the 04:46→06:19 double-occupancy
  window, but I could not confirm it from `events.jsonl` or `spans.jsonl`. The 06:19→08:36
  window is fully explained by `consumer_completed` alone.
* A **successful** refill after un-latching (§2b). The control shows the gate re-opening and the
  correct stale-drop being taken; the toy harness had no fresh work left to admit.
* `rubertlite-dr-unified-v4`'s width is inferred from behaviour (two evals 2 s apart, 9.73 h at
  occupancy 2), not read from config. All headline figures are given both with and without it.
* Whether the outer-loop cadences are *in fact* safe against a moving log. I checked that they
  are at_node-idempotent and event-gated, which is the structural argument; I did not audit all
  eleven of them individually. Options 1 and 2 both require that audit as part of the work.
* Nothing here was run inside the repo or inside `runs/`. All experiments are in the scratchpad
  against a toy task; all run-corpus reads were read-only.

---

## 9 · What shipped (2026-08-13)

**Option 1, with one departure from §6's recommendation and one correction to its cost table.**
Status of this document is no longer "analysis only": §§0–8 stand as written except where this
section says otherwise. The backlog rows are
[F1f](29-operator-backlog-2026-08-11.md#f1f-the-eval-batch-is-a-barrier-so-one-slow-node-idles-a-gpu-for-hours)
and its new sibling
[F1g](29-operator-backlog-2026-08-11.md#f1g-yield_outer-sterilizes-the-run-mid-eval-so-no-eval-runs-for-1677-gpu-h).

**What landed.**

* The eval task group is owned by `Engine.run` (§6 Option 1's "hoist to run scope"). It is opened
  around `_run_with_llm_broker` rather than around its turn loop — the same lifetime, without
  re-indenting ~300 lines — and `_run_with_llm_broker` drains adopted evals itself immediately before
  `finalize_run`, so the group's join is a backstop rather than the quiescence rule.
* `CardSession.open_for_new_work` split into `open_for_admission` (the fold-derived stop conditions
  only) and `open_for_production` (those plus the two live flags). `consumer_completed` was renamed
  `boundary_owed`, which is what it always meant.
* `eval_inflight` is engine-level (`Engine._eval_inflight`) and is the object every `CardSession` is
  handed, so adoption needs no handover step. The eval child no longer holds a session reference at
  all: it publishes its terminal debt (`_eval_boundary_owed`) and its wake-up (`_eval_notify`)
  engine-level, because it can outlive the session that admitted it.
* `_refuse_finish_over_adopted_evals` + `_drain_adopted_evals` are the extended quiescence ladder
  §6 said had to land in the same change. `_settle_speculation_depth`'s quiescence check and the
  outer loop's `_drop_stale_speculation` both learned about `_eval_inflight` too — the latter would
  otherwise terminalize a node whose sandbox is burning GPU minutes right now.
* `Engine.run` unwraps the task group's lone exception. anyio collapses even a single exception into
  a `BaseExceptionGroup`, and `Engine.run`'s failure TYPE is a contract (`_RefusalBoundaryGroup`
  prints an `OperatorRefusal` as one line at exit 2). This was not anticipated anywhere in §6 and it
  broke ~90 tests before it was fixed; note it as a real cost of the option.

**The departure: §6's answer (b) to the `_proposal_authority_seq` exposure is wrong, and no gate was
added.** §6 recommended keeping the outer `creates` branch gated on eval quiescence, "which costs
nothing here because in Card mode the `creates` branch is a fallback the Card selector rarely
reaches". That is not what the branch contains. `_stage_card_creates` — the ONLY writer of Card
INVENTORY — lives in it. Gating it on quiescence would make inventory unmintable for the whole
duration of every evaluation, which is §5's 167.7 GPU-h serial gap restated one lane over, i.e. the
larger of the two numbers this document measures. §6's own §5 says Option 1's payoff is that
card-producing work becomes reachable mid-eval; answer (b) would have foreclosed exactly that.

**The correction: the exposure §6 priced is not there.** Every `_reserve_node_build` call site — the
parallel-build reservation comprehension, `_prepare_node_idea`, `_create_node_scoped`,
`_build_refine_block_child` — is reached from SYNCHRONOUS main-task code, and every node terminal is
appended from an anyio task on the same event loop. A terminal cannot interleave with the fence's
window, so the window is quiet BY CONSTRUCTION rather than by waiting. (The fence is also narrower
than §6 implies: it is captured inside `_reserve_node_build`, *after* the paid Developer call, and it
only compares across CAS retries.)

The one writer that can interleave is an eval WORKER THREAD, and the only folded rows it writes are
`SETUP_THREAD_APPENDABLE` (`run_setup_open`/`run_setup_done`, once per run). Those are now excluded
from `_proposal_authority_seq`. That is admissible on evidence and is **not** a precedent for
`node_evaluated`: they are the only folded pair in the repo whose splice-position neutrality has
actually been proven (`tests/test_setup_thread_appendable.py`), and the fold keys them purely by
command. A node terminal moves `best`, the parent snapshot and every Card score — §6 is right that it
carries selection authority, and it stays in the fence.

**Prefetch economics, measured rather than argued.** The one thing a change to this gate could
silently cost is the prefetch's own yield — invariant #1 records what that looks like (depth-1
speculation going serial, 17 builds / 5 discards becoming 12 / 0). Ten real `Engine.run`s per tree on
the toy quadratic, `max_nodes=12`, `speculation_depth=1`, `role_factory=task.build_roles` (the
harness `test_card_budget_refund.py`'s end-to-end test uses), reading
`speculation_quality.speculation_budget_observation`:

| | requested | committed | stale | discarded | refunded | charged_discards | evaluated |
|---|---|---|---|---|---|---|---|
| master (`4ddd0a0b`) | 142 | 139 | 3 | 19 | 19 | 0 | 120 |
| this change | 141 | **140** | **1** | 20 | 20 | 0 | 120 |

Equal experiments, one more commit, and two fewer `stale` closes — a `stale` close is a prefetch
abandoned before it was ever committed, i.e. a Developer call bought and thrown away, and it is the
disposition this document's §2a latch produced ("8 of 8 `stale` closes were `allow_commit=False`").
Every discard is still refunded and `charged_discards` is still 0 on both. The offline smoke
(`looplab run --backend toy`) produces an identical event-type inventory and the identical champion on
both trees.

**Left undone.** §5's serial gap is only made *addressable*, exactly as §7's table promised — the
board can now be refilled mid-eval, but nothing yet makes the card-producing cadences fire *because*
an evaluation is running. They stay node-count-paced. F1g's last paragraph states what closing the
rest of that 167.7 GPU-h would take. §8's open items are unchanged: the both-latches-off control
still has no live-proposal-lane demonstration in the scratchpad, and the eleven outer-loop cadences
were not individually audited for safety against a moving log — the structural argument
(at_node-idempotent and event-gated) is what this change rests on.
