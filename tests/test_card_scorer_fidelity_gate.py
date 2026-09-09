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
        "no_forced_debug",
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
    # RENAMED and INVERTED with F5 (2026-08-13). The case still carries a ready `debug` Card and an
    # `improve` distractor on the same board; what it pins is now that the distractor is what both
    # authorities select, because a failed leaf forces nothing and a `debug` Card is never
    # executable. Every speculation calibration receipt issued before that commit is revoked — this
    # row is part of `search/speculation_quality.py`'s derivation.
    assert results["no_forced_debug"]["expected_ownership"] == ["debug-distractor"]


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


# ---------------------------------------------------------------------------------------------
# doc 25 SE-12, DECLINED 2026-09-08: the two halves of the number the decline rests on.
#
# SE-12 read the 15-case matrix and its fixture builders as a unit-test suite shipped by mistake and
# recommended moving all but "a handful of forced-gate cases" into this file. The 2026-08-05
# adjudication deferred it for want of a measurement ("a performance concern with no measurement
# attached"). These two tests ARE that measurement, and they live here rather than in the doc so the
# decline goes red the day it stops being true -- the same rule CLAUDE.md states for machine
# constraints: measure it, do not write it down.
# ---------------------------------------------------------------------------------------------


def test_the_matrix_the_receipt_carries_is_a_bounded_share_of_its_byte_budget():
    """The COST half: what the matrix actually costs the receipt it is embedded in.

    `speculation_quality.py` puts this report into the receipt body verbatim and `_self_digest`
    hashes it, so its size is a real receipt cost rather than a hypothetical one.  Measured
    2026-09-08 at 5,858 bytes: 2.2 % of the scorer bound and 0.56 % of the whole-receipt bound.
    The assertions are SHARES with headroom, not the exact byte count, because the point of the
    number is its order of magnitude -- a matrix grown to a fifth of its own bound would be a
    different item, and that is what should turn this red.
    """
    from looplab.core.jsonutil import canonical_json
    from looplab.search.speculation_quality import _MAX_RECEIPT_BYTES, _MAX_SCORER_BYTES

    body = canonical_json(scorer_fidelity_gate())

    assert len(body) < _MAX_SCORER_BYTES // 10, (
        f"the scorer body is {len(body)} bytes of the {_MAX_SCORER_BYTES}-byte bound "
        "`_scorer_fidelity_section` refuses past; SE-12's cost side has changed"
    )
    assert len(body) < _MAX_RECEIPT_BYTES // 50


def test_the_forced_cases_alone_would_drop_the_cadence_and_bandit_verdicts():
    """The BENEFIT half: what SE-12's "handful of forced-gate cases" would stop proving.

    Driven, not asserted: each case is replayed through the legacy authority it is compared against
    and reduced to the `(kind, _reason)` verdicts it makes that authority emit.  The four cases the
    recommendation keeps reach 3 of the 8 the whole matrix reaches; the five they never reach are
    the entire merge cadence, the entire ablate cadence and the whole operator-bandit path -- which
    is exactly where the Card lane and `GreedyTree.next_actions` have room to disagree, and where
    the two rows pinning a DECLINED ownership (`ablate_every_at`, `bandit_untried_ablate`) live.

    A receipt issued off the shrunk matrix would therefore assert parity it had not checked on any
    protected cadence.  That trade -- 5 of 8 verdicts of live evidence for 0.13 % of one validation
    -- is the whole reason SE-12 is declined rather than deferred.
    """
    verdicts = {
        case.name: frozenset(
            (action.get("kind"), action.get("_reason"))
            for action in case.policy.next_actions(case.state)
        )
        for case in _cases()
    }
    assert set(verdicts) == set(SCORER_FIDELITY_CASE_NAMES)

    kept = {"forced_pending", "forced_seed", "no_forced_debug", "forced_budget"}
    assert kept < set(SCORER_FIDELITY_CASE_NAMES)
    everything = frozenset().union(*verdicts.values())
    forced_only = frozenset().union(*(verdicts[name] for name in kept))

    assert len(everything) == 8
    assert len(forced_only) == 3
    assert {reason for _kind, reason in everything - forced_only} == {
        "merge top-2",
        "ablate highest-impact param",
        "bandit: merge top-2",
        "bandit: ablate highest-impact param",
        "bandit: exploit best",
    }
    # And the loss is not recoverable by keeping one more row: every lost verdict is contributed by
    # exactly one case, so no cheaper subset of the eleven preserves the coverage.
    for verdict in everything - forced_only:
        contributors = [name for name, seen in verdicts.items() if verdict in seen]
        assert len(contributors) == 1, f"{verdict} is carried by {contributors}"
