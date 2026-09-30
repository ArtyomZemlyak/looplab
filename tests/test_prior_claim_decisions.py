"""The operator's claim decisions reach the passive prior (doc 69 §5.4, item 69.21).

`minionerec-backbones-v10`: the operator rejected claims (`claim-decide --reject`), and the claims
surface showed the rejection — while the passive cross-run prior, the text every role's prompt
carries, read `lessons.jsonl` alone and kept serving the rejected lesson to every later proposal.

Under `Settings.lesson_prior_claim_decisions` the prior withholds a lesson whose claim group the
operator rejected, decided by the claims surface's OWN grouping and decision lookup
(`engine/claims_assessments.py::operator_rejected_claim_uids`), says how many in the prompt and the
`prior_injected` receipt, discloses an unreadable decision ledger instead of guessing, and a decision
made mid-run counts as a change for the refresh (`engine/lessons.py::lessons_store_stamp`).
"""
from __future__ import annotations

import json

from looplab.core.config import LEGACY_CONFIG_SNAPSHOT_DEFAULTS, Settings, settings_from_snapshot
from looplab.engine.claims import (
    claim_assessments, lesson_claim_uid, load_claim_decisions, operator_rejected_claim_uids,
    record_claim_decision)
from looplab.engine.lesson_hygiene import lesson_id
from looplab.engine.options import EngineOptions
from looplab.events.types import EV_PRIOR_INJECTED
from tests.factories import make_engine

REJECTED = "Warm-starting the tokenizer from the base checkpoint improves recall"
KEPT = "Cosine learning-rate decay with a short warmup improves recall"
TASK = "toy_quadratic"


def _lesson(statement, **extra):
    return {"statement": statement, "outcome": "supported", "task_id": TASK, "direction": "min",
            "role": "researcher", "confidence": 0.7, "evidence_count": 2, "evidence": [0],
            "fingerprint": ["kind:quadratic", "dir:min"], "run_id": "other-run", **extra}


def _memory(tmp_path, *rows):
    mem = tmp_path / "mem"
    mem.mkdir(exist_ok=True)
    (mem / "lessons.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows or (_lesson(REJECTED), _lesson(KEPT))),
        encoding="utf-8")
    return mem


def _prior(tmp_path, mem, *, on=True, role="researcher"):
    eng = make_engine(tmp_path / ("run-on" if on else "run-off"), n_seeds=1, max_nodes=1,
                      reflection_priors=True, memory_dir=str(mem),
                      lesson_prior_claim_decisions=on)
    return eng, eng.lessons._pick_role_prior(eng.lessons._scan_prior_context(None, None), role)


# ------------------------------------------------------------------------------------ the prior
def test_a_rejected_claim_leaves_the_prior_and_the_prompt_says_so(tmp_path):
    """MUTATIONS: skip the filter; count rows that were never candidates; drop the disclosure."""
    mem = _memory(tmp_path)
    record_claim_decision(str(mem), statement=REJECTED, decision="rejected", scope=TASK)
    _eng, (text, receipt) = _prior(tmp_path, mem)
    assert REJECTED not in text and KEPT in text
    assert "[MEMORY_OPERATOR_REJECTED: 1 lesson(s) whose claim the operator rejected are not shown.]" in text
    assert receipt["operator_rejected"] == 1
    assert [row["id"] for row in receipt["rows"]] == [lesson_id(KEPT)]
    # OFF: the historical prior, byte for byte — the rejected lesson and no disclosure.
    _eng, (off, off_receipt) = _prior(tmp_path, mem, on=False)
    assert REJECTED in off and "MEMORY_OPERATOR_REJECTED" not in off
    assert "operator_rejected" not in off_receipt


def test_the_count_is_of_this_prompt_s_candidates_not_of_the_store(tmp_path):
    """A rejected lesson for the OTHER role was never going to be weighed for this one, so it is not
    counted as withheld here (MUTATION: count every rejected row in the scan)."""
    other = "Pinning the CUDA allocator config fixes the fragmentation crash"
    mem = _memory(tmp_path, _lesson(REJECTED), _lesson(other, role="developer"), _lesson(KEPT))
    for statement in (REJECTED, other):
        record_claim_decision(str(mem), statement=statement, decision="rejected", scope=TASK)
    _eng, (text, receipt) = _prior(tmp_path, mem)
    assert receipt["operator_rejected"] == 1 and "[MEMORY_OPERATOR_REJECTED: 1 lesson(s)" in text
    _eng, (dev, dev_receipt) = _prior(tmp_path, mem, role="developer")
    assert dev_receipt["operator_rejected"] == 1 and other not in dev


def test_with_no_decision_the_prior_is_the_historical_one(tmp_path):
    mem = _memory(tmp_path)
    _eng, (on, on_receipt) = _prior(tmp_path, mem)
    _eng, (off, off_receipt) = _prior(tmp_path, mem, on=False)
    assert on == off and on_receipt == off_receipt


def test_a_ratified_or_pinned_claim_and_a_cleared_rejection_stay(tmp_path):
    mem = _memory(tmp_path)
    record_claim_decision(str(mem), statement=KEPT, decision="ratified", scope=TASK)
    record_claim_decision(str(mem), statement=REJECTED, decision="rejected", scope=TASK)
    record_claim_decision(str(mem), statement=REJECTED, decision="clear", scope=TASK)
    _eng, (text, receipt) = _prior(tmp_path, mem)
    assert REJECTED in text and KEPT in text and "operator_rejected" not in receipt


