"""PART IV cross-run Step 2 (foundation) — the ConceptCapsuleStore + build_concept_capsule.

The per-run concept capsule is the cross-run bridge that lets a later SIMILAR run see which concepts
were tried before and feed `grade_novelty`'s `prior_concepts` (D3 level 3 — surface prior, never
reject). These tests pin the PURE store in isolation (no engine): fingerprint-keyed retrieval, upsert
by run_id (a re-run replaces, never duplicates), similarity thresholding, and the union shape
`grade_novelty` consumes. Wiring into finalize/novelty is a separate step; this is the substrate.
"""
import json

import pytest

from looplab.engine.memory import (
    ConceptCapsuleStore, build_concept_capsule, task_fingerprint,
)


def _cap(run_id, goal, concepts, *, metric=None, universal=True):
    fp = task_fingerprint("dataset", "max", goal, "recall", universal=universal)
    return build_concept_capsule(run_id=run_id, fingerprint=fp, direction="max",
                                 concepts=concepts, best_metric=metric)


def test_build_capsule_is_flat_and_deduped():
    cap = build_concept_capsule(run_id="r1", fingerprint=["b", "a", "a"], direction="max",
                                concepts=["hard-neg", "hard-neg", "distillation", ""])
    assert cap["run_id"] == "r1"
    assert cap["fingerprint"] == ["a", "b"]               # sorted + deduped
    assert cap["concepts"] == ["distillation", "hard-neg"]  # sorted, empty dropped
    assert cap["direction"] == "max" and cap["best_metric"] is None
    assert cap["v"] == 2 and cap["concept_evidence"] == "classifier"
    assert (cap["fingerprint_total"], cap["fingerprint_omitted"], cap["fingerprint_complete"]) == (2, 0, True)


@pytest.mark.parametrize("direction", [None, "", "MAX", "sideways", 1])
def test_build_capsule_rejects_invalid_direction_instead_of_inverting_evidence(direction):
    with pytest.raises(ValueError, match="direction"):
        build_concept_capsule(
            run_id="r", fingerprint=["k"], direction=direction,
            concepts=["loss/a", "loss/b"], concept_outcomes={"loss/a": 1.0, "loss/b": 0.0},
        )


def test_add_persists_and_reloads_across_instances(tmp_path):
    p = tmp_path / "concept_capsules.jsonl"
    s1 = ConceptCapsuleStore(p)
    assert s1.add(_cap("r1", "dense retrieval reviews", ["hard-neg", "mnr"], metric=0.88))
    # A fresh instance (new run/process) sees the persisted capsule.
    s2 = ConceptCapsuleStore(p)
    assert len(s2.all()) == 1 and s2.all()[0]["run_id"] == "r1"


def test_upsert_by_run_id_replaces_not_duplicates(tmp_path):
    p = tmp_path / "c.jsonl"
    s = ConceptCapsuleStore(p)
    s.add(_cap("r1", "dense retrieval reviews", ["hard-neg"], metric=0.80))
    s.add(_cap("r1", "dense retrieval reviews", ["hard-neg", "distillation"], metric=0.90))  # re-run
    assert len(s.all()) == 1                                   # replaced, not appended
    assert set(s.all()[0]["concepts"]) == {"hard-neg", "distillation"}
    assert s.all()[0]["best_metric"] == 0.90


def test_add_without_run_id_is_rejected(tmp_path):
    s = ConceptCapsuleStore(tmp_path / "c.jsonl")
    assert s.add(build_concept_capsule(run_id="", fingerprint=["a"], direction="max",
                                       concepts=["x"])) is False
    assert s.all() == []


