"""Deleting a run may take its OWN cross-run memory with it — and nothing else.

The operator's rule, verbatim: "delete only what we can really attribute to the run being deleted;
if something was merged (concepts, say) then leave it alone."

That rule is not decoration. These stores merge. A consolidated lesson keeps the NEWEST
contributor's `run_id` while carrying other runs' support in `evidence_count` / `evidence_refs`, so
the obvious implementation — `[row for row in rows if row["run_id"] != deleted]` — would delete
evidence earned by runs that still exist, and would do it silently. Everything below exists to make
that specific mistake a red test.
"""
from __future__ import annotations

import json

import orjson
import pytest

from looplab.serve.memory_cascade import (
    NOT_THIS_RUN, RunIdentity, attributable_memory, capsule_keep_reason, case_keep_reason,
    claim_keep_reason, lesson_keep_reason, merged_concept_ids, purge_attributable_memory)

GONE = "doomed-run"
KEPT = "surviving-run"
# The durable identity. `run_id` is a DIRECTORY NAME and is reused; every predicate keys on the uid
# when the row has one, so the fixtures carry both exactly as the real writers do.
GONE_UID = "uid-doomed"
KEPT_UID = "uid-surviving"
DOOMED = RunIdentity(GONE, GONE_UID)


def _write(path, rows, *, dumps=orjson.dumps):
    payload = b"".join(
        (dumps(row) if isinstance(dumps(row), bytes) else dumps(row).encode()) + b"\n"
        for row in rows)
    path.write_bytes(payload)


def _lesson(run_id, statement, **extra):
    uid = GONE_UID if run_id == GONE else KEPT_UID
    return {"run_id": run_id, "run_uid": uid, "task_id": "t", "statement": statement,
            "outcome": "won", **extra}


@pytest.fixture()
def memory(tmp_path):
    d = tmp_path / "memory"
    d.mkdir()
    return d


# --------------------------------------------------------------------------------- the predicates

def test_a_consolidated_lesson_is_never_this_runs_to_delete():
    """`evidence_count` counts DISTINCT contributing runs. Above 1 the row speaks for others."""
    assert lesson_keep_reason(_lesson(GONE, "mine alone"), DOOMED) == ""
    merged = _lesson(GONE, "folded", evidence_count=3)
    assert "other runs" in lesson_keep_reason(merged, DOOMED)


def test_a_lesson_whose_lineage_names_another_run_is_kept_even_at_evidence_count_one():
    """The two fields can disagree — `evidence_refs` is written by a later pass than the count, and
    a row carrying one is the row whose support we can actually SEE belongs elsewhere."""
    row = _lesson(GONE, "x", evidence_count=1,
                  evidence_refs=[{"node_id": 2, "run_id": KEPT, "run_uid": KEPT_UID}])
    assert "other runs" in lesson_keep_reason(row, DOOMED)
    own = _lesson(GONE, "x", evidence_count=1, evidence_refs=[{"node_id": 2, "run_id": GONE, "run_uid": GONE_UID}])
    assert lesson_keep_reason(own, DOOMED) == ""


def test_support_whose_origin_was_never_recorded_is_not_support_we_may_discard():
    row = _lesson(GONE, "x", evidence_count=1, evidence_untraceable_count=2)
    assert "not recorded" in lesson_keep_reason(row, DOOMED)


def test_an_unreadable_evidence_count_is_treated_as_shared_not_as_absent():
    """Fail CLOSED. A row whose support cannot be read is exactly the row we must not gamble on."""
    assert lesson_keep_reason(_lesson(GONE, "x", evidence_count="lots"), DOOMED) != ""
    assert lesson_keep_reason(_lesson(GONE, "x", evidence_untraceable_count=None), DOOMED) == ""
    assert lesson_keep_reason(_lesson(GONE, "x", evidence_untraceable_count="?"), DOOMED) != ""


