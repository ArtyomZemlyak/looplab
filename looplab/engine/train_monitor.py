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
- Phase 3: only when `train_monitor_kill` is explicitly enabled, claim a CONFIRMED, sufficiently
  confident `broken` verdict about an IDENTIFIED training stage and reuse the evaluation
  cancel/tree-kill path. The node still terminates once with `reason=monitor_broken`.

Which log is judged is part of the contract, not an implementation detail: the eval writes one
`<stage>.log` per stage plus `setup.log` (dep install) and, on the single-command path, `eval.log`.
Only a stage that runs the candidate's own work can be judged by a TRAINING-health prompt — see
`eval_log_plan` / `resolve_stage_log`.

With intervention off, the monitor never changes the metric champion or node lifecycle. Diagnostics can
still feed the separately configured watchdog-reflection prompt cue on a later proposal.

Design constraints this file must keep (engine invariants):
- **The runtime never calls the LLM** (layering: `runtime` imports nothing above itself), so an LLM-driven
  watchdog CANNOT live in the sandbox — it lives here, in the engine, reading the log FILE the sandbox
  already writes (`_tee_drain(log_path=…)`).
- Verdicts are DIAGNOSTIC events (fold-ignored), so their thread-dependent splice position never changes
  folded state. Replay reads the ordinary terminal event and NEVER re-invokes the monitor LLM.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field

from looplab.core.llm_broker import in_llm_lane

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
    reason: str = Field(description="One short sentence naming the SPECIFIC log evidence for the status.")
    confidence: float = Field(default=0.5, description="Confidence in the status, 0.0 to 1.0.")
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
    "evidence. Be concise and specific about the evidence you saw.")


def training_log_digest(text: str, *, max_lines: int = 40, max_chars: int = 4000) -> str:
    """Reduce a raw training-log tail to a compact digest that preserves the recent TRAJECTORY (the LLM
    context in Phase 1).

    Two kinds of repetition, handled differently:
    - A tqdm/epoch bar overwrites ONE line in place with carriage returns (no newline until it finishes),
      so within a newline-delimited record we keep only the LAST `\\r` segment — the bar's final rendered
      state — collapsing thousands of snapshots to one.
    - Distinct per-step log LINES ("step 1 loss: 0.5", "step 2 loss: 0.4", …) are separate newline
      records and are KEPT: their sequence IS the loss trajectory the monitor must reason over. We keep
      the last `max_lines` of them (the recent trend), then bound to `max_chars`.
    Pure and deterministic — no I/O — so it is unit-testable and safe to reuse anywhere."""
    if not text:
        return ""
    records: list[str] = []
    # normalize only the platform newline pair. Splitting each Windows `\r\n` record on
    # bare `\r` first made its final segment empty, silently disabling both watchdogs on Windows;
    # genuine standalone carriage-return progress redraws remain intact for the collapse below.
    for rec in text.replace("\r\n", "\n").split("\n"):
        seg = rec.split("\r")[-1].rstrip()   # in-place re-renders: keep the final rendered segment only
        if seg.strip():
            records.append(seg)
    out = "\n".join(records[-max_lines:])
    return out[-max_chars:] if len(out) > max_chars else out


# Phase 2 self-pacing constants. After this many CONSECUTIVE healthy verdicts (and no explicit
# agent-requested recheck), the monitor geometrically backs OFF — a steadily-healthy run does not need
# close watching — capped so it never fully stops (a late failure is still caught, just cheaply).
_HEALTHY_BACKOFF_K = 3
_MONITOR_CADENCE_CAP_S = 3600.0     # never wait more than an hour between checks (stays safe on late failures)
# Per-node LLM-call backstop. The adaptive cadence + healthy backoff already bound calls to ~budget/base
# (≈150 even for a 24h eval); this is a never-normally-hit ceiling for a pathological always-changing,
# never-healthy log. Past it the monitor keeps observing (trace) but stops spending on the LLM.
_MAX_MONITOR_LLM_CALLS = 200

# Phase 3 confirmation window. A kill needs this many CONSECUTIVE confident `broken` verdicts about the
# SAME stage log; anything else (a 'watch'/'healthy' tick, a switch to another stage's log) re-arms it
# from zero. The sibling ASHA watchdog will not stop a node on one observation either — it wants a grace
# window, a min-siblings floor and an LLM judge — and the loss here is the same multi-hour training with
# no repair and no retry, so one sampled verdict must not be the whole gate. Deliberately small: the
# watchdog's whole point is to catch a wasted run EARLY, and a second look costs one extra cadence.
_MONITOR_KILL_CONFIRM_TICKS = 2
# The confirmation look is scheduled SOONER than the ordinary cadence (which is up to 30 min on a long
# budget) so arming the gate delays a real kill by seconds, not by another full watch interval.
_MONITOR_CONFIRM_DELAY_S = 30.0

