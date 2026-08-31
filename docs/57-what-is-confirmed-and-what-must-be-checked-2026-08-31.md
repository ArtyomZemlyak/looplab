# 57. What is confirmed, what is not, and what must be checked

Written 2026-08-31, after a container restart destroyed the working tree and forced a full re-read
of what this programme actually knows. Four sources were audited independently: docs/56 in full
(71 sections), the finished paired campaign in the surviving snapshot (markers, ledgers, logs), the
older operator lists (`looplab-open-questions.md`, `looplab-todo-2026-08-11.md`, docs/53), and this
session's own measurements.

It exists because a claim nobody can re-derive is a claim nobody should act on, and because the
audit found that several numbers this programme has been quoting for a week do not survive being
looked up.

**Read §57.1 before quoting any speedup from docs/56.**

---

## 57.1 Three corrections to things stated as fact for days

**The paired campaign ran 2026-08-24 10:12 → 2026-08-26 09:40 UTC, not on 08-29.** Verified from
`epoch=` fields in `campaign-final/*.attempts`. The driver's log prints bare clock times with no
date, and the snapshot that carries it is dated 08-29, so "the campaign finished at 06:10 on the
29th" was read off a timestamp that never said that. Every statement of the form "the campaign of
2026-08-29" in this session's reports is wrong by five days.

**Arm A did not run once per task; it ran two to five times.** The scoring markers carry
`attempt=a3` and `attempt=a5`, and those attempts were launched by a separate driver
(`logs/rerun-arm-a.log`) AFTER the final campaign reported itself complete, with configuration that
changed between attempts (`ALGOTUNE_LLM_TIMEOUT_S=1900`). Arm B has one attempt per task. The design
is therefore not paired; it is two unsynchronised runs reconciled afterwards.

**Inside the final campaign proper, arm A collapsed and its markers say otherwise.** 16 of the 20
arm-A markers in `logs/final-A-wauto.log` and `final-A-w1.log` record `wall` between 2 and 19
seconds with `rc=0 state=ran_to_completion attempt=a1`. A task-arm cannot complete in five seconds.
The marker vocabulary has no way to say "exited immediately", so the campaign's own record reports
sixteen instant successes. Eight further tasks are honestly marked `operator_skip` with `wall=0`.

---

## 57.2 What is confirmed, and on what sample

| claim | sample | standing |
|---|---|---|
| Both arms were priced identically | ledger reconstruction to 1e-15: $0.14/$0.28 per 1M | **solid** |
| Arm B respects its ceiling | 20/20 runs land in $0.933–$1.011, hard refusal at the limit | **solid** |
| The arena's own accounting under-counts spend | 6 of 12 arm-A tasks exceed budget on a single scoring attempt: `pde_heat1d` 241 %, `sparse_eigenvectors_complex` 238 %, `rbf_interpolation` 201 %. Cause found: it does not count reasoning tokens — it logs `$1.0523` where the wire says `$2.4065`, and the discrepancy is monotone in completion tokens and vanishes where they are few | **solid, and it is the strongest result the campaign produced** |
| Every run ends on `budget_exhausted` | 46/46, and no run has ever ended on anything else | **solid** |
| The pip repair removed its own cause | 92 mentions of "No module named pip" before, 0 after | **solid** |
| `pde_heat1d`'s checker punishes being more accurate than the reference | discovered independently by two probes, 2 of 2, each wording it in its own words | **solid** |
| No train→test drift | measured across the corpus | **solid** |
| The loop's ruler on this box is honest | reference-against-itself: `pagerank` 1.0024/1.0022/0.9997, `pde_heat1d` 0.9958, `edge_expansion` 0.9847, all against baselines re-measured here | **solid, this box only** |
| Restored baselines are not valid after a restart | this box measures the reference 6.4 % slower than on 08-24; the same solver scored 0.948–0.961 against the old cache and 1.0024 against a fresh one | **solid** |

## 57.3 What is NOT confirmed, and must stop being quoted as if it were

