"""A metric must carry what it was measured AGAINST, and every ranking surface must refuse across it.

THE INCIDENT THIS IS WRITTEN FROM. `runs/` on this box holds recall@100 values of 0.8776, 0.793426,
0.792082 and 0.774207 and they were compared out loud for a day. Some were measured on one test set
and some on another; the product index also changed, independently of the test set, and a bigger
corpus makes recall@100 strictly harder. **No record said which, and no surface refused.**

Verified 2026-08-20 and it is why the record had to be built rather than read:
  * `node_evaluated.metric_provenance` binds a metric to the artefact that PRODUCED it (path,
    `file_identity`, size, digest). The output side is solid; the input side was empty.
  * `data_provenance` — recorded as SHIPPED, "a sha256 of every task asset" — fires **0 times** in
    all 8 run directories under `runs/` that have an event log. Its gate is `if prov:` over
    `self._assets`, and `adapters/repo_task.py::assets()` returns `{}` for every repo task.
  * `core/comparison.py::ComparisonContract` is the right 13-facet vocabulary and is declared by
    **0** of the task snapshots on this box.
  * `engine/eval_contract.py` calls `e5small-dr-unified-v2` and `-v4` the SAME contract — identical
    command, identical reader, identical declared paths — which is true of the declarations and is
    exactly the pair that cannot be compared.
  * The eval's own two data files carry no version marker, no manifest and no checksum:
    `test.parquet` (62,920,840 B) has only `ARROW:schema` in its parquet metadata, and
    `smkt_all.index.parquet` (37,785,295 B, 641,261 rows) has nothing at all. They are identified by
    PATH ALONE, so replacing either in place is undetectable from every artefact the eval writes.

EVERY TEST BELOW DRIVES THE PROPERTY, not the digest's existence. The refusal tests fold a real
event log and go through the real projection / the real CLI / the real case library.
"""
from __future__ import annotations

import json

import pytest

from looplab.engine.comparability import (AUTHORITY_DECLARED, AUTHORITY_INFERRED,
                                          AUTHORITY_MEASURED, DIFFERENT, SAME, UNKNOWN,
                                          comparability_notice, comparability_record,
                                          comparability_status, group_token, record_of,
                                          run_split_by_key)
from looplab.runtime.metric_inputs import bind_inputs, input_declaration


# The real dense-retrieval declaration, verbatim from the task this box runs, so the fixtures below
# are the shape the mechanism actually meets rather than an invented one.
_TASK = {
    "kind": "repo",
    "editable_path": "/home/jovyan/data/vectorizer-unified",
    "eval": {"command": ["python", "-m", "vectorsearch.test"],
             "metric": {"kind": "stdout_regex", "pattern": "RECALL@100: ([0-9.]+)"}},
}


