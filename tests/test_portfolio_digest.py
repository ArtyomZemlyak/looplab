"""PART IV cross-run Step 7 (§21.20.11) — portfolio_digest: the recursive axis-cluster summary.

A summary LEVEL above the flat concept overview: concepts grouped by their axis prefix into clusters with
rollup counts. Deterministic (no LLM). GATED — ships as inspector DATA only, not wired into prompts until
it beats the flat baseline. Honors concept aliases (CR1a).
"""
from __future__ import annotations

from looplab.engine.concept_registry import load_concept_aliases, record_concept_alias
from looplab.engine.memory import build_concept_capsule, portfolio_digest


def _cap(run_id, concepts):
    return build_concept_capsule(run_id=run_id, fingerprint=["k"], direction="max",
                                 concepts=concepts, concept_outcomes={})


def test_groups_concepts_by_axis_prefix():
    caps = [
        _cap("r1", ["data/hard-negative-mining", "data/augmentation", "loss/mnr"]),
        _cap("r2", ["data/hard-negative-mining", "encoderarch/adapter"]),
    ]
    dg = portfolio_digest(caps)
    axes = {a["axis"]: a for a in dg["axes"]}
    assert dg["n_axes"] == 3 and axes["data"]["n_concepts"] == 2
    assert axes["data"]["n_runs"] == 2 and axes["loss"]["n_concepts"] == 1
    assert dg["axes"][0]["axis"] == "data"                 # most-concept cluster first
    assert dg == portfolio_digest(list(reversed(caps)))      # input-order independent


def test_ungrouped_for_no_prefix():
    dg = portfolio_digest([_cap("r1", ["mnr", "data/x"])])
    axes = {a["axis"] for a in dg["axes"]}
    assert "(ungrouped)" in axes and "data" in axes


def test_digest_honors_aliases(tmp_path):
    record_concept_alias(str(tmp_path), from_concept="data/hn", to_concept="data/hard-neg")
    caps = [_cap("r1", ["data/hn"]), _cap("r2", ["data/hard-neg"])]
    dg = portfolio_digest(caps, aliases=load_concept_aliases(str(tmp_path)))
    data = [a for a in dg["axes"] if a["axis"] == "data"][0]
    assert data["n_concepts"] == 1 and data["n_runs"] == 2   # aliased concepts collapse


def test_empty_is_well_formed():
    assert portfolio_digest([]) == {
        "n_axes": 0, "n_concepts": 0, "axes": [],
        "axes_omitted": 0, "concepts_omitted": 0,
        "source_complete": True, "partial_capsules": 0, "source_unknown_capsules": 0,
        "source_concepts_omitted": 0, "source_outcomes_omitted": 0,
        "source_store_complete": True, "source_rows_total": 0,
        "source_rows_quarantined": 0, "source_malformed_rows": 0,
        "source_invalid_capsule_rows": 0, "source_duplicate_run_rows": 0,
    }


def test_axis_run_total_is_computed_before_overview_run_cap():
    dg = portfolio_digest([_cap(f"r{i:02d}", ["data/shared"]) for i in range(70)])
    assert dg["axes"] == [{
        "axis": "data", "n_concepts": 1, "n_runs": 70,
        "concepts": ["data/shared"], "concepts_omitted": 0,
    }]
    assert dg["axes_omitted"] == dg["concepts_omitted"] == 0


def test_digest_bounds_display_only_and_reports_exact_omissions():
    concepts = [f"axis{i:03d}/concept" for i in range(513)]
    caps = [_cap(f"r{i}", concepts[i:i + 256]) for i in range(0, len(concepts), 256)]
    dg = portfolio_digest(caps)
    assert dg["n_axes"] == dg["n_concepts"] == 513
    assert len(dg["axes"]) == 512 and dg["axes_omitted"] == 1
    assert dg["concepts_omitted"] == 1
    assert all(axis["concepts_omitted"] == 0 for axis in dg["axes"])


def test_digest_bounds_each_axis_and_keeps_source_completeness_separate():
    dg = portfolio_digest([_cap("r1", [f"data/c{i:03d}" for i in range(300)])])
    data = dg["axes"][0]
    assert data["n_concepts"] == 256 and len(data["concepts"]) == 64
    assert data["concepts_omitted"] == dg["concepts_omitted"] == 192
    assert dg["source_complete"] is False
    assert dg["source_concepts_omitted"] == 44


def test_cli_cross_run_digest(tmp_path):
    from typer.testing import CliRunner
    from looplab.cli import app
    from looplab.engine.memory import ConceptCapsuleStore
    s = ConceptCapsuleStore(tmp_path / "concept_capsules.jsonl")
    s.add(_cap("r1", ["data/hard-negative-mining", "loss/mnr"]))
    res = CliRunner().invoke(app, ["cross-run-digest", str(tmp_path)])
    assert res.exit_code == 0 and "axis-cluster" in res.stdout and "data/" in res.stdout


def test_cli_digest_discloses_bounded_output(tmp_path):
    from typer.testing import CliRunner
    from looplab.cli import app
    from looplab.engine.memory import ConceptCapsuleStore
    s = ConceptCapsuleStore(tmp_path / "concept_capsules.jsonl")
    s.add(_cap("r1", [f"data/c{i:03d}" for i in range(65)]))
    res = CliRunner().invoke(app, ["cross-run-digest", str(tmp_path)])
    assert res.exit_code == 0
    assert "bounded digest omitted 0 axis-cluster(s) and 1 concept label(s)" in res.stdout


def test_cli_digest_empty_is_clean_error(tmp_path):
    from typer.testing import CliRunner
    from looplab.cli import app
    res = CliRunner().invoke(app, ["cross-run-digest", str(tmp_path)])
    assert res.exit_code == 1 and "no concept capsules" in res.stdout