# Which eval phase a log belongs to. The eval writes one log per phase into the node workdir
# (`runtime/command_eval.py`): `setup.log` for the dep install, `<stage>.log` for every resolved
# pipeline stage, and `eval.log` for the single-command path. Those three shapes are the WHOLE naming
# contract, and these roles say which of them a TRAINING-health verdict may be formed about at all
# (see `eval_log_plan` / `resolve_stage_log`, and the `log_role` conjunct of `should_monitor_kill`).
LOG_ROLE_TRAINING = "training"   # the candidate's own work stage — the thing this watchdog judges
LOG_ROLE_SETUP = "setup"         # pip/dep install: no loss trajectory exists yet, by construction
LOG_ROLE_SCORE = "score"         # the engine-appended protected scorer: training already FINISHED
LOG_ROLE_UNKNOWN = "unknown"     # no plan available / a log the plan cannot name — advisory only
# Roles a training-health prompt can say nothing useful about. Feeding one of these to the judge is not
# merely low-value: a short CPU-only scorer that prints framework warnings and has no loss trajectory
# hits three separate clauses of `TrainingVerdict.status`'s `broken` contract at once.
_NON_TRAINING_ROLES = frozenset({LOG_ROLE_SETUP, LOG_ROLE_SCORE})


def next_monitor_sleep(base: float, *, status: Optional[str] = None,
                       recheck_after_s: Optional[float] = None, healthy_streak: int = 0,
                       backoff_after: int = _HEALTHY_BACKOFF_K, cap: float = _MONITOR_CADENCE_CAP_S) -> float:
    """The delay until the NEXT check, given the base cadence and the latest verdict. Pure/deterministic.

    Precedence: an explicit agent-requested `recheck_after_s` wins (the observer self-paces — but never
    faster than `base`, to bound LLM cost, and never slower than `cap`); otherwise a run that has been
    healthy for `backoff_after`+ consecutive checks backs off geometrically (×2 each extra healthy tick,
    bounded); everything else keeps the base cadence."""
    if isinstance(recheck_after_s, (int, float)) and not isinstance(recheck_after_s, bool) and recheck_after_s > 0:
        return min(cap, max(base, float(recheck_after_s)))
    if status == "healthy" and healthy_streak >= backoff_after:
        return min(cap, base * (2.0 ** min(healthy_streak - backoff_after + 1, 6)))
    return base


def _normalize_monitor_confidence(value: object) -> tuple[float, bool]:
    """Return a bounded confidence plus whether the input was finite.

    The safe numeric fallback keeps alerts and traces serializable/observable; callers making an
    intervention decision must additionally require the validity bit.
    """
    try:
        confidence = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0, False
    # max/min is not a validity check: ``min(1.0, NaN)`` evaluates to 1.0 in Python.
    # Treat every non-finite model value as invalid and observable-at-zero, never as kill authority.
    if not math.isfinite(confidence):
        return 0.0, False
    return max(0.0, min(1.0, confidence)), True


def should_monitor_kill(verdict: Optional["TrainingVerdict"], *, enabled: bool, threshold: float,
                        log_role: str = LOG_ROLE_UNKNOWN, broken_streak: int = 0,
                        confirm_ticks: int = _MONITOR_KILL_CONFIRM_TICKS) -> bool:
    """Whether a verdict warrants an EARLY KILL (Phase 3). Pure/deterministic — the WHOLE kill decision
    surface, so what it takes to end a node is one testable expression rather than a scatter of loop
    state.

    Four independent conjuncts, every one fail-closed on its default:

    - `enabled`: the opt-in (`train_monitor_kill`).
    - a `broken` verdict at confidence >= `threshold`. The prompt makes a slow/plateauing-but-progressing
      run 'watch', never 'broken'; 'watch'/'healthy' stay advisory.
    - `log_role` is `LOG_ROLE_TRAINING`: the tail came from a stage that can HAVE training health. A
      verdict about `setup.log`, the protected scorer, or a log nothing could attribute (`
      LOG_ROLE_UNKNOWN`, the default) is advisory evidence and never authority — the monitor must not
      act on a stage it cannot identify.
    - `broken_streak >= confirm_ticks`: the verdict has been REPEATED. One confident tick used to be the
      whole gate, which is out of step with every sibling control in this family — the ASHA watchdog
      needs a grace window, a min-siblings floor AND an LLM judge before it may stop a node, and the
      cost of being wrong here is identical (a multi-hour training discarded with no repair, no retry
      and no refund of its `max_nodes` slot). `confirm_ticks=1` restores single-tick behaviour for a
      caller that wants it; 0 or less cannot disable the requirement, because `broken_streak` counts
      the current tick and is therefore always >= 1 at a real call site.
    """
    if not enabled or verdict is None or verdict.status != "broken":
        return False
    if log_role != LOG_ROLE_TRAINING:
        return False
    try:
        needed = int(confirm_ticks)
    except (TypeError, ValueError, OverflowError):
        return False
    if broken_streak < max(1, needed):
        return False
    confidence, confidence_valid = _normalize_monitor_confidence(verdict.confidence)
    return confidence_valid and confidence >= threshold