def _inputs(tmp_path, *, corpus: bytes, testset: bytes = b"queries-v2"):
    """A bound `eval.inputs` record over two files whose CONTENT is the whole identity."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "smkt_all.index.parquet").write_bytes(corpus)
    (tmp_path / "test.parquet").write_bytes(testset)
    return bind_inputs(["smkt_all.index.parquet", "test.parquet"], str(tmp_path))


# --------------------------------------------------------------------------------------------------
# THE INVERSION. An absent key is `unknown` and is never "the same as mine".
# --------------------------------------------------------------------------------------------------

def test_two_records_that_say_nothing_have_not_agreed():
    """The defect in one line. Every row in `runs/` has no key; a rule that defaulted absent-to-equal
    would certify the whole corpus as mutually comparable, which is the false statement being acted
    on. Reflexivity is deliberately NOT assumed — `status(None, None)` is UNKNOWN, not SAME."""
    assert comparability_status(None, None) == UNKNOWN
    assert comparability_status({"authority": "measured", "keys": {"measured": "x"}}, None) == UNKNOWN
    assert comparability_status(None, {"authority": "measured", "keys": {"measured": "x"}}) == UNKNOWN


def test_an_identical_declaration_is_not_evidence_of_an_identical_evaluation(tmp_path):
    """THE PAIR THE WHOLE MODULE EXISTS FOR. `e5small-dr-unified-v2` and `-v4` have byte-identical
    task snapshots — same command, same reader, same editable path — so `eval_contract` calls them
    the same contract and every surface ranked them together. Two runs of THAT task on two different
    corpora must read UNKNOWN, never SAME: the `inferred` authority may refuse and may not certify."""
    left = comparability_record(task=_TASK, inputs_prov=None)
    right = comparability_record(task=dict(_TASK), inputs_prov=None)
    assert left is not None and left["authority"] == AUTHORITY_INFERRED
    assert left["keys"] == right["keys"], "precondition: the two DECLARATIONS really are identical"
    assert comparability_status(left, right) == UNKNOWN, (
        "an inferred match is two task files looking alike, which is what v2 and v4 are")
    assert "COMPARABILITY UNKNOWN" in comparability_notice(left, right, other_run_id="v4")


def test_a_task_with_no_evaluation_identity_gets_no_key_rather_than_an_empty_one():
    """An empty key would compare EQUAL to every other empty key, so "two runs that recorded nothing
    are the same evaluation" would be the mechanism's own first claim. `None` instead — which is the
    same refusal `eval_contract.contract_from_task` makes for the 14 toy/probe snapshots here."""
    assert comparability_record(task={}, inputs_prov=None) is None
    assert comparability_record(task=None, inputs_prov=None) is None
    assert group_token(None) == ""


# --------------------------------------------------------------------------------------------------
# THE MEASURED AUTHORITY. Content, never path.
# --------------------------------------------------------------------------------------------------

def test_a_bigger_product_index_is_a_different_evaluation(tmp_path):
    """THE FACTOR THE OPERATOR NAMED FIRST and the one no declaration carries: the index's SIZE.
    recall@100 over 641,261 documents and recall@100 over more documents are not one quantity, and
    the corpus file says nothing about which it is. Same path, same task, same everything else."""
    small = comparability_record(task=_TASK, inputs_prov=_inputs(tmp_path / "a", corpus=b"i" * 100))
    big = comparability_record(task=_TASK, inputs_prov=_inputs(tmp_path / "b", corpus=b"i" * 400))
    assert small["authority"] == AUTHORITY_MEASURED and big["authority"] == AUTHORITY_MEASURED
    assert comparability_status(small, big) == DIFFERENT
    assert "NOT COMPARABLE" in comparability_notice(small, big, other_run_id="v4")


def test_a_different_test_set_at_the_same_path_is_a_different_evaluation(tmp_path):
    """`test.parquet` carries no version marker of any kind, so "v1 or v2" is not a question any
    artefact can answer — only the bytes can. Identical path, identical name, different content."""
    v1 = comparability_record(task=_TASK,
                              inputs_prov=_inputs(tmp_path / "a", corpus=b"i", testset=b"v1"))
    v2 = comparability_record(task=_TASK,
                              inputs_prov=_inputs(tmp_path / "b", corpus=b"i", testset=b"v2"))
    assert comparability_status(v1, v2) == DIFFERENT


def test_the_same_index_reached_by_two_paths_is_one_evaluation(tmp_path):
    """THE OTHER HALF, and it is why the key may not BE the path. One corpus is routinely reached
    through a mount, a symlink and an absolute path; a key over paths would call that three
    different evaluations while calling two different corpora at one path the same one — wrong in
    both directions on exactly the case it exists for."""
    here = tmp_path / "here"
    there = tmp_path / "there"
    here.mkdir()
    there.mkdir()
    (here / "smkt_all.index.parquet").write_bytes(b"corpus")
    (here / "test.parquet").write_bytes(b"queries")
    (there / "renamed.index.parquet").write_bytes(b"corpus")
    (there / "renamed.test.parquet").write_bytes(b"queries")
    left = comparability_record(task=_TASK, inputs_prov=bind_inputs(
        ["smkt_all.index.parquet", "test.parquet"], str(here)))
    right = comparability_record(task=_TASK, inputs_prov=bind_inputs(
        ["renamed.index.parquet", "renamed.test.parquet"], str(there)))
    assert comparability_status(left, right) == SAME


def test_an_input_may_be_absolute_and_outside_the_workdir(tmp_path):
    """The MIRROR of the subject rule, and it is load-bearing rather than a relaxation: the test set
    and the product index of the task this box runs live at `/home/jovyan/data/dr-local/v2/…`,
    reached through an env var, and are mounted into no workdir at all. A confinement rule copied
    from `metric_subject` would refuse every real input in the corpus."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "corpus.parquet").write_bytes(b"c")
    workdir = tmp_path / "wd"
    workdir.mkdir()
    record = bind_inputs([str(outside / "corpus.parquet")], str(workdir))
    assert record["inputs_bound"] is True, record
    assert record["inputs"][0]["digest"]


