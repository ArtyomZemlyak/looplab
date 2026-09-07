"""Score a failure classifier against `failure_triage.v1` — with the COST of each error, not one
accuracy number.

A single accuracy number is exactly what hides the thing that matters here. The classifier's answer
is not a report; it SELECTS a repair directive (`crash_repair._repair_error_context`), it decides
whether a produced metric may be salvaged (`metric_salvage.NEVER_SALVAGED_REASONS`), and it gates
the triage-driven dependency install (`_prepare_env_from_triage`, `crash` only). Two errors with the
same accuracy cost differ by orders of magnitude:

* saying `crash` when the truth is `oom` costs a repair round pointed at the wrong thing — the
  Developer is told "diagnose the root cause" while the directive that says "return a script that
  fits in LESS memory" sits one branch away. Measured on `runs/e5small-dr-unified-v3`: eight repairs
  across three nodes, two of them returning byte-identical files, no metric, run stopped systemic;
* saying `oom` when the truth is `diverged` costs repair rounds spent in the OPPOSITE direction —
  "cut the memory" against an instability that needs "stabilise the numerics". Measured on
  `runs/rubertlite-dr-unified-v6` node 5: three rounds halving 8192 → 2048 → 512 → 256 at ~3
  GPU-minutes each while the divergence went untouched;
* saying a `NEVER_SALVAGED` reason when the truth is not one SUPPRESSES a metric the eval really
  produced — the node's whole compute;
* saying a salvageable reason when the truth is `drift` or `setup` would ADMIT a metric the trust
  gate refused — the one direction that can move a champion. `timeout` and `diverged` are also in
  `NEVER_SALVAGED_REASONS` and are deliberately NOT scored that way: `metric_salvage` re-reads
  `res.timed_out` and `res.diverged` directly, one line under the reason test, precisely so the
  refusal is a property of the RESULT and not of whichever label the classifier reached for. A cost
  model that charged those would over-claim, and this one used to.

`ERROR_COSTS` names each of those and the report prints the matrix WITH the cost class in every
off-diagonal cell. There is deliberately no weighted total: a weighted total is a knob, and a knob
is how a bench is made to say what its author wanted.

## Three arms, never averaged

* `recorded_candidate` — the reason the engine actually recorded at the time. Zero reconstruction:
  this is the historical incumbent for the code each run was on.
* `frozen_replay_candidate` — the classifier AS IT STOOD when this corpus was cut, replayed on a
  `res` rebuilt from the row. **It reads no production name at all** — `_frozen_failure_reason_v1`
  below is a verbatim snapshot, and so is `HISTORICAL_AUTHENTICATED_REASONS`. That is deliberate and
  it is the correction of a real defect: until 2026-08-20 this arm imported the live
  `_failure_reason`, so the day the classifier changed, the arm silently began measuring a different
  program while still being labelled "the incumbent". A bench may lose many things; the record of
  how the OLD decider scored is not one of them.
* `live_engine_candidate` — the classifier at HEAD. This one MUST read production, and it is the arm
  that makes the bench useful going forward. It scores only the DETERMINISTIC half of today's
  classifier, because that is the half a bench can run offline: `_failure_reason` now answers
  structurally from what the engine caused, ran or measured, and hands `crash` / `no_metric` /
  `check_failed` to a diagnostician that costs a model call. The report prints the handoff count
  beside the score — those rows are the headroom the diagnostician has to win, and `--answers` is
  how a real one gets scored on them.
* `jsonl_candidate` — a candidate's (or a diagnostician's) captured answers, offline, no network.

`--arm frozen-widened` exists to separate the RULE from the WINDOW, and on this corpus it separated
them sharply: the frozen `_is_torch_oom` scores 0 of 23 OOMs over the durable stderr tail and 16 of
23 once the triage agent's own log reads are in front of it. The 7 it still missed were every row
whose capture was truncated PAST the exception line and kept only the allocator's message body
("Tried to allocate 8.79 GiB. GPU 0 has a total capacity of 139.80 GiB of which 4.59 GiB is free").
That finding is why the marker list is gone from production entirely: `oom` is now answer-only, a
kind no deterministic rule produces, because both of its producers were text rules. The frozen arm
keeps the measurement that argued for it.

**Agreement between two arms is churn, not accuracy** — the same rule `judge_corpus.py` follows, and
`ScoreReport` keeps the two in separate fields with no code path that averages them.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Optional

from looplab.judgebench.triage_corpus import (
    CORPUS_LIMITS, DIVERGE_SENTINEL, HIGH_CONFIDENCE_BASES, LABEL_UNKNOWN, STALL_SENTINEL,
    TORCH_OOM_MARKERS)

# The reasons whose repair directive is its OWN text in `crash_repair._repair_error_context`.
# Everything else falls through to the generic "diagnose the root cause" tail, which is why a wrong
# answer inside the generic group is cheap and a wrong answer that LEAVES it is not.
SPECIFIC_DIRECTIVES = frozenset({"timeout", "oom", "diverged", "stalled", "not_learning",
                                 "needs_failed"})
GENERIC_DIRECTIVE = frozenset({"crash", "no_metric", "check_failed", "expect_failed", "drift",
                               "setup"})

# Directives that tell the Developer to do OPPOSITE things. Mistaking one for another does not merely
# waste a round — it spends the round moving the wrong dial, and the corpus measured what that costs.
OPPOSED_DIRECTIVES = (
    frozenset({"oom", "diverged"}), frozenset({"oom", "stalled"}),
    frozenset({"oom", "not_learning"}), frozenset({"oom", "timeout"}),
    frozenset({"diverged", "not_learning"}),
)

# A copy of `engine/metric_salvage.py::NEVER_SALVAGED_REASONS`, deliberately (see
# `triage_corpus.TORCH_OOM_MARKERS` for why a bench copies rather than imports what it measures).
# `tests/test_triage_bench.py` asserts the two still agree.
NEVER_SALVAGED_REASONS = frozenset({"drift", "setup", "timeout", "diverged"})

# The two members `metric_salvage` ALSO guards with the authenticated flag itself (`res.timed_out`,
# `res.diverged`), immediately after the reason test. Getting the label wrong on one of these does
# not open the salvage gate, so charging it as if it did would be this bench inventing a cost.
FLAG_GUARDED_REASONS = frozenset({"timeout", "diverged"})

# The reason that gates the triage-driven dependency install (`_prepare_env_from_triage`).
ENV_PREP_REASON = "crash"

# A frozen copy of the tuple `engine/triage.py` used to call AUTHENTICATED_FAILURE_REASONS (deleted
# 2026-08-20 with the ownership split, so this is deliberately NOT a live citation) — the half of
# the vocabulary the
# ENGINE observed out of band and no judge may contradict. Reported as its own accuracy because the
# two halves answer different questions and mixing them flatters both:
#
#   * On an AUTHENTICATED truth the classifier and this bench's label often read the SAME engine
#     fact — `check_failed` comes from `res.stages[-1]["status"]` on both sides — so HEAD being
#     right there is the BRANCH EXISTING, not a reading being better. Real (it is how the ten
#     `no_metric`-for-`check_failed` rows got fixed) but it is not evidence about diagnosis.
#   * The other half — `crash` / `oom` / `no_metric` (`triage.JUDGED_FAILURE_REASONS`) plus the
#     `not_learning` only a live judge can name — is inferred from the dead process's TEXT. That is
#     the whole of what an agentic diagnostician replaces, and it is the number to judge one on.
#
# FROZEN 2026-08-20, and it may not be re-pointed at production. It was a copy of a tuple that name
# no longer exists in `engine/triage.py`, and the ownership split that
# replaced it draws the line in a DIFFERENT place (`check_failed` moved to the diagnosable side,
# because its "channel" is itself a model reading the candidate's stdout). Both facts are reasons to
# freeze rather than follow: this partition is a property of the LABELS, which were cut once and do
# not move, and keeping it fixed is the only way two arms measured months apart remain comparable.
# The LIVE partition is `LIVE_ENGINE_FINAL_REASONS` / `LIVE_DIAGNOSABLE_REASONS` below, and it is
# reported separately rather than substituted here.
HISTORICAL_AUTHENTICATED_REASONS = frozenset({
    "drift", "timeout", "setup", "diverged", "stalled",
    "needs_failed", "expect_failed", "check_failed"})
AUTHENTICATED_LABELS = HISTORICAL_AUTHENTICATED_REASONS   # the name the report and tests use

# The LIVE partition, READ FROM PRODUCTION AT IMPORT and never copied — this is the half that must
# follow what ships, because scoring what ships is the whole point of the live arm.
#
# IT DETECTS THE SHAPE RATHER THAN HARD-IMPORTING ONE, and that is the lesson of this bench applied
# to the bench itself. Two partitions have shipped inside a week: `AUTHENTICATED_FAILURE_REASONS` /
# `JUDGED_FAILURE_REASONS` in `engine/triage.py`, and the ownership split
# (`ENGINE_FINAL_REASONS` / `DIAGNOSABLE_ENGINE_REASONS` in `engine/failure_diagnosis.py`) that
# replaces them. A live arm that imported either one by name would be an `ImportError` on the other
# side of that change — which is exactly how this module went red, and exactly what a measurement
# leg must not do to a merge. So it reads whichever is present and NAMES it, the name is printed in
# the report above the score, and `tests/test_triage_bench.py` asserts the detected shape agrees
# with production rather than asserting one shape exists.
LIVE_SHAPE_OWNERSHIP = "ownership_split"          # engine-final vs diagnosable (2026-08-20 onward)
LIVE_SHAPE_AUTHENTICATED = "authenticated_judged"  # authenticated vs judged (before it)


def live_ownership_split() -> dict:
    """What production's classifier vocabulary is RIGHT NOW, and which of the two shapes it is."""
    try:
        from looplab.engine import failure_diagnosis as _fd
    except ImportError:
        from looplab.engine import triage as _tri
        return {
            "shape": LIVE_SHAPE_AUTHENTICATED,
            "engine_final": frozenset(_tri.AUTHENTICATED_FAILURE_REASONS),
            "diagnosable": frozenset(_tri.JUDGED_FAILURE_REASONS),
            "answerable": frozenset(_tri.JUDGED_FAILURE_REASONS),
            "unclassified": None,
        }
    return {
        "shape": LIVE_SHAPE_OWNERSHIP,
        "engine_final": frozenset(_fd.ENGINE_FINAL_REASONS),
        "diagnosable": frozenset(_fd.DIAGNOSABLE_ENGINE_REASONS),
        "answerable": frozenset(_fd.DIAGNOSED_FAILURE_REASONS),
        # kind -> the engine answers it may be named under (2026-09-06; `{}` on an older tree).
        # Detected with `getattr` for the same reason the shape is: a bench must not go red on
        # the other side of a production change it exists to measure.
        "context_bound": {k: frozenset(v) for k, v in
                          dict(getattr(_fd, "DIAGNOSED_CONTEXT_BOUND", {}) or {}).items()},
        "unclassified": _fd.UNCLASSIFIED_REASON,
    }


