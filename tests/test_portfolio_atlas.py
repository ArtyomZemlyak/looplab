"""PART IV cross-run Step 6 (§21.20) — portfolio_atlas + the `atlas` CLI.

The Research Atlas DATA view: one payload composing the concept overview (Step 3), claim assessments
(Step 4) and the bounded context pack (Step 5) into "what's explored / where it's thin / what's
contradictory". Pure/deterministic — the read-model the UI (or an agent) renders. Pins the composition,
the thin-coverage gap proxy (single-run concepts, NOT a false 'never tried'), and the CLI.
"""
from __future__ import annotations

import orjson
from typer.testing import CliRunner

from looplab.cli import app
from looplab.engine.claims import portfolio_atlas
from looplab.engine.memory import build_concept_capsule


def _cap(run_id, concepts, outcomes, direction="max"):
    return build_concept_capsule(run_id=run_id, fingerprint=["kind:dataset"], direction=direction,
                                 concepts=concepts, concept_outcomes=outcomes)


def _lesson(statement, outcome, evidence, run_id="r1"):
    return {"statement": statement, "outcome": outcome, "evidence": evidence, "run_id": run_id, "task_id": "t"}


def test_atlas_composes_all_sections():
    caps = [
        _cap("r1", ["hard-neg", "distillation"], {"hard-neg": 0.88}),
        _cap("r2", ["hard-neg"], {"hard-neg": 0.90}),
    ]
    lessons = [
        _lesson("hard-neg helps", "supported", [1], run_id="r1"),
        _lesson("distillation helps", "supported", [2], run_id="r1"),
        _lesson("distillation helps", "refuted", [3], run_id="r2"),
    ]
    atlas = portfolio_atlas(lessons, caps)
    assert atlas["n_runs"] == 2 and atlas["n_concepts"] == 2
    assert atlas["n_claims"] == 2 and atlas["n_contested"] == 1
    # explored: hard-neg in 2 runs leads
    assert atlas["explored"][0]["concept"] == "hard-neg" and atlas["explored"][0]["n_runs"] == 2
    # contradictions: the mixed claim
    assert atlas["contradictions"][0]["statement"] == "distillation helps"
    # context pack present and contested-first
    assert atlas["context_pack"]["claims"][0]["epistemic"] == "mixed"


def test_thin_coverage_is_single_run_concepts_not_false_never_tried():
    caps = [
        _cap("r1", ["hard-neg", "quantization"], {}),
        _cap("r2", ["hard-neg"], {}),
    ]
    atlas = portfolio_atlas([], caps)
    assert atlas["thin_coverage"] == ["quantization"]     # explored exactly once
    assert "hard-neg" not in atlas["thin_coverage"]       # explored twice -> not thin


def test_atlas_derived_sections_use_full_rows_before_overview_cap():
    popular = [f"popular/c{index:03d}" for index in range(512)]
    caps = []
    for group in range(2):
        concepts = popular[group * 256:(group + 1) * 256]
        for repeat in range(3):
            caps.append(_cap(f"popular-{group}-{repeat}", concepts, {}))
    for repeat in range(2):
        caps.append(_cap(
            f"tendency-{repeat}", ["zz/target", "zz/baseline"],
            {"zz/target": 0.9, "zz/baseline": 0.1},
        ))
    caps.append(_cap("thin", ["zz/thin"], {}))

    atlas = portfolio_atlas([], caps)

    # All 512 popular rows outrank these rows in the public overview. Atlas must derive its independent
    # sections from the full internal retained set before applying its own max_items display envelope.
    assert atlas["n_concepts"] == atlas["explored_total"] == 515
    assert len(atlas["explored"]) == 8 and atlas["explored_omitted"] == 507
    assert atlas["thin_coverage"] == ["zz/thin"]
    assert atlas["thin_coverage_total"] == 1 and atlas["thin_coverage_omitted"] == 0
    assert "zz/target (n=2)" in atlas["context_pack"]["coverage"]["helps"]
    assert "zz/baseline (n=2)" in atlas["context_pack"]["coverage"]["hurts"]


def test_atlas_every_outward_section_has_exact_omission_receipts():
    caps = [_cap("r1", ["alpha", "beta"], {})]
    lessons = [
        _lesson("alpha helps", "supported", [1], run_id="r1"),
        _lesson("alpha helps", "refuted", [2], run_id="r2"),
        _lesson("beta helps", "supported", [3], run_id="r1"),
        _lesson("beta helps", "refuted", [4], run_id="r2"),
    ]

    atlas = portfolio_atlas(lessons, caps, max_items=1)

    assert (atlas["explored_total"], atlas["explored_omitted"]) == (2, 1)
    assert (atlas["thin_coverage_total"], atlas["thin_coverage_omitted"]) == (2, 1)
    assert (atlas["contradictions_total"], atlas["contradictions_omitted"]) == (2, 1)


