"""Guards for the FAILURE-CLASSIFIER bench (`looplab/judgebench/triage_corpus.py` + `triage_score.py`).

The sibling guard `tests/test_judge_bench.py` states the two jobs; these are the same two applied to
a bench whose label is harder and whose cost model is the point:

1. **The committed dataset is DERIVED and must not be hand-edited.** `test_every_label_rederives`
   recomputes all 122 labels from each row's own stored facts, through the production rule, on a
   machine with no `runs/` — so a label edited in the artefact goes red offline.
2. **The bench must not lie about what it measured.** The label vocabulary stays the classifier's
   own (imported, not copied); the two vocabularies this bench deliberately COPIES from production
   (`_TORCH_OOM_MARKERS`, `NEVER_SALVAGED_REASONS`) are asserted to still agree with their originals,
   because a bench that silently follows the thing it measures cannot detect that it moved; the
   corpus-limits paragraph travels in the header; and `unknown` never enters the accuracy
   denominator.

Everything that can be DRIVEN is driven over a synthetic run directory rather than pinned against
the committed artefact, because the enumeration rule and the label rule are exactly what a substring
pin cannot check. The committed artefact is asserted only for the properties that are ABOUT it: it
re-derives, it is small, its vocabulary is closed, and the three findings it is cited for are in it.
"""
from __future__ import annotations

import json
import os
import time

import pytest

from looplab.core.models import FAILURE_REASONS
from looplab.engine import metric_salvage, triage
from looplab.judgebench import triage_corpus, triage_score
from looplab.judgebench.triage_corpus import (
    CORPUS_LIMITS, DATASET_SCHEMA, DEFAULT_DATASET, HIGH_CONFIDENCE_BASES, LABELS, LABEL_BASES,
    LABEL_UNKNOWN, build_dataset, extract_run, read_dataset, rederive_label, write_dataset)


# ------------------------------------------------------------------- synthetic run (drives the rule)

_OOM_TAIL = ("Traceback (most recent call last):\n  File \"t.py\", line 1, in <module>\n"
             "torch.OutOfMemoryError: CUDA out of memory. Tried to allocate 8.79 GiB\n")
_CRASH_TAIL = ("Traceback (most recent call last):\n  File \"t.py\", line 3, in main\n"
               "NameError: name 'find_optimal_threshold' is not defined\n")
_DIVERGE_TAIL = ("  0%|  | 50/90000 [00:41<17:41:45,  1.41it/s]\n"
                 "‼ LOOPLAB health-check: training DIVERGED — non-finite loss/grad_norm "
                 "reported repeatedly; aborting the stage early.\n")


def _age(path):
    """Backdate an event log past `LIVE_RUN_GRACE_S` — a fixture written this second is, correctly,
    indistinguishable from a run still in flight."""
    old = time.time() - triage_corpus.LIVE_RUN_GRACE_S - 60.0
    os.utime(path, (old, old))


