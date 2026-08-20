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


def test_the_copied_production_vocabularies_still_agree():
    """Two lists are COPIED from production on purpose — a bench that moves when the thing it
    measures moves cannot detect that it moved. That only works if the copies are checked."""
    assert triage_corpus.TORCH_OOM_MARKERS == triage._TORCH_OOM_MARKERS
    assert triage_score.NEVER_SALVAGED_REASONS == metric_salvage.NEVER_SALVAGED_REASONS
    assert triage_score.FLAG_GUARDED_REASONS <= triage_score.NEVER_SALVAGED_REASONS


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
    assert triage_score.AUTHENTICATED_LABELS == set(triage.AUTHENTICATED_FAILURE_REASONS)
    assert not (triage_score.AUTHENTICATED_LABELS & set(triage.JUDGED_FAILURE_REASONS))
    rows = read_dataset(DEFAULT_DATASET)["rows"]
    report = triage_score.score_dataset(rows, triage_score.recorded_candidate, name="t")
    assert report.authenticated[1] + report.text_read[1] == report.label_coverage
    assert report.authenticated[0] + report.text_read[0] == report.correct
    assert report.text_read[1] > 0 and report.authenticated[1] > 0


def test_the_head_replay_never_reads_the_recorded_reason():
    """The second arm is only honest if nothing it rebuilds comes from the answer it is being
    compared against. Driven: rewriting every recorded reason to nonsense must not move it."""
    rows = read_dataset(DEFAULT_DATASET)["rows"]
    head = triage_score.head_replay_candidate()
    before = [head(r) for r in rows]
    for row in rows:
        row["recorded"]["reason"] = "not-a-reason"
    assert [head(r) for r in rows] == before


def test_the_marker_rule_scores_zero_over_the_durable_tail():
    """The finding the bench exists to be able to state, pinned so it cannot quietly stop being
    checked: `_is_torch_oom` gets NO OOM right over the 500-char record and most of them right once
    the log reads are in front of it. The win is the window, not only the rule."""
    rows = read_dataset(DEFAULT_DATASET)["rows"]
    ooms = [r for r in rows if r["label"]["reason"] == "oom"]
    narrow = triage_score.head_replay_candidate(widened=False)
    wide = triage_score.head_replay_candidate(widened=True)
    assert sum(1 for r in ooms if narrow(r) == "oom") == 0
    assert sum(1 for r in ooms if wide(r) == "oom") > len(ooms) // 2
    assert not [r for r in rows
                if any(m in r["evidence"]["at_classification"]["stderr_tail"]
                       for m in triage_corpus.TORCH_OOM_MARKERS)]


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
