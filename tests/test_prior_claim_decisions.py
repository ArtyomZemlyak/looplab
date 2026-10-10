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


# ------------------------------------------------------------------------------------ critic crit_v49
_OVER_FENCE = ["kind:quadratic", "dir:min"] + [f"tok{i}" for i in range(300)]


def test_a_row_the_claims_surface_refuses_still_answers_for_its_own_claim(tmp_path):
    """F2: the surface refuses a row whose fingerprint is over the fence (doc 69 §5.2: 29 of 77 rows
    of the real store) while the prior still renders it — so with no valid sibling in the window the
    rejection never reached it (driven by the critic as the purge variant). Its OWN claim identity is
    resolved through the surface's candidate chain now (MUTATION: withhold by the window's groups
    only)."""
    from looplab.engine.claims import claims_for_memory
    mem = _memory(tmp_path, _lesson(REJECTED, fingerprint=_OVER_FENCE), _lesson(KEPT))
    record_claim_decision(str(mem), statement=REJECTED, decision="rejected", scope=TASK)
    assert [c["statement"] for c in claims_for_memory(str(mem))] == [KEPT], "the surface refuses it"
    _eng, (text, receipt) = _prior(tmp_path, mem)
    assert REJECTED not in text and KEPT in text and receipt["operator_rejected"] == 1


def test_the_row_s_own_lookup_walks_the_surface_s_whole_candidate_chain():
    """The per-row lookup is the surface's chain, not the uid alone (the critic's M24 was wrong for
    exactly this): an UNSCOPED rejection reaches a task-scoped row through the wider candidates."""
    from looplab.engine.claims import (decision_for_claim, lesson_rejected, load_claim_decisions,
                                       record_claim_decision as record)
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        record(d, statement=REJECTED, decision="rejected")           # no scope: portfolio-wide
        decisions = load_claim_decisions(d)
    row = _lesson(REJECTED)
    assert lesson_rejected(row, decisions)
    assert not lesson_rejected(_lesson(KEPT), decisions)
    assert not lesson_rejected({"statement": ""}, decisions) and not lesson_rejected("x", decisions)
    assert decision_for_claim({}, uid="u", rep=REJECTED, scope=TASK, metric="") == (None, "")
    # A task-scoped decision with no metric reaches the same task's METRIC-qualified row only
    # through the chain's wider candidate — no legacy key holds a scoped decision (MUTATION: the
    # row's own uid alone).
    metric_row = _lesson(REJECTED, fingerprint=["kind:quadratic", "dir:min", "metric:recall"])
    with tempfile.TemporaryDirectory() as d:
        record(d, statement=REJECTED, decision="rejected", scope=TASK)
        scoped = load_claim_decisions(d)
        record(d, statement=KEPT, decision="ratified", scope=TASK)
        ratified = load_claim_decisions(d)
    assert lesson_rejected(metric_row, scoped)
    # Only a REJECTION withholds (MUTATION: any decision found).
    assert not lesson_rejected(_lesson(KEPT), ratified) and lesson_rejected(row, ratified)


def test_with_no_rejection_in_the_ledger_no_claim_projection_is_built(tmp_path, monkeypatch):
    """F4: the projection cost +194 ms per prior build with no ledger at all. With no REJECTION in
    the ledger nothing is projected, by the prior or the pull (MUTATION: drop `rejects_anything`).
    The patches name the module that READS the two names — `operator_rejected_lessons`' own — since
    a patch on the `claims` re-export is read by nothing any more and passed with the guard gone."""
    import looplab.engine.claims as claims
    import looplab.engine.claims_assessments as assessments

    def boom(*_a, **_k):
        raise AssertionError("projected with nothing to withhold")

    monkeypatch.setattr(assessments, "operator_rejected_claim_uids", boom)
    monkeypatch.setattr(assessments, "lesson_rejected", boom)
    mem = _memory(tmp_path)
    record_claim_decision(str(mem), statement=KEPT, decision="ratified", scope=TASK)
    _eng, (text, receipt) = _prior(tmp_path, mem)
    assert REJECTED in text and "claim_decisions_unavailable" not in receipt
    pulled = _search(mem, on=True)
    assert REJECTED in pulled and "CLAIM_DECISIONS_UNAVAILABLE" not in pulled
    assert claims.operator_rejected_lessons(
        [_lesson(REJECTED)], {"k": {"decision": "ratified"}}) == frozenset()
    assert claims.rejects_anything({"k": {"decision": "rejected"}})
    assert not claims.rejects_anything({"k": {"decision": "ratified"}, "j": "rejected"})
    assert not claims.rejects_anything(None)


