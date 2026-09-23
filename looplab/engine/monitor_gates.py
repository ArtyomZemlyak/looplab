"""The live-log watchdog's DECISIONS, as pure functions of what one tick knows.

When to look again (`next_monitor_sleep`); what it takes to END a stage (`should_monitor_kill`) and
to stop it FOR REPAIR (`should_monitor_repair`, where `citation_authenticates` is the one
out-of-band channel that may lift the trajectory veto); whether a first `broken` arms the prompt
confirmation look (`_confirmation_would_act`); how a non-finite confidence stays observable but
powerless (`_normalize_monitor_confidence`); the shared per-eval kill claim both watchdogs race for
(`claim_watchdog_kill`); and the resume scan both recover their last durable row with
(`last_lifecycle_row`) — with the pacing and confirmation constants and the role sets those gates
are written against.

Pure and deterministic, so each has a truth table instead of being reachable only through a
simulated multi-hour eval (`tests/test_train_monitor.py`, `tests/test_verdict_citation_authenticates.py`).
Every conjunct fails CLOSED on a missing input; none authorizes on one.

Split out of `engine/train_monitor.py` by review 2026-09-22 (ENG3-13, doc 50 EM-06), with
`engine/loss_trajectory.py` and `engine/eval_log_plan.py`. Moved VERBATIM, comments included;
`train_monitor` re-exports every name as the SAME object. The loop's own tuning constants
(`_MAX_MONITOR_LLM_CALLS`, the arm bounds, the same-digest retry) moved WITH the block that
documents them, and the loop reads them through `train_monitor`'s import of them — so a test that
lowers one patches `train_monitor`, the module whose code reads it; a patch here would reach no
reader (`tests/test_train_monitor_split.py` refuses it).
"""
from __future__ import annotations

import math
from typing import TYPE_CHECKING, Optional

from looplab.engine.loss_trajectory import LossTrajectory, trajectory_vetoes_kill
# The log-role vocabulary lives in `events/types.py`, where readers below the engine can name a role
# (see the note at `train_monitor`'s own import of it); what each role MEANS to the watchdog is here.
from looplab.events.types import (
    LOG_ROLE_AMBIGUOUS,
    LOG_ROLE_SCORE,
    LOG_ROLE_SETUP,
    LOG_ROLE_TRAINING,
    LOG_ROLE_UNKNOWN,
)

if TYPE_CHECKING:                  # an annotation only: `train_monitor` imports this module
    from looplab.engine.train_monitor import TrainingVerdict


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
# SAME stage log. EVERYTHING else re-arms the gate from zero, and the list below is exhaustive on
# purpose — the previous wording said "anything else" while the code only ever touched the streak
# inside `if verdict is not None`, so a tick the model ANSWERED but whose answer did not validate left
# the arm standing:
#   • a parseable 'watch'/'healthy' verdict;
#   • a switch to another stage's log (`armed_key`);
#   • a tick that produced NO parseable verdict — an endpoint failure, model output that fails schema
#     validation, or the per-node LLM cap. That second case was the commit's own cited mitigation:
#     told the stage was a scorer, `deepseek-v4-flash` answers `unknown`, which is not in
#     `TrainingVerdict.status`'s `Literal[healthy|watch|broken]`, so `parse_structured` returns None.
#     The model DECLINING to confirm must not read the same as never having been asked. Reproduced:
#     arm, six `unknown` ticks, one `broken` -> killed;
#   • the arm outliving `_MONITOR_ARM_TTL_S` or spending `_MONITOR_ARM_MAX_LOOKS` (below).
# The sibling ASHA watchdog will not stop a node on one observation either — it wants a grace
# window, a min-siblings floor and an LLM judge — and the loss here is the same multi-hour training with
# no repair and no retry, so one sampled verdict must not be the whole gate. Deliberately small: the
# watchdog's whole point is to catch a wasted run EARLY, and a second look costs one extra cadence.
_MONITOR_KILL_CONFIRM_TICKS = 2
# The confirmation look is scheduled SOONER than the ordinary cadence (which is up to 30 min on a long
# budget) so arming the gate delays a real kill by seconds, not by another full watch interval.
_MONITOR_CONFIRM_DELAY_S = 30.0
# BOUNDS ON THE ARM. The arming tick bypasses the changed-digest gate so "diverged, then stopped
# printing" cannot survive by saying nothing — but that bypass held for as long as the arm did, and the
# arm had no bound at all. On a frozen log with a dead endpoint the monitor issued one billable call per
# cadence, every one carrying a BYTE-IDENTICAL prompt, until `_MAX_MONITOR_LLM_CALLS` (~100 minutes of
# re-asking the same question; measured: 25 calls, 1 distinct prompt, log bytes unchanged). Two
# independent bounds, both of which simply disarm:
#   • looks: the bypass buys exactly ONE re-ask of an unchanged digest. A second identical prompt is
#     not additional evidence — the confirmation defends against sampling noise, and on a frozen log
#     there is no new sample to take. A tick that DOES see new bytes was never a bypass, so a live log
#     is unaffected;
#   • wall clock: an arm older than this is stale regardless of why, so a slow or hanging endpoint
#     cannot leave a kill primed indefinitely. Sized well above one confirmation delay plus a full
#     provider timeout, and far below the runaway.
_MONITOR_ARM_MAX_LOOKS = 1
_MONITOR_ARM_TTL_S = 300.0
# The SAME digest is re-asked at most this many times before it is retired as judged. `last_digest` is
# deliberately committed only when a verdict parses (a transient endpoint failure must not permanently
# skip judging the current digest — for a slow-logging stage that is a long window to be blind in), but
# "only on success" and "forever" are different promises: against an endpoint that never answers, an
# unchanged log was re-sent every cadence. After this many failures the digest is retired and the
# monitor goes quiet until the log actually changes, which is the pre-arm behaviour it was protecting.
_MONITOR_SAME_DIGEST_RETRIES = 2

