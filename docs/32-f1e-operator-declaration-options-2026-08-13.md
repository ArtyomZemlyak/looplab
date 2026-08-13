# 32 · F1e is unreachable — who may repair a declaration, and what the promotion would be asserting

**Status: ANALYSIS AND OPTIONS. Nothing here is implemented.** Written 2026-08-13 against
`5b7010c7`, about `2b980d0b` (`feat(salvage): re-check a repaired artifact declaration instead of
salvaging blind`) and [backlog F1e](29-operator-backlog-2026-08-11.md#f1e-re-check-a-repaired-artifact-contract-instead-of-leaving-the-metric-salvaged).

The owner's two coupled questions:

* may the operator's appended `score` stage carry an operator-declared `expect`?
* who may repair an operator's declaration?

Short answers, argued below: **(1) letting `score` carry an `expect` does not make F1e reachable —
it makes the re-check logically vacuous, because a declaration nobody may rewrite is re-asked
against the same sentence that already failed. (2) The honest reachable case is not a promotion at
all: it is FINISHING the pipeline. (3) F1e is not quite dead code — it has exactly one live trigger,
and that trigger is a filesystem race, which is worse than dead.**

---

## 1. The impossibility, stated as a proof rather than a list of gates

The four gates (`looplab/engine/metric_salvage.py`, RE-CHECK section; bound in
`looplab/engine/evaluate.py::_recheck_repaired_contract`) are each individually defensible. What
makes them jointly unsatisfiable is not any one of them but a shape argument:

1. A promotion requires a **corrected** declaration. `declaration_only_repair` refuses an empty
   change set explicitly — "a pass would be the ORIGINAL contract passing on a second look".
2. The only writer inside a run is the Developer, and gate 2 restricts what it may have written to
   **exactly** `looplab_stages.json`.
3. `looplab_stages.json` is read only in Developer-manifest mode
   (`engine/eval_stages.py::_resolve_stages`), and only for the stages that PRECEDE the operator's
   command. The final stage is always the engine-appended `score`, built as
   `{name, command, timeout}` — no `expect`, so it cannot fail an artifact contract.
4. Gate 1 requires the contract failure to be on the **last** stage of the re-resolved chain.

(3) and (4) contradict each other in manifest mode. In operator `cmd.stages` mode (4) is
satisfiable, but the failing declaration lives in `task.snapshot.json`, which (2) forbids anyone
from having changed.

So the escape routes are enumerable, and there are only three:

* **relax (4)** — promote a failure that is not on the last stage;
* **break (2)/(3)** — let an agent-writable declaration govern the last stage, or let some other
  actor rewrite the operator's;
* **stop promoting** — replace the promotion with something else that uses the same evidence.

Everything in §5 is one of those three.

## 2. CORRECTION to "no live node is ever promoted": there is one live trigger, and it is a race

The commit says the two halves are mutually exclusive "in both of today's pipeline shapes". That is
true of the *intended* mechanism and false of the *implemented* one, because **gate 2 checks which
files the repair wrote, never that the write reached the declaration that failed.**

In operator `cmd.stages` mode:

* the last stage may declare `expect.files` (`validate_stages` is the SAME validator for
  `EvalSpec.stages` and for the Developer's manifest — `command_eval.py:942`, and `EvalSpec.stages`
  reserves nothing, "the operator owns scoring");
* when it fails its contract, `metric_salvage_repair` asks the Developer for a fix, and
  `SALVAGE_CAUSE_DIRECTIVE` names the file to fix: *"typically the path in `looplab_stages.json`
  (`expect.files`)"*. In this mode that file is ignored by the engine — but the Developer writes it,
  because that is what it was asked to do;
* `changed == ["looplab_stages.json"]` ⇒ gate 2 passes;
* `_resolved_stages` re-resolves to the **operator's unchanged chain**, so `recheckable_expect`
  returns the same wrong path, and `verify_stage_artifacts` is re-asked against the same declaration
  and the same floor;
* if the filesystem's answer has changed in the interim, the node is **promoted to MEASURED, with no
  violation, on a declaration nobody corrected.**

Measured, not argued — `scratchpad/f1e_probe.py`, a real `Engine` with operator-declared stages, a
Developer manifest written into the workdir, and the artifact appearing between the two checks:

```
original check   : stage 'train' exited 0 but did NOT produce its declared artifact 'out/unified-ba…
re-resolved chain: [('prep', None), ('train', ['out/unified-baseline/final/model.safetensors'])]
PROMOTED?        : True
provenance       : {"salvaged": false, "declaration_repaired": true, "producer": "operator_stage",
                    "expect_files": ["out/unified-baseline/final/model.safetensors"], …}
```

Note the `expect_files` on the provenance: it records the **still-wrong** path as the corrected
declaration. An operator reading that record is told a fix landed that did not.

**Is the race realistic?** The window between the original check and the re-check contains an entire
Developer LLM round trip (`_repair_salvaged_cause`), i.e. seconds to minutes — not microseconds.
Three of the four `verify_stage_artifacts` outcomes can flip across it:

| original outcome | can it flip to pass? | how |
|---|---|---|
| `EXPECT_MISSING` | yes | a detached writer/uploader still flushing; a FUSE/geesefs mount whose negative directory entry has not caught up. CLAUDE.md records 105–950 ms `lstat` for an absent file on this box's mount and a live run's own writes invalidating the listing |
| `EXPECT_EMPTY` (0 bytes) | yes | a large `save_pretrained`/`safetensors` write whose bytes land after the check |
| `EXPECT_STALE` | yes | anything that rewrites the file after the stage, moving `st_mtime` forward past the floor |
| `EXPECT_ESCAPES` | no | a path rule, not a filesystem answer |

So F1e's only live behaviour is: **a contract that failed for a reason nobody diagnosed is recorded
as satisfied because a `stat` answered differently the second time.** That is strictly worse than
unreachable code, and it holds under every option below that keeps gate 2 in its current form.

**How urgent is it?** Not very, today: **0 of the 88 runs on this box used operator `cmd.stages`
mode** (§4), so the path has never had a live run to fire in. It becomes reachable the first time an
operator declares a staged `cmd` with an `expect` on its last stage — which is a documented,
first-class shape (`EvalSpec.stages`, "the operator owns scoring"), not an exotic one. Fix it before
that happens, not after.

**The fix for that is independent of the owner's decision and should land either way.** Gate 2's
question is the wrong one. It should not be "which files did the repair write" but **"is the
declaration that is now being checked DIFFERENT from the one that failed"** — capture
`self._resolved_stages(node, workdir)` *before* `_repair_salvaged_cause` runs, and require the
failing stage's `expect.files` to have actually changed. That is a few lines, it is checkable, and
it makes "the sentence was corrected" true by construction instead of by inference from a filename.

> **REVIEW (mega-review 2026-08-13): this fix LANDED before this document did.** Commit `7e9a9a2`
> ("the F1e re-check must prove the repair reached the declaration that failed", an ancestor of the
> commit that landed this doc) shipped exactly the recommendation above:
> `evaluate.py::_recheck_repaired_contract` now calls
> `metric_salvage.declaration_actually_corrected(...)` before `verify_stage_artifacts`, so the §2
> promotion race described as live is CLOSED at HEAD, and §1's "four gates" are now five. The
> analysis is kept as written for its argument; do not re-diagnose or re-fix the race from this
> page.

## 3. Where the number actually comes from, in each shape

This is the question every option turns on, so it is worth being pedantic. `metric_salvage` reads
the operator's declared metric spec over **the failing stage's captured stdout** (the `expect_failed`
early return in `_run_stages` sets `stdout` to that stage's output and hard-codes `metric=None`).

| pipeline shape | failing stage | whose bytes the number came from | did the operator's scorer run? | `stage_producer` |
|---|---|---|---|---|
| manifest mode, `train` fails (v6 node 3) | agent's `train` | the agent's own training script's stdout | **no** — `merge` and the appended `score` never executed | `agent_stage` |
| manifest mode, appended `score` fails (needs §5-A) | operator's `score` | the operator's `cmd` stdout | yes | `operator_stage` |
| operator-stages mode, last stage fails | operator-declared stage | that stage's stdout — which by `EvalSpec`'s own contract is where the metric lives | yes (that stage IS the scorer) | `operator_stage` |
| operator-stages mode, a middle stage fails | operator-declared stage | a stage that is not the scorer | **no** | `operator_stage` |

Two things fall out of that table.

**`operator_stage` does not mean "the operator's code ran".** `stage_producer` answers "who declared
this stage", not "who wrote the file it executes". In operator-stages mode the operator writes
`command: ["python", "train.py"]` and the **agent** writes `train.py`. What actually makes the
scoring path operator-owned is a different mechanism entirely: `protect` /
`RepoTask._entrypoint_protect`, which freezes the FILE the eval command executes — and even that is
documented as leaving a residual hole (a scorer reading its checkpoint path from an editable config;
closed separately by `runtime/read_fence.py`). Any option that leans on `producer` should say
`operator_stage` means *the operator chose which command runs here*, and nothing more.

**All three artifact-contract failures in the corpus are row 1** (§4) — and row 1 is the case F1e was
written for, and the one where promoting is least defensible.
0.728113 on v6 node 3 is a number the agent's `train` script printed for a model that never went
through the `merge` step the node's whole idea was about, and never through the operator's scorer.
Promoting it files a train-only result under a node whose declared pipeline was train→merge→score
and puts it in the same comparison pool as node 4, which really did run all three. Gate 1 is right.

## 4. What the runs say about frequency

Re-derived from scratch over **88 `events.jsonl` files** (52 top-level run dirs + 36 nested under
`specgate*/seedN-depthM/`), 27,276 event lines, 0 malformed. Structural matching on JSON values, not
substrings.

| | count | where |
|---|---|---|
| `node_evaluated` events, all runs | 666 | — |
| …carrying `metric_provenance` at all | **1** | v6 node 3 |
| …with `metric_provenance.salvaged == true` | **1** | v6 node 3 |
| `stage_finished` rows with `status: expect_failed` | **3** | v5 node 0, v5 node 2, v6 node 3 |
| `node_repaired` with `triage_action: salvage_cause_fix` | **1** | v6 node 3 |
| …whose `changed == ["looplab_stages.json"]` exactly | **1 of 1** | v6 node 3 |
| runs using operator `cmd.stages` mode | **0** | — |
| runs in Developer-manifest mode (`eval.stages == []`) | 4 | v2, v4, v5, v6 |
| runs with `metric_salvage` configured at all | **1** | v6 (the only run with the key) |

The prior count of "3 artifact-contract failures" holds. Its per-operator breakdown does not: the
three are **v5 node 0, v5 node 2, and v6 node 3** — two in v5, one in v6, all three on a stage named
`train`, all three `exit_code: 0`.

**The decisive fact: in all three, the failing stage was the FIRST stage of the resolved pipeline,
never the last.** v5's manifests declare `[train]`, so the chain was `train → score`; v6 node 3
declares `[train, merge]`, so `train → merge → score`. No manifest anywhere in the corpus declares a
`score` stage (0 of 147 declared stages), confirming `score` is always engine-appended. For v6 node
3 no `merge` and no `score` `stage_finished` event was ever emitted — the pipeline aborted at `train`,
and `eval_seconds` 5120.02 ≈ the `train` stage's own 5118.47.

**So how often would F1e's shape ACTUALLY have fired?**

| option | historical firings out of 3 | note |
|---|---|---|
| as shipped | **0** | gate 1 refuses all three (failure on the first stage) |
| A (operator `expect` on `score`) | **0** | no run was in a mode where it changes anything; and see §5-A |
| B (relax gate 1) | **1** — v6 node 3 | the promotion §5-B measures: `agent_stage`, `feasible: True`, scorer never ran |
| B+C (relax gate 1, require `operator_stage`) | **0** | producer is `agent_stage` on all three |
| D (finish the pipeline) | **3** | all three are "the artifact was there, the declaration was wrong" |

Two caveats on those numbers, both in the honest direction:

* `metric_salvage` was only ever configured in **one run** (v6). v5's two identical failures became
  `node_failed / no_metric` and lost 4,568 and 4,601 GPU-seconds outright, because the salvage
  machinery did not exist yet. So the denominator for anything salvage-shaped is 1 run, 7 nodes —
  n=1, exactly as the F1e backlog entry itself corrected to.
* **No historical stage row carries `expect_since`**, because the `EXPECT_SINCE_KEY` writer shipped
  in `2b980d0b`, after every run in the corpus. Gate 3 fails closed with no floor, so even with gate
  1 relaxed, none of the three could be re-checked from their recorded rows. The base rate above is
  computed on the *shape*, which is the right question, but it is a shape rate and not a replay.

**v6 node 3 in detail, because it is the entire evidence base.** The declaration said
`…/meanmerge_nllcos_rubert-tiny-lite/final/model.safetensors`; the testbed composed
`…/meanmerge_nllcos_rubert-tiny-lite_rubert-tiny-lite/final/model.safetensors`, whose mtime is
`1786585052` — **172 seconds before** the contract check at `1786585224`, and comfortably after the
stage's own start (`≈1786580106`). Gates 2, 3 and 4 would all have passed on the corrected path;
only gate 1 refused. The Developer's fix changed exactly one file and got the path right.

Two things about that node that are not in the F1e write-up and matter here:

* the near-miss hint the engine printed named **`checkpoint-4236/model.safetensors`**, not the
  `final/` file. There are **four** fresh `model.safetensors` in that workdir (three `checkpoint-*`
  plus `final/`), and `_artifacts_written_elsewhere` returns them sorted, so the diagnostic showed
  the oldest. The Developer got the right answer from the *naming convention*, not from the hint.
* the salvaged 0.728113 fed two `lessons_distilled` entries and a `reward_hack_suspected`
  (`critic:params_ignored`) on the very next event. The `metric_salvaged` violation keeps it out of
  `feasible_nodes()`; it does **not** keep it out of the run's distilled knowledge.

## 5. The options

Each is stated as: what it makes possible · what it costs · what it risks · what would have to be
true for it to be safe.

### A. Let the operator declare `expect` on `cmd` / the appended `score` stage

**Makes possible.** An artifact contract on the *scoring* stage: "the scorer must have written
`score.log` / `preds.jsonl`, non-empty, this run". Today `score` is the one stage in a
Developer-manifest pipeline that is held to nothing but its exit code. Mechanically this is small —
`validate_stages` already accepts `expect` from `EvalSpec`, so it is a new `EvalSpec` field
(`expect`, or `score_expect`) threaded into `_resolve_stages`' `final = {...}` dict.

**Does it make F1e reachable? No, and the reason is worth stating precisely.** It satisfies gate 1
(the failure is now on the last stage) and gate 4, and it makes `producer == operator_stage`. It
cannot satisfy gate 2, because a corrected declaration requires a writer and there is none: the
Developer may not touch `task.snapshot.json`, and no engine path rewrites it. The re-check would be
re-asking an unchanged sentence — which is precisely the "ORIGINAL contract passing on a second
look" that `declaration_only_repair`'s empty-set clause exists to refuse. So under A, F1e's
promotion path stays exactly as reachable as it is today: only via the §2 race.

**Costs.** A new failure mode with no in-run repair. If the operator's `score` `expect` is wrong,
EVERY node in the run fails its contract, the Developer is asked to fix a declaration it cannot
reach, and each node burns a repair attempt discovering that. This is the same shape as
`PROTECTED_SCRIPT_MISSING` — a failure whose only fix is the operator's — and it deserves the same
treatment: a submit-time check and an error message that names the operator as the only actor who
can act.

**Risks.** Low, and in the useful direction: an `expect` can only ever make a node FAIL, never pass.
It cannot be used to make a failing node succeed.

**Safe if.** (i) It is validated at submit time against the same `_validate_expect`; (ii) the
resulting failure is classified as operator-fault and does NOT consume `inline_repair_attempts` or
buy a Developer call (reuse the `PROTECTED_SCRIPT_MISSING` precedent); (iii) it is documented as
*not* closing the read-side hole — `expect` checks what a stage writes and never what it reads, and
v6 node 4 passed its contract while scoring a human's checkpoint.

**Verdict: worth doing on its own merits, as a contract feature. It is not an answer to F1e.**

### B. Relax gate 1 — promote a failure that is not on the last stage

**Makes possible.** The v6 node 3 shape becomes promotable: manifest mode, `train` fails its
contract, the Developer corrects the manifest path, the artifact is there, the node becomes MEASURED.

**What it would then be asserting.** "One stage of this pipeline wrote what it declared" — presented
as "this node's metric is comparable to every other node's". Those are different claims. The stages
after the failure never ran, by construction: a contract failure returns early from `_run_stages`.

**Measured, on a throwaway copy of the tree** (`git archive HEAD` → scratchpad, gate 1's last-stage
clause relaxed to "every stage before the failing one ran ok/reused"), driving the real
`Engine._evaluate` over a real subprocess pipeline in the v6 node 3 shape:

```
terminal       : node_evaluated
metric         : 0.74325
provenance     : {"salvaged": false, "declaration_repaired": true, "producer": "agent_stage", …}
violations     : []
feasible       : True
stages that RAN: ['train']
```

`feasible: True`, no violation, the operator's `score` stage never executed, and the number is what
the **agent's own training script printed to stdout**. Two of the suite's end-to-end tests flip from
pass to fail under that one-line relaxation, which is the suite working as intended.

**Risks.** This is a complete, cheap, fully agent-controlled route around the protected scorer, and
it needs no cleverness to exploit — it is the *default* behaviour of an agent that declares a stage,
prints a number, and gets a path wrong. Write `print(f"RECALL@100: 0.999")`, declare `expect.files:
["out/model.bin"]`, write `out/model_typo.bin`, then "fix" the manifest to name any fresh non-empty
file the stage wrote. Champion. `looplab/trust/reward_hack.py` does not cover this (it is OFF by
default and scans for grader access, not for this shape).

**Safe if.** It is bolted to a producer rule (§C) — at which point, see §C, it stops being reachable
again. There is no version of "relax gate 1" that is both reachable and safe.

**Verdict: reject.**

### C. Split the promotion by PRODUCER

**Makes possible.** Nothing new by itself; it is a *constraint*, not an enabler. Adding
`producer == OPERATOR_PRODUCED` to `recheckable_salvage` refuses the agent-stage promotion §B opens.

**Measured** (same throwaway tree, relaxed gate 1 **plus** the producer requirement): the v6 node 3
shape goes straight back to `salvaged: true`, `violations: [metric_salvaged]`, `feasible: False`.

**What it collapses to.** In manifest mode `eval_spec["stages"]` is empty, so `stage_producer`
returns `operator_stage` only for the stage literally named `score` — which is the appended one,
which needs §A to carry an `expect`, whose declaration nobody may repair. In operator-stages mode
every stage is `operator_stage`, so the constraint adds nothing there and the "the scorer never ran"
problem of a middle-stage failure (§3, row 4) survives untouched. **So B+C is unreachable again, and
C alone changes no outcome.**

**Verdict: adopt as a REQUIREMENT on any option that promotes, never as an option in itself.** The
argument for it is exactly the one `SalvagedMetric.violation_rows` already makes for `select`, and
the asymmetry is the same: calling agent output "operator" lets an agent-printed number compete;
calling operator output "agent" costs only an exclusion.

### D. Finish the pipeline instead of promoting a partial number — RECOMMENDED

The re-check's evidence is better used as an **admission test for resuming the pipeline** than as a
promotion.

When the contract failure was a path typo, what we learn from re-asking the corrected declaration is
precisely: *the failing stage's declared outputs exist, are non-empty, and were written by this run
of this stage.* That is exactly the evidence needed to **skip that stage and run the ones after it**
— including the operator's protected `score`.

**Makes possible.** v6 node 3 runs `merge` and `score` for real. The metric comes from
`run_command_eval`'s primary read over the operator's scorer stdout, through the full tail
(constraints, extra readers, drift cross-check) — not from `salvage_gates`' re-asked approximation.
There is no promotion, no `metric_salvaged` violation to argue about, no `producer` question, and
nothing about the protected-scorer guarantee is touched.

**Costs.** The remaining stages' runtime — on v6 node 3, a weight merge plus the scorer, i.e.
minutes against the 76 GPU-minutes a full re-train costs. Not free, which is the honest trade: F1e
bought a number for zero, and the number was not worth what it claimed.

**What it needs.** Both halves already exist and are small:

* `_run_stages(start_stage=…)` already starts the pipeline at a named stage and marks the earlier
  ones `reused`. To *skip* the succeeded-but-misdeclared stage, pass the name of the stage AFTER it —
  `_run_from` is a plain index lookup, so nothing new is required in the runner.
* `_safe_reuse_start` currently refuses reuse across any `looplab_stages.json` change, with a stated
  reason: "A change to the stage MANIFEST rewrites the pipeline's argv (e.g. train hyperparams), so
  the completed checkpoint no longer matches the declared command". That reason is *checkable*
  rather than assumed: resolve the chain before and after the repair and require every stage's
  `command` and `timeout` to be byte-identical, with only `expect`/`needs` differing. A declaration-
  only manifest edit provably does not rewrite any argv.
* `_repair_forces_full_retrain` already returns False when `next_start` is set, so this does not
  consume `inline_repair_retrain_cap` — correctly, since it discards no completed work.

**Risks, and why they are small.** The Developer could WEAKEN the failing stage's `expect` (name
`train.log` instead of the checkpoint) to buy a resume. Under a promotion that would be fatal; here
it costs almost nothing, because the later stages still have to work against whatever is actually on
disk and the operator's scorer still runs and still produces the number. A weakened declaration that
does not correspond to reality shows up as a crashed `merge` or a scorer that finds no checkpoint —
i.e. as an ordinary failure, repaired the ordinary way.

**Safe if.** (i) The argv-identity check above is what licenses the skip, not the filename;
(ii) the skip is bounded — at most once per node, on its own durable event, so a resume cannot
re-enter it (invariant #3); (iii) the node's terminal records that a stage was skipped on the
strength of a re-checked declaration, for the same reason `declaration_repaired` is recorded today.

**Verdict: this is the shape that makes the honest case reachable.** It answers "who may repair an
operator's declaration?" with "nobody has to" — the correction is to the AGENT's manifest, and the
operator's measurement is then actually taken rather than inferred.

### E. Let the ENGINE correct the declaration, deterministically

**Makes possible.** Removes the agent from the repair loop for this failure class. The near-miss
scan `command_eval._artifacts_written_elsewhere` already finds every fresh same-basename file the
stage wrote, and `metric_salvage._relocated` already treats **exactly one** such file as an
unambiguous answer and **two as a refusal** ("a metric chosen by directory-walk order is a metric
that can differ between two reads of the same workdir"). The identical rule can correct an
`expect.files` entry: one fresh same-basename file inside the workdir ⇒ that is what the declaration
meant; zero or ≥2 ⇒ refuse.

**Costs.** A new engine-authored edit to an agent-authored file, which has to be recorded as such
(the node's manifest on disk would no longer be what the fold says the Developer wrote — the same
class of drift `_stamp_workdir`/`_workdir_matches` exists to police). Cheapest form: don't write the
manifest at all, just use the corrected `expect` for the re-check/resume decision and let the
Developer's own fix be the durable one.

**Measured: it would have REFUSED on the only real case.** v6 node 3's workdir holds **four** fresh
`model.safetensors` — `checkpoint-4236/`, `checkpoint-5648/`, `checkpoint-7060/` and `final/` —
so `_artifacts_written_elsewhere` returns 4 and the `len(found) != 1` rule abstains. That is the
rule working correctly (a metric or a declaration chosen by directory-walk order is not
deterministic), but it means E buys nothing on the shape that motivated all of this: a training run
that keeps checkpoints is the *normal* case, not the exotic one, and the basename will almost always
be ambiguous. The same fact also explains why the near-miss diagnostic on that node named the OLDEST
checkpoint — the Developer got the right path from the naming convention, not from the hint.

**Risks.** Applied to an OPERATOR declaration this would be the engine overruling the operator about
how their run is measured — do not do that. Applied to the agent's manifest it is strictly safer
than asking the agent, because the rule is deterministic, bounded and inside the workdir.

**Safe if.** It never touches an operator declaration, and ambiguity is a refusal (already the rule).

**Verdict: DOWNGRADE.** The idea is sound and the safety argument holds, but on this corpus it fires
0 times out of 1 because checkpointing makes the basename ambiguous by default. If it is built, it
must be as a fast path that *supplements* the Developer's fix (which was correct here), never as a
replacement for it — and it does not address gate 1's honesty problem on its own.

### F. Remove F1e, keep the cause-repair

**What is lost.** Operationally, on the evidence: nothing — see §4. No node has ever been promoted,
and the one live trigger (§2) is a bug. What is genuinely lost:

* the **rules as written down**. `declared_pipeline_completed`, `declaration_only_repair`,
  `recheckable_expect` and `recheck_floor` are four named, tested, individually-argued rules. Their
  value is not the promotion; it is that four decisions an owner would otherwise have to re-derive
  are stated with their reasons. Options D and E need all four of them.
* the **`EXPECT_SINCE_KEY` writer** (`command_eval._run_stages`), which records the floor a contract
  was held to on the stage row. That is additive, fold-ignored, useful diagnostically on its own, and
  required by D. **Keep it regardless.**
* ~370 lines of test that pin, among other things, that a pipeline which aborted early is never
  promoted — a property worth keeping even if nothing can promote.

**Verdict: removing the *promotion* is defensible; removing the *rules* is not.** If the owner wants
this closed today with no further work, delete `_recheck_repaired_contract`'s call site and the
`declaration_repair_provenance` writer, keep the four predicates and their tests as the specification
D would build on, and keep `EXPECT_SINCE_KEY`.

### G. A weaker rung than "measured" — promote for BREEDING but not for CHAMPION

**Makes possible.** The stated cost of the default `audit` rung is that a salvaged node "is NOT bred
from, so a run whose only node was salvaged proposes fresh ideas instead of improving that one". On
v6 that is the real loss: node 3 had the best number in the run and could neither win nor be a
parent. Champion-hood is a claim about comparability; breeding is a claim about "this direction
looked promising", which a salvaged number supports much better than it supports the first.

Measured on v6: node 3's salvaged 0.728113 was excluded from `feasible_nodes()` and therefore from
breeding — while still feeding two `lessons_distilled` entries, which are not gated on feasibility.
So the run already propagated the number into its own knowledge; the only thing the violation
actually withheld was the ability to build on the node directly.

**Costs.** `feasible = not violations` is a single fold rule read by both champion selection and
breeding (`RunState.feasible_nodes()`). Splitting them means a second derived predicate and touching
the selection vocabulary — not large, but it is selection machinery, which is the part of the engine
where a mistake is silent and expensive.

**Risks.** Low. Breeding from a salvaged node cannot make the salvaged number win; it can only
misdirect the search, which is what the search is for.

**Verdict: the cheapest way to recover most of F1e's actual VALUE without any of its trust
questions**, and it is orthogonal to everything above. Worth costing separately.

## 6. Ranking

| # | option | reachable? | would have fired (of 3) | trust cost | recommend |
|---|---|---|---|---|---|
| 1 | **D — finish the pipeline instead of promoting** | yes | **3** | none | **build** |
| 2 | **Gate-2 fix: require the DECLARATION to have changed** (§2) | n/a | n/a | removes a live defect | **build regardless of the rest** |
| 3 | **C — producer split** | n/a (a constraint) | n/a | reduces | adopt as a requirement on any promoting path |
| 4 | **G — breed-but-never-champion rung** | yes | 1 | low | cost it; it is where the measured loss was |
| 5 | **A — operator `expect` on `score`** | no (for F1e) | 0 | low | do it as a contract feature, not as an F1e fix |
| 6 | **F — remove the promotion, keep the rules** | — | — | none | the do-nothing-further answer; acceptable |
| 7 | **E — engine-authored deterministic correction** | yes, with D | **0** (basename ambiguous) | low | shelve; supplement at best |
| 8 | **B — relax gate 1** | yes | 1, and it is the wrong 1 | **catastrophic** | **reject** |

**Recommended package:** (2) now, because it is a live defect, independent of the decision, and
cheap; then (1)+(3) as the answer to F1e proper; (4) costed separately, because on this corpus the
measured loss was a node that could not be BRED FROM, not a node that could not be champion. (5)
whenever an operator wants a contract on their scorer, documented as unrelated to this. (7) shelved
on the measurement. If the owner wants this closed today with no build, (6).

**Answers to the owner's two questions, in one line each.** *May the appended `score` carry an
operator `expect`?* Yes, and it is a reasonable feature — but it does not make F1e reachable, so do
not adopt it *for* F1e. *Who may repair an operator's declaration?* **Nobody, and nothing here needs
them to.** The reachable honest case only ever repairs the AGENT's manifest, and then finishes the
run so the operator's own measurement is taken rather than inferred.

## 7. What must not be lost whichever way this goes

* `EXPECT_SINCE_KEY` and its writer — additive, fold-ignored, and the only record of the floor a
  contract was held to.
* `declared_pipeline_completed`'s argument. Whatever replaces the promotion, "a node is its whole
  pipeline, not its first stage" is the load-bearing sentence, and D is the only option that
  satisfies it by making the rest of the pipeline actually run.
* The stated non-claim on the provenance record: a re-checked contract says the stage produced what
  it declared, **not** that the operator's scorer ran. If any promotion survives, that sentence and
  the `producer` field survive with it.

## 8. How the claims here were verified

Every mechanism claim was either read out of the code or driven. Nothing was run inside `runs/` and
nothing in the repo was modified.

* **§2, the operator-stages promotion.** A real `Engine` with `eval_spec["stages"]` set, a Developer
  manifest written into the workdir, and the declared artifact created between the two checks:
  promoted, with the still-wrong path recorded as `expect_files`.
  (`scratchpad/f1e_probe.py`.)
* **§5-B, the relaxed gate 1.** `git archive HEAD` into the scratchpad, gate 1's last-stage clause
  relaxed there, then the suite's own end-to-end harness driven over a real subprocess pipeline in
  the v6 node 3 shape: `feasible: True`, no violation, `producer: agent_stage`, only `train` ran.
  Two suite tests flip red under that one change. (`scratchpad/tree/`, `probe_relaxed.py`.)
* **§5-C, the producer split.** Same tree, `recheckable_salvage` additionally requiring
  `OPERATOR_PRODUCED`: the same node returns to `salvaged: true` / `feasible: False`.
* **§4, the corpus.** A from-scratch structural parse of all 88 `events.jsonl` (27,276 lines) plus a
  read-only `stat` of `runs/rubertlite-dr-unified-v6/nodes/node_3/`.
* **Not verified.** Whether the §2 stat race fires in practice on this box's geesefs mount. The
  mechanism is proved; the *frequency* is not, and reproducing it would mean provoking mount-level
  eventual consistency, which is out of scope here. It does not need to be frequent to matter — the
  fix is a few lines either way.