def test_an_input_that_did_not_bind_yields_no_key_rather_than_a_weaker_one(tmp_path):
    """A key over a half-bound set would digest "the files we could read", so two runs that each
    failed to read a DIFFERENT file would hash the same material and read SAME. The absence of proof
    has to be UNKNOWN — the whole mechanism is one long argument for that."""
    workdir = tmp_path / "wd"
    workdir.mkdir()
    (workdir / "present.parquet").write_bytes(b"c")
    record = bind_inputs(["present.parquet", "absent.parquet"], str(workdir))
    assert record["inputs_bound"] is False and record["unbound_reason"] == "missing"
    built = comparability_record(task=_TASK, inputs_prov=record)
    assert built["authority"] == AUTHORITY_INFERRED, "it falls back, it does not certify"
    assert AUTHORITY_MEASURED not in built["keys"]


def test_a_declaration_is_filtered_rather_than_trusted():
    """`_grandfathered` reloads a recorded `task.snapshot.json` WITHOUT re-validating it, so a
    non-string entry reaches the binder as `Path(workdir) / 123` — an uncaught TypeError out of the
    eval worker, i.e. a node with no terminal that re-dies on every resume."""
    assert input_declaration({"inputs": ["a", 3, None, "  ", "b"]}) == ["a", "b"]
    assert input_declaration({"inputs": "notalist"}) == []
    assert input_declaration(None) == []


# --------------------------------------------------------------------------------------------------
# THE AUTHORITY LADDER.
# --------------------------------------------------------------------------------------------------

def test_a_measured_record_is_never_ruled_different_from_a_merely_inferred_one(tmp_path):
    """Two runs that recorded DIFFERENT AMOUNTS OF EVIDENCE are not thereby different evaluations.
    Deciding at the strongest SHARED authority is what stops the ladder from manufacturing a refusal
    out of a newer binary — the same false-negative `eval_contract.comparable()` fails open for."""
    measured = comparability_record(task=_TASK, inputs_prov=_inputs(tmp_path / "a", corpus=b"c"))
    inferred = comparability_record(task=_TASK, inputs_prov=None)
    assert measured["authority"] == AUTHORITY_MEASURED
    assert comparability_status(measured, inferred) == UNKNOWN, (
        "they share only `inferred`, which may not certify — and must not manufacture a difference")


def test_an_operator_written_contract_may_certify_where_an_inference_may_not():
    """`ComparisonContract` is a human deliberately asserting 13 facets including `dataset_lineage`.
    That is a claim someone is accountable for, so equality earns SAME; a task snapshot that merely
    LOOKS like another is nobody's claim and earns UNKNOWN. The digest is not re-derived here —
    `core/comparison.py::_bind_contract_id` owns it, and a second hashing of those facets in the
    engine is how two spellings of one identity start disagreeing."""
    contract = {"contract_id": "a" * 64}
    left = comparability_record(task={**_TASK, "comparison_contract": contract}, inputs_prov=None)
    right = comparability_record(task={**_TASK, "comparison_contract": dict(contract)},
                                 inputs_prov=None)
    assert left["authority"] == AUTHORITY_DECLARED
    assert comparability_status(left, right) == SAME
    other = comparability_record(task={**_TASK, "comparison_contract": {"contract_id": "b" * 64}},
                                 inputs_prov=None)
    assert comparability_status(left, other) == DIFFERENT