def claim_watchdog_kill(kill_signal: dict, cancel, *, reason: str, terminal_reason: str,
                        confidence: Optional[float] = None) -> bool:
    """Atomically claim the shared per-eval watchdog terminal in the cooperative event loop.

    The training-health and ASHA-rank monitors are sibling tasks and can reach a kill decision on the
    same tick. Only the first decision may own the persisted failure explanation; a later sibling still
    exits because the winner has already set ``cancel``. Returns whether this caller won the claim.
    """
    # there is deliberately no await between this guard and the one-shot dict update. Both
    # watchdogs run on the same cooperative event loop, so the first writer owns reason + terminal_reason
    # as one indivisible decision instead of a later task producing a mixed/overwritten terminal record.
    if kill_signal.get("kill"):
        return False
    payload = {
        "kill": True,
        "reason": str(reason),
        "terminal_reason": str(terminal_reason),
    }
    if confidence is not None:
        payload["confidence"] = confidence
    kill_signal.update(payload)
    cancel.set()
    return True


def last_lifecycle_row(rows, event_type: str, node_id: int, generation: int) -> Optional[dict]:
    """The NEWEST row of `event_type` belonging to exactly this `(node_id, generation)`, as its data
    dict — or None when the watchdog never spoke for this lifecycle.

    Both watchdogs need this on re-entry: `resume` can restart an observer inside the same node
    generation, and without recovering the last durable row the first healthy verdict looks like a
    first observation, so a pre-crash warning is lost instead of being closed (doc 25 EC-04). Three
    sites hand-rolled the scan — both resume recoveries and `asha_monitor.latest_train_verdict` — with
    the bool-guarded field validation copied verbatim.

    That guard is the point, and it is easy to get subtly wrong: rows are UNTRUSTED append-only data,
    and `isinstance(True, int)` is True in Python, so a payload carrying `node_id: true` matches a
    plain `== node_id` test against node 1 and hands a watchdog another lifecycle's history. The
    generation half matters the same way.

    Returns the newest MATCHING row even when its contents are unusable — callers decide what an
    unreadable payload means, and every one of them treats it as "no history", never as a reason to
    keep scanning backwards into an older row that would answer for a stale tick.

    Pure; safe on an empty or None row list.
    """
    for event in reversed(list(rows or ())):
        if getattr(event, "type", None) != event_type:
            continue
        data = getattr(event, "data", None) or {}
        nid, gen = data.get("node_id"), data.get("generation")
        if isinstance(nid, bool) or not isinstance(nid, int) or nid != node_id:
            continue
        if isinstance(gen, bool) or not isinstance(gen, int) or gen != generation:
            continue
        return data
    return None


# ------------------------------------------------------------------ which log belongs to which stage
_SETUP_LOG = "setup.log"
_SINGLE_COMMAND_LOG = "eval.log"
# `score` is RESERVED for the engine-appended protected scoring stage (`engine/eval_stages.py`
# appends it to every Developer manifest, and `command_eval.validate_stages(reserved=("score",))`
# refuses it to the agent). An operator-declared pipeline MAY own the name, and there it means the
# same thing — so an exact `score` match is an unambiguous "this stage is the scorer, not training".
_SCORE_STAGE = "score"


def _log_name_key(name: str) -> str:
    """Case-folded log basename, matching `_log_path_key`'s Windows handling."""
    return os.path.normcase(str(name))


@dataclass(frozen=True)
class EvalLogPlan:
    """Every log file ONE eval attempt can write, mapped to the stage that writes it and its role.

    Built by the engine from the SAME resolved stage list the eval runs (`_resolved_stages`), so the
    watchdogs stop guessing which phase produced the bytes they are reading.
    """

    roles: dict                  # case-folded basename -> (stage name or None, LOG_ROLE_*)
    stage_names: tuple = ()      # the resolved pipeline order; () for a single-command eval


def eval_log_plan(stages) -> EvalLogPlan:
    """The log plan for a resolved eval pipeline. Pure/deterministic — no I/O — so what the watchdogs
    are allowed to judge is unit-testable without a filesystem.

    `stages` is `Engine._resolved_stages`' output: the ordered pipeline, or `[]`/None for the classic
    single-command eval (whose one command trains AND scores in one process — see the
    `command_eval.py` comment on that branch — so its `eval.log` IS a training log).

    Every stage except the protected `score` scorer is a work stage, which is exactly the set
    `command_eval` already treats as "where TRAINING runs" (it enables the divergence `health_check`
    for declared stages and disables it for the short scorer `cmd`). A stage that shadows `setup.log`
    wins the name over the dep install, because its output is what actually lands in that file.
    """
    names = tuple(str(s.get("name")) for s in (stages or [])
                  if isinstance(s, dict) and s.get("name") is not None)
    roles: dict = {_log_name_key(_SETUP_LOG): (None, LOG_ROLE_SETUP)}
    if names:
        for name in names:
            roles[_log_name_key(f"{name}.log")] = (
                name, LOG_ROLE_SCORE if name == _SCORE_STAGE else LOG_ROLE_TRAINING)
    else:
        roles[_log_name_key(_SINGLE_COMMAND_LOG)] = (None, LOG_ROLE_TRAINING)
    return EvalLogPlan(roles=roles, stage_names=names)


