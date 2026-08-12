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
the pool size or a per-experiment ceiling. The only channel that carries that today is the operator's
goal prose — v5's said "two H200 GPUs are available", and the Researcher reasonably read it as "you
may have both". A role asked to size a request against a budget it cannot see will get it wrong at
some rate, and there is no reason for that rate to be non-zero.

**What it would take.** Either (a) a hint carrying `max(1, pool // eval_parallel)` into both
Researcher prompts — which means the `RESEARCHER_HINT_ATTRS` registry, both readers, every
delegating wrapper and the forwarding tests, per the registry rule in CLAUDE.md; or (b) clamping
the declared footprint to the per-experiment share instead of to the pool, which decides that the
operator's `eval_parallel` outranks the Researcher's declaration. (b) is one line and a real policy
change — it would fence a legitimately multi-GPU experiment onto one device — so it is a decision,
not a fix. (a) is the honest one and is the larger job.

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
the 26-hour / 1,705-call measurement that motivated it). What is left is the narrower question the
operator actually asked — once the environment is proven, is a fresh Debug node a better use of a
budget slot than the inline repair that just gave up? Nobody has measured it. The evidence to collect
is already in the logs: for every node whose triage said `abandon`, did the debug child that followed
reach a metric? Answer that before changing the operator.

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

## F6 · Conversation trace: usability, and the generations that are invisible

**Asked:** "make the conversation trace more convenient. You cannot see traces from earlier versions
of a node (bugs happened and repair kicked in)."

**Today.** `/nodes/{n}/trace` and `/nodes/{n}/conversation` are scoped by `(node_id, generation)`, and
the UI reads the CURRENT generation. Earlier generations are in the span log and reachable by
`trace_id`, but no surface offers them. Separately, `traceview._CONVERSATION_TURN_CAP = 256` is what
truncates the reasoning the operator wants to read; `CLAUDE.md` records the measurement that lifting
it at a fixed span window costs **0 ms** of server time.

Related and already fixed: run-level agents (the Researcher above all) had no surface at all — see the
new Operations panel.

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
