# LoopLab — External Works Synergy: AVO · Mechanist · SkillZip · Apodex · LDM · OmniScientist · Prime Agent (2026-09-03)

**Status: analysis; nothing here is shipped, nothing here flips a default.** A dated external-works
analysis in the [doc 13](13-external-works-analysis-2026-07.md) /
[doc 26](26-ouroboros-airi-analysis-2026-08-02.md) / [doc 41](41-external-works-synergy-2026-08-14.md)
line: seven works published or released around August 2026, each **fetched from its primary source
and checked against the forwarded summary that prompted this pass**, each mapped onto LoopLab module
paths that were re-derived against `master` on 2026-09-03. It never outranks source or tests.

Companion authorities: [doc 41](41-external-works-synergy-2026-08-14.md) (the previous cohort — AREX,
Skill-SP, Frontis-MA1, EvoLib, PACEvolve; several items below are the *same* item under a new
external name and say so), [doc 28](28-deep-research-sota-roadmap-2026-08-10.md) (Deep Research
ledger, DR-xx), [doc 27](27-agent-system-mega-review-2026-08-09.md) (agent-system review + eval
ladder), [doc 36](36-agent-driven-decisions-2026-08-13.md) (the NEXT-vs-RECORD principle),
[doc 42](42-pre-chewed-evidence-survey-2026-08-14.md) (what LoopLab's roles are handed instead of
looking), [doc 45](45-claim-surfaces-2026-08-20.md) (why a recorded claim is pinned to the site that
decides it), [doc 50](50-architecture-review-2026-09-02.md) (the current whole-tree finding ledger).

---

## 0. What was checked, and how