@pytest.fixture()
def synthetic_run(tmp_path):
    """A run holding one of each shape the label rule has to tell apart.

    node 0 — two failed attempts then a terminal that follows its own repair with NO intervening
             eval (so the terminal is the SAME failure and must merge, not become a third row);
    node 1 — a stage-check refusal the operator later ACQUITTED by resetting the node, reusing the
             very same stage output and scoring a healthy metric;
    node 2 — the engine's own diverge sentinel, whose exit code is byte-identical to a kernel OOM.
    """
    run = tmp_path / "synthetic-run"
    run.mkdir()
    (run / "spans.jsonl").write_text("", encoding="utf-8")
    events = [
        # --- node 0: crash -> oom -> terminal-on-the-same-failure
        {"seq": 1, "ts": 100.0, "type": "stage_finished",
         "data": {"node_id": 0, "name": "train", "status": "fail", "exit_code": 1, "seconds": 9.0}},
        {"seq": 2, "ts": 110.0, "type": "node_repaired",
         "data": {"node_id": 0, "attempt": 1, "reason": "crash", "error_in": _CRASH_TAIL,
                  "triage_action": "repair", "rationale": "mechanical NameError"}},
        {"seq": 3, "ts": 200.0, "type": "stage_finished",
         "data": {"node_id": 0, "name": "train", "status": "fail", "exit_code": 1, "seconds": 8.0}},
        {"seq": 4, "ts": 210.0, "type": "node_repaired",
         "data": {"node_id": 0, "attempt": 2, "reason": "crash", "error_in": _OOM_TAIL,
                  "triage_action": "repair", "rationale": "genuine CUDA OOM"}},
        {"seq": 5, "ts": 211.0, "type": "node_failed",
         "data": {"node_id": 0, "reason": "crash", "error": _OOM_TAIL, "failed_stage": "train"}},
        # --- node 1: a check refusal the artefact itself later refuted
        {"seq": 6, "ts": 300.0, "type": "stage_finished",
         "data": {"node_id": 1, "name": "train", "status": "check_failed", "exit_code": 0,
                  "seconds": 2070.0, "concern": "Loss stagnant at 13.3, indicating no learning."}},
        {"seq": 7, "ts": 301.0, "type": "node_failed",
         "data": {"node_id": 1, "reason": "no_metric", "failed_stage": "train",
                  "error": "stage 'train' failed verification: Loss stagnant at 13.3, indicating "
                           "no learning."}},
        {"seq": 8, "ts": 900.0, "type": "stage_finished",
         "data": {"node_id": 1, "name": "train", "status": "reused", "exit_code": 0, "seconds": 0.0}},
        {"seq": 9, "ts": 901.0, "type": "stage_finished",
         "data": {"node_id": 1, "name": "score", "status": "ok", "exit_code": 0, "seconds": 67.0}},
        {"seq": 10, "ts": 902.0, "type": "node_evaluated", "data": {"node_id": 1, "metric": 0.805}},
        # --- node 2: the watchdog kill that looks exactly like a kernel OOM
        {"seq": 11, "ts": 400.0, "type": "stage_finished",
         "data": {"node_id": 2, "name": "train", "status": "fail", "exit_code": -9, "seconds": 44.0}},
        {"seq": 12, "ts": 410.0, "type": "node_repaired",
         "data": {"node_id": 2, "attempt": 1, "reason": "oom", "error_in": _DIVERGE_TAIL,
                  "triage_action": "repair", "rationale": "halve the batch"}},
        # a node the card gate replaced: never run against its own idea, never a classification
        {"seq": 13, "ts": 500.0, "type": "node_failed",
         "data": {"node_id": 3, "reason": "superseded", "error": "superseded by Card freshness gate"}},
        # the run's best, so the reuse-score gate has something to be a fraction OF
        {"seq": 14, "ts": 600.0, "type": "node_evaluated", "data": {"node_id": 4, "metric": 0.8835}},
    ]
    (run / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events),
                                      encoding="utf-8")
    _age(run / "events.jsonl")
    return run


def _by_case(rows):
    return {(r["provenance"]["node_id"], r["provenance"]["attempt"]): r for r in rows}


# ------------------------------------------------------------------------------- the enumeration

def test_a_terminal_that_follows_its_own_repair_is_not_a_second_failure(synthetic_run):
    """Node 0 failed TWICE and terminalized on the second. Three rows would count one eval twice.

    The merge test is "was there an eval in between", never "is the error text the same": four of
    `e5small-dr-unified-v3` node 2's attempts produced a byte-identical artifact-contract message,
    and merging on text would have collapsed four distinct classifications into one.
    """
    rows = _by_case(extract_run(synthetic_run))
    assert sorted(rows) == [(0, 1), (0, 2), (1, 1), (2, 1)]
    assert rows[(0, 2)]["provenance"]["terminal"] is True
    assert rows[(0, 1)]["provenance"]["terminal"] is False


def test_superseded_is_not_a_classification(synthetic_run):
    """The card gate replaced node 3 before it ran. Nothing about an eval was read, so there is no
    reading to score — and a corpus that counted it would be scoring the gate, not the classifier."""
    rows = extract_run(synthetic_run)
    assert not [r for r in rows if r["provenance"]["node_id"] == 3]
    assert "superseded" in triage_corpus.NON_CLASSIFICATION_REASONS


# ------------------------------------------------------------------------------- the label rule

def test_the_recorded_reason_is_never_the_label(synthetic_run):
    """The whole design constraint in one assertion: on every row where the incumbent was WRONG the
    label disagrees with it, and it disagrees because of a fact the incumbent did not author."""
    rows = _by_case(extract_run(synthetic_run))
    oom = rows[(0, 2)]
    assert oom["recorded"]["reason"] == "crash" and oom["label"]["reason"] == "oom"
    assert oom["label"]["basis"] == "oom_marker_in_evidence"
    diverged = rows[(2, 1)]
    assert diverged["recorded"]["reason"] == "oom" and diverged["label"]["reason"] == "diverged"
    assert diverged["label"]["basis"] == "watchdog_sentinel"


def test_a_reused_stage_that_later_scored_acquits_the_stage(synthetic_run):
    """Node 1's checker called a converged training dead. The operator reset the node, the SAME
    train output came back `reused` (seconds 0.0 — nothing was recomputed) and it scored 0.805
    against a 0.8835 best. So the stage did not fail; the mechanism was the contract check."""
    row = _by_case(extract_run(synthetic_run))[(1, 1)]
    assert row["recorded"]["reason"] == "no_metric"
    assert row["label"]["reason"] == "check_failed"
    assert row["label"]["basis"] == "reused_stage_later_scored"
    assert row["label"]["reused_stage_later_scored"]["metric"] == pytest.approx(0.805)


