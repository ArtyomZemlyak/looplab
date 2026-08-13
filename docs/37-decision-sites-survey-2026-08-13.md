# Decision sites — where a fixed rule decides something context could reverse

**A survey with a qualifying verdict per site, ranked by MEASURED cost.** The principle it applies is
`docs/36-agent-driven-decisions-2026-08-13.md`; the test applied to every row is doc 36's table and
nothing else:

> does this decision determine what happens **NEXT** (recoverable, re-checkable — an agent may decide
> it) or what goes into the **RECORD** (the metric, the champion, selectability, whether a violation
> stands — deterministic rungs over AUTHENTICATED evidence, never a model)?

Two rows were converted (§2). The rest are ranked with their verdicts (§3), and §4 is the other half
of the answer: the sites that must **stay** deterministic, and why. Getting this wrong in the
permissive direction is the expensive error — it is how a system starts grading its own homework.

---

## 1 · The measurement, and what it reversed

Everything below is derived from the shipped corpus (`runs/`, read-only): 86 `events.jsonl` files,
**26,199 events** — note that 260 of those lines are batch rows (`type == ["__looplab_event_batch_v1__"]`
with the real events nested under `data.events[]`), and a scan that ignores them misses ~1,100 events.

The ranking metric is **compute-seconds discarded**, taken from `stage_finished.seconds` grouped by
`status`. That is the honest denominator: it is what each terminal decision actually threw away.

| `stage_finished.status` | stages | hours |
|---|---:|---:|
| `ok` | 184 | **109.23** |
| `check_failed` — the LLM inter-stage checker said something | 21 | **46.57** |
| `timeout` — the wall-clock deadline | 4 | **22.00** |
| `fail` — a real non-zero exit | 50 | 2.05 |
| `expect_failed` — declared artifact absent | 1 | 1.42 |
| `reused` | 37 | 0.00 |
| `needs_failed` · `stalled` · `diverged` · `oom` | 0 | 0.00 |

**Two fixed rules discarded 68.6 GPU-hours — 63 % of all the compute that ever produced a usable
metric.** Everything else in the table put together is 3.5 hours.

### Three readings that measurement reversed

These are recorded because the repo's history says they will otherwise be repeated.

1. **`superseded` looked like the second-largest cost and is not.** Measuring node wall-clock
   (`node_created` → `node_failed`) attributed **73 hours** to it across 66 rows. Sixty-two of those
   carry `error: "superseded by Card freshness gate"`, `eval_seconds: 0.0`, `never_evaluated: true` —
   bookkeeping for nodes that never ran. The 73 hours were nodes sitting idle, not work destroyed.
   Node wall-clock is the wrong denominator; `stage_finished.seconds` is the right one.
2. **The OOM misclassification that motivates doc 36 has cost nothing measurable *yet*.** There are
   **zero** `oom`, `diverged` and `stalled` rows in the corpus, and zero `stalled` stages: the two
   watchdog verdicts were split out of `oom` only in `c862045c`, so the corpus predates the
   vocabulary. The incident is real and the fix is right; but a survey ranking by measured cost
   cannot rank this family above what is actually in the logs.
3. **The backlog's "anti-stuck counter, defeated once already by an error whose signature changed
   every attempt" names code that no longer exists.** That was `triage.py::_normalize_error_sig`,
   deleted on 2026-08-05 — `triage.py`'s own module docstring records the deletion and the 1,741-repair
   Cyrillic-symbol incident behind it. What still answers to the name is
   `agents/stuck.py::StuckDetector`, a different mechanism at a different layer, and it is **not**
   vulnerable to that failure: it compares canonical `(action, observation)` pairs, with no error-text
   normalization anywhere in it. This is the third backlog entry found to name the wrong function.

---

## 2 · Converted

### 2.1 · The inter-stage checker's verdict vocabulary — 46.57 h

**Was:** `engine/eval_stages.py` asked an LLM whether a stage physically succeeded and read the answer
with `return None if out.upper().startswith("OK") else out[:300]`. Any other string became
`check_failed`, which stops the pipeline and fails the node.

One string carried three different facts, and the rule mapped all three onto the most expensive one:

* a **physical failure** — a traceback, a NaN/inf loss, no checkpoint, a silent fallback to a stale
  model. Correct, and the reason the gate exists.
* a **quality judgement**, forbidden by the prompt since the incident where the checker failed the
  run's best model. It kept doing it: `rubertlite-dense-retrieval` node 21 was killed with
  *"validation recall (0.79) is below previous best (0.8491)"* — the banned comparison, verbatim, in
  the concern that ended the node. **The prompt rung is spent.**
* **"I cannot tell"** — *"No output provided for stage 'prep', cannot confirm success"*
  (`rubertlite-dr-unified-v4` nodes 1, 4 and 8). An absence of evidence is not evidence of failure,
  and a silent-but-successful data-prep stage produces the identical bytes. No wording separates them.

The two most expensive single stages in the whole corpus are here: `rubert-dr-0807` nodes 1 and 3,
**14.7 h and 14.9 h of training that exited 0** and were discarded on a sentence.

