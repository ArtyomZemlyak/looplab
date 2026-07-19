"""PART IV cross-run Step 3 (§21.20) — portfolio_concept_overview read-model + the `cross-run-concepts` CLI.

The Atlas-lite "what has been tried across the portfolio" view over the per-run concept capsules from
Step 2. Deterministic, drillable to run_id, and — critically — it does NOT compare raw metrics across
tasks (each concept lists the runs that explored it with their OWN outcome). These tests pin the
aggregation, the no-false-comparison rule, and the CLI surface (incl. the empty-store guard).
"""
from __future__ import annotations

from typer.testing import CliRunner

from looplab.cli import app
from looplab.engine.memory import (
    ConceptCapsuleStore, build_concept_capsule, portfolio_concept_overview,
)

runner = CliRunner()


def _cap(run_id, concepts, outcomes, *, direction="max", best=None):
    return build_concept_capsule(run_id=run_id, fingerprint=["kind:dataset", "dir:" + direction],
                                 direction=direction, concepts=concepts, best_metric=best,
                                 concept_outcomes=outcomes)


def test_overview_ranks_concepts_by_run_count_and_lists_runs():
    caps = [
        _cap("r1", ["hard-neg", "distillation"], {"hard-neg": 0.88, "distillation": 0.80}, best=0.88),
        _cap("r2", ["hard-neg"], {"hard-neg": 0.90}, best=0.90),
        _cap("r3", ["hard-neg", "quantization"], {"hard-neg": 0.86}, best=0.86),
    ]
    ov = portfolio_concept_overview(caps)
    assert ov["n_runs"] == 3 and ov["n_concepts"] == 3
    # most-explored first: hard-neg (3 runs) before distillation/quantization (1 each)
    assert ov["concepts"][0]["concept"] == "hard-neg" and ov["concepts"][0]["n_runs"] == 3
    hn_runs = {r["run_id"]: r["metric"] for r in ov["concepts"][0]["runs"]}
    assert hn_runs == {"r1": 0.88, "r2": 0.90, "r3": 0.86}     # each run's OWN outcome, drillable


def test_overview_does_not_fabricate_a_cross_task_best():
    # Two DIFFERENT tasks (min vs max) touching the same concept name — the overview must NOT collapse
    # them into one 'best' number; it keeps per-run outcomes + directions so the incomparability is visible.
    caps = [
        _cap("rmax", ["encoder"], {"encoder": 0.9}, direction="max"),
        _cap("rmin", ["encoder"], {"encoder": 0.1}, direction="min"),
    ]
    ov = portfolio_concept_overview(caps)
    e = ov["concepts"][0]
    assert "best_metric" not in e and "best_run" not in e      # no cross-contract 'best' claim
    dirs = {r["run_id"]: r["direction"] for r in e["runs"]}
    assert dirs == {"rmax": "max", "rmin": "min"}


def test_overview_dedupes_run_count_but_keeps_cards():
    caps = [_cap("r1", ["a", "a", "b"], {"a": 0.5})]
    ov = portfolio_concept_overview(caps)
    assert ov["runs"][0]["run_id"] == "r1" and ov["runs"][0]["n_concepts"] == 2   # a,b (deduped)
    assert {e["concept"] for e in ov["concepts"]} == {"a", "b"}


def test_canonical_collapse_uses_the_run_direction_best_outcome_not_first_alias():
    aliases = {"method/a": "method/canonical", "method/b": "method/canonical"}
    capsules = [
        _cap("max-run", ["method/a", "method/b"], {"method/a": 0.1, "method/b": 0.9}),
        _cap("min-run", ["method/a", "method/b"], {"method/a": 0.8, "method/b": 0.2},
             direction="min"),
    ]

    overview = portfolio_concept_overview(capsules, aliases=aliases)
    row = next(item for item in overview["concepts"] if item["concept"] == "method/canonical")

    assert {run["run_id"]: run["metric"] for run in row["runs"]} == {
        "max-run": 0.9,
        "min-run": 0.2,
    }


