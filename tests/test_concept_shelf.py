"""The CONCEPT SHELF: concepts as a navigational axis over cross-run knowledge, and — the property that
matters more than the feature — an HONEST one.

A concept filter that silently returns nothing is worse than no filter, because an operator reads the
empty result as "this lab learned nothing about that" when the truth is "nothing was ever tagged". So
most of these tests are about what the shelf REFUSES to claim: it never invents a concept from text, it
never launders run-level inheritance into a record-level tag, and it always states its own coverage.
"""
from __future__ import annotations

import json

import orjson
import pytest

from looplab.core.models import Idea, Node, NodeStatus, RunState
from looplab.engine.concept_shelf import (
    ATTRIBUTION_RECORD, ATTRIBUTION_RUN, ATTRIBUTION_SOURCES, attribute_row, attribute_rows,
    bounded_row_concepts, build_shelf, run_concept_index, shelf_coverage, shelf_tree, state_concepts,
)
from looplab.events.digest import concept_rollup, folded_concepts, theme_rollup


def _state(direction="min", **kwargs):
    return RunState(task_id="t", goal="g", direction=direction, **kwargs)


def _node(state, nid, concepts, *, metric=None, theme=None, tombstoned=False):
    idea = Idea(operator="draft", summary="s", params={}, **({"theme": theme} if theme else {}))
    node = Node(id=nid, parent=None, idea=idea, operator="draft", status=NodeStatus.evaluated,
                metric=metric, tombstoned=tombstoned)
    state.nodes[nid] = node
    if concepts is not None:
        state.node_concepts[nid] = list(concepts)
    return node


# --------------------------------------------------------------------------- the run-level rollup

def test_concept_rollup_keys_whole_ids_where_theme_rollup_keys_axes():
    """The reason a second rollup exists at all: `themes` cannot answer a concept question."""
    state = _state()
    _node(state, 1, ["loss/contrastive/in-batch"], metric=1.0)
    _node(state, 2, ["loss/contrastive/dcl", "retrieval/dense"], metric=0.5)
    assert set(theme_rollup(state)) == {"loss", "retrieval"}          # axis-truncated
    assert set(concept_rollup(state)) == {
        "loss/contrastive/in-batch", "loss/contrastive/dcl", "retrieval/dense"}
    # the metric follows the run's direction, per-concept
    assert concept_rollup(state)["retrieval/dense"] == {"count": 1, "best_metric": 0.5}
    assert concept_rollup(state)["loss/contrastive/in-batch"]["count"] == 1


def test_concept_rollup_has_no_legacy_theme_fallback_so_empty_means_untagged():
    """`theme_rollup` backfills from `idea.theme`; this rollup must not, or "untagged" is unstatable.

    This is THE property the whole shelf's honesty rests on. If a pre-concept run reported concepts
    derived from its legacy theme slug, every coverage receipt downstream would over-report, and the
    Memory panel would claim attribution it does not have.
    """
    state = _state()
    _node(state, 1, None, metric=1.0, theme="optimization")           # legacy: no folded entry at all
    assert theme_rollup(state) == {"optimization": {"count": 1, "best_metric": 1.0}}
    assert concept_rollup(state) == {}, "a legacy theme is not a concept"


def test_concept_rollup_treats_an_explicit_empty_membership_as_untagged():
    state = _state()
    _node(state, 1, [], metric=1.0, theme="optimization")   # deliberately cleared by an operator retag
    assert concept_rollup(state) == {}
    assert folded_concepts(state, state.nodes[1]) == set()


def test_concept_rollup_excludes_tombstoned_and_aborted_nodes():
    state = _state()
    _node(state, 1, ["a/keep"], metric=1.0)
    _node(state, 2, ["b/tombstoned"], metric=0.1, tombstoned=True)
    _node(state, 3, ["c/aborted"], metric=0.1)
    state.aborted_nodes.append(3)
    assert set(concept_rollup(state)) == {"a/keep"}


def test_concept_rollup_is_bounded_and_deterministic():
    from looplab.events.digest import MAX_ROLLUP_CONCEPTS
    state = _state()
    for nid in range(MAX_ROLLUP_CONCEPTS + 20):
        _node(state, nid, [f"axis/c{nid:04d}"], metric=float(nid))
    rollup = concept_rollup(state)
    assert len(rollup) == MAX_ROLLUP_CONCEPTS
    # lexically-lowest retained, so two polls of the same state agree regardless of hash seed
    assert set(rollup) == {f"axis/c{nid:04d}" for nid in range(MAX_ROLLUP_CONCEPTS)}


# --------------------------------------------------------------------------- write-side attribution