def test_a_dead_reuse_score_does_not_acquit_anything(synthetic_run, tmp_path):
    """The gate is a FRACTION of the run's best, not the mere existence of a later metric: a node
    re-scored at 0.02 was not acquitted by that number and calling it healthy would be the bench
    inventing its own evidence."""
    run = tmp_path / "dead"
    run.mkdir()
    (run / "spans.jsonl").write_text("", encoding="utf-8")
    events = json.loads(json.dumps([json.loads(line) for line
                                    in (synthetic_run / "events.jsonl").read_text().splitlines()]))
    for event in events:
        if event["type"] == "node_evaluated" and event["data"]["node_id"] == 1:
            event["data"]["metric"] = 0.02
    (run / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events),
                                      encoding="utf-8")
    _age(run / "events.jsonl")
    row = _by_case(extract_run(run))[(1, 1)]
    assert row["label"]["reused_stage_later_scored"] is None
    assert row["label"]["basis"] != "reused_stage_later_scored"


def test_no_label_rests_on_the_triage_agents_prose():
    """The rule that read the agent's rationale was written, run and DELETED — it fired once in 122
    rows and was wrong there. Nothing may quietly put it back: no basis names a rationale, and the
    committed artefact's `oom` labels all rest on strings torch itself printed."""
    assert not [b for b in LABEL_BASES if "rationale" in b]
    rows = read_dataset(DEFAULT_DATASET)["rows"]
    ooms = [r for r in rows if r["label"]["reason"] == "oom"]
    assert ooms and all(r["label"]["basis"] in ("oom_marker_in_evidence",
                                                "allocator_message_in_stderr") for r in ooms)


def test_a_cause_the_vocabulary_cannot_name_is_annotated_not_labelled():
    """`Idea.params` is a PROPOSAL. On 4 rows the stage's own parser REFUSED the parameters the
    engine substituted, so the hyperparameters the node existed to test never ran — and `crash` is
    still the honest classification, because no member of `FAILURE_REASONS` says that.

    Recorded beside the label rather than as one: inventing a thirteenth reason would break the
    single property that lets this corpus claim the classifier is wrong at all, which is that its
    vocabulary is the classifier's own. Measured silent twin (2026-08-20): 457 comparisons of
    declared params against the node's `config.yaml`, 41 diverged, and those produce no failure at
    all so they cannot appear here.
    """
    rows = read_dataset(DEFAULT_DATASET)["rows"]
    rejected = [r for r in rows if r["cause_notes"]["params_rejected_by_stage"]]
    assert rejected, "the corpus lost the params-rejection rows"
    assert all(r["label"]["reason"] == "crash" for r in rejected)
    assert all(a.startswith("--") for r in rejected
               for a in r["cause_notes"]["params_rejected_by_stage"])
    # And the annotation is genuinely outside the label: nothing scores on it.
    assert "params" not in " ".join(LABEL_BASES)


# --------------------------------------------------------------------------- the committed artefact

def test_every_label_rederives():
    """The artefact is DERIVED. Re-deriving each label from the row's own stored facts is what makes
    a hand-edited one red on a machine that has never seen the operator's disk."""
    dataset = read_dataset(DEFAULT_DATASET)
    drifted = [r["case_id"] for r in dataset["rows"]
               if rederive_label(r)["reason"] != r["label"]["reason"]]
    assert not drifted, "labels that do not re-derive: %s" % drifted[:5]


def test_the_vocabulary_is_the_classifiers_own():
    """A bench with its own private list of reasons can only ever report disagreement with a stale
    list. The label vocabulary IS `FAILURE_REASONS` plus `unknown`, and every label used is in it."""
    assert set(LABELS) == set(FAILURE_REASONS) | {LABEL_UNKNOWN}
    dataset = read_dataset(DEFAULT_DATASET)
    used = {r["label"]["reason"] for r in dataset["rows"]}
    assert used <= set(LABELS)
    assert {r["label"]["basis"] for r in dataset["rows"]} <= set(LABEL_BASES)