def test_another_runs_row_is_reported_as_not_ours_never_as_a_refusal():
    """The distinction drives a NUMBER the operator reads. Counting the whole store's other rows as
    "kept back" would claim the cascade considered and refused thousands of rows it never owned."""
    assert lesson_keep_reason(_lesson(KEPT, "theirs"), DOOMED) == NOT_THIS_RUN
    assert case_keep_reason({"run_id": KEPT}, DOOMED) == NOT_THIS_RUN


def test_a_capsule_whose_concepts_were_merged_stays():
    merged = frozenset({"optimization/analytic"})
    row = {"run_id": GONE, "concepts": ["loss/contrastive", "optimization/analytic"]}
    assert "merged" in capsule_keep_reason(row, DOOMED, merged_concepts=merged)
    private = {"run_id": GONE, "concepts": ["loss/contrastive"]}
    assert capsule_keep_reason(private, DOOMED, merged_concepts=merged) == ""


def test_both_ends_of_a_concept_merge_count_as_merged(memory):
    """The source id disappears into the target; the target now stands for more than the run that
    coined it. Recording only one end would delete half of every merge's evidence."""
    _write(memory / "concept_curation_log.jsonl", [
        {"run_id": KEPT, "proposals": {"merges": [
            {"from_concept": "optimization/analytical_solution",
             "to_concept": "optimization/analytic"}], "splits": [], "purges": []}},
        {"run_id": GONE, "proposals": {"merges": [], "splits": [], "purges": []}},
    ])
    assert merged_concept_ids(memory) == {
        "optimization/analytical_solution", "optimization/analytic"}


def test_a_claim_pool_another_run_has_curated_is_left_alone():
    curated = frozenset({"shared-task"})
    assert claim_keep_reason({"run_id": GONE, "task_id": "shared-task"}, DOOMED,
                             curated_tasks=curated) != ""
    assert claim_keep_reason({"run_id": GONE, "task_id": "private-task"}, DOOMED,
                             curated_tasks=curated) == ""


# ------------------------------------------------------------------------------ survey and purge

def _populate(memory):
    _write(memory / "lessons.jsonl", [
        _lesson(GONE, "mine alone"),
        _lesson(GONE, "folded", evidence_count=4),
        _lesson(KEPT, "theirs"),
    ])
    _write(memory / "meta_notes.jsonl", [
        {"run_id": GONE, "note": "finished", "task_id": "t"},
        {"run_id": KEPT, "note": "also finished", "task_id": "t"},
    ])
    _write(memory / "research_claims.jsonl", [
        {"run_id": GONE, "task_id": "t", "record_kind": "source_receipt"},
    ])
    _write(memory / "concept_capsules.jsonl", [
        {"run_id": GONE, "task_id": "t", "concepts": ["loss/contrastive"]},
        {"run_id": KEPT, "task_id": "t", "concepts": ["loss/contrastive"]},
    ])


def test_the_survey_counts_what_would_go_and_what_would_stay_and_why(memory):
    _populate(memory)
    report = attributable_memory(memory, GONE, GONE_UID)
    assert report["available"] is True
    assert report["deletable"] == 4                        # 1 lesson + 1 note + 1 claim + 1 capsule
    assert report["kept"] == 1                             # the consolidated lesson
    lessons = next(s for s in report["stores"] if s["file"] == "lessons.jsonl")
    assert lessons["reasons"] == [
        {"reason": "consolidated: it carries evidence from other runs", "rows": 1}]
    # The never-cascaded tiers are stated, not left for the operator to infer from silence — EVERY
    # one the store registry marks preserved (review 2026-09-22, ENG3-07: there used to be two
    # stated, while seven more stores sat on no list at all), each with its reason and its files.
    from looplab.engine.memory_stores import PRESERVED, MEMORY_STORES
    preserved = {p["store"]: p for p in report["preserved"]}
    assert {"skills", "curation_logs"} <= set(preserved)
    assert sorted(name for p in preserved.values() for name in p["files"]) == sorted(
        s.name for s in MEMORY_STORES if s.policy == PRESERVED)
    assert all(p["reason"] for p in preserved.values())


