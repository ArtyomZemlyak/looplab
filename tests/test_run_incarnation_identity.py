"""Two runs that share a directory NAME are two runs, at every reader that has to tell.

`run_id` is the run directory name (`orchestrator.py`: `self.run_dir.name`). It is reused the moment
a run is deleted and re-created, it is `demo`/`baseline` on half the corpus, and it is identical
across two checkouts sharing the default `~/.looplab/memory`. `engine/concept_capsules.py` states
the rule outright — "`run_id` is only a run-root-local label… key by a persisted globally unique
run-incarnation UID" — and every WRITER already records `run_uid`.

Three READERS did not, and the same mistake produced three different failures. This file drives one
two-incarnation fixture per store, because the collapse is invisible to a single-incarnation test:
every assertion below passes trivially when only one run named `demo` exists.

  * `lessons_reconcile` retired a PREVIOUS incarnation's lesson under the lock, because it could not
    match the new run's evidence signature and was therefore judged stale.
  * `concept_capsules` counted two incarnations as a duplicate, so the portfolio reported one run
    and `source_complete: False` — which withholds the profit tendencies, forbids the steward's
    splits and purges, and prints PARTIAL on every surface.
  * `claims_health` merged two complete v3 receipt row sets into one group whose retained count
    could not match, so `producer_receipt_known` went False and every one-sided verdict was demoted
    to `inconclusive` portfolio-wide.

The asymmetry the shared rule keeps is `serve/memory_cascade.py::RunIdentity`'s, worked out first
for the destructive path: a row carrying a uid is matched ONLY on that uid, while a row carrying
none falls back to the NAME even for a uid-bearing caller — otherwise every row written before
`run_uid` existed becomes permanently unattributable.
"""
from __future__ import annotations

import pytest

from looplab.core.run_identity import (LEGACY_REF_PREFIX, row_belongs_to_run, run_ref,
                                       run_ref_is_legacy)

_UID_A = "11111111-1111-4111-8111-111111111111"
_UID_B = "22222222-2222-4222-8222-222222222222"
_CONCEPT = "training/negative-mining"


def _valid(uid: str, metric: float) -> dict:
    """A capsule the store's own validator accepts, differing ONLY in incarnation."""
    from looplab.engine.concept_capsules import (CONCEPT_CAPSULE_VERSION,
                                                 NODE_CONCEPT_PROVENANCE_CLASSIFIER)
    return {
        "v": CONCEPT_CAPSULE_VERSION,
        "concept_evidence": NODE_CONCEPT_PROVENANCE_CLASSIFIER,
        "run_id": "demo", "run_uid": uid, "task_id": "t",
        "direction": "max", "best_metric": metric,
        "fingerprint": ["f"], "concepts": [_CONCEPT],
        "concept_outcomes": {_CONCEPT: metric},
    }


# --- the rule itself ----------------------------------------------------------------------------

def test_two_incarnations_of_one_name_are_two_refs():
    """MUTATION: return `run_id` -> every collapse below comes back at once."""
    a = run_ref({"run_id": "demo", "run_uid": _UID_A})
    b = run_ref({"run_id": "demo", "run_uid": _UID_B})

    assert a != b, "two incarnations of one directory name must not share a grouping key"
    assert a == _UID_A and b == _UID_B


def test_a_row_that_names_no_incarnation_groups_under_its_name():
    """The best that can be said about it — and deliberately NOT merged with a uid-bearing row of
    the same name, which is the collapse this exists to end."""
    legacy = run_ref({"run_id": "demo"})

    assert legacy == f"{LEGACY_REF_PREFIX}demo" and run_ref_is_legacy(legacy)
    assert legacy != run_ref({"run_id": "demo", "run_uid": _UID_A})


def test_a_row_saying_nothing_gets_no_identity_rather_than_a_shared_bucket():
    assert run_ref({}) == ""
    assert run_ref({"run_uid": "  "}) == ""


@pytest.mark.parametrize("row,uid,name,expected,why", [
    ({"run_id": "demo", "run_uid": _UID_B}, _UID_A, "demo", False,
     "a row that names its incarnation is matched ONLY on that"),
    ({"run_id": "demo", "run_uid": _UID_A}, _UID_A, "demo", True, "same incarnation"),
    ({"run_id": "demo"}, _UID_A, "demo", True,
     "a uid-less row falls back to the NAME even for a uid-bearing caller, or rows written before "
     "run_uid existed become unattributable forever"),
    ({"run_id": "other"}, _UID_A, "demo", False, "a different name is a different run"),
])
def test_attribution_keeps_the_cascade_asymmetry(row, uid, name, expected, why):
    """NOT `run_ref` equality — `row_belongs_to_run` is a separate function precisely because its
    failure mode differs, and a caller must pick the one it can live with.

    MUTATION: implement it as `run_ref(row) == run_ref(uid, name)` -> the legacy case flips and
    every pre-`run_uid` row silently stops being attributed to the run that wrote it.
    """
    assert row_belongs_to_run(row, run_uid=uid, run_id=name) is expected, why