_LIVE = live_ownership_split()
LIVE_SHAPE = _LIVE["shape"]
LIVE_ENGINE_FINAL_REASONS = _LIVE["engine_final"]
LIVE_DIAGNOSABLE_REASONS = _LIVE["diagnosable"]
LIVE_ANSWERABLE_REASONS = _LIVE["answerable"]
LIVE_CONTEXT_BOUND = _LIVE.get("context_bound") or {}
LIVE_UNCLASSIFIED_REASON = _LIVE["unclassified"]


def answerable_for(engine_reason) -> frozenset:
    """What the diagnostician may answer when the engine's own answer is `engine_reason`.

    `LIVE_ANSWERABLE_REASONS` minus every context-bound kind whose context this is not. Until
    2026-09-06 the answer vocabulary was one set for every handoff and the four `diverged`-truth
    rows the stage checker caught (`rubertlite-dense-retrieval` 15/60/68/74 — the watchdog did
    not exist yet) were unwinnable by construction; `diverged` is now admissible on a tagged
    `check_failed` and on nothing else, so "unwinnable" has to be asked per handoff."""
    out = set(LIVE_ANSWERABLE_REASONS)
    for kind, contexts in LIVE_CONTEXT_BOUND.items():
        if str(engine_reason) not in contexts:
            out.discard(kind)
    return frozenset(out)

