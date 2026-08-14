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
    # The two never-cascaded tiers are stated, not left for the operator to infer from silence.
    assert {p["store"] for p in report["preserved"]} == {"skills", "curation_logs"}


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