def test_state_concepts_scopes_to_the_given_evidence_nodes():
    """A lesson records the concepts of the experiments it describes, not everything the run touched."""
    state = _state()
    _node(state, 1, ["loss/contrastive"], metric=1.0)
    _node(state, 2, ["data/augmentation"], metric=2.0)
    assert state_concepts(state, [1]) == ["loss/contrastive"]
    assert state_concepts(state, [1, 2]) == ["data/augmentation", "loss/contrastive"]
    assert state_concepts(state) == ["data/augmentation", "loss/contrastive"]   # None = run-wide


def test_state_concepts_returns_empty_rather_than_widening_to_the_run():
    """The refusal that keeps `record` a stronger claim than `run`.

    Widening an untagged lesson's scope to the run would publish a run-level guess wearing a
    record-level label — and the reader could no longer tell the two apart, which is the one
    distinction the attribution vocabulary exists to preserve.
    """
    state = _state()
    _node(state, 1, ["loss/contrastive"], metric=1.0)
    _node(state, 2, [], metric=2.0)
    assert state_concepts(state, [2]) == []
    assert state_concepts(state, [99]) == []          # a node the run no longer has


def test_state_concepts_never_raises_on_a_malformed_state():
    assert state_concepts(object()) == []
    assert state_concepts(None, [1]) == []


# --------------------------------------------------------------------------- read-side attribution

def test_bounded_row_concepts_drops_rather_than_coerces_an_unusable_id():
    """`str()` would launder `7`/`None` into the valid ids `'7'`/`'None'` and publish them as evidence."""
    assert bounded_row_concepts([7, None, {"a": 1}, "Loss/Contrastive"]) == ["loss/contrastive"]
    assert bounded_row_concepts("loss/contrastive") == []      # a bare string is not a list
    assert bounded_row_concepts(None) == []


def test_run_concept_index_distinguishes_untagged_from_absent():
    """Present-and-empty (folded, no concepts) is a weaker fact than absent (run gone)."""
    index = run_concept_index([
        {"run_id": "tagged", "concepts": {"loss/contrastive": {"count": 1}}},
        {"run_id": "untagged", "concepts": {}},
    ])
    assert index == {"tagged": ["loss/contrastive"], "untagged": []}
    assert "untagged" in index and "deleted" not in index


def test_a_record_tag_outranks_run_inheritance():
    index = {"r1": ["run/wide"]}
    row = {"statement": "s", "run_id": "r1", "concepts": ["own/tag"]}
    assert attribute_row(row, index) == (["own/tag"], ATTRIBUTION_RECORD)


def test_run_inheritance_is_labelled_as_the_weaker_claim():
    index = {"r1": ["loss/contrastive", "retrieval/dense"]}
    concepts, source = attribute_row({"statement": "s", "run_id": "r1"}, index)
    assert concepts == ["loss/contrastive", "retrieval/dense"]
    assert source == ATTRIBUTION_RUN, "an inherited tag must never be reported as record-level"


def test_an_untagged_row_stays_untagged_and_is_not_guessed_from_its_text():
    """No keyword match, no embedding, no task_id->concept guess. Untagged is a valid answer."""
    index = {"r1": []}
    row = {"statement": "contrastive loss with dense retrieval", "run_id": "r1",
           "task_id": "retrieval_finetune"}
    assert attribute_row(row, index) == ([], None)
    attribute_rows([row], index)
    assert "concepts" not in row and "concept_source" not in row, (
        "absence — not `concepts: []` — is the wire shape a client tests for coverage with")


def test_shelf_coverage_reports_what_it_cannot_classify():
    index = {"tagged": ["loss/contrastive"], "untagged": []}
    rows = [
        {"statement": "a", "run_id": "tagged"},                       # inherited
        {"statement": "b", "concepts": ["own/tag"]},                  # recorded
        {"statement": "c", "run_id": "untagged"},                     # run folded, no concepts
        {"statement": "d"},                                           # no run at all
    ]
    receipt = shelf_coverage(attribute_rows(rows, index))
    assert receipt == {"total": 4, "tagged": 2, "untagged": 2,
                       "by_source": {ATTRIBUTION_RECORD: 1, ATTRIBUTION_RUN: 1}}


