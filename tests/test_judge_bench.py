"""Guards for the judge bench (`looplab/judgebench/`).

Two jobs, and they are different guards:

1. **The committed dataset is DERIVED and must not be hand-edited.** It is 278 KB of gzipped
   evidence in the repo, so "never hand-edit it" needs something that enforces it on a machine with
   no `runs/` at all. `test_every_label_rederives` recomputes every label from the row's own stored
   facts through the production rule, so an edited label goes red offline.
2. **The bench must not lie about what it measured.** The label/verdict vocabularies stay closed,
   the two agreements stay in separate fields, `budget_exhausted` stays out of the accuracy
   denominator, and the corpus-limits paragraph travels with the artefact.

Most of these DRIVE the property over a synthetic run directory built here rather than pinning
source text, because the label rule is exactly the kind of thing a substring pin cannot check.
"""
from __future__ import annotations

import gzip
import json
import typing

import pytest

from looplab.judgebench import judge_corpus, score
from looplab.judgebench.judge_corpus import (
    CORPUS_LIMITS, DATASET_SCHEMA, DEFAULT_DATASET, LABELS, LABEL_BUDGET_EXHAUSTED,
    LABEL_PRODUCTIVE, LABEL_UNKNOWN, LABEL_WASTED, VERDICTS, build_dataset, extract_run,
    messages_of, read_dataset, rederive_label, render_prompt, write_dataset)


# ---------------------------------------------------------------- synthetic run (drives the rule)

_SYSTEM = "You are the ML engineer who wrote this training script."
_STAGE_CTX = ("This is the live log of pipeline stage 'train' (stage 2 of 3; the pipeline is "
              "mine -> train -> score). Judge THIS stage's output only.")


def _decision_spans(*, node_id, phase_span, start, digest, status, context="Optimizing metric 'm'."):
    """One monitor DECISION as the engine records it: a tool turn, then the `emit` that ends it."""
    messages = render_prompt({"system": _SYSTEM, "context": context, "stage_context": _STAGE_CTX,
                              "trajectory": "", "look_invitation": "", "digest": digest})
    common = {"kind": "generation", "name": "generation", "run_id": "synthetic",
              "trace_id": "t" * 32, "parent_id": phase_span, "status": "OK", "duration_s": 1.0}
    return [
        {**common, "span_id": phase_span + "a", "start": start,
         "attributes": {"phase": "train_monitor", "phase_span": phase_span, "node_id": node_id,
                        "generation": 0, "model": "synthetic-model", "input": messages,
                        "output": "[tool_calls: read_log]",
                        "tool_calls": [{"name": "read_log", "arguments": "{}"}]}},
        {**common, "span_id": phase_span + "b", "start": start + 1.0,
         "attributes": {"phase": "train_monitor", "phase_span": phase_span, "node_id": node_id,
                        "generation": 0, "model": "synthetic-model", "input": messages,
                        "output": "done",
                        "tool_calls": [{"name": "emit", "arguments": json.dumps(
                            {"status": status, "reason": "synthetic", "confidence": 0.9})}]}},
    ]