**Which arm wins.** Only **10 of 20 tasks** have a number from both sides. Arm A wins 6, arm B wins
4. The median ratio B/A is **0.963**; the geometric mean is **1.538** — the two statistics point in
opposite directions because the distribution spans two orders of magnitude (0.252…24.47). Sign test:
**p = 0.75**. There is no resolution here in either direction.

**Every "N× arm A" figure in docs/56** — 127×, 18.3×, 7.4×, 2.8×. All are ratios against arm-A
constants drawn from the campaign audited above.

**Eight of arm B's twenty numbers were obtained by re-scoring** with `eval_seconds` roughly halved
and the evaluation WIDTH not recorded. Width moves speedup by about 1.6× on this box
(`ab2-summary.txt`, `validate-parallel.tsv`), so those eight carry an unrecorded factor.

**Whether the two arms shared a baseline is not established.** Arm A's ruler is checkable — its
`.baseline_times` cache reconciles with `avg_oracle_time_ms` to three decimals. All twenty arm-B
files say `baseline_source: in-harness`, which records no baseline to compare.

**No loop switch has ever shown an effect on the final number.** Four separate arms — stage
guidance, the budget note, the novelty gate, read_code+card — each reached p ≈ 0.1 on n = 3 and then
collapsed on the fourth point. That is a consistent finding about our experiments, not about the
switches.

## 57.4 Contamination: the broken ruler of 2026-08-21

The 2026-08-21 arm-B campaign was measured with a ruler that timed the candidate and the baseline
with different code (a daemonic-worker downgrade) under thread oversubscription. Solvers
byte-for-byte equal to the reference scored 1.0008 / 1.0730 / 0.9999 serially and 1.6992 / 0.2802 /
0.2509 in that regime. **The error is not a constant factor** (+70 % on one task, −75 % on another),
so those numbers must be discarded rather than rescaled, and task ranking is destroyed too.

**docs/56 never mentions this.** `grep` finds zero occurrences of the daemonic-worker downgrade,
`ALGOTUNE_EVAL_WORKERS`, or thread oversubscription anywhere in its 71 sections, and no section
dates the campaign it draws its constants from. Worse, §34 states that probes were measured "the
same way … and as the campaign".

Provenance cannot be resolved from the tree: docs/51 §10 and docs/52 §8 contradict each other on
this point and carry an OPEN marker, and `runs-B/`, `model-probes/` and `campaign-final/` from that
era no longer exist. **Until a probe re-measures them on a verified ruler, every comparison constant
in docs/56 is of unknown provenance.**

## 57.5 Single-point claims

`discrete_log`'s 9.4× was flagged in §63.1 as the weakest load-bearing number and **collapsed at
§68 exactly as predicted** — the second probe scored 2.84, a 5.1× spread. The mechanism was visible:
the first probe had a second node, the second did not.

Current holders of the same risk, each quoted more than once and measured once:
`rbf_interpolation` 0.9977 (and it carries the strongest limiting conclusion in the document);
`convex_hull` at $10 = 26.65; the corpus record dsBN 344.43 (itself confounded, yet an input to
every comparison arm); the noise band "2–8.5 %" (one pair at each end); AlgoTuner's 1.96× accounting
figure; the kernel bit's 7.6× (denominator is two nodes).

## 57.6 Inconsistencies the audit found that the document did not

- **§60/§63.1 print "127× over 36 probes" and mark it solid**, pooling epochs — which violates §57's
  own "never pool epochs" rule, introduced eight sections earlier.
- **§61 and §68.1 give irreconcilable "nodes per dollar"** (26 runs / median 3.0 / $0.339 versus
  55 / 2 / $0.409) with no explanation.
- **§71's "median 35" does not follow from its own table**: the median of the eleven printed values
  is 47, and the sample — how many of the twenty arms — is never named.
- **The ds3 zero has three inconsistent accounts** across §21.8, §22, §33 and §51.2: whether the
  solver imported an extension gets three different answers.
- §2's prose contradicts its own table; §1's sample is stated two ways.

## 57.7 What must be checked — in order

