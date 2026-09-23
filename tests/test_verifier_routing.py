"""DR-04's router: verification chooses the next round, and a spent bound never reads as clean.

The truth table is the point of this file. `route_after_verification` is a pure function over two
dicts the run already paid for, so every rung can be driven directly rather than pinned as text —
tier 1 of CLAUDE.md's guard ladder — and the registry halves are re-derived by AST, because a
closed action vocabulary that a typo can leave unreachable is the `TRIAGE_ACTIONS` defect one
package over.
"""
from __future__ import annotations

import ast
import inspect

from looplab.trust import verifier_routing as vr
from looplab.trust.verifier_routing import (NEXT_ACTIONS, SPENT_REASONS, progress_digest,
                                            route_after_verification)


def _verdicts(**counts):
    """A verification block holding `counts` rows of each verdict, in the writer's own shape.

    `unsupported=` means a CITATION defect (`kind="citation"`), which is what an unstamped row from
    an old log also reads as; `unsupported_support=` is the claim-level judgement the LLM pass
    stamps. The two are separate keyword arguments because the whole point of `kind` is that they
    are different questions — a helper that took one count would let this file's tests share the
    ambiguity the field removed.
    """
    rows = []
    for verdict, n in counts.items():
        if verdict == "unsupported_support":
            rows.extend({"statement": f"s{i}", "verdict": "unsupported", "kind": "support"}
                        for i in range(n))
            continue
        kind = "citation" if verdict == "unsupported" else None
        for i in range(n):
            row = {"statement": f"c{i}", "verdict": verdict}
            if kind:
                row["kind"] = kind
            rows.append(row)
    return {"verdicts": rows, "method": "deterministic",
            "unsupported": counts.get("unsupported", 0) + counts.get("unsupported_support", 0),
            "total_verdicts": len(rows), "omitted_verdicts": 0}


# ---------------------------------------------------------------------------------------------
# The five actions, one rung each.

def test_evidence_that_does_NOT_CARRY_the_claim_asks_for_RETRIEVE_first():
    """`kind="support"`: the verifier read the evidence and judged it insufficient. The most
    decisive gap, and the only one that outranks the number channels."""
    routing = route_after_verification(_verdicts(unsupported_support=2),
                                       {"quoted": 9, "elsewhere": 5})
    assert routing.action == "retrieve"
    assert "does not carry" in routing.reason
    assert not routing.is_terminal


def test_a_BAD_FOOTNOTE_asks_for_retrieve_LAST_and_never_outranks_a_number():
    """The measurement that reordered this module: ranked first, a citation defect routed 233 of
    237 real memos to `retrieve` (98.3%), because nearly every memo has one. MUTATION: move the
    `unsupported_citation` rung above the number channels and this goes red."""
    routing = route_after_verification(_verdicts(unsupported=3),
                                       {"quoted": 9, "matched": 4, "elsewhere": 2})
    assert routing.action == "narrow", "a real number in the wrong experiment outranks a footnote"
    routing = route_after_verification(_verdicts(unsupported=3), {"quoted": 9, "matched": 9})
    assert routing.action == "retrieve" and "cite nothing that resolves" in routing.reason


def test_an_UNSTAMPED_row_reads_as_a_CITATION_defect():
    """Invariant #5's reader-side default, and it is the conservative direction: claiming the
    verifier judged SUPPORT when the field that says so is absent would promote every old log's
    footnote defect into the most decisive gap."""
    old_log = {"verdicts": [{"statement": "c", "verdict": "unsupported"}]}
    routing = route_after_verification(old_log, {"quoted": 4, "elsewhere": 1})
    assert routing.action == "narrow", "an unstamped row must not outrank the number channel"
    counts = routing.counts["verdicts"]
    assert counts["unsupported_citation"] == 1 and counts["unsupported_support"] == 0


def test_a_real_number_in_the_WRONG_experiment_asks_for_NARROW():
    """The `run` channel is the one the corpus pass called actionable: 250 of 5267 quoted decimals
    are real metrics of the run attributed to an experiment the claim does not cite. That is
    unclear SUPPORT, not missing evidence, and it must not route to `retrieve`."""
    routing = route_after_verification(_verdicts(supported=1), {"quoted": 9, "matched": 6,
                                                                "elsewhere": 3, "unmatched": 0})
    assert routing.action == "narrow"
    assert "3 quoted number(s)" in routing.reason