@dataclass(frozen=True)
class ActiveStageLog:
    """The log a watchdog tick is looking at, plus WHICH eval phase wrote it."""

    path: Path
    stage: Optional[str]
    role: str


def resolve_stage_log(workdir, plan: Optional[EvalLogPlan] = None) -> Optional[ActiveStageLog]:
    """The workdir's live log, ATTRIBUTED to the eval phase that writes it. None when there is nothing
    the caller may read (no `*.log` yet, or none the plan can name).

    Freshest-mtime still tracks the moving active stage — that part of the old heuristic was right, and
    the sandbox's live stage cursor genuinely is unobservable from here. What was wrong was acting on
    the answer without knowing WHICH stage it named:

    REVIEW NOTE (superseded): this glob used to be deliberately broad ("the failure is benign — a
    slightly-less-relevant tail feeds an ADVISORY verdict"). That premise died when `train_monitor_kill`
    became the default: the freshest `*.log` is `setup.log` during a minutes-long pip install and
    `score.log` during the ALWAYS-appended final score stage (`engine/eval_stages.py`), and both were
    fed to a training-health judge holding kill authority, with the changed-digest gate guaranteeing a
    fresh LLM call on every file switch. A `score.log` verdict killed the training that had just
    SUCCEEDED. The engine knows the resolved stage list; pass it in (`eval_log_plan`) and the answer is
    named instead of guessed.

    With a plan, logs the plan cannot name are IGNORED rather than read: a stray `*.log` the candidate's
    own code drops is at best a duplicate of the stage log (which captures the subprocess's whole
    stdout/stderr), and silently judging unattributable bytes is the exact defect above. Without a plan
    the old freshest-file answer stands, tagged `LOG_ROLE_UNKNOWN` so callers can degrade to advisory.
    """
    try:
        logs = list(Path(workdir).glob("*.log"))
    except OSError:
        return None
    if plan is not None:
        logs = [p for p in logs if _log_name_key(p.name) in plan.roles]
    if not logs:
        return None
    try:
        newest = max(logs, key=lambda f: f.stat().st_mtime)
    except OSError:
        return None
    if plan is None:
        return ActiveStageLog(path=newest, stage=None, role=LOG_ROLE_UNKNOWN)
    stage, role = plan.roles[_log_name_key(newest.name)]
    return ActiveStageLog(path=newest, stage=stage, role=role)


def monitor_stage_context(resolved: Optional[ActiveStageLog],
                          plan: Optional[EvalLogPlan] = None) -> str:
    """One line telling the observer WHICH stage's log it is about to read. Pure/deterministic.

    The judge used to receive a tail headed "LIVE TRAINING LOG" with nothing saying which phase of the
    eval produced it, so it had no way to notice it was being shown a scorer. This rides in the
    caller-supplied `context` (the system prompt and the log header stay verbatim — prompt text is a
    contract), and it is the only thing that makes an UNKNOWN attribution visible to the model at all.
    """
    if resolved is None:
        return ""
    if resolved.role == LOG_ROLE_UNKNOWN or plan is None:
        return ("NOTE: this log could not be attributed to a named eval stage, so it may not be the "
                "training stage's output. Judge only what the lines themselves support.")
    if not plan.stage_names:
        return ("This eval runs ONE command that both trains and scores in a single process; the log "
                "below is that command's complete live output.")
    order = " -> ".join(plan.stage_names)
    if resolved.stage in plan.stage_names:
        position = plan.stage_names.index(resolved.stage) + 1
        return (f"This is the live log of pipeline stage {resolved.stage!r} "
                f"(stage {position} of {len(plan.stage_names)}; the pipeline is {order}). "
                "Judge THIS stage's output only.")
    return f"This eval runs the pipeline {order}. Judge only the stage output shown below."


def active_training_log(workdir, plan: Optional[EvalLogPlan] = None) -> Optional[Path]:
    """The live log a TRAINING observer may read, or None.

    A thin role filter over `resolve_stage_log`: `setup.log` (dep install) and the protected `score`
    stage carry no training signal at all, so they are not returned — a tick during those phases has
    nothing to observe, exactly like a tick before the first log exists. Without a plan this is the
    historical freshest-`*.log` answer (see `resolve_stage_log`'s superseded review note).
    """
    resolved = resolve_stage_log(workdir, plan)
    if resolved is None or resolved.role in _NON_TRAINING_ROLES:
        return None
    return resolved.path