ERROR_COSTS = {
    "admits_refused_metric": (
        "the truth is a reason the salvage gate REFUSES and the answer is not — a metric the trust "
        "gate would have rejected can be admitted, which is the only error here that can move a "
        "champion. Nothing in this corpus does it; it is scored because it is the direction that "
        "cannot be undone."),
    "suppresses_real_metric": (
        "the answer is a NEVER_SALVAGED reason and the truth is not — a metric the eval really "
        "produced is refused, costing the node's whole compute."),
    "opposed_directive": (
        "the repair directive points the OPPOSITE way (cut memory vs stabilise numerics vs unblock "
        "a hang). Measured on rubertlite-dr-unified-v6 node 5: three rounds halving 8192 -> 256 at "
        "~3 GPU-min each against an instability the batch size had nothing to do with."),
    "generic_for_specific": (
        "the truth has a directive of its own and the answer falls back to 'diagnose the root "
        "cause'. Measured on e5small-dr-unified-v3: 8 repairs over 3 nodes, 2 returning "
        "byte-identical files, 0 metrics, run stopped systemic."),
    "diverged_without_the_flag": (
        "the answer is `diverged` and the divergence watchdog did not say so — the ONE diagnosed "
        "kind that is also NEVER_SALVAGED (admitted 2026-09-06, only over a tagged `check_failed`). "
        "No metric moves: salvage is decided on the engine's own answer branches earlier, and "
        "`salvage_condition` re-reads `res.diverged`. What it costs is the numerics directive "
        "against a run whose loss was finite, plus a durable row naming a never-salvaged reason for "
        "a node the gate would have admitted — the audit trail says 'unsalvageable' about a result "
        "that was not."),
    "specific_for_generic": (
        "a specific directive is issued for a failure that has no such shape — one repair round "
        "pointed at memory (or at the numerics) instead of at the real bug."),
    "misses_dependency_install": (
        "the truth is `crash` and the answer is not, so `_prepare_env_from_triage` never runs and a "
        "missing library is never installed before the Developer is asked to work around it."),
    "wrong_within_group": (
        "both answers reach the same directive and the same gates; the cost is the AUDIT TRAIL "
        "only — the record names the wrong cause for every later reader and for the F8 critic that "
        "compares causes across attempts."),
}


