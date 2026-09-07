# 58. What is confirmed, what is not, and what must be checked

Written 2026-08-31, after a container restart destroyed the working tree and forced a full re-read
of what this programme actually knows. Four sources were audited independently: docs/56 in full
(71 sections), the finished paired campaign in the surviving snapshot (markers, ledgers, logs), the
older operator lists (`looplab-open-questions.md`, `looplab-todo-2026-08-11.md`, docs/53), and this
session's own measurements.

It exists because a claim nobody can re-derive is a claim nobody should act on, and because the
audit found that several numbers this programme has been quoting for a week do not survive being
looked up.

An independent re-check on 2026-08-31 then found 26 defects in THIS document, including three
quotations of text that does not exist and two rows marked **solid** that its own sources refute.
Those are corrected in place below, marked rather than swept, and the method that produced them is
in §58.11.

**Read §58.1 before quoting any speedup from docs/56, and §58.11 before quoting this document.**

---

## 58.1 Three corrections to things stated as fact for days

**The paired campaign ran 2026-08-24 10:12 → 2026-08-25 06:10 UTC, not on 08-29.** Verified from
`epoch=` fields in `campaign-final/*.attempts` — the earliest is `a1 started=2026-08-24T10:12:06Z
epoch=1787566326` — against `logs/run_final-relaunch.log`, which opens
`[10:12:06] ===== FINAL CAMPAIGN: both arms, 20 tasks, $1.00 each, whole-physical-core lanes =====`
and closes `[06:10:13] ===== FINAL CAMPAIGN COMPLETE =====`. The driver's log prints bare clock
times with no date, and the snapshot that carries it is dated 08-29, so "the campaign finished at
06:10 on the 29th" was read off a timestamp that never said that. Every statement of the form "the
campaign of 2026-08-29" in this session's reports is wrong by five days.

**Corrected 2026-08-31 — this bullet closed the campaign at 08-26 09:40, and that is the wrong
driver.** 08-26 09:40:40 is real: it is the last `epoch=` in `campaign-final/A-*.attempts`
(`min_dominating_set`, `multi_dim_knapsack`, `rectanglepacking`, `set_cover_conflicts`, all `a3`,
all `epoch=1787737240`). It was written by the POST-campaign arm-A relaunch in
`logs/rerun-arm-a.log`, whose last restart is stamped `[14:02:32] ===== ARM A, RELAUNCH with the
model entry timeout=1900 — 20 tasks, $1.00 each =====` and whose final line is `[09:40:40] arm A
w=auto rc=0 | markers 11`. Taking it for the campaign's end is the same error this bullet was
written to correct: a timestamp read off whichever log was nearest.

**Arm A did not run once per task; it ran two to five times.** The scoring markers carry
`attempt=a3` and `attempt=a5`, and those attempts were launched by a separate driver
(`logs/rerun-arm-a.log`) AFTER the final campaign reported itself complete, with configuration that
changed between attempts (`ALGOTUNE_LLM_TIMEOUT_S=1900`). Arm B has one attempt per task. The design
is therefore not paired; it is two unsynchronised runs reconciled afterwards.

**Inside the final campaign proper, arm A collapsed and its markers say otherwise.** 16 of the 20
arm-A markers in `logs/final-A-wauto.log` and `final-A-w1.log` record `wall` between **3 and 19**
seconds with `rc=0 state=ran_to_completion attempt=a1` — the floor is `queens_with_obstacles`
`wall=3`, the ceiling `pagerank` `wall=19` — while the remaining four ran 2 063 – 2 179 s. A
task-arm cannot complete in five seconds. The marker vocabulary has no way to say "exited
immediately", so the campaign's own record reports sixteen instant successes.

**Corrected 2026-08-31 — the lower bound was 2, and the eight `operator_skip` tasks were not
"further" tasks.** No marker in either campaign log says `wall=2`; the smallest is 3. And
"eight further tasks" cannot be true, because 16 + 8 > 20: the two campaign logs carry exactly 20
arm-A markers between them (11 + 9) and **none of them says `operator_skip` at all**. The eight
`operator_skip` records live in `campaign-final/A-*.done`, they are a SUBSET of the same twenty
tasks, and they are the state left by the relaunch rather than the campaign's own record. Only five
carry `wall=0` (`max_clique_cpsat`, `max_common_subgraph`, `max_independent_set_cpsat`,
`max_weighted_independent_set`, `queens_with_obstacles`); the other three read
`state=operator_skip reason=campaign_stopped_by_operator … wall_h=0.0` (`min_dominating_set`,
`multi_dim_knapsack`, `set_cover_conflicts`) — an operator's decision, recorded as one, and the
honest part of the record rather than a further collapse.