def test_the_survey_is_empty_rather_than_wrong_when_there_is_no_memory_dir(tmp_path):
    for value in (None, "", tmp_path / "nope"):
        report = attributable_memory(value, GONE, GONE_UID)
        assert report["available"] is False and report["deletable"] == 0
    assert attributable_memory(tmp_path, "", GONE_UID)["available"] is False


def test_the_purge_removes_exactly_the_surveyed_rows(memory):
    _populate(memory)
    surveyed = attributable_memory(memory, GONE, GONE_UID)["deletable"]
    result = purge_attributable_memory(memory, GONE, GONE_UID)
    assert result["ok"] is True and result["deleted"] == surveyed == 4

    lessons = [orjson.loads(l) for l in (memory / "lessons.jsonl").read_bytes().splitlines() if l]
    assert [row["statement"] for row in lessons] == ["folded", "theirs"]
    notes = [orjson.loads(l) for l in (memory / "meta_notes.jsonl").read_bytes().splitlines() if l]
    assert [row["run_id"] for row in notes] == [KEPT]
    assert (memory / "research_claims.jsonl").read_bytes().strip() == b""
    capsules = [orjson.loads(l)
                for l in (memory / "concept_capsules.jsonl").read_bytes().splitlines() if l]
    assert [row["run_id"] for row in capsules] == [KEPT]


def test_the_purge_is_idempotent(memory):
    """It is "remove every row attributable solely to R", so the second run is the first run's
    no-op. This is what makes it safe to re-issue after a partial failure, and what makes a retry of
    an already-succeeded deletion harmless."""
    _populate(memory)
    first = purge_attributable_memory(memory, GONE, GONE_UID)
    after_first = (memory / "lessons.jsonl").read_bytes()
    second = purge_attributable_memory(memory, GONE, GONE_UID)
    assert first["deleted"] == 4 and second["deleted"] == 0
    assert (memory / "lessons.jsonl").read_bytes() == after_first


def test_a_line_the_reader_cannot_parse_survives_the_purge_byte_for_byte(memory):
    """A cascade is not a licence to launder damage out of a shared store. The quarantine bytes are
    somebody's unresolved corruption; erasing them would make the next health receipt read clean."""
    damaged = b'{"run_id": "' + GONE.encode() + b'", "statement": TORN'
    (memory / "lessons.jsonl").write_bytes(
        orjson.dumps(_lesson(GONE, "mine alone")) + b"\n" + damaged + b"\n")
    assert purge_attributable_memory(memory, GONE, GONE_UID)["ok"] is True
    raw = (memory / "lessons.jsonl").read_bytes()
    assert damaged in raw
    assert b"mine alone" not in raw


def test_a_case_group_never_loses_its_champion_to_a_deletion(memory):
    """`active` marks the best contribution per (task, direction). Dropping the run that happened to
    hold it would leave the task retrievable as if it had never been solved — for every run that
    still exists. The election is re-run over what survives."""
    _write(memory / "cases.jsonl", [
        {"run_id": GONE, "run_uid": GONE_UID, "task_id": "t", "direction": "max",
         "goal": "g", "metric": 0.9, "active": True},
        {"run_id": KEPT, "run_uid": KEPT_UID + "-a", "task_id": "t", "direction": "max",
         "goal": "g", "metric": 0.7, "active": False},
        {"run_id": KEPT, "run_uid": KEPT_UID + "-b", "task_id": "t", "direction": "max",
         "goal": "g", "metric": 0.5, "active": False},
    ], dumps=json.dumps)
    assert purge_attributable_memory(memory, GONE, GONE_UID)["deleted"] == 1
    rows = [json.loads(l) for l in (memory / "cases.jsonl").read_text().splitlines() if l.strip()]
    assert len(rows) == 2
    active = [row for row in rows if row.get("active")]
    assert len(active) == 1 and active[0]["metric"] == 0.7, "the next best took the vacant slot"