def cost_of(answer: str, truth: str) -> str:
    """Which cost class an (answer, truth) pair falls in. Ordered worst-first."""
    if answer == truth:
        return "correct"
    if (truth in NEVER_SALVAGED_REASONS - FLAG_GUARDED_REASONS
            and answer not in NEVER_SALVAGED_REASONS):
        return "admits_refused_metric"
    if (answer in NEVER_SALVAGED_REASONS - FLAG_GUARDED_REASONS
            and truth not in NEVER_SALVAGED_REASONS):
        return "suppresses_real_metric"
    # Above the opposed pair it would otherwise fall into (`oom`/`diverged`, `diverged`/
    # `not_learning`): a model-named `diverged` is a different error from an engine-named one and
    # is charged as itself, whatever the truth was.
    if answer == "diverged" and truth != "diverged":
        return "diverged_without_the_flag"
    if any({answer, truth} <= pair for pair in OPPOSED_DIRECTIVES):
        return "opposed_directive"
    if truth in SPECIFIC_DIRECTIVES and answer in GENERIC_DIRECTIVE:
        return "generic_for_specific"
    if answer in SPECIFIC_DIRECTIVES and truth in GENERIC_DIRECTIVE:
        return "specific_for_generic"
    if truth == ENV_PREP_REASON and answer != ENV_PREP_REASON:
        return "misses_dependency_install"
    return "wrong_within_group"


# --- the arms ----------------------------------------------------------------------------------

def recorded_candidate(row: dict) -> Optional[str]:
    """The reason the engine actually recorded. The incumbent, not the truth."""
    return (row.get("recorded") or {}).get("reason")


# What a HEAD replay can rebuild and from what. Stated per field because the honesty of the second
# arm rests entirely on this: a field rebuilt FROM THE RECORDED REASON would make the replay agree
# with the incumbent by construction, so no field here is.
REPLAY_SOURCES = {
    "drift": "always None — no row in this corpus was refused by the cross-reader",
    "timed_out": "stage_finished.status == 'timeout' (the engine's own clock)",
    "diverged": "the engine-authored '%s' sentinel in the recorded stderr" % DIVERGE_SENTINEL,
    "stalled": "the engine-authored '%s' sentinel in the recorded stderr" % STALL_SENTINEL,
    "exit_code": "stage_finished.exit_code; where the older event schema recorded none, 0 when the "
                 "stage row carries a contract status (all three exit 0) and 1 when the tail holds "
                 "a traceback — never from the recorded reason",
    "stderr": "the durable ~500-char tail, or (--stderr widened) that tail plus the triage agent's "
              "own log reads and the paired stage log",
    "stages": "the attempt's stage_finished rows verbatim",
}

_CONTRACT_STATUSES = ("needs_failed", "expect_failed", "check_failed")


def replay_result(row: dict, *, widened: bool = False):
    """Rebuild the `res` `_failure_reason` reads, or `None` when a field cannot be recovered."""
    at = (row.get("evidence") or {}).get("at_classification") or {}
    on = (row.get("evidence") or {}).get("on_demand") or {}
    stages = list(at.get("stages") or [])
    status = at.get("failed_stage_status")
    stderr = str(at.get("stderr_tail") or "")
    if widened:
        extra = list(on.get("triage_log_reads") or [])
        tail = ((on.get("stage_log") or {}).get("tail") or "")
        stderr = "\n".join([stderr] + extra + ([tail] if tail else []))
    exit_code = at.get("exit_code")
    if exit_code is None:
        if status in _CONTRACT_STATUSES:
            exit_code = 0
        elif "Traceback" in stderr or (row.get("label") or {}).get("terminal_exception"):
            exit_code = 1
        else:
            return None
    # `_failure_reason` reads the LAST stage row's status for the three contract branches. Older
    # runs recorded no stage rows at all; a contract failure there is still visible because the
    # engine wrote its own prefix into the stderr, and that is where the status came from.
    if not stages and status:
        stages = [{"name": at.get("failed_stage"), "status": status, "exit_code": exit_code}]
    return SimpleNamespace(
        drift=None,
        timed_out=(status == "timeout"),
        stderr=stderr,
        diverged=DIVERGE_SENTINEL in str(at.get("stderr_tail") or ""),
        stalled=STALL_SENTINEL in str(at.get("stderr_tail") or ""),
        exit_code=exit_code,
        stages=stages,
    )


