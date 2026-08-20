"""Read one or two `triage_diagnostician_replay.py` capture files and say what they measured.

    python tools/triage_diagnostician_report.py bench-out/cand.durable.jsonl bench-out/cand.widened.jsonl

Offline: it makes no provider call and reads only the committed corpus plus the capture files.

FOUR QUESTIONS, and the accuracy number is only the first of them. A diagnostician that is right
while citing nothing checkable is a different proposition from one that is right and shows you
where; a confident answer on a row whose truth is outside its vocabulary is a `diverged` failure
being called something else; and a cost has to be quotable in the same unit as the 8.82 provider
calls per failure the triage call already spends, or it cannot be weighed against it.

THE BASELINE IS THE ENGINE'S OWN ANSWER ON THE SAME ROWS, not the corpus-wide 74.6 %. Of the 118
labelled rows, 94 are handed to the diagnostician and 65 of those are already right structurally —
so the question this file answers is what happened to the OTHER 29 and whether any of the 65 were
lost. `score-triage --answers` prints the corpus-wide number beside it; both are reported, never
averaged.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from looplab.engine.failure_diagnosis import (  # noqa: E402
    DIAGNOSABLE_ENGINE_REASONS, DIAGNOSED_FAILURE_REASONS)
from looplab.engine.triage import _failure_reason  # noqa: E402
from looplab.judgebench import triage_corpus, triage_score  # noqa: E402


def load_answers(path: Path) -> dict:
    out = {}
    for line in Path(path).open("r", encoding="utf-8"):
        line = line.strip()
        if line:
            row = json.loads(line)
            out[row.get("case_id")] = row
    return out


def deterministic_map(rows: list) -> dict:
    det = {}
    for row in rows:
        res = triage_score.replay_result(row)
        det[row["case_id"]] = None if res is None else _failure_reason(res)
    return det


def report(rows: list, answers: dict, name: str) -> list:
    det = deterministic_map(rows)
    out = ["", "=" * 88, "ARM: %s" % name, "=" * 88]

    # --- 1. the score, against the frozen deterministic half and the reachable ceiling -----------
    scored = triage_score.score_dataset(
        rows, lambda r: (answers.get(r["case_id"]) or {}).get("reason"), name=name)
    live = triage_score.score_dataset(rows, triage_score.live_engine_candidate(),
                                      name="live", live=True)
    covered = scored.label_coverage
    out += ["", "SCORE over %d labelled rows" % covered,
            "  deterministic half (engine only) : %d/%d = %.1f%%"
            % (live.correct, live.label_coverage, 100.0 * live.correct / live.label_coverage),
            "  with the diagnostician           : %d/%d = %.1f%%"
            % (scored.correct, covered, 100.0 * scored.correct / covered)]
    handed = [r for r in rows
              if det[r["case_id"]] in DIAGNOSABLE_ENGINE_REASONS
              and (r.get("label") or {}).get("reason") not in (None, triage_corpus.LABEL_UNKNOWN)]
    right_by_luck = sum(1 for r in handed if det[r["case_id"]] == r["label"]["reason"])
    unwinnable = [r for r in handed
                  if r["label"]["reason"] not in DIAGNOSED_FAILURE_REASONS
                  and r["label"]["reason"] != det[r["case_id"]]]
    ceiling = live.correct + (len(handed) - right_by_luck) - len(unwinnable)
    out += ["  reachable ceiling                : %d/%d = %.1f%%   (headroom %d, unwinnable %d)"
            % (ceiling, covered, 100.0 * ceiling / covered,
               len(handed) - right_by_luck, len(unwinnable))]

    # --- the 29: won, lost, still wrong ---------------------------------------------------------
    won, lost, still_wrong, unanswered = [], [], [], []
    for row in handed:
        truth = row["label"]["reason"]
        engine = det[row["case_id"]]
        answer = (answers.get(row["case_id"]) or {}).get("reason")
        if answer is None:
            unanswered.append((row["case_id"], engine, truth))
        elif answer == truth and engine != truth:
            won.append((row["case_id"], engine, truth))
        elif answer != truth and engine == truth:
            lost.append((row["case_id"], engine, answer))
        elif answer != truth and engine != truth:
            still_wrong.append((row["case_id"], engine, answer, truth))
    out += ["", "THE 94 HANDED ROWS (%d already right structurally, %d the headroom)"
            % (right_by_luck, len(handed) - right_by_luck),
            "  won   (engine wrong -> diagnostician right) : %d" % len(won),
            "  lost  (engine right -> diagnostician wrong) : %d" % len(lost),
            "  still wrong                                 : %d" % len(still_wrong),
            "  no answer captured                          : %d" % len(unanswered)]
    for label, items in (("WON", won), ("LOST", lost)):
        for case, engine, other in items:
            out.append("    %-6s %-44s engine=%-13s -> %s" % (label, case, engine, other))
    if still_wrong:
        out.append("    STILL WRONG (engine -> answer, truth):")
        for case, engine, answer, truth in still_wrong:
            out.append("      %-44s %-13s -> %-13s truth=%s" % (case, engine, answer, truth))

    # --- the confusion matrix over the handed rows ----------------------------------------------
    confusion = Counter()
    for row in handed:
        answer = (answers.get(row["case_id"]) or {}).get("reason") or "<no answer>"
        confusion[(row["label"]["reason"], answer)] += 1
    out += ["", "CONFUSION over the handed rows (truth -> answer)"]
    for truth in sorted({t for t, _ in confusion}):
        cells = sorted(((a, n) for (t, a), n in confusion.items() if t == truth),
                       key=lambda kv: -kv[1])
        total = sum(n for _, n in cells)
        hit = dict(cells).get(truth, 0)
        out.append("  truth=%-14s n=%-3d correct=%-3d  %s"
                   % (truth, total, hit,
                      "  ".join("%s:%d" % (a, n) for a, n in cells if a != truth) or "-"))

    # --- 2. citation quality --------------------------------------------------------------------
    asked = [answers[c] for c in answers if answers[c].get("asked")]
    sources = Counter(str((a.get("evidence") or {}).get("source")) for a in asked)
    resolved = Counter(str(a.get("evidence_resolved")) for a in asked)
    checkable = [a for a in asked
                 if (a.get("evidence") or {}).get("source") in ("code", "log")]
    ok = sum(1 for a in checkable if a.get("evidence_resolved") is True)
    out += ["", "CITATION QUALITY over %d asked rows" % len(asked),
            "  evidence_source     : %s" % dict(sources),
            "  evidence_resolved   : %s   (None = nothing filesystem-shaped was cited)"
            % dict(resolved),
            "  filesystem citations: %d of %d asked (%.1f%%); resolved %d = %.1f%% of those"
            % (len(checkable), len(asked),
               100.0 * len(checkable) / max(1, len(asked)), ok,
               100.0 * ok / max(1, len(checkable)))]
    quoted = sum(1 for a in asked if str((a.get("evidence") or {}).get("quote") or "").strip())
    out.append("  carried a quote     : %d of %d (%.1f%%)"
               % (quoted, len(asked), 100.0 * quoted / max(1, len(asked))))

    # --- 3. the unwinnable rows -----------------------------------------------------------------
    out += ["", "THE UNWINNABLE ROWS (truth outside DIAGNOSED_FAILURE_REASONS)"]
    for row in unwinnable:
        rec = answers.get(row["case_id"]) or {}
        out.append("  %-44s truth=%-10s engine=%-13s answered=%-13s evidence=%s"
                   % (row["case_id"], row["label"]["reason"], det[row["case_id"]],
                      rec.get("reason"), (rec.get("evidence") or {}).get("source")))
        if rec.get("rationale"):
            out.append("      %s" % str(rec["rationale"])[:160])

    # --- 4. cost --------------------------------------------------------------------------------
    calls = sum(int(a.get("calls") or 0) for a in asked)
    seconds = sum(float(a.get("seconds") or 0.0) for a in asked)
    prompt = sum(int(a.get("prompt_tokens") or 0) for a in asked)
    completion = sum(int(a.get("completion_tokens") or 0) for a in asked)
    priced = sum(int(a.get("priced_calls") or 0) for a in asked)
    n = max(1, len(asked))
    out += ["", "COST, MEASURED, per DECISION (the %d rows a model was asked about)" % len(asked),
            "  provider calls  : %d total, %.2f per decision" % (calls, calls / n),
            "  wall seconds    : %.0f total, %.1f per decision (median %.1f)"
            % (seconds, seconds / n,
               sorted(float(a.get("seconds") or 0.0) for a in asked)[len(asked) // 2]
               if asked else 0.0),
            "  prompt tokens   : %d total, %d per decision" % (prompt, prompt // n),
            "  completion      : %d total, %d per decision" % (completion, completion // n),
            "  priced calls    : %d of %d — `cost` is a floor wherever these disagree"
            % (priced, calls)]
    errors = [a for a in answers.values() if a.get("harness_error")]
    if errors:
        out.append("  HARNESS ERRORS  : %d — %s"
                   % (len(errors), Counter(a["harness_error"].split(":")[0] for a in errors)))
    return out


def compare(rows: list, captures: list) -> list:
    """The gap between two arms, ROW BY ROW — which is the price of the thin durable record.

    An aggregate difference of accuracy would hide the shape: a wider window can win a row and lose
    another at the same score. What is reported is the rows that MOVED, in both directions, and the
    count of rows where widening added nothing to look at (so the arms are the same question)."""
    det = deterministic_map(rows)
    (name_a, ans_a), (name_b, ans_b) = captures
    out = ["", "=" * 88, "ARM GAP: %s -> %s" % (name_a, name_b), "=" * 88]
    moved_right, moved_wrong, both_wrong_moved = [], [], []
    identical_evidence = 0
    for row in rows:
        truth = (row.get("label") or {}).get("reason")
        if truth in (None, triage_corpus.LABEL_UNKNOWN):
            continue
        if det[row["case_id"]] not in DIAGNOSABLE_ENGINE_REASONS:
            continue
        # BOTH captures must hold this row. An absent case_id is "this arm has not reached it",
        # which is a fact about the capture; a present row whose `reason` is None is a fact about
        # the diagnostician. Conflating them makes an unfinished run look like a regression.
        if row["case_id"] not in ans_a or row["case_id"] not in ans_b:
            continue
        on = (row.get("evidence") or {}).get("on_demand") or {}
        extra = list(on.get("triage_log_reads") or [])
        if (on.get("stage_log") or {}).get("tail"):
            extra.append("tail")
        if not extra:
            identical_evidence += 1
        a = (ans_a.get(row["case_id"]) or {}).get("reason")
        b = (ans_b.get(row["case_id"]) or {}).get("reason")
        if a == b:
            continue
        if b == truth:
            moved_right.append((row["case_id"], a, b))
        elif a == truth:
            moved_wrong.append((row["case_id"], a, b))
        else:
            both_wrong_moved.append((row["case_id"], a, b, truth))
    out += ["  handed rows where widening adds NOTHING to read : %d (the two arms ask the same "
            "question there)" % identical_evidence,
            "  widening turned WRONG -> RIGHT                  : %d" % len(moved_right),
            "  widening turned RIGHT -> WRONG                  : %d" % len(moved_wrong),
            "  moved between two wrong answers                 : %d" % len(both_wrong_moved)]
    for label, items in (("-> RIGHT", moved_right), ("-> WRONG", moved_wrong)):
        for case, a, b in items:
            out.append("    %-9s %-44s %s -> %s" % (label, case, a, b))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("captures", nargs="+", type=Path)
    ap.add_argument("--dataset", type=Path, default=triage_corpus.DEFAULT_DATASET)
    args = ap.parse_args(argv)
    rows = triage_corpus.read_dataset(args.dataset)["rows"]
    lines, loaded = [], []
    for capture in args.captures:
        answers = load_answers(capture)
        arm = next((str(a.get("arm")) for a in answers.values() if a.get("arm")), "?")
        loaded.append((arm, answers))
        lines += report(rows, answers, "%s (%s, %d rows captured)"
                        % (capture.name, arm, len(answers)))
    if len(loaded) == 2:
        lines += compare(rows, loaded)
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