def test_empty_portfolio_is_well_formed():
    ov = portfolio_concept_overview([])
    assert ov == {
        "n_runs": 0, "n_concepts": 0, "concepts": [], "runs": [],
        "source_complete": True, "partial_capsules": 0,
        "source_unknown_capsules": 0,
        "source_concepts_omitted": 0, "source_outcomes_omitted": 0,
        "source_store_complete": True, "source_rows_total": 0,
        "source_rows_quarantined": 0, "source_malformed_rows": 0,
        "source_invalid_capsule_rows": 0, "source_duplicate_run_rows": 0,
    }


def test_overview_quarantines_malformed_rows_and_caps_nested_runs_truthfully():
    caps = [
        None,
        {"run_id": "poison", "fingerprint": 7, "concepts": "characters"},
        *[_cap(f"r{index:03}", ["shared"], {"shared": index / 100}) for index in range(70)],
    ]

    overview = portfolio_concept_overview(caps)
    shared = overview["concepts"][0]

    assert overview["n_runs"] == 70 and shared["n_runs"] == 70
    assert overview["source_complete"] is False
    assert overview["source_invalid_capsule_rows"] == 2
    assert len(shared["runs"]) == 64 and shared["runs_omitted"] == 6
    assert [row["run_id"] for row in shared["runs"]] == sorted(
        row["run_id"] for row in shared["runs"])


def test_duplicate_run_rows_are_deterministic_but_never_exact_source():
    first = _cap("same", ["axis/a"], {"axis/a": 1.0})
    second = _cap("same", ["axis/b"], {"axis/b": 2.0})

    left = portfolio_concept_overview([first, second])
    right = portfolio_concept_overview([second, first])

    assert left == right
    assert left["n_runs"] == 1
    assert left["source_complete"] is False
    assert left["source_duplicate_run_rows"] == left["source_rows_quarantined"] == 1


def test_overview_caps_top_level_collections_with_full_totals():
    caps = [
        _cap("a", [f"a/{index:03}" for index in range(256)], {}),
        _cap("b", [f"b/{index:03}" for index in range(256)], {}),
        _cap("c", [f"c/{index:03}" for index in range(100)], {}),
    ]

    overview = portfolio_concept_overview(caps)

    assert overview["n_concepts"] == 612
    assert len(overview["concepts"]) == 512 and overview["concepts_omitted"] == 100
    assert overview["runs"][0]["n_concepts"] == 256
    assert len(overview["runs"][0]["concepts"]) == 64
    assert overview["runs"][0]["concepts_omitted"] == 192


def test_overview_surfaces_capsule_source_omissions_on_result_and_run_card():
    concepts = [f"axis/c{index:03}" for index in range(300)]
    capsule = _cap("wide", concepts, {concept: float(index) for index, concept in enumerate(concepts)})

    overview = portfolio_concept_overview([capsule])
    card = overview["runs"][0]

    assert overview["source_complete"] is False and overview["partial_capsules"] == 1
    assert overview["source_concepts_omitted"] == overview["source_outcomes_omitted"] == 44
    assert card["source_concepts_total"] == card["source_outcomes_total"] == 300
    assert card["source_concepts_omitted"] == card["source_outcomes_omitted"] == 44
    assert card["source_concepts_complete"] is card["source_outcomes_complete"] is False