def test_a_group_that_still_has_its_champion_is_not_re_elected(memory):
    _write(memory / "cases.jsonl", [
        {"run_id": GONE, "run_uid": GONE_UID, "task_id": "t", "direction": "max",
         "goal": "g", "metric": 0.4, "active": False},
        {"run_id": KEPT, "run_uid": KEPT_UID + "-a", "task_id": "t", "direction": "max",
         "goal": "g", "metric": 0.9, "active": True},
    ], dumps=json.dumps)
    purge_attributable_memory(memory, GONE, GONE_UID)
    rows = [json.loads(l) for l in (memory / "cases.jsonl").read_text().splitlines() if l.strip()]
    assert [(row["run_id"], row["active"]) for row in rows] == [(KEPT, True)]


def test_cases_are_parsed_with_the_parser_they_were_written_with(memory):
    """`JsonlCaseLibrary` writes cases with stdlib json, which emits the `NaN` literal orjson
    REFUSES. Parsing the store with the wrong codec would classify a live row as quarantine — and
    quarantine is retained, so the deletion would silently do nothing and report success."""
    _write(memory / "cases.jsonl", [
        {"run_id": GONE, "task_id": "t", "direction": "max", "goal": "g", "metric": float("nan")},
    ], dumps=json.dumps)
    assert b"NaN" in (memory / "cases.jsonl").read_bytes()
    assert purge_attributable_memory(memory, GONE, GONE_UID)["deleted"] == 1
    assert (memory / "cases.jsonl").read_bytes().strip() == b""


def test_a_missing_memory_dir_is_a_reported_failure_not_a_silent_success(tmp_path):
    result = purge_attributable_memory(tmp_path / "absent", GONE, GONE_UID)
    assert result["ok"] is False and result["deleted"] == 0 and result["failures"]


def test_skills_are_never_cascaded_even_when_every_predicate_would_delete(memory):
    """Skills are outside the tier table entirely — promotion REQUIRES a second, differently
    fingerprinted task, so a promoted skill is cross-run by construction.

    The previous version of this test asserted a `.md` file still existed after a purge, which no
    code path could have removed: mutating all three keep-predicates to "delete everything" left it
    green. It now asserts the STRUCTURAL fact — `skills` is not a cascaded tier and is disclosed as
    preserved — which is what actually protects the directory.
    """
    from looplab.serve.memory_cascade import CASCADED_TIERS, PRESERVED_TIERS

    assert not any("skill" in filename for filename, _label in CASCADED_TIERS)
    assert "skills" in {store for store, _why in PRESERVED_TIERS}
    (memory / "skills").mkdir()
    (memory / "skills" / "a-skill.md").write_text("# claim\nfingerprints: [[\"a\"],[\"b\"]]\n")
    _populate(memory)
    purge_attributable_memory(memory, GONE, GONE_UID)
    assert (memory / "skills" / "a-skill.md").exists()
    assert {s["file"] for s in attributable_memory(memory, GONE, GONE_UID)["stores"]} \
        <= {f for f, _ in CASCADED_TIERS}


# ------------------------------------------------------------------- WHICH run a row belongs to

def test_a_second_run_reusing_the_directory_name_keeps_its_own_memory(memory):
    """THE defect this identity exists for, driven end to end.

    `run_id` is the run DIRECTORY NAME. Delete a run and create another with the same name — which
    the operator does constantly, and which every `demo`/`baseline` run shares by default — and
    matching on it deleted the SURVIVOR's rows. Both incarnations below are called `doomed-run`.
    """
    survivor = RunIdentity(GONE, "uid-second-incarnation")
    _write(memory / "lessons.jsonl", [
        {"run_id": GONE, "run_uid": GONE_UID, "task_id": "t", "statement": "first", "outcome": "won"},
        {"run_id": GONE, "run_uid": survivor.run_uid, "task_id": "t", "statement": "second",
         "outcome": "won"},
    ])
    assert purge_attributable_memory(memory, GONE, GONE_UID)["deleted"] == 1
    rows = [json.loads(line) for line in (memory / "lessons.jsonl").read_text().splitlines() if line]
    assert [r["statement"] for r in rows] == ["second"], "the live incarnation's lesson was deleted"


