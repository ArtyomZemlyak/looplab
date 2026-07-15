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


def test_cli_atlas_prints_sections(tmp_path):
    ConceptCapsuleStore = __import__("looplab.engine.memory", fromlist=["ConceptCapsuleStore"]).ConceptCapsuleStore
    store = ConceptCapsuleStore(tmp_path / "concept_capsules.jsonl")
    store.add(_cap("r1", ["hard-neg", "quantization"], {"hard-neg": 0.88}))
    store.add(_cap("r2", ["hard-neg"], {"hard-neg": 0.90}))
    (tmp_path / "lessons.jsonl").write_bytes(
        b"\n".join(orjson.dumps(l) for l in [
            _lesson("mnr helps", "supported", [1], run_id="r1"),
            _lesson("mnr helps", "tested", [2], run_id="r2"),
        ]) + b"\n")
    res = CliRunner().invoke(app, ["atlas", str(tmp_path)])
    assert res.exit_code == 0
    assert "Research Atlas: 2 run(s)" in res.stdout
    assert "hard-neg" in res.stdout and "Thin (explored once)" in res.stdout and "quantization" in res.stdout
    assert "Contradictions" in res.stdout and "mnr helps" in res.stdout


def test_cli_atlas_json(tmp_path):
    (tmp_path / "lessons.jsonl").write_bytes(orjson.dumps(_lesson("x helps", "supported", [1])) + b"\n")
    res = CliRunner().invoke(app, ["atlas", str(tmp_path), "--json"])
    assert res.exit_code == 0
    a = orjson.loads(res.stdout)
    assert a["n_claims"] == 1


def test_cli_atlas_empty_dir_is_clean_error(tmp_path):
    res = CliRunner().invoke(app, ["atlas", str(tmp_path)])
    assert res.exit_code == 1 and "no cross-run memory" in res.stdout