def test_the_live_arms_vocabularies_still_agree_with_production():
    """The bench has TWO kinds of reference to production and they are checked differently.

    This test is for the LIVE half only — the names an arm that scores today's classifier must
    follow, so a drift between the bench's idea of the ownership split and the real one goes red
    here rather than being discovered as a wrong number. It was originally pointed at the marker
    list and the authenticated tuple; both of those turned out to belong to the HISTORICAL half,
    which is why they are now frozen and are asserted by
    `test_the_historical_arm_reads_no_production_name` instead. A good test that was aimed at the
    wrong half.
    """
    live = triage_score.live_ownership_split()
    assert live["shape"] in (triage_score.LIVE_SHAPE_OWNERSHIP,
                             triage_score.LIVE_SHAPE_AUTHENTICATED)
    # Asserted against whichever partition production actually exposes. Not a skip and not a
    # fallback that hides a third shape: an unknown shape fails on the line above, and each known
    # one is checked against its own source of truth below.
    if live["shape"] == triage_score.LIVE_SHAPE_OWNERSHIP:
        from looplab.engine import failure_diagnosis as fd
        assert live["diagnosable"] == frozenset(fd.DIAGNOSABLE_ENGINE_REASONS)
        assert live["engine_final"] == frozenset(fd.ENGINE_FINAL_REASONS)
        assert live["answerable"] == frozenset(fd.DIAGNOSED_FAILURE_REASONS)
        assert live["unclassified"] == fd.UNCLASSIFIED_REASON
        assert live["unclassified"] in LABELS
    else:
        assert live["diagnosable"] == frozenset(triage.JUDGED_FAILURE_REASONS)
        assert live["engine_final"] == frozenset(triage.AUTHENTICATED_FAILURE_REASONS)
        assert live["unclassified"] is None
    assert triage_score.NEVER_SALVAGED_REASONS == metric_salvage.NEVER_SALVAGED_REASONS
    assert triage_score.FLAG_GUARDED_REASONS <= triage_score.NEVER_SALVAGED_REASONS
    # Whatever production may ANSWER must be sayable in the corpus's label vocabulary, or the live
    # arm is scoring answers the bench has no truth for.
    assert live["answerable"] <= set(LABELS)
    assert live["engine_final"] <= set(LABELS)


def test_the_historical_arm_reads_no_production_name():
    """The record of how the OLD decider scored may not move when the classifier does.

    This is the defect the 2026-08-20 ownership split exposed: the "incumbent replayed" arm imported
    the live `_failure_reason`, so the hour the classifier changed, the arm began measuring a
    different program while still being labelled the incumbent — and two of its three assertions
    went red as `AttributeError`, which is the lucky version. The unlucky version is a number that
    quietly means something else.

    Driven rather than asserted about: monkeypatching production's classifier to a constant must not
    move the frozen arm by a single row, and must move the live one.
    """
    rows = read_dataset(DEFAULT_DATASET)["rows"]
    frozen = triage_score.frozen_replay_candidate()
    before = [frozen(r) for r in rows]
    live_before = [triage_score.live_engine_candidate()(r) for r in rows]

    original = triage._failure_reason
    try:
        triage._failure_reason = lambda res: "setup"
        assert [frozen(r) for r in rows] == before, "the frozen arm followed production"
        moved = [triage_score.live_engine_candidate()(r) for r in rows]
        assert moved != live_before, "the live arm did NOT follow production"
    finally:
        triage._failure_reason = original
    # And the frozen marker list is the bench's OWN literal, not an import that tracks production
    # or vanishes with it. Driven both ways rather than asserted about, because the interesting
    # failure is the silent one: production had this name when the corpus was cut, deleted it hours
    # later, and could grow it back tomorrow with different contents — none of which may move a
    # label. (`hasattr` is deliberately not the test: whether production HAS the name is production's
    # business; whether the bench FOLLOWS it is the bench's.)
    assert triage_score._FROZEN_TORCH_OOM_MARKERS[0] == "OutOfMemoryError"
    assert triage_corpus.TORCH_OOM_MARKERS == triage_score._FROZEN_TORCH_OOM_MARKERS
    had = getattr(triage, "_TORCH_OOM_MARKERS", None)
    try:
        triage._TORCH_OOM_MARKERS = ("NOT AN OOM MARKER",)
        assert triage_corpus.TORCH_OOM_MARKERS == triage_score._FROZEN_TORCH_OOM_MARKERS
        assert [frozen(r) for r in rows] == before
        # …and the labels the corpus ships do not move either, which is the thing that matters.
        assert not [r for r in rows
                    if triage_corpus.rederive_label(r)["reason"] != r["label"]["reason"]]
    finally:
        if had is None:
            delattr(triage, "_TORCH_OOM_MARKERS")
        else:
            triage._TORCH_OOM_MARKERS = had


def test_the_header_carries_the_caveat_and_the_counts():
    """A caveat that lives only in a doc is a caveat nobody reading the number sees."""
    header = read_dataset(DEFAULT_DATASET)["header"]
    assert header["schema"] == DATASET_SCHEMA
    assert header["limits"] == CORPUS_LIMITS
    assert "not accuracy" in CORPUS_LIMITS.lower() or "NOT THE TRUTH" in CORPUS_LIMITS
    assert header["rows"] == header["labelled"] + header["unlabelled"]
    assert header["unlabelled"] > 0, ("a corpus with nothing unlabelled has either solved every "
                                      "case or guessed one, and the second is far more likely")
    # The event-log position each run was read at, so a rebuild over a run that has since grown is
    # a visible diff in the header rather than a silent one in the rows.
    assert all("last_seq" in source for source in header["sources"])


