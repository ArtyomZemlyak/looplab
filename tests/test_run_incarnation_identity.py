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
    """MUTATION: key `dedup_valid_capsules` on `run_id` -> `duplicates` is 1, `source_complete`
    goes False, and the portfolio prints PARTIAL while withholding the steward's actions."""
    from looplab.engine.concept_capsules import dedup_valid_capsules

    def _capsule(uid):
        return _valid(uid, 0.5)

    rows = dedup_valid_capsules([_capsule(_UID_A), _capsule(_UID_B)])

    assert len(rows) == 2, "two incarnations collapsed into one capsule"
    assert rows.source_health["source_duplicate_run_rows"] == 0, (
        "and neither was reported as a duplicate, which is what flips `source_complete`")


def test_a_concept_keeps_a_run_row_per_incarnation():
    """The `_runs` map was keyed by name, so one incarnation overwrote the other and the concept
    lost a run. `run_id` stays on the ROW for display — this module's own prescription."""
    from looplab.engine.concept_capsules import portfolio_concept_overview_data

    overview, _rows = portfolio_concept_overview_data(
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


# --- the three readers doc 50 EK-03 named (doc 52 row 4, 2026-09-06) -----------------------------

def test_evidence_refs_and_claim_groups_qualify_by_incarnation():
    """`_qualify_refs` keyed evidence by NAME, so two incarnations' node 0 were one ref and two
    supporting lessons counted as one support. MUTATION: qualify by `run_id` again -> `n_support`
    below is 1 and `run_refs` collapses to the name."""
    from looplab.engine.claims import claim_assessments
    from looplab.engine.claims_health import _qualify_refs

    assert _qualify_refs({"run_id": "demo", "run_uid": _UID_A}, [0]) != \
        _qualify_refs({"run_id": "demo", "run_uid": _UID_B}, [0])
    assert _qualify_refs({"run_id": "demo"}, [0]) == ["demo:0"], "a uid-less row keeps its spelling"
    assert _qualify_refs({"run_id": "demo", "run_uid": _UID_A}, [0]) == [f"demo@{_UID_A}:0"]

    def _lesson(uid):
        return {"statement": "hard negatives help", "outcome": "supported", "evidence": [0],
                "run_id": "demo", "run_uid": uid, "task_id": "t"}

    [claim] = claim_assessments([_lesson(_UID_A), _lesson(_UID_B)])
    assert claim["n_support"] == 2, "two incarnations' evidence collapsed into one ref"
    assert claim["runs"] == ["demo"], "the display list still shows the directory name"
    assert claim["run_refs"] == sorted([_UID_A, _UID_B])
    [legacy] = claim_assessments([{**_lesson(_UID_A), "run_uid": ""}])
    assert legacy["run_refs"] == [f"{LEGACY_REF_PREFIX}demo"]


def test_the_atlas_counts_two_incarnations_as_two_runs():
    """`portfolio_atlas` unioned capsule `run_id`s with lesson run names. MUTATION: key on names
    again -> `n_runs` is 1 for two incarnations of `demo`."""
    from looplab.engine.claims_retrieval import portfolio_atlas

    atlas = portfolio_atlas([], [_valid(_UID_A, 0.5), _valid(_UID_B, 0.9)])
    assert atlas["n_runs"] == 2 and atlas["context_pack"]["coverage"]["n_runs"] == 2
    # …and a lesson-only memory still counts its runs, by incarnation where it has one.
    # A real assertion, not a placeholder: the atlas projects with the structured claim key by
    # default, where a string carrying no subject/relation is not a claim at all.
    lessons = [{"statement": "hard negatives help recall", "outcome": "supported", "evidence": [0],
                "run_id": "demo", "run_uid": uid, "task_id": "t"} for uid in (_UID_A, _UID_B)]
    assert portfolio_atlas(lessons, [])["n_runs"] == 2
    # A capsule and a lesson from the SAME incarnation are one run, not two.
    assert portfolio_atlas(lessons[:1], [_valid(_UID_A, 0.5)])["n_runs"] == 1


def test_the_concept_shelf_inherits_from_the_incarnation_that_wrote_the_row():
    """`run_concept_index` was keyed by name and `attribute_row` looked rows up by name, so a row
    written by a DELETED incarnation of `demo` inherited the live `demo`'s concepts. MUTATION: drop
    the uid key -> the gone-incarnation case below inherits the wrong run's tags."""
    from looplab.engine.concept_shelf import ATTRIBUTION_RUN, attribute_row, run_concept_index

    index = run_concept_index([{"run_id": "demo", "run_uid": _UID_A,
                                "concepts": {"loss/contrastive": {"count": 1}}}])
    assert index["demo"] == ["loss/contrastive"] and index[_UID_A] == ["loss/contrastive"]
    live = attribute_row({"statement": "s", "run_id": "demo", "run_uid": _UID_A}, index)
    assert live == (["loss/contrastive"], ATTRIBUTION_RUN)
    gone = attribute_row({"statement": "s", "run_id": "demo", "run_uid": _UID_B}, index)
    assert gone == ([], None), "a deleted incarnation's row must not inherit the live run's tags"
    legacy = attribute_row({"statement": "s", "run_id": "demo"}, index)
    assert legacy == (["loss/contrastive"], ATTRIBUTION_RUN), "a uid-less row still falls back to the name"


# --- the live builders and the D8 writer (review 2026-09-22, ENG3-01) ----------------------------

def _lesson_row(uid: str) -> dict:
    return {"statement": "hard negatives help recall", "outcome": "supported", "evidence": [1],
            "run_id": "demo", "run_uid": uid, "task_id": "t", "direction": "max"}


@pytest.mark.parametrize("row_uid,expected", [
    (_UID_B, 1),   # another run ROOT that shares this run's directory name: prior evidence
    (_UID_A, 0),   # this incarnation's own row: never "prior"
])
def test_both_live_builders_exclude_this_incarnation_not_this_name(tmp_path, row_uid, expected):
    """The Strategist note and the Researcher pack both excluded "this run" by `run_id` — the
    directory NAME — so another root's `demo` was withheld from both prompts while the agent's own
    bound `cross_run_*` tools (`LessonScope.is_current_run`) showed it one call away. MUTATION:
    compare `run_id` again in `cross_run_context.visible_row_predicate` (Strategist) or in
    `proposal_cues._cross_run_advisory_text` (Researcher) -> the `_UID_B` case reads 0.

    This is the one prompt-content change the fix makes, and ONLY in the name-collision case: a
    row with its own uid under this run's name used to be hidden and is now shown."""
    import orjson

    from looplab.core.models import RunState
    from looplab.engine.proposal_cues import ProposalCuesMixin
    from looplab.engine.strategy import StrategyCadenceMixin

    (tmp_path / "lessons.jsonl").write_bytes(orjson.dumps(_lesson_row(row_uid)) + b"\n")

    class _Proposal(ProposalCuesMixin):
        _cross_run_advisory = True

        def __init__(self):
            self.memory_dir = str(tmp_path)

    class _Strategy(StrategyCadenceMixin):
        _cross_run_advisory = True

        def __init__(self):
            self.memory_dir = str(tmp_path)

    state = RunState(run_id="demo", run_uid=_UID_A, task_id="t", direction="max")
    proposal, strategy = _Proposal(), _Strategy()
    proposal._cross_run_advisory_text(state)
    strategy._cross_run_note_for_ctx(state)

    assert proposal._cross_run_advisory_receipt.get("n_lessons", 0) == expected, (
        proposal._cross_run_advisory_receipt)
    assert strategy._cross_run_note_receipt.get("n_lessons", 0) == expected, (
        strategy._cross_run_note_receipt)


def _research_rows(path) -> list[dict]:
    import json
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def test_a_d8_refinalize_replaces_only_its_own_incarnations_claims(tmp_path):
    """`record_research_claims` retires "this run's" rows before writing the new snapshot. Folded
    onto `row_belongs_to_run`, a row carrying a uid is matched on that uid alone — the behaviour the
    hand-rolled predicate already had, which this pins across the fold. MUTATION: match the name
    for a uid-bearing row -> incarnation A's claim is retired by B's re-finalize."""
    from looplab.engine.claims import record_research_claims

    def _write(uid, statement):
        return record_research_claims(
            tmp_path, run_id="demo", run_uid=uid, task_id="t", direction="max",
            claims=[{"statement": statement, "node_ids": [1]}])

    assert _write(_UID_A, "incarnation A found X") == 1
    assert _write(_UID_B, "incarnation B found Y") == 1
    assert _write(_UID_B, "incarnation B re-finalized: Z") == 1
    rows = _research_rows(tmp_path / "research_claims.jsonl")
    by_uid = {(r.get("run_uid"), r.get("statement")) for r in rows}
    assert (_UID_A, "incarnation A found X") in by_uid, "another incarnation's claim was retired"
    assert (_UID_B, "incarnation B re-finalized: Z") in by_uid
    assert (_UID_B, "incarnation B found Y") not in by_uid, "B's own superseded claim must go"


def test_a_uid_less_d8_row_of_the_same_name_is_attributed_to_the_uid_bearing_run(tmp_path):
    """THE ONE STATED DIFFERENCE of the fold. The hand-rolled predicate never let a uid-bearing
    re-finalize retire a UID-LESS row; `row_belongs_to_run` attributes such a row to its directory
    name — the rule `serve/memory_cascade.py` already DELETES research claims on, and the widening
    `ConceptCapsuleStore.add` accepted in the same words. A different name is still untouched."""
    import json

    from looplab.engine.claims import record_research_claims

    legacy = {"statement": "pre-uid claim", "run_id": "demo", "task_id": "t",
              "direction": "max", "node_ids": [1]}
    elsewhere = {**legacy, "statement": "another run's pre-uid claim", "run_id": "other"}
    path = tmp_path / "research_claims.jsonl"
    path.write_text(json.dumps(legacy) + "\n" + json.dumps(elsewhere) + "\n", encoding="utf-8")

    record_research_claims(tmp_path, run_id="demo", run_uid=_UID_A, task_id="t",
                           direction="max", claims=[{"statement": "current", "node_ids": [1]}])
    statements = {r.get("statement") for r in _research_rows(path)}
    assert "pre-uid claim" not in statements
    assert {"another run's pre-uid claim", "current"} <= statements


# --- ONE attribution rule, and nothing re-spells it by name (review 2026-09-22, ENG3-01) ---------
#
# "Is this row this run's?" was decided in nine places and three compared only `run_id`, the
# directory NAME — one of them deleted another incarnation's lessons from the shared store. The rule
# has two homes: `core/run_identity.py::row_belongs_to_run` (attribution) and
# `trust/cross_run.py::LessonScope.is_current_run` (the read-side scope). This guard refuses a new
# comparison of a row's `run_id` in `engine/`, `core/` or `trust/` outside the entries below, each of
# which says why it may still spell the name itself; a fixed site must LEAVE the list (shrink-only).
# AST, not substrings: a comment cannot satisfy or trip it.

_NAME_COMPARISON_ALLOWED = {
    ("core/run_identity.py", "row_belongs_to_run"):
        "the attribution rule's home: the uid-less legacy fallback to the name IS the rule",
    ("trust/cross_run.py", "LessonScope.is_current_run"):
        "the read-side scope's home: the legacy fallback when either side names no incarnation",
    ("engine/lessons.py", "LessonMemory.store_concept_capsule"):
        "NOT folded: its uid-less-caller branch matches uid-BEARING rows by name, so "
        "`row_belongs_to_run` would change behaviour for rows that carry a run_uid",
    ("engine/concept_capsules.py", "ConceptCapsuleStore.prior_capsules"):
        "NOT folded, same reason: a uid-less caller excludes uid-BEARING rows of its name",
    ("engine/lessons_distill.py", "LessonDistillMixin.write_reflection_note"):
        "NOT folded: the meta-note crash-retry de-dup must also match a run that recorded NO name "
        "(a log with no `run_started`), which `row_belongs_to_run` refuses to attribute",
    ("engine/curation_protocol.py", "CurationProtocolMixin._legacy_curation_terminal"):
        "curation rows carry no run_uid in any generation; the v1 bridge's name is its only key",
}
_SCANNED = ("engine", "core", "trust")


def _reads_row_run_id(node) -> bool:
    import ast

    for sub in ast.walk(node):
        if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)
                and sub.func.attr == "get" and sub.args
                and isinstance(sub.args[0], ast.Constant) and sub.args[0].value == "run_id"):
            return True
        if (isinstance(sub, ast.Subscript) and isinstance(sub.slice, ast.Constant)
                and sub.slice.value == "run_id"):
            return True
    return False