**Now:** the line is **mechanical, not descriptive**, the same shape `triage.py`'s engine-minted
verdicts use. The checker answers `OK` / `FAIL <kind>: …` / `INCONCLUSIVE: …`, and a stage dies only
when `<kind>` is a member of the closed `runtime/command_eval.py::STAGE_CHECK_HARD_KINDS`. Everything
else — an out-of-enum kind, prose, a bare `FAIL`, an empty reply — coerces to `inconclusive`, is
recorded on the stage row under its own key (`check_inconclusive`, deliberately not `concern` — which means "this is why the stage FAILED" to the repair loop and to `metric_salvage`), and fails nothing.
`declared_condition_violated` is refused unless the stage actually declared an `expect.assert`: **a
checker may not invoke a contract that does not exist**, and an undeclared "condition" is exactly what
the previous-best comparison is from the inside.

**Verdict: NEXT.** Nothing in the record rests on it. The deterministic `expect.files` artifact
contract has already run and passed *before* the checker is consulted (that ordering is load-bearing
and predates this change); the metric still comes from the operator's own reader over the protected
`score` stage; `metric_salvage.VETO_STAGE_STATUSES` still refuses to salvage anything the checker
*did* condemn. A wrong `inconclusive` costs one more stage's runtime. A wrong `check_failed` cost
46.57 hours.

**Fail-open, and why that is not a weakening.** The direction of the coercion was chosen against the
measured asymmetry, not by taste. A checker that cannot be read is not evidence of anything, and the
things that keep the record honest are all downstream of it and all deterministic. Where a stage
declares no `expect.files` at all, continuing does mean running the next stage on an unverified
artifact — stated plainly because it is the real cost of the trade, and it is bounded by that stage's
runtime, not by a training's.

`tests/test_stage_check_verdict.py` drives both directions through a real `run_command_eval` — the
property is *did the next stage RUN*, which no source pin can see — and replays three corpus concerns
verbatim. The legacy bare-string `check_fn` keeps its gate byte-for-byte; downgrading those would
silently retire the gate for every unmigrated caller.

### 2.2 · The eval deadline — 22.00 h

**Was:** at `elapsed >= timeout`, `runtime/sandbox.py::_tee_drain` tree-kills unconditionally.

All four `timeout` stages in the corpus land **exactly** on their 4 h / 6 h / 8 h wall. The entire
captured record of `rubertlite-dense-retrieval` node 72 — a 12.45-hour node, five repair attempts,
6.00 h on its final one — ends:

```
97%|█████████▋| 644/664 [00:17<00:00, 37.88it/s]
99%|█████████▉| 660/664 [00:17<00:00, 38.13it/s]
100%|██████████| 664/664 [00:17<00:00, ...
```

At the wall, "two seconds from writing its checkpoint" and "will never finish" are the identical fact.
Only something reading the log separates them. Raising the budget is not the fix: it moves the same
cliff later and pays for every genuinely hung run on the way there.

**Now:** the deadline branch asks an optional judge once, and may grant a bounded, one-shot extension.

**Verdict: NEXT**, and it touches nothing in the record: the metric still comes from the operator's
reader, a graced stage that misses the extended wall reports `timed_out` exactly as before, and the
seconds bought are stamped on the stage row from the out-of-band `signals`, never read back out of the
log.

**What bounds it** — because the judge reads the *candidate's own live log*, which is precisely the
input that must not be trusted with anything expensive:

| bound | where |
|---|---|
| the reply is ONE WORD, never a number, and fail-**closed** | `eval_stages.py::parse_deadline_reply` |
| the seconds are the OPERATOR'S | `Settings.eval_deadline_grace_s` |
| clamped again inside the runtime (inf → cap; NaN, negatives, strings, a raising judge → 0) | `sandbox.py::_granted_grace` |
| asked AT MOST ONCE per command | `_tee_drain`'s `graced` latch |
| an operator `cancel` is NEVER graced | `_tee_drain`'s split branch |
| OFF by default | `eval_deadline_grace_s = 0.0` |

So a solution that prints a convincing progress bar buys `eval_deadline_grace_s` seconds, once, and no
other thing. **This widens the action space, not the trusted set** (doc 36 corollary 2).

Note the fail direction is the **opposite** of §2.1's, deliberately: there an unreadable answer *saves*
a node, here it would *spend*. "Unreadable" must resolve to whichever answer is cheap, and that is not
the same answer in both places. `runtime` may not call an LLM, so the judge arrives as a callback —
the same seam `command_eval`'s `check_fn` already uses.

---

## 3 · Ranked, not converted

