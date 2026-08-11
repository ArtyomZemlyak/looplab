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

* **A timed-out control command makes a run uncontrollable.** Reproduced live 2026-08-11: a `pause`
  from the previous day sat at `status: timed_out`, and `reject_if_active` then refused *every*
  subsequent control with `command_retry_required`. The documented remedy (`POST
  /commands/{id}/retry`) works, but nothing in the UI surfaces it, so the run reads as wedged. The
  deeper cause is that the pause postcondition is `paused_and_stopped` — it requires the engine
  PROCESS to exit, which a pause cannot deliver while a multi-hour evaluation is in flight, so on
  exactly the runs where pausing matters it can only ever time out.
* **Node build and resume are silent.** Long waits with nothing on screen; the operator asked for
  "maximum transparency". The pieces exist (`ui/src/buildingModel.js`, the `node_building` event) but
  nothing streams what phase the build is in.
* **Cross-run memory is mostly toy residue.** Measured through `/api/memory` on 2026-08-11: 161
  lessons of which 7 come from a real task, 163 notes of which **71 are distinct** (56% duplicates,
  one repeated 23 times), 10 cases of which 9 are test fixtures. Retrieval over that store is what
  produces the "changing x and y parameters regressed the metric" card the operator screenshotted.
