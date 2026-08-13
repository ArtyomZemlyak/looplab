# Operator backlog — 2026-08-11

Feature-sized asks from the operator that are **not** bugs and were deliberately not half-implemented
in the same pass. Each entry states what was asked, what the code does today (verified, with the
site), and what building it would actually take. Bugs from the same session were fixed and are in the
git history, not here.

Unlike [`BACKLOG.md`](BACKLOG.md) — which warns at length that it contradicts itself and is six weeks
stale — every status below was read off the tree on 2026-08-11.

---

## F1 · Derive the run width from the proposals, not from the box

**Asked:** "if I want to run one experiment per card — who decides that and how? Ideally
automatically, from the propose."

**Today.** `eval_parallel` / `llm_parallel` / `speculation_depth` all settle ONCE at run start and are
pinned into `run_started` (`engine/orchestrator.py`, the `_eval_parallel_value == 0` branch). AUTO
means *one experiment per detected GPU*, i.e. the width is a fact about the BOX. A per-Card footprint
(`{"gpus": N}`, `core/models.py::effective_card_footprint`) does influence that node's device
reservation and admission, but nothing lets a proposal change the run's width. The full behaviour is
now written up in [configuration.md](guide/configuration.md#one-experiment-per-gpu-who-decides-and-what-happens-when-two-runs-want-the-same-cards).

**What it would take.** The width is load-bearing for replay: it is pinned so a resume on a different
box continues the run's own treatment (engine invariant #6), and `speculation_depth` AUTO resolves off
it. A proposal-derived width therefore cannot just be recomputed per turn — it needs a durable
event that RE-pins the width mid-run (`budget_extend` is the existing shape for exactly this), plus a
rule for what happens when the proposals ask for more cards than exist. Start there, not at the
resolver.

### F1b · Tell the Researcher what the per-experiment GPU budget IS

**Asked:** "why is the current rubert run using 2 GPUs for one experiment instead of running them in
parallel?"

**Measured, 2026-08-12, on `rubertlite-dr-unified-v5`.** Nothing in the engine was wrong.
`run_started` pins `eval_parallel: 2` — AUTO settled correctly off the two H200s — and the engine was
in fact running card-1's build concurrently with node 0's eval. But `card_added` for BOTH cards
carries `footprint: {"gpus": 2}`, so `_resource_request_for_node` took the declared value (declared
beats AUTO, `engine/resources.py`), `_clamp_resource_footprint` clamped it to the POOL size rather
than to the per-experiment share, and the reservation took `[0, 1]`. Confirmed at the process level:
the eval launcher's `/proc/<pid>/environ` reads `CUDA_VISIBLE_DEVICES=0,1`, two ranks,
`WORLD_SIZE=2`. The Developer then sized the stage to that envelope —
`accelerate launch --multi_gpu --num_processes 2` — which is exactly what
`roles.py::_developer_footprint_guidance` instructs it to do. So `eval_parallel=2` is real and
currently unusable: node 0 holds both devices for its whole lifecycle, and card-1 will block on
`_acquire_gpus(2)` until it terminates.

**The gap.** `roles.py::_FOOTPRINT_GUIDANCE` asks the Researcher to declare `gpus` and never tells it
a per-experiment ceiling. The operator's goal prose is the only channel that names a number the
Researcher can act on — v5's said "two H200 GPUs are available", and the Researcher reasonably read
it as "you may have both". A role asked to size a request against a budget it cannot see will get it
wrong at some rate, and there is no reason for that rate to be non-zero.

**CORRECTION (2026-08-13).** The first version of this entry said the goal prose was the ONLY channel
carrying GPU information at all. That was wrong: `engine/proposal_cues.py::_cue_gpu_contract` already
spliced "GPU RESOURCE CONTRACT — this pool exposes at most {pool} GPU(s)". So the engine did speak —
and what it announced was the POOL SIZE, which if anything invited the `{"gpus": 2}` declaration this
entry is about. A pool announcement and a per-experiment ceiling are different facts, and only the
second one answers "how much may THIS card ask for". The fix therefore splices the ceiling LAST, so
it is the final device number the model reads; the pool line is left untouched because prompt strings
are contracts.

**What it would take.** Either (a) a hint carrying `max(1, pool // eval_parallel)` into both
Researcher prompts — which means the `RESEARCHER_HINT_ATTRS` registry, both readers, every
delegating wrapper and the forwarding tests, per the registry rule in CLAUDE.md; or (b) clamping
the declared footprint to the per-experiment share instead of to the pool, which decides that the
operator's `eval_parallel` outranks the Researcher's declaration. (b) is one line and a real policy
change — it would fence a legitimately multi-GPU experiment onto one device — so it is a decision,
not a fix. (a) is the honest one and is the larger job.

**BUILT, 2026-08-13 — option (a).** A new registry hint `_gpu_budget_hint`
(`agents/roles.py::RESEARCHER_HINT_ATTRS` + `RESEARCHER_PROMPT_CUES`, so it reaches BOTH readers
through the shared `collect_hint_cues` and all four wrappers through the shared `forward_hints`),
stamped per proposal by `engine/proposal_cues.py::_stamp_gpu_budget_hint` off the rule
`engine/widths.py::per_experiment_gpu_budget`. The proposed `max(1, pool // eval_parallel)` was
adopted with three edges decided against it: pool 0 yields **0** (a positive `gpus` is
`required_unavailable` and fails admission closed, so "you may have one" would produce exactly the
declaration that cannot be served); a CPU-locked task (`gpu_capable() -> False`) is told **nothing**;
a width above the pool still yields **1**, never the floor's 0. The stamp is per-proposal rather than
at construction so it reads the width AFTER `_repin_settled_widths` and after any `budget_extend` —
computing it in `Engine.__init__` beside the width settling would quote a resumed box's own AUTO
resolution instead of the pin. The POOL half stays live, because the reservation clamps against the
live pool. `_FOOTPRINT_GUIDANCE` was also reworded: it said "`gpus=1` only when the experiment
specifically needs one GPU", which read as discouraging the common case; it now says a stated ceiling
is a ceiling and that exceeding it serialises the run instead of buying hardware.
`tests/test_researcher_gpu_budget_hint.py` drives it end to end (both real prompts, a real wrapper
chain, and a real resume onto a bigger box). Option (b) — clamping the declaration to the share — was
NOT taken; a declared footprint is still authoritative at admission.

**Today's workaround.** Pin the Card's resource request (`card_resource_pinned`, `{"gpus": 1}`) —
`effective_card_footprint` merges the pin over the declaration at admission. **Caveat that matters:**
the pin reaches the SCHEDULER, not the Developer prompt (`_developer_footprint_guidance` reads
`idea.footprint`), so pinning after the code is written fences a `--num_processes 2` program onto one
device. Pin before the build lands, or expect to repair the stage command. For the next run, say it
as the contract the engine actually reads: *"each experiment gets exactly ONE GPU — declare
`footprint: {"gpus": 1}` on every card and write single-GPU training code."*

## F1c · Catch a path that escapes the node's own workspace

**Found by watching, 2026-08-12.** `rubertlite-dr-unified-v6` node 0 burned roughly 3.5 GPU-hours and
produced no recorded metric. The train stage was correct throughout: it wrote its checkpoint to
exactly the workdir-relative path its manifest declared. `vectorsearch/configs/config.yaml` carried

```yaml
checkpoint_path: /home/jovyan/data/vectorizer-unified/vectorsearch/experiments/<this node's name>/final
```

— an ABSOLUTE path into the editable SOURCE tree. The experiment name in it is this node's, so the
Developer authored the line. A node runs in its own materialized copy, so a path into the source tree
can never name a node's own output: `.exists()` fails on every node, on every attempt, forever.

The commit `b0327182` closed the half that let this become expensive (the scorer is now protected, so
a Developer can no longer "fix" it by teaching the scorer to retrain). It also ships a cheap advisory
— a note on a successful write whose content hard-codes a path under an editable source root. What
remains is the MECHANICAL detection, and it is deliberately not a blanket ban.

**Why not a blanket ban.** An absolute source path is legitimately needed for a large untracked
in-tree input that `seed_mode: auto` does not copy. The first-class answer there is a `data:` or
`references:` mount, or `seed_mode: "all"` — but refusing a write the model cannot satisfy costs a
repair attempt, and the STAGES phase's retries are the scarcest budget in the loop.

**The false-positive-free version is a COLLISION check against the manifest.** For each declared
`expect.files` entry `F` and each editable source root `R`, a staged file containing
`R + "/" + <path overlapping F's directory chain>` is an unambiguous "this node writes it here and
reads it there" contradiction, decidable from two artifacts LoopLab already owns. On v6,
`F = vectorsearch/experiments/<name>/final/model.safetensors` and the config carried
`<R>/vectorsearch/experiments/<name>/final` — a prefix of `F` under `R`.

**Two things to decide before building it.** (a) ORDERING: `RepoWriteTools` has the manifest in
`self.files["looplab_stages.json"]` after the STAGES phase but not DURING it, and not for a repair
whose manifest arrives only in the seeded working set — so the check must degrade gracefully exactly
where it matters most, and refuse-vs-bounce-vs-note is a real call. (b) A RUNTIME half would be
stronger and is probably the better first build: "the score stage spawned a process running a stage
command the pipeline already ran" is a fact the engine can assert, it cannot be evaded by the
indirection any static check can, and there is already per-stage process supervision to hang it on
(`engine/train_monitor.py`, `engine/asha_monitor.py`). It belongs beside `engine/eval_stages.py` with
its own event type.

**A third, orthogonal and trivial piece:** refuse an `eval.command` / `eval.cwd` that itself names the
editable source root absolutely, at submit time. Cheap — but it catches an operator mistake, not this
one.

## F1d · A repo task cannot declare ENVIRONMENT for its stages

**Found by watching, 2026-08-12.** `rubertlite-dr-unified-v6` node 0 crashed on its first attempt with
`botocore ClientError: InvalidAccessKeyId … ListObjects` — the data loader reached for S3 because
`VS_LOCAL_DATA_ROOT` was unset. The repair was correct and took three minutes: it added

```python
os.environ.setdefault("VS_LOCAL_DATA_ROOT", "/home/jovyan/data/dr-local")
```

to `vectorsearch/config.py`, at import, so both the train and the score stage see it.

Then **node 1 hit the identical error**, because a node is seeded from the SOURCE repo, not from a
sibling node's workdir. Every node in the run rediscovers the same fact and spends one repair attempt
on it. With `inline_repair_attempts: 12` that is affordable but wasteful, and it is not the agent's
mistake: `EvalSpec` has no `env` field and a stage accepts only
`{name, command, timeout, check, expect}`, so **code is the only surface the Developer has** for an
environment variable. It did the best available thing.

**The workaround, for now:** export the variable in the ENGINE's environment at launch —
`VS_LOCAL_DATA_ROOT=/home/jovyan/data/dr-local python -m looplab.cli run …` — and every node inherits
it with no repair spent. The goal text asking the agent to "set VS_LOCAL_DATA_ROOT" is asking for
something the operator can supply once instead.

**What building it would take.** A `stages[].env` map, and/or an `eval.env` applied to every stage, is
the obvious shape — the runner already builds a per-stage environment (`_resource_eval_env` composes
`CUDA_VISIBLE_DEVICES` there). Two things to decide: whether the values are part of the run's pinned
treatment (they change what the code does, so probably yes — `run_started` and the config snapshot),
and whether the Developer may DECLARE env in `declare_stages` or only the operator may set it. I lean
operator-only: an agent that can set arbitrary environment for its own scoring stage has another route
around the trust boundary that `b0327182` just closed for the scorer's code.

## F1e · Re-check a repaired artifact contract instead of leaving the metric SALVAGED

**Found by watching, 2026-08-13, and it is a systematic bias — not a one-off.** On
`rubertlite-dr-unified-v6`:

| node | operator | metric | provenance |
|---|---|---|---|
| 0 | draft | 0.708762 | measured |
| 1 | draft | 0.715142 | measured |
| 2 | draft | 0.727991 | measured |
| 3 | **merge** | **0.728113** | **salvaged** |
| 4 | merge | 0.224975 | measured |

**CORRECTION (same night).** The first version of this entry read "every DRAFT is measured, every
MERGE is salvaged" and predicted node 4 would fail the same way. It did not: node 4 set
`run_name: unified-baseline`, the repo composed `unified-baseline_rubert-tiny-lite`, and the
declaration named exactly that — a correct path, a fresh checkpoint, a measured metric. (Its 0.2249
is a real negative result: mean-merging the two models' weights destroyed them.) So the sample is
ONE salvaged merge, not a systematic bias against the merge operator, and the claim was n=1 plus a
prediction. What follows stands on the single honest case and should not special-case merges. A merge node's Developer authors a fresh config
with a new `run_name` and gets the testbed's composed `<run_name>_<model>` directory wrong, so the
`train` stage exits 0, fails its declared artifact contract, and metric salvage recovers the number
from the stage's stdout. Node 4 declares `unified-baseline_rubert-tiny-lite/final/…` — the OLD HUMAN
experiment's name, not its own.

**The consequence is the part that matters.** Under the default `metric_salvage: audit` a salvaged
metric carries a `metric_salvaged` violation and is excluded from `feasible_nodes()`, so it can never
become champion or be bred from. Node 3 produced the best number in the run (0.728113 vs the
champion's 0.727991) and cannot win — on the strength of a path typo, not of anything about the
number. Neither `audit` nor `select` is the right answer to that — `select` would
admit agent-produced bytes wholesale, which is the boundary salvage exists to keep.

**The fix is specific and cheap: the metric was never actually unmeasured.** The pipeline DID produce
the artifact — the near-miss diagnostic proves it, naming the exact file — and `metric_salvage_repair`
already fixes the declaration in the same attempt (node 3: `changed: ["looplab_stages.json"]`,
`cause_repaired: true`). What is missing is one step: after the cause repair corrects the manifest,
**re-run the artifact CHECK against the corrected declaration — not the stage.** The file is on disk;
`verify_stage_artifacts` is a handful of `stat` calls. If it now passes, the contract is satisfied by
the artifact the pipeline really produced, and the node should be recorded as MEASURED with no
violation, because nothing about the number was ever in doubt — only the sentence describing where it
lived.

**What to decide before building it.** (a) The freshness gate: the re-check must keep `since` at the
stage's own start, or a leftover from an earlier attempt could satisfy the corrected path. (b) Which
repairs qualify: only a repair whose `changed` set is exactly the manifest — a repair that touched
CODE has changed what the stage would produce, and its artifact must be re-run, not re-checked.
(c) Ordering against `metric_salvage`: the re-check belongs BEFORE the salvage decision, so a node
that passes never enters the salvage path at all and needs no provenance.

This subsumes most of the value of [F1c](#f1c-catch-a-path-that-escapes-the-nodes-own-workspace)'s
static half without its false-positive problem, because it acts on a contract that has already failed
and an artifact that already exists.

## F1f · The eval batch is a BARRIER, so one slow node idles a GPU for hours

**Found while watching `rubertlite-dr-unified-v6`, 2026-08-13.** Not the same defect as the
`freshness_stale` one fixed in `6f0a8be3` — that one stopped two cards from ever being *selectable*
at the same time. This one stops a new turn from *starting*, and it survives that fix.

**Measured.** `run_started` pins `eval_parallel: 2`. Nodes 5 and 6 were dispatched from the same turn
and really did train concurrently, so the fan-out works. Node 6 finished at 06:19 (86 minutes); node
5 was still training. At 08:11 — **~2 hours later** — `nvidia-smi` showed GPU 1 at `4 MiB` and 0%,
`card-3` folded to `selection_ready=True` with an EMPTY blocker list, and the log since 06:19
contained nothing but `llm_usage`, `train_monitor_alert`, and four `research_completed`/`hint` pairs.
No `card_build_requested`, no `node_building`, no `card_added`. Node 5 was at step 4415/7060 with
2:09 remaining, so the idle window is ~4 hours on one device.

**Why — CORRECTED 2026-08-13, the first answer named the wrong code.** The measurements above stand;
the mechanism below replaces what this entry originally said.

*What it said, and why it was wrong.* It blamed `Engine._dispatch_evals`, which does join its whole
task group before returning. But v6's `run_started` pins `card_driven_selection: true` and
`speculation_depth: 2`, so `_speculation_enabled()` is true and every eval goes through
`Engine._run_card_session` (`engine/speculation.py`) — which delegates to `_dispatch_evals` ONLY when
speculation is off. There are two dispatchers with two different barriers, and the one this entry
described is in the path no run on this box uses. A second claim was wrong the same way: nodes 5 and
6 were NOT "dispatched from the same turn" — node 5 was created 04:01:08, card-7's build was
requested 04:01:11, and node 6 was admitted 04:46:13, i.e. while node 5 was already training.

*What actually happens.* The continuous cross-turn dispatcher this entry asked to be built ALREADY
EXISTS. The Card session admits from `state.pending_nodes()` — the whole folded board, not a per-turn
batch — refills a freed slot on the next poll, runs its own producer, and already commits
`node_created` from the main task inside the dispatch loop. It is switched off by two booleans:
`CardSession.consumer_completed`, set in the `finally` of EVERY eval child, and `yield_outer`. Either
makes `open_for_new_work()` false for ALL slots, and `_card_phase_decide_exit` then will not let the
session return until the LAST eval drains. So the run stops starting work at the FIRST terminal and
still cannot reach the outer boundary until the slowest eval lands. That asymmetry is the defect, and
the code carries its own unresolved `CODEX AGENT` TODO at that line.

Verified on a bounded toy-backend run, not inferred: at the idle moment the probe reads width 2, one
slot free, `terminal`/`budget`/`outer_rebuild` all False, `consumer_completed=True`,
`yield_outer=True`, `open_for_new_work=False`, and `admissible_pending: [2]` — a prefetched,
committed node the engine itself judged admissible, sitting unstarted.
`tests/test_card_budget_refund.py:488` independently documents the same latch.

**The cost, measured across the six width-2 runs on this box.** 115.6 GPU-h of barrier idle against
164.4 GPU-h of work actually done — **82.6% of all second-slot time available while the box was busy
went unused**. Worst single window: `rubert-dr-0807`, 41.8 h at occupancy 1 after having been at 2.
v6 reached width 2 for 1.55 h out of 17.31; v5 never ran two evals at once at all. A SECOND and
larger cost sits beside it — 167.7 GPU-h with no eval running at all, same root (`yield_outer` latches
the producer off during a long eval because the board is only refilled by outer-loop cadences), and
it deserves its own entry.

**What it would take.** The full option table is `docs/33-cross-turn-dispatch-options-2026-08-13.md`.
The recommended shape hoists the eval task group to run scope and needs NO new invariant-#1
exception: eval children are already engine-loop tasks, all eight terminal appends in `evaluate.py`
are lexically inside `async with self._write_lock`, `_record_eval_start_boundary` stays at the
dispatch decision, and resume is already written — `EV_NODE_EVAL_STARTED` exists precisely to rebuild
the inflight set. The real cost is honest and specific: `_proposal_authority_seq`'s quiet window is
lost for the outer `creates` branch, and the recommended answer is to keep that branch gated on
quiescence rather than widen the fence, because a node terminal genuinely does carry selection
authority.

**Do first regardless of the option chosen:** the regression the code's own TODO asks for — an
unequal-duration refill test driving a real engine. Nothing in an 8,900-test suite currently fails
when the second GPU goes dark for two hours.

**Cheap mitigation available today, no code:** an `eval_timeout` closer to the real training cost
bounds the worst-case idle. `eval_parallel: 1` is the honest floor but costs the 1.55 h v6 did use
and forecloses the prefetch design's justification. Neither is a fix.

**Related, and worth stating because it made this window worse:** node 5's batch size was 8192 as
proposed and is 256 as it runs — three repair rounds shrank it 32x chasing an OOM that never
happened (the watchdog-vs-OOM misclassification fixed in `c862045c`). At 2.93 s/it that turned a
~1.5 h training into a ~6 h one, and it is the 6 h that the barrier then idles a GPU against.

## F2 · Give the Developer simple shell commands

**Asked:** "let the developer run simple bash commands (to check compilation, validate data, etc.)."

**Today.** The Developer writes files and declares evaluation *stages*; it has no interactive shell.
`tools/env_inspect.py` is the read-only escape hatch (package version / API / source), and dependency
installation goes through the declarative path (`runtime/deps.py`, `engine/crash_repair.py`). When a
Developer hits something it cannot inspect it improvises around it — the shape the operator saw was
*"Since I have no shell/install ability, the cleanest repair is a small loguru shim module"*.

**What it would take.** The sandbox tiers already exist (`runtime/sandbox.py`), so the execution is
not the hard part; the contract is. A shell tool needs: an allow-list or a tier that cannot mutate the
shared environment (the 2026-08-11 incident where a mid-run `pip install` corrupted a running node is
the cautionary case), a bounded output projection, and a decision about whether its output becomes a
durable event — because if the Developer can act on something not in the log, replay stops
reconstructing the run.

## F3 · Node workspaces on `git worktree`

**Asked:** "move to git worktree?"

**Today.** A node workspace is a materialized copy (`workspace_seeded` records `.[auto]:N tracked`).
On this box that is ~75 tracked files per node onto a geesefs mount.

**Worth measuring first.** The win is disk and seed time; the costs are real and specific here: geesefs
has no exec bit and `os.link` across mounts fails `EXDEV` (both already bite — see the environment
note), a worktree makes every node share one `.git`, whose index lock is *already* the most contended
thing in this repo under concurrent agents, and a node that corrupts its worktree can now damage the
source repo. Measure the seed cost before paying for that coupling.

## F4 · Assistant: an always-on mode

**Asked:** "infinite assistant mode; waiting on statuses; monitoring every N."

**Today.** An assistant turn is bounded twice: `agent_max_turns` (default `0` = unlimited) and a hard
wall-clock `agent_time_budget_s`, which falls back to **300 s** when unset
(`serve/assistant.py::run_turn`). The 300 s floor is the strong suspect behind "the assistant hangs
around 40 tool uses and then a bare tool use arrives as the reply" — on budget exhaustion the loop
returns whatever it has. That part is a bug to confirm and fix.

The three asks on top of it are a different thing: a turn that *waits* for a run to reach a state, and
a turn that wakes on a schedule, both need the turn to outlive its HTTP request. That is a job
(`serve/jobs.py`) plus a durable wake-up record, not a longer timeout — otherwise a browser refresh
silently ends the monitoring the operator asked for.

## F5 · Debug nodes: keep, scope, or remove

**Asked:** "we have an inline-repair limit; when we exceed it a Debug node is created and the fixing
starts again. This looks useless. Either name the cases where it is right, or drop it."

**Today.** Half of this shipped on 2026-08-11: a run in which **nothing has ever worked** now stops
instead of grinding (`systemic_failure_stop`, and see `orchestrator.systemic_failure_stop_reason` for
the 26-hour / 1,705-call measurement that motivated it). What was left was the narrower question the
operator actually asked — once the environment is proven, is a fresh Debug node a better use of a
budget slot than the inline repair that just gave up?

**DECIDED, 2026-08-13 — the Debug node goes.** The operator answered the question rather than asking
for the measurement: *"дебаг ноду нафиг убираем. У нас репейринг есть. Им вот и должно всё
решаться."* And the half that matters more, because it is what the removal would otherwise be evaded
by: **no `draft`/`improve` node may be created that is a Debug node under another name** — i.e. a new
node whose purpose is another attempt at an experiment that just failed. A failure is fixed **inside
the one node, for as long as it takes.**

That "as long as it takes" is not a licence to spin: the bound moves from a counter to a judgment,
which is [F8](#f8-repair-without-a-fixed-bound-stopped-by-judgment-instead-of-by-a-counter) and is
the same change. `systemic_failure_stop` stays as the floor under both. The two must land together —
removing the Debug node while the repair bound is still a fixed count would convert "give up and open
a new node" into "give up", which is strictly worse than today.

Design principle behind both: [`36-agent-driven-decisions-2026-08-13.md`](36-agent-driven-decisions-2026-08-13.md).

## F7 · The Research Atlas: what it uniquely holds, and what it duplicates

**Asked:** "What is the Atlas even for?"

**Today, measured 2026-08-12.** It has four sections, and they are not equal:

| section | also available elsewhere? |
|---|---|
| Concepts seen across runs (+ "observed in one run") | YES — the run list's **Concepts** view is strictly richer: a full `is_a` forest, co-occurrence, a per-concept detail pane and, since this week, the lessons/cases/notes that carry each concept |
| Mixed-evidence claim records | **NO** |
| Claim records | **NO** — 30 rows live on this box; `/api/cross-run/claims` has no other reader |
| Recent proposals + outcomes (the steward curation log) | **NO** |

So three of its four sections are the ONLY home for research claims and for what the paid stewards
proposed and what came of it. The one section that reads as redundant is its concepts list, which a
different view outgrew.

**Recommendation, for the owner to accept or reject.** Keep it and narrow it: drop the concepts
section in favour of a link to the Concepts view, and rename the destination to what it uniquely
holds — claims and curation. Deleting the Atlas outright would take the claim ledger and the steward
outcomes offline with it, which is not what the question was about. Renaming is not cosmetic here:
"Research Atlas" is what makes an operator expect the concept map and then find a worse one.

**ACCEPTED, 2026-08-13.** The operator agreed the rename is the point. So: drop the concepts section
in favour of a link to the Concepts view, and rename the destination after the three things it is the
only home for — the claim records, the mixed-evidence records, and the steward curation log. The name
is the deliverable here, not a layout tweak.

## F6 · Conversation trace: usability, and the steps that are invisible — SHIPPED 2026-08-13

**Asked:** "make the conversation trace more convenient. You cannot see traces from earlier versions
of a node (bugs happened and repair kicked in)."

**What this actually was.** The first reading of it — "earlier GENERATIONS are invisible" — was half
right and shipped on 2026-08-12 as the Inspector's attempt picker (`6640349e`). Measuring the node
the complaint came from showed the other half, and it is the bigger one:

* `runs/rubert-dr-0804` node 1 has **14,507 spans across 2,345 inline repairs over 3 h 50 m, and ALL
  of them are lifecycle generation 0.** `Node.attempt` is bumped by `node_reset`, never by inline
  repair (`core/models.py::Node.attempt` says so; `traceProjection.js` and `docs/guide/ui.md` both
  claimed the opposite until today). So on the very node the operator was reading, the attempt picker
  has one option and does not render at all.
* The window those routes read is a **TAIL**, so widening is the same tail extended. That node's
  512-span default window covers its **last 7.6 minutes** and the 4,096-span ceiling its **last
  59.3** — 74 % of the experiment, including every early repair where the bug first showed,
  unreachable at any `limit`. Raising the ceiling is not the fix and was not done: the conversation
  costs 3.4 ms/span in `full_spans_for_node` plus 0.9 ms/span in `build_conversation`, all on the
  request thread (17.3 s at the ceiling on that node, ~64 s with no ceiling).

**What shipped.**

1. **The window MOVES.** `?before=<span_id>` on `/nodes/{n}/trace` and `/nodes/{n}/conversation`:
   the same `limit` spans, ending at a chosen step. Same rows read, same cost, same ceiling. An
   anchor the run's index cannot place is refused (409 `trace_anchor_unknown`), never degraded to the
   tail — answering with the newest spans under an older episode's label is worse than an empty
   panel. It is material in both the index's window revision and the route's ETag, so no conditional
   read can answer one anchor with another's body.
2. **The node publishes where to point it.** `GET /nodes/{n}/episodes` — every band the conversation
   reads, with none of their contents, each carrying its `anchor`, its recorded repair ordinal, its
   duration and its triage reason. It IS `_conversation_bands`, so the map cannot drift from the
   surface it maps, and it is derived from the in-memory light index with no `spans.jsonl` bytes at
   all: 7,048 episodes in 82 ms on that node.
3. **The render caps stopped throwing away work already paid for.** `_CONVERSATION_TURN_CAP = 256`
   and `_CONVERSATION_STAGE_CAP = 64` were flat numbers scaled by the window; on that node a
   512-span window derived 256 bands and 425 turns and rendered 64 and 105. They are now the
   window's own arithmetic bound (≤1 band and ≤2 turns per span), so what the window READ is what
   the operator READS. Re-measured 2026-08-13, capped vs uncapped at the same window:
   `build_conversation` 0.17 s vs 0.18 s at x1 and 1.42 s vs 1.39 s at the x8 ceiling — **0 ms**, as
   `CLAUDE.md` recorded. It costs bytes: 193 KB -> 778 KB at the default window.
4. **A VISIBLE control** (`ui/src/traceEpisodeModel.js` + `Inspector.jsx::TraceEpisodes`), beside the
   attempt picker and explicitly not its substitute — attempt selects a lifecycle, this selects a
   position inside one. It steps by ordinal rather than listing 2,345 rows, and the map loads when
   the control is opened.

Seeking node 1 to its first repair now returns `propose → implement → train → triage →
inline_repair #1` — the beginning of the experiment, which no surface could open before.

Related and already fixed: run-level agents (the Researcher above all) had no surface at all — see the
new Operations panel.

## F8 · Repair without a fixed bound, stopped by judgment instead of by a counter

**Asked, 2026-08-13:** *"я бы хотел чтобы репейринг у нас был по сути бесконечный, но стопался бы
каким-нибудь LLM критиком и самим девелопером, что типа я фиг знает как чинить."* Paired with the
[F5](#f5--debug-nodes-keep-scope-or-remove) decision to delete the Debug node: everything is fixed
inside the ONE node, for as long as it takes, and nothing may open a fresh node to have another go.

**Today.** The transition from "keep repairing" to "stop" is a COUNTER. A counter cannot distinguish
a repair loop converging on a fix from one that has been rewriting the same line for an hour, and the
two recorded disasters are both cases where the counter was the only thing looking: the 2,345-repair
runaway on `rubert-dr-0804` (whose 369 distinct error signatures defeated the anti-stuck counter
because the underlying `transformers`/`torch` break renamed its symbol every attempt), and the three
rounds of batch-halving on v6 node 5 chasing an OOM that never happened.

**Two signals that bear directly on the question already exist and are not used for it.** The
Developer knows when it is out of ideas — nothing asks it. A critic can see whether successive
attempts address different causes or circle one — nothing runs one. Both are cheap next to the GPU
hours a bad stop decision costs in either direction.

**What it must not become.** An unbounded spend with no floor. The bound moves from a count to a
budget plus a judgment; `systemic_failure_stop` (a run where nothing has ever worked stops) remains
underneath, and the money ceiling stays a hard number. The judgment decides *whether to keep going*,
never *what the result was* — the line is drawn in
[`36-agent-driven-decisions-2026-08-13.md`](36-agent-driven-decisions-2026-08-13.md).

**Ordering constraint.** F5 and F8 land together. Removing the Debug node while the repair bound is
still a fixed count turns "give up and open a new node" into plain "give up".

---

## Verified-but-unfixed bugs from the same session

Kept here so they are not lost; each is a fix, not a feature.

* **A timed-out control command makes a run PERMANENTLY uncontrollable.** Reproduced end to end on
  `rubertlite-dr-unified-v2`, 2026-08-11, and it is worse than a dead chip in the timeline. Three
  compounding faults:

  1. The pause postcondition is `paused_and_stopped` — it requires the engine PROCESS to exit, which
     a pause cannot deliver while a multi-hour evaluation is in flight. On exactly the runs where
     pausing matters it can only ever time out. Here it did, on 2026-08-10, ~20 minutes after a pause
     that had actually landed within one second.
  2. From then on `reject_if_active` refused **every** later control with `command_retry_required`.
     The run could not be paused, stopped or steered at all.
  3. The documented remedy — `POST /commands/{id}/retry` — returned `accepted`, then `executing`,
     then `timed_out` again, and **no `pause` event was ever appended**. The retry re-drives the
     ORIGINAL command, whose `event_seq` points at an intent that a later `resume` already consumed,
     so it re-observes a superseded event instead of issuing a new pause. The run stayed in phase
     `search` throughout.

  Net effect: after 30 hours, 8 failed nodes, 0 evaluated and 2,323 provider calls, the only way to
  stop the run was `kill` on the engine PID. A control plane that fails closed must still leave one
  path forward; this one leaves none. Fixing it means at minimum: a pause postcondition that observes
  the PAUSE (the effect the operator asked for) separately from process exit, a retry that mints a
  FRESH intent when the old one has been superseded, and a UI that surfaces the remedy instead of a
  frozen "Stop requested…" chip.
* **Node build and resume are silent.** Long waits with nothing on screen; the operator asked for
  "maximum transparency". The pieces exist (`ui/src/buildingModel.js`, the `node_building` event) but
  nothing streams what phase the build is in.
* **Cross-run memory is mostly toy residue.** Measured through `/api/memory` on 2026-08-11: 161
  lessons of which 7 come from a real task, 163 notes of which **71 are distinct** (56% duplicates,
  one repeated 23 times), 10 cases of which 9 are test fixtures. Retrieval over that store is what
  produces the "changing x and y parameters regressed the metric" card the operator screenshotted.