def test_shelf_tree_materializes_ancestors_and_marks_which_were_really_tagged():
    tiers = {"lessons": [{"concepts": ["loss/contrastive/in-batch"]}, {"concepts": ["loss/margin"]}]}
    tree = shelf_tree(tiers)
    assert tree["roots"] == ["loss"]
    assert tree["nodes"]["loss"]["tagged"] is False, "a synthetic ancestor is structure, not evidence"
    assert tree["nodes"]["loss/contrastive/in-batch"]["tagged"] is True
    assert tree["nodes"]["loss"]["children"] == ["loss/contrastive", "loss/margin"]
    # counts are DIRECT, never rolled up: a reader that wants a subtree total sums what it shows
    assert tree["counts"] == {"loss/contrastive/in-batch": 1, "loss/margin": 1}


def test_build_shelf_reports_run_coverage_beside_row_coverage():
    """Two different reasons a tier reads thin, and only one is fixed by tagging more runs."""
    index = run_concept_index([
        {"run_id": "r1", "concepts": {"loss/contrastive": {}}},
        {"run_id": "r2", "concepts": {}},
        {"run_id": "r3", "concepts": {}},
    ])
    shelf = build_shelf({"lessons": [{"statement": "a", "run_id": "r2"}]}, index)
    assert shelf["runs_indexed"] == 3 and shelf["runs_tagged"] == 1
    assert shelf["coverage"]["lessons"]["tagged"] == 0
    assert shelf["sources"] == list(ATTRIBUTION_SOURCES)


# --------------------------------------------------------------------------- the vocabulary registry

def test_attribution_vocabulary_reaches_its_consumers():
    """Two-way registry scan (CLAUDE.md duck-typed-seam convention).

    The source value reaches the HTTP wire and the UI renders a DIFFERENT affordance per member, so a
    typo would silently degrade a record-level claim into an unlabelled one rather than failing.
    """
    import inspect
    from looplab.engine import concept_shelf
    shelf_source = inspect.getsource(concept_shelf)
    ui = (_repo_root() / "ui" / "src" / "conceptShelf.js").read_text(encoding="utf-8")
    panels = (_repo_root() / "ui" / "src" / "panels.jsx").read_text(encoding="utf-8")
    for member in ATTRIBUTION_SOURCES:
        assert f'"{member}"' in shelf_source, f"{member} must be a named constant, not a literal"
        assert f"'{member}'" in ui, f"the UI must know the {member!r} attribution source"
        assert f"'{member}'" in panels, f"the payload validator must accept {member!r}"
    # And the other direction: no member may be dropped from the wire without this test going red.
    assert len(ATTRIBUTION_SOURCES) == len(set(ATTRIBUTION_SOURCES)) == 2


def _repo_root():
    from pathlib import Path
    return Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------- the HTTP surface

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from looplab.serve.server import make_app  # noqa: E402


def _write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"".join(orjson.dumps(row) + b"\n" for row in rows))


def test_memory_endpoint_publishes_the_shelf_with_an_honest_receipt(tmp_path, monkeypatch):
    monkeypatch.setenv("LOOPLAB_MEMORY_DIR", "")
    memory = tmp_path / "mem"
    _write(memory / "lessons.jsonl", [
        {"statement": "tagged at the record", "run_id": "r1", "task_id": "t",
         "concepts": ["loss/contrastive/in-batch"]},
        {"statement": "no concept anywhere", "run_id": "r-gone", "task_id": "t"},
    ])
    _write(memory / "meta_notes.jsonl", [{"note": "n", "run_id": "r1", "task_id": "t"}])
    client = TestClient(make_app(tmp_path / "runs"))
    assert client.put("/api/settings", json={"settings": {"memory_dir": str(memory)}}).status_code == 200

    body = client.get("/api/memory").json()
    index = body["concept_index"]
    assert index["tree"]["roots"] == ["loss"]
    assert index["coverage"]["lessons"] == {
        "total": 2, "tagged": 1, "untagged": 1,
        "by_source": {ATTRIBUTION_RECORD: 1, ATTRIBUTION_RUN: 0}}
    tagged = next(row for row in body["lessons"] if row["statement"] == "tagged at the record")
    assert tagged["concepts"] == ["loss/contrastive/in-batch"]
    assert tagged["concept_source"] == ATTRIBUTION_RECORD
    untagged = next(row for row in body["lessons"] if row["statement"] == "no concept anywhere")
    assert "concepts" not in untagged, "an unattributable row must not be dressed as tagged"


