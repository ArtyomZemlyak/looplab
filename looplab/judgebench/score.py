"""Score a candidate judge against the recorded corpus — TWO numbers, never one.

A candidate is a different model, a changed prompt, a changed threshold, or the incumbent itself
(`recorded`, which is what makes the corpus self-describing). Scoring it answers two questions that
people constantly merge, and merging them is how a bench starts lying:

* **Agreement with the LABEL** — did the candidate get it RIGHT? Only meaningful where the run
  itself later supplied an outcome the judge did not author. This is accuracy.
* **Agreement with the RECORDED VERDICT** — did the candidate answer the way the incumbent did?
  This is NOT accuracy. It is a churn measure: it says how much behaviour a change moves, which is
  what you want when you are asking "is this a safe swap", and it is maximised by a candidate that
  reproduces every one of the incumbent's mistakes.

`ScoreReport` keeps them in separate fields and there is deliberately **no combined score** and no
code path that averages them. For a judge with no recoverable outcome (the novelty gate: a rejected
idea is never run, so nothing on disk says whether it would have worked) only the second number can
ever exist, and `label_coverage` reporting 0 is how that shows up rather than a silent zero.

## Which labels enter the matrix

`PRIMARY_LABELS` is `wasted` / `productive`. `budget_exhausted` (a `timeout`) and `unknown` are
excluded and counted separately — see `judge_corpus.py` for why charging a `timeout` as a missed
`broken` would penalise the judge for obeying its own system prompt.

## Offline by default

The default candidate reads a JSONL of `{"case_id": ..., "status": ...}` — answers captured
earlier, replayed with no network. `llm_candidate` builds the live arm out of the repo's own
`parse_structured`; it is not the default, it is not reachable without being constructed
explicitly, and calling it spends money per row.

## THE VERDICT IS NOT THE STOP (`Gate`, opt-in)

By default a row's "stop" is `status == "broken"`, and that is the right unit for comparing two
PROMPTS. It is the wrong unit for the two numbers an operator pays in, because the engine does not
act on a verdict — it acts on `should_monitor_kill` / `should_monitor_repair`, a conjunction the
model only supplies two terms of. Measured on this corpus: **all five of the false stops the
headline reports sit at confidence 0.62-0.75, below the shipped `train_monitor_kill_confidence`
of 0.8, while 49 of the 53 true stops clear it** — so the engine's own confidence conjunct takes the
false-stop count from 5 to 0 at a cost of 4 true stops (7 -> 6 wasted attempts caught). A prompt
change benched against a false-stop number the engine never pays is being read against the wrong
target, in the expensive direction.

`Gate` models the conjuncts a recorded row can still answer, and REFUSES to model the one it
cannot:

* **confidence bar** — the model's own number against `threshold`, through the production
  `_normalize_monitor_confidence`, so a non-finite or non-numeric confidence fails closed here
  exactly as it does in the engine.
* **trajectory veto** — re-read from the row's own stored `prompt.trajectory` text (`DIRECTION:
  descending` with no `ANOMALY:` line), which is what `trajectory_vetoes_kill` answers `True` for.
  On this corpus it is INERT: 144 of the 144 rows with a measured `descending` curve were judged
  `healthy`, so it vetoes nothing and moves no number. That is worth reporting rather than
  assuming — the veto shipped after v7's flat-tail misread, and the four v6 false stops it reads as
  its motivating cases carry NO measured trajectory at all (the tracker postdates those runs).
* **NOT the confirm streak.** `_MONITOR_KILL_CONFIRM_TICKS` needs two consecutive `broken` verdicts
  about the same stage log, and the corpus cannot supply the second one: the arm that schedules a
  confirmation look at `_MONITOR_CONFIRM_DELAY_S` (30 s) is gated on `_KILL_ELIGIBLE_ROLES`, every
  recorded alert carries `log_role: work`, so **no confirmation look was ever taken** — measured,
  the median gap from a `broken` verdict to the next decision about the same attempt is 617.8 s
  (min 605.5, n=76), i.e. the ordinary cadence. Modelling a streak over ticks ten minutes apart
  measures a different mechanism on different evidence; for the record it would report 2 of 27
  wasted attempts caught instead of 7, which is a number about the corpus's spacing and not about
  the gate.

`ScoreReport.gate` is what the report prints so a gated number can never be quoted as an ungated
one, and the gate is OFF by default so every baseline in `tests/test_judge_bench.py` is unmoved.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from looplab.judgebench.judge_corpus import (
    CORPUS_LIMITS, LABEL_PRODUCTIVE, LABEL_WASTED, VERDICTS, messages_of, render_prompt)

# The two labels a correctness claim may rest on. Everything else is reported, never scored.
PRIMARY_LABELS = (LABEL_WASTED, LABEL_PRODUCTIVE)

# Which verdicts count as "this run should stop". `watch` is deliberately NOT a stop: the system
# prompt defines it as the non-fatal answer, and Phase 3 (`should_monitor_kill`) claims only
# `broken`. Changing this constant changes what the bench MEANS, so it is named, not inlined.
STOP_VERDICTS = frozenset({"broken"})

# The row's own copy of what `trajectory_context` rendered. Read rather than re-derived: the tail
# the tracker observed is gone, but the SENTENCE the engine wrote from its measurement is stored
# verbatim in `prompt.trajectory`, and `trajectory_vetoes_kill` is a function of exactly the two
# facts that sentence carries.
_TRAJECTORY_DIRECTION_RE = re.compile(r"DIRECTION: (\w+)")
_TRAJECTORY_ANOMALY = "ANOMALY:"


@dataclass(frozen=True)
class Gate:
    """The deterministic conjuncts the ENGINE applies after the model has spoken.

    Defaults are the shipped ones (`Settings.train_monitor_kill_confidence`). See the module
    docstring for what this deliberately does NOT model and why.

    IT READS PRODUCTION ON PURPOSE, and that is the opposite of the sibling bench's frozen arm —
    worth stating, because the two look like the same decision and are not. `triage_score.py`'s
    `frozen_replay_candidate` reads NO production name (`_frozen_failure_reason_v1` is a verbatim
    snapshot) because its job is to record how the OLD decider scored, and an arm that follows the
    code silently starts measuring a different program the day that code changes. This `Gate` is the
    other kind — `live_engine_candidate`'s kind. It answers "what would the engine act on TODAY", so
    it MUST follow `_normalize_monitor_confidence` and the shipped threshold; freezing it would make
    it lie the first time the gate moved. What stops it drifting SILENTLY is not a snapshot but the
    `judgebench` entry in `tests/test_cross_package_private_seams.py`, which turns a rename into a
    red test. If a HISTORICAL gated number is ever wanted, add a frozen arm beside this one rather
    than freezing this one.
    """
    threshold: float = 0.8
    trajectory_veto: bool = True

    def stops(self, row: dict, status, confidence) -> bool:
        """Whether this answer would reach an intervention. Pure/deterministic."""
        if status not in STOP_VERDICTS:
            return False
        from looplab.engine.train_monitor import _normalize_monitor_confidence
        value, valid = _normalize_monitor_confidence(confidence)
        if not (valid and value >= self.threshold):
            return False
        return not (self.trajectory_veto and trajectory_vetoes(row))

    def describe(self) -> str:
        return ("confidence>=%.2f%s, confirm streak NOT modelled (see module docstring)"
                % (self.threshold, ", trajectory veto" if self.trajectory_veto else ""))


def trajectory_vetoes(row: dict) -> bool:
    """`trajectory_vetoes_kill` re-read from the row's stored trajectory text. Pure."""
    text = ((row.get("prompt") or {}).get("trajectory") or "")
    match = _TRAJECTORY_DIRECTION_RE.search(text)
    return bool(match and match.group(1) == "descending" and _TRAJECTORY_ANOMALY not in text)