# --------------------------------------------------------------------------------------------------
# ENFORCEMENT. The refusals, driven through the shipped surfaces.
# --------------------------------------------------------------------------------------------------

def _log(tmp_path, name, nodes, *, task=None):
    """A real run directory. `nodes` is [(node_id, metric, comparability_record_or_None)]."""
    from looplab.events.eventstore import EventStore
    rd = tmp_path / name
    rd.mkdir(parents=True)
    (rd / "task.snapshot.json").write_text(json.dumps(task or _TASK), encoding="utf-8")
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": name, "task_id": "repo_task", "goal": "recall",
                                 "direction": "max"})
    for node_id, metric, record in nodes:
        store.append("node_created", {"node_id": node_id, "parent_ids": [], "operator": "draft",
                                      "idea": {"operator": "draft", "params": {},
                                               "rationale": "seed"}, "code": "pass\n"})
        payload = {"node_id": node_id, "generation": 0, "metric": metric, "violations": []}
        if record is not None:
            payload["metric_provenance"] = {"comparability": record}
        store.append("node_evaluated", payload)
    return rd


def test_a_run_whose_own_nodes_were_measured_differently_caveats_its_champion(tmp_path):
    """THE WITHIN-RUN REFUSAL, through the real fold and the real `/api/runs` projection. A selector
    that ordered 0.79 measured on one test set against 0.77 measured on another has not chosen the
    better model, and until this member no rung in the tree could say so."""
    pytest.importorskip("fastapi")
    from looplab.engine.champion_caveats import CHAMPION_CAVEAT_MIXED_COMPARABILITY
    from looplab.serve import run_projections
    from looplab.serve.server import make_app

    a = {"version": 1, "authority": "measured", "keys": {"measured": "1111111111111111"}}
    b = {"version": 1, "authority": "measured", "keys": {"measured": "2222222222222222"}}
    _log(tmp_path, "mixed", [(0, 0.79, a), (1, 0.77, b)])
    _log(tmp_path, "clean", [(0, 0.79, a), (1, 0.77, dict(a))])
    srv = make_app(tmp_path).state.looplab
    rows = {row["run_id"]: row for row in run_projections.run_summaries(srv)}
    assert CHAMPION_CAVEAT_MIXED_COMPARABILITY in rows["mixed"]["best_metric_caveats"]
    assert rows["clean"]["best_metric_caveats"] == [], "one key across the field is not news"
    assert rows["mixed"]["best_metric"] == 0.79, "the value is untouched — this qualifies, it never moves a number"
    assert rows["mixed"]["best_metric_comparability"] == a, (
        "the row publishes the CHAMPION's key, so thirteen browser surfaces need no fix of their own")


def test_an_unkeyed_corpus_gains_no_caveat(tmp_path):
    """THE NEGATIVE CONTROL, and it is the whole reason the within-run member fires on DIFFERENT and
    never on UNKNOWN. Every node of every run on this box has no key; a member that caveated silence
    would fire on all 46 run directories and therefore mean nothing."""
    pytest.importorskip("fastapi")
    from looplab.serve import run_projections
    from looplab.serve.server import make_app

    _log(tmp_path, "legacy", [(0, 0.79, None), (1, 0.77, None)])
    srv = make_app(tmp_path).state.looplab
    row = next(r for r in run_projections.run_summaries(srv) if r["run_id"] == "legacy")
    assert row["best_metric_caveats"] == []
    assert row["best_metric_comparability"] is None, "absent, never an empty key that would compare equal"