# ---------------------------------------------------------------------------------------------
# THE FROZEN INCUMBENT. A verbatim snapshot of `engine/triage.py::_failure_reason` as it stood on
# 2026-08-20 when this corpus was cut, kept HERE so the historical arm reads nothing that can move.
#
# DO NOT UPDATE THIS FUNCTION. If today's classifier is what you want to measure, that is
# `live_engine_candidate`. This one exists to keep the numbers 74.6 % / 88.1 % meaning what they
# meant on the day they were measured — the classifier they describe was deleted hours later, and an
# arm that had gone on importing the live name would have silently started reporting a different
# program's score under the label "the incumbent". That is the one failure a bench may not have.
#
# Its two text rules are the whole reason it is gone: `_FROZEN_TORCH_OOM_MARKERS` and the
# `-9/137 with no Traceback` kernel signature. `oom` is now answer-only in production precisely
# because both of its producers read the failure's own text.
_FROZEN_TORCH_OOM_MARKERS: tuple[str, ...] = (
    "OutOfMemoryError", "CUDA out of memory", "HIP out of memory", "XPU out of memory")


def _frozen_failure_reason_v1(res) -> str:
    """The 2026-08-20-morning classifier, verbatim. See the block above before touching it."""
    if getattr(res, "drift", None) is not None:
        return "drift"
    if res.timed_out:
        return "timeout"
    if (res.stderr or "").startswith("setup failed:"):
        return "setup"
    if getattr(res, "diverged", False):
        return "diverged"
    if getattr(res, "stalled", False):
        return "stalled"
    if res.exit_code != 0:
        if res.exit_code in (-9, 137) and "Traceback" not in (res.stderr or ""):
            return "oom"
        if any(marker in (res.stderr or "") for marker in _FROZEN_TORCH_OOM_MARKERS):
            return "oom"
        return "crash"
    _rows = [row for row in (getattr(res, "stages", None) or []) if isinstance(row, dict)]
    _last = str(_rows[-1].get("status") or "") if _rows else ""
    if _last == "needs_failed":
        return "needs_failed"
    if _last == "expect_failed":
        return "expect_failed"
    if _last == "check_failed":
        return "check_failed"
    return "no_metric"


def frozen_replay_candidate(*, widened: bool = False) -> Callable:
    """The incumbent as it stood when the corpus was cut. Reads no production name."""
    def candidate(row: dict) -> Optional[str]:
        res = replay_result(row, widened=widened)
        return None if res is None else _frozen_failure_reason_v1(res)
    return candidate


def live_engine_candidate() -> Callable:
    """`engine/triage.py::_failure_reason` AT HEAD — the deterministic half of today's classifier.

    This is the arm that must follow production, and the import is deliberately not frozen. What it
    scores is only half of what ships: since 2026-08-20 the classifier answers structurally from what
    the engine caused, ran or measured, and hands `crash` / `no_metric` / `check_failed` on to a
    diagnostician that costs a model call and cannot be run inside a test. So this number is not
    "the new classifier's accuracy" and the report never labels it as one — it is the accuracy of
    everything decided WITHOUT a model, and `diagnosable_handoff` beside it counts the rows where the
    answer is a nomination rather than a decision. Those rows are the diagnostician's to win, and
    `--answers` is how a real one is scored on them.
    """
    from looplab.engine.triage import _failure_reason

    def candidate(row: dict) -> Optional[str]:
        res = replay_result(row)
        return None if res is None else _failure_reason(res)
    return candidate


def head_replay_candidate(*, widened: bool = False) -> Callable:
    """Deprecated alias kept so an older invocation does not silently change meaning.

    It used to import the live `_failure_reason`; that is now `live_engine_candidate`, and what this
    name MEANT to its callers — the incumbent replayed — is `frozen_replay_candidate`. It resolves to
    the frozen one, because a caller that recorded a number from it recorded the incumbent's."""
    return frozen_replay_candidate(widened=widened)