def answer_of(value):
    """A candidate's answer as `(status, confidence)`.

    A bare status string is still an answer — that is what every offline candidate returned before
    the gate existed, and an ungated score never needs the second half. A candidate that wants to be
    scored THROUGH the gate answers with a mapping (or any object carrying `.status`/`.confidence`),
    because the confidence bar is the conjunct that separates this corpus's five false stops from
    all 49 of its confident true ones and it is not derivable from the status.
    """
    if value is None or isinstance(value, str):
        return value, None
    if isinstance(value, dict):
        return value.get("status"), value.get("confidence")
    return getattr(value, "status", None), getattr(value, "confidence", None)


@dataclass
class ScoreReport:
    """Everything measured, with the two agreements kept apart."""
    candidate: str = "?"
    rows: int = 0
    answered: int = 0
    label_coverage: int = 0            # rows with a PRIMARY label — the accuracy denominator
    recorded_coverage: int = 0         # rows the incumbent actually answered
    # vs the LABEL (accuracy)
    label_confusion: dict = field(default_factory=dict)   # (label, verdict) -> n
    false_stop: int = 0                # said `broken` about a run that finished productive
    missed_stop: int = 0               # never said `broken` about a wasted run
    true_stop: int = 0
    true_continue: int = 0
    # vs the RECORDED VERDICT (churn, NOT accuracy)
    recorded_agreement: int = 0
    recorded_confusion: dict = field(default_factory=dict)  # (recorded, candidate) -> n
    # excluded, reported
    by_label: dict = field(default_factory=dict)
    by_stage: dict = field(default_factory=dict)
    unanswered: list = field(default_factory=list)
    limits: str = CORPUS_LIMITS
    # None = a stop is the bare `broken` verdict (the prompt-comparison unit). A `Gate` means the
    # stop counts are what the ENGINE would have acted on, and `format_report` says so.
    gate: Optional[Gate] = None

    @property
    def label_accuracy(self) -> Optional[float]:
        """Agreement with the outcome. None when nothing is labelled — never 0.0, because "no
        evidence" and "always wrong" are different answers and a chart cannot tell them apart."""
        total = self.true_stop + self.true_continue + self.false_stop + self.missed_stop
        return None if total == 0 else (self.true_stop + self.true_continue) / total

    @property
    def recorded_agreement_rate(self) -> Optional[float]:
        """How much behaviour a change moves. NOT accuracy — see the module docstring."""
        return None if self.recorded_coverage == 0 else self.recorded_agreement / self.recorded_coverage