| # | site | measured cost | qualifying verdict | why not now |
|---|---|---|---|---|
| 3 | inline-repair bound + `triage.py` verdict vocabulary | 2,345 repairs on one node in 210 min (`rubert-dr-0804`), all `rationale: "fallback: attempt repair"` against a 402-ing provider; 93.9 % of every repair in the corpus | **NEXT** for the three model verdicts (already agent-driven); the two engine-minted ones are not a judgement at all | **TAKEN** by the F5/F8 sibling, which is converting exactly this counter |
| 4 | `expect_failed` re-check — "the declaration is wrong and I can fix it here" | 1.42 h, 1 stage (`v6` node 3) | **NEXT** — and the `salvage_cause_fix` path already expresses it once per lifecycle | the F1e sibling owns the re-check that proves a repair reached the failed declaration |
| 5 | `needs_failed` — the input contract | 0 occurrences | **NEXT**; already carries the only directive in `crash_repair.py` that says "find which of the two declarations is wrong" rather than "debug this stage" | nothing measured; the mechanism shipped days ago |
| 6 | the STALL watchdog (`_MAX_STALL_S = 1800`, silence-not-progress) | **0** — one hit corpus-wide, in the synthetic `runs/live-stall-0804` | **NEXT** | zero measured cost, and it is *already* trivially reversible by the candidate: printing any line resets the clock. A judge cannot make it stricter and would only add latency to a path that has never fired in anger |
| 7 | `train_monitor_kill_confidence = 0.8` | 26 `broken` alerts; 17 at ≥ 0.8, 9 below — the threshold does discriminate | **NEXT** (it kills an eval) | `train_monitor_kill` was off in every corpus run, so the cost of a wrong answer is unmeasured. It is also a threshold over a number *the model writes about itself*; replacing it with a second judgement adds a rung, not information |
| 8 | `asha_live_kill_confidence`, `_ASHA_GRACE_TICKS = 2`, `_MAX_ASHA_JUDGE_CALLS = 20` | **0** — `asha_rank` does not appear once; ASHA ran as a policy in 2 of 69 `strategy_decision` rows | **NEXT** | no evidence exists to rank it by |
| 9 | `_MAX_DEP_ROUNDS = 6` | **has never bound**: observed `deps_installed.round` = 1 (×6), 2 (×3), 3 (×1) | mixed — the *round bound* is a budget (fine); the *"is this progressing?"* question is NEXT | the real defect here was a wasted round on pip's "Requirement already satisfied", and it was already fixed by the submodule probe, not by moving the counter |
| 10 | `agents/stuck.py::StuckDetector` (`stuck_repeat=4`, `stuck_alternate=4`) | 0 attributable | **NEXT** | see §1.3 — the backlog names deleted code; this mechanism has a different failure surface and no measured cost |
| 11 | `_TRIAGE_REASK_LIMIT = 1` | — | **not a candidate** | it bounds how many times the engine re-asks an unreadable judge. Its own comment argues the asymmetry from measurement; a judgement about how long to wait for a judgement is a regress |

---

## 4 · Must stay deterministic — and why

This half is as load-bearing as the conversions. Every row here is a place where reaching for an agent
would be the expensive mistake, because a wrong answer becomes **the result**.

* **`engine/metric_salvage.py`'s readers.** The stated rule, and the generalization doc 36 is built
  on: *the agent writes the training script and therefore writes the very text an extractor — or an
  LLM — would read.* A model here is a route around the protected `score` stage. Deterministic rungs
  only. **This is the sentence the whole survey is calibrated against.**
* **`triage.py::_failure_reason`.** Tempting, because it is a signature match and doc 36's founding
  incident is a misclassification. But its answer gates salvage and selection — it is a RECORD input.
  `c862045c` fixed it the right way: read the AUTHENTICATED out-of-band `signals`, never the stderr
  sentinel, which is mixed into the candidate's own output and is forgeable. Both conversions above
  preserve that property (the deadline grace reports through `signals`, not through the log text).
* **`runtime/command_eval.py::verify_stage_artifacts` (`expect.files`).** It runs *before* the LLM
  checker, unconditionally, and it is exactly what makes §2.1 safe to fail open. Making it agentic
  would remove the floor the conversion stands on.
* **`triage.py::coerce_triage_action`, and the minting of `unanswerable`.** "Nobody could read this"
  and "nobody was there" are different facts; the split is mechanical (`drive_tool_loop` raising vs
  returning) precisely so no model can assert either about itself.
* **`orchestrator.py::systemic_failure_stop_reason`.** Doc 36 F8 names it as the floor beneath a
  judgement-based repair bound. A floor that can be argued with is not a floor.
* **`triage.py::_holdout_indices`**, `search/speculation_quality.py`'s replicate invariants, and the
  metric readers behind `READER_PATH_KEYS`. Selection and receipt machinery: a changed derivation
  revokes issued receipts.
* **`engine/metric_salvage.py::SALVAGE_CAUSE_TRIAGE_ACTION`.** A marker, not a verdict, deliberately
  absent from `TRIAGE_ACTIONS`: no model may emit it and the coercion must never accept it.

The pattern across all of them: **authenticate the evidence, then let the agent read it.** Where the
evidence cannot be authenticated, the agent may still choose the ACTION — it may not supply the FACT.

---

## 5 · What a later reader should re-derive rather than trust

The hour figures are `stage_finished.seconds` grouped by `status`, over `runs/**/events.jsonl` with
batch rows unpacked. They will drift as the corpus grows, and two of the rows above (`needs_failed`,
`stalled`) are zero only because their vocabulary is days old. Re-run the grouping before re-ranking;
do not reason from the numbers in this table as if they were constants.