def test_an_empty_window_discloses_nothing_even_with_an_unreadable_ledger(tmp_path):
    """MUTATION (M02): ask the ledger with nothing parsed -> a prior with no lessons would say a
    rejected lesson "may still appear below"."""
    mem = tmp_path / "mem"
    mem.mkdir()
    (mem / "lessons.jsonl").write_text("", encoding="utf-8")
    (mem / "claim_decisions.jsonl").write_text('{"decision": "rejected"}\n', encoding="utf-8")
    eng = make_engine(tmp_path / "run", n_seeds=1, max_nodes=1, reflection_priors=True,
                      memory_dir=str(mem), lesson_prior_claim_decisions=True)
    *_rest, health, _case = eng.lessons._scan_prior_context(None, None)
    assert health["claim_decisions_unavailable"] is False and not health["claim_rejected"]


def test_an_unreadable_ledger_is_counted_as_a_containment(tmp_path):
    """MUTATION (M06): swallow the ledger failure without `contain` -> the containment census and
    `looplab timings` never see it."""
    from looplab.core.containment import containment_counts
    mem = _memory(tmp_path)
    (mem / "claim_decisions.jsonl").write_text('{"decision": "rejected"}\n', encoding="utf-8")
    before = containment_counts().get("prior claim decisions", 0)
    _eng, (_text, receipt) = _prior(tmp_path, mem)
    assert receipt["claim_decisions_unavailable"] is True
    assert containment_counts().get("prior claim decisions", 0) > before


def test_a_rejection_of_the_other_role_s_lesson_says_nothing_here(tmp_path):
    """MUTATION (M10): disclose whenever any scanned row is rejected -> "0 lesson(s) … not shown"
    and an `operator_rejected: 0` receipt on a prompt that withheld nothing."""
    other = "Pinning the CUDA allocator config fixes the fragmentation crash"
    mem = _memory(tmp_path, _lesson(other, role="developer"), _lesson(KEPT))
    record_claim_decision(str(mem), statement=other, decision="rejected", scope=TASK)
    _eng, (text, receipt) = _prior(tmp_path, mem)
    assert "MEMORY_OPERATOR_REJECTED" not in text and "operator_rejected" not in receipt
    _eng, (dev, dev_receipt) = _prior(tmp_path, mem, role="developer")
    assert dev_receipt["operator_rejected"] == 1 and other not in dev


def test_a_rejected_lesson_is_counted_as_rejected_before_it_is_counted_useless(tmp_path):
    """The operator's decision comes before the read-side hygiene: a lesson both rejected and
    useless (shown 9 times, never cited) is counted once, as the operator's (MUTATION M15: move the
    rejection after `filter_useless` -> it is counted as useless and the operator count drops)."""
    mem = _memory(tmp_path)
    (mem / "lesson_utility.jsonl").write_text(json.dumps(
        {"lesson_id": lesson_id(REJECTED), "shown": 9, "cited": 0, "run_id": "r0"}) + "\n",
        encoding="utf-8")
    record_claim_decision(str(mem), statement=REJECTED, decision="rejected", scope=TASK)
    _eng, (text, receipt) = _prior(tmp_path, mem)
    assert receipt["operator_rejected"] == 1 and receipt.get("quarantined_useless", 0) == 0
    _eng, (_off, off_receipt) = _prior(tmp_path, mem, on=False)
    assert off_receipt["quarantined_useless"] == 1, "off: the useless filter took it, as before"