def _stop(status) -> Optional[bool]:
    if status not in VERDICTS:
        return None
    return status in STOP_VERDICTS


def _stop_of(row, answer, gate: Optional[Gate]):
    """`(status, stopped)` for one answer, under the gate if there is one.

    A gate with no confidence to weigh is a REFUSAL and not a quiet `False`: scoring it as "did not
    stop" would hand a candidate that reports no confidence a perfect false-stop record for saying
    nothing, which is the same defect the `unanswered` branch below exists to stop one field over.
    """
    status, confidence = answer_of(answer)
    if gate is None or status not in VERDICTS:
        return status, _stop(status)
    if status in STOP_VERDICTS and confidence is None:
        raise ValueError(
            "case %r answered %r with no confidence, and a Gate cannot weigh it — a candidate "
            "scored through the gate must answer {'status': ..., 'confidence': ...}"
            % (row.get("case_id"), status))
    return status, gate.stops(row, status, confidence)


def score_dataset(rows: list, candidate: Callable, *, name: str = "candidate",
                  gate: Optional[Gate] = None) -> ScoreReport:
    """Run `candidate(row) -> status` over the corpus and measure it both ways.

    `candidate` returns one of `VERDICTS`, or None for "no answer" — a real failure mode (a parser
    miss, a refusal), counted in `unanswered` rather than silently scored as a `healthy`. It may
    instead return `{"status": ..., "confidence": ...}`, which is what a `gate` needs (`answer_of`).

    `gate` changes what `false_stop`/`true_stop` COUNT — the engine's intervention rather than the
    model's verdict — and nothing else: the confusion matrices and the churn number stay keyed on
    the verdict, because a gate does not change what the candidate SAID.
    """
    report = ScoreReport(candidate=name, rows=len(rows), gate=gate)
    for row in rows:
        label = (row.get("label") or {}).get("label")
        recorded = (row.get("recorded") or {}).get("status")
        stage = (row.get("context") or {}).get("stage")
        report.by_label[label] = report.by_label.get(label, 0) + 1
        answer, stopped = _stop_of(row, candidate(row), gate)
        if answer not in VERDICTS:
            # An answer OUTSIDE the closed vocabulary is not an answer. Letting it through counted
            # it as `answered` and then, via `_stop` returning None, scored it exactly like
            # `healthy` — so a candidate that replies "unknown" to every row would have posted a
            # perfect false-stop record for saying nothing.
            report.unanswered.append(row.get("case_id"))
            continue
        report.answered += 1
        key = "%s/%s" % (stage, answer)
        report.by_stage[key] = report.by_stage.get(key, 0) + 1
        if recorded in VERDICTS:
            report.recorded_coverage += 1
            pair = "%s->%s" % (recorded, answer)
            report.recorded_confusion[pair] = report.recorded_confusion.get(pair, 0) + 1
            if recorded == answer:
                report.recorded_agreement += 1
        if label in PRIMARY_LABELS:
            report.label_coverage += 1
            cell = "%s/%s" % (label, answer)
            report.label_confusion[cell] = report.label_confusion.get(cell, 0) + 1
            stop = stopped
            if label == LABEL_WASTED:
                report.true_stop += int(bool(stop))
                report.missed_stop += int(not stop)
            else:
                report.false_stop += int(bool(stop))
                report.true_continue += int(not stop)
    return report