def test_a_ref_naming_a_surviving_run_by_uid_keeps_the_lesson(memory):
    """`evidence_refs` carries BOTH ids so a reader can use the durable one. Reading `run_id` first
    meant a ref pointing at a SURVIVING run that shares this run's directory name compared equal,
    and the corroboration was deleted — the exact outcome the predicate exists to prevent."""
    row = {"run_id": GONE, "run_uid": GONE_UID, "task_id": "t", "statement": "x", "outcome": "won",
           "evidence_count": 1,
           "evidence_refs": [{"node_id": 2, "run_id": GONE, "run_uid": "uid-someone-else"}]}
    assert "other runs" in lesson_keep_reason(row, RunIdentity(GONE, GONE_UID))


def test_a_legacy_row_with_no_uid_still_matches_by_name_and_says_so(memory):
    """Rows written before `run_uid` existed have only the directory name. That IS their best
    identity — but the report must say which one was used, because the two are not equally safe.

    `identity` used to key on the CALLER alone, so a uid-keyed purge that had in fact fallen back to
    bare-name matching reported `run_uid` — the label an operator reads as "this cannot have touched
    another run", over the one case where it can: two checkouts sharing `~/.looplab/memory`, both
    holding a directory named `demo`. That case is `mixed`, and the count is per store."""
    _write(memory / "meta_notes.jsonl", [{"run_id": GONE, "note": "old", "task_id": "t"}])
    report = attributable_memory(memory, GONE, GONE_UID)
    assert report["deletable"] == 1 and report["identity"] == "mixed"
    assert report["name_matched"] == 1
    notes = next(s for s in report["stores"] if s["file"] == "meta_notes.jsonl")
    assert notes["name_matched"] == 1

    assert attributable_memory(memory, GONE)["identity"] == "run_id", (
        "a run whose own uid we could not read must disclose that it matched by name alone")

    # A store whose rows all carry a uid is NOT mixed — the disclosure has to be specific enough to
    # be worth reading, or every receipt carries the warning and none of them mean it.
    _write(memory / "meta_notes.jsonl", [{"run_id": GONE, "run_uid": GONE_UID, "note": "new",
                                          "task_id": "t"}])
    exact = attributable_memory(memory, GONE, GONE_UID)
    assert exact["deletable"] == 1 and exact["identity"] == "run_uid"
    assert exact["name_matched"] == 0


def test_a_store_that_cannot_be_read_is_a_failure_not_a_clean_success_at_any_uid(memory):
    """THE SAME PROPERTY AS ITS SIBLING BELOW, without a file mode — so it runs as root too.

    The sibling makes the store unreadable with `chmod 0o000` and skips when that has no effect,
    which is correct about the CONSTRUCTION and wrong about the PROPERTY: "an unreadable store is a
    failure, not a clean success" does not depend on who is asking. This box runs as root, so that
    skip fired every time, and measured 2026-09-08 the whole property was untested here — restoring
    the documented defect (`except OSError: return []` in `memory_cascade._rows`) left this file
    and its three neighbours at 82 passed.

    A DIRECTORY where a file belongs raises `IsADirectoryError` for root exactly as for anyone else,
    which is the same `OSError` branch a permission failure takes.
    """
    _populate(memory)
    path = memory / "lessons.jsonl"
    path.unlink()
    path.mkdir()                          # unreadable as a file, for every uid

    survey = attributable_memory(memory, GONE, GONE_UID)
    assert [u["file"] for u in survey["unreadable"]] == ["lessons.jsonl"]
    result = purge_attributable_memory(memory, GONE, GONE_UID)
    assert result["ok"] is False, "an unreadable store reported a clean purge"
    assert any(f["file"] == "lessons.jsonl" for f in result["failures"])
    # …and the OTHER stores still got purged. One unreadable store must not stop the rest.
    assert result["deleted"] == 3