1. **Re-measure the comparison constants on a verified ruler.** Everything in §57.3 and §57.4 hangs
   on this and nothing else can be trusted until it is done. The standing rule (score a solver
   byte-for-byte equal to the reference, require ~1.0) now passes on this box, so the ruler is ready.
2. **Re-run arm A honestly.** One attempt per task, one driver, one configuration, recorded. The
   present numbers come from a3/a5 attempts launched after the fact with changing settings.
3. **Give the marker vocabulary a way to say "exited immediately".** Sixteen five-second runs are
   currently recorded as `ran_to_completion`.
4. **Record the evaluation width in every score.** Width moves the number ~1.6× and eight arm-B
   figures do not carry it.
5. **Make arm B's baseline checkable.** `baseline_source: in-harness` records nothing that can be
   compared against the other arm.
6. **Deliver the budget cue to the three roles that still cannot see it.** Measured on the finished
   `accEE`: `propose` 46/52 and `repropose` 9/9 carry "Spend guidance"; `plan` 0/49,
   `foresight_rank` 0/7, `hyp_prioritize` 0/4 do not. The commit claimed five roles.
7. **Find out whether the model uses the reference module now that the card names it.** `accEE`
   finished with 0 of 3 loop-written files importing or calling it. Corpus base is 3.0 % / 2.3 %, so
   one probe settles nothing.
8. **Rebuild the quality-vs-spend curve.** The $10 points exist (14 nodes, champion node_10, 285.58
   on test); the truncation-reconstructed curve does not, and the runs it would have read were
   destroyed. Needs fresh runs; label the result an anytime profile, not a prediction.
9. **Rewrite `run_final.sh`.** The campaign driver was never committed and died with `/var/tmp`. A
   full campaign cannot be repeated until it exists.
10. **Decide what to do about `.baseline_times` in snapshots.** They survive a restart physically
    but not meaningfully, and restoring them silently inflates every speedup by ~6 %.

## 57.8 Live loop defects carried over from docs/53

Nine of docs/53's fourteen items are closed and two were retired by decision. What survives:

- **The expensive one: $0.42 per measurement against the reference arm's $0.0175.** It never had its
  own marker — it is buried in a "What survives" paragraph — and doc 56 re-measures the split as
  32 % writing code, 68 % search scaffolding. Quote doc 56's numbers, not §2's.
- Instance profile reaches the card but not the engine.
- The repair path cannot see phases.
- An error terminal still drops `store_case`, budget and diversity bookkeeping, and the curators:
  `finalize.py::_recover_scoped_terminal` runs exactly one checklist step.
- 56 stream aborts remain unexplained.
- Nobody has measured whether models stop planning measurement steps.

## 57.9 Documentation that is now actively wrong

- **"There is no dollar budget"** appears in `docs/02-architecture.md` and `docs/guide/concepts.md`,
  including in caveats added to fix earlier untruths. The field exists —
  `Settings.llm_budget_usd` is threaded into `CostAccountant(limit=…)` — and it is the ceiling that
  ends every campaign run in `budget_exhausted`.
- **docs/53 §11 "Order to fix in" is stale end to end**: its first item is "novelty gate, 66.3 % of
  budget", which §2 of the same document refutes (the real cost is 0.6 %). One of its five lines
  still holds.
- **docs/53 §10 says `--full-context` is OFF BY DEFAULT**; `make_task.py` has `default=True`, and §1
  of the same document says so correctly.
- `looplab-todo-2026-08-11.md` §4/§5 are superseded by `docs/43-operator-list-audit-2026-08-19.md`.
- ROADMAP.md declares itself a historical snapshot from 06-24 and is not a source of open questions.

## 57.10 Waiting on a person, not a measurement

Six or seven of the ten operator questions are still open — the todo's claim that all ten are open
is itself stale (LiteLLM was resolved by a doc caveat; the dollar-budget field was built; the
merge-queue question lapsed). The largest live one: **there is no convergence or futility stop at
all.** Every `"reason"` literal in the engine was enumerated; neither `converged` nor `futile`
exists, and the Strategist's `stall_window` only changes operator choice — it never ends a run.