@pytest.fixture()
def synthetic_run(tmp_path):
    """A run holding one wasted node, one productive node and one timeout — the three label classes.

    `stage_finished` rows are written the way the engine writes them (all of an eval attempt's rows
    flushed together at the END of the attempt), because that burst is exactly what the attempt
    clustering has to cope with and a fixture that spread them out would test nothing.
    """
    run = tmp_path / "synthetic-run"
    run.mkdir()
    (run / "task.snapshot.json").write_text(json.dumps({"direction": "max"}), encoding="utf-8")

    spans = []
    spans += _decision_spans(node_id=1, phase_span="p1", start=1000.0, digest="loss 5.0\nloss 5.0\n",
                             status="broken")
    spans += _decision_spans(node_id=2, phase_span="p2", start=3000.0, digest="loss 2.0\nloss 1.0\n",
                             status="healthy")
    spans += _decision_spans(node_id=3, phase_span="p3", start=5000.0, digest="slow but moving\n",
                             status="healthy")
    (run / "spans.jsonl").write_text("".join(json.dumps(s) + "\n" for s in spans), encoding="utf-8")

    events = [
        # node 1: train ran to completion and the model it produced scored nothing -> wasted.
        {"seq": 1, "ts": 2000.0, "type": "stage_finished",
         "data": {"node_id": 1, "name": "train", "status": "ok", "exit_code": 0, "seconds": 900.0}},
        {"seq": 2, "ts": 2000.1, "type": "stage_finished",
         "data": {"node_id": 1, "name": "score", "status": "ok", "exit_code": 0, "seconds": 60.0}},
        {"seq": 3, "ts": 2001.0, "type": "node_evaluated",
         "data": {"node_id": 1, "metric": 0.0, "eval_seconds": 960.0}},
        # node 2: healthy all the way through, real metric -> productive.
        {"seq": 4, "ts": 4000.0, "type": "stage_finished",
         "data": {"node_id": 2, "name": "train", "status": "ok", "exit_code": 0, "seconds": 900.0}},
        {"seq": 5, "ts": 4001.0, "type": "node_evaluated",
         "data": {"node_id": 2, "metric": 0.8, "eval_seconds": 960.0}},
        # node 3: the stage ran out of budget -> budget_exhausted, NOT wasted.
        {"seq": 6, "ts": 6000.0, "type": "stage_finished",
         "data": {"node_id": 3, "name": "train", "status": "timeout", "exit_code": -9,
                  "seconds": 3600.0}},
        {"seq": 7, "ts": 6001.0, "type": "node_failed",
         "data": {"node_id": 3, "reason": "crash", "eval_seconds": 3600.0}},
    ]
    (run / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events),
                                      encoding="utf-8")
    return run


def _by_node(rows):
    return {r["provenance"]["node_id"]: r for r in rows}


# ---------------------------------------------------------------------------- the label rule

def test_label_rule_separates_the_three_outcome_classes(synthetic_run):
    """The rule's whole point: a stage that SUCCEEDED and produced a dead model is `wasted`, and a
    stage that ran out of budget is neither `wasted` nor `productive`."""
    rows = _by_node(extract_run(synthetic_run))
    assert rows[1]["label"]["label"] == LABEL_WASTED
    assert rows[1]["label"]["label_basis"] == "node_metric_degenerate"
    assert rows[1]["label"]["stage_status"] == "ok"          # stage status alone would say "fine"
    assert rows[2]["label"]["label"] == LABEL_PRODUCTIVE
    assert rows[3]["label"]["label"] == LABEL_BUDGET_EXHAUSTED
    assert rows[3]["label"]["label_basis"] == "stage_timeout"


def test_degenerate_is_relative_to_the_run_best_not_to_zero(synthetic_run):
    """`2e-05` is not `0.0` and is just as dead; `0.225` against a `0.728` best is neither."""
    rows = _by_node(extract_run(synthetic_run))
    best = rows[1]["label"]["run_best_metric"]
    assert best == pytest.approx(0.8)
    assert judge_corpus._is_degenerate(2e-05, best, "max") is True
    assert judge_corpus._is_degenerate(0.225, 0.728, "max") is False
    # A minimised metric inverts the whole rule and no run in the corpus exercises it, so the
    # honest answer is "undecidable" and not the silently-wrong "not degenerate".
    assert judge_corpus._is_degenerate(0.0, 0.8, "min") is None


def test_a_stage_that_worked_is_not_charged_for_a_later_stage_failing(tmp_path):
    """The attribution that removes the corpus's biggest spurious miss: eight `healthy` verdicts
    about `mine`, on nodes whose `train` crashed minutes later. `mine` was fine and the judge
    watching it could not see `train`."""
    run = tmp_path / "r"
    run.mkdir()
    (run / "task.snapshot.json").write_text('{"direction": "max"}', encoding="utf-8")
    stage_ctx = ("This is the live log of pipeline stage 'mine' (stage 1 of 3; the pipeline is "
                 "mine -> train -> score). Judge THIS stage's output only.")
    spans = _decision_spans(node_id=0, phase_span="q0", start=10.0, digest="mining 5/10\n",
                            status="healthy")
    for span in spans:
        span["attributes"]["input"] = render_prompt(
            {"system": _SYSTEM, "context": "c", "stage_context": stage_ctx, "trajectory": "",
             "look_invitation": "", "digest": "mining 5/10\n"})
    (run / "spans.jsonl").write_text("".join(json.dumps(s) + "\n" for s in spans), encoding="utf-8")
    (run / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in [
        {"seq": 1, "ts": 100.0, "type": "stage_finished",
         "data": {"node_id": 0, "name": "mine", "status": "ok", "exit_code": 0, "seconds": 80.0}},
        {"seq": 2, "ts": 100.1, "type": "stage_finished",
         "data": {"node_id": 0, "name": "train", "status": "fail", "exit_code": 1, "seconds": 6.0}},
        {"seq": 3, "ts": 101.0, "type": "node_failed", "data": {"node_id": 0, "reason": "crash"}},
    ]), encoding="utf-8")
    row = extract_run(run)[0]
    assert row["context"]["stage"] == "mine"
    assert row["label"]["label"] == LABEL_PRODUCTIVE
    assert row["label"]["label_basis"].startswith("stage_ok_node_failed_elsewhere")