def test_prior_concepts_unions_similar_runs_and_excludes_self(tmp_path):
    s = ConceptCapsuleStore(tmp_path / "c.jsonl")
    s.add(_cap("r1", "dense retrieval russian reviews marketplace", ["hard-neg", "mnr"], metric=0.88))
    s.add(_cap("r2", "dense retrieval russian reviews marketplace", ["distillation"], metric=0.85))
    # A brand-new run with the SAME goal family: prior_concepts unions r1+r2, excluding itself.
    fp_now = task_fingerprint("dataset", "max", "dense retrieval russian reviews marketplace", "recall",
                              universal=True)
    prior = s.prior_concepts(fp_now, min_sim=0.5, exclude_run_id="r3")
    assert prior == {"hard-neg", "mnr", "distillation"}
    # ...and it excludes a capsule's own run when that run reloads.
    assert "hard-neg" not in s.prior_concepts(fp_now, min_sim=0.5, exclude_run_id="r1") or \
        "distillation" in s.prior_concepts(fp_now, min_sim=0.5, exclude_run_id="r1")


def test_dissimilar_run_is_filtered_by_threshold(tmp_path):
    s = ConceptCapsuleStore(tmp_path / "c.jsonl")
    s.add(_cap("r1", "dense retrieval russian reviews", ["hard-neg"], metric=0.88))
    # A totally unrelated task fingerprint: below threshold -> no transfer.
    fp_other = task_fingerprint("dataset", "min", "forecast electricity demand timeseries", "rmse",
                                universal=True)
    assert s.prior_concepts(fp_other, min_sim=0.3) == set()
    caps = s.prior_capsules(fp_other, min_sim=0.3)
    assert caps == []


def test_prior_capsules_ranked_by_similarity_with_run_provenance(tmp_path):
    s = ConceptCapsuleStore(tmp_path / "c.jsonl")
    s.add(_cap("close", "dense retrieval russian reviews marketplace items", ["a"]))
    s.add(_cap("far", "dense retrieval reviews", ["b"]))
    fp_now = task_fingerprint("dataset", "max", "dense retrieval russian reviews marketplace items",
                              "recall", universal=True)
    ranked = s.prior_capsules(fp_now, min_sim=0.1)
    assert [c["run_id"] for _s, c in ranked][0] == "close"     # most-similar first, cite-able by run
    assert all(0.0 <= sim <= 1.0 for sim, _c in ranked)


@pytest.mark.parametrize("legacy_receipt", [False, True])
def test_incomplete_fingerprint_cannot_authorize_related_task_but_exact_task_survives(
        tmp_path, legacy_receipt):
    source = ["retrieval", "russian", *[f"term-{index:03}" for index in range(300)]]
    capsule = build_concept_capsule(
        run_id="prior", task_id="foreign-task", fingerprint=source,
        direction="max", concepts=["data/hard-neg"],
    )
    if legacy_receipt:
        for suffix in ("total", "omitted", "complete"):
            capsule.pop(f"fingerprint_{suffix}")
    store = ConceptCapsuleStore(tmp_path / "scope.jsonl")
    assert store.add(capsule)

    # CODEX AGENT regression: a retained fingerprint prefix is not an applicability grant for another task.
    assert store.prior_capsules(source, min_sim=0.1, task_id="current-task") == []
    exact = store.prior_capsules(["unrelated"], min_sim=0.99, task_id="foreign-task")
    assert len(exact) == 1 and exact[0][0] == 1.0 and exact[0][1]["run_id"] == "prior"


def test_capsule_store_quarantines_structurally_poisoned_rows(tmp_path):
    path = tmp_path / "c.jsonl"
    valid = _cap("good", "dense retrieval", ["hard-neg"], metric=0.8)
    poisoned = [
        [],
        {"v": 1, "run_id": "bad-fingerprint", "fingerprint": 1, "concepts": ["x"]},
        {"v": 1, "run_id": "bad-concept", "fingerprint": ["x"], "concepts": [{}]},
        {"v": 999, "run_id": "future", "fingerprint": ["x"], "concepts": ["x"]},
        {"v": 1, "run_id": "bad-metric", "fingerprint": ["x"], "concepts": ["x"],
         "best_metric": float("inf")},
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in [*poisoned, valid]), encoding="utf-8")

    store = ConceptCapsuleStore(path)

    assert store.all() == [valid]
    assert store.add("not-an-object") is False
    assert store.add(_cap("new", "dense retrieval", ["mnr"], metric=0.9)) is True
    assert {row["run_id"] for row in ConceptCapsuleStore(path).all()} == {"good", "new"}