def test_a_store_that_cannot_be_read_is_a_failure_not_a_clean_success(memory):
    """It used to return [] — "nothing of ours here" — so the purge reported success having done
    nothing, routing straight around the failures[] and the retry that exist for this."""
    import os

    _populate(memory)
    path = memory / "lessons.jsonl"
    os.chmod(path, 0o000)
    try:
        if os.access(path, os.R_OK):
            pytest.skip("running as a user that ignores file modes")
        survey = attributable_memory(memory, GONE, GONE_UID)
        assert [u["file"] for u in survey["unreadable"]] == ["lessons.jsonl"]
        result = purge_attributable_memory(memory, GONE, GONE_UID)
        assert result["ok"] is False
        assert any(f["file"] == "lessons.jsonl" for f in result["failures"])
        # …and the OTHER stores still got purged. One unreadable store must not stop the rest.
        assert result["deleted"] == 3
    finally:
        os.chmod(path, 0o600)


def test_an_unreadable_governance_log_protects_everything_rather_than_nothing(memory):
    """If we cannot see which concepts were merged, we must not conclude that none were."""
    import os

    _write(memory / "concept_capsules.jsonl",
           [{"run_id": GONE, "run_uid": GONE_UID, "task_id": "t", "concepts": ["a/b"]}])
    log = memory / "concept_curation_log.jsonl"
    _write(log, [{"run_id": KEPT, "proposals": {"merges": [], "splits": [], "purges": []}}])
    os.chmod(log, 0o000)
    try:
        if os.access(log, os.R_OK):
            pytest.skip("running as a user that ignores file modes")
        assert attributable_memory(memory, GONE, GONE_UID)["deletable"] == 0
    finally:
        os.chmod(log, 0o600)


def test_a_case_group_with_no_measured_survivor_still_elects_one(memory):
    """`JsonlCaseLibrary._add_locked` takes `candidates[-1]` when nothing is measured, because
    `valid_case_record` admits `metric=None`. Leaving the group with nobody active makes it invisible
    to `search()`/`all()`, which filter `active is not False` — the task then reads as never solved,
    which is what this re-election exists to prevent."""
    _write(memory / "cases.jsonl", [
        {"run_id": GONE, "run_uid": GONE_UID, "task_id": "t", "direction": "max", "goal": "g",
         "metric": 0.9, "active": True},
        {"run_id": KEPT, "run_uid": KEPT_UID, "task_id": "t", "direction": "max", "goal": "g",
         "metric": None, "active": False},
    ], dumps=json.dumps)
    assert purge_attributable_memory(memory, GONE, GONE_UID)["deleted"] == 1
    rows = [json.loads(l) for l in (memory / "cases.jsonl").read_text().splitlines() if l.strip()]
    assert [(r["run_id"], r["active"]) for r in rows] == [(KEPT, True)]


def test_the_champion_guard_is_load_bearing(memory):
    """A group that already HAS an active champion must not be rewritten. Without the guard the
    cascade re-elects on every purge, including in groups the deleted run never touched — and the
    previous test for it passed with the guard deleted, because its fixture made the incumbent
    champion also the metric-best row."""
    _write(memory / "cases.jsonl", [
        {"run_id": GONE, "run_uid": GONE_UID, "task_id": "t", "direction": "max", "goal": "g",
         "metric": 0.1, "active": False},
        # The incumbent is NOT the metric-best row, so a re-election would visibly move it.
        {"run_id": KEPT, "run_uid": KEPT_UID, "task_id": "t", "direction": "max", "goal": "g",
         "metric": 0.4, "active": True},
        {"run_id": KEPT, "run_uid": KEPT_UID + "-b", "task_id": "t", "direction": "max", "goal": "g",
         "metric": 0.9, "active": False},
    ], dumps=json.dumps)
    purge_attributable_memory(memory, GONE, GONE_UID)
    rows = [json.loads(l) for l in (memory / "cases.jsonl").read_text().splitlines() if l.strip()]
    active = [r for r in rows if r.get("active")]
    assert len(active) == 1 and active[0]["metric"] == 0.4, (
        "the incumbent champion was re-elected away by an unrelated deletion")