---

## 58.2 What is confirmed, and on what sample

| claim | sample | standing |
|---|---|---|
| Both arms were priced identically | ledger reconstruction to 1e-15: $0.14/$0.28 per 1M | **solid** |
| Arm B's spend lands on its ceiling | 20/20 runs land in **$0.9329 – $1.0113**, summed per `arm`/`task` from `meter/meter.jsonl`; the one undershoot is `max_weighted_independent_set` at $0.9329 | **solid for the money; NOT solid for the mechanism.** See the row below |
| Arm B was STOPPED by its ceiling | the explicit refusal line — `Refused: LLM spend ceiling reached: $1.00xx of the $1.0000 set by \`llm_budget_usd\`` — appears in **11** of the 20 `campaign-final/B-*.log`, matching the 11 `.done` markers that read `rc=2 state=stopped_after_start` | **NOT solid — refuted for 9 of 20.** The other nine (`convex_hull`, `count_riemann_zeta_zeros`, `max_independent_set_cpsat`, `max_weighted_independent_set`, `pagerank`, `pde_heat1d`, `queens_with_obstacles`, `rbf_interpolation`, `rectanglepacking`) end `stop: PAUSED (node N) — resumable, NOT finished` / `pause reason: auto-paused: a Developer session crashed`. They had reached ~$1.00 anyway, so the ceiling is where the money went — but it is not what ended the run |
| The arena's own accounting under-counts spend | **12 of 12** scored arm-A tasks exceed the $1.00 budget on their scoring attempt alone (100 % – 241 %); the six that exceed it by ≥ 12 % are `pde_heat1d` 241 %, `sparse_eigenvectors_complex` 238 %, `rbf_interpolation` 201 %, `rectanglepacking` 126 %, `kcenters` 125 %, `integer_factorization` 112 %. Cause found: it does not count reasoning tokens — `A-pde_heat1d.log` prints `Spend limit of $1.0000 reached. Current spend: $1.0523` where the ledger sums `$2.4065`, and the discrepancy is monotone in completion tokens and vanishes where they are few | **solid, and it is the strongest result the campaign produced.** The earlier "6 of 12" applied an unnamed ≥ 112 % threshold and read as though six tasks stayed inside budget; none did |
| Every finished PROBE ends on `budget_exhausted` | docs/56 §61: `run_finished.reason` across all 46 completed probes, 46 of 46 | **solid about the probe corpus and about nothing else.** It does not hold in the campaign this table is about: 11 of 20 arm-B runs stopped on the ceiling, 9 were auto-paused by a crashed Developer session and never resumed. "No run has ever ended on anything else" was this table's own addition to docs/56's sentence, and those nine refute it |
| The pip repair removed its own cause | 92 mentions of "No module named pip" before, 0 after | **was solid; no longer re-derivable.** The probe trees it was counted over died in the restart. `grep` over everything that survives — the campaign snapshot and the six probes under `runs-archive/model-probes/` and `/var/tmp/looplab-bench/model-probes/` — returns 0, which is consistent with the "after" half and is no evidence at all about the "before" half |
| `pde_heat1d`'s checker punishes being more accurate than the reference | discovered independently by two probes, 2 of 2, each wording it in its own words | **solid** |
| No train→test drift | docs/56 §51.1: 54 probes carrying both figures, median `test / best-train-node` **0.986**, mean 0.973, test below train in 42 of 54 | **solid** |
| The loop's ruler on this box is honest | reference-against-itself: `pagerank` 1.0024/1.0022/0.9997, `pde_heat1d` 0.9958, `edge_expansion` 0.9847, all against baselines re-measured here | **solid, this box only** |
| Restored baselines are not valid after a restart | this box measures the reference **6.4 % slower than on 08-29** — `stale-baselines-from-20260829/WHY-SET-ASIDE.md` dates the cache it compares against and gives `pagerank`'s instance median as 110.5 ms today against 103.8 ms then; the same solver scored 0.9517 / 0.9480 / 0.9606 / 0.9533 against the old cache and 1.0024 / 1.0022 / 0.9997 against a fresh one | **solid.** The comparison date was 08-24 in the first version; the source says 08-29 |