def per_attempt_report(rows: list, candidate: Callable, gate: Optional[Gate] = None) -> dict:
    """The operator's actual question, which the per-decision matrix cannot answer.

    A `healthy` at minute ten of an eval that goes bad at hour three is not a mistake — the judge is
    asked again every ten minutes and only has to be right ONCE to save the compute. So the unit
    that matters is the EVAL ATTEMPT: did the candidate EVER say stop on a wasted one, and did it
    ever say stop on a productive one. Reported beside the per-decision matrix, never instead of it,
    because the per-decision matrix is the one stable enough to A/B a prompt on.

    The key is the ATTEMPT and not the node on purpose. A node whose train stage failed three times
    and then succeeded holds three wasted attempts and one productive one; keyed by node they
    collapse into a single misleading `wasted` covering 20 decisions, 16 of which watched the
    attempt that worked.
    """
    attempts: dict = {}
    for row in rows:
        label = (row.get("label") or {}).get("label")
        if label not in PRIMARY_LABELS:
            continue
        prov = row.get("provenance") or {}
        context = row.get("context") or {}
        key = "%s:n%s:%s@%s" % (prov.get("run"), prov.get("node_id"), context.get("stage"),
                                context.get("attempt_end_ts"))
        entry = attempts.setdefault(key, {"label": label, "decisions": 0, "stops": 0,
                                          "first_stop_ts": None,
                                          "attempt_end_ts": context.get("attempt_end_ts")})
        entry["decisions"] += 1
        if _stop_of(row, candidate(row), gate)[1]:
            entry["stops"] += 1
            ts = prov.get("ts") or 0.0
            if entry["first_stop_ts"] is None or ts < entry["first_stop_ts"]:
                entry["first_stop_ts"] = ts
    for entry in attempts.values():
        # Seconds of compute a stop at the first `broken` would have avoided. APPROXIMATE and says
        # so: `attempt_end_ts` is when the eval attempt's `stage_finished` rows were flushed, which
        # is the end of the whole attempt and not of the watched stage alone.
        entry["approx_seconds_saveable"] = (
            round(entry["attempt_end_ts"] - entry["first_stop_ts"], 1)
            if entry["first_stop_ts"] and entry["attempt_end_ts"] else None)
    return attempts


def attempt_totals(attempts: dict) -> dict:
    """The two headline counts, at the unit an operator pays in."""
    wasted = [e for e in attempts.values() if e["label"] == LABEL_WASTED]
    productive = [e for e in attempts.values() if e["label"] == LABEL_PRODUCTIVE]
    caught = [e for e in wasted if e["stops"]]
    return {"wasted_attempts": len(wasted),
            "wasted_caught": len(caught),
            "productive_attempts": len(productive),
            "productive_falsely_stopped": len([e for e in productive if e["stops"]]),
            "approx_seconds_saveable": round(
                sum(e["approx_seconds_saveable"] or 0.0 for e in caught), 1)}


def recorded_candidate(row):
    """The incumbent replaying itself — the baseline every other candidate is read against.

    Answers the MAPPING form, so the incumbent is scorable through a `Gate` without a second
    candidate function. `answer_of` reduces it to the status everywhere a gate is absent, so every
    ungated number this function has ever produced is byte-identical.
    """
    recorded = row.get("recorded") or {}
    return {"status": recorded.get("status"), "confidence": recorded.get("confidence")}


def jsonl_candidate(path):
    """Answers captured earlier: one `{"case_id": ..., "status": ...}` per line. Fully offline.

    A `confidence` on the line is carried through — that is what makes a captured live arm (the
    JSONL `llm_candidate` writes) scorable through a `Gate` offline, at no further cost.
    """
    answers = {}
    with Path(path).open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            answers[row.get("case_id")] = {"status": row.get("status"),
                                           "confidence": row.get("confidence")}
    return lambda row: answers.get(row.get("case_id"))