def test_a_run_still_being_appended_to_is_refused(tmp_path, synthetic_run):
    """Every label rests on what happened NEXT, and a run in flight has not produced its own next.

    Measured while this corpus was built: `runs/e5small-dr-unified-v4` grew from 292 to 853 events
    between two extractions minutes apart and silently added one unlabellable row to the second, so
    the committed artefact would have stopped being reproducible with nothing saying why.
    """
    live = tmp_path / "live-run"
    live.mkdir()
    (live / "spans.jsonl").write_text("", encoding="utf-8")
    (live / "events.jsonl").write_text((synthetic_run / "events.jsonl").read_text(),
                                       encoding="utf-8")
    os.utime(live / "events.jsonl", (time.time(), time.time()))
    with pytest.raises(triage_corpus.LiveRunRefused):
        extract_run(live)
    dataset = build_dataset([live, synthetic_run])
    assert dataset["header"]["skipped_live_runs"] == ["live-run"]
    assert [s["run"] for s in dataset["header"]["sources"]] == ["synthetic-run"]


def test_a_rebuild_is_byte_identical(tmp_path, synthetic_run):
    """Sorted rows, sorted keys and `mtime=0`, so the committed file's diff is only ever real change
    and a reviewer is never asked to read a gzip timestamp."""
    dataset = build_dataset([synthetic_run])
    first = write_dataset(dataset, tmp_path / "a.jsonl.gz").read_bytes()
    second = write_dataset(build_dataset([synthetic_run]), tmp_path / "b.jsonl.gz").read_bytes()
    assert first == second


# ------------------------------------------------------------------------------------ the scoring

def test_unknown_never_enters_the_accuracy_denominator():
    """The 4 rows whose evidence does not say are EXCLUDED, not guessed. A bench that guessed them
    would report a number about its own guesses."""
    rows = read_dataset(DEFAULT_DATASET)["rows"]
    report = triage_score.score_dataset(rows, triage_score.recorded_candidate, name="t")
    unknown = sum(1 for r in rows if r["label"]["reason"] == LABEL_UNKNOWN)
    assert unknown > 0
    assert report.label_coverage == len(rows) - unknown
    assert report.unlabelled == unknown


def test_agreement_and_accuracy_are_separate_fields():
    """The recorded arm agrees with itself perfectly and is right about two thirds of the time. A
    bench that let those two numbers merge would report 100% for the incumbent."""
    rows = read_dataset(DEFAULT_DATASET)["rows"]
    report = triage_score.score_dataset(rows, triage_score.recorded_candidate, name="recorded")
    assert report.agreement_with_recorded == report.agreement_denominator
    assert report.accuracy < 0.7
    assert not [f for f in vars(report) if "combined" in f or "overall" in f]


def test_the_cost_model_does_not_charge_a_flag_guarded_salvage_error():
    """`timeout` and `diverged` are in `NEVER_SALVAGED_REASONS`, and `metric_salvage` ALSO re-reads
    `res.timed_out` / `res.diverged` one line below the reason test — so a wrong label on either
    cannot open the salvage gate. Charging it would be this bench inventing a cost, and it did."""
    assert triage_score.cost_of("oom", "diverged") == "opposed_directive"
    assert triage_score.cost_of("no_metric", "timeout") == "generic_for_specific"
    # The direction that CAN move a champion is still charged, at the top of the ordering.
    assert triage_score.cost_of("crash", "drift") == "admits_refused_metric"
    assert triage_score.cost_of("setup", "crash") == "suppresses_real_metric"


def test_the_two_halves_of_the_vocabulary_are_scored_apart():
    """`check_failed` is decided by the same `res.stages[-1]["status"]` on BOTH sides, so a headline
    that mixed it with `oom` would credit a branch EXISTING as if it were a reading improving. The
    text-read half is the number an agentic diagnostician should be judged on."""
    # FROZEN, and pinned as frozen: this partition is a property of the LABELS, which were cut once
    # and do not move. Production's split moved on 2026-08-20 (`check_failed` crossed to the
    # diagnosable side) and following it would have made two arms measured months apart
    # incomparable, which is the opposite of what a bench is for.
    assert triage_score.AUTHENTICATED_LABELS is triage_score.HISTORICAL_AUTHENTICATED_REASONS
    assert triage_score.AUTHENTICATED_LABELS == {
        "drift", "timeout", "setup", "diverged", "stalled",
        "needs_failed", "expect_failed", "check_failed"}
    assert triage_score.AUTHENTICATED_LABELS <= set(LABELS)
    rows = read_dataset(DEFAULT_DATASET)["rows"]
    report = triage_score.score_dataset(rows, triage_score.recorded_candidate, name="t")
    assert report.authenticated[1] + report.text_read[1] == report.label_coverage
    assert report.authenticated[0] + report.text_read[0] == report.correct
    assert report.text_read[1] > 0 and report.authenticated[1] > 0


