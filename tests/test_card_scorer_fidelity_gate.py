"""Deterministic scientific gate for Card-driven GreedyTree scorer fidelity."""
from __future__ import annotations

import json

from looplab.search.card_selection import card_next_actions
from looplab.search.policy import operator_yields
from looplab.search.scorer_fidelity import (
    SCORER_FIDELITY_CASE_COUNT,
    SCORER_FIDELITY_CASE_NAMES,
    SCORER_FIDELITY_SCHEMA,
    _cases,
    scorer_fidelity_gate,
)


def test_scorer_fidelity_matrix_is_exact_bounded_and_json_ready():
    report = scorer_fidelity_gate()

    assert set(report) == {"schema", "passed", "cases", "mismatches", "case_results"}
    assert report["schema"] == SCORER_FIDELITY_SCHEMA
    assert report["passed"] is True
    assert report["mismatches"] == 0
    assert report["cases"] == len(report["case_results"]) == \
        SCORER_FIDELITY_CASE_COUNT == 15
    assert len(json.dumps(report, sort_keys=True)) < 32_000

    assert SCORER_FIDELITY_CASE_NAMES == (
        "forced_pending",
        "forced_seed",
        "forced_debug",
        "forced_budget",
        "direction_min",
        "direction_max",
        "merge_every_before",
        "merge_every_at",
        "merge_every_after",
        "ablate_every_before",
        "ablate_every_at",
        "ablate_every_after",
        "bandit_untried_merge",
        "bandit_untried_ablate",
        "bandit_yield_improve",
    )
    assert tuple(result["name"] for result in report["case_results"]) == \
        SCORER_FIDELITY_CASE_NAMES
    results = {result["name"]: result for result in report["case_results"]}
    assert results["merge_every_at"]["expected"][0]["_reason"] == "merge top-2"
    assert results["ablate_every_at"]["expected"][0]["_reason"] == \
        "ablate highest-impact param"
    assert results["bandit_untried_merge"]["expected"][0]["_reason"] == \
        "bandit: merge top-2"
    assert results["bandit_untried_ablate"]["expected"][0]["_reason"] == \
        "bandit: ablate highest-impact param"
    assert results["bandit_yield_improve"]["expected"][0]["_reason"] == \
        "bandit: exploit best"
    assert all(result["semantics_passed"] for result in results.values())
    assert all(result["ownership_passed"] for result in results.values())
    assert all(result["expected"] == result["actual"] for result in results.values())
    assert all(
        result["expected_ownership"] == result["actual_ownership"]
        for result in results.values()
    )
    assert results["forced_pending"]["expected_ownership"] == [None, None]
    assert results["forced_budget"]["expected_ownership"] == []
    assert results["forced_seed"]["expected_ownership"] == ["seed-a", "seed-b"]
    assert results["forced_debug"]["expected_ownership"] == ["debug-matching"]


def test_bandit_yield_fixture_uses_unequal_nonzero_exploration_counts():
    case = next(case for case in _cases() if case.name == "bandit_yield_improve")

    counts = {
        operator: stats["n"]
        for operator, stats in operator_yields(case.state).items()
    }

    assert counts == {"improve": 2, "merge": 1}
    assert case.policy.next_actions(case.state)[0]["_reason"] == "bandit: exploit best"


def test_scorer_fidelity_gate_detects_intentional_audit_metadata_divergence():
    def divergent_card_actions(state, policy, max_nodes):
        actions = card_next_actions(state, policy, max_nodes)
        for action in actions:
            if "_reason" in action:
                action["_reason"] = "intentional scorer divergence"
                break
        return actions

    report = scorer_fidelity_gate(divergent_card_actions)

    assert report["passed"] is False
    assert report["mismatches"] > 0
    assert report["mismatches"] == sum(
        not result["passed"]
        for result in report["case_results"]
    )
    assert any(
        result["expected"] != result["actual"]
        and result["actual"][0].get("_reason") == "intentional scorer divergence"
        for result in report["case_results"]
        if result["actual"]
    )


def test_scorer_fidelity_gate_rejects_semantically_equal_legacy_delegation():
    report = scorer_fidelity_gate(
        lambda state, policy, max_nodes: policy.next_actions(state)
    )

    assert report["passed"] is False
    assert report["mismatches"] > 0
    assert any(
        result["semantics_passed"] is True
        and result["ownership_passed"] is False
        and result["expected_ownership"] != result["actual_ownership"]
        for result in report["case_results"]
    )
    assert report["mismatches"] == sum(
        not result["passed"]
        for result in report["case_results"]
    )