def _name_comparisons(pkg) -> dict[tuple[str, str], list[int]]:
    """`{(path under the package, enclosing qualname): [line, ...]}` for every `==`/`!=`/`in`/
    `not in` comparison one of whose operands reads a row's `run_id`."""
    import ast

    from tests._source_scan import iter_trees

    found: dict[tuple[str, str], list[int]] = {}

    def visit(node, scope, rel):
        for child in ast.iter_child_nodes(node):
            inner = scope
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                inner = (*scope, child.name)
            if (isinstance(child, ast.Compare)
                    and any(isinstance(op, (ast.Eq, ast.NotEq, ast.In, ast.NotIn))
                            for op in child.ops)
                    and any(_reads_row_run_id(side) for side in (child.left, *child.comparators))):
                found.setdefault((rel, ".".join(scope)), []).append(child.lineno)
            visit(child, inner, rel)

    for sub in _SCANNED:
        if not (pkg / sub).is_dir():
            continue
        for path, tree in iter_trees(pkg / sub):
            visit(tree, (), path.relative_to(pkg).as_posix())
    return found


def test_no_reader_or_writer_re_spells_run_attribution_by_name():
    from tests._source_scan import PKG

    found = _name_comparisons(PKG)
    unexpected = {site: lines for site, lines in found.items()
                  if site not in _NAME_COMPARISON_ALLOWED}
    assert not unexpected, (
        "a row's `run_id` is compared directly — the directory NAME deciding which run a row is. "
        "Use `core/run_identity.py::row_belongs_to_run` (attribution) or "
        f"`trust/cross_run.py::LessonScope.is_current_run` (read-side scope): {unexpected}")
    stale = sorted(set(_NAME_COMPARISON_ALLOWED) - set(found))
    assert not stale, f"these sites no longer compare a name; delete their allow-list rows: {stale}"


def test_the_name_comparison_guard_sees_the_shape_it_refuses(tmp_path):
    """The guard's own teeth, on a throwaway tree: the exact line ENG3-01 found, plus the two
    respellings a fix could drift to, are each reported; the fixed spelling is not."""
    engine = tmp_path / "engine"
    engine.mkdir()
    (engine / "m.py").write_text(
        "def stale(o, state):\n"
        "    return o.get('run_id') != state.run_id\n"
        "def wrapped(row, rid):\n"
        "    return str(row.get('run_id') or '') == rid\n"
        "def member(row, names):\n"
        "    return row['run_id'] in names\n"
        "def fixed(row, state):\n"
        "    # o.get('run_id') != state.run_id  -- a comment cannot trip it\n"
        "    from looplab.core.run_identity import row_belongs_to_run\n"
        "    return row_belongs_to_run(row, run_uid=state.run_uid, run_id=state.run_id)\n",
        encoding="utf-8")
    assert set(_name_comparisons(tmp_path)) == {
        ("engine/m.py", "stale"), ("engine/m.py", "wrapped"), ("engine/m.py", "member")}