## 58.3 What is NOT confirmed, and must stop being quoted as if it were

**Which arm wins.** Only **10 of 20 tasks** have a number from both sides. Arm A wins 6, arm B wins
4. The median ratio B/A is **0.963**; the geometric mean is **1.538** — the two statistics point in
opposite directions because the distribution spans two orders of magnitude (0.252…24.47). Sign test:
**p = 0.75**. There is no resolution here in either direction.

**The "N× arm A" figures in docs/56.** The largest are **127×** (`edge_expansion`, §60 and §63.1),
**124.6×** (`pde_heat1d`, §64), **20×** (`integer_factorization`, §44) and **18.3×** (§65, which
supersedes §63.1's 17.1×); at least nine more are printed — 17.1×, 10.3×, 9.4×, 7.4×, 6.2×, 2.8×,
2.6×, 1.6×, 0.60×. All are ratios against arm-A constants drawn from the campaign audited above.
The first version of this paragraph listed four (127×, 18.3×, 7.4×, 2.8×) after the word "Every",
which reads as an enumeration and is a sample — it omitted the second-largest figure in the
document.

**Eight of arm B's twenty numbers were obtained by re-scoring** (`logs/rescore.log`, 2026-08-25
05:02–05:10) with `eval_seconds` at **0.357 – 0.558** of the first pass — 306.8 – 341.6 s down to
119.2 – 180.3 s, from each file's own `rescored_from` block — and the evaluation WIDTH not recorded.
"Roughly halved" was the first version's phrase and it flatters four of the eight, which fell to
nearly a third. Width moves speedup by about 1.6× on this box: `ab2-summary.txt` scores the same
`discrete_log` solver at 1.0007 / 0.9973 on `workers=1` and 1.6318 / 1.6054 on `workers=24`, and
`validate-parallel.tsv` gives 1.0037 against 1.5930. So those eight carry an unrecorded factor.

**And exactly one of the eight is inside a comparable pair.** Seven are tasks arm A never finished,
so their unrecorded factor cannot reach the head-to-head at all. The eighth is `rectanglepacking`,
whose 0.0 became 2.262 in the re-score — and it is one of arm B's four wins. Remove it and the ten
pairs become nine: arm B wins **3 of 9** and the median B/A is **0.937**. The sentence "those eight
carry an unrecorded factor" was true and left the reader to assume the factor was spread across the
comparison, when it lands on one row of it, and that row is a win.

**Whether the two arms shared a baseline is not established.** Arm A's ruler is checkable — its
`.baseline_times` cache reconciles with `avg_oracle_time_ms` to three decimals. All twenty arm-B
files say `baseline_source: in-harness`, which records no baseline to compare.

**No loop switch has ever shown an effect on the final number.** Four separate arms — stage
guidance, the budget note, the novelty gate, read_code+card — each reached p ≈ 0.1 on n = 3 and then
collapsed on the fourth point. That is a consistent finding about our experiments, not about the
switches.

## 58.4 Contamination: the broken ruler of 2026-08-21

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
this point and carry an OPEN marker
with the slug `docs52-launch-block-contradicts-docs51-regime`, and nothing from that era survives under
`runs-B/`, `model-probes/` or `campaign-final/`. The NAMES are reused and the paths exist today —
`runs-archive/model-probes/` holds three probes from 08-31, `/var/tmp/looplab-bench/model-probes/`
three more, and every `campaign-final/` on disk belongs to the 08-24 campaign — so the correct
statement is that the 08-21 runs are gone, not that the directories are.
**Until a probe re-measures them on a verified ruler, every comparison constant
in docs/56 is of unknown provenance.**

## 58.5 Single-point claims

