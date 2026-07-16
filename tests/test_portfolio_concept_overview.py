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


def test_empty_portfolio_is_well_formed():
    ov = portfolio_concept_overview([])
    assert ov == {"n_runs": 0, "n_concepts": 0, "concepts": [], "runs": []}


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