def test_legacy_v2_without_completeness_is_partial_and_its_rank_signs_are_ignored():
    capsule = _cap("legacy", ["loss/a", "loss/b"], {"loss/a": 1.0, "loss/b": 0.0})
    for key in (
        "concepts_total", "concepts_omitted", "concepts_complete",
        "concept_outcomes_total", "concept_outcomes_omitted", "concept_outcomes_complete",
    ):
        capsule.pop(key)
    assert capsule["concept_signs"] == {"loss/a": 1, "loss/b": -1}  # could be fabricated by old cap order

    overview = portfolio_concept_overview([capsule])
    rows = {row["concept"]: row for row in overview["concepts"]}

    assert overview["source_complete"] is False
    assert overview["partial_capsules"] == overview["source_unknown_capsules"] == 1
    assert rows["loss/a"]["runs"][0]["metric"] == 1.0  # positive retained observation survives
    assert rows["loss/a"]["n_helped"] == rows["loss/a"]["n_hurt"] == 0
    assert overview["runs"][0]["source_outcomes_total"] is None


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def test_cli_prints_overview(tmp_path):
    store = ConceptCapsuleStore(tmp_path / "concept_capsules.jsonl")
    store.add(_cap("r1", ["hard-neg"], {"hard-neg": 0.88}))
    store.add(_cap("r2", ["hard-neg", "distillation"], {"hard-neg": 0.90}))
    res = runner.invoke(app, ["cross-run-concepts", str(tmp_path)])
    assert res.exit_code == 0
    assert "2 run(s), 2 concept(s)" in res.stdout
    assert "hard-neg" in res.stdout and "r1=0.88" in res.stdout


def test_cli_text_discloses_bounded_overview_projection(tmp_path):
    store = ConceptCapsuleStore(tmp_path / "concept_capsules.jsonl")
    store.add(_cap("a", [f"a/{index:03d}" for index in range(256)], {}))
    store.add(_cap("b", [f"b/{index:03d}" for index in range(256)], {}))
    store.add(_cap("c", [f"c/{index:03d}" for index in range(100)], {}))

    res = runner.invoke(app, ["cross-run-concepts", str(tmp_path), "--top", "1"])

    assert res.exit_code == 0
    assert "612 concept(s)" in res.stdout
    assert "Bounded overview omitted 100 concept row(s)" in res.stdout


def test_cli_honors_recorded_merge_and_split(tmp_path):
    # live-test regression: the cross-run-concepts inspector MUST reflect recorded operator/steward
    # governance (merge/purge/split), consistent with cross-run-digest / atlas / agent tools — it used to
    # show the STALE raw graph.
    from looplab.engine.concept_registry import record_concept_alias
    store = ConceptCapsuleStore(tmp_path / "concept_capsules.jsonl")
    store.add(_cap("r1", ["data/hn", "loss/hard-margin"], {}))
    store.add(_cap("r2", ["data/hard-negative-mining"], {}))   # dup spelling of data/hn
    store.add(_cap("r3", ["junk-xyz"], {}))
    record_concept_alias(str(tmp_path), from_concept="data/hn", to_concept="data/hard-negative-mining")  # merge
    record_concept_alias(str(tmp_path), from_concept="junk-xyz", to_concept="")                          # purge
    res = runner.invoke(app, ["cross-run-concepts", str(tmp_path), "--json"])
    assert res.exit_code == 0
    import json
    concepts = {c["concept"]: c for c in json.loads(res.stdout)["concepts"]}
    assert "data/hard-negative-mining" in concepts and concepts["data/hard-negative-mining"]["n_runs"] == 2
    assert "data/hn" not in concepts          # merged away
    assert "junk-xyz" not in concepts         # purged


def test_cli_json_mode(tmp_path):
    store = ConceptCapsuleStore(tmp_path / "concept_capsules.jsonl")
    store.add(_cap("r1", ["hard-neg"], {"hard-neg": 0.88}))
    res = runner.invoke(app, ["cross-run-concepts", str(tmp_path), "--json"])
    assert res.exit_code == 0
    import json
    ov = json.loads(res.stdout)
    assert ov["n_runs"] == 1 and ov["concepts"][0]["concept"] == "hard-neg"


def test_cli_missing_store_is_a_clean_error(tmp_path):
    res = runner.invoke(app, ["cross-run-concepts", str(tmp_path)])
    assert res.exit_code == 1 and "no concept capsules" in res.stdout
