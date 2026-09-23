"""Training-log monitor — a per-eval background observer of the LIVE training log (I-series watchdog
family, sibling of `runtime/sandbox._StageHealthMonitor`).

A repo eval's declared training stage runs for a long time (often multi-hour) while the engine's async
loop is otherwise idle — `_evaluate` runs the eval in a worker thread. This mixin adds a periodic task
in that same task group (alongside the mid-eval intervention `_watch`) that tails the stage's live log
and implements the complete bounded phase stack:

- Phase 0: read the live-log tail on a timer and emit a `train_monitor` TRACE span;
- Phase 1: when a client is available, classify the bounded digest and append fold-ignored
  `train_monitor_alert` diagnostics for non-healthy verdicts;
- Phase 2: self-pace later observations from the run budget, healthy streak, and bounded model hint;
- Phase 3: when `train_monitor_kill` is on — ON in the product `Settings` since 2026-08-04, OFF in the
  bare-library `EngineOptions` (claim `train-monitor-kill-ships-on`) — claim a CONFIRMED, sufficiently
  confident `broken` verdict about an IDENTIFIED training stage — one the engine's own MEASURED loss
  trajectory does not contradict — and reuse the evaluation cancel/tree-kill path. The node still
  terminates once with `reason=monitor_broken`.

WHO OWNS WHICH QUESTION. The judge is asked what a TAIL can answer — is anything anomalous, what is
this run saying about itself — and the engine owns "is it still descending", because that is a
statement about the whole curve and the tail is ~30 seconds of it (see the trajectory section of
`engine/loss_trajectory.py`, measured on `runs/rubertlite-dr-unified-v7`). The measurement is
derived from the candidate's own log text, so it is held to `engine/metric_salvage.py`'s rule: it
may REFUSE an intervention and may never authorize one, and it reaches no metric, champion or
selection record.

Which log is judged is part of the contract, not an implementation detail: the eval writes one
`<stage>.log` per stage plus `setup.log` (dep install) and, on the single-command path, `eval.log`.
Only a stage that runs the candidate's own work can be judged by a TRAINING-health prompt — see
`eval_log_plan` / `resolve_stage_log`.

With intervention off, the monitor never changes the metric champion or node lifecycle. Diagnostics can
still feed the separately configured watchdog-reflection prompt cue on a later proposal.

WHAT LIVES HERE AND WHAT DOES NOT (review 2026-09-22, ENG3-13 / doc 50 EM-06). This module is the
watchdog itself: its verdict schema and prompts, the tool builders its judge — and the triage and
stage-check judges — LOOK with, and the `TrainingMonitorMixin` loop. Its pure halves live beside it
and are re-exported here as the SAME objects: `engine/loss_trajectory.py` (what the engine measures
from the log), `engine/monitor_gates.py` (the kill / repair / arming decisions and their constants)
and `engine/eval_log_plan.py` (which log is whose, and the attempt-bounded readers). A patch on this
module reaches only what THIS module's code reads (`tests/test_train_monitor_split.py`).

Design constraints this file must keep (engine invariants):
- **The runtime never calls the LLM** (layering: `runtime` imports nothing above itself), so an LLM-driven
  watchdog CANNOT live in the sandbox — it lives here, in the engine, reading the log FILE the sandbox
  already writes (`_tee_drain(log_path=…)`).
- Verdicts are DIAGNOSTIC events (fold-ignored), so their thread-dependent splice position never changes
  folded state. Replay reads the ordinary terminal event and NEVER re-invokes the monitor LLM.
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field

from looplab.core.llm import BudgetExceeded
from looplab.core.llm_broker import in_llm_lane
# The log-role vocabulary is stamped on the DURABLE `EV_TRAIN_MONITOR_ALERT` row, so it lives in
# `events/types.py` where readers below the engine (`events/digest.py`'s `watchdog_reflection`) can
# name a role without importing the engine (layering: `events` imports only `core`). Imported here
# and re-exported, because `train_monitor.LOG_ROLE_*` is the spelling engine code and tests use.
# `events.types` imports nothing, so this module-level import cannot become a cycle.
from looplab.events.types import (  # noqa: F401 — re-exported
    LOG_ROLE_AMBIGUOUS,
    LOG_ROLE_SCORE,
    LOG_ROLE_SETUP,
    LOG_ROLE_TRAINING,
    LOG_ROLE_UNKNOWN,
    LOG_ROLE_WORK,
)
# THE PURE HALVES, split out of this module (review 2026-09-22, ENG3-13 / doc 50 EM-06) and imported
# back WHOLE: every name is re-exported as the SAME object, so each `train_monitor.<name>` spelling
# the engine, the bench, `serve/attention.py` and the suite use keeps resolving. What this module's
# own code reads through these bindings — the loop's tuning constants, `read_training_tail`,
# `stamp_projected_overrun` — is also what a patch on `train_monitor` still reaches; a name only a
# sibling reads must be patched THERE (`tests/test_train_monitor_split.py`).
from looplab.engine.loss_trajectory import (  # noqa: F401 — re-exported
    declared_schedule_shortfall, LossTrajectory, LossTrajectoryTracker, LossWindow,
    OVERRUN_BEYOND_BAR_KEY, OVERRUN_BEYOND_BAR_KEYS, parse_loss_points, projected_overrun_s,
    schedule_reading, ScheduleReading, STAGE_CHECK_TRAJECTORY_KIND, stage_contract_context,
    stage_trajectory_note, stamp_projected_overrun, summarize_loss_window, summarize_trajectory,
    training_log_digest, trajectory_acquits_stage_check, trajectory_context,
    TRAJECTORY_MOVED_DIRECTIONS, trajectory_row, trajectory_vetoes_kill, wall_unreachable,
    _anomaly_of, _BREAK_RE, _eta_of, _fmt_loss, _GRAD_NORM_NONFINITE_RE, _latest_progress,
    _LOSS_POINT_RE, _LOSS_VALUE, _MAX_TRAJECTORY_WINDOWS, _OVERRUN_ALERT_FLOOR_FRACTION,
    _OVERRUN_ALERT_FLOOR_S, _PROGRESS_RE, _SCHEDULE_EPOCH_RE, _SCHEDULE_MAX_HALF_WIDTH,
    _TRAJECTORY_EXPLOSION_RATIO, _TRAJECTORY_MIN_ABSOLUTE_DROP, _TRAJECTORY_MIN_RELATIVE_DROP,
)
from looplab.engine.monitor_gates import (  # noqa: F401 — re-exported
    citation_authenticates, claim_watchdog_kill, kill_superseded_by, last_lifecycle_row,
    MONITOR_REPAIR_REASON, next_monitor_sleep, should_monitor_kill, should_monitor_repair,
    _confirmation_would_act, _HEALTHY_BACKOFF_K, _KILL_ELIGIBLE_ROLES, _MAX_MONITOR_LLM_CALLS,
    _MONITOR_ARM_MAX_LOOKS, _MONITOR_ARM_TTL_S, _MONITOR_CADENCE_CAP_S, _MONITOR_CONFIRM_DELAY_S,
    _MONITOR_KILL_CONFIRM_TICKS, _MONITOR_SAME_DIGEST_RETRIES, _NON_TRAINING_ROLES,
    _normalize_monitor_confidence,
)
from looplab.engine.eval_log_plan import (  # noqa: F401 — re-exported
    active_training_log, ActiveStageLog, attempt_byte_floor, eval_log_plan, EvalLogPlan,
    monitor_log_sources, monitor_stage_context, read_stage_trajectory, read_training_tail,
    read_training_tail_raw, resolve_stage_log, snapshot_training_logs, stage_check_trajectory,
    STAGE_TRAJECTORY_MAX_CHUNK, STAGE_TRAJECTORY_MIN_CHUNK, STAGE_TRAJECTORY_WINDOWS,
    StageDeclaration, training_authority_spent, TrainingLogCursor, TrainingLogSnapshot,
    _CURSOR_PROBE_BYTES, _file_identity, _is_scorer_stage, _log_name_key, _log_path_key,
    _RESERVED_SCORER_NAMES, _SETUP_LOG, _SINGLE_COMMAND_LOG,
)

# The verdict schema the log observer returns. `status` drives everything downstream: a non-"healthy"
# verdict becomes an EV_TRAIN_MONITOR_ALERT (Phase 1) and, later, a gated early kill (Phase 3, "broken"
# only). Field descriptions are part of the schema the model sees — they ARE the classification contract.
class TrainingVerdict(BaseModel):
    status: Literal["healthy", "watch", "broken"] = Field(
        description="healthy = training is progressing normally (loss decreasing or stable, no errors); "
                    "watch = something looks off but not necessarily fatal (slow, plateauing, warnings); "
                    "broken = clear evidence the run is WASTED and cannot recover — diverged loss, a silent "
                    "CPU fallback while a GPU was expected, data-loader errors repeating in the loop, loss "
                    "stuck at its initialization value (not learning), or an uncaught exception.")
    fault: Literal["implementation", "hypothesis", "environment", "unknown"] = Field(
        default="unknown",
        description="Only meaningful when status is 'broken': WHOSE fault it is, because the two get "
                    "opposite treatments. 'implementation' = the code or its configuration is wrong "
                    "and a fix is available — a parameter set to a value the log itself shows is "
                    "absurd, a loss that cannot descend as written, a normalization or reduction "
                    "applied to the wrong axis, a data loader feeding the same batch, a checkpoint "
                    "never saved. This STOPS the run and hands it back for REPAIR, so the experiment "
                    "is retried once the bug is fixed. 'hypothesis' = the code is doing exactly what "
                    "it was told and the IDEA is what failed; the run's poor result is a real "
                    "finding and must be recorded as one, not fixed away. 'environment' = neither "
                    "(a missing device, a broken mount, an OOM). 'unknown' = you cannot tell from "
                    "the evidence — say that rather than guessing, it is the safe answer. Prefer "
                    "'implementation' ONLY when you can name the specific thing to change; a "
                    "hypothesis wrongly called a bug costs a repair round, a bug wrongly called a "
                    "hypothesis records a verdict about an idea that was never actually tested.")
    reason: str = Field(description="One short sentence naming the SPECIFIC log evidence for the status.")
    confidence: float = Field(default=0.5, description="Confidence in the status, 0.0 to 1.0.")
    evidence_source: Literal["code", "log", "none"] = Field(
        default="none",
        description="WHERE the thing you are pointing at lives, so the engine can go and look at it "
                    "itself. 'code' = a file in this run's workdir that you READ with your tools. "
                    "'log' = a place in a stage log you read. 'none' = you are reasoning from the "
                    "tail you were handed and cited nothing checkable — which is an honest answer "
                    "and the right one when it is true.")
    evidence_locator: str = Field(
        default="",
        description="WHERE EXACTLY, as `path:line` or `path:start-end` relative to the workdir — "
                    "for example training/loss.py followed by a colon and the line number. "
                    "THIS IS RE-READ BY THE ENGINE: "
                    "a locator pointing at a file that is not there does not make your verdict "
                    "wrong, but it does mean nobody can re-derive it, so cite something you "
                    "actually opened. Leave empty when evidence_source is 'none'.")
    recheck_after_s: Optional[float] = Field(
        default=None,
        description="Optional: how many seconds until you want to look again. Use a LARGER value when the "
                    "run is healthy and steady, so a boring, well-behaved run is not watched closely. You "
                    "cannot look sooner than the automatic cadence (values below it are ignored); the "
                    "automatic pace already tightens on shorter runs. Omit to keep the default cadence.")


# The observer's framing. A contract: it fixes the observer's role (the engineer who wrote THIS loop),
# what it may rely on (log evidence, NOT the unknown final metric), and its bias (flag EARLY, before the
# whole budget is burned — but do not cry wolf on a normal slow-but-progressing run).
_MONITOR_SYSTEM = (
    "You are the ML engineer who wrote this training script, watching its LIVE log during a long run to "
    "catch a wasted run EARLY — before its whole (often multi-hour) time budget is spent. Judge ONLY from "
    "the log evidence; the final metric is not known yet, so do not guess it. A run that is merely slow or "
    "plateauing but still progressing is 'watch', not 'broken' — reserve 'broken' for clear, cannot-recover "
    "evidence. Be concise and specific about the evidence you saw. When you do call something broken, "
    "also say WHOSE fault it is (`fault`): a bug in the code or its configuration is REPAIRED and "
    "retried, while a sound implementation of a bad idea is a real result and is recorded as one. "
    "Look before you attribute — the run's own log is where its parameters, its device and its data "
    "shapes were echoed.")


# The sentence that tells the judge the tail is not all it may have. Spliced ONLY when tools are
# actually wired (`monitor_log_tools`), at the same position pattern as `stage_context` and
# `trajectory_text`, so `train_monitor_tools=false` restores the historical message byte for byte.
#
# It NAMES the failure it exists to end, because a model handed both a tail and a tool still reasons
# from the tail: on v7 the spliced tail was ten loss values ~30 seconds apart on a five-hour run, and
# the verdict "pinned at ~23.0 … no learning trend from its initialization value" was a correct
# reading of exactly that. Telling it the window is small is not the same as telling it the window is
# *too small to answer the question it is being asked* — so this says the second thing.
_LOOK_INVITATION = (
    "YOU CAN LOOK FURTHER. The tail below is a SHORT window — often under a minute of a run that has "
    "been going for hours — and 'converged', 'stuck at initialization' and 'still descending slowly' "
    "are indistinguishable inside it, because the difference is smaller than the step-to-step noise. "
    "Before you call anything broken, USE YOUR TOOLS: `metric_series` for what the loss has actually "
    "done over the whole run at a granularity you choose, and `read_log` to tail further back, read "
    "the run's start, or search for a traceback. If the tools disagree with the tail, the tools have "
    "more evidence. You can also READ THE CODE this run is executing — `list_dir`/`find_files` to "
    "see what is in the workdir, `read_file` for a whole file, `grep` to find where a symbol, flag "
    "or config key is set. That is what tells a BUG from a bad idea: a loss that is frozen because "
    "the objective cannot descend as written looks exactly like one frozen because the idea does "
    "not work, and only the source says which. Read before you attribute `fault`, and name what "
    "you found.")

# The tool-loop turn budget for ONE monitor tick. Deliberately well below `trust.judge.JUDGE_MAX_TURNS`
# (15): this judge fires up to `_MAX_MONITOR_LLM_CALLS` times per node on a timer, so a turn budget is
# multiplied by ~200 in a way the two one-shot verifiers' never is. Six was enough for the shape the
# invitation asked for when it was logs only — a whole-run series, a narrower one, a search, and
# the emit — and a loop that spends it degrades to `parse_structured` on the same messages rather
# than to nothing. NINE since 2026-08-18, because the invitation now asks for one more THING and
# that thing takes more than one turn: attributing `fault` means locating the file that sets the
# parameter (a `grep`), reading it (a `read_file`, possibly a second page), and doing that WITHOUT
# giving up the log evidence the verdict is primarily about. A budget that forces the judge to
# choose between looking at the curve and looking at the code produces exactly the guess the
# attribution exists to replace. Still far below `trust.judge.JUDGE_MAX_TURNS` (15) and still
# multiplied by ~200 ticks per node, which is why it moved by three and not by ten.
_MONITOR_LOOK_TURNS = 9


# THE CHECKLIST, and it is the difference between an invitation and an obligation. `_LOOK_INVITATION`
# above has told this judge to read the source since 2026-08-18, and it DOES: five of node 3's
# sixteen `broken` verdicts on `e5small-dr-unified-v4` cite a file in their prose, including
# the line of the candidate's own loss module that declares the -1e9 masked-logit sentinel which
# made that run's objective unbounded below. The judge found the mechanism by reading the code.
#
# What it could not do was make that finding COUNT. The citation lived in `reason`, which is prose,
# so nothing re-resolved it, and the engine could not tell a verdict that had opened the file from
# one that had invented the line number. A deterministic rung that can observe only "the number is
# going down" then overruled it thirteen times.
#
# So the citation is a FIELD now, and this says what earns it. It asks for the three things
# `failure_diagnosis.evidence_citation_resolves` can actually check and refuses to ask for more:
# WHICH file, WHICH line, and that the judge opened it. It does not ask for more confidence — a
# wrong citation is still a wrong verdict. It asks for a claim somebody else can re-derive.
_CITE_INVITATION = (
    "IF YOU CALL THIS BROKEN AND BLAME THE IMPLEMENTATION, CITE THE LINE. Fill `evidence_source` "
    "and `evidence_locator` with the file and line you actually opened, written as the path "
    "inside this run's workdir followed by a colon and the line number — not a description of it. "
    "THE ENGINE RE-READS WHAT YOU "
    "CITE. A verdict that names the mechanism in a file the engine can open carries weight prose "
    "cannot, because a later reader can go and check it; a verdict that cites nothing is read as a "
    "reading of the tail, which is what it is.\n"
    "WORK THROUGH THIS BEFORE YOU CONCLUDE:\n"
    "  1. `metric_series` over the WHOLE run — what the loss has actually done, not what the last "
    "minute of it looks like.\n"
    "  2. If the numbers are impossible FOR THIS OBJECTIVE — a contrastive loss below zero, a loss "
    "in the millions, a cross-entropy above ln(vocab) — `grep` for the loss class and `read_file` "
    "it. The arithmetic either can or cannot produce what you are seeing, and only the source says "
    "which. A sentinel constant reaching the reduction is the usual answer.\n"
    "  3. `grep` for the parameters the log echoed, in the config AND in the training script. They "
    "disagree more often than anyone expects, and the script is what ran.\n"
    "  4. Only then decide, and say in `reason` what you found and where.\n"
    "Take the turns. The run costs hours; this costs seconds.")


def needs_log_snapshot(engine, eval_spec) -> bool:
    """Must this attempt take the pre-attempt log snapshot and resolve its log plan?

    A NAMED RULE because it has three independent readers with different lifetimes and the third one
    cannot take its own: the two watchdogs read the logs WHILE the attempt runs, and the repair
    triage reads them AFTER it has died. A snapshot is a "before" — deriving it lazily at the failure
    is too late, because by then the attempt's own bytes are in the file and there is nothing left to
    take a before OF, so every `LogSource.floor` would be 0 and a repairer would read its
    predecessor's curve as its own. So the decision has to be made where the attempt STARTS, on
    behalf of a reader that has not been asked for anything yet.

    It was an inline `or` of the first two clauses in `engine/evaluate.py::_evaluate`, and this
    change would have made it three — i.e. a rule no test could reach without driving a whole
    sandboxed eval. Driven on a throwaway copy of the tree, deleting the repair clause from that
    inline form left every guard in the suite green, which is why it is a function now. Every disjunct is gated on `eval_spec` because only the
    command-eval path writes the per-stage `<stage>.log` any of this is about; the `solution.py`
    paths (toy/dataset) have no such log and must keep paying nothing.
    """
    if not eval_spec:
        return False
    if getattr(engine, "_train_monitor", False):
        return True
    if getattr(engine, "_asha_live", False) and isinstance(eval_spec, dict):
        return True
    # Every default here is the value a real Engine settles the switch to (the two watchdogs off,
    # the repair judge's tools on) — a double must not take a snapshot decision no real Engine
    # would (review 2026-09-22, ENG1-03; `tests/test_engine_knob_defaults.py`).
    return bool(getattr(engine, "_repair_log_tools", True))


def _log_query_tools(workdir, log_plan, log_snapshot):
    """The `LogQueryTools` provider over THIS eval's stage logs, or None when there is no nameable
    log yet. The ONE construction both LOOK paths use — the gate is the caller's.

    It exists as its own function because there are now two roles that may look and they must not be
    able to disagree about WHAT is lookable. The source map is `monitor_log_sources` in both cases —
    one derivation of rule 2's boundary (`eval_log_plan` over the resolved pipeline) and one reading
    of rule 1's floor (`attempt_byte_floor`), so a repairer diagnosing attempt N reaches exactly the
    bytes the watchdog was reading while attempt N ran, and no byte of attempt N-1.

    The map stays a CALLABLE even on the repair path, where the eval is already over and it cannot
    move. That is deliberate: a second, frozen construction here would be a second answer to "which
    logs exist", and the whole point of this function is that there is one.

    The "is there anything to name" probe is the FIRST derivation and the provider is handed it, so
    the construction costs one derivation and not two. It matters because the derivation is not
    cheap: `monitor_log_sources` globs the workdir and opens + fstats every stage log, with
    `attempt_byte_floor` probe-READING each one, on mounts where a stat can cost most of a second.
    `LogQueryTools` holds it for the rest of THIS call and drops it again (`_one_derivation`), so a
    later tool call still re-derives — a new stage log appearing mid-eval is exactly what the
    callable is for.
    """
    from looplab.tools.log_tools import LogQueryTools
    first = monitor_log_sources(workdir, log_plan, log_snapshot)
    if not first:
        return None
    pending = [first]

    def resolve():
        # The probe's own answer serves the first read; every later one re-derives, because the
        # active stage log MOVES during an eval and a frozen map answers about the wrong phase.
        return pending.pop() if pending else monitor_log_sources(workdir, log_plan, log_snapshot)

    return LogQueryTools(resolve)


def monitor_tools(engine, workdir, log_plan=None, log_snapshot=None):
    """Everything this tick's judge may look with: the eval's own logs AND the code that wrote
    them. None when neither is available, which is what `structured_judge` reads as "no tools" and
    is the historical one-shot call byte for byte.

    Composed here rather than at the call site so the two watchdogs cannot come to disagree about
    what looking means — the same reason `_log_query_tools` exists as one function. `CompositeTools`
    de-dups by tool NAME with the first provider winning, and the logs go first deliberately: they
    are the evidence the verdict is primarily about, and a name collision must never silently
    shadow `read_log` with a general file reader that knows nothing about attempt floors.
    """
    providers = [p for p in (monitor_log_tools(engine, workdir, log_plan, log_snapshot),
                             monitor_code_tools(engine, workdir)) if p is not None]
    if not providers:
        return None
    if len(providers) == 1:
        return providers[0]
    from looplab.agents.tool_loop import CompositeTools
    return CompositeTools(providers)


def monitor_code_tools(engine, workdir):
    """The read-only CODE scouts the live judge may look with, rooted at the NODE WORKDIR — or None
    when the tools are off or there is no workdir to root them at.

    WHY THE JUDGE NEEDS THEM. It is asked a question the log alone often cannot answer. `fault`
    splits a `broken` verdict into "the code is wrong" and "the idea is wrong", and those get
    opposite treatments — one is repaired and retried, the other is recorded as a real negative
    result. A frozen loss looks identical either way from the outside; what tells them apart is
    whether something in the running code cannot work as written. Measured on
    `e5small-dr-unified-v2`: nodes 2 and 4 spent ~10 GPU-hours reaching 0.0 and 2e-05 under 48
    `broken` verdicts between them, every one correct about the symptom and none able to say
    whether a reduction, a normalization or the hypothesis was at fault — because the judge could
    read every byte of the log and not one line of the program that wrote it.

    ROOTED AT THE WORKDIR, and that is the whole safety argument as well as the accuracy one:

    - it is the code that is ACTUALLY RUNNING. The Developer's own scouts are rooted at the
      editable SOURCE, which is a different filesystem from the one the eval sees — a distinction
      that already cost a run (`runs/rubert-dr-0807` node 2 died on a missing
      `<workdir>/looplab_eval.py` while the repair session's `read_file` cheerfully returned it).
      A judge reading the source tree would be answering about a program that is not the one on
      trial.
    - it is the one region that provably holds only what THIS node produced, which is what
      `monitor_log_sources` already relies on and what `read_allowlist`/`read_fence` already grant.
      No other node's workspace, no operator secret outside it, and no engine source.
    - the direction of harm is favourable. Everything the judge reads here is the candidate's own
      text, exactly like the log it has always read — and the widest thing that text can now buy is
      the CHEAP action: `fault="implementation"` routes to a repair-stop, whose cost when wrong is
      one restart. It cannot mint a metric, a champion, a violation or a selection; the terminal
      kill keeps its own narrow gate, and `hypothesis` — the answer that ends a node — is the one
      the code cannot argue itself into, because refusing to blame the implementation is what
      leaves it standing.

    `RepoScoutTools` is reused rather than re-derived for the reason `_log_query_tools` gives about
    its own single construction: two answers to "what is readable" is how two roles come to
    disagree. It is already the right shape for this mount — path-safe, secret-filtered, bounded
    per page and per walk, and it already skips the gigabyte directories a trainer workdir carries
    (`ckpt`, `checkpoints`, `wandb`, `lightning_logs`), which on geesefs is the difference between
    a grep and a stall.

    The switch's default is the True a real Engine settles it to (review 2026-09-22, ENG1-03).
    """
    if not getattr(engine, "_train_monitor_tools", True):
        return None
    try:
        root = Path(workdir)
        if not root.is_dir():
            return None
    except OSError:
        return None
    from looplab.tools.reposcout import RepoScoutTools
    return RepoScoutTools(roots=[str(root)], default_root=str(root))


def monitor_log_tools(engine, workdir, log_plan=None, log_snapshot=None):
    """The log tools this tick's judge may LOOK with, or None to keep the historical one-shot call.

    A FREE FUNCTION taking the engine, not a mixin method, and that is the point — the same reason
    `engine/speculation_gate.py`'s envelope is one. BOTH watchdogs need it, so as a method it has to
    live on a mixin one of them does not have: on `TrainingMonitorMixin` an ASHA-only object raises
    AttributeError, and `_monitor_asha`'s own per-tick containment `except` swallows that into a
    watchdog that silently stops producing verdicts for the rest of a multi-hour eval.
    `tests/test_asha_monitor.py`'s `_AshaStub` is exactly that object and is what caught it. Moving it
    to `SharedEngineMixin` only moves which stub breaks; a free function depends on no MRO at all, and
    `getattr` below is total over a partially-built engine.

    None when `Settings.train_monitor_tools` is off or the eval has written no nameable log yet — and
    `None` is exactly what `structured_judge` treats as "no tools", so the off path is the plain
    `parse_structured` both judges have always made, byte for byte.

    The provider is built PER TICK and its source map is a CALLABLE, because both halves move: a new
    stage log appears when the pipeline advances, and the log the judge is being asked about changes
    with it. A provider frozen at eval start would answer a question about `train.log` while the model
    read `setup.log`.
    """
    if not getattr(engine, "_train_monitor_tools", True):     # the settled default (ENG1-03)
        return None
    return _log_query_tools(workdir, log_plan, log_snapshot)


def repair_log_tools(engine, workdir, log_plan=None, log_snapshot=None):
    """The log tools the CRASH/TIMEOUT TRIAGE judge may LOOK with, or None to keep the historical
    stderr-tail-only ask.

    WHY THE REPAIR PATH NEEDED THIS TOO (measured, `runs/rubertlite-dr-unified-v8` node 3, attempt 5)
    ---------------------------------------------------------------------------------------------
    The watchdogs got `read_log`/`metric_series` because a bounded tail cannot answer the question
    they are asked. Triage is asked a HARDER version of the same question — "why did this stage die"
    — and it was still working from a slice, and a much smaller one: `_eval_failure_text` hands it
    `res.stderr[-500:]`, five hundred CHARACTERS, where the watchdog's digest at least came off a
    128 KiB read.

    Node 3 declared a 22,000 s ceiling and was SIGKILLed at 22,003 s. Its `train.log` holds two
    progress bars with different totals: the training bar reached `10590/10590 [5:29:35]` and the
    stage then printed `{'train_runtime': 19775.3, …, 'epoch': 14.98}` — all 15 epochs, done — after
    which a RETRIEVAL bar started and was killed at `223/361 [31:29<19:50]`, about twenty minutes
    from finishing. The durable `node_repaired.error_in` for that attempt is 522 characters and
    contains ONLY the last two renders of the second bar. The triage verdict read the second bar's
    elapsed field as training progress — *"node 3 is still in epoch 1 at 31:20"*, and `31:20` is
    verbatim the `222/361` render — and prescribed halving the batch AND cutting `n_epochs` 15 -> 8.
    The evidence that refutes it was 83,697 characters back in the same file, on a plain
    non-progress-bar line. 6.1 GPU-hours were discarded — and then spent AGAIN: the epochs cut never
    landed (`repair_verify` stamped `unmet: ['grad_accum', 'n_epochs']` on that row, no repaired file
    sets it, `config.yaml` still reads 15), so attempt 6 re-ran the SAME 10,590 steps. This docstring
    projected that retry at 22,096 s into the same 22,000 s ceiling, extrapolated live at
    1.798 s/step. **THE RUN FALSIFIED THE PROJECTION AND IT IS RETRACTED HERE**: `train` passed in
    19,915.75 s, `score` ran 3,130.3 s, and the node recorded 0.762048 to become v8's champion. What
    bought the margin was a SECOND edit in the same repair — deleting the in-`train` `test_model()`
    call, so the full-index retrieval moved out of the train budget and into the protected `score`
    stage (verified in the durable change set: attempt 4's `train.py` CALLS `test_model(...)`,
    attempt 5's carries the import and a note that retrieval "is run independently").
    **What this rung exists for is UNCHANGED and is not the projection**: the verdict was drawn from
    a 522-character tail holding only the second progress bar, and it was wrong about where the time
    was going. A misdiagnosis that happens to be rescued by an unrelated edit in the same repair is
    not a diagnosis that worked.

    So the fix is the same one, one role over: let it ASK the log instead of being handed the end of
    it. Everything else — the boundary, the floor, the source map — is `monitor_log_tools`' and is
    shared through `_log_query_tools` rather than written a second time.

    A FREE FUNCTION for the same reason its sibling is (see above), and gated on its OWN setting
    (`Settings.repair_log_tools`) rather than on `train_monitor_tools`: the two are different paid
    surfaces on different cadences, and an operator who turned the timer-fired watchdog's tools off
    to stop ~200 agentic ticks per node has said nothing about the one look that happens when a
    multi-hour node has already died. `getattr` is total over a partially-built engine.

    None also when the eval wrote no nameable log — which is every non-command eval (`solution.py`,
    toy, dataset) and every failure before the first stage opened its log. `UnifiedAgent.triage_crash`
    reads `None` as "no extra tools" and splices no invitation, so the off path is the historical
    prompt and the historical toolset, byte for byte.
    """
    if not getattr(engine, "_repair_log_tools", True):        # the settled default (ENG1-03)
        return None
    return _log_query_tools(workdir, log_plan, log_snapshot)


def stage_check_tools(engine, workdir, log_plan=None, log_snapshot=None):
    """The log tools the INTER-STAGE CHECKER may LOOK with, or None to keep the historical one-line
    completion over `run.out[-4000:]` (doc 52 row 9).

    The fourth gate over the ONE `_log_query_tools` derivation, and the last judge to get it: the
    two watchdogs (2026-08-14) and the crash/timeout triage judge (2026-08-15) were moved off fixed
    slices because a slice was measured wrong on each of them, and `docs/BACKLOG.md` §0.9 recorded
    this checker — the one that can END A NODE — as the still-open residue: its window is
    `run.out[-4000:]` of a `run.out` that is itself a 64,000-byte tail clamp, so the trainer's
    banner, the first losses, the `Saving model` line, a traceback that preceded a long progress
    bar and every restart are outside it by construction (`STAGE_CHECK_TRAJECTORY_KIND`'s block
    above holds the ten-node measurement). Same boundary as its siblings — a log is named, never
    pathed; the map is `eval_log_plan` over the resolved pipeline; every read is a bounded seek that
    states its byte range; the floor is `attempt_byte_floor` over the snapshot `_stage_check_fn`
    took BEFORE this attempt's first stage wrote a byte, so the checker of attempt N cannot read
    attempt N-1's log as N's.

    Its OWN switch (`Settings.stage_check_tools`), not the triage judge's or the watchdogs': this
    role is paid once per checked stage on the eval-blocking path between stages, and it is the
    only one of the four whose verdict can be terminal. `getattr` is total over a partially-built
    engine and over the doubles in the suite. None also when no stage has written a nameable log
    yet, which is why `_stage_check_fn` builds it at CHECK time rather than at construction — the
    checked stage's log does not exist until the stage has run.
    """
    if not getattr(engine, "_stage_check_tools", True):       # the settled default (ENG1-03)
        return None
    return _log_query_tools(workdir, log_plan, log_snapshot)


# ---------------------------------------------------------------- THE WATCHDOG TICK, WRITTEN ONCE
# Both live-log watchdogs — `_monitor_training` below and `asha_monitor.py::_monitor_asha` — wrap
# their own judgement in the same scaffold: recover the last durable row on re-entry, spend at most
# one bounded paid look per tick in a worker, redact what the model wrote, and append one
# diagnostic row that a decided stop must not lose. Each loop carried its own copy, so every fix to
# the scaffold had to land twice, and did — the in-worker tool build with a 14-line docstring stated
# once per loop, the pre-call cancel check, the `finally` bounds (review 2026-09-22, ENG3-13 / doc 50
# EM-05). These are that scaffold ONCE, as free functions for the reason `monitor_log_tools` is one:
# no MRO can hide one from a stub (`tests/test_asha_monitor.py::_AshaStub` runs the ASHA loop with no
# `TrainingMonitorMixin`), and each takes exactly what it touches. Both loops resolve them through
# THIS module — the ASHA loop by a call-time import — so one patch here reaches both.


async def recover_last_row(engine, event_type: str, node_id: int,
                           generation: int) -> Optional[dict]:
    """The newest durable `event_type` row of exactly this `(node_id, generation)`, or None.

    `resume` can restart a watchdog inside the same node generation, and without its last durable
    row the first healthy observation looks like a first observation, so a pre-crash warning is lost
    instead of being closed (doc 25 EC-04). The READ is shared — the whole log fetched on a worker
    thread, never on the event loop, and scanned by `last_lifecycle_row`'s bool-guarded lifecycle
    match; what the row MEANS stays with each watchdog.

    Advisory: a read that fails answers None — "no history" — and the live watch still starts.
    """
    # Local: `evaluate` imports this module, so a module-level import would be a cycle.
    from looplab.engine.evaluate import _watch_limiter
    import anyio

    try:
        rows = await anyio.to_thread.run_sync(engine.store.read_all, limiter=_watch_limiter())
        return last_lifecycle_row(rows, event_type, node_id, generation)
    except Exception:  # noqa: BLE001 - advisory history lookup; the live monitor still proceeds
        return None


class JudgeCalls:
    """How many paid looks ONE watchdog has STARTED on one node lifecycle — answered, raised and
    budget-stopped alike: counting only the looks that RETURNED is what let a judge that raised be
    re-asked every tick, past any cap (review 2026-09-22, ENG3-02). The loop owns it; the cap it is
    held to is read by the loop, per tick, from the loop's own module, so a test that lowers the cap
    patches the module whose code reads it."""

    __slots__ = ("started",)

    def __init__(self) -> None:
        self.started = 0


async def watchdog_judge_tick(sp, cancel, judge, calls: JudgeCalls, *, cap: int,
                              on_raised=None) -> tuple:
    """Spend at most ONE paid look by a live-log watchdog's judge. Returns `(answer, stop)`.

    `stop` True means the watch must END, and the span says why: `cancelled_before_call` (the eval
    ended while this tick was reading) or `budget_stop` (the run's spend ceiling). Otherwise
    `answer` is whatever `judge` returned — None when it parsed nothing, and None WITHOUT a call
    when the per-node backstop is spent (`llm_capped` on the span: a silent cap would read as "all
    healthy" to one watchdog and as "the judge keeps sparing this node" to the other, when in fact
    nobody is asking any more). A judge that RAISES propagates, so the tick's own handler skips the
    tick as it always did — after the look is counted and `on_raised` has run.

    `judge` RUNS IN A WORKER, AND SO DOES EVERYTHING IT DERIVES. The paid call is synchronous and so
    is its tool derivation: `monitor_log_tools` GLOBS the workdir for `*.log`, opens + fstats every
    stage log the plan names, and `attempt_byte_floor` probe-READS each one. As an ARGUMENT to
    `run_sync` it was evaluated on the EVENT-LOOP thread, the one place in a tick that pays for a
    slow mount: on the geesefs/S3 mounts `runs/` lives on, an `lstat` of a file that is NOT there
    costs 105-950 ms (`core/fence.py::_warm_directory_lookup` measured it), so one blocking
    derivation per tick per running eval stalled the whole engine loop. So each caller's `judge`
    builds its tools — and its prompt evidence, and whatever it re-reads after the answer — INSIDE
    itself, PER TICK, because the log the judge reads is being written while it thinks. (This was
    stated twice, once per loop, because each built its tools at its own call site.)

    THE CALL IS JOINED, NEVER ABANDONED (`abandon_on_cancel=False`). The verdict may be advisory, but
    its client usage is billable and recorded on shared run state, so an in-flight call is joined on
    eval cancellation and no detached worker can emit cost after the node or run has finalized.
    Endpoint timeouts remain the upper bound for that ownership hand-off — which is why the `cancel`
    check sits immediately before the spend: the eval can end (it finished, an operator aborted, or
    the SIBLING watchdog claimed the kill) while this tick was reading, and starting the call then
    would buy a verdict about a node that no longer exists AND hold node teardown open for a whole
    endpoint timeout to pay for it — once per watchdog.

    BOTH BOUNDS ARE COMMITTED IN `finally` (review 2026-09-22, ENG3-02). They were committed only
    after the await RETURNED, and each tick's blind handler swallowed whatever did not: a judge that
    raised counted toward neither the per-node cap nor the training monitor's same-digest retry, and
    was asked again next tick — 300 times in 5 s on a frozen log in the reproduction
    (`review/ENG3/budget_swallow.py`), each one a provider call. `on_raised` is where a caller's own
    bound on an unanswered look is committed.

    THE SPEND CEILING ENDS THE WATCH. `BudgetExceeded` is the operator's ceiling, not a tick hiccup:
    it is said on the tick's span and the watch stops. RETURN, not re-raise — a watcher shares the
    eval's task group, and tearing down the stage whose training the run is already paying for is
    not a watchdog's decision to make. The accountant is sticky, so the run's next paid call raises
    the same stop on the main path, which is where it ends the run.
    """
    if calls.started >= cap:
        sp.set("llm_capped", True)
        return None, False
    if cancel.is_set():
        sp.set("cancelled_before_call", True)
        return None, True
    import anyio

    attempt = "raised"
    try:
        answer = await anyio.to_thread.run_sync(judge, abandon_on_cancel=False)
        attempt = "answered"
        return answer, False
    except BudgetExceeded:
        attempt = "budget_stop"
        sp.set("budget_stop", True)
        return None, True
    finally:
        calls.started += 1
        if attempt == "raised" and on_raised is not None:
            on_raised()


def watchdog_redact(engine, text: str, limit: int = 300) -> str:
    """What a watchdog's judge wrote, redacted and bounded before it lands in the trace, the event
    log or the attention feed. It is model text derived from the candidate's own log, so it takes
    the funnel `_evaluate` stores stderr tails through (`engine/audit.py::Engine._redact`);
    `getattr` because a watchdog runs on hosts that never ran `Engine.__init__`."""
    redact = getattr(engine, "_redact", None)
    return (redact(text) if callable(redact) else text)[:limit]


async def append_watchdog_row(event_type: str, row: dict, engine, *, shield: bool) -> None:
    """Append ONE watchdog row under the engine's write lock — SHIELDED when the row records a
    decided stop.

    A decided stop means cancellation is already in flight (this watchdog's own claim or its
    sibling's): `_evaluate` cancels the eval's task group, and the write lock is the row's next
    cancellation checkpoint, so an unshielded append would be preempted and the ONLY durable record
    of the decision lost — the kill would leave no diagnostic behind. Bounded (one append), and the
    same shielding `_evaluate` uses for its own promised terminal. A row that decided nothing keeps
    the plain best-effort append.

    DIAGNOSTIC rows only, asserted here for every caller: both watchdogs append from a task beside
    the main one, and only a fold-ignored row's thread-dependent position cannot move folded state
    (invariant #1). `event_type` comes FIRST on purpose: the payload contract's writer scan
    (`tests/_source_scan.py::event_payload_writers`) knows a writer by a call whose first argument
    is the event type and whose second is the payload, and a helper that took the engine first
    would hide every watchdog row from it.
    """
    import anyio

    from looplab.events.types import DIAGNOSTIC_EVENTS

    assert event_type in DIAGNOSTIC_EVENTS, event_type
    with anyio.CancelScope(shield=shield):
        async with engine._write_lock:
            engine.store.append(event_type, row)


def verdict_citation_resolved(verdict, workdir) -> Optional[bool]:
    """The ENGINE's re-read of the place a verdict says it read — the training monitor's one
    post-answer filesystem touch. `failure_diagnosis.evidence_citation_resolves` performs it,
    confined to the node workdir and refusing `..`, an absolute path and a symlink out. Three
    answers, and the third one matters: None = it cited nothing checkable (which is also what NO
    verdict reads as — no `evidence_source`, so nothing filesystem-shaped, and no I/O), False = it
    cited something that is not there, True = the engine found it. Only True authenticates
    (`citation_authenticates`).

    Called INSIDE `_monitor_training`'s `_judge`, i.e. in the worker, with the verdict it re-reads
    (review 2026-09-22, ENG3-13 / doc 50 EM-08). It ran on the event loop after the worker
    returned: a stat of a path the MODEL named, so possibly absent, in a tick whose judge's own tool
    build had already been moved off the loop because such a stat costs 105-950 ms on the mounts
    `runs/` lives on (`watchdog_judge_tick`). A probe must never end the watcher, so any failure of
    the re-read is None.
    """
    # Local, as it always was: `failure_diagnosis` reaches back into this module (`repair_log_tools`),
    # and a call-time import is also what lets a test patch the re-read where it is defined.
    from looplab.engine.failure_diagnosis import evidence_citation_resolves
    try:
        return evidence_citation_resolves(
            {"source": getattr(verdict, "evidence_source", "none"),
             "locator": getattr(verdict, "evidence_locator", "")}, workdir)
    except Exception:  # noqa: BLE001 — a probe must never end the watcher
        return None


class TrainingMonitorMixin:
    """The engine's training-log monitor cluster. `self` IS the Engine (mixin convention — see
    orchestrator.py). Gated on `self._train_monitor`; started as a sibling task in `_evaluate`'s task
    group so it lives exactly as long as the eval and is cancelled with it."""

    def _monitor_cadence(self) -> float:
        """The BASE check interval, derived from the per-experiment time budget so a short training is
        watched often and a multi-hour one sparsely (a fixed 600s would miss a 5-minute run entirely and
        over-watch a 5-hour one). ~10% of the budget, clamped to [30s, 30min], then floored by the config
        `train_monitor_interval_s` so the user can force MORE-frequent checks. Falls back to the config
        interval when no budget is known (solution.py path / no eval_spec)."""
        cfg = max(0.02, float(getattr(self, "_train_monitor_interval_s", 600.0) or 600.0))
        budget = None
        fn = getattr(self, "_experiment_time_budget", None)
        if callable(fn):
            try:
                budget = fn()
            except Exception:  # noqa: BLE001 — cadence is advisory; a budget hiccup just uses the config
                budget = None
        if isinstance(budget, (int, float)) and not isinstance(budget, bool) and budget > 0:
            derived = min(1800.0, max(30.0, float(budget) * 0.1))
            return min(cfg, derived)         # config is an upper bound: the user can only tighten it
        return cfg

    def _training_verdict(self, digest: str, context: str, stage_context: str = "",
                          trajectory_text: str = "", tools=None,
                          contract_text: str = "") -> Optional[TrainingVerdict]:
        """One-shot LLM judgment of the live log (SYNC — the caller runs it in a worker thread). Uses the
        Developer's client (the Developer wrote the loop, so it knows what its own logs should look like)
        with a fresh, STATELESS structured call — it never mutates the shared role object, so it is safe to
        fire while the eval thread runs. The client records its own usage/cost. Returns None when there is
        no client (offline / toy path) or the model output can't be parsed — advisory, never fatal.

        `stage_context` (from `monitor_stage_context`) NAMES the eval phase the digest came from and sits
        immediately above the log header, where the model reads it as part of the evidence. This is the
        second, independent layer of the mis-scoped-verdict fix: scoping decides which log is read, and
        this decides whether a model shown the wrong one can NOTICE. Measured on the live endpoint with a
        flat-loss + `CUDA not available` scorer tail: with the stage identity present, `deepseek-v4-flash`
        answers "cannot determine, the stage is 'score' not 'train'" and `qwen3.5-122b` answers healthy —
        neither says `broken`. Without it the same tail reads as three separate `broken` clauses at once.
        `trajectory_text` (from `trajectory_context`) is the third such layer and the one that made the
        question ANSWERABLE: scoping decides which log is read, the stage identity decides whether a
        model shown the wrong one can notice, and this decides whether a model shown the right one has
        enough of it. Measured, the digest the judge received was ~10 loss values over ~30 seconds of a
        multi-hour run — see the trajectory section above for the two live cases where that produced a
        confident "not learning" about a run that had descended 4.73 and 1.41 respectively.

        `contract_text` (from `stage_contract_context`) is the FIFTH such layer and the only one that
        is not about the CURVE. Measured over the committed 450-decision bench, the judge answers
        `broken` on 48 of the 53 decisions whose node trained fine and scored ~0 (91 %) and on 2 of
        the 38 whose stage EXITED 0 and was then failed by the engine on its own declared contract
        (5 %) — and 13.4 h of the 20.1 h an oracle could still save sits in that second class. The
        judge was never blind there; it was never told the contract existed. Sits immediately below
        the stage identity because it is a fact about the SAME stage, and above the trajectory
        because it can make a perfectly healthy curve irrelevant.

        `tools` (from `monitor_log_tools`) is the FOURTH such layer and the one that stops the engine
        choosing for the judge what it is allowed to see. The three above are all still fixed slices;
        this one lets the judge ASK — tail the log further back, read its start, search it for a
        traceback, or query the loss series over the whole run at a granularity it picks. The digest
        still arrives spliced, so a model that ignores the tools answers exactly as it did before;
        `_LOOK_INVITATION` is what tells it they are there, and it is spliced at the SAME position
        pattern as the three above (empty when there are no tools, reproducing the historical message
        byte for byte).

        Additive by construction: `_MONITOR_SYSTEM` and the log header are unchanged (prompt strings are
        contracts), and an empty `stage_context`/`trajectory_text` reproduces the historical message byte
        for byte."""
        client = getattr(getattr(self, "developer", None), "client", None)
        if client is None:
            return None
        messages = [
            {"role": "system", "content": _MONITOR_SYSTEM},
            {"role": "user", "content": ((context + "\n\n") if context else "")
             + ((stage_context + "\n\n") if stage_context else "")
             + ((contract_text + "\n\n") if contract_text else "")
             + ((trajectory_text + "\n\n") if trajectory_text else "")
             + ((_LOOK_INVITATION + "\n\n") if tools is not None else "")
             # Spliced at the SAME position pattern and under the SAME condition as the invitation
             # above: both are about what the judge may go and DO, and neither can be honoured
             # without tools. `train_monitor_tools=false` therefore still reproduces the historical
             # message byte for byte, which is what makes the whole tool rung shippable.
             + ((_CITE_INVITATION + "\n\n") if tools is not None else "")
             + "LIVE TRAINING LOG (recent tail):\n" + digest
             + "\n\nClassify this run's health from the log evidence above."},
        ]
        try:
            from looplab.engine.shared import judge_evidence_kwargs
            from looplab.trust.judge import structured_judge
            # `parser="tool_call"` is what the two other judges in this repo use, and `structured_judge`
            # falls back to the plain `parse_structured` whenever the tool loop yields nothing valid —
            # so an agentic hiccup degrades to the historical verdict rather than to no verdict.
            # What its tools return is the candidate's own log and code, fenced when the run's
            # evidence envelope is on (review 2026-09-22, TAT-02).
            return structured_judge(client, messages, TrainingVerdict, parser="tool_call",
                                    tools=tools, max_turns=_MONITOR_LOOK_TURNS,
                                    **judge_evidence_kwargs(self))
        except BudgetExceeded:  # a hard budget stop must propagate, never degrade (core/containment.py)
            raise
        except Exception:  # noqa: BLE001 — a parser/endpoint failure means "no verdict this tick", not a crash
            return None

    @in_llm_lane("enrichment")
    async def _monitor_training(self, node_id: int, generation: int, workdir, cancel,
                                context: str = "", kill_signal: Optional[dict] = None,
                                log_snapshot: Optional[TrainingLogSnapshot] = None,
                                log_plan: Optional[EvalLogPlan] = None) -> None:
        """Tail the live training log every `train_monitor_interval_s`, ask the Developer to judge its
        health, record the verdict, and (when `train_monitor_kill` is on) kill a broken run early.

        WHICH log (`log_plan`, from `eval_log_plan`): the monitor lives across the WHOLE eval — setup,
        every stage, the always-appended score stage — so "the freshest `*.log`" is not a synonym for
        "the training". With a plan it reads only the stage logs that can carry the candidate's own
        output and the judge is TOLD which stage it is looking at; `setup.log`, the pipeline's scorer
        and an unattributable filename produce no tick at all. Which of the logs it DOES read may kill
        is a second, stricter question, answered by `eval_log_plan` (only a log that is the whole eval)
        and enforced in `should_monitor_kill`. Without a plan it keeps the historical freshest-file
        read but can never kill — the monitor must not act on a stage it cannot identify.

        Advisory (always): every tick with a CHANGED digest emits a `train_monitor` trace span carrying
        the verdict; a NON-healthy verdict additionally appends an EV_TRAIN_MONITOR_ALERT diagnostic event
        (fold-ignored, so it cannot directly change lifecycle/champion/replay). The raw
        diagnostic can still steer a later Researcher prompt when watchdog_reflection is enabled, and it
        also feeds the owner attention view + audit.
        Healthy verdicts stay trace-only except for a healthy transition after an alert; that explicit
        recovery row lets every lifecycle projection clear the earlier warning.

        Intervention (Phase 3, only when `_train_monitor_kill` is on): a confident 'broken' verdict about
        an identified training stage ARMS the gate and schedules a prompt re-look; the second consecutive
        such verdict claims the kill — the monitor records the reason into `kill_signal` and sets `cancel`
        (the SAME tree-kill path an operator abort uses), then stops. `_evaluate` sees the killed eval and
        writes the node's single terminal `node_failed` (reason='monitor_broken'); replay reconstructs the
        node from that terminal and never re-invokes the LLM. A plateau is 'watch', never 'broken', so it
        is never killed — and a `broken` verdict the engine's own measured trajectory contradicts is not
        killed either: it neither arms the gate nor claims, it records the measurement beside the verdict
        and keeps watching. The alert row records whether this monitor actually OWNED that terminal, so an
        audit of "which watchdog stopped what" reads the durable log instead of guessing.

        With no LLM client wired it degrades to trace-only observation. Exits when the eval finishes
        (`cancel`, or the task group is cancelled); a per-tick hiccup skips the tick and never disables the
        watcher for the rest of a long eval."""
        # Local: `evaluate` imports this module, so a module-level import would be a cycle.
        from looplab.engine.evaluate import _watch_limiter
        import anyio

        from looplab.events.types import EV_TRAIN_MONITOR_ALERT
        # Base cadence derived from the per-experiment time budget (Phase 2): a short training is watched
        # often, a multi-hour one sparsely. The next delay adapts per verdict — the observer self-paces
        # (LLM `recheck_after_s`) and a steadily-healthy run backs off — via `next_monitor_sleep`.
        base = self._monitor_cadence()
        next_sleep = base
        last_digest: Optional[str] = None
        healthy_streak = 0
        last_event_status: Optional[str] = None
        # resume may restart the observer inside the same node generation. Recover its last
        # durable state so the first healthy verdict can close a pre-crash warning instead of losing it.
        prior = await recover_last_row(self, EV_TRAIN_MONITOR_ALERT, node_id, generation)
        if prior is not None:
            status = str(prior.get("status") or "").strip().lower()
            last_event_status = status if status in ("healthy", "watch", "broken") else None
        llm_calls = JudgeCalls()
        # Phase 3 arming state. `broken_streak` counts CONSECUTIVE `broken` verdicts about the
        # same stage log — ANY of them, at ANY confidence: the increment below is
        # `if verdict.status == "broken"` and never reads the number. This comment said
        # "confident-broken" until 2026-08-27 and that was wrong in a way that matters, because
        # it makes the two kill conjuncts look like one. They are independent: repetition is
        # satisfied by sub-bar verdicts, and the confidence bar is what actually holds the gun.
        # On `runs/e5small-dr-unified-v8` node 2 the streak reached 3 across verdicts of 0.70,
        # 0.65 and 0.70 while nothing was armed to fire; `armed_key` is the log they were about, so a stage change (train.log ->
        # score.log) can never let two different subjects confirm each other. `armed_at` is set only
        # when the KILL GATE actually arms (a broken verdict about a kill-eligible log) and is what
        # licenses the changed-digest bypass; with `arm_looks` it bounds how long and how expensively
        # an arm may stand (`_MONITOR_ARM_TTL_S` / `_MONITOR_ARM_MAX_LOOKS`). `disarm()` is the ONE
        # spelling of "re-arm from zero", so every path listed on `_MONITOR_KILL_CONFIRM_TICKS`
        # provably resets the same three variables.
        broken_streak = 0
        armed_key: Optional[str] = None
        armed_at: Optional[float] = None
        arm_looks = 0
        # Bounded retry of one unchanged digest whose verdict never parsed (`_MONITOR_SAME_DIGEST_RETRIES`).
        failed_digest: Optional[str] = None
        failed_digest_tries = 0
        # The run-scale loss curve, accumulated one reduction per tick from the tails this loop
        # already reads (see `LossTrajectoryTracker`). It is what the judge is shown besides the
        # tail and what `should_monitor_kill` consults; it is reset when the active stage log
        # changes, alongside the kill gate, because two stages are two curves.
        tracker = LossTrajectoryTracker()
        # Tracked separately from `last_digest`, which is committed only on a PARSED verdict: an
        # endpoint failure must not make the same window be counted twice as two readings.
        last_tracked: Optional[str] = None

        def disarm() -> None:
            nonlocal broken_streak, armed_at, arm_looks
            broken_streak, armed_at, arm_looks = 0, None, 0

        def unjudged(digest: str, sp) -> None:
            """Count one UNANSWERED look at `digest` toward `_MONITOR_SAME_DIGEST_RETRIES`, and retire
            it once that bound is spent (quiet until the log actually changes), saying so on the
            tick's span. One spelling for the two ways a look goes unanswered — a verdict that did not
            parse, and a judge that RAISED (`watchdog_judge_tick`'s `on_raised`) — because the second
            used to commit nothing at all (review 2026-09-22, ENG3-02)."""
            nonlocal failed_digest, failed_digest_tries, last_digest
            if digest == failed_digest:
                failed_digest_tries += 1
            else:
                failed_digest, failed_digest_tries = digest, 1
            if failed_digest_tries >= _MONITOR_SAME_DIGEST_RETRIES:
                last_digest = digest     # retire it: quiet until the log actually changes
                sp.set("digest_retired", True)

        while True:
            await anyio.sleep(next_sleep)    # only cancellation (eval finished) unwinds the task, from here
            if cancel.is_set():
                return
            try:
                def _observe_log():
                    """ONE attributed read per tick, in the worker thread: which stage log is live, and
                    (only when that phase can carry the candidate's own output) its digested tail.
                    `setup.log`, the pipeline's scorer and an unattributable filename return no tail at
                    all — there is nothing for a training-health prompt to say about a dep install or
                    about a scorer running after the training already finished, and asking anyway is
                    what produced a confident wrong verdict."""
                    resolved = resolve_stage_log(workdir, log_plan)
                    if resolved is None or resolved.role in _NON_TRAINING_ROLES:
                        return resolved, "", False
                    # Same thread as the rest of this tick's I/O, so the stat never touches the
                    # event loop. Read EVERY tick, not once: the artifact appears mid-eval, and it
                    # is the appearance that spends the authority.
                    spent = training_authority_spent(workdir, log_plan)
                    return (resolved,
                            read_training_tail(workdir, snapshot=log_snapshot, plan=log_plan),
                            spent)

                resolved, tail, authority_spent = await anyio.to_thread.run_sync(
                    _observe_log, limiter=_watch_limiter())
                log_role = resolved.role if resolved is not None else LOG_ROLE_UNKNOWN
                if authority_spent and log_role == LOG_ROLE_TRAINING:
                    # The declaration bought authority over the TRAINING; the training is over.
                    # Downgrading to the advisory role rather than going silent keeps the verdict,
                    # the alert row and the narration — only the gun is handed back.
                    log_role = LOG_ROLE_WORK
                log_key = _log_path_key(resolved.path) if resolved is not None else None
                if log_key != armed_key:
                    armed_key = log_key
                    disarm()                 # a different subject re-arms from zero
                    tracker.reset()          # ...and a different subject is a different curve
                    last_tracked = None
                elif armed_at is not None and (
                        anyio.current_time() - armed_at) > _MONITOR_ARM_TTL_S:
                    disarm()                 # a stale arm is not evidence — see _MONITOR_ARM_TTL_S
                # KNOWN BLIND SPOT of this changed-digest gate: a HUNG training (process alive, no
                # new log output) holds the digest constant forever, so the LLM is never consulted
                # again and the hang is never judged here. The STALL watchdog in `run_argv` is what
                # catches that case — it is output-based and tree-kills on silence — so this monitor
                # deliberately stays a judge of what the run SAYS, not of whether it says anything.
                #
                # ONE exception: once the kill gate is ARMED, the confirming look must happen even if
                # the log has gone quiet since. "Diverged, then stopped printing" is precisely the run
                # the confirmation is meant to end, and skipping it there would turn the confirmation
                # window into a way for a broken run to survive by saying nothing.
                if not tail:
                    continue                 # no live log yet (or none this watchdog may read)
                # MEASURE BEFORE ASKING, and measure on EVERY tick with new bytes — including the
                # ones the changed-digest gate below then declines to spend an LLM call on, and the
                # ones whose verdict never parses. The trajectory's value is its span, so a window
                # skipped here is a hole in the run's history that no later tick can refill.
                if tail != last_tracked:
                    tracker.observe(tail, at=anyio.current_time())
                    last_tracked = tail
                trajectory = tracker.summary()
                unchanged = tail == last_digest
                if unchanged:
                    if armed_at is None:
                        continue             # nothing new since last tick -> no LLM call
                    if arm_looks >= _MONITOR_ARM_MAX_LOOKS:
                        # The arm has already bought its one re-ask of this identical digest. Asking
                        # again buys a byte-identical prompt, not a second sample, so disarm and let
                        # the ordinary changed-digest gate take over (see _MONITOR_ARM_MAX_LOOKS).
                        disarm()
                        continue
                    arm_looks += 1
                # Open the span BEFORE the LLM call so the observer's LLM turn bands under `train_monitor`
                # (not the enclosing `evaluate`) — the same trace-attribution fix `_triage_crash` uses.
                with self.tracer.span("train_monitor", node_id=node_id) as sp:
                    # WHETHER A KILL IS REACHABLE AT ALL for this eval, on every tick's span. The
                    # role gate is a property of the resolved PIPELINE, not of the run's health, so
                    # a pipeline that declared no training stage can be read as unstoppable from
                    # its first tick instead of after the hours it takes for a verdict to matter.
                    if log_plan is not None and LOG_ROLE_TRAINING not in {
                            role for _stage, role in log_plan.roles.values()}:
                        sp.set("kill_reachable", False)
                    sp.set_many(generation=generation, log_role=log_role,
                                digest_lines=tail.count("\n") + 1, digest_chars=len(tail))
                    if resolved is not None and resolved.stage:
                        sp.set("stage", resolved.stage)
                    if trajectory.windows:
                        # The measured curve on the span too, so "why did (or didn't) it act" is
                        # answerable from the trace and not only from the durable alert row.
                        sp.set_many(trajectory=trajectory.direction,
                                    trajectory_windows=trajectory.windows,
                                    trajectory_points=trajectory.points)
                    if unchanged:
                        # The confirming look at a FROZEN log re-asks a byte-identical question. It
                        # defends against sampling noise, never against a systematic misread — say so
                        # on the span rather than letting "two verdicts" imply two observations.
                        sp.set("confirm_digest_unchanged", True)
                    def _judge():
                        # The paid call AND everything it needs, derived in the worker with it — the
                        # prompt's evidence and the tools before it, the citation re-read after it;
                        # `watchdog_judge_tick` says why none of it may run on the event loop, and
                        # why it is built PER TICK.
                        stage_text = monitor_stage_context(resolved, log_plan)
                        trajectory_text = trajectory_context(trajectory)
                        # The watched stage's own declared contract, and the engine's live reading of
                        # whether the trainer's configured schedule can meet it. `resolved.stage` is
                        # the stage this tick's bytes came from — never the pipeline's, never the
                        # declared training stage's — so a tick reading `mine.log` is shown `mine`'s
                        # promise and nothing else. Empty for a stage that promised nothing, which
                        # is the historical message byte for byte.
                        contract_text = ""
                        if (getattr(self, "_train_monitor_contract", True)
                                and log_plan is not None and resolved is not None
                                and resolved.stage is not None):
                            contract_text = stage_contract_context(
                                log_plan.declarations.get(resolved.stage), tail)
                        verdict = self._training_verdict(
                            tail, context, stage_text, trajectory_text,
                            monitor_tools(self, workdir, log_plan, log_snapshot),
                            contract_text=contract_text)
                        # ...and the engine's own re-read of what the verdict CITED, here with it:
                        # a stat of a model-named path, never made on the loop (review 2026-09-22,
                        # ENG3-13 / doc 50 EM-08).
                        return verdict, verdict_citation_resolved(verdict, workdir)

                    # Per-node backstop on LLM cost (the adaptive cadence + healthy-backoff are the primary
                    # budget control; this only bounds a pathological run whose digest keeps changing while
                    # staying non-healthy). Past the cap we keep OBSERVING (trace-only) but stop calling the
                    # LLM. The cap, the pre-call cancel check, the joined worker call, the spend ceiling
                    # and both `finally` bounds are the protocol both watchdogs share
                    # (`watchdog_judge_tick`); this monitor's own bound on an unanswered look is
                    # `unjudged`, committed through `on_raised` when the judge RAISES.
                    answer, stop = await watchdog_judge_tick(
                        sp, cancel, _judge, llm_calls, cap=_MAX_MONITOR_LLM_CALLS,
                        on_raised=lambda: unjudged(tail, sp))
                    if stop:
                        return
                    # No call (the cap) is no verdict and nothing cited.
                    verdict, _citation_resolved = answer if answer is not None else (None, None)
                    if verdict is None:
                        # NO PARSEABLE ANSWER this tick — an endpoint failure, model output that failed
                        # schema validation (`unknown` is not a `TrainingVerdict.status`), or the
                        # per-node LLM cap. Under `_MONITOR_KILL_CONFIRM_TICKS` that is "anything
                        # else", so it re-arms the gate from zero: the streak used to be touched only
                        # inside this branch's `else`, which left an arm standing across six `unknown`
                        # ticks and let the NEXT `broken` kill as though the two were consecutive.
                        if armed_at is not None:
                            next_sleep = base   # drop the shortened confirmation cadence with the arm
                        disarm()
                        sp.set("verdict_unparsed", True)
                        # Bounded same-digest retry (see `_MONITOR_SAME_DIGEST_RETRIES`): `last_digest`
                        # is otherwise committed only on a usable verdict, so an endpoint that never
                        # answers re-sent a byte-identical prompt every cadence until the LLM cap.
                        unjudged(tail, sp)
                    else:
                        conf, confidence_valid = _normalize_monitor_confidence(verdict.confidence)
                        # The reason is LLM text derived from the raw log; redact it before it lands in the
                        # trace / event log / attention feed, matching how `_evaluate` stores stderr tails.
                        reason = watchdog_redact(self, verdict.reason or "")
                        # The durable event keeps the fuller reason (300); the trace span carries a shorter
                        # preview (200) — spans are a high-volume sidecar, the event is the authoritative record.
                        sp.set_many(status=verdict.status, confidence=round(conf, 3), reason=reason[:200])
                        if not confidence_valid:
                            sp.set("confidence_valid", False)
                        failed_digest, failed_digest_tries = None, 0   # this digest WAS judged
                        healthy_streak = healthy_streak + 1 if verdict.status == "healthy" else 0
                        if verdict.status == "broken":
                            broken_streak += 1
                        else:
                            disarm()         # a parseable non-broken verdict re-arms from zero
                        next_sleep = next_monitor_sleep(
                            base, status=verdict.status, recheck_after_s=verdict.recheck_after_s,
                            healthy_streak=healthy_streak)
                        # Phase 3 intervention (`train_monitor_kill`, ON in the product surface): a
                        # CONFIRMED, confident 'broken' verdict about an identified training stage is
                        # tree-killed EARLY. Hand the reason to `_evaluate` via `kill_signal`, set
                        # `cancel` (same path as an operator abort), and stop watching — `_evaluate`
                        # writes the single terminal node_failed.
                        # No `or`-coercion on the confidence bar. `x or 0.0` turns an unset/None/0.0
                        # knob into a ZERO threshold — i.e. EVERY `broken` verdict kills — which is
                        # the wrong direction to fail in, and it matters much more now that
                        # `train_monitor_kill` defaults to True. Mirrors the identical rule in
                        # `asha_monitor.py`; a non-numeric knob falls back to the schema default.
                        # REPETITION IS ALREADY REQUIRED, AND IT IS NOT THE THING HOLDING THE GUN.
                        # `should_monitor_kill` needs `broken_streak >= confirm_ticks` AND this
                        # confidence bar, and the streak counts ANY `broken` verdict at any
                        # confidence (see its increment). So the tempting fix — "kill once the
                        # verdict has repeated K times" — is already shipped, and what would
                        # actually change behaviour is DROPPING or LOWERING the confidence conjunct
                        # so repetition alone can fire. That is the thing measured and refused here.
                        #
                        # `runs/e5small-dr-unified-v8` node 2 drew three `broken` verdicts
                        # under the bar — 0.70 at 08:31:58, 0.65 at 08:44:38, 0.70 at 08:56:39 —
                        # before the 0.90 kill at 09:07:14, so 36 minutes of GPU ran under a
                        # watchdog that already believed the node broken three times. The obvious
                        # answer is "kill on K consecutive sub-bar broken verdicts". It was measured
                        # against every alert this box has recorded and it is REFUSED:
                        #
                        #   rubertlite-dr-unified-v6 node 1   0.62, 0.62, 0.75  ->  recorded 0.715142
                        #   e5small-dr-unified-v4    node 3   0.75, 0.70, 0.75  ->  recorded 0.790898
                        #   e5small-dr-unified-v8    node 2   0.70, 0.65, 0.70  ->  failed not_learning
                        #   e5small-dr-unified-v4    node 12  0.62, 0.70        ->  idea_rejected,
                        #                                                          never trained
                        #
                        # Two good nodes destroyed per node saved, and one of the two carries the
                        # strongest number in its neighbourhood. The 36 minutes are real and are the
                        # PRICE of that ratio, not an argument against it. What would change the
                        # answer is a signal that separates those rows — the trajectory veto already
                        # tried, and it is consulted on every one of them.
                        #
                        # DECLINED[broken-verdict-ladder] killing by accumulation of sub-bar `broken`
                        # verdicts. measured: 4 node-generations in `runs/` reach 2+ consecutive
                        # sub-0.8 `broken` verdicts across all 259 alerts; a K=3 ladder fires on 3 of
                        # them and TWO recorded real metrics (0.715142, 0.790898) against ONE true
                        # positive — docs/guide/llm-and-agents.md
                        _kc = getattr(self, "_train_monitor_kill_confidence", 0.8)
                        threshold = (float(_kc) if isinstance(_kc, (int, float))
                                     and not isinstance(_kc, bool) else 0.8)
                        # REPAIR FIRST, and on every judged stage. A named bug is a thing to fix,
                        # not a verdict about an idea — so this is asked before the terminal kill
                        # and wins it: a collapsed training whose cause the judge can point at goes
                        # back to its Developer, and only an implementation the judge will NOT
                        # blame reaches the gun. See `should_monitor_repair` for why the role gate
                        # that guards the kill does not guard this.
                        # THE ENGINE GOES AND LOOKS. `_citation_resolved` is its re-read of the place
                        # the verdict says it read (`verdict_citation_resolved`), confined to this
                        # node's workdir and refusing `..`, an absolute path and a symlink out. Three
                        # answers, and the third one matters: None = it cited nothing checkable, False
                        # = it cited something that is not there, True = the engine found it. Only
                        # True authenticates. Computed once per tick, outside the gate, because it is
                        # a FILESYSTEM read and the gates are pure/deterministic —
                        # `tests/test_train_monitor.py` drives them with no disk at all — and in the
                        # WORKER with the verdict, never here on the loop (ENG3-13 / doc 50 EM-08).
                        repair_decided = kill_signal is not None and should_monitor_repair(
                            verdict, enabled=getattr(self, "_train_monitor_kill", False),
                            threshold=threshold, log_role=log_role, broken_streak=broken_streak,
                            trajectory=trajectory, citation_resolved=_citation_resolved)
                        stop_decided = (not repair_decided) and kill_signal is not None \
                            and should_monitor_kill(
                                verdict, enabled=getattr(self, "_train_monitor_kill", False),
                                threshold=threshold, log_role=log_role,
                                broken_streak=broken_streak, trajectory=trajectory)
                        # Both counterfactuals below are asked only when the monitor did NOT act —
                        # `acted`, never `not stop_decided`. `stop_decided` is `(not repair_decided)
                        # and ...`, so a REPAIR-stop left it False and both receipts armed: one row
                        # then carried `repair_decided`, `kill: true` AND `kill_role_withheld`, the
                        # node stopped and its record saying the role prevented it (review
                        # 2026-09-22, ENG3-09 / EM-04, driven by `review/ENG3/em04.py`).
                        acted = stop_decided or repair_decided
                        # The COUNTERFACTUAL, evaluated only when the measurement is what refused.
                        # Pure and cheap, and it is what makes "the monitor would have ended this
                        # node but for the curve it measured" a durable fact rather than something
                        # an auditor has to re-derive from a log that has since grown.
                        trajectory_veto = (not acted and kill_signal is not None
                                           and trajectory_vetoes_kill(trajectory)
                                           and should_monitor_kill(
                                               verdict,
                                               enabled=getattr(self, "_train_monitor_kill", False),
                                               threshold=threshold, log_role=log_role,
                                               broken_streak=broken_streak))
                        if trajectory_veto:
                            sp.set("trajectory_veto", True)
                        # The OTHER counterfactual, and the one that cost the most: every conjunct
                        # cleared except the role. `e5small-dr-unified-v2` node 2 sat in exactly
                        # this state 31 times over 7.3 hours and NOTHING said so — the alert rows
                        # read as ordinary `broken` verdicts, indistinguishable from ones the kill
                        # path had simply not confirmed yet, so the unreachability of the early
                        # stop for multi-stage pipelines stayed invisible until a node scored 0.0.
                        # Pure, cheap, and evaluated only when the role is what refused; asking the
                        # SAME predicate with the role swapped is what makes this a fact about the
                        # gate rather than a second opinion about the run.
                        role_withheld = (not acted and kill_signal is not None
                                         and log_role not in _KILL_ELIGIBLE_ROLES
                                         and should_monitor_kill(
                                             verdict,
                                             enabled=getattr(self, "_train_monitor_kill", False),
                                             threshold=threshold, log_role=LOG_ROLE_TRAINING,
                                             broken_streak=broken_streak, trajectory=trajectory))
                        if role_withheld:
                            sp.set("kill_role_withheld", log_role)
                        # CLAIM BEFORE RECORDING. The sibling ASHA watchdog can decide on the same tick,
                        # and only one of them owns the node's terminal — so the alert must state what
                        # actually happened to the node, not what this monitor wanted. The guard->update
                        # inside `claim_watchdog_kill` is await-free, so the answer is exact.
                        claimed = (stop_decided or repair_decided) and claim_watchdog_kill(
                            kill_signal, cancel, reason=reason,
                            terminal_reason=(MONITOR_REPAIR_REASON if repair_decided
                                             else "monitor_broken"),
                            confidence=round(conf, 3))
                        if stop_decided or repair_decided:
                            sp.set_many(stop_decided=True, kill=bool(claimed),
                                        fault=str(getattr(verdict, "fault", "unknown")))
                        if repair_decided:
                            sp.set("repair_decided", True)
                        elif (verdict.status == "broken" and broken_streak == 1
                              and kill_signal is not None
                              and _confirmation_would_act(
                                  verdict, enabled=getattr(self, "_train_monitor_kill", False),
                                  threshold=threshold, log_role=log_role, trajectory=trajectory,
                                  citation_resolved=_citation_resolved)):
                            # ARMED, not acting: re-look promptly instead of after another full cadence
                            # (up to 30 min on a long budget), so confirmation costs seconds of a
                            # multi-hour budget rather than a meaningful slice of it. `armed_at` starts
                            # the TTL at the TRANSITION, never on a later broken tick — otherwise a
                            # flapping log could renew the arm indefinitely. It is also what licenses
                            # the changed-digest bypass, so a tick that could not act on a confirmation
                            # never buys a re-look — a billable re-ask and a changed-digest bypass
                            # bought for nothing.
                            #
                            # WHICH tick that is, is asked as the COUNTERFACTUAL
                            # (`_confirmation_would_act`) rather than re-listed here, and that
                            # changed on 2026-08-20. The hand-written conjuncts were
                            # `log_role in _KILL_ELIGIBLE_ROLES` plus the
                            # trajectory veto, written when a KILL was the only action a confirmation
                            # could reach; `should_monitor_repair` then opened the repair-stop to
                            # EVERY judged role, so on exactly the roles it was opened for the second
                            # look cost a full cadence — and, on a log that diverged and then went
                            # silent, never arrived at all, because `unchanged and armed_at is None`
                            # `continue`s and only an arm bypasses it. The list was also missing the
                            # CONFIDENCE bar it claimed to be a proxy for: a `broken` at 0.3 on a
                            # training stage armed, re-asked at 30 s and bypassed the digest gate for
                            # a kill `should_monitor_kill` refuses on confidence. Asking the two real
                            # predicates with the streak already satisfied answers both directions at
                            # once and cannot drift from them.
                            next_sleep = min(next_sleep, _MONITOR_CONFIRM_DELAY_S)
                            armed_at, arm_looks = anyio.current_time(), 0
                            sp.set("kill_armed", True)
                        sp.set("next_check_s", round(next_sleep, 2))
                        # The same precondition the alert body uses for the projection, hoisted so
                        # the gate and the body agree about when a projection is even possible.
                        measured_for_gate = trajectory_row(trajectory)
                        # THE CLOCK IS NOT A HEALTH VERDICT, and until 2026-08-30 it could only
                        # be heard through one. `stamp_projected_overrun` was called INSIDE the
                        # branch below, so a stage whose measured ETA cannot fit its own declared
                        # wall recorded that fact only if the judge ALSO had a health concern —
                        # and a run that is training perfectly is exactly the case where it does
                        # not. MEASURED on `e5small-dr-unified-v11` node 2: judged healthy on every
                        # tick that emitted (correctly — loss 41.46 -> 23.62, descending), so every
                        # row was suppressed by the rule below, and its train stage was SIGKILLed by
                        # its own 36000 s wall at step 1764/2109 — 84 %, 9h56m21s, 10.0 GPU-hours,
                        # then charged a full retrain. 2109 steps x 19.35 s/it = 11.3 h against a
                        # 10 h wall, decidable hours earlier by the projection this line discarded.
                        #
                        # Node 3 of the same run shows the asymmetry that hid it: it happened to
                        # draw a `watch` early, so `last_event_status` opened the gate and all 12 of
                        # its rows carry `projected_overrun_s`. The signal was never missing — it
                        # was conditional on an unrelated fact.
                        #
                        # DERIVED ONCE, HERE, and read twice. `stamp_projected_overrun` stays a
                        # stamp and not a decision (its own docstring's rule), so it fills a scratch
                        # dict that the gate consults and the alert then absorbs — one derivation,
                        # so the row's numbers and the reason it was written cannot disagree.
                        # Only the BEYOND-BAR clearance opens the gate, never the raw projection: a
                        # 40-second overrun on a ten-hour stage is the noise the grace bar exists to
                        # swallow, and this must not become a second, louder spelling of it.
                        _overrun_fields: dict = {}
                        if measured_for_gate is not None:
                            stamp_projected_overrun(
                                _overrun_fields, trajectory, resolved, log_plan,
                                # AUTO (-1.0) is what a real Engine settles the cap to; `None`
                                # resolved to NO grace for any engine lacking it (ENG1-03).
                                grace_cap=getattr(self, "eval_deadline_grace_s", -1.0))
                        _wall_unreachable = wall_unreachable(_overrun_fields)
                        if (verdict.status != "healthy"
                                or last_event_status in ("watch", "broken")
                                or _wall_unreachable):
                            # healthy is normally trace-only, but the transition from an alert
                            # is a durable recovery edge. Without it, projections can only ever discover the
                            # old bad verdict and keep warning after the live curve has recovered.
                            # (A DIAGNOSTIC row: `append_watchdog_row` asserts it for both watchdogs.)
                            alert = {
                                "node_id": node_id, "generation": generation,
                                "status": verdict.status, "reason": reason,
                                "confidence": round(conf, 3),
                                # STAGE ATTRIBUTION on the DURABLE row, not only on the trace span.
                                # The span is a self-described high-volume sidecar; the alert is the
                                # authoritative record and the one every projection reads, so without
                                # these `watchdog_reflection` told the next Researcher "node 0:
                                # training flagged broken ... loss is stuck at its initialization
                                # value" about a node whose training had not started. Additive and
                                # fold-ignored; readers default an absent role to "unknown".
                                "log_role": log_role}
                            if resolved is not None and resolved.stage:
                                alert["stage"] = str(resolved.stage)[:64]
                            # THE MEASUREMENT beside the verdict. `watchdog_reflection` narrates
                            # this row to the next Researcher, and on v7 that meant carrying "loss
                            # pinned at ~23.0 ... no learning trend" into the next proposal about a
                            # run that had gone 24.28 -> 22.90. Additive and fold-ignored; an
                            # absent `trajectory` means the engine measured nothing (an old row, or
                            # a log printing no parseable loss), NEVER that the loss was flat.
                            measured = measured_for_gate
                            if measured is not None:
                                alert["trajectory"] = measured
                                # THE PROJECTION AGAINST THE WALL, while it is still cheap to act
                                # on. The engine has measured a stage's remaining time since the ETA
                                # shipped and compared it against nothing: node 6 was recorded at
                                # "6% of a ~10h run" seven hours before a 28000 s wall killed it and
                                # discarded 7.78 GPU-hours. The deadline judge is the LAST line and
                                # is right to refuse a run two hours short; this is the first one.
                                # Additive and fold-ignored — it records that the engine knew.
                                # Computed above the write gate (see the note there) so that a
                                # HEALTHY stage which cannot finish in time still gets a row; this
                                # absorbs that one derivation rather than repeating it.
                                alert.update(_overrun_fields)
                            if trajectory_veto:
                                alert["trajectory_veto"] = True
                            if role_withheld:
                                # Additive and fold-ignored; readers default its absence to "the
                                # role was not what refused". It names the role that HELD, so the
                                # operator reads "this stage was never kill-eligible" instead of
                                # re-deriving it from a manifest that may since have changed.
                                alert["kill_role_withheld"] = str(log_role)[:32]
                            if not confidence_valid:
                                alert["confidence_valid"] = False
                            if verdict.status == "broken":
                                # The judge's own attribution, on the durable row whether or not it
                                # led anywhere: "the code is wrong" and "the idea is wrong" are the
                                # two answers the search must be able to tell apart afterwards, and
                                # a run that recorded only the second learned the wrong lesson from
                                # every bug. Additive and fold-ignored; absent reads as "unknown".
                                alert["fault"] = str(getattr(verdict, "fault", "unknown"))[:16]
                                # WHAT IT CITED AND WHETHER THE ENGINE FOUND IT, on the durable row
                                # whether or not it led anywhere. This is the audit half of the
                                # authentication: a later reader asking "was that stop justified?"
                                # or "why did the veto hold?" needs the citation AND the engine's
                                # own answer about it, and re-deriving either from a workdir that
                                # has since been reaped is impossible. Additive and fold-ignored;
                                # `citation_resolved` is deliberately omitted rather than written
                                # `false` when nothing was cited, because "cited nothing" and
                                # "cited something absent" are different facts about this judge.
                                _loc = str(getattr(verdict, "evidence_locator", "") or "").strip()
                                if _loc:
                                    alert["evidence_source"] = str(
                                        getattr(verdict, "evidence_source", "none"))[:16]
                                    alert["evidence_locator"] = watchdog_redact(self, _loc)
                                if _citation_resolved is not None:
                                    alert["citation_resolved"] = bool(_citation_resolved)
                            if repair_decided:
                                alert["repair_decided"] = True
                            if stop_decided or repair_decided:
                                # Attribution, additive and fold-ignored, using the SAME vocabulary as
                                # the sibling EV_ASHA_VERDICT row: `stop_decided` is what this monitor
                                # decided, `kill` whether it then WON the shared per-eval claim. Neither
                                # says the node stopped — `_evaluate` terminalizes a claim only
                                # `if kill_signal.get("kill") and not ok`, so a claim against an eval
                                # that already produced a usable result still ends `node_evaluated`. The
                                # node's single terminal remains the authority on the outcome.
                                alert["stop_decided"] = True
                                alert["kill"] = bool(claimed)
                                if not claimed:
                                    alert["kill_superseded_by"] = kill_superseded_by(kill_signal)
                            # Once claimed, `_evaluate` cancels this task group, so a row recording a
                            # decided stop is appended SHIELDED — or the kill would leave NO
                            # diagnostic behind (`append_watchdog_row`).
                            await append_watchdog_row(EV_TRAIN_MONITOR_ALERT, alert, self,
                                                      shield=stop_decided or repair_decided)
                        last_event_status = verdict.status
                        # Committed only once a USABLE verdict came back. Setting it before the call
                        # meant a transient endpoint failure (verdict None) permanently skipped
                        # judging THIS digest: the monitor went quiet until the log changed again,
                        # which for a slow-logging stage is a long window to be blind in. That
                        # protection is now BOUNDED rather than unconditional — after
                        # `_MONITOR_SAME_DIGEST_RETRIES` failed attempts the None branch above commits
                        # the same digest itself, so an endpoint that never answers stops re-sending a
                        # byte-identical prompt every cadence.
                        last_digest = tail
                        if stop_decided or repair_decided:
                            return           # won or lost, this attempt is ending — stop watching it
            except anyio.get_cancelled_exc_class():
                raise                        # cooperative cancellation — must propagate, never be swallowed
            except BudgetExceeded:
                # The tick-level half of the rule above: whatever in a tick reaches the spend
                # ceiling, it ends the watch rather than becoming a skipped tick that is re-paid on
                # the next one (review 2026-09-22, ENG3-02).
                return
            except Exception:  # noqa: BLE001 — a transient per-tick hiccup (disk/LLM/tracer) SKIPS this tick;
                continue                     # it must never disable the watcher for the rest of a long eval
