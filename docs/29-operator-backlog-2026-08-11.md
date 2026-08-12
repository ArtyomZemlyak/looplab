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