def test_disagreeing_claims_ask_for_CONTRADICTION_and_never_pick_a_side():
    routing = route_after_verification(_verdicts(supported=2), None,
                                       contradictions=(("c0", "c1"),))
    assert routing.action == "contradiction"
    assert "surface both sides" in routing.reason


def test_an_evidence_id_that_no_longer_resolves_asks_for_REFRESH():
    routing = route_after_verification(_verdicts(supported=1), None, stale_evidence=("ev-1", "ev-2"))
    assert routing.action == "refresh"
    assert "branch only" in routing.reason, "a stale branch is re-read, not the whole episode"


def test_an_unsettled_verdict_asks_for_NARROW_too():
    """`unclear` is the verifier saying it could not settle the support — the same remedy as a
    number in the wrong experiment (cite better or claim less), so it shares the action rather
    than minting a sixth."""
    assert route_after_verification(_verdicts(unclear=1)).action == "narrow"


def test_nothing_actionable_FINALIZES_CLEAN_with_no_residual():
    routing = route_after_verification(_verdicts(supported=3),
                                       {"quoted": 4, "matched": 4, "elsewhere": 0, "unmatched": 0})
    assert (routing.action, routing.spent, routing.residual) == ("finalize", "clean", ())


# ---------------------------------------------------------------------------------------------
# The three bounds, and the red line: a spent bound is never a clean badge.

def test_a_SPENT_BUDGET_finalizes_and_still_names_the_open_gap():
    """The roadmap's red line, driven: "the system must never turn exhausted budget into a clean
    trust badge". MUTATION: return `spent="clean"` here, or drop `residual`, and this goes red."""
    routing = route_after_verification(_verdicts(unsupported=1), {"quoted": 2, "elsewhere": 1},
                                       budget_remaining=False)
    assert routing.action == "finalize" and routing.spent == "budget"
    assert routing.spent != "clean"
    assert routing.residual, "a bound that ends the loop with a gap open must name the gap"
    assert any("no usable citation" in line for line in routing.residual), (
        "an unstamped/citation-kind gap is still a gap a spent bound must name")


def test_the_ROUND_BOUND_finalizes_before_reading_any_gap():
    routing = route_after_verification(_verdicts(unsupported=5), rounds_used=2, max_rounds=2)
    assert (routing.action, routing.spent) == ("finalize", "rounds")
    assert "2 round(s)" in routing.reason
    assert routing.residual


def test_NO_PROGRESS_finalizes_when_a_round_changed_nothing():
    same = progress_digest(["a gap"], ["ev-1"], "a draft")
    routing = route_after_verification(_verdicts(unsupported=1), None,
                                       previous_digest=same, current_digest=same)
    assert (routing.action, routing.spent) == ("finalize", "no_progress")


def test_the_BOUNDS_are_checked_BEFORE_the_gaps():
    """Order is the design: a loop that picks an action it cannot afford reports the action it
    WANTED as the action it took. Every bound outranks every gap."""
    for kwargs in ({"budget_remaining": False},
                   {"rounds_used": 3, "max_rounds": 2},
                   {"previous_digest": "x", "current_digest": "x"}):
        routing = route_after_verification(_verdicts(unsupported=9), {"quoted": 9, "elsewhere": 9},
                                           stale_evidence=("a",), contradictions=(("a", "b"),),
                                           **kwargs)
        assert routing.action == "finalize", kwargs
        assert routing.spent in SPENT_REASONS and routing.spent != "clean"


# ---------------------------------------------------------------------------------------------
# Absence is not zero.

def test_a_memo_NOBODY_VERIFIED_does_not_read_as_verified_clean():
    """`ran` is the half that distinguishes them, and it must survive into `counts` so a reader of
    a durable routing row can tell "checked and clean" from "never checked"."""
    unverified = route_after_verification(None, None)
    assert unverified.counts["verdicts"]["ran"] is False
    assert unverified.counts["numbers"]["ran"] is False
    verified = route_after_verification(_verdicts(supported=1), {"quoted": 1, "matched": 1})
    assert verified.counts["verdicts"]["ran"] is True