def jsonl_candidate(path) -> Callable:
    """A candidate's captured answers: JSONL of `{"case_id": ..., "reason": ...}`. No network.

    The callable carries `.records` — the raw rows by `case_id` — so `score_dataset` can replay
    the shipped override rule over a capture that predates it (`override_rule_replay`). A capture
    that carries only `reason` replays as nothing changed, which is the honest reading of a file
    that recorded no evidence."""
    answers, records = {}, {}
    with Path(path).open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            answers[row.get("case_id")] = row.get("reason") or row.get("failure_kind")
            records[row.get("case_id")] = row

    def candidate(row: dict) -> Optional[str]:
        return answers.get(row.get("case_id"))
    candidate.records = records
    return candidate


def override_rule_answer(record: dict) -> tuple:
    """`(reason after the shipped override rule, the source it lacked or "")` for one captured
    diagnostician row.

    The capture (`tools/triage_diagnostician_replay.py`) records `engine_reason`, the answered
    `reason` and the primary `evidence` (`{source, locator, quote}`) — and, on a newer harness,
    `findings` — so the verdict the rule reads can be REBUILT and handed to the production rule
    itself (`failure_diagnosis.reason_override_refused`), never to a copy of it: a bench that
    re-spelled the rule would measure the re-spelling. A record with no `engine_reason` (a bare
    `{case_id, reason}` file) is returned unchanged."""
    reason = record.get("reason") or record.get("failure_kind")
    engine = record.get("engine_reason")
    if not engine or not reason:
        return reason, ""
    try:
        from looplab.engine.failure_diagnosis import reason_override_refused
    except ImportError:                      # an older tree: no rule to replay
        return reason, ""
    ev = record.get("evidence") or {}
    verdict = {"action": record.get("action") or "repair", "failure_kind": reason,
               "evidence_source": ev.get("source", ""), "evidence_locator": ev.get("locator", ""),
               "evidence_quote": ev.get("quote", ""), "findings": record.get("findings") or []}
    lacked = reason_override_refused(str(engine), verdict)
    return (str(engine) if lacked else reason), lacked


def override_rule_replay(rows: list, records: dict) -> dict:
    """How many labelled rows the shipped override rule CHANGES on a capture, and which way.

    `{"changed", "wrong_to_right", "right_to_wrong", "wrong_to_wrong", "correct_before",
    "correct_after", "labelled", "lacked": {source: n}}` — the four directions are reported
    separately and never netted, because "+6" hides that one of the rows it moved was the corpus's
    only genuine `not_learning` (`rubertlite-dense-retrieval` node 12, which cited nothing)."""
    out = {"changed": 0, "wrong_to_right": 0, "right_to_wrong": 0, "wrong_to_wrong": 0,
           "correct_before": 0, "correct_after": 0, "labelled": 0, "lacked": {}}
    for row in rows:
        truth = (row.get("label") or {}).get("reason")
        if truth in (None, LABEL_UNKNOWN):
            continue
        record = records.get(row.get("case_id"))
        if not record:
            continue
        before = record.get("reason") or record.get("failure_kind")
        after, lacked = override_rule_answer(record)
        out["labelled"] += 1
        out["correct_before"] += int(before == truth)
        out["correct_after"] += int(after == truth)
        if after == before:
            continue
        out["changed"] += 1
        out["lacked"][lacked] = out["lacked"].get(lacked, 0) + 1
        if before != truth and after == truth:
            out["wrong_to_right"] += 1
        elif before == truth and after != truth:
            out["right_to_wrong"] += 1
        else:
            out["wrong_to_wrong"] += 1
    return out


# --- the report --------------------------------------------------------------------------------

@dataclass
class ScoreReport:
    candidate: str = "?"
    rows: int = 0
    answered: int = 0
    unanswered: int = 0                # a candidate that could not answer, kept apart from a wrong one
    label_coverage: int = 0            # rows with a real label — the accuracy denominator
    high_confidence: int = 0
    unlabelled: int = 0
    correct: int = 0
    confusion: dict = field(default_factory=dict)      # (truth, answer) -> n
    costs: dict = field(default_factory=dict)          # cost class -> n
    per_basis: dict = field(default_factory=dict)      # label basis -> [correct, total]
    agreement_with_recorded: int = 0
    agreement_denominator: int = 0
    # Not a cost MODEL — what the corpus records happened to the node this answer was the last word
    # on. A wrong reason on a terminal row is a wrong reason nothing downstream could correct.
    errors_on_terminal_rows: int = 0
    terminal_rows: int = 0
    errors_on_rescued_nodes: int = 0
    # The same accuracy split by WHO COULD HAVE KNOWN: an engine fact the classifier reads
    # structurally, versus an inference from the dead process's text. See `AUTHENTICATED_LABELS`.
    authenticated: list = field(default_factory=lambda: [0, 0])   # [correct, total]
    text_read: list = field(default_factory=lambda: [0, 0])
    # LIVE-ARM ONLY, and it is not a score. Today's classifier answers `crash` / `no_metric` /
    # `check_failed` as a NOMINATION handed to a diagnostician, not as a decision. This counts those
    # rows and how many the nomination happens to get right, so "the deterministic half is 74.6 %"
    # can never be read as "the new classifier is 74.6 %".
    diagnosable_handoff: list = field(default_factory=lambda: [0, 0])   # [right by luck, handed on]
    # Of the rows handed on, how many have a TRUTH the diagnostician is not permitted to give. Its
    # answer vocabulary is closed and deliberately narrower than the label vocabulary — a model may
    # not assert that an engine mechanism it cannot observe fired — so these rows are unwinnable by
    # construction and a ceiling computed without them is a promise the design cannot keep.
    unreachable_by_diagnosis: int = 0
    scores_live_classifier: bool = False    # set only by the live arm; gates the handoff block
    # A captured-answers arm only: what the shipped override rule (`failure_diagnosis.
    # OVERRIDE_EVIDENCE_REQUIRED`) would change on this capture. None when the candidate carried
    # no records to replay over.
    override_rule: Optional[dict] = None

    @property
    def accuracy(self) -> Optional[float]:
        return None if not self.label_coverage else self.correct / self.label_coverage