def test_a_uid_bearing_row_deleted_on_a_bare_name_ref_is_disclosed_as_mixed(memory):
    """The SECOND place a directory name can decide a deletion, and the half that was missing.

    This row carries the doomed run's uid, so its own attribution is exact. What is not exact is the
    corroboration test that lets it go: `lesson_keep_reason` reads a uid-less `evidence_refs` entry
    naming `doomed-run` as a self-reference — deliberately, because a mixed-generation store is full
    of this run's own pre-uid refs — and a name is what made that call. Reading only the ROW's own
    identity reported `identity: "run_uid"`, the label an operator reads as "this purge cannot have
    touched another run", over precisely the decision that can.
    """
    row = {"run_id": GONE, "run_uid": GONE_UID, "task_id": "t", "statement": "x", "outcome": "won",
           "evidence_count": 1, "evidence_refs": [{"node_id": 2, "run_id": GONE}]}
    assert lesson_keep_reason(row, DOOMED) == "", "control: the bare-name ref makes it deletable"
    assert DOOMED.name_matched(row) is True

    _write(memory / "lessons.jsonl", [row])
    report = attributable_memory(memory, GONE, GONE_UID)
    assert report["deletable"] == 1 and report["identity"] == "mixed"
    assert report["name_matched"] == 1

    # A ref that names the run by UID decided nothing by name, so it is not disclosed. The
    # disclosure has to be specific enough to be worth reading.
    exact = dict(row, evidence_refs=[{"node_id": 2, "run_id": GONE, "run_uid": GONE_UID}])
    assert lesson_keep_reason(exact, DOOMED) == ""
    assert DOOMED.name_matched(exact) is False
    _write(memory / "lessons.jsonl", [exact])
    assert attributable_memory(memory, GONE, GONE_UID)["identity"] == "run_uid"


def test_the_disclosure_scans_exactly_what_the_deletion_decided_on(memory):
    """`lesson_keep_reason` has always scanned `evidence_refs` unbounded; the disclosure predicate
    stopped at 256. So a uid-less self-reference past that index deleted the row while the receipt
    said `identity: "run_uid"` — the two predicates reading different sets, which makes the
    disclosure worse than none. Both now go through one spelling."""
    refs = [{"node_id": i, "run_id": KEPT, "run_uid": KEPT_UID} for i in range(300)]
    refs.append({"node_id": 300, "run_id": GONE})          # the bare-name self-reference, at 300
    row = {"run_id": GONE, "run_uid": GONE_UID, "task_id": "t", "statement": "x", "outcome": "won",
           "evidence_count": 1, "evidence_refs": refs}
    # Every ref before index 300 names a SURVIVING run by uid, so it is not a self-reference and
    # would keep the row — except that they are refs to another run, which is corroboration.
    assert "other runs" in lesson_keep_reason(row, DOOMED)

    # Make them all this run's own, so the only thing deciding the row is the far-out bare-name ref.
    row["evidence_refs"] = [{"node_id": i, "run_id": GONE, "run_uid": GONE_UID}
                            for i in range(300)] + [{"node_id": 300, "run_id": GONE}]
    assert lesson_keep_reason(row, DOOMED) == "", "control: still deletable"
    assert DOOMED.name_matched(row) is True, (
        "a decision made past index 256 must still be disclosed — the deletion scan saw it")

    _write(memory / "lessons.jsonl", [row])
    assert attributable_memory(memory, GONE, GONE_UID)["identity"] == "mixed"


# ------------------------------------------- the stores that were on no list (review 2026-09-22, ENG3-07)

def test_a_utility_row_is_the_runs_own_measurement_and_goes_with_it():
    """`lesson_utility.jsonl` rows are per (run, lesson) and never merged, so ownership IS the rule —
    including when the lesson measured belongs to a run that survives."""
    from looplab.serve.memory_cascade import utility_keep_reason

    own = {"lesson_id": "les-a", "run_id": GONE, "run_uid": GONE_UID, "shown": 8, "cited": 0}
    theirs = dict(own, run_id=KEPT, run_uid=KEPT_UID)
    assert utility_keep_reason(own, DOOMED) == ""
    assert utility_keep_reason(theirs, DOOMED) == NOT_THIS_RUN