@dataclass(frozen=True)
class TrainingLogCursor:
    """The immutable boundary between two eval attempts for one existing log file."""

    offset: Optional[int]
    identity: Optional[tuple[int, int]]
    probe_start: int = 0
    probe: Optional[bytes] = None


@dataclass(frozen=True)
class TrainingLogSnapshot:
    """Best-effort cursors for every ``*.log`` that existed before an eval attempt started."""

    cursors: dict[str, TrainingLogCursor]
    complete: bool = True


_CURSOR_PROBE_BYTES = 64


def _log_path_key(path: Path) -> str:
    """Stable-enough process-local path identity, including Windows case folding."""
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _file_identity(stat_result) -> Optional[tuple[int, int]]:
    """Return an OS file identity when the filesystem exposes one (Windows does via ``st_ino`` too).

    Deliberately a SUBSET of `core/atomicio.file_identity`: this asks only "is this the same file?",
    never "is it unchanged?" — the monitor tails a log that is expected to grow between reads, so
    including size/mtime would report a rotation on every ordinary append. It also returns None when
    the filesystem cannot prove identity (inode 0), which the canonical tuple has no way to express.
    """
    try:
        device = int(stat_result.st_dev)
        inode = int(stat_result.st_ino)
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None
    return (device, inode) if inode else None


def snapshot_training_logs(workdir) -> TrainingLogSnapshot:
    """Capture attempt-start byte cursors for the workdir's existing stage logs.

    The small boundary probe distinguishes append from truncate-and-regrow even when a filesystem does
    not expose a useful inode. A path that cannot be snapshotted is retained as an unreadable cursor so
    a transient permission/stat failure cannot make a later monitor consume prior-attempt bytes.
    """
    try:
        paths = list(Path(workdir).glob("*.log"))
    except OSError:
        return TrainingLogSnapshot({}, complete=False)
    cursors: dict[str, TrainingLogCursor] = {}
    for path in paths:
        key = _log_path_key(path)
        try:
            with open(path, "rb") as fh:
                stat_result = os.fstat(fh.fileno())
                size = max(0, int(stat_result.st_size))
                probe_start = max(0, size - _CURSOR_PROBE_BYTES)
                fh.seek(probe_start)
                probe = fh.read(size - probe_start)
            cursors[key] = TrainingLogCursor(
                offset=size,
                identity=_file_identity(stat_result),
                probe_start=probe_start,
                probe=probe,
            )
        except (OSError, TypeError, ValueError, OverflowError):
            cursors[key] = TrainingLogCursor(offset=None, identity=None, probe=None)
    return TrainingLogSnapshot(cursors)


def read_training_tail_raw(workdir, *, max_read_bytes: int = 131_072,
                           snapshot: Optional[TrainingLogSnapshot] = None,
                           plan: Optional[EvalLogPlan] = None) -> str:
    """The RAW (un-digested) utf-8 tail of the active stage log — the last `max_read_bytes`. Bounded
    seek-to-tail read so a multi-GB log never loads into memory; a torn leading line is dropped by the
    'replace' decode. '' when there is no log yet. Used by the ASHA watchdog, which feeds it to the
    eval's own metric reader (digesting first would collapse the very metric lines it must parse).

    With an attempt-start ``snapshot``, bytes that predate the current eval are excluded. Replacement,
    rotation, and truncation start a fresh file at byte zero; an unreadable/ambiguous old boundary fails
    closed to an empty tail rather than reusing a stale metric.

    With an eval ``plan`` the read is confined to the stage logs that can carry training output —
    `setup.log` and the protected scorer return '' rather than a tail, so neither watchdog classifies
    (or ranks) another phase's bytes as this training's.
    """
    path = active_training_log(workdir, plan)
    if path is None:
        return ""
    limit = max(0, int(max_read_bytes))
    if limit == 0:
        return ""
    key = _log_path_key(path)
    cursor = snapshot.cursors.get(key) if snapshot is not None else None
    if snapshot is not None and not snapshot.complete:
        return ""
    try:
        with open(path, "rb") as fh:
            stat_result = os.fstat(fh.fileno())
            size = max(0, int(stat_result.st_size))
            current_identity = _file_identity(stat_result)
            if cursor is None and snapshot is not None and current_identity is not None:
                # A rotation can rename the old file to another ``*.log`` path. Follow identity across
                # that rename so the renamed prior-attempt bytes are not mistaken for a brand-new log.
                cursor = next((old for old in snapshot.cursors.values()
                               if old.identity == current_identity), None)
            if cursor is None and snapshot is not None:
                # Some filesystems expose no stable inode. A matching old EOF probe is sufficient to
                # classify an unknown path as a renamed old log; a false match only suppresses advisory
                # evidence (safe), whereas treating it as new could resurrect a stale kill metric.
                for old in snapshot.cursors.values():
                    if old.offset is None or old.probe is None or size < old.offset:
                        continue
                    fh.seek(old.probe_start)
                    if fh.read(len(old.probe)) == old.probe:
                        cursor = old
                        break
            floor = 0
            if cursor is not None:
                if cursor.offset is None or cursor.probe is None:
                    return ""
                replaced = (cursor.identity is not None and current_identity is not None
                            and cursor.identity != current_identity)
                truncated = size < cursor.offset
                boundary_changed = False
                if not replaced and not truncated:
                    fh.seek(cursor.probe_start)
                    boundary_changed = fh.read(len(cursor.probe)) != cursor.probe
                # only a proven append may inherit the old EOF. Rotation/replacement or a
                # truncate-and-regrow (including past the old size before the first watchdog tick) starts
                # at zero; if identity is unavailable, the boundary probe supplies the same protection.
                floor = 0 if (replaced or truncated or boundary_changed) else cursor.offset
            fh.seek(max(floor, size - limit))
            raw = fh.read(limit)
    except (OSError, TypeError, ValueError, OverflowError):
        return ""
    return raw.decode("utf-8", "replace")