def score_dataset(rows: list, candidate: Callable, *, name: str = "?",
                  high_confidence_only: bool = False, live: bool = False) -> ScoreReport:
    report = ScoreReport(candidate=name, rows=len(rows), scores_live_classifier=live)
    for row in rows:
        label = row.get("label") or {}
        truth, basis = label.get("reason"), label.get("basis")
        answer = candidate(row)
        recorded = recorded_candidate(row)
        if answer is None:
            report.unanswered += 1
        else:
            report.answered += 1
        if recorded is not None and answer is not None:
            report.agreement_denominator += 1
            report.agreement_with_recorded += int(answer == recorded)
        if truth in (None, LABEL_UNKNOWN):
            report.unlabelled += 1
            continue
        if high_confidence_only and basis not in HIGH_CONFIDENCE_BASES:
            report.unlabelled += 1
            continue
        report.label_coverage += 1
        report.high_confidence += int(basis in HIGH_CONFIDENCE_BASES)
        key = (truth, answer if answer is not None else "<no answer>")
        report.confusion[key] = report.confusion.get(key, 0) + 1
        bucket = report.per_basis.setdefault(basis, [0, 0])
        bucket[1] += 1
        half = report.authenticated if truth in AUTHENTICATED_LABELS else report.text_read
        half[1] += 1
        half[0] += int(answer == truth)
        if answer in LIVE_DIAGNOSABLE_REASONS:
            report.diagnosable_handoff[1] += 1
            report.diagnosable_handoff[0] += int(answer == truth)
            if answer != truth and truth not in answerable_for(answer):
                report.unreachable_by_diagnosis += 1
        if row.get("provenance", {}).get("terminal"):
            report.terminal_rows += 1
            report.errors_on_terminal_rows += int(answer != truth)
        if basis == "reused_stage_later_scored":
            report.errors_on_rescued_nodes += int(answer != truth)
        if answer == truth:
            report.correct += 1
            bucket[0] += 1
            report.costs["correct"] = report.costs.get("correct", 0) + 1
        elif answer is None:
            report.costs["<no answer>"] = report.costs.get("<no answer>", 0) + 1
        else:
            cost = cost_of(answer, truth)
            report.costs[cost] = report.costs.get(cost, 0) + 1
    records = getattr(candidate, "records", None)
    if isinstance(records, dict) and records:
        report.override_rule = override_rule_replay(rows, records)
    return report