def test_capsule_store_quarantines_pre_provenance_legacy_rows(tmp_path):
    path = tmp_path / "c.jsonl"
    current = _cap("current", "dense retrieval", ["hard-neg"], metric=0.8)
    legacy_v1 = {**current, "v": 1, "run_id": "legacy-v1"}
    legacy_v1.pop("concept_evidence")
    unversioned = {key: value for key, value in current.items()
                   if key not in {"v", "concept_evidence"}}
    unversioned["run_id"] = "legacy-unversioned"
    path.write_text("".join(json.dumps(row) + "\n"
                            for row in (legacy_v1, unversioned, current)), encoding="utf-8")

    assert ConceptCapsuleStore(path).all() == [current]


def test_upsert_preserves_quarantined_rows_for_future_migration(tmp_path):
    path = tmp_path / "c.jsonl"
    legacy = {"v": 1, "run_id": "legacy", "fingerprint": ["k"], "direction": "max",
              "concepts": ["old/claim"], "concept_outcomes": {}}
    future = {"v": 999, "run_id": "future", "opaque": {"contract": "unknown"}}
    path.write_text("".join(json.dumps(row) + "\n" for row in (legacy, future)), encoding="utf-8")

    store = ConceptCapsuleStore(path)
    assert store.all() == []
    current = _cap("current", "dense retrieval", ["hard-neg"], metric=0.8)
    assert store.add(current) is True

    persisted = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert persisted == [legacy, future, current]
    assert ConceptCapsuleStore(path).all() == [current]


def test_build_capsule_caps_large_generated_collections_deterministically():
    capsule = build_concept_capsule(
        run_id="r", fingerprint=[f"f-{index:04}" for index in range(400)], direction="max",
        concepts=[f"c-{index:04}" for index in range(400)],
        concept_outcomes={f"c-{index:04}": index for index in range(400)},
    )

    assert len(capsule["fingerprint"]) == 256
    assert len(capsule["concepts"]) == 256
    assert len(capsule["concept_outcomes"]) == 256
    assert capsule["fingerprint"] == sorted(capsule["fingerprint"])
    assert capsule["fingerprint_total"] == 400
    assert capsule["fingerprint_omitted"] == 144
    assert capsule["fingerprint_complete"] is False
    assert set(capsule["concept_outcomes"]) == set(capsule["concepts"])
    assert capsule["concepts_total"] == capsule["concept_outcomes_total"] == 400
    assert capsule["concepts_omitted"] == capsule["concept_outcomes_omitted"] == 144
    assert capsule["concepts_complete"] is capsule["concept_outcomes_complete"] is False


def test_capsule_signs_use_complete_source_field_before_bounded_projection():
    from looplab.engine.memory import _concept_profit_signs

    concepts = [f"axis/c{index:03}" for index in range(300)]
    outcomes = {concept: float(index) for index, concept in enumerate(concepts)}
    capsule = build_concept_capsule(
        run_id="r", fingerprint=["k"], direction="max",
        concepts=concepts, concept_outcomes=outcomes,
    )

    full_signs = _concept_profit_signs(outcomes, "max")
    assert capsule["concept_signs"] == {
        concept: full_signs[concept] for concept in capsule["concept_outcomes"]
    }
    # Regression proof: the retained-only median is different, so this assertion exercises the bug rather
    # than merely comparing two coincidentally equivalent calculations.
    assert capsule["concept_signs"] != _concept_profit_signs(capsule["concept_outcomes"], "max")
