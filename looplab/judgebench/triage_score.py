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
* `head_replay_candidate` — `engine/triage.py::_failure_reason` re-run TODAY on a `res` rebuilt from
  the row. What it can and cannot see is stated per field in `REPLAY_SOURCES`, and rows whose
  `exit_code` is unrecoverable answer `None`, which the report counts as `no-answer` rather than
  folding into a wrong answer — 4 of 122.
* `jsonl_candidate` — a candidate's captured answers, offline, no network.

`--arm head-widened` exists to separate the RULE from the WINDOW, and on this corpus it separates
them sharply: `_is_torch_oom` scores 0 of 23 OOMs over the durable stderr tail and 16 of 23 once the
triage agent's own log reads are in front of it. The 7 it still misses are every row whose capture
was truncated PAST the exception line and kept only the allocator's message body ("Tried to allocate
8.79 GiB. GPU 0 has a total capacity of 139.80 GiB of which 4.59 GiB is free") — a string
`_TORCH_OOM_MARKERS` does not list. That is a finding about the marker list, and the bench is the
only thing that could have produced it.

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

# A copy of `engine/triage.py::AUTHENTICATED_FAILURE_REASONS` — the half of the vocabulary the
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
AUTHENTICATED_LABELS = frozenset({"drift", "timeout", "setup", "diverged", "stalled",
                                  "needs_failed", "expect_failed", "check_failed"})

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


def head_replay_candidate(*, widened: bool = False) -> Callable:
    """`engine/triage.py::_failure_reason` re-run today over the rebuilt `res`."""
    from looplab.engine.triage import _failure_reason

    def candidate(row: dict) -> Optional[str]:
        res = replay_result(row, widened=widened)
        return None if res is None else _failure_reason(res)
    return candidate


def jsonl_candidate(path) -> Callable:
    """A candidate's captured answers: JSONL of `{"case_id": ..., "reason": ...}`. No network."""
    answers = {}
    with Path(path).open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            answers[row.get("case_id")] = row.get("reason") or row.get("failure_kind")
    return lambda row: answers.get(row.get("case_id"))


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

    @property
    def accuracy(self) -> Optional[float]:
        return None if not self.label_coverage else self.correct / self.label_coverage


def score_dataset(rows: list, candidate: Callable, *, name: str = "?",
                  high_confidence_only: bool = False) -> ScoreReport:
    report = ScoreReport(candidate=name, rows=len(rows))
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