def test_both_roles_withhold_it(tmp_path):
    mem = _memory(tmp_path, _lesson(REJECTED, role=None), _lesson(KEPT, role=None))
    record_claim_decision(str(mem), statement=REJECTED, decision="rejected", scope=TASK)
    for role in ("researcher", "developer"):
        _eng, (text, receipt) = _prior(tmp_path, mem, role=role)
        assert REJECTED not in text and receipt["operator_rejected"] == 1, role


def test_an_unreadable_ledger_withholds_nothing_and_says_so(tmp_path):
    """A guessed subset of the operator's decisions is not a decision: the ledger that cannot be
    projected (`GovernanceLedgerUnavailable`) leaves the prior whole and disclosed."""
    mem = _memory(tmp_path)
    (mem / "claim_decisions.jsonl").write_text('{"decision": "rejected"}\n', encoding="utf-8")
    _eng, (text, receipt) = _prior(tmp_path, mem)
    assert REJECTED in text and KEPT in text
    assert "[MEMORY_DECISIONS_UNAVAILABLE:" in text and receipt["claim_decisions_unavailable"] is True
    _eng, (off, off_receipt) = _prior(tmp_path, mem, on=False)
    assert "MEMORY_DECISIONS_UNAVAILABLE" not in off and "claim_decisions_unavailable" not in off_receipt


def test_the_receipt_the_run_records_counts_the_withheld_lessons(tmp_path):
    mem = _memory(tmp_path)
    record_claim_decision(str(mem), statement=REJECTED, decision="rejected", scope=TASK)
    eng = make_engine(tmp_path / "run", n_seeds=1, max_nodes=1, reflection_priors=True,
                      memory_dir=str(mem), lesson_prior_claim_decisions=True)
    eng.lessons.load_reflection_priors_both()
    eng.lessons.record_prior_injection(at_node=0, phase="run_start")
    rows = [e.data for e in eng.store.read_all() if e.type == EV_PRIOR_INJECTED]
    researcher = next(r for r in rows if r["role"] == "researcher")
    assert researcher["operator_rejected"] == 1
    assert lesson_id(REJECTED) not in {row["id"] for row in researcher["rows"]}


def test_a_decision_made_mid_run_is_a_change_for_the_refresh(tmp_path):
    """MUTATION: stamp only `lessons.jsonl` -> a rejection waits for the next run's lesson."""
    mem = _memory(tmp_path)
    on = make_engine(tmp_path / "on", reflection_priors=True, memory_dir=str(mem),
                     lesson_prior_claim_decisions=True)
    off = make_engine(tmp_path / "off", reflection_priors=True, memory_dir=str(mem))
    before_on, before_off = on.lessons.lessons_store_stamp(), off.lessons.lessons_store_stamp()
    record_claim_decision(str(mem), statement=REJECTED, decision="rejected", scope=TASK)
    assert on.lessons.lessons_store_stamp() != before_on
    assert off.lessons.lessons_store_stamp() == before_off, "off: the historical stamp"
    (mem / "lessons.jsonl").unlink()
    assert on.lessons.lessons_store_stamp() is None, "no lessons store: no stamp, as before"


# ------------------------------------------------------------------------------------ the rule
def test_the_withheld_set_is_the_claims_surface_s_rejected_set():
    """One grouping and one decision lookup: every lesson files under the uid `claim_assessments`
    shows it under, and the rejected set is its `operator-rejected` rows (MUTATION: key the lesson on
    another scope or metric than the grouping)."""
    lessons = [_lesson(REJECTED), _lesson(KEPT), _lesson(REJECTED + " a lot"),
               _lesson(KEPT, task_id="other_task"),
               _lesson(REJECTED, fingerprint=["kind:quadratic", "dir:min", "metric:recall"])]
    rows = claim_assessments(lessons, bounded=False)
    assert {lesson_claim_uid(lesson) for lesson in lessons} == {row["claim_uid"] for row in rows}
    decided = {"clm": {}}
    assert operator_rejected_claim_uids(lessons, decided) == frozenset()
    assert lesson_claim_uid({"statement": ""}) is None and lesson_claim_uid("x") is None
    assert lesson_claim_uid({"statement": "the"}) is None, "no subject content: no claim"


def test_a_scoped_rejection_reaches_its_own_task_only_and_an_unscoped_one_reaches_all(tmp_path):
    mine, theirs = _lesson(REJECTED), _lesson(REJECTED, task_id="other_task")
    record_claim_decision(str(tmp_path), statement=REJECTED, decision="rejected", scope=TASK)
    rejected = operator_rejected_claim_uids([mine, theirs], load_claim_decisions(tmp_path))
    assert lesson_claim_uid(mine) in rejected and lesson_claim_uid(theirs) not in rejected
    record_claim_decision(str(tmp_path), statement=REJECTED, decision="rejected")
    rejected = operator_rejected_claim_uids([mine, theirs], load_claim_decisions(tmp_path))
    assert {lesson_claim_uid(mine), lesson_claim_uid(theirs)} <= rejected


# ------------------------------------------------------------------------------------ the switch
def test_on_for_new_runs_off_for_a_pre_field_snapshot_and_in_the_bare_library(tmp_path):
    assert Settings().lesson_prior_claim_decisions is True
    assert LEGACY_CONFIG_SNAPSHOT_DEFAULTS["lesson_prior_claim_decisions"] is False
    legacy = Settings().masked_snapshot()
    legacy.pop("lesson_prior_claim_decisions")
    assert settings_from_snapshot(legacy).lesson_prior_claim_decisions is False
    assert EngineOptions().lesson_prior_claim_decisions is False
    assert EngineOptions.from_settings(Settings()).lesson_prior_claim_decisions is True
    assert make_engine(tmp_path / "run")._lesson_prior_claim_decisions is False