# Which eval phase a log belongs to. The eval writes one log per phase into the node workdir
# (`runtime/command_eval.py`): `setup.log` for the dep install, `<stage>.log` for every resolved
# pipeline stage, and `eval.log` for the single-command path. Those three shapes are the WHOLE naming
# contract, and these roles say which of them a TRAINING-health verdict may be formed about at all
# (see `eval_log_plan` / `resolve_stage_log`, and the `log_role` conjunct of `should_monitor_kill`).
# The vocabulary itself is defined in `events/types.py` and imported at the top of this module (see
# the note there); only what each role MEANS to this watchdog is decided here.
#
# Roles a training-health prompt can say nothing useful about, so they produce no tick at all. Feeding
# one of these to the judge is not merely low-value: a short CPU-only scorer that prints framework
# warnings and has no loss trajectory hits three separate clauses of `TrainingVerdict.status`'s
# `broken` contract at once. `LOG_ROLE_AMBIGUOUS` joins them because a filename with two possible
# writers cannot be attributed at all (see `eval_log_plan`), and "we do not know whose bytes these
# are" is the same answer as "these are not training bytes" for a prompt that must name evidence.
_NON_TRAINING_ROLES = frozenset({LOG_ROLE_SETUP, LOG_ROLE_SCORE, LOG_ROLE_AMBIGUOUS})
# The ONLY role that may end a node. A set, so the kill conjunct reads as membership rather than an
# equality that silently widens when a role is added: `LOG_ROLE_WORK` is judged like training but is
# deliberately NOT here.
_KILL_ELIGIBLE_ROLES = frozenset({LOG_ROLE_TRAINING})


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
                        confirm_ticks: int = _MONITOR_KILL_CONFIRM_TICKS,
                        trajectory: Optional["LossTrajectory"] = None) -> bool:
    """Whether a verdict warrants an EARLY KILL (Phase 3). Pure/deterministic — the WHOLE kill decision
    surface, so what it takes to end a node is one testable expression rather than a scatter of loop
    state.

    Five independent conjuncts, every one fail-closed on its default:

    - `enabled`: the opt-in (`train_monitor_kill`).
    - a `broken` verdict at confidence >= `threshold`. The prompt makes a slow/plateauing-but-progressing
      run 'watch', never 'broken'; 'watch'/'healthy' stay advisory.
    - `log_role` is in `_KILL_ELIGIBLE_ROLES`, i.e. `LOG_ROLE_TRAINING`: the tail is provably the run's
      own training — `eval_log_plan` grants that role to a log that is the WHOLE eval, or to the one
      stage a manifest DECLARES is the training loop (see that function on why a declaration is
      admissible where a stage name is not). A verdict
      about `setup.log`, the pipeline's scorer, an unattributable filename, a pipeline WORK stage
      (`LOG_ROLE_WORK` — judged, but the plan cannot prove it is the training step) or a log nothing
      could attribute at all (`LOG_ROLE_UNKNOWN`, the default) is advisory evidence and never
      authority — the monitor must not act on a stage it cannot identify.
    - `broken_streak >= confirm_ticks`: the verdict has been REPEATED. One confident tick used to be the
      whole gate, which is out of step with every sibling control in this family — the ASHA watchdog
      needs a grace window, a min-siblings floor AND an LLM judge before it may stop a node, and the
      cost of being wrong here is identical (a multi-hour training discarded with no repair, no retry
      and no refund of its `max_nodes` slot). `confirm_ticks=1` restores single-tick behaviour for a
      caller that wants it; 0 or less cannot disable the requirement, because `broken_streak` counts
      the current tick and is therefore always >= 1 at a real call site. What COUNTS toward that
      streak is the caller's business and is spelled out on `_MONITOR_KILL_CONFIRM_TICKS`.
    - the engine's own MEASURED `trajectory` does not contradict the verdict
      (`trajectory_vetoes_kill`). The other four conjuncts all ask who is speaking and how often;
      this one asks whether the question was answerable from what the speaker was shown. On v7 the
      judge saw ten loss values spanning half a minute of a five-hour run and called a run that had
      gone 24.28 -> 22.90 "pinned at ~23.0 ... showing no learning trend from its initialization
      value" at confidence 0.82. `None` (no measurement) never vetoes, so a run that prints no
      parseable loss at all is exactly as killable as it was before.
    """
    if not enabled or verdict is None or verdict.status != "broken":
        return False
    if log_role not in _KILL_ELIGIBLE_ROLES:
        return False
    if trajectory_vetoes_kill(trajectory):
        return False
    try:
        needed = int(confirm_ticks)
    except (TypeError, ValueError, OverflowError):
        return False
    if broken_streak < max(1, needed):
        return False
    confidence, confidence_valid = _normalize_monitor_confidence(verdict.confidence)
    return confidence_valid and confidence >= threshold