| Work | Source | One line, from the source |
|---|---|---|
| **NVIDIA AVO** | [developer.nvidia.com blog](https://developer.nvidia.com/blog/nvidia-avo-reaches-100-on-arc-agi-3-demonstrating-a-frontier-level-general-purpose-architecture-for-long-horizon-autonomous-agents/) | *Agentic Variation Operators*: a general-purpose coding-agent architecture — persistent memory, a supervisor watching the trajectory for stagnation, and a hypothesis→act→observe→update loop. 100.00 RHAE on all 25 public ARC-AGI-3 environments / 183 levels, in 6,624 environment actions (~12 % fewer than VISTA's 7,542). Separately: seven continuous days on GPU-kernel optimization, 500+ directions explored, kernels beating cuDNN by up to 3.5 % and FlashAttention-4 by up to 10.5 %. |
| **Mechanist** | [arXiv:2608.12036](https://arxiv.org/abs/2608.12036) | *AI as a Scientific Instrument for Discovering the Mechanisms of Intelligence* (Wang, Fang, Qiao, … Zhang, McAuley, Chua, Chen). An agentic interpretability researcher over an interpretability knowledge graph of ~13,000 papers, a multidisciplinary database, and a library of **32 analytical methods**; hypothesis → experiment → verification → iteration. Reports a cross-modal unsafe-trait-transfer risk, a mechanism theory of belief representation, and interventions derived from both. |
| **SkillZip** | [arXiv:2608.05604](https://arxiv.org/abs/2608.05604) | *Contract-Preserving Graph Compression for Scalable Agent Skill Libraries* (Tan, Wang, Liu, Xu, Yuan, Zhu, Zhang). Compresses a skill library at the **section-level graph**, turning recurring contract-valid motifs into reversible ported macros that preserve boundary signatures and dependencies; hydrates a dependency-closed context at inference and expands macros only when needed. **ReZip** updates the compressed library as skills evolve, from execution evidence. Up to +12.2 points over baselines; 3.46× compression, 99.2 % dependency preservation, 98.7 % verifier reachability, over libraries of 200–100K skills. |
| **Apodex** | [arXiv:2608.11341](https://arxiv.org/abs/2608.11341) | The *heavy-duty solver* framing: a foundation model, a harness, tools and **control policies** pursuing extended, stateful, verifiable investigations, over a common environment/task/episode abstraction with a fixed TRACES episode interface. Its evaluation is **HDS6** — Tools, Repair, Alternatives, Coherence, Evidence, Scope — scored **independently of final-task success**, so performance differences attribute to specific solver components. |
| **LDM** | [arXiv:2608.15669](https://arxiv.org/abs/2608.15669) | A *Large Discovery Model*: a generative model proposes and refines structured candidates while a surrogate predicts their performance **and quantifies uncertainty**, yielding an uncertainty-aware value that guides generation, refinement and selection. Three scenarios: neural-network training, antibody design (−18.2 % binding energy), molecular multi-objective design (>60 % relative gain). |
| **OmniScientist** | [arXiv:2608.13558](https://arxiv.org/abs/2608.13558) | *An Omni-Modal Omni-Discipline AI Scientist* (Li, Fei, Ju, Lee, Hsu). Three agents — ideation, experimentation, manuscript — over a perception layer active across the whole lifecycle, consuming images, signals, audio, video, 3-D structures, trajectories, tables, formulae and graphs. Code-enforced checks: novelty screening, statistical validity, execution provenance, numerical traceability. Against a baseline given only **precomputed scalar features**, direct perception improves all 7 evaluation dimensions and **wins 85 % of head-to-head judgments**. |
| **Prime Agent** | [arXiv:2608.23552](https://arxiv.org/abs/2608.23552) | *A Self-Improving RLM Harness* (Karten, Zhang, Thomas, … Hagemann, Jaghouar; Prime Intellect). An open harness for extended-horizon work: recursive subagents coordinating through direct agent-to-agent communication, and a continual harness preserving **histories, memories, skills, prompts and subagent specifications across trajectories**. ARC-AGI-3 RHAE Best@1 30 % → 95.5 %. |

### 0.1 The forwarded summary against the source — six deltas worth keeping

The pass began from a forwarded set of summaries. Checking each against its primary source moved six
claims, and the deltas are not cosmetic: **two of them are the transferable part of the work.**

| Claim as forwarded | What the source says | Why it matters here |
|---|---|---|
| Apodex's four components are "task formulation, environment, verification, error-correction loops" | The heavy-duty solver is *foundation model + harness + tools + control policies*; the contribution being evaluated is **HDS6**, six trajectory dimensions scored *independently of final-task success* | The forwarded reading describes infrastructure LoopLab already has. The source's reading describes a **rubric LoopLab does not have at all** — see §4. |
| SkillZip = section graph + MotifZip + PathHydrate | …**and ReZip**, which updates the compressed library from execution evidence as skills evolve | The forwarded half is retrieval. The dropped half is the *lifecycle*, which is exactly doc 41 §2's still-open Skill-SP item — see §3. |
| LDM "explicitly manages transitions between exploitation / exploration / discovery" | The abstract names no such three regimes; it names an **uncertainty-aware value** guiding generation, refinement and selection | The regime vocabulary may be in the body — not verified here. What *is* verified is the uncertainty channel, and that is the part LoopLab drops on the floor (§5). |
| OmniScientist's checks are "against HARKing, data leakage, unsupported claims" | novelty screening, statistical validity, **execution provenance**, numerical traceability | Closer to LoopLab's own vocabulary than the paraphrase: `metric_subject` / `applied_params` / `metric_inputs` *are* execution provenance and numerical traceability. |
| Prime Agent: ARC-AGI-3 30 % → 95 %; "L0–L3 state hierarchy" | 30 % → **95.5 %**; the abstract does not state an L0–L3 hierarchy, only the persisted set | The hierarchy may be in the body. Do not cite "L0–L3" as sourced. |
| AVO "beat FlashAttention-4 by 10.5 %" | *up to* 10.5 %, and *up to* 3.5 % over cuDNN | An `up to` dropped in transit is [doc 45](45-claim-surfaces-2026-08-20.md)'s defect verbatim. |

This table is the point of the section, not an aside. The repo's own most expensive recorded failure
of this shape cost three nodes to `torch.OutOfMemoryError` because a benchmark row was quoted into a
goal without its subject ([doc 45](45-claim-surfaces-2026-08-20.md)). A forwarded summary is a
secondary source; **read the abstract before mapping a work onto a module path.**

### 0.2 What this checkout could NOT re-derive, said out loud

This analysis was written in a fresh clone with **no `runs/` directory and no `~/.looplab`**. Every
corpus number below that is attributed to `CLAUDE.md` or to another doc is quoted **as recorded
there**, never as re-derived here, and is marked as such at each use. Every claim about *this tree* —
every symbol, every absence — was re-derived on 2026-09-03 and is stated with the file it was read
from. Where the two disagree, the tree wins.

---

## 1. AVO — the thesis, externally and expensively confirmed

**What it is.** A coding agent with three structural pieces: persistent memory carrying prior
implementations, evaluation results, profiler output and accumulated reasoning so the agent "resumes
from the current state rather than repeatedly reconstructing the search"; a **supervisor** that
"monitors the broader trajectory for stagnation or repeated unproductive cycles and can redirect the
main agent toward alternative strategies"; and a loop in which agents "form a hypothesis, act,
observe evidence, update state, and continue."

**What it validates.** The base model scores 30 % on ARC-AGI-3; the same model inside AVO scores
100 %. That is the sharpest public statement of the premise LoopLab is built on, and each of AVO's
three pieces already has a named owner here:

| AVO piece | LoopLab | Note |
|---|---|---|
| persistent memory | the append-only `events.jsonl` + pure `fold`, plus the cross-run stores under `engine/memory.py` / `engine/concept_shelf.py` | LoopLab's is *stronger*: replayable and provenance-carrying, where AVO's is a store. |
| supervisor watching for stagnation | `looplab/search/lock_in.py` | Also stronger, and deliberately so: `lock_in_signal` is a **pure deterministic read** over the concept DAG that fires on *staying inside one region of the action space* — "independent of whether the metric is still improving" (its own docstring). A model watching a trajectory cannot make that promise. Consumed by `engine/concept_cadence.py` and `engine/proposal_cues.py`, both re-derived 2026-09-03. |
| hypothesis → act → observe → update | the engine loop; the variation operators are `search/policy.py`'s `KIND_DRAFT` / `KIND_IMPROVE` / `KIND_MERGE` / `KIND_EXPAND` | "Agentic Variation Operators" is a new name for the thing `search/policy.py` and `search/operators.py::merge_idea` already are. |

**What to borrow.** One thing, and it is small: AVO reports **actions-to-result beside the score**
(6,624 vs VISTA's 7,542). LoopLab reports the champion metric, and separately reports cost through
`looplab tokens` / `looplab timings`; it publishes no single "what this champion cost to reach"
figure. That is a reporting join over data the run already holds, not new instrumentation — and it is
the number that would make a *harness* change (as opposed to a model change) legible.

**What NOT to borrow.** Nothing here argues for an LLM supervisor beside `lock_in.py`. LoopLab
already learned this one the hard way in the other direction: the training-log watchdog's model-side
verdict is *vetoed* by a deterministic `LossTrajectoryTracker`, and the veto may only ever refuse
(`engine/train_monitor.py`). A stagnation supervisor that can *raise* an intervention on model
judgement alone is the same authority this repo has repeatedly taken away.

---

## 2. Mechanist — the missing edge is literature ↔ concept graph

**What it is.** An agentic system that generates interpretability hypotheses, runs experiments,
verifies and iterates, standing on an interpretability **knowledge graph of ~13,000 papers**, a
multidisciplinary database, and a library of **32 analytical methods**. The point is not the agent
loop — LoopLab has one — it is that the loop is grounded in a *structured, persistent* body of
methods and prior results rather than in whatever the model recalls.

**What it pressures here.** LoopLab has both halves and **no edge between them**:

- `looplab/search/concept_graph.py` (413 lines, re-derived 2026-09-03) is a real axis-DAG of
  concepts with curated task skeletons, plus taggers, a lens and cross-run projections
  (`concept_tagging.py` / `concept_lens.py` / `concept_analytics.py` / `concept_map.py`).
- `looplab/tools/literature.py` is an arXiv search returning top titles + abstracts, flag-gated
  because "network egress is unreliable on some boxes."

The literal string `literature` occurs **zero** times in `search/concept_graph.py`, and `literature`,
`arxiv` and `paper` occur **zero** times in `looplab/events/types.py`. So a paper the Researcher
retrieves is a transient string spliced into one prompt: nothing durable, nothing attached to the
concept the run is exploring, nothing a later run can find. LoopLab builds a taxonomy of what *it*
tried and a search over what the *field* published, and the two never meet — which is precisely the
substrate Mechanist had to construct before it could do anything.

**What to borrow, and where.** The cheap, in-charter version is not a 13,000-paper graph. It is one
registered event type recording *what was retrieved, for which concept, in service of which
proposal*, so that (a) `search/concept_lens.py` can show prior art beside a concept's own
experiments, and (b) `trust/memo_verify.py` has an external citation to check against rather than
only the run's own nodes. By invariant #7 that record cannot exist without a type in
`events/types.py`, which is why the open item is pinned there — §9,
`retrieved-literature-is-never-durable` (spelled here without its marker token, the way doc 27 does
it: a slug is declared exactly once, and that declaration is §9's).

**What NOT to borrow.** The 32-method library as a *new* subsystem. LoopLab's `tools/skills.py` is
the method library; §3 is about fixing its unit of storage, not adding a second one beside it. And
Mechanist's subject — interpreting model internals — is out of charter here
([doc 26](26-ouroboros-airi-analysis-2026-08-02.md) §3.1): LoopLab's improvement target is the
external candidate solution.

---

## 3. SkillZip — the unit of retrieval is wrong here, and the body is unbounded

**What it is.** The observation that a skill library grows past the context window, and that the
usual fix — retrieve whole skills — double-loads every procedure two skills share. SkillZip changes
the stored unit to a **section with a contract** (boundary signature, dependencies, verifiers),
compresses recurring motifs into reversible macros that preserve those contracts, and hydrates a
*dependency-closed* subgraph at inference, expanding macros only as far as the task needs. ReZip
updates the compressed library from execution evidence as skills evolve.

**What it pressures here.** Two things, and both were re-derived on 2026-09-03.

**(a) The unit is the whole body, and it is served unbounded.** `tools/skills.py::SkillTools.execute`
answers `use_skill` with `return s.body` — the entire Markdown body of the skill, with no cap. The
string `clip` occurs **zero** times in `looplab/tools/skills.py`, and the derivation is worth writing
out because it makes the file unique rather than merely unusual: of the 32 modules under
`looplab/tools/`, 20 import from `_base` and 12 do not; 12 name `clip(` or `fit_rows(` (one of them
`_base.py` itself, where they are defined); and of the 12 that import nothing from `_base`, **exactly
one defines the ToolProvider pair `specs`/`execute`** — the other eleven are libraries
(`retrieval.py`, `memora.py`, `vectorstore.py`, `patch.py`, `edit_match.py`, …), not surfaces an
agent calls. That one is `skills.py`. `tools/_base.py` is described in `CLAUDE.md` as owning "the
shared bounded-output rules", and every other agent-facing reader in the tree obeys them —
`log_tools.py` states the byte range it covered and the call that continues past it; `run_tools.py::_research_memo` was reworked on
2026-08-19 specifically so a memo becomes *addressable* by `section=` instead of being cut by the
agent layer. That `section=` rework is PathHydrate, arrived at independently, for one document. The
skill library never got it. (§9, `skill-body-served-whole-and-unbounded`.)

**(b) A promoted skill is promoted forever.** The whole lifecycle lives in one expression at
`looplab/engine/memory.py`:

```python
status = ("promoted" if different or prior_status == "promoted" else "candidate")
```

`candidate` becomes `promoted` when a second, differently-fingerprinted task confirms the claim
(Jaccard < 0.6). There is no other transition. Nothing demotes, nothing retires, no usage or utility
counter exists, and the word `retired` does not occur in `engine/memory.py` at all. Execution
feedback flows *into* promotion exactly once and never again — which is ReZip's problem statement,
and Skill-SP's Controller, and doc 41 §2's still-open item, now with the deciding line named.
(§9, `skill-status-never-demoted-on-later-evidence`.)

**What to borrow, and where.** In order of cost:

1. **Bound the answer** (`tools/skills.py`, joining `_base.clip`/`fit_rows`). Mechanical, and it is
   the precondition for everything else: a library whose reads are unbounded cannot be measured.
2. **Make a skill addressable.** The `section=` shape `run_tools.py` already ships for research
   memos, applied to skill bodies, with the same rule that an answer ends by naming what it left out
   *and the call that returns it* — a remedy the caller has not already spent (`log_tools.py` rule 3).
3. **Demotion on recorded outcomes**, deterministic and code-owned. The doc 36 split is mandatory: a
   model may draft a refined body; only code may move `status`, and only from folded outcomes. The
   trigger question is already open as `auto-skill-promotion-run-end-only`, declared in
   `docs/BACKLOG.md` — cited by slug and not by section, because that file carries two `§0.12`s
   and two `§0.14`s, so a section number there is not an address. No second slug is minted here.
4. Macro extraction across skills is *last*, and probably never: the measured win (3.46×) is for
   libraries of 200–100K skills. Nobody has published a skill count for this box. Do not compress a
   library before measuring that it is large.

**What NOT to borrow.** Any scheme in which the *agent* decides what a skill's contract is. A skill
body here is distilled from `node_created.code` — the candidate's own source — and
`tools/skills.py` already labels auto-provenance bodies `UNTRUSTED_MEMORY_AUTO_SKILL` at the tool
payload rather than by directory. A "verifier" attached to a skill by the same model that wrote the
skill is a self-promotion primitive, exactly like the `SPECULATION_CUDA_PROBE_CODE_PREFIX` incident
recorded in `CLAUDE.md`.

---

## 4. Apodex — the rubric that scores the trajectory, not the answer

**What it is.** The forwarded summary described infrastructure. The source describes **HDS6**: six
dimensions — Tools, Repair, Alternatives, Coherence, Evidence, Scope — scored *independently of
final-task success*, over a fixed episode interface, so that a performance difference can be
attributed to a specific solver component.

**What it pressures here.** LoopLab records the richest trajectory substrate of any system in this
document — `events.jsonl`, `spans.jsonl`, the light span index, `looplab timings`, `looplab tokens`,
per-node episodes — and **scores none of it**. A run's verdict is the champion's metric plus
violations plus caveats. `looplab/judgebench/` scores *judges*, per decision, against outcomes the
run later supplied, and its own header says so; `score.py` deliberately keeps agreement-with-label
and agreement-with-incumbent in separate fields with no combined number. That is a rubric for one
component's *decisions*, not for the run's conduct.

The consequence is concrete and is already in the record: a run can spend 167.7 GPU-h with no
evaluation running (`engine/cadence.py`'s occupancy pace exists because of it), or buy 21.0 % of one
run's builds and never evaluate them (`looplab tokens`' per-card roll-up on `e5small-dr-unified-v9`),
and still report a clean champion. **Both figures are quoted as recorded in `CLAUDE.md`, not
re-derived here** (§0.2) — what is re-derived is that `occupancy_due` exists in
`looplab/engine/cadence.py` and that nothing rolls either fact into a run-level verdict.
Repair, Alternatives and Scope are exactly the dimensions on which those runs were bad, and the
report has no field for any of them.

**What to borrow, and where.** HDS6's *shape*, not its six words: a deterministic per-run trajectory
report derived from folded state and the span index — how many attempts were repaired and whether
the repair did anything (`engine/repair_verify.py` already answers this per repair, and nothing
aggregates it), how much of the action space was visited (`search/lock_in.py` already computes the
longest same-axis streak), how much paid work reached a terminal, how many claims carried evidence
(`trust/memo_verify.py`). All four already exist as per-item facts; none is rolled up.

This is **not** a new open item: it is rungs 2/4/5 of `agent-trajectory-eval-ladder-absent` in
[doc 27](27-agent-system-mega-review-2026-08-09.md) §4 (spelled there without its marker token here,
because a slug is declared exactly once and that declaration is doc 27's), whose 2026-08-20 amendment says in as many
words that "nothing here scores a TRAJECTORY". The contribution of this section is the *source* —
HDS6 is a published, six-dimensional, success-independent instance of the thing that item asks for,
and it settles the design question that item leaves open: score the trajectory **independently of
whether the run found a good number**, or the rubric collapses into the metric it is supposed to
explain.

**What NOT to borrow.** A model scoring the trajectory. Every dimension named above is derivable
deterministically from the log here, and a rubric a model authors about a run the same family of
models drove is not evidence — `judgebench/score.py`'s own warning about a churn measure being
"maximised by a candidate that reproduces every one of the incumbent's mistakes" applies with more
force one level up.

---

## 5. LDM — the composition already exists; the uncertainty is thrown away

**What it is.** A generative model proposes and refines *structured* candidates; a surrogate predicts
their performance **and quantifies uncertainty**; an uncertainty-aware value guides generation,
refinement and selection. Reported on NN training, antibody design (−18.2 % binding energy) and
molecular multi-objective design (>60 % relative gain).

**What it validates, and the claim NOT to make.** It is tempting to write that LoopLab "has an LLM
proposer and a surrogate that never meet." That is false, and re-deriving it is what this section is
worth: `search/panel.py::PanelResearcher` generates **K candidate ideas from the LLM and ranks them
with the empirical k-NN surrogate** — its docstring even records that this was chosen over an
LLM-judge because "an LLM-as-judge is ~random at ranking top vs bottom ideas" — and
`search/foresight.py::ForesightPanelResearcher` ranks the *structural* ideas the numeric surrogate is
blind to. LDM's headline composition is shipped here and predates it.

**What it does pressure.** `core/numeric.py::knn_idw` is the shared core of all three empirical
predictors and returns **`(prediction, nearest_distance)`** — the second value being the only
uncertainty proxy in the search layer. Of its three callers:

- `search/surrogate.py::_predict` returns both and uses the distance as an explicit exploration term:
  `acq = pred - self.explore * nearest` (sign by direction). This is UCB, and it is correct.
- `search/panel.py::_predict` keeps `pred = res[0]` and nothing else — the literal `res[1]` occurs
  **zero** times in the file. So the panel ranks K LLM ideas by **point estimate alone**: pure
  exploitation, over candidates whose whole reason for existing is that they are diverse.
- `search/proxy.py::ProxyScorer.score` ends `return None if res is None else res[0]`; `res[1]` occurs
  **zero** times there too. So the pre-eval **kill** is decided on a point estimate with no
  abstain-on-uncertainty rung — and killing the candidate the surrogate understands least is
  precisely backwards.

One primitive, three callers, and the one that keeps the uncertainty is the one that does not need it
most. (§9, `knn-uncertainty-dropped-by-two-of-three-callers`.)

**What to borrow, and where.** `nearest_distance` is a *distance*, not a calibrated variance, and
this document does not claim otherwise. Two changes are cheap and honest: give the panel the same
exploration term `surrogate.py` already computes from the same core, and give the proxy an
**abstain** band — never skip a candidate whose nearest neighbour is far, since `should_skip`'s own
docstring already promises it "never skips when it would be the best" and this is the same promise
about a candidate it cannot see. Both are pure functions of folded state, so both stay replay-safe
exactly as the existing predictors are.

**What NOT to borrow.** A trained surrogate. The zero-dependency, pure-Python, deterministic-given-
seed property of these three predictors is why they are replay-safe, and `search/proxy.py`'s
docstring names the seam (`ProxyScorer.score`) for the day a richer eval contract exposes a partial
signal. Uncertainty should arrive through that seam, not through a fitted model with weights.

---

## 6. OmniScientist — LoopLab has been converging on this, and the repo task is still blind

**What it is.** Three agents over a **perception layer active at every stage**, consuming raw
scientific data directly. The measurement that matters: against a baseline handed only *precomputed
scalar features*, direct perception improves all 7 evaluation dimensions and wins **85 % of
head-to-head judgments**.

**What it validates.** That is external, quantified confirmation of the single direction this repo
has spent the most effort on, catalogued in [doc 42](42-pre-chewed-evidence-survey-2026-08-14.md).
`tools/log_tools.py` exists because the training-log judge was handed a 40-record digest — about ten
tqdm lines, ~30 s of a five-hour run — and answered "pinned at ~23.0 … no learning trend" about a
curve that had gone 27.69 → 22.92 (recorded in `CLAUDE.md`; not re-derivable in this checkout, which
has no `runs/`). The fix was not a better digest, it was letting the judge **look**: `read_log`,
`metric_series`, a bounded seek that names its own byte range. The same move was then made for the
crash/timeout triage judge, and again for the monitor's code scouts. OmniScientist is the same
finding at a different scale, and its "precomputed scalar features" baseline is *literally* what
`training_log_digest` was.

**What it pressures.** The perception LoopLab bought is for **judges**. The **Researcher** — the role
that decides what to try — still gets scalars. And on the task family that does all the real work,
it gets nothing at all:

- Profiling is gated at `engine/orchestrator.py` on `getattr(self.task, "columns", None)` being
  callable; only then is `EV_DATA_PROFILED` appended and `state.data_profile` populated.
- `def columns` is implemented in `adapters/classification.py`, `dataset_task.py`, `mlebench.py`,
  `regression.py` (twice) and `timeseries.py`. The literal `def columns` occurs **zero** times in
  `looplab/adapters/repo_task.py`, which also implements no `data_samples` hook.
- So for a `repo_task` run, `state.data_profile` is `None`, and
  `search/foresight.py::verified_report(data_profile=state.data_profile, …)` — the "Verified Data
  Analysis Report" that primes predict-before-execute — is primed with nothing.

`CLAUDE.md` records that every `task_id` on the operator's box is `repo_task` or `toy_quadratic`.
If that still holds, the perception layer is off for every real run. (§9, `repo-task-exposes-no-perception-hook`.)

**What to borrow, and where.** Not omni-modality. A `repo_task`-shaped `columns`/`data_samples`
implementation — a bounded, deterministic look at the declared `data:` mounts the run already has
allow-listed in `runtime/read_allowlist.py` — is the whole of it. Everything downstream is already
wired: the event, the fold field, the foresight prompt, the leakage front-end in `core/profile.py`.

**What NOT to borrow.** The code-enforced checks, because LoopLab's are stronger and more specific:
`trust/leakage.py`, `trust/reward_hack.py`, `trust/cv.py`, `trust/gate.py`, `trust/confirm.py`, plus
`runtime/metric_subject.py` and `runtime/applied_params.py`. OmniScientist's "execution provenance"
and "numerical traceability" are one paragraph; here they are two modules with a corpus behind them.

---

## 7. Prime Agent — cross-trajectory continuity, and the half the charter refuses

**What it is.** An open harness for extended-horizon work: recursive subagents coordinating through
direct agent-to-agent communication, and a continual harness that preserves **histories, memories,
skills, prompts and subagent specifications across trajectories**. ARC-AGI-3 RHAE Best@1 30 % →
95.5 %. (The "L0–L3" hierarchy the forwarded summary describes is not in the abstract; see §0.1.)

**What it validates.** Cross-trajectory continuity, which LoopLab has for the first three of that
list — histories (`events.jsonl` + replay), memories (`lessons.jsonl`, `cases`, `concept_capsules`,
`skills/`, indexed by `engine/concept_shelf.py`), skills (§3). What it does **not** persist across
runs is **prompts**, and that is already open as `prompt-governance-has-no-typed-registry` in
[doc 27](27-agent-system-mega-review-2026-08-09.md) — Genesis, assistants, reports, monitors and
stewards keep separate prompt families and no typed registry exists. This document does not mint a
second slug for it; it adds the external argument that prompt continuity is not a tidiness concern
but the third leg of the thing that took a harness from 30 % to 95.5 %.

**What NOT to borrow, explicitly.** The *self-improving* half. A harness that rewrites its own
prompts and subagent specifications from its own trajectories is charter-refused here
([doc 26](26-ouroboros-airi-analysis-2026-08-02.md) §3.1 / §4.2 #12): the improvement target is the
external candidate solution, not the harness. Nor the persistent REPL: `tools/dev_probe.py` builds a
`mkdtemp` world and removes it in a `finally` precisely so the probe has **no side effect** — which
is what lets it emit no domain event under invariant #3, and what keeps `node_created.files` the
whole record of what the Developer built. Making it persistent would require a durable event and
would put a second, unrecorded authoring surface beside the one the run is audited from. The
disposability is the design, not a limitation.

Agent-to-agent communication *without halting* is the one piece with a real analogue and a real gap,
and it is a smaller gap than it looks: `serve/assistant_watch.py` already gives agent↔human
continuity that outlives a process, with the trigger evaluated **by the server over folded state**
rather than declared by the agent. Any agent↔agent channel added later must inherit that rule, or an
agent gains the ability to wake another on a signal it controls.

---

## 8. Synergy — one contour, seven angles

The seven works agree on a structure LoopLab mostly has, and disagree with it in five places.

**Where they agree with what is here.** AVO's memory+supervisor+loop is `events.jsonl` +
`search/lock_in.py` + the engine, with LoopLab's versions deterministic where AVO's are not.
LDM's generator-plus-surrogate composition is `search/panel.py` + `search/foresight.py`, shipped and
argued from a measurement. Prime Agent's continual harness is the cross-run store family.
OmniScientist's perception thesis is `tools/log_tools.py`, arrived at from a specific failure.
**None of these is a gap, and writing them up as gaps would be the exact drift `CLAUDE.md` warns
about** — a status marker nobody re-derived.

**Where they disagree, in dependency order.**

1. **Memory hygiene before memory growth** (SkillZip/ReZip, §3). A library that is served whole,
   unbounded, and never demoted cannot be grown safely. Bound the read, make it addressable, then
   let outcomes move `status`. Items: `skill-body-served-whole-and-unbounded`,
   `skill-status-never-demoted-on-later-evidence`.
2. **Perception for the proposer, not only the judges** (OmniScientist, §6). A `columns` hook on
   `repo_task` turns the whole already-wired profiling chain on for the runs that matter. Item:
   `repo-task-exposes-no-perception-hook`.
3. **Uncertainty into the two callers that drop it** (LDM, §5). One primitive already returns it.
   Item: `knn-uncertainty-dropped-by-two-of-three-callers`.
4. **A durable record of retrieved prior art** (Mechanist, §2), which is also what gives
   `trust/memo_verify.py` something external to check. Item: `retrieved-literature-is-never-durable`.
5. **A success-independent trajectory rubric** (Apodex/HDS6, §4), over the facts that (1)–(4) will
   by then have made trustworthy. Tracked by the existing `agent-trajectory-eval-ladder-absent`.

The order is not preference. (1) is a precondition for measuring anything about skills; (2) and (3)
are independent and cheap; (4) supplies evidence (5) would otherwise have to invent; and (5) is the
only one that requires the others to be honest first, because a rubric over a record that overstates
is worse than no rubric.

---

## 9. The open items

Five new markers, each with a falsifier re-derived against this tree on 2026-09-03. Three further
items belong to slugs declared elsewhere and are referenced rather than duplicated
(`agent-trajectory-eval-ladder-absent`, `prompt-governance-has-no-typed-registry` — both
[doc 27](27-agent-system-mega-review-2026-08-09.md); `auto-skill-promotion-run-end-only` —
`docs/BACKLOG.md`), because a slug names exactly one item.

**A red `test_open_item_index` on any of these is not a defect: it means the item shipped. Delete the
marker and say in the prose what landed.**

Each falsifier below was **driven, not asserted**. Following `CLAUDE.md`'s rule for verifying a
guard, a throwaway tree (`git archive HEAD | tar -x -C …`) was mutated with the *shipped form* of
each fix — `clip(s.body, RESULT_CAP)` in `skills.py`; a `next_skill_status(...)` call replacing the
monotone expression; `res[1]` read in both `panel.py` and `proxy.py`; a `def columns` on `RepoTask`;
an `EV_LITERATURE_RETRIEVED` constant in `events/types.py` — and every predicate flipped True → False.
A falsifier that cannot go false is the vacuous guard this repo has found nine times in one day.

- *Closed 2026-09-06 (doc 52 row 17 shipped): the marker `skill-body-served-whole-and-unbounded`
  stood here. `tools/skills.py` now imports `_base`'s `clip`/`fit_rows`: `render_skill_body`
  answers `use_skill` in WHOLE sections under `SKILL_RESULT_CAP` (bytes are cut only when one
  section alone is over the cap, and then it says so), names every section it left out beside the
  exact `use_skill(name=…, section=…)` call that returns it, and `section=` makes a skill
  addressable exactly as `run_tools._research_memo` made a memo. A body that fits is byte-identical
  to the file. `tests/test_skill_sections_and_lifecycle.py` drives it. Deleted per the index rule.*
- *Closed 2026-09-06 (doc 52 row 17 shipped): the marker `skill-status-never-demoted-on-later-evidence`
  stood here. The lifecycle is a lattice: `engine/memory.py::next_auto_skill_status` is the SUPPORT
  edge (candidate → promoted on a different task family; a demoted card re-earns promotion the same
  way; `retired` never moves automatically) and `reconcile_auto_skill_statuses` the CONTRADICTION
  edge, run at finalize beside the writer: the newest lessons-store row about the card's claim
  (`source_statement_sha256` / `claim_sha256`) with a negative verdict demotes it — a promoted card
  only from a task family it was confirmed on — and the second demotion retires it; every move is
  receipted on the `reflection_note` (`skills_demoted`). Only code moves `status`, only from
  recorded outcomes. Deleted per the index rule.*
- **OPEN[knn-uncertainty-dropped-by-two-of-three-callers]** — `core/numeric.py::knn_idw` returns
  `(prediction, nearest_distance)`; `search/surrogate.py` spends the second value as a UCB
  exploration term while `search/panel.py` and `search/proxy.py` keep only `res[0]`, so the K-idea
  panel ranks purely exploitatively and the pre-eval kill has no abstain-on-uncertainty rung
  (§5; LDM).
  proof:absent:nearest@looplab/search/panel.py
  (bound to the panel, the ranking caller: the in-tree exemplar tuple-unpacks, so `res[1]` would
  never appear, and `proxy.py` already names `nearest` in prose)
- **OPEN[repo-task-exposes-no-perception-hook]** — data profiling is gated on the task exposing
  `columns`, six adapters implement it, and `repo_task` — the family the real GPU runs use —
  implements neither it nor `data_samples`, so `EV_DATA_PROFILED` never fires, `state.data_profile`
  stays `None`, and `foresight.verified_report` primes predict-before-execute with no view of the
  data at all (§6; OmniScientist).
  proof:`absent:def columns@looplab/adapters/repo_task.py`
- *Closed 2026-09-06 (doc 52 row 16 shipped): the marker `retrieved-literature-is-never-durable`
  stood here. `events/types.py::EV_LITERATURE_RETRIEVED` is registered (`BACKGROUND_APPENDABLE`),
  `engine/research_cadence.py::_record_deep_research` appends it beside the memo with the papers
  `core/research_record.py::parse_literature` read off each `arxiv_search` answer (id over the
  title, sha256 + length of the abstract), and `events/replay.py::_on_literature_retrieved` folds
  them onto `RunState.literature` deduplicated by that id, so a later run and the verifier have a
  durable record of what was actually read. Deleted per the index rule.*

---

## 10. What must not change

- **Engine invariants 1–7** (`CLAUDE.md`). Every item above is either a bounded read, a pure function
  of folded state, or one additive registered event type with reader-side defaults — chosen that way
  so none of them touches the sole-writer rule, the one-terminal rule, or `fold`'s determinism.
- **The doc 36 split.** A model may decide what happens NEXT — which skill body to draft, which idea
  to try, which log line to quote. Deterministic code over authenticated evidence owns the RECORD:
  a skill's `status`, a metric's subject and coordinates, a claim's verdict, a run's rubric.
  Every borrow in this document is a widening of what a role may SEE, never of what it may decide.
- **Charter-bound self-evolution** ([doc 26](26-ouroboros-airi-analysis-2026-08-02.md) §3.1 / §4.2
  #12). Prime Agent's self-improving harness and Mechanist's model-internals subject both stay out.
- **`tools/dev_probe.py`'s disposability** (§7) and the four rules that make it emit no event.
- **The trust layer's independence.** Any new memory writer joins
  `engine/memory.py::unreliable_metric_ids` rather than bypassing it — including anything that
  records retrieved literature or a trajectory score.

---

## 11. Recommended order

1. **`tools/skills.py` joins the bounded-output contract.** Smallest change in this document, and the
   precondition for every later claim about the skill library.
2. **`columns` (or `data_samples`) on `RepoTask`.** Everything downstream is already wired; this is
   one adapter hook standing between the real runs and a profiling chain that already exists.
3. **The exploration term into `search/panel.py`, the abstain band into `search/proxy.py`.** Two pure
   functions over a value the shared core already computes.
4. **`section=` addressability for skill bodies**, following `run_tools.py::_research_memo`'s shape
   and its rule that an answer names what it withheld and the call that returns it.
5. **A registered event type for retrieved literature**, then the `concept_lens` join.
6. **Skill demotion from folded outcomes**, code-owned, after (1) and (4) make the library
   measurable — and after the `auto-skill-promotion-run-end-only` trigger is settled, since a
   lifecycle that only runs at wrap-up can only demote at wrap-up.
7. **The trajectory rubric** (§4), last, over a record the first six steps have made honest.

Steps 1–3 are hours of work each and change no interface. Steps 4–6 are the compounding loop. Step 7
is the one that makes a harness change legible as a harness change — which is, in one sentence, what
all seven of these works are for.