def llm_candidate(client, *, overrides: Optional[dict] = None):
    """THE LIVE ARM. Constructing this is the decision to spend money — one provider call per row.

    `overrides` replaces prompt ingredients before re-rendering (`{"system": "..."}` benches a
    changed system prompt over the SAME recorded evidence). A row whose `prompt_split_exact` is
    false cannot be re-rendered safely, so an override is refused there rather than silently
    replaying the original message and reporting it as the new prompt.

    Deliberately NOT agentic. 369 of the 450 recorded decisions were reached WITH log tools and this
    replays without them, so a candidate scored here is answering from the tail alone. That is a
    real handicap against the incumbent and the reason `provenance.tools_available` is on every row:
    slice to `tools_available == false` for a like-for-like comparison, or accept that the number is
    a floor.
    """
    from looplab.core.parse import parse_structured
    from looplab.engine.train_monitor import TrainingVerdict

    def run(row):
        parts = dict(row.get("prompt") or {})
        if overrides:
            if not parts.get("prompt_split_exact"):
                return None
            parts.update(overrides)
            messages = render_prompt(parts)
        else:
            messages = messages_of(row)
        verdict = parse_structured(client, messages, TrainingVerdict, "tool_call")
        # The MAPPING form (`answer_of`), so one paid pass is scorable ungated AND through a
        # `Gate`, and so `fault` — which no recorded row carries, because the field postdates every
        # preserved run — is captured by the arm that is already spending the money.
        return {"status": getattr(verdict, "status", None),
                "confidence": getattr(verdict, "confidence", None),
                "fault": getattr(verdict, "fault", None)}

    return run


def format_report(report: ScoreReport, attempts: Optional[dict] = None) -> str:
    """Plain text, and the limits paragraph is printed FIRST — a caveat under the number is a
    caveat nobody reads."""
    lines = ["", "!! %s" % report.limits, "",
             "candidate: %s   rows=%d answered=%d" % (report.candidate, report.rows,
                                                      report.answered)]
    accuracy = report.label_accuracy
    lines.append("")
    if report.gate is not None:
        # ABOVE the numbers, for the same reason the limits paragraph is: a gated stop count and an
        # ungated one differ by 5 on this corpus, and a reader who learns which one they are looking
        # at underneath the number has already quoted it.
        lines.append("GATED: a stop is what the ENGINE would act on, not what the model SAID —")
        lines.append("       %s" % report.gate.describe())
    lines.append("AGREEMENT WITH THE LABEL (accuracy) over %d outcome-labelled decisions"
                 % report.label_coverage)
    lines.append("  accuracy            : %s" % ("n/a — nothing labelled" if accuracy is None
                                                 else "%.3f" % accuracy))
    stopped = "would have stopped" if report.gate is not None else "said broken"
    lines.append("  %s, finished fine (false stop) : %d" % (stopped, report.false_stop))
    lines.append("  never %s on a wasted run       : %d" % (stopped, report.missed_stop))
    lines.append("  %s on a wasted run             : %d" % (stopped, report.true_stop))
    lines.append("  let a productive run continue           : %d" % report.true_continue)
    for cell in sorted(report.label_confusion):
        lines.append("    %-28s %d" % (cell, report.label_confusion[cell]))
    rate = report.recorded_agreement_rate
    lines.append("")
    lines.append("AGREEMENT WITH THE RECORDED VERDICT (churn, NOT accuracy) over %d decisions"
                 % report.recorded_coverage)
    lines.append("  agreement           : %s" % ("n/a" if rate is None else "%.3f" % rate))
    for pair in sorted(report.recorded_confusion):
        lines.append("    %-28s %d" % (pair, report.recorded_confusion[pair]))
    lines.append("")
    lines.append("EXCLUDED FROM ACCURACY (reported, never scored)")
    for label in sorted(report.by_label, key=str):
        lines.append("  %-20s %d" % (label, report.by_label[label]))
    if report.unanswered:
        lines.append("  unanswered           %d" % len(report.unanswered))
    if attempts:
        totals = attempt_totals(attempts)
        lines.append("")
        lines.append("PER EVAL ATTEMPT (did it EVER say stop — the unit compute is paid in)")
        lines.append("  wasted attempts caught      : %d of %d"
                     % (totals["wasted_caught"], totals["wasted_attempts"]))
        lines.append("  productive attempts stopped : %d of %d"
                     % (totals["productive_falsely_stopped"], totals["productive_attempts"]))
        lines.append("  approx compute saveable     : %.1f h (see per_attempt_report on why approx)"
                     % (totals["approx_seconds_saveable"] / 3600.0))
        for key in sorted(attempts):
            entry = attempts[key]
            lines.append("    %-52s %-11s decisions=%-3d stops=%-3d saveable_s=%s"
                         % (key, entry["label"], entry["decisions"], entry["stops"],
                            entry["approx_seconds_saveable"]))
    return "\n".join(lines) + "\n"