# What a repair-stop terminalizes as. In `FAILURE_REASONS`, therefore in the default
# `inline_repair_reasons`, therefore picked up by the inline repair loop with no new plumbing —
# which is the whole design: the engine already knows how to hand a failed node back to its
# Developer, and this only decides WHICH failures deserve that instead of a verdict.
MONITOR_REPAIR_REASON = "not_learning"


def citation_authenticates(verdict, *, resolved: Optional[bool]) -> bool:
    """Did this verdict point at a place in the node's own workdir that the ENGINE could re-open?

    THE OUT-OF-BAND CHANNEL, and it is the repo's own rule applied where it had not been: text may
    NOMINATE, it may never DECIDE. A `broken` verdict is a model reading a log, and on its own it may
    not end a stage. A `broken` verdict whose `evidence_locator` the engine RE-READ and found is a
    claim somebody else can go and check — and the re-read is a filesystem fact the model does not
    author. `failure_diagnosis.evidence_citation_resolves` performs it, confined to the workdir and
    refusing `..`, an absolute path and a symlink out.

    IT DOES NOT CHECK THAT THE VERDICT IS RIGHT, and nothing here pretends otherwise — the same
    honest limit `evidence_citation_resolves` states for the diagnostician. A wrong citation to a
    real file still authenticates. What it buys is that the finding is RE-DERIVABLE, which is
    exactly the property a deterministic measurement has and unsupported prose does not.

    `fault == "implementation"` rides along because that is the only attribution a citation can
    substantiate: "the code is wrong, here is the line". A `hypothesis` verdict is a claim about an
    IDEA and no file can carry it — those are recorded, never repaired.
    """
    if verdict is None or getattr(verdict, "status", "") != "broken":
        return False
    if getattr(verdict, "fault", "unknown") != "implementation":
        return False
    return resolved is True