`discrete_log`'s 9.4× was flagged in §63.1 as the weakest load-bearing number and **collapsed at
§68 exactly as predicted** — the second probe scored 2.84, a 5.1× spread. The mechanism was visible:
dsDL got a second EVALUATED node and dsDL2 did not. dsDL2 did create a second node — §68 reads
"Two nodes created, **one evaluated**", with `node_1` created "with an empty `files` map when the
money ran out" — so what separates the two probes is one evaluation, not one node. (§68 also writes
"dsDL2 never reached a node_1" four lines later, which is its own inconsistency and not a reason to
repeat it.)

Current holders of the same risk, each quoted more than once and measured once:
`rbf_interpolation` 0.9977 (and it carries the strongest limiting conclusion in the document);
`convex_hull` at $10 = 26.65; the corpus record dsBN 344.43 (itself confounded, yet an input to
every comparison arm); the noise band "2–8.5 %" (one pair at each end); AlgoTuner's 1.96× accounting
figure; the kernel bit's 7.6× (denominator is two nodes).

## 58.6 Inconsistencies the audit found that the document did not

- **§60 and §63.1 both print 127× on 36 `edge_expansion` probes, and §63.1 marks it solid**, pooling
  the pip-repair eras — which is what §57 of the same document forbids. §60's row is
  `| edge_expansion | 1.109 | 36 | 141.12 | **127×** |` and states no standing at all; the `solid`
  is §63.1's: `| edge_expansion | 36 | 127× arm A | solid, and the source of every
  over-generalisation §62 had to walk back |`. §57's rule reads: "**and never pool across the pip
  repair** — the eras are different instruments".

  **Corrected 2026-08-31, and this is the worst bullet in the document.** Its first version put two
  strings inside quotation marks that appear nowhere: `grep` over docs/56 returns **zero** for
  `127× over 36 probes` and **zero** for `never pool epochs`. Both were paraphrases — right
  document, right section, right idea, wrong words. It also wrote "introduced eight sections
  earlier" when the distances are 60 − 57 = **3** and 63.1 − 57 = **6**, and it attributed the
  `solid` to both sections when §60 merely prints the number. Four errors in one sentence, in the
  bullet whose subject is a document quoting itself carelessly. See §58.11.
- **§61 and §68.1 give irreconcilable cost per evaluated node** — §61: 26 runs at $1, median **3.0**
  evaluated nodes, "One evaluated node costs a median **$0.339**"; §68.1: **55** ~$1 probes, median
  **2**, **$0.409** per evaluated node — with no explanation. Neither section uses the phrase
  "nodes per dollar"; that was this document's own, and it should not have been in quotation marks.
- **§71's median does not follow from its own table.** It prints `Median **35**`, but the eleven
  values in the table above it are 58, 60, 57, 54, 49, 47, 35, 25, 15, 14 and 7 — median **47**,
  with 35 the seventh of them. The sample — eleven of the twenty task-arms — is never named as one.
- **The ds3 zero has four inconsistent accounts** across §21.8, §22, §33 and §51.2 — four sections,
  four answers to one question. Did the solver import the extension? §33: it "never imported the
  extension". §51.2: the import "of it failed on the graded pass". §21.8: "the champion calls
  `pyximport.install()`" and imports `cutcounter`, compiling inside the timed evaluation. §22: the
  import succeeded and bound a stale `cutcounter.cpython-311-x86_64-linux-gnu.so` that the pip
  repair had left in the shared venv. The earlier count of three listed four sections and did not
  count them.
- §2's prose contradicts its own table; §1's sample is stated two ways.

## 58.7 What must be checked — in order

1. **Re-measure the comparison constants on a verified ruler.** Everything in §58.3 and §58.4 hangs
   on this and nothing else can be trusted until it is done. The standing rule (score a solver
   byte-for-byte equal to the reference, require ~1.0) now passes on this box, so the ruler is ready.
2. **Re-run arm A honestly.** One attempt per task, one driver, one configuration, recorded. The
   present numbers come from a3/a5 attempts launched after the fact with changing settings.
3. **Give the marker vocabulary a way to say "exited immediately".** Sixteen runs of 3 – 19 seconds
   are currently recorded as `ran_to_completion`.
4. **Record the evaluation width in every score.** Width moves the number ~1.6× and eight arm-B
   figures do not carry it.
5. **Make arm B's baseline checkable.** `baseline_source: in-harness` records nothing that can be
   compared against the other arm.
6. **Deliver the budget cue to the three roles that still cannot see it.** Measured on the finished
   `accEE`: `propose` 46/52 and `repropose` 9/9 carry "Spend guidance"; `plan` 0/49,
   `foresight_rank` 0/7, `hyp_prioritize` 0/4 do not. The commit claimed five roles.
7. **Find out whether the model uses the reference module now that the card names it.** `accEE`
   finished with 0 of 3 loop-written files importing or calling it. The base is **not** 3.0 % /
   2.3 %: docs/56 §69.1 retired that as "the LIFETIME corpus figure" and pinned the criterion, before
   the data arrived, at **4.9 – 8.3 %** (dsCH6 5.4 / 8.1, dsRBF2 8.3 / 8.3, dsPde2 4.9 / 4.9) —
   "The comparison that counts is dsDL3 against 4.9–8.3 %, not against 3.0 %." One probe settles
   nothing against either, but quoting the retired base understates what acceptance requires.
8. **Rebuild the quality-vs-spend curve.** The $10 points exist — sol10, 14 nodes, champion node 10,
   285.58 on test — but must be carried with docs/56 §19's own caveat about them: sol10 patched
   `sys.argv` to route around the missing pip, and §19 calls the result "an exploitation of the
   harness defect rather than an honest optimisation, and 259.68/285.58 should be remembered as
   such". The truncation-reconstructed curve does not exist, and the runs it would have read were
   destroyed. Needs fresh runs; label the result an anytime profile, not a prediction.
9. **Rewrite `run_final.sh`.** The campaign driver was never committed and died with `/var/tmp`. A
   full campaign cannot be repeated until it exists.
10. **Decide what to do about `.baseline_times` in snapshots.** They survive a restart physically
    but not meaningfully, and restoring them silently inflates every speedup by ~6 %.

## 58.8 Live loop defects carried over from docs/53

docs/53 carries fourteen `##` headings: one unnumbered divider (`## The two the CAMPAIGN found, not
the transcripts`) and thirteen numbered 0 – 11, **two of them numbered 9**. Ten of the thirteen are
defect items — §0 is a list of withdrawn claims, the first §9 is what must not be lost, §11 is an
order of work. Of those ten: **five are closed outright** (items 1, 4, 5, 7, 10), **two were retired
by decision rather than fixed** (item 2 `REFUTED`, item 6 `CLOSED … as NOT A DEFECT — the owner's
call`), and **three are closed only in part** — item 3's repair path, item 8's `store_case` / budget
receipt / curators, and the second §9, marked `PART CLOSED, PART REFUTED, PART STILL OPEN`. The
earlier count, "nine of fourteen closed and two retired", counted heading lines including the
divider, missed the duplicated 9, and left no room for the six survivors it then listed. What
survives:

- **The expensive one: $0.42 per measurement against the counterpart arm's $0.0175.** (docs/53 §2
  says *counterpart*, not "reference" — the campaign's other arm. The word matters in a document
  that also uses "the reference arm" for a different thing.) It never had its own marker — it is
  buried in a "What survives" paragraph — and doc 56 re-measures the split as
  32 % writing code, 68 % search scaffolding. Quote doc 56's numbers, not §2's.
- Instance profile reaches the card but not the engine.
- The repair path cannot see phases.
- An error terminal still drops `store_case`, budget and diversity bookkeeping, and the curators:
  `finalize.py::_recover_scoped_terminal` runs exactly one checklist step.
- 56 stream aborts remain unexplained.
- Nobody has measured whether models stop planning measurement steps.

## 58.9 Documentation that is now actively wrong

- **TWO sentences about the dollar budget are wrong, out of four that were paraphrased as one.**
  This bullet said `"There is no dollar budget"` appears in `docs/02-architecture.md` and
  `docs/guide/concepts.md`. Re-derived 2026-08-31: that string appears in NEITHER — it is a
  paraphrase of four different sentences that was written inside quotation marks, which is the
  defect this section exists to name. Two of the four are accurate about the shipped DEFAULT
  (`llm_budget_usd` is `0.0`, i.e. no ceiling unless the operator sets one): `02-architecture.md:15`
  "costs are metered without a configured dollar hard stop", and `guide/concepts.md:464` "LoopLab
  currently exposes no configured run-dollar limit". A third, the cost-governance bullet at
  `02-architecture.md:645`, is accurate about something else entirely — "No gateway-enforced
  hierarchical dollar budget or Settings-level 80%/100% stop ships", and neither of those ships.
  **The wrong ones are two, not one.** `02-architecture.md:459` — "Model cost is metered and
  reported; shipped Settings expose no hard dollar cap" — and `02-architecture.md:502-504`, inside
  the `Current cost boundary (2026-08-08)` blockquote — "`CostAccountant` can enforce a limit
  supplied directly by a caller, but every shipped Settings path uses no limit". Settings expose one
  and a shipped path uses it: `Settings.llm_budget_usd`
  (`looplab/core/config.py::Settings.llm_budget_usd`, `llm_budget_usd: float = Field(default=0.0, ge=0.0)`) is threaded
  into `CostAccountant(limit=…)` at `looplab/core/llm.py::run_cost_accountant` inside `run_cost_accountant`, and it
  is the ceiling that ends **eleven of the campaign's twenty** arm-B runs with `Refused: LLM spend
  ceiling reached` — not "every campaign run", which is the §58.2 over-reach corrected above.
  Two further defects of this bullet's own, corrected here: the emphasis in `without a *configured*
  dollar hard stop` and `no *configured* run-dollar limit` was ADDED — neither source carries the
  asterisks — inside quotation marks, in the paragraph about paraphrasing inside quotation marks;
  and the rule it appealed to, "§57.4", names a section that exists in no document in this tree.
  CLAIM[architecture-says-settings-expose-no-dollar-cap] `docs/02-architecture.md` still carries the
  sentences this bullet calls wrong, and `Settings` still carries the field that makes them wrong.
  decided:`present:shipped Settings expose no hard dollar cap@docs/02-architecture.md+present:llm_budget_usd: float@looplab/core/config.py`
- **docs/53 §11 "Order to fix in" is stale end to end.** Its first line is "**Item 2** (novelty
  gate, 66.3 % of budget)", which §2 of the same document refutes: the gate's own adjudication is
  "**$0.1141 of $17.6867 (0.6 %)**". Line by line for the rest: line 2 points at item 1 and line 3
  at item 4, both CLOSED; line 4 points at item 6, closed "as NOT A DEFECT — the owner's call";
  line 5 bundles items 3, 5, 7 and 8, of which 5 and 7 are closed and only item 3's repair path and
  item 8's `store_case` / budget receipt / curators survive. **No line still holds intact.** The
  earlier claim that one of the five does is not reproducible under any reading of "holds".
- **docs/53 §10 says `--full-context` is OFF BY DEFAULT**; `make_task.py` has `default=True`, and §1
  of the same document says so correctly.
- `looplab-todo-2026-08-11.md` §4/§5 point at doc 29's operator backlog, and
  `docs/43-operator-list-audit-2026-08-19.md` re-verifies that list item by item and is its current
  status. Not "superseded", which is what this bullet said: docs/43 spends a paragraph refusing the
  word — "**Doc 29 is not superseded.** It is the record of the 2026-08-11 session and keeps its own
  dated integrity" — so the bullet had imposed a frame the document it cites explicitly declines.
- ROADMAP.md declares itself a historical snapshot from 06-24 and is not a source of open questions.

## 58.10 Waiting on a person, not a measurement

**Eight of the ten** operator questions in `looplab-open-questions.md` are still open — the todo's
claim that all ten are open is stale by exactly two. #2 LiteLLM was resolved by a doc caveat
(`docs/01-product-design.md:14` now reads "`LiteLLMClient` is optional and is not selected by
Settings"), and #6, "A dollar budget", was built: the field it reports as absent is
`Settings.llm_budget_usd`. **There is no merge-queue question among the ten** — the earlier version
of this paragraph retired one that was never on the list, and then said "six or seven" where its own
two resolutions leave eight. The largest live one is #10: **there is no convergence or futility stop
at all.** Every `"reason"` literal in the engine was enumerated; neither `converged` nor `futile`
exists, and the Strategist's `stall_window` only changes operator choice — it never ends a run.

## 58.11 What failed in THIS document's method

An independent re-check on 2026-08-31 found 26 defects in the text above. Three were quotations of
strings that do not exist. Two were table rows marked **solid** that the campaign's own files
refute. Those five are not typos, and a document written to argue that *a claim nobody can re-derive
is not a basis for action* does not get to repair them quietly. Each correction above is marked
where it happened; this section names what produced them, so the next document can be checked for
the same five things rather than re-read line by line.

**1. Quoting from memory of a file that was open in the next window.** All three invented quotations
— `"127× over 36 probes"` and `"never pool epochs"` in §58.6, `"There is no dollar budget"` in
§58.9 — are accurate PARAPHRASES set inside quotation marks. Every one names the right document, the
right section and the right idea; none is the text. The failure is not ignorance of the source, it
is the belief that knowing what a source says is the same as knowing what it says. §58.9 caught the
third instance and diagnosed it correctly — and then committed the same fault twice inside the
correction, adding emphasis the sources do not carry (`*configured*`, twice) within quotation marks,
in the paragraph about paraphrase within quotation marks. The rule that follows is mechanical and
would have caught all five: **no string inside quotation marks that was not pasted out of a `grep`
run in the same minute.**

**2. Moving a true sentence to a corpus it was not measured on.** "Every run ends on
`budget_exhausted` — 46/46" is a correct statement about docs/56's 46 finished PROBES. It was
carried into a table about the CAMPAIGN, where 9 of 20 arm-B runs end `PAUSED` instead, and given a
**solid**. The second solid row failed the same way in miniature: "Arm B respects its ceiling …
hard refusal at the limit" welded a measurement that holds 20/20 (the money) to an explanation that
holds 11/20 (the refusal), and marked the pair solid. Both are the shape docs/56 §60.2 named — every
one of its own corrections "came from reading a table cell instead of the runs behind it" — committed
in the document that cites that audit.

**3. Presenting a selection as an enumeration.** "Every 'N× arm A' figure in docs/56" listed four
and omitted at least nine, including the second-largest number in the document. "Nine of docs/53's
fourteen items" counted heading lines, two of which are both numbered 9 and one of which is a
divider, and then listed six survivors the arithmetic leaves no room for. §58.10's "six or seven of
ten" did not survive its own three exclusions, one of which was of a question that is not on the
list. A count published without the rule that produced it cannot be re-derived — which is exactly
this document's complaint against docs/56 §71.

**4. Rounding a number until it became a different number.** `eval_seconds` "roughly halved" for
ratios of 0.357 – 0.558. "6 of 12 arm-A tasks exceed budget" for 12 of 12, at an unstated ≥ 112 %
threshold, phrased so that six tasks appear to have stayed inside budget. "`wall` between 2 and 19
seconds" for a floor of 3. Each shipped figure sits in the true figure's neighbourhood and outside
its meaning, and each was one `sort` away from being right.

**5. Taking a timestamp or a section number from the nearest source instead of the right one.** The
campaign's end time came from the post-campaign arm-A relaunch. The rule §58.6 said was
"introduced eight sections earlier" is 3 and 6 sections earlier. "§57.4" cites a section that exists in no document in
this tree. "Superseded by docs/43" adopted a word docs/43 spends a paragraph refusing. A
"merge-queue question" was attributed to a ten-item list that has never contained one. In every
case a plausible neighbour was substituted for the thing itself, and in every case the substitution
was invisible without opening the file.

None of the five is a failure of care about the SUBJECT — the mechanisms this document describes
survived re-checking, and the two solid rows that fell fell because of what was welded onto them,
not because the underlying measurements were wrong. All five are failures of care about the
CITATION. That distinction is not a mitigation: §58.4 exists to say that a number of unknown
provenance is not a number, and a quotation of unknown provenance is not a quotation. The
corrections above are marked in place rather than applied silently, because a document that repairs
itself quietly becomes one more claim nobody can re-derive.

**What is still not re-derivable in this document, and is flagged rather than fixed.** The pip-repair
row in §58.2 ("92 mentions before, 0 after") was counted over probe trees that the restart
destroyed; only the "0 after" half can be checked today. §58.7 item 6's per-role cue counts
(`propose` 46/52, `repropose` 9/9, `plan` 0/49, `foresight_rank` 0/7, `hyp_prioritize` 0/4) come
from a probe whose run tree is not in the campaign snapshot and could not be re-derived here. Both
are listed so the next reader does not mistake "unchecked" for "checked and confirmed".