def test_attempts_are_clustered_from_bursts_not_from_stage_windows(tmp_path):
    """`ts - seconds` is NOT a stage's window: the engine flushes every stage row of an eval attempt
    together at the end, so `mine`'s row can claim a start an hour before the burst while `train`'s
    claims one six seconds before it, both ending at the same instant. Placing a decision by that
    arithmetic put 32 of 168 decisions in the wrong attempt or none at all."""
    run = tmp_path / "r"
    run.mkdir()
    (run / "task.snapshot.json").write_text('{"direction": "max"}', encoding="utf-8")
    (run / "spans.jsonl").write_text("", encoding="utf-8")
    (run / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in [
        {"seq": 1, "ts": 5000.0, "type": "stage_finished",
         "data": {"node_id": 0, "name": "mine", "status": "ok", "seconds": 3600.0}},
        {"seq": 2, "ts": 5000.05, "type": "stage_finished",
         "data": {"node_id": 0, "name": "train", "status": "fail", "seconds": 6.0}},
        {"seq": 3, "ts": 9000.0, "type": "stage_finished",
         "data": {"node_id": 0, "name": "mine", "status": "reused", "seconds": 0.0}},
        {"seq": 4, "ts": 9000.05, "type": "stage_finished",
         "data": {"node_id": 0, "name": "train", "status": "ok", "seconds": 3000.0}},
    ]), encoding="utf-8")
    attempts = judge_corpus._stage_attempts(run / "events.jsonl")[0]
    assert [end for end, _ in attempts] == [5000.05, 9000.05]
    assert attempts[0][1]["train"][0] == "fail" and attempts[1][1]["train"][0] == "ok"
    # A decision at t=4000 sits INSIDE the first attempt even though `mine`'s claimed window
    # (1400..5000) and `train`'s (4994..5000) disagree about where it is.
    assert next(smap for end, smap in attempts if 4000.0 <= end)["train"][0] == "fail"


# ---------------------------------------------------------------- the committed dataset

@pytest.fixture(scope="module")
def dataset():
    return read_dataset(DEFAULT_DATASET)


def test_every_label_rederives(dataset):
    """THE anti-hand-edit guard. Runs with no `runs/` present, so it holds in CI and in a
    `git archive HEAD` tree: every stored label is recomputed from that row's own stored facts
    through the production rule, and a label edited by hand no longer matches."""
    for row in dataset["rows"]:
        fresh = rederive_label(row)
        assert fresh["label"] == row["label"]["label"], row["case_id"]
        assert fresh["label_basis"] == row["label"]["label_basis"], row["case_id"]


def test_vocabularies_stay_closed(dataset):
    assert set(LABELS) == {LABEL_WASTED, LABEL_PRODUCTIVE, LABEL_BUDGET_EXHAUSTED, LABEL_UNKNOWN}
    for row in dataset["rows"]:
        assert row["label"]["label"] in LABELS
        assert row["recorded"]["status"] in VERDICTS or row["recorded"]["status"] is None
        assert row["schema"] == DATASET_SCHEMA
    assert len({r["case_id"] for r in dataset["rows"]}) == len(dataset["rows"])