def test_the_score_can_fall():
    """A bench whose number cannot go DOWN measures nothing.

    Six mechanisms in this repo were found shipping a vacuous green in one day, all the same shape:
    the guard named the MECHANISM instead of the PROPERTY, so it passed over an empty set. The
    bench equivalent is a score that is high whatever the candidate answers, and the only way to
    know is to drive a candidate that is deliberately wrong and watch the number collapse.
    """
    rows = read_dataset(DEFAULT_DATASET)["rows"]

    def constant(reason):
        return lambda row: reason

    oracle = triage_score.score_dataset(
        rows, lambda row: row["label"]["reason"], name="oracle")
    assert oracle.accuracy == 1.0                       # the ceiling is reachable
    # `setup` is in the vocabulary and labels nothing here, so a candidate that always says it is
    # wrong on every single row. If THAT scores above zero the denominator is not what it claims.
    assert triage_score.score_dataset(rows, constant("setup"), name="s").accuracy == 0.0
    # And the majority class — the answer a lazy classifier gives — must not look good.
    crash_only = triage_score.score_dataset(rows, constant("crash"), name="c")
    assert crash_only.accuracy < 0.5
    incumbent = triage_score.score_dataset(rows, triage_score.recorded_candidate, name="r")
    assert crash_only.accuracy < incumbent.accuracy < oracle.accuracy


def test_a_label_that_reads_the_classifiers_own_field_is_quarantined():
    """The one place a row's label CAN'T be wrong, named and kept out of the number that matters.

    `reused_stage_later_scored` proves the stage did not fail and then names the MECHANISM from
    `res.stages[-1]["status"]` — the very field `_failure_reason`'s contract branches read. A
    classifier with that branch is right on those rows by construction. That is real (it is how ten
    `no_metric` terminals got their right answer) and it is not evidence about diagnosis, so every
    such row must land in the AUTHENTICATED half, where the report shows it apart. If one ever
    leaked into the text-read half it would inflate the exact number a diagnostician is judged on.
    """
    rows = read_dataset(DEFAULT_DATASET)["rows"]
    shared_field = [r for r in rows
                    if r["label"]["reason"] != LABEL_UNKNOWN
                    and r["label"]["reason"] == r["evidence"]["at_classification"]
                                                  ["failed_stage_status"]]
    assert shared_field, "no row exercises the shared-field case — the quarantine is untested"
    assert all(r["label"]["reason"] in triage_score.AUTHENTICATED_LABELS for r in shared_field)
    # And the half that IS about diagnosis is the larger one, so the headline is not mostly this.
    report = triage_score.score_dataset(rows, triage_score.recorded_candidate, name="r")
    assert report.text_read[1] > report.authenticated[1]