def test_memory_endpoint_inherits_concepts_from_a_real_tagged_run(tmp_path, monkeypatch):
    """The bridge for memory written BEFORE the durable field existed — end to end, over HTTP.

    A pre-existing lesson carries a `run_id` and nothing else; the run's own folded concept membership
    is what makes it navigable, and the wire must say the attribution is run-level.
    """
    monkeypatch.setenv("LOOPLAB_MEMORY_DIR", "")
    runs = tmp_path / "runs"
    _write(runs / "r1" / "events.jsonl", [
        {"seq": 0, "ts": 1.0, "type": "run_started",
         "data": {"task_id": "t", "goal": "g", "direction": "min", "run_id": "r1"}},
        {"seq": 1, "ts": 2.0, "type": "node_created",
         "data": {"node_id": 0, "parent_ids": [], "operator": "draft",
                  "idea": {"operator": "draft", "params": {}, "summary": "s"}}},
        {"seq": 2, "ts": 3.0, "type": "node_concepts",
         "data": {"node_id": 0, "concepts": ["retrieval/dense"], "mode": "llm", "generation": 0}},
        {"seq": 3, "ts": 4.0, "type": "node_evaluated", "data": {"node_id": 0, "metric": 1.0}},
    ])
    memory = tmp_path / "mem"
    _write(memory / "lessons.jsonl", [{"statement": "legacy lesson", "run_id": "r1", "task_id": "t"}])
    client = TestClient(make_app(runs))
    assert client.put("/api/settings", json={"settings": {"memory_dir": str(memory)}}).status_code == 200

    summaries = client.get("/api/runs").json()
    assert summaries[0]["concepts"] == {"retrieval/dense": {"count": 1, "best_metric": 1.0}}

    body = client.get("/api/memory").json()
    lesson = body["lessons"][0]
    assert lesson["concepts"] == ["retrieval/dense"]
    assert lesson["concept_source"] == ATTRIBUTION_RUN
    assert body["concept_index"]["runs_indexed"] == 1
    assert body["concept_index"]["runs_tagged"] == 1


def test_memory_endpoint_survives_an_unreadable_run_list(tmp_path, monkeypatch):
    """A broken run list must cost the INHERITED tags, never the whole Memory panel."""
    monkeypatch.setenv("LOOPLAB_MEMORY_DIR", "")
    memory = tmp_path / "mem"
    _write(memory / "lessons.jsonl", [{"statement": "durable", "concepts": ["a/b"], "run_id": "r1"}])
    client = TestClient(make_app(tmp_path / "runs"))
    assert client.put("/api/settings", json={"settings": {"memory_dir": str(memory)}}).status_code == 200
    import looplab.serve.appstate as appstate
    monkeypatch.setattr(appstate.AppState, "run_summaries",
                        lambda self: (_ for _ in ()).throw(OSError("run root exploded")))
    body = client.get("/api/memory").json()
    assert body["lessons"][0]["concepts"] == ["a/b"]
    assert body["concept_index"]["runs_indexed"] == 0, "and it must SAY the index is empty"


def test_legacy_memory_rows_still_load_unchanged(tmp_path, monkeypatch):
    """Invariant 5, reader side: no migration, and an old row is not degraded by the new field."""
    monkeypatch.setenv("LOOPLAB_MEMORY_DIR", "")
    memory = tmp_path / "mem"
    _write(memory / "cases.jsonl", [
        {"task_id": "t", "goal": "g", "direction": "min", "params": {"x": 1}, "metric": 0.5,
         "rationale": "why"}])
    client = TestClient(make_app(tmp_path / "runs"))
    assert client.put("/api/settings", json={"settings": {"memory_dir": str(memory)}}).status_code == 200
    case = client.get("/api/memory").json()["cases"][0]
    assert case["task_id"] == "t" and case["metric"] == 0.5 and case["params"] == {"x": 1}
    assert "concepts" not in case


# --------------------------------------------------------------------------- the durable write path

def test_a_finished_run_writes_its_concepts_into_cross_run_memory(tmp_path, monkeypatch):
    """Drive the property, not a source pin: a REAL run finishes, then the memory files are read.

    This is what makes the shelf improve over time rather than staying a projection over old data.
    """
    from tests.factories import make_engine
    import anyio

    memory = tmp_path / "mem"
    memory.mkdir()
    engine = make_engine(tmp_path / "run", memory_dir=str(memory), max_nodes=2,
                         reflection_priors=True, comparative_lessons=False)
    # Tag the nodes the way the classifier would, then let finalization distil memory from them.
    original = engine.lessons.store_case

    def _tagged_store_case(final):
        for nid in final.nodes:
            final.node_concepts[nid] = ["optimization/analytic"]
        return original(final)

    monkeypatch.setattr(engine.lessons, "store_case", _tagged_store_case)
    anyio.run(engine.run)

    cases = [json.loads(line) for line in (memory / "cases.jsonl").read_text().splitlines() if line]
    assert cases and cases[0]["concepts"] == ["optimization/analytic"]
    assert cases[0]["run_id"], "a case must carry its run or it can never be joined at all"