def test_bench_verdicts_match_the_production_schema():
    """A registry guard, resolved from the real `Literal` rather than from a substring: if
    `TrainingVerdict.status` grows a fourth value the bench is silently no longer measuring the
    judge it claims to measure."""
    from looplab.engine.train_monitor import TrainingVerdict
    annotation = TrainingVerdict.model_fields["status"].annotation
    assert set(typing.get_args(annotation)) == set(VERDICTS)


def test_prompt_splits_round_trip_exactly(dataset):
    """The replay seam. If `render_prompt(row["prompt"])` did not reproduce the recorded messages,
    a "changed prompt" replay would be measuring a prompt nobody can reconstruct."""
    exact = 0
    for row in dataset["rows"]:
        if not row["prompt"]["prompt_split_exact"]:
            assert row.get("messages"), row["case_id"]     # inexact rows MUST keep the original
            continue
        exact += 1
        assert "messages" not in row, row["case_id"]       # and exact rows must not duplicate it
        assert render_prompt(row["prompt"]) == messages_of(row), row["case_id"]
        assert row["prompt"]["digest"], row["case_id"]
    assert exact == len(dataset["rows"])


def test_corpus_limits_travel_with_the_artifact(dataset):
    """A caveat that lives only in a doc is a caveat nobody reading the number sees."""
    assert dataset["header"]["limits"] == CORPUS_LIMITS
    report = score.score_dataset(dataset["rows"][:5], score.recorded_candidate)
    text = score.format_report(report)
    assert CORPUS_LIMITS in text
    assert text.index(CORPUS_LIMITS) < text.index("accuracy")


# ---------------------------------------------------------------- redaction (driven, not pinned)

def test_a_secret_in_a_recorded_log_is_masked_by_the_shared_rule(synthetic_run):
    """Driven, because the preserved corpus happens to contain no credential shape at all — a
    fixpoint check over it would pass whether or not the extractor redacts anything."""
    spans = [json.loads(line) for line in
             (synthetic_run / "spans.jsonl").read_text(encoding="utf-8").splitlines()]
    leak = "api_key=AKIAIOSFODNN7EXAMPLEKEY"
    for span in spans:
        if span["attributes"]["phase_span"] == "p1":
            span["attributes"]["input"][1]["content"] += "\n" + leak
    (synthetic_run / "spans.jsonl").write_text(
        "".join(json.dumps(s) + "\n" for s in spans), encoding="utf-8")
    row = _by_node(extract_run(synthetic_run))[1]
    blob = json.dumps(row)
    assert "AKIAIOSFODNN7EXAMPLEKEY" not in blob
    assert "api_key" in blob                                # the field name survives; the value does not


def test_stored_text_is_a_fixpoint_of_the_shared_redactor(dataset):
    """Applied ONCE, with the same rule persisted tails already use — so re-applying changes
    nothing. This is what says the committed file went through the screen rather than around it."""
    from looplab.core.redact import redact_output_tail
    for row in dataset["rows"][:80]:
        for text in (row["prompt"]["digest"], row["prompt"]["context"], row["recorded"]["reason"]):
            assert redact_output_tail(text, entropy=True) == text, row["case_id"]


# ---------------------------------------------------------------- the scorer

def _always(status):
    return lambda row: status


def test_the_two_agreements_are_never_merged(dataset):
    """Accuracy and churn answer different questions. A candidate that says `broken` every time
    catches every wasted decision AND stops every productive one — high on one, ruinous on the
    other — and no field may average them away."""
    rows = dataset["rows"]
    report = score.score_dataset(rows, _always("broken"))
    assert report.false_stop > 0 and report.missed_stop == 0
    assert report.true_continue == 0
    assert report.recorded_agreement_rate is not None
    assert report.recorded_agreement_rate < 1.0
    assert not any("combined" in f or "overall" in f for f in vars(report))
    fields = set(vars(report))
    assert {"false_stop", "missed_stop", "recorded_agreement"} <= fields


def test_no_label_means_no_accuracy_number_not_a_zero():
    """For a judge whose verdict has no recoverable outcome — the novelty gate rejects an idea, so
    the idea is never run — the honest answer is `None`. A 0.0 would be charted as "always wrong"."""
    unlabelled = [{"case_id": "x", "label": {"label": LABEL_UNKNOWN},
                   "recorded": {"status": "healthy"}, "context": {}, "provenance": {}}]
    report = score.score_dataset(unlabelled, score.recorded_candidate)
    assert report.label_accuracy is None
    assert report.label_coverage == 0
    assert report.recorded_agreement_rate == 1.0          # consistency still measurable, and it is
    assert "NOT accuracy" in score.format_report(report)   # labelled as what it is