def test_a_seeded_regime_row_is_never_a_runs_to_take(memory):
    """`benchmarks/regime_table.py --seed-ledger` stamps the PROBE directory as `run_id`, no uid, and
    the archived log as `seeded_from`. A run that merely shares the probe's directory name must not
    take it — and to `memory-orphans` every seeded row's "run" is gone by construction, so without
    this rule the first sweep would delete the whole seed."""
    from looplab.serve.memory_cascade import regime_keep_reason

    lived = {"task_id": "t", "direction": "max", "run_id": GONE, "run_uid": GONE_UID,
             "regimes": {"plain": {"n": 3, "median": 1.0, "max": 1.2, "min": 0.8}}, "nodes": 3}
    seeded = {"task_id": "t", "direction": "max", "run_id": GONE,
              "seeded_from": f"/archive/model-probes/{GONE}/runs/r1/run/events.jsonl",
              "regimes": {"jit": {"n": 2, "median": 2.0, "max": 2.5, "min": 1.5}}, "nodes": 2}
    assert regime_keep_reason(lived, DOOMED) == ""
    assert "seeded" in regime_keep_reason(seeded, DOOMED)
    assert regime_keep_reason(dict(lived, run_uid=KEPT_UID), DOOMED) == NOT_THIS_RUN

    _write(memory / "regime_contrast.jsonl", [lived, seeded])
    receipt = purge_attributable_memory(memory, GONE, GONE_UID)
    assert receipt["deleted"] == 1 and receipt["kept"] == 1
    rows = [orjson.loads(l) for l in (memory / "regime_contrast.jsonl").read_bytes().splitlines()
            if l.strip()]
    assert [row.get("seeded_from") for row in rows] == [seeded["seeded_from"]]


def test_the_orphan_survey_sees_every_tier_and_the_sweep_leaves_the_preserved_ones(tmp_path):
    """`orphan_survey` walked the same hand-kept five stores the purge did, so rows a GONE run left in
    `lesson_utility.jsonl` / `regime_contrast.jsonl` were invisible to `looplab memory-orphans` and
    survived every sweep. Now every CASCADED registry store is counted and swept, and every PRESERVED
    one is listed with its reason — and left byte-for-byte alone by the sweep."""
    from looplab.engine.memory_stores import cascaded_tiers, preserved_tiers
    from looplab.serve.memory_cascade import (orphan_survey, purge_orphan_identities,
                                              render_orphan_survey)
    from tests._memory_store_rows import plant_every_store, rows_of, snapshot_preserved

    runs = tmp_path / "runs"
    (runs / KEPT).mkdir(parents=True)
    (runs / KEPT / "events.jsonl").write_text(json.dumps(
        {"seq": 0, "ts": 0, "v": 1, "type": "run_started", "data": {"run_uid": KEPT_UID}}) + "\n")
    store = tmp_path / "memory"
    store.mkdir()
    plant_every_store(store, run_id=GONE, run_uid=GONE_UID, survivor_id=KEPT,
                      survivor_uid=KEPT_UID)
    preserved_before = snapshot_preserved(store)

    survey = orphan_survey(store, runs)
    counted = {entry["file"]: entry for entry in survey["stores"]}
    assert set(counted) == {name for name, _label in cascaded_tiers()}
    assert all(entry["orphan_rows"] == 1 and entry["live_rows"] == 1
               for entry in counted.values()), counted
    assert survey["identities"] == [
        {"run_id": GONE, "run_uid": GONE_UID, "rows": len(cascaded_tiers())}]
    assert [(tier["store"], tier["reason"]) for tier in survey["preserved"]] == list(
        preserved_tiers())
    printed = "\n".join(render_orphan_survey(survey))
    assert all(f"preserved: {reason}" in printed for _group, reason in preserved_tiers())

    receipt = purge_orphan_identities(store, survey["identities"])
    assert receipt["deleted"] == len(cascaded_tiers()) and not receipt["failures"]
    for name, _label in cascaded_tiers():
        assert [row["run_id"] for row in rows_of(store, name)] == [KEPT], name
    assert snapshot_preserved(store) == preserved_before