def test_the_live_arm_scores_todays_classifier_and_says_what_it_is_not_scoring():
    """The arm that makes the bench useful going forward, and the honesty that has to ride with it.

    Today's classifier is two things: `_failure_reason`, which decides structurally from what the
    engine caused, ran or measured, and a diagnostician that costs a model call and cannot run
    inside a test. A bench that scored the first and called the number "the new classifier" would be
    the same over-claim this file exists to prevent — so the live arm counts the handoff and the
    report refuses to fold it into the accuracy line.
    """
    rows = read_dataset(DEFAULT_DATASET)["rows"]
    report = triage_score.score_dataset(rows, triage_score.live_engine_candidate(),
                                        name="live", live=True)
    assert report.label_coverage == 118
    # Every answer it gives is either engine-final or an explicit nomination. Nothing else.
    answered = {a for (_t, a) in report.confusion} - {"<no answer>"}
    assert answered <= (set(triage_score.LIVE_ENGINE_FINAL_REASONS)
                        | set(triage_score.LIVE_DIAGNOSABLE_REASONS)
                        | set(LABELS))
    # The handoff is real and is the larger part of the corpus, so a number that ignored it would be
    # describing a minority of the rows.
    handed_on, total = report.diagnosable_handoff[1], report.label_coverage
    assert handed_on > total // 2
    assert report.diagnosable_handoff[0] < handed_on      # there IS headroom to win
    # A ceiling that ignores what the diagnostician is FORBIDDEN to say is a promise the design
    # cannot keep. Its answer vocabulary is closed and narrower than the label vocabulary on
    # purpose — a model may not assert that an engine mechanism it cannot observe fired — so the
    # report states the unwinnable rows and computes the REACHABLE ceiling, not the arithmetic one.
    assert triage_score.LIVE_ANSWERABLE_REASONS < set(LABELS), (
        "the diagnostician may say everything the corpus can label — then nothing is out of reach "
        "and the reachable ceiling is not measuring anything")
    # DRIVEN, because how many rows are unwinnable TODAY depends on which partition ships and a
    # test that asserted a number would be green for the wrong reason on the other side of a merge.
    # The property is the mechanism: a row handed on whose truth the diagnostician may not give is
    # counted, and the reachable ceiling drops by exactly that many.
    forbidden = sorted(set(LABELS) - triage_score.LIVE_ANSWERABLE_REASONS - {LABEL_UNKNOWN})
    assert forbidden, "nothing is forbidden; see above"
    target = forbidden[0]
    marked = [r for r in rows if r["label"]["reason"] == target]
    assert marked, "no row carries %r, so the drive below proves nothing" % target
    handed = sorted(triage_score.LIVE_DIAGNOSABLE_REASONS)[0]
    driven = triage_score.score_dataset(
        rows, lambda row: handed if row["label"]["reason"] == target else row["label"]["reason"],
        name="driven", live=True)
    assert driven.unreachable_by_diagnosis == len(marked)
    text_driven = triage_score.format_report(driven)
    reachable = driven.correct + (driven.diagnosable_handoff[1] - driven.diagnosable_handoff[0]
                                  ) - driven.unreachable_by_diagnosis
    assert "%d/%d" % (reachable, driven.label_coverage) in text_driven

    text = triage_score.format_report(report)
    assert "REACHABLE CEILING" in text
    assert "HANDED TO THE DIAGNOSTICIAN" in text
    assert "NOT part of any accuracy claim" in text
    # The report must NAME which of production's two partitions it scored: the same number means
    # different things under them, and a reader six months from now has no other way to tell.
    assert repr(triage_score.LIVE_SHAPE) in text
    # And the block does NOT appear on an arm that is not scoring the live classifier, where the
    # phrase "handed to the diagnostician" would be a claim about a program that never ran.
    frozen = triage_score.score_dataset(rows, triage_score.frozen_replay_candidate(), name="f")
    assert "HANDED TO THE DIAGNOSTICIAN" not in triage_score.format_report(frozen)


def test_the_limits_name_the_repaired_defect_in_the_incumbent():
    """A corpus that measures a decider containing a since-fixed bug must SAY so, in the artefact.

    Ten of the sixteen `rubertlite-dense-retrieval` terminals were condemned by a stage checker
    reading only `run.out[-4000:]`; the trajectory veto widened that window, so those nodes would
    not be condemned today. No label moves — what acquits them is the operator's own
    reused-and-scored re-run, not the checker's later repair — but a reader who took "10 of 16 were
    false refusals" as a property of the checker that SHIPS would be quoting a historical rate as a
    current one. The caveat rides in the header, which is what the report prints, so it cannot be
    separated from the number.
    """
    header = read_dataset(DEFAULT_DATASET)["header"]
    limits = header["limits"]
    assert limits == CORPUS_LIMITS
    assert "DEFECT SINCE REPAIRED" in limits
    assert "4,000" in limits and "trajectory veto" in limits
    assert "NOT ONE LABEL MOVES" in limits
    # And it is printed, not merely stored.
    rows = read_dataset(DEFAULT_DATASET)["rows"]
    report = triage_score.score_dataset(rows, triage_score.recorded_candidate, name="r")
    assert "DEFECT SINCE REPAIRED" in triage_score.format_report(report, limits=limits)


def test_no_row_uses_a_label_the_classifier_cannot_produce():
    """The load-bearing half of "the vocabulary is the classifier's own", asserted on the ARTEFACT.

    `labels` in the header is a fact about the BUILD TREE and is allowed to lag: the vocabulary
    grew a member (`unclassified`) between two of this corpus's rebuilds, and a test demanding
    equality would turn every such addition into a red merge that can only be cleared by rebuilding
    a dataset whose ROWS did not change. That is the "bench follows the thing it measures" defect
    one level down, and it is not worth reintroducing to catch a stale header field.

    What must never lag is the direction that can make the corpus incoherent: a row labelled with a
    reason the classifier cannot produce would be scoring candidates against an answer that does not
    exist. That is what this asserts, and it is asserted against the live vocabulary.
    """
    dataset = read_dataset(DEFAULT_DATASET)
    used = {r["label"]["reason"] for r in dataset["rows"]}
    assert used <= set(LABELS), (
        "the corpus labels rows with reasons this tree cannot produce: %s"
        % sorted(used - set(LABELS)))
    # The header's own vocabulary must at least cover what the rows use, or the artefact
    # contradicts itself without needing the tree at all.
    assert used <= set(dataset["header"]["labels"])