def read_training_tail(workdir, *, max_read_bytes: int = 131_072,
                       max_lines: int = 40, max_chars: int = 4000,
                       snapshot: Optional[TrainingLogSnapshot] = None,
                       plan: Optional[EvalLogPlan] = None) -> str:
    """Read only the LAST `max_read_bytes` of the active stage log and digest it (collapse tqdm
    re-renders, keep the recent trajectory). '' when there is no log yet.

    REVIEW NOTE (accepted, not fixed): this bounded seek-to-tail pattern (stat size → seek → read) also
    appears inline in `serve/routers/runs.py::_tail` and `events/eventstore.py::_disk_last_seq`. Each copy
    differs in its line-boundary handling (and there is no existing shared helper — `sandbox._clamp_tail_bytes`
    clamps an in-memory STRING, not a file), so a 3-call-site extraction is deferred as not worth the churn."""
    raw = read_training_tail_raw(workdir, max_read_bytes=max_read_bytes, snapshot=snapshot, plan=plan)
    if not raw:
        return ""
    return training_log_digest(raw, max_lines=max_lines, max_chars=max_chars)


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

    def _training_verdict(self, digest: str, context: str,
                          stage_context: str = "") -> Optional[TrainingVerdict]:
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
        Additive by construction: `_MONITOR_SYSTEM` and the log header are unchanged (prompt strings are
        contracts), and an empty `stage_context` reproduces the historical message byte for byte."""
        client = getattr(getattr(self, "developer", None), "client", None)
        if client is None:
            return None
        messages = [
            {"role": "system", "content": _MONITOR_SYSTEM},
            {"role": "user", "content": ((context + "\n\n") if context else "")
             + ((stage_context + "\n\n") if stage_context else "")
             + "LIVE TRAINING LOG (recent tail):\n" + digest
             + "\n\nClassify this run's health from the log evidence above."},
        ]
        try:
            from looplab.core.parse import parse_structured
            return parse_structured(client, messages, TrainingVerdict)
        except Exception:  # noqa: BLE001 — a parser/endpoint failure means "no verdict this tick", not a crash
            return None

    @in_llm_lane("enrichment")
    async def _monitor_training(self, node_id: int, generation: int, workdir, cancel,
                                context: str = "", kill_signal: Optional[dict] = None,
                                log_snapshot: Optional[TrainingLogSnapshot] = None,
                                log_plan: Optional[EvalLogPlan] = None) -> None:
        """Tail the live training log every `train_monitor_interval_s`, ask the Developer to judge its
        health, record the verdict, and (opt-in) kill a broken run early.

        WHICH log (`log_plan`, from `eval_log_plan`): the monitor lives across the WHOLE eval — setup,
        every stage, the always-appended score stage — so "the freshest `*.log`" is not a synonym for
        "the training". With a plan it reads only the stage logs that can carry training output and the
        judge is TOLD which stage it is looking at; `setup.log` and the protected scorer produce no tick
        at all. Without a plan it keeps the historical freshest-file read but can never kill (see
        `should_monitor_kill`) — the monitor must not act on a stage it cannot identify.

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
        is never killed. The alert row records whether this monitor actually OWNED that terminal, so an
        audit of "which watchdog stopped what" reads the durable log instead of guessing.

        With no LLM client wired it degrades to trace-only observation. Exits when the eval finishes
        (`cancel`, or the task group is cancelled); a per-tick hiccup skips the tick and never disables the
        watcher for the rest of a long eval."""
        # Local: `evaluate` imports this module, so a module-level import would be a cycle.
        from looplab.engine.evaluate import _watch_limiter
        import anyio

        from looplab.events.types import DIAGNOSTIC_EVENTS, EV_TRAIN_MONITOR_ALERT
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
        try:
            prior_rows = await anyio.to_thread.run_sync(
                self.store.read_all, limiter=_watch_limiter())
            prior = last_lifecycle_row(
                prior_rows, EV_TRAIN_MONITOR_ALERT, node_id, generation)
            if prior is not None:
                status = str(prior.get("status") or "").strip().lower()
                last_event_status = status if status in ("healthy", "watch", "broken") else None
        except Exception:  # noqa: BLE001 - advisory history lookup; the live monitor still proceeds
            pass
        llm_calls = 0
        # Phase 3 arming state. `broken_streak` counts CONSECUTIVE confident-broken verdicts about the
        # same stage log; `armed_key` is the log they were about, so a stage change (train.log ->
        # score.log) can never let two different subjects confirm each other.
        broken_streak = 0
        armed_key: Optional[str] = None
        while True:
            await anyio.sleep(next_sleep)    # only cancellation (eval finished) unwinds the task, from here
            if cancel.is_set():
                return
            try:
                def _observe_log():
                    """ONE attributed read per tick, in the worker thread: which stage log is live, and
                    (only when that stage can HAVE training health) its digested tail. `setup.log` and
                    the protected scorer return no tail at all — there is nothing for a training-health
                    prompt to say about a dep install or about a scorer running after the training
                    already finished, and asking anyway is what produced a confident wrong verdict."""
                    resolved = resolve_stage_log(workdir, log_plan)
                    if resolved is None or resolved.role in _NON_TRAINING_ROLES:
                        return resolved, ""
                    return resolved, read_training_tail(workdir, snapshot=log_snapshot, plan=log_plan)

                resolved, tail = await anyio.to_thread.run_sync(
                    _observe_log, limiter=_watch_limiter())
                log_role = resolved.role if resolved is not None else LOG_ROLE_UNKNOWN
                log_key = _log_path_key(resolved.path) if resolved is not None else None
                if log_key != armed_key:
                    broken_streak, armed_key = 0, log_key   # a different subject re-arms from zero
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
                if not tail or (tail == last_digest and broken_streak == 0):
                    continue                 # no live log yet, or nothing new since last tick -> no LLM call
                # Open the span BEFORE the LLM call so the observer's LLM turn bands under `train_monitor`
                # (not the enclosing `evaluate`) — the same trace-attribution fix `_triage_crash` uses.
                with self.tracer.span("train_monitor", node_id=node_id) as sp:
                    sp.set_many(generation=generation, log_role=log_role,
                                digest_lines=tail.count("\n") + 1, digest_chars=len(tail))
                    if resolved is not None and resolved.stage:
                        sp.set("stage", resolved.stage)
                    # Per-node backstop on LLM cost (the adaptive cadence + healthy-backoff are the primary
                    # budget control; this only bounds a pathological run whose digest keeps changing while
                    # staying non-healthy). Past the cap we keep OBSERVING (trace-only) but stop calling the
                    # LLM. Surfaced on the span — a silent cap would read as "all healthy" when it isn't.
                    verdict = None
                    if llm_calls >= _MAX_MONITOR_LLM_CALLS:
                        sp.set("llm_capped", True)
                    elif cancel.is_set():
                        # The eval ended (finished, operator abort, or the SIBLING ASHA watchdog already
                        # claimed the kill) while this tick was reading the log. Starting the call now
                        # would buy a verdict about a node that no longer exists AND — because the call
                        # is deliberately un-abandonable below — hold node teardown open for a whole
                        # endpoint timeout to pay for it. Checked here, immediately before the spend.
                        sp.set("cancelled_before_call", True)
                        return
                    else:
                        # the verdict is advisory, but its client usage is billable and is
                        # recorded on shared run state. Join an in-flight call on eval cancellation so
                        # no detached worker can emit cost after the node/run has finalized. Endpoint
                        # timeouts remain the upper bound for this ownership hand-off.
                        verdict = await anyio.to_thread.run_sync(
                            self._training_verdict, tail, context,
                            monitor_stage_context(resolved, log_plan), abandon_on_cancel=False)
                        llm_calls += 1
                    if verdict is not None:
                        conf, confidence_valid = _normalize_monitor_confidence(verdict.confidence)
                        # The reason is LLM text derived from the raw log; redact it before it lands in the
                        # trace / event log / attention feed, matching how `_evaluate` stores stderr tails.
                        _redact = getattr(self, "_redact", None)
                        reason = (verdict.reason or "")
                        reason = (_redact(reason) if callable(_redact) else reason)[:300]
                        # The durable event keeps the fuller reason (300); the trace span carries a shorter
                        # preview (200) — spans are a high-volume sidecar, the event is the authoritative record.
                        sp.set_many(status=verdict.status, confidence=round(conf, 3), reason=reason[:200])
                        if not confidence_valid:
                            sp.set("confidence_valid", False)
                        healthy_streak = healthy_streak + 1 if verdict.status == "healthy" else 0
                        broken_streak = broken_streak + 1 if verdict.status == "broken" else 0
                        next_sleep = next_monitor_sleep(
                            base, status=verdict.status, recheck_after_s=verdict.recheck_after_s,
                            healthy_streak=healthy_streak)
                        # Phase 3 intervention (opt-in): a CONFIRMED, confident 'broken' verdict about an
                        # identified training stage is tree-killed EARLY. Hand the reason to `_evaluate`
                        # via `kill_signal`, set `cancel` (same path as an operator abort), and stop
                        # watching — `_evaluate` writes the single terminal node_failed.
                        # No `or`-coercion on the confidence bar. `x or 0.0` turns an unset/None/0.0
                        # knob into a ZERO threshold — i.e. EVERY `broken` verdict kills — which is
                        # the wrong direction to fail in, and it matters much more now that
                        # `train_monitor_kill` defaults to True. Mirrors the identical rule in
                        # `asha_monitor.py`; a non-numeric knob falls back to the schema default.
                        _kc = getattr(self, "_train_monitor_kill_confidence", 0.8)
                        threshold = (float(_kc) if isinstance(_kc, (int, float))
                                     and not isinstance(_kc, bool) else 0.8)
                        stop_decided = kill_signal is not None and should_monitor_kill(
                            verdict, enabled=getattr(self, "_train_monitor_kill", False),
                            threshold=threshold, log_role=log_role, broken_streak=broken_streak)
                        # CLAIM BEFORE RECORDING. The sibling ASHA watchdog can decide on the same tick,
                        # and only one of them owns the node's terminal — so the alert must state what
                        # actually happened to the node, not what this monitor wanted. The guard->update
                        # inside `claim_watchdog_kill` is await-free, so the answer is exact.
                        claimed = stop_decided and claim_watchdog_kill(
                            kill_signal, cancel, reason=reason, terminal_reason="monitor_broken",
                            confidence=round(conf, 3))
                        if stop_decided:
                            sp.set_many(stop_decided=True, kill=bool(claimed))
                        elif (verdict.status == "broken" and broken_streak == 1
                              and log_role == LOG_ROLE_TRAINING and kill_signal is not None
                              and getattr(self, "_train_monitor_kill", False)):
                            # ARMED, not acting: re-look promptly instead of after another full cadence
                            # (up to 30 min on a long budget), so confirmation costs seconds of a
                            # multi-hour budget rather than a meaningful slice of it.
                            next_sleep = min(next_sleep, _MONITOR_CONFIRM_DELAY_S)
                            sp.set("kill_armed", True)
                        sp.set("next_check_s", round(next_sleep, 2))
                        if (verdict.status != "healthy"
                                or last_event_status in ("watch", "broken")):
                            # healthy is normally trace-only, but the transition from an alert
                            # is a durable recovery edge. Without it, projections can only ever discover the
                            # old bad verdict and keep warning after the live curve has recovered.
                            assert EV_TRAIN_MONITOR_ALERT in DIAGNOSTIC_EVENTS
                            alert = {
                                "node_id": node_id, "generation": generation,
                                "status": verdict.status, "reason": reason,
                                "confidence": round(conf, 3)}
                            if not confidence_valid:
                                alert["confidence_valid"] = False
                            if stop_decided:
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
                                    alert["kill_superseded_by"] = str(
                                        kill_signal.get("terminal_reason") or "")[:64]
                            # Once claimed, `_evaluate` cancels this task group; `self._write_lock` is
                            # the next checkpoint, so an unshielded append would be preempted and the
                            # kill would leave NO diagnostic behind. Bounded (one append), and the same
                            # shielding `_evaluate` uses for its own promised terminal.
                            with anyio.CancelScope(shield=stop_decided):
                                async with self._write_lock:
                                    self.store.append(EV_TRAIN_MONITOR_ALERT, alert)
                        last_event_status = verdict.status
                        # Committed only once a USABLE verdict came back. Setting it before the call
                        # meant a transient endpoint failure (verdict None) permanently skipped
                        # judging THIS digest: the monitor went quiet until the log changed again,
                        # which for a slow-logging stage is a long window to be blind in.
                        last_digest = tail
                        if stop_decided:
                            return           # won or lost, this node is ending — stop watching it
            except anyio.get_cancelled_exc_class():
                raise                        # cooperative cancellation — must propagate, never be swallowed
            except Exception:  # noqa: BLE001 — a transient per-tick hiccup (disk/LLM/tracer) SKIPS this tick;
                continue                     # it must never disable the watcher for the rest of a long eval