def test_a_block_from_an_OLDER_WRITER_degrades_rather_than_raising():
    for junk in ({"verdicts": "not a list"}, {"verdicts": [None, 3, {"verdict": None}]},
                 {"quoted": "9"}, {}):
        assert route_after_verification(junk, junk).action in NEXT_ACTIONS


def test_the_progress_digest_ignores_ORDER_and_not_CONTENT():
    assert progress_digest(["a", "b"], ["e1"]) == progress_digest(["b", "a"], ["e1"])
    assert progress_digest(["a", "b"], ["e1"]) == progress_digest(["A", " b "], ["e1"])
    assert progress_digest(["a"], ["e1"]) != progress_digest(["a"], ["e2"])
    assert progress_digest(["a"], ["e1"], "draft one") != progress_digest(["a"], ["e1"], "draft two")


# ---------------------------------------------------------------------------------------------
# The registries, re-derived in both directions.

def _constructed_actions() -> set:
    """Every first positional argument this module hands `Routing(...)`, by AST.

    Comments are not AST nodes, which is the whole reason this is not a substring scan — the
    module's own docstring lists all five action names, so `assert "refresh" in source` would be
    satisfied by the prose that describes the bug.
    """
    tree = ast.parse(inspect.getsource(vr))
    out = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "Routing" and node.args
                and isinstance(node.args[0], ast.Constant)):
            out.add(node.args[0].value)
    return out


def test_every_constructed_action_is_registered_and_every_registered_one_is_reachable():
    constructed = _constructed_actions()
    assert constructed <= set(NEXT_ACTIONS), (
        f"these are returned but not registered: {sorted(constructed - set(NEXT_ACTIONS))}")
    assert set(NEXT_ACTIONS) <= constructed, (
        f"these are registered but no code path returns them: "
        f"{sorted(set(NEXT_ACTIONS) - constructed)}")


def test_every_spent_reason_is_reachable_and_registered():
    tree = ast.parse(inspect.getsource(vr))
    emitted = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "Routing"):
            continue
        for kw in node.keywords:
            if kw.arg == "spent" and isinstance(kw.value, ast.Constant):
                emitted.add(kw.value.value)
    assert emitted == set(SPENT_REASONS), (
        f"registry and emitters disagree: emitted-only {sorted(emitted - set(SPENT_REASONS))}, "
        f"registered-only {sorted(set(SPENT_REASONS) - emitted)}")


def test_the_two_vocabularies_stay_DISJOINT():
    """"What to do next" and "why there is no next" are different questions. One word in both is how
    `inert` came to mean two different failures in `node_repaired`."""
    assert not (set(NEXT_ACTIONS) & set(SPENT_REASONS))


def test_spent_is_set_ONLY_on_finalize():
    """A non-terminal routing carrying a `spent` reason would be a row saying the loop both
    continues and has stopped."""
    for routing in (route_after_verification(_verdicts(unsupported=1)),
                    route_after_verification(_verdicts(unclear=1)),
                    route_after_verification(_verdicts(supported=1), None,
                                              stale_evidence=("a",)),
                    route_after_verification(_verdicts(supported=1), None,
                                              contradictions=(("a", "b"),))):
        assert routing.spent is None, routing
        assert not routing.is_terminal


def test_a_gap_the_action_does_not_close_rides_in_RESIDUAL():
    """One step per round, so the ones not taken must still be named — otherwise a `retrieve` round
    silently drops the fact that three numbers are also in the wrong experiment."""
    routing = route_after_verification(_verdicts(unsupported_support=1),
                                       {"quoted": 5, "elsewhere": 2}, stale_evidence=("ev",))
    assert routing.action == "retrieve"
    assert any("not in a cited experiment" in line for line in routing.residual)
    assert any("no longer resolve" in line for line in routing.residual)
    assert not any("does not carry" in line for line in routing.residual), (
        "the gap this action DOES close must not also be reported as left open")