def test_with_the_setting_on_and_no_ledger_a_new_lesson_still_moves_the_stamp(tmp_path):
    """MUTATION (M17): an absent ledger returns no stamp at all -> with the product default ON and
    no decision ever made (the common case) every refresh compares None == None and never fires."""
    mem = _memory(tmp_path)
    eng = make_engine(tmp_path / "on", reflection_priors=True, memory_dir=str(mem),
                      lesson_prior_claim_decisions=True)
    before = eng.lessons.lessons_store_stamp()
    assert before is not None
    with (mem / "lessons.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(_lesson("Label smoothing at 0.1 improves recall")) + "\n")
    assert eng.lessons.lessons_store_stamp() != before


def test_a_same_size_rewrite_of_the_ledger_moves_the_stamp(tmp_path):
    """MUTATION (M19): stamp the ledger by its size -> a `rejected` rewritten to `ratified` (same
    byte length) is no change and the refresh keeps the old verdict."""
    import os
    mem = _memory(tmp_path)
    record_claim_decision(str(mem), statement=REJECTED, decision="rejected", scope=TASK)
    eng = make_engine(tmp_path / "on", reflection_priors=True, memory_dir=str(mem),
                      lesson_prior_claim_decisions=True)
    before = eng.lessons.lessons_store_stamp()
    ledger = mem / "claim_decisions.jsonl"
    # BYTES, not text: a text-mode write on Windows turns each "\n" into "\r\n", and the rewrite
    # would no longer be the same size (the Windows CI leg, 2026-09-30: 362 vs 361).
    body = ledger.read_bytes()
    assert len("rejected") == len("ratified")
    fresh = mem / "claim_decisions.jsonl.new"
    fresh.write_bytes(body.replace(b'"rejected"', b'"ratified"'))
    os.replace(fresh, ledger)
    assert ledger.stat().st_size == len(body)
    assert eng.lessons.lessons_store_stamp() != before


# ------------------------------------------------------------ the pull tools (doc 69 69.21b)

def _search(mem, *, on, query="recall"):
    from looplab.tools.memory_tools import MemoryTools
    return MemoryTools(str(mem), role="researcher", claim_decisions=on).execute(
        "search_lessons", {"query": query})


def test_search_lessons_withholds_what_the_prior_withholds_and_says_how_many(tmp_path):
    """crit_v49 (driven): the prior hid a rejected lesson and `search_lessons` returned it as
    `UNTRUSTED_OUTCOME='supported'`. The same rule now answers for the pull, counted among the rows
    the query matched. MUTATIONS: skip the filter; count rows the query never matched; drop the
    disclosure. OFF: the historical result, byte for byte."""
    mem = _memory(tmp_path)
    record_claim_decision(str(mem), statement=REJECTED, decision="rejected", scope=TASK)
    on = _search(mem, on=True)
    assert REJECTED not in on and KEPT in on
    assert "[OPERATOR_REJECTED: 1 matching lesson(s) whose claim the operator rejected are withheld.]" in on
    unmatched = _search(mem, on=True, query="cosine")
    assert KEPT in unmatched and "OPERATOR_REJECTED" not in unmatched
    off = _search(mem, on=False)
    assert REJECTED in off and "OPERATOR_REJECTED" not in off
    (tmp_path / "plain").mkdir()
    assert off == _search(_memory(tmp_path / "plain"), on=False), "OFF: as if nothing was decided"


def test_search_lessons_with_an_unreadable_ledger_withholds_nothing_and_says_so(tmp_path):
    mem = _memory(tmp_path)
    (mem / "claim_decisions.jsonl").write_text('{"decision": "rejected"}\n', encoding="utf-8")
    on = _search(mem, on=True)
    assert REJECTED in on and KEPT in on and "[CLAIM_DECISIONS_UNAVAILABLE:" in on
    assert "CLAIM_DECISIONS_UNAVAILABLE" not in _search(mem, on=False)


def test_search_lessons_over_no_stated_lesson_reads_no_ledger_and_discloses_nothing(tmp_path):
    """The prior's M02 for the pull: with no row stating a lesson there is nothing to withhold, so
    an unreadable ledger is not read and not disclosed (MUTATIONS: read the ledger with no
    candidate; count a statement-less row as one -> "a lesson they reject may be shown" over none)."""
    mem = _memory(tmp_path, {"note": "a row that states no lesson"}, {"statement": ""})
    (mem / "claim_decisions.jsonl").write_text('{"decision": "rejected"}\n', encoding="utf-8")
    assert "CLAIM_DECISIONS_UNAVAILABLE" not in _search(mem, on=True, query="")


def test_the_pull_tools_are_built_with_the_setting_the_prior_reads(tmp_path):
    """The flag reaches BOTH builders of the agents' pull tools — the shared providers and the repo
    Developer's own set — and the constructors stay OFF (a prompt is a contract). MUTATION: drop a
    builder's keyword -> that toolset serves the rejected lesson."""
    import inspect

    from looplab.adapters.repo_developer import LLMRepoDeveloper
    from looplab.agents.providers import _shared_providers
    from looplab.tools.cross_run_tools import CrossRunTools
    from looplab.tools.memory_tools import MemoryTools

    for cls in (MemoryTools, CrossRunTools, LLMRepoDeveloper):
        assert inspect.signature(cls).parameters["claim_decisions"].default is False, cls
    from looplab.adapters.toytask import ToyTask
    from tests.factories import TOY_TASK

    mem = _memory(tmp_path)
    for flag in (True, False):
        settings = Settings(memory_dir=str(mem), cross_run_read_tools=True,
                            lesson_prior_claim_decisions=flag)
        built = [p for p in _shared_providers(ToyTask.load(TOY_TASK), settings, role="researcher")
                 if isinstance(p, (MemoryTools, CrossRunTools))]
        assert len(built) == 2 and all(p.claim_decisions is flag for p in built), built


def test_search_lessons_withholds_a_row_the_claims_surface_refuses(tmp_path):
    """The prior's F2 case, for the pull: a row whose fingerprint is over the fence is refused by the
    claims surface, so no group marks it — its OWN claim still resolves to the rejection. MUTATION:
    withhold by the window's groups only -> the rejected lesson is returned."""
    mem = _memory(tmp_path, _lesson(REJECTED, fingerprint=_OVER_FENCE), _lesson(KEPT))
    record_claim_decision(str(mem), statement=REJECTED, decision="rejected", scope=TASK)
    on = _search(mem, on=True)
    assert REJECTED not in on and KEPT in on and "[OPERATOR_REJECTED: 1 " in on


# ------------------------------------------------------------ the shared rule's row mapping
# A statement over `normalize_statement`'s 160-character cap: two claims that differ only past the
# cap share one LEGACY statement key, which is how an old unscoped decision reaches a claim group
# through the group's representative spelling alone (`claims_assessments.py::decision_for_claim`).
_HEAD = ("Warm-starting the tokenizer from the base checkpoint improves recall on the long-tail "
         "queries of the retrieval benchmark when the vocabulary overlap with the pretraining "
         "corpus is high")
_DECIDED = _HEAD + " and the batch is small"
_GROUP_REP = _HEAD + " and the learning rate is warmed up"
_RESPELLED = _GROUP_REP.replace("from the base", "from a base", 1)   # the same claim, another key
_RESPELLED_MARK = "from a base checkpoint"


def test_a_row_is_withheld_when_its_group_is_rejected_through_the_group_s_spelling(tmp_path):
    """The claims surface looks a group's decision up under the group's REPRESENTATIVE spelling
    (`_decision_for`): an unscoped decision on `_DECIDED` reaches the group `_GROUP_REP` heads (one
    legacy key) and so marks `_RESPELLED`, filed in that group, rejected — while that row's own
    lookup finds nothing. The prior and the pull withhold what the surface shows (MUTATION: drop
    the group clause of `operator_rejected_lessons` -> the respelled lesson is served)."""
    from looplab.engine.claims import lesson_rejected, operator_rejected_lessons
    rows = [_lesson(_GROUP_REP), _lesson(_GROUP_REP, run_id="third-run"), _lesson(_RESPELLED)]
    mem = _memory(tmp_path, *rows)
    record_claim_decision(str(mem), statement=_DECIDED, decision="rejected")
    decisions = load_claim_decisions(mem)
    [group] = claim_assessments(rows, decisions=decisions, bounded=False)
    assert group["maturity"] == "operator-rejected" and group["statement"] == _GROUP_REP
    assert group["decision"]["resolved_via"] == "legacy_statement_key"
    assert not lesson_rejected(rows[2], decisions), "the row's own spelling finds no decision"
    assert operator_rejected_lessons(rows, decisions) == {0, 1, 2}
    _eng, (text, _receipt) = _prior(tmp_path, mem)
    _eng, (off, _off_receipt) = _prior(tmp_path, mem, on=False)
    assert _RESPELLED_MARK not in text and _RESPELLED_MARK in off
    pulled, plain = _search(mem, on=True, query="tokenizer"), _search(mem, on=False, query="tokenizer")
    assert _RESPELLED_MARK not in pulled and _RESPELLED_MARK in plain


def test_a_row_the_scan_skips_does_not_shift_the_withheld_lesson(tmp_path):
    """The rule answers in positions of the rows it was HANDED; the prior and the pull hand it the
    rows their scan keeps and map each answer back to the window's own position (MUTATION: read the
    answer as a window position -> the statement-less row is "withheld" and the rejected lesson is
    served)."""
    mem = _memory(tmp_path, {"note": "a row that states no lesson"}, _lesson(REJECTED), _lesson(KEPT))
    record_claim_decision(str(mem), statement=REJECTED, decision="rejected", scope=TASK)
    _eng, (text, receipt) = _prior(tmp_path, mem)
    assert REJECTED not in text and KEPT in text and receipt["operator_rejected"] == 1
    pulled = _search(mem, on=True)
    assert REJECTED not in pulled and KEPT in pulled and "[OPERATOR_REJECTED: 1 " in pulled


def test_the_pull_groups_only_the_rows_its_scope_shows(tmp_path):
    """The prior's scan keeps the rows its scope allows BEFORE it groups them; so does the pull. Two
    of THIS run's own rows (never prior evidence) would otherwise lend the group their spelling —
    and with it the legacy key an old decision sits under (MUTATION: group every loaded row -> the
    respelled lesson is withheld on the strength of rows the tool never shows)."""
    from types import SimpleNamespace

    from looplab.tools.memory_tools import MemoryTools
    mem = _memory(tmp_path, _lesson(_GROUP_REP, run_id="live"), _lesson(_GROUP_REP, run_id="live"),
                  _lesson(_RESPELLED))
    record_claim_decision(str(mem), statement=_DECIDED, decision="rejected")
    tools = MemoryTools(str(mem), role="researcher", claim_decisions=True)
    tools.bind_state(SimpleNamespace(run_id="live", task_id=TASK, direction="min", goal=""))
    pulled = tools.execute("search_lessons", {"query": "tokenizer"})
    assert _RESPELLED_MARK in pulled and "OPERATOR_REJECTED" not in pulled, pulled
    assert "from the base checkpoint" not in pulled, "this run's own rows stay out of the pull"


def test_the_pull_tools_decide_once_per_store_state(tmp_path, monkeypatch):
    """crit_v58 L1, driven: with one rejection in the ledger every `search_lessons` call re-built the
    claims projection over the window (~1.1 s at 1,000 rows). The answer is kept per (store, ledger,
    scope) state and shared by every tool of the process; a new decision is a new state. MUTATION:
    drop the memo -> one projection per call."""
    import looplab.engine.claims as claims

    calls = []
    real = claims.operator_rejected_lessons
    monkeypatch.setattr(claims, "operator_rejected_lessons",
                        lambda *a, **k: calls.append(1) or real(*a, **k))
    mem = _memory(tmp_path)
    record_claim_decision(str(mem), statement=REJECTED, decision="rejected", scope=TASK)
    for _ in range(3):
        assert REJECTED not in _search(mem, on=True)          # a fresh MemoryTools each time
    assert len(calls) == 1
    record_claim_decision(str(mem), statement=KEPT, decision="rejected", scope=TASK)
    both = _search(mem, on=True)
    assert REJECTED not in both and KEPT not in both and len(calls) == 2


def test_the_memo_is_keyed_on_the_store_s_state(tmp_path):
    """crit_v59 F4 (M3, driven): a rejection recorded and a tool asked once, THEN a lesson stating
    the rejected claim is appended — the store's identity is in the memo's key, so the new row is
    decided. MUTATION: key the memo without the store's identity -> the stale empty answer serves
    it."""
    mem = _memory(tmp_path, _lesson(KEPT))
    record_claim_decision(str(mem), statement=REJECTED, decision="rejected", scope=TASK)
    assert KEPT in _search(mem, on=True)
    with open(mem / "lessons.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(_lesson(REJECTED)) + "\n")
    after = _search(mem, on=True)
    assert REJECTED not in after and KEPT in after, after


def _bound_search(mem, *, goal="", on=True):
    from types import SimpleNamespace

    from looplab.tools.memory_tools import MemoryTools
    tools = MemoryTools(str(mem), role="researcher", claim_decisions=on)
    tools.bind_state(SimpleNamespace(run_id="live", task_id=TASK, direction="min", goal=goal))
    return tools.execute("search_lessons", {"query": "recall"})


def test_the_memo_is_keyed_on_the_reader_s_scope(tmp_path):
    """crit_v59 F4 (M4, driven): two readers of one store — one BOUND to this task fills the memo
    first, then an UNBOUND (portfolio-wide) one asks. The scope is in the key, so the unbound reader
    groups every row and withholds the other task's rejected lesson. MUTATION: key the memo without
    the scope -> the bound reader's empty answer serves it."""
    mem = _memory(tmp_path, _lesson(KEPT), _lesson(REJECTED, task_id="other_task"))
    record_claim_decision(str(mem), statement=REJECTED, decision="rejected", scope="other_task")
    assert REJECTED not in _bound_search(mem), "another task's row: out of the bound reader's scope"
    unbound = _search(mem, on=True)
    assert REJECTED not in unbound and KEPT in unbound, unbound


def test_the_memo_is_keyed_on_the_reader_s_goal(tmp_path):
    """crit_v59 F4 (M5): a bound reader's GOAL decides which related-task rows it sees
    (`trust/cross_run.py::LessonScope.related_goal`), so its terms are part of the key: a reader
    whose goal relates to another task's rejected lesson withholds it after a reader whose goal does
    not filled the memo. MUTATION: drop `goal_terms` from the scope key -> served."""
    related = _lesson(REJECTED, task_id="other_task",
                      fingerprint=["kind:quadratic", "tokenizer", "checkpoint", "recall"])
    mem = _memory(tmp_path, _lesson(KEPT), related)
    assert REJECTED in _bound_search(mem, goal="tokenizer checkpoint recall", on=False), \
        "the related goal admits the other task's row"
    record_claim_decision(str(mem), statement=REJECTED, decision="rejected", scope="other_task")
    assert REJECTED not in _bound_search(mem, goal="quadratic bowl minimum")
    related_out = _bound_search(mem, goal="tokenizer checkpoint recall")
    assert REJECTED not in related_out and KEPT in related_out, related_out


# ------------------------------------------------ every builder hands the pull tools the switch (N2)

def test_every_pull_tool_the_package_builds_is_handed_the_operator_s_switch():
    """crit_v58 N2: three builders — Genesis's CLI door, its route, the owner Assistant — built a
    `CrossRunTools` without `claim_decisions=`, so a lesson the operator rejected still reached them
    while the switch (ON by default) withheld it everywhere else. AST over the package, so a fourth
    builder cannot appear unguarded: every construction of a pull tool passes the keyword. EXEMPT:
    the judgebench fixture world, whose tool results ARE the case (no run, so no operator's switch).
    MUTATION: drop the keyword at any builder -> named here."""
    import ast

    from tests._source_scan import PKG, iter_trees

    exempt = {("trajectory.py", "_provider_memory")}
    built, offenders = [], []
    for path, tree in iter_trees(PKG):
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for call in ast.walk(fn):
                if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                        and call.func.id in {"CrossRunTools", "MemoryTools"}):
                    continue
                site = (path.name, fn.name)
                built.append(site)
                switch = next((k.value for k in call.keywords if k.arg == "claim_decisions"), None)
                if site not in exempt and switch is None:
                    offenders.append(f"{path.name}::{fn.name} builds {call.func.id}")
                elif site not in exempt and isinstance(switch, ast.Constant):
                    # The operator's switch, never a literal (crit_v59 F4, W3: the Genesis route
                    # passing `claim_decisions=False` survived a keyword-only check).
                    offenders.append(f"{path.name}::{fn.name} passes a constant switch")
    assert len(set(built)) >= 7 and set(exempt) <= set(built), "the scan saw the builders"
    assert not offenders, offenders


def test_genesis_and_the_owner_assistant_read_the_switch(tmp_path, monkeypatch):
    """The three builders crit_v58 N2 named, driven: Genesis's author hands its `CrossRunTools` the
    switch it is given, the CLI gives it the run's setting through the ONE reader, and the owner
    Assistant's portfolio provider reads it too. MUTATIONS, each red here: drop the keyword at a
    builder; the CLI passes no `claim_decisions`."""
    from types import SimpleNamespace

    import looplab.cli as cli
    import looplab.engine.genesis as genesis
    from looplab.cli import app
    from looplab.core.config import claim_decisions_enabled
    from looplab.serve.assistant import build_tools
    from looplab.serve.principal import OWNER_PRINCIPAL
    from looplab.tools.cross_run_tools import CrossRunTools
    from typer.testing import CliRunner

    assert claim_decisions_enabled(Settings()) is True, "the switch ships ON"
    assert claim_decisions_enabled(SimpleNamespace()) is False, "a stub without the field: OFF"

    captured = {}

    def _stop(_client, tools, *_a, **_kw):
        captured["tools"] = tools
        raise RuntimeError("stop after assembling the tools")

    monkeypatch.setattr(genesis, "agentic_struct", _stop)
    for on in (True, False):
        genesis.author_task("classify some text", client=object(), kinds=("dataset",),
                            memory_dir=str(tmp_path), cross_run_read_tools=True, claim_decisions=on)
        tools = captured.pop("tools")
        crt = next(p for p in getattr(tools, "providers", [tools]) if isinstance(p, CrossRunTools))
        assert crt.claim_decisions is on

    seen = []
    monkeypatch.setattr(cli, "make_llm_client", lambda settings, **k: object())
    # Genesis takes the endpoint preflight first (doc 75 UX-01); the model here is a stand-in.
    import looplab.agents.preflight as preflight
    monkeypatch.setattr(preflight, "preflight_role_endpoints", lambda *a, **k: None)

    def _author(goal, **k):
        seen.append(k.get("claim_decisions"))
        return genesis.GenesisResult(
            task={"kind": "quadratic", "goal": goal, "direction": "min",
                  "bounds": {"x": [-10.0, 10.0], "y": [-10.0, 10.0]}}, rationale="r")

    monkeypatch.setattr(genesis, "author_task", _author)
    runner = CliRunner()
    for flag, expected in (([], True), (["-s", "lesson_prior_claim_decisions=false"], False)):
        result = runner.invoke(app, [
            "run", "--genesis", "--kind", "quadratic", "--goal", "minimize x^2", "-s", "max_nodes=1",
            "-s", "backend=toy", *flag, "--out", str(tmp_path / f"cli-{expected}")])
        assert result.exit_code == 0, result.output
        assert seen[-1] is expected, seen

    for on in (True, False):
        settings = SimpleNamespace(memory_dir=str(tmp_path / "mem"), cross_run_read_tools=True,
                                   lesson_prior_claim_decisions=on)
        tools = build_tools(tmp_path, mode="auto", settings=settings, principal=OWNER_PRINCIPAL)
        crt = next(p for p in tools.providers if isinstance(p, CrossRunTools))
        assert crt.claim_decisions is on


def test_a_row_that_states_no_claim_is_never_withheld_by_key(tmp_path, monkeypatch):
    """A lesson whose statement names no claim (`lesson_claim_uid` is None: punctuation, a stopword)
    has no claim key, so no member of the rejected set may stand for it — a `None` there withholds
    every such row at once. Driven by a rule that calls EVERY row of the window rejected: the two
    claim rows are withheld, the claim-less one still answers the search. MUTATION: keep `None` in
    `operator_rejected_keys`' set -> it is withheld too."""
    import looplab.engine.claims as claims

    mem = _memory(tmp_path, _lesson(REJECTED), _lesson(KEPT), _lesson("…"))
    record_claim_decision(str(mem), statement=REJECTED, decision="rejected", scope=TASK)
    monkeypatch.setattr(claims, "operator_rejected_lessons",
                        lambda rows, _decisions: frozenset(range(len(rows))))
    out = _search(mem, on=True, query="")
    assert "[RESULT_SET: matched=1; returned=1;" in out, out
    assert "[OPERATOR_REJECTED: 2 matching lesson(s)" in out, out