def test_atlas_n_runs_counts_lesson_runs_and_stays_internally_consistent():
    # A lesson-only / legacy memory (no opt-in concept capsules) must NOT report zero runs: n_runs unions
    # the runs cited by lessons. And the top-level count must AGREE with the embedded context_pack coverage
    # (one payload never reports two different n_runs).
    atlas = portfolio_atlas(
        [_lesson("hard-neg helps", "supported", [1], run_id="rA"),
         _lesson("mnr helps", "supported", [2], run_id="rB")],
        [])                                                        # no capsules at all
    assert atlas["n_runs"] == 2 and atlas["n_concepts"] == 0       # both lesson runs counted, not zero
    assert atlas["context_pack"]["coverage"]["n_runs"] == 2        # embedded coverage agrees with the top


def test_atlas_empty_is_well_formed():
    atlas = portfolio_atlas([], [])
    assert atlas["n_runs"] == 0 and atlas["explored"] == [] and atlas["contradictions"] == []
    assert atlas["thin_coverage"] == [] and atlas["context_pack"]["claims"] == []
    assert atlas["explored_total"] == atlas["thin_coverage_total"] == 0
    assert atlas["contradictions_total"] == 0
    assert atlas["explored_omitted"] == atlas["thin_coverage_omitted"] == 0
    assert atlas["contradictions_omitted"] == 0
    assert atlas["concept_source"] == {
        "source_complete": True, "partial_capsules": 0, "source_unknown_capsules": 0,
        "source_concepts_omitted": 0, "source_outcomes_omitted": 0,
    }


def test_atlas_surfaces_aggregate_partial_capsule_receipt():
    concepts = [f"axis/c{index:03}" for index in range(300)]
    atlas = portfolio_atlas([], [_cap(
        "wide", concepts, {concept: float(index) for index, concept in enumerate(concepts)})])

    assert atlas["concept_source"] == {
        "source_complete": False, "partial_capsules": 1, "source_unknown_capsules": 0,
        "source_concepts_omitted": 44, "source_outcomes_omitted": 44,
    }
    assert atlas["concept_source"] == {
        key: atlas["context_pack"]["coverage"][key] for key in atlas["concept_source"]
    }


def test_cli_atlas_prints_sections(tmp_path):
    ConceptCapsuleStore = __import__("looplab.engine.memory", fromlist=["ConceptCapsuleStore"]).ConceptCapsuleStore
    store = ConceptCapsuleStore(tmp_path / "concept_capsules.jsonl")
    store.add(_cap("r1", ["hard-neg", "quantization"], {"hard-neg": 0.88}))
    store.add(_cap("r2", ["hard-neg"], {"hard-neg": 0.90}))
    (tmp_path / "lessons.jsonl").write_bytes(
        b"\n".join(orjson.dumps(lesson) for lesson in [
            _lesson("mnr helps", "supported", [1], run_id="r1"),
            _lesson("mnr helps", "tested", [2], run_id="r2"),
        ]) + b"\n")
    res = CliRunner().invoke(app, ["atlas", str(tmp_path)])
    assert res.exit_code == 0
    assert "Research Atlas: 2 run(s)" in res.stdout
    assert "hard-neg" in res.stdout and "Observed in one returned run" in res.stdout
    assert "quantization" in res.stdout and "not an untried-gap claim" in res.stdout
    assert "Mixed-evidence claim records" in res.stdout and "mnr helps" in res.stdout


def test_cli_atlas_discloses_each_bounded_section(tmp_path):
    ConceptCapsuleStore = __import__(
        "looplab.engine.memory", fromlist=["ConceptCapsuleStore"]).ConceptCapsuleStore
    ConceptCapsuleStore(tmp_path / "concept_capsules.jsonl").add(
        _cap("r1", ["alpha", "beta"], {}))

    res = CliRunner().invoke(app, ["atlas", str(tmp_path), "--max-items", "1"])

    assert res.exit_code == 0
    assert ("Bounded projection omitted: 1 concept observation(s), "
            "1 single-run observation(s), 0 mixed-evidence record(s).") in res.stdout


def test_cli_atlas_warns_that_legacy_capsule_counts_are_lower_bounds(tmp_path):
    ConceptCapsuleStore = __import__(
        "looplab.engine.memory", fromlist=["ConceptCapsuleStore"]).ConceptCapsuleStore
    legacy = _cap("legacy", ["hard-neg"], {"hard-neg": 0.88})
    for stem in ("concepts", "concept_outcomes"):
        for suffix in ("total", "omitted", "complete"):
            legacy.pop(f"{stem}_{suffix}")
    ConceptCapsuleStore(tmp_path / "concept_capsules.jsonl").add(legacy)

    res = CliRunner().invoke(app, ["atlas", str(tmp_path)])

    assert res.exit_code == 0
    assert "WARNING: concept capsule source is PARTIAL" in res.stdout
    assert "retained lower bounds only" in res.stdout
    assert "1 legacy capsule(s) have unknown totals" in res.stdout


def test_cli_atlas_json(tmp_path):
    (tmp_path / "lessons.jsonl").write_bytes(orjson.dumps(_lesson("x helps", "supported", [1])) + b"\n")
    res = CliRunner().invoke(app, ["atlas", str(tmp_path), "--json"])
    assert res.exit_code == 0
    a = orjson.loads(res.stdout)
    assert a["n_claims"] == 1


def test_cli_atlas_empty_dir_is_clean_error(tmp_path):
    res = CliRunner().invoke(app, ["atlas", str(tmp_path)])
    assert res.exit_code == 1 and "no cross-run memory" in res.stdout