def should_monitor_repair(verdict: Optional["TrainingVerdict"], *, enabled: bool, threshold: float,
                          log_role: str = LOG_ROLE_UNKNOWN, broken_streak: int = 0,
                          confirm_ticks: int = _MONITOR_KILL_CONFIRM_TICKS,
                          trajectory: Optional["LossTrajectory"] = None,
                          citation_resolved: Optional[bool] = None) -> bool:
    """Whether a verdict warrants stopping this stage FOR REPAIR. Pure/deterministic.

    The sibling of `should_monitor_kill`, and the reason the role gate can finally open. Every
    conjunct there is about ONE cost: a kill discards a multi-hour training with no repair, no retry
    and no refunded `max_nodes` slot, so the monitor must not hold that gun over a stage it cannot
    identify. A repair-stop costs something else entirely — one restart of a run the judge has just
    said is wasted, with the diagnosis attached — so being wrong here is recoverable in the way
    being wrong there is not. That is why this admits EVERY role the judge is allowed to read:

    - a `mine` stage feeding empty negatives, a post-train stage exporting a broken checkpoint and a
      five-stage pipeline's third stage are all things the code can be wrong about, and none of them
      is the training loop. Refusing to act on them was never a judgement that they are healthy; it
      was a judgement that the only available action was too expensive to risk.
    - `_NON_TRAINING_ROLES` still cannot reach here, because they are not judged at all
      (`active_training_log` returns None for the dep install, the scorer and an unattributable
      filename), so there is no verdict about them to act on.

    `fault == "implementation"` is the load-bearing conjunct and it is the model's own attribution:
    the schema tells it that a bug is repaired and a bad idea is recorded, and that `unknown` is the
    safe answer. Everything else is the kill gate's arithmetic unchanged — the same confidence bar,
    the same repeated-verdict requirement, and the same measured-trajectory veto, because a curve
    that is still descending is not evidence of anything being wrong with the code either.
    """
    if not enabled or verdict is None or verdict.status != "broken":
        return False
    if getattr(verdict, "fault", "unknown") != "implementation":
        return False
    if log_role in _NON_TRAINING_ROLES:
        return False
    # THE VETO YIELDS TO A RESOLVED CITATION, AND ONLY HERE — never in `should_monitor_kill`.
    #
    # `trajectory_vetoes_kill` refuses to end a descending, non-anomalous curve. For a loss bounded
    # below that is right and it is why the veto exists. It is WRONG for an objective that is
    # unbounded below, where descent is the symptom: measured on `e5small-dr-unified-v4` node 3,
    # whose DCL mask sentinel — a finite -1e9 in the candidate's own loss module, not this repo's
    # code — reaches the batch mean, the loss ran 40.07 ->
    # -2.4e7 and the veto blocked every one of five `broken` verdicts at or above the bar, one of
    # them at confidence 0.90 with the streak already satisfied. Zero of twenty-four alerts stopped
    # anything.
    #
    # The rung refused permanently in this file (see the DECLINED marker below) is a THRESHOLD on
    # the trajectory, and it is refused for a measured reason: no bar separates the broken n74 (peak
    # 2.54e+08) from champion n48 (2.53e+08). This is not a threshold. It is the engine re-reading a
    # file the judge says it opened — and n48's run contains ZERO `train_monitor_alert` rows, so it
    # cannot enter this path at all, in either direction.
    #
    # AND ONLY THE REPAIR PATH, because the costs are not symmetric and this file says so above: a
    # kill discards a multi-hour training with no repair, no retry and no refunded slot, while a
    # repair-stop costs ONE restart of a run the judge has just said is wasted, with the diagnosis
    # attached. Being wrong here is recoverable in the way being wrong there is not.
    if trajectory_vetoes_kill(trajectory) and not citation_authenticates(
            verdict, resolved=citation_resolved):
        return False
    try:
        needed = int(confirm_ticks)
    except (TypeError, ValueError, OverflowError):
        return False
    if broken_streak < max(1, needed):
        return False
    confidence, confidence_valid = _normalize_monitor_confidence(verdict.confidence)
    return confidence_valid and confidence >= threshold


def _confirmation_would_act(verdict: Optional["TrainingVerdict"], *, enabled: bool, threshold: float,
                            log_role: str = LOG_ROLE_UNKNOWN,
                            trajectory: Optional["LossTrajectory"] = None,
                            confirm_ticks: int = _MONITOR_KILL_CONFIRM_TICKS,
                            citation_resolved: Optional[bool] = None) -> bool:
    """Whether REPEATING this verdict would reach an intervention. Pure/deterministic.

    The arming question, and it is deliberately the same COUNTERFACTUAL shape as the `role_withheld`
    / `trajectory_veto` receipts in `_monitor_training`: ask the real predicates with the one thing
    the tick is missing — the streak — already satisfied, rather than re-listing their conjuncts at
    the arming site where the two copies can silently disagree. They did: see the comment at that
    call site for both directions of the drift the hand-written list carried.

    It grants no authority of its own. Both predicates are unchanged, this only decides WHEN the
    second look happens, and the second look is still judged on its own evidence — so this can never
    turn a verdict the gate refuses into one it accepts.
    """
    return bool(
        should_monitor_kill(verdict, enabled=enabled, threshold=threshold, log_role=log_role,
                            broken_streak=confirm_ticks, confirm_ticks=confirm_ticks,
                            trajectory=trajectory)
        # …AND WITH THE CITATION, for the same reason the streak is passed already satisfied: this
        # is the counterfactual "would a REPEAT of this verdict act", and every input the real gate
        # reads has to be the one it will read. Omitting it re-introduced exactly the drift this
        # function's docstring exists to prevent: a first `broken` tick carrying a RESOLVED
        # citation would act on its repeat, but the counterfactual — computing the veto without the
        # citation — said it would not, so the monitor did not arm and the second look waited a
        # full cadence (up to thirty minutes) instead of `_MONITOR_CONFIRM_DELAY_S`. On a node
        # burning ~4 GPU-hours per attempt that is the whole point of arming, lost silently.
        #
        # `should_monitor_kill` above is deliberately NOT given it and cannot be: it takes no such
        # parameter. The counterfactual therefore inherits the same asymmetry as the real gates,
        # which is what "cannot drift from them" has to mean.
        or should_monitor_repair(verdict, enabled=enabled, threshold=threshold, log_role=log_role,
                                 broken_streak=confirm_ticks, confirm_ticks=confirm_ticks,
                                 trajectory=trajectory, citation_resolved=citation_resolved))


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