# --- one two-incarnation fixture per store ------------------------------------------------------

def test_capsule_readers_do_not_report_two_incarnations_as_a_duplicate():
    """MUTATION: key `_dedup_valid_capsules` on `run_id` -> `duplicates` is 1, `source_complete`
    goes False, and the portfolio prints PARTIAL while withholding the steward's actions."""
    from looplab.engine.concept_capsules import _dedup_valid_capsules

    def _capsule(uid):
        return _valid(uid, 0.5)

    rows = _dedup_valid_capsules([_capsule(_UID_A), _capsule(_UID_B)])

    assert len(rows) == 2, "two incarnations collapsed into one capsule"
    assert rows.source_health["source_duplicate_run_rows"] == 0, (
        "and neither was reported as a duplicate, which is what flips `source_complete`")


def test_a_concept_keeps_a_run_row_per_incarnation():
    """The `_runs` map was keyed by name, so one incarnation overwrote the other and the concept
    lost a run. `run_id` stays on the ROW for display — this module's own prescription."""
    from looplab.engine.concept_capsules import _portfolio_concept_overview_data

    overview, _rows = _portfolio_concept_overview_data(
        [_valid(_UID_A, 0.5), _valid(_UID_B, 0.9)])
    entry = next(c for c in overview["concepts"] if c["concept"] == _CONCEPT)

    assert overview["n_runs"] == 2, "the portfolio counted two incarnations as one run"
    assert entry["n_runs"] == 2, (
        f"one incarnation overwrote the other in the concept's run map: {entry}")
    assert sorted(r["metric"] for r in entry["runs"]) == [0.5, 0.9], (
        "both incarnations' outcomes must survive; keyed on the name one is simply lost")
    assert {r["run_id"] for r in entry["runs"]} == {"demo"}, (
        "the NAME is still what is DISPLAYED — the uid is the key, not the label")


def test_claim_receipt_groups_are_per_incarnation():
    """MUTATION: group on `run_id` -> two complete v3 row sets merge, the retained count cannot
    match its recorded cardinality, and `producer_receipt_known` goes False portfolio-wide."""
    from looplab.engine.claims_health import _research_source_summary

    def _rows(uid):
        return [{"v": 3, "run_id": "demo", "run_uid": uid, "claim": f"c-{uid[:4]}",
                 "node_ids": [1], "source_total": 1, "source_omitted": 0}]

    merged = _research_source_summary(_rows(_UID_A) + _rows(_UID_B))
    apart = _research_source_summary(_rows(_UID_A))

    assert merged.get("unknown", 0) == apart.get("unknown", 0), (
        f"merging two incarnations' receipts changed the summary: {merged} vs {apart}")


def test_reconcile_leaves_another_incarnations_lesson_alone():
    """The expensive one: a previous incarnation's lesson cannot match the new run's evidence
    signature, so keyed on the name it was judged stale and RETIRED under the lock.

    Driven at the predicate the loop consults, because the retirement itself needs a whole engine.
    MUTATION: compare `run_id` -> the first assertion flips and the lesson is retired.
    """
    mine = {"run_id": "demo", "run_uid": _UID_A, "source": "reflect"}
    theirs = {"run_id": "demo", "run_uid": _UID_B, "source": "reflect"}
    legacy = {"run_id": "demo", "source": "reflect"}

    assert not row_belongs_to_run(theirs, run_uid=_UID_A, run_id="demo"), (
        "another incarnation's lesson is not this run's to retire")
    assert row_belongs_to_run(mine, run_uid=_UID_A, run_id="demo")
    assert row_belongs_to_run(legacy, run_uid=_UID_A, run_id="demo"), (
        "a lesson written before run_uid existed is still this run's")


def test_a_run_with_no_uid_still_reconciles_its_own_lessons():
    """The regression this change could most easily cause: an offline/toy run records no uid, and
    keying strictly on one would make it unable to reconcile anything it wrote."""
    assert row_belongs_to_run({"run_id": "demo"}, run_uid="", run_id="demo")
    assert not row_belongs_to_run({"run_id": "other"}, run_uid="", run_id="demo")
