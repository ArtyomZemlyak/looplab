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
"""
from __future__ import annotations

import json
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


def score_dataset(rows: list, candidate: Callable, *, name: str = "candidate") -> ScoreReport:
    """Run `candidate(row) -> status` over the corpus and measure it both ways.

    `candidate` returns one of `VERDICTS`, or None for "no answer" — a real failure mode (a parser
    miss, a refusal), counted in `unanswered` rather than silently scored as a `healthy`.
    """
    report = ScoreReport(candidate=name, rows=len(rows))
    for row in rows:
        label = (row.get("label") or {}).get("label")
        recorded = (row.get("recorded") or {}).get("status")
        stage = (row.get("context") or {}).get("stage")
        report.by_label[label] = report.by_label.get(label, 0) + 1
        answer = candidate(row)
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
            stop = _stop(answer)
            if label == LABEL_WASTED:
                report.true_stop += int(bool(stop))
                report.missed_stop += int(not stop)
            else:
                report.false_stop += int(bool(stop))
                report.true_continue += int(not stop)
    return report


def per_attempt_report(rows: list, candidate: Callable) -> dict:
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
        if _stop(candidate(row)):
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


def recorded_candidate(row) -> Optional[str]:
    """The incumbent replaying itself — the baseline every other candidate is read against."""
    return (row.get("recorded") or {}).get("status")


def jsonl_candidate(path):
    """Answers captured earlier: one `{"case_id": ..., "status": ...}` per line. Fully offline."""
    answers = {}
    with Path(path).open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            answers[row.get("case_id")] = row.get("status")
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
        return getattr(verdict, "status", None)

    return run


def format_report(report: ScoreReport, attempts: Optional[dict] = None) -> str:
    """Plain text, and the limits paragraph is printed FIRST — a caveat under the number is a
    caveat nobody reads."""
    lines = ["", "!! %s" % report.limits, "",
             "candidate: %s   rows=%d answered=%d" % (report.candidate, report.rows,
                                                      report.answered)]
    accuracy = report.label_accuracy
    lines.append("")
    lines.append("AGREEMENT WITH THE LABEL (accuracy) over %d outcome-labelled decisions"
                 % report.label_coverage)
    lines.append("  accuracy            : %s" % ("n/a — nothing labelled" if accuracy is None
                                                 else "%.3f" % accuracy))
    lines.append("  said broken, finished fine (false stop) : %d" % report.false_stop)
    lines.append("  never said broken on a wasted run       : %d" % report.missed_stop)
    lines.append("  said broken on a wasted run             : %d" % report.true_stop)
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