def format_report(report: ScoreReport, *, limits: str = CORPUS_LIMITS) -> str:
    out = ["", "=" * 88, "candidate: %s" % report.candidate, "=" * 88,
           "rows %d   answered %d   no-answer %d   labelled %d (high-confidence %d)   unlabelled %d"
           % (report.rows, report.answered, report.unanswered, report.label_coverage,
              report.high_confidence, report.unlabelled)]
    if report.accuracy is not None:
        out.append("ACCURACY vs label   %d/%d = %.1f%%"
                   % (report.correct, report.label_coverage, 100 * report.accuracy))
    if report.agreement_denominator:
        out.append("agreement with the RECORDED reason (churn, NOT accuracy)   %d/%d = %.1f%%"
                   % (report.agreement_with_recorded, report.agreement_denominator,
                      100 * report.agreement_with_recorded / report.agreement_denominator))
    out += ["", "ACCURACY BY WHO COULD HAVE KNOWN", "-" * 88]
    for name, (right, total) in (
            ("AUTHENTICATED — the engine observed it out of band", report.authenticated),
            ("READ FROM THE TEXT — what a diagnostician replaces", report.text_read)):
        if total:
            out.append("  %-52s %d/%d = %.1f%%" % (name, right, total, 100 * right / total))
    if report.scores_live_classifier and report.diagnosable_handoff[1]:
        right, total = report.diagnosable_handoff
        out += ["",
                "HANDED TO THE DIAGNOSTICIAN  (a nomination, not a decision)", "-" * 88,
                "  production's partition today is %r" % LIVE_SHAPE,
                "  %d of %d labelled rows got an answer in %s"
                % (total, report.label_coverage, sorted(LIVE_DIAGNOSABLE_REASONS)),
                "  %d of those %d happen to be right already; the other %d are the headroom a "
                "diagnostician" % (right, total, total - right),
                "  has to win, and are what `--answers` scores. This is NOT part of any accuracy "
                "claim above."]
        reachable = report.correct + (total - right) - report.unreachable_by_diagnosis
        out += ["  of that headroom, %d have a TRUTH the diagnostician may not give (outside its "
                "closed" % report.unreachable_by_diagnosis,
                "  answer vocabulary), so they are unwinnable by construction",
                "  REACHABLE CEILING if it wins every row it is allowed to: %d/%d = %.1f%%"
                % (reachable, report.label_coverage,
                   100 * reachable / max(1, report.label_coverage))]
    if report.override_rule is not None:
        o = report.override_rule
        out += ["",
                "OVERRIDE RULE REPLAYED  (a `not_learning` over a tagged `check_failed` must cite a "
                "'log' source; else the check's verdict stands)", "-" * 88,
                "  rows the rule CHANGES on this capture : %d of %d labelled" % (o["changed"],
                                                                                o["labelled"]),
                "    wrong -> right %d   right -> wrong %d   wrong -> wrong %d"
                % (o["wrong_to_right"], o["right_to_wrong"], o["wrong_to_wrong"]),
                "  correct before %d/%d -> after %d/%d   (the score above is the CAPTURE; a live "
                "re-run applies the rule inside `diagnosed_failure_reason`)"
                % (o["correct_before"], o["labelled"], o["correct_after"], o["labelled"])]
        if o["lacked"]:
            out.append("  evidence source lacked: %s" % dict(sorted(o["lacked"].items())))
    out += ["", "CONFUSION  (truth -> answer)", "-" * 88]
    truths = sorted({t for t, _a in report.confusion})
    for truth in truths:
        cells = sorted(((a, n) for (t, a), n in report.confusion.items() if t == truth),
                       key=lambda kv: -kv[1])
        total = sum(n for _a, n in cells)
        out.append("  %-16s n=%-4d" % (truth, total))
        for answer, count in cells:
            mark = "OK " if answer == truth else "-> "
            cost = "" if answer == truth else "   [%s]" % (
                "<no answer>" if answer == "<no answer>" else cost_of(answer, truth))
            out.append("      %s%-16s %d%s" % (mark, answer, count, cost))
    out += ["", "COST OF THE ERRORS", "-" * 88]
    for cost, count in sorted(report.costs.items(), key=lambda kv: -kv[1]):
        if cost == "correct":
            continue
        out.append("  %-26s %d" % (cost, count))
        if cost in ERROR_COSTS:
            for line in _wrap(ERROR_COSTS[cost], 84):
                out.append("      " + line)
    out += ["", "OBSERVED CONSEQUENCES ON THIS CORPUS  (recorded facts, not a cost model)", "-" * 88,
            "  wrong on a TERMINAL failure (nothing downstream could correct it)   %d/%d"
            % (report.errors_on_terminal_rows, report.terminal_rows),
            "  wrong on a node whose own artefact later scored a healthy metric    %d"
            % report.errors_on_rescued_nodes]
    out += ["", "ACCURACY BY LABEL BASIS  (what the label rests on)", "-" * 88]
    for basis, (right, total) in sorted(report.per_basis.items(), key=lambda kv: -kv[1][1]):
        flag = "" if basis in HIGH_CONFIDENCE_BASES else "   (medium confidence)"
        out.append("  %-30s %d/%d%s" % (basis, right, total, flag))
    out += ["", "LIMITS", "-" * 88] + ["  " + line for line in _wrap(limits, 84)] + [""]
    return "\n".join(out) + "\n"


def _wrap(text: str, width: int) -> list:
    words, lines, line = text.split(), [], ""
    for word in words:
        if len(line) + len(word) + 1 > width:
            lines.append(line)
            line = word
        else:
            line = (line + " " + word).strip()
    if line:
        lines.append(line)
    return lines