def test_the_frozen_vocabulary_travels_in_the_artefact():
    """A snapshot that lives only in a module is a snapshot the next reader has to go find."""
    header = read_dataset(DEFAULT_DATASET)["header"]
    frozen = header["frozen_vocabulary"]
    assert frozen["torch_oom_markers"] == list(triage_corpus.TORCH_OOM_MARKERS)
    assert set(frozen["authenticated_reasons"]) == triage_score.HISTORICAL_AUTHENTICATED_REASONS
    assert frozen["as_of"] == "2026-08-20"


def test_the_head_replay_never_reads_the_recorded_reason():
    """The second arm is only honest if nothing it rebuilds comes from the answer it is being
    compared against. Driven: rewriting every recorded reason to nonsense must not move it."""
    rows = read_dataset(DEFAULT_DATASET)["rows"]
    head = triage_score.head_replay_candidate()
    before = [head(r) for r in rows]
    for row in rows:
        row["recorded"]["reason"] = "not-a-reason"
    assert [head(r) for r in rows] == before


def test_the_durable_record_cannot_support_an_oom_determination():
    """A property of the RECORD, not of any classifier — which is what it was really measuring.

    It used to say "`_is_torch_oom` scores 0 of 23 over the durable tail". That rule is deleted:
    `oom` is answer-only in production now, precisely BECAUSE both of its producers read the
    failure's own text. So the classifier-shaped half of the finding has no subject any more, and
    the half that survives is the one that was always load-bearing: **not one of the 122 preserved
    stderr tails contains an allocator marker at all.** `node_repaired.error_in` is 500 characters
    and `res.stderr` was clamped at 64,000, so what reached disk cannot decide an OOM by any rule,
    present or future. Every `oom` label in this corpus therefore rests on evidence OUTSIDE
    `at_classification` — the triage agent's own log reads, or a stage log paired to the attempt.

    That is the standing consequence for anyone replaying this corpus: a candidate handed only
    `evidence.at_classification` is strictly worse informed than the engine was, and it is filed as
    the durable-record task (`the record does not preserve what the decider saw`).
    """
    rows = read_dataset(DEFAULT_DATASET)["rows"]
    tails = [r["evidence"]["at_classification"]["stderr_tail"] for r in rows]
    assert len(tails) == 122
    assert not [t for t in tails
                if any(m in t for m in triage_corpus.TORCH_OOM_MARKERS)], (
        "a preserved tail now carries an allocator marker; the finding has changed and the "
        "durable-record task may be closable")

    ooms = [r for r in rows if r["label"]["reason"] == "oom"]
    assert len(ooms) == 23
    # Every one of them is labelled from something the durable tail does not hold.
    outside = [r for r in ooms
               if r["label"]["basis"] == "oom_marker_in_evidence"
               or r["label"]["basis"] == "allocator_message_in_stderr"]
    assert len(outside) == len(ooms)
    from_the_tail_alone = [r for r in ooms if r["label"]["basis"] == "oom_marker_in_evidence"
                           and any(m in r["evidence"]["at_classification"]["stderr_tail"]
                                   for m in triage_corpus.TORCH_OOM_MARKERS)]
    assert not from_the_tail_alone

    # And the consequence, driven: the widened evidence recovers OOMs the durable tail cannot, on
    # the frozen incumbent, which is the arm whose 0-of-23 measurement argued for deleting the rule.
    narrow = triage_score.frozen_replay_candidate(widened=False)
    wide = triage_score.frozen_replay_candidate(widened=True)
    assert sum(1 for r in ooms if narrow(r) == "oom") == 0
    assert sum(1 for r in ooms if wide(r) == "oom") > len(ooms) // 2


def test_an_unpaired_stage_log_is_never_stored(tmp_path):
    """A workspace holds ONE log per stage and every re-run truncates it, so an unpaired log is a
    LATER attempt's output. Attaching it to this row would be fabrication of exactly the kind this
    bench exists to prevent, and the rows that have none say so rather than borrowing one."""
    rows = read_dataset(DEFAULT_DATASET)["rows"]
    logs = [r["evidence"]["on_demand"]["stage_log"] for r in rows]
    present = [log for log in logs if log]
    assert present, "no run in the corpus preserved a stage log — the pairing rule is untested"
    assert all(log["tail"] is None for log in present if not log["paired_to_this_attempt"])
    assert any(log["tail"] for log in present)


def test_high_confidence_bases_are_a_subset_of_the_declared_ones():
    """A basis that is 'high confidence' but not in the closed list would be a label nobody
    declared."""
    assert HIGH_CONFIDENCE_BASES <= set(LABEL_BASES)
    rows = read_dataset(DEFAULT_DATASET)["rows"]
    report = triage_score.score_dataset(rows, triage_score.recorded_candidate, name="t",
                                        high_confidence_only=True)
    assert 0 < report.label_coverage <= report.label_coverage + report.unlabelled