def test_budget_exhausted_never_enters_the_accuracy_denominator(dataset):
    """60 of 450 decisions watched a stage that timed out. The compute was wasted, but the judge's
    own system prompt tells it a slow-but-progressing run is `watch`, so charging those as missed
    stops would penalise it for obeying its instructions — and would move the headline."""
    timeouts = [r for r in dataset["rows"] if r["label"]["label"] == LABEL_BUDGET_EXHAUSTED]
    assert timeouts, "the corpus must still exercise this class"
    report = score.score_dataset(timeouts, score.recorded_candidate)
    assert report.label_coverage == 0
    assert (report.true_stop, report.missed_stop, report.false_stop, report.true_continue) == (
        0, 0, 0, 0)
    assert report.by_label[LABEL_BUDGET_EXHAUSTED] == len(timeouts)
    assert LABEL_BUDGET_EXHAUSTED not in score.PRIMARY_LABELS


def test_an_unanswered_row_is_not_scored_as_healthy(dataset):
    """A candidate that fails to parse has NOT said the run is fine."""
    report = score.score_dataset(dataset["rows"][:10], lambda row: None)
    assert report.answered == 0 and len(report.unanswered) == 10
    assert report.label_coverage == 0 and report.recorded_coverage == 0


def test_attempts_are_the_unit_not_nodes(dataset):
    """Keyed by node, a node whose train failed three times and then worked collapses into one
    `wasted` row covering every decision including the ones that watched the attempt that worked."""
    attempts = score.per_attempt_report(dataset["rows"], score.recorded_candidate)
    keys = [k for k in attempts if k.startswith("e5small-dr-unified-v2:n1:")]
    assert len({attempts[k]["label"] for k in keys}) == 2, keys
    totals = score.attempt_totals(attempts)
    assert totals["wasted_attempts"] + totals["productive_attempts"] == len(attempts)


# ---------------------------------------------------------------- the incumbent's pinned baseline

def test_the_incumbent_baseline_is_what_a_candidate_is_read_against(dataset):
    """These are the numbers every candidate is compared to, so they are pinned deliberately.

    A change here is NOT a test to update — it means the corpus moved, and a bench whose baseline
    moved silently is a bench that will be believed while measuring something else. Re-derive the
    dataset, re-argue the numbers, then edit this.
    """
    report = score.score_dataset(dataset["rows"], score.recorded_candidate)
    assert report.rows == 450
    assert report.label_coverage == 354
    assert (report.true_stop, report.false_stop, report.missed_stop, report.true_continue) == (
        53, 5, 101, 195)
    assert report.recorded_agreement_rate == 1.0     # the incumbent replaying itself, by definition
    totals = score.attempt_totals(score.per_attempt_report(dataset["rows"],
                                                           score.recorded_candidate))
    assert (totals["wasted_caught"], totals["wasted_attempts"]) == (7, 27)
    assert (totals["productive_falsely_stopped"], totals["productive_attempts"]) == (3, 49)


def test_the_dataset_regenerates_from_the_runs_it_names(tmp_path, dataset):
    """The other half of "derived, never hand-edited": on a machine that HAS the runs, rebuilding
    the exact sources the header names must reproduce the file byte for byte. Skips elsewhere,
    because `runs/` is not in the repository."""
    import os
    import pathlib
    # The corpus is not in the repository, and in an agent worktree it is not beside the tests
    # either, so the location is overridable rather than assumed.
    root = pathlib.Path(os.environ.get("LOOPLAB_BENCH_RUNS")
                        or pathlib.Path(__file__).resolve().parents[1] / "runs")
    sources = [root / s["run"] for s in dataset["header"]["sources"]]
    if not all((p / "spans.jsonl").exists() for p in sources):
        pytest.skip("runs/ not present — the regeneration half needs the source corpus")
    rebuilt = write_dataset(build_dataset(sources), tmp_path / "rebuilt.jsonl.gz")
    assert gzip.open(rebuilt, "rb").read() == gzip.open(DEFAULT_DATASET, "rb").read()