def test_the_cli_refuses_a_cross_key_ranking_and_exits_nonzero(tmp_path):
    """THE CROSS-RUN REFUSAL, driven end to end through the shipped command. This is the surface the
    operator actually uses to ask "which of these numbers is better", and the answer for two runs on
    two corpora has to be a refusal with a non-zero exit — not a smaller number in a list."""
    from typer.testing import CliRunner

    from looplab.cli import app

    a = {"version": 1, "authority": "measured", "keys": {"measured": "1111111111111111"}}
    b = {"version": 1, "authority": "measured", "keys": {"measured": "2222222222222222"}}
    left = _log(tmp_path, "corpusA", [(0, 0.793426, a)])
    right = _log(tmp_path, "corpusB", [(0, 0.774207, b)])
    same = _log(tmp_path, "corpusA2", [(0, 0.792082, dict(a))])

    refused = CliRunner().invoke(app, ["comparability", str(left), str(right)])
    assert refused.exit_code == 3, refused.output
    assert "DIFFERENT" in refused.output and "NOT COMPARABLE" in refused.output
    assert "0.793426" in refused.output and "0.774207" in refused.output, (
        "the values are still SHOWN — each is true of its own measurement; only the ordering is refused")

    allowed = CliRunner().invoke(app, ["comparability", str(left), str(same)])
    assert allowed.exit_code == 0, allowed.output
    assert "SAME evaluation" in allowed.output

    unknown = CliRunner().invoke(app, ["comparability", str(_log(tmp_path, "old", [(0, 0.8776, None)])),
                                       str(left)])
    assert unknown.exit_code == 4, (
        "UNKNOWN is not success: a caller that asked for a ranking did not get one, and the one "
        "thing this command may never do is let silence read as yes")


def test_the_case_library_elects_a_champion_within_one_evaluation_only(tmp_path):
    """THE WARM START — the memory tier whose whole job is handing a PREVIOUS run's number to the
    NEXT one. `_add_locked` grouped on `(task_id, direction)` alone, so a case measured on one test
    set could be elected `active` over a case measured on another and its `params` then reached the
    new run's prompt as the configuration to beat."""
    from looplab.engine.memory import JsonlCaseLibrary

    path = tmp_path / "cases.jsonl"
    lib = JsonlCaseLibrary(path)
    base = {"task_id": "repo_task", "direction": "max", "params": {"lr": 1e-3}, "goal": "g"}
    lib.add({**base, "run_uid": "u1", "metric": 0.77, "comparability": "measured:aaaa"})
    lib.add({**base, "run_uid": "u2", "metric": 0.88, "comparability": "measured:bbbb"})
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    active = {row["comparability"]: row["active"] for row in rows}
    assert active == {"measured:aaaa": True, "measured:bbbb": True}, (
        "0.88 on ANOTHER corpus does not depose 0.77 — each evaluation elects its own champion")


def test_the_existing_case_store_elects_exactly_as_it_did(tmp_path):
    """THE INERTNESS PROOF for the durable store, which is the one place this change could have
    rewritten history. Every case row already written carries no key, so they all share the `""`
    partition and the election is byte-for-byte the pre-existing one."""
    from looplab.engine.memory import JsonlCaseLibrary

    path = tmp_path / "cases.jsonl"
    lib = JsonlCaseLibrary(path)
    base = {"task_id": "repo_task", "direction": "max", "params": {}, "goal": "g"}
    lib.add({**base, "run_uid": "u1", "metric": 0.77})
    lib.add({**base, "run_uid": "u2", "metric": 0.88})
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert {row["run_uid"]: row["active"] for row in rows} == {"u1": False, "u2": True}


def test_run_split_by_key_is_silent_about_silence():
    """The primitive the within-run member is written on. An absent key is not a second key."""
    class _N:
        def __init__(self, provenance):
            self.metric_provenance = provenance

    a = {"version": 1, "authority": "measured", "keys": {"measured": "aaaa"}}
    b = {"version": 1, "authority": "measured", "keys": {"measured": "bbbb"}}
    assert run_split_by_key([_N(None), _N(None)]) is False
    assert run_split_by_key([_N({"comparability": a}), _N(None)]) is False
    assert run_split_by_key([_N({"comparability": a}), _N({"comparability": b})]) is True
    assert run_split_by_key([_N("not a dict"), _N({"comparability": a})]) is False
    assert record_of(_N({"comparability": {"keys": {}}})) is None, "an empty key map is no key"
