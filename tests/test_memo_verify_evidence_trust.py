"""A D8 research claim may not be ratified on a node this run refuses to select on.

`finalize_verified_evidence` is the gate between a verifier's `supported` verdict and a row in the
shared cross-run `research_claims.jsonl`, which later runs retrieve as evidence. Every check it made
asked whether a cited node still EXISTS in the state the verifier saw it in — tombstoned, aborted,
terminal, same generation — and none asked whether the run trusts its NUMBER.

So a node the run itself refuses to select on could ground a durable cross-run claim: SALVAGED and
not admitted (`metric_salvaged`, i.e. nobody measured the value — v5 node 0's shape) or hard
reward-hack/leakage flagged under `trust_gate=gate|block`. That is the identical leak
`engine/memory.py::unreliable_metric_ids` was written to close for `lessons.jsonl`/`skills/`, at the
only other writer of a durable claim grounded in a node — and CLAUDE.md carried it as a stated
residual ("STILL OPEN") rather than a regression until 2026-08-15.

Driven against the real predicate over a real folded-shape `RunState`, both directions.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from looplab.core.advisory_payloads import RESEARCH_RECEIPT_VERSION
from looplab.core.models import Idea, Node, NodeStatus, RunState
from looplab.engine.metric_salvage import SALVAGE_VIOLATION
from looplab.trust.memo_verify import finalize_verified_evidence


def _receipt(nodes: int, urls: int = 0) -> dict:
    return {"v": RESEARCH_RECEIPT_VERSION,
            "node_refs_total": nodes, "node_refs_retained": nodes, "node_refs_omitted": 0,
            "url_refs_total": urls, "url_refs_retained": urls, "url_refs_omitted": 0,
            "complete": True}


def _claim(node_ids) -> dict:
    return {"statement": "the KL term helps", "node_ids": list(node_ids), "urls": [],
            "url_identities": [], "evidence_receipt": _receipt(len(node_ids))}


def _verdict(node_ids) -> dict:
    return {"verdict": "supported",
            "evidence": {"v": RESEARCH_RECEIPT_VERSION, "complete": True,
                         "node_refs": [{"node_id": nid, "generation": 0} for nid in node_ids],
                         "url_identities": []}}


def _state(*, violations=(), flagged=(), trust_gate="audit") -> RunState:
    """One evaluated node with a real metric — the shape a claim is normally ratified on.

    Built the way the fold leaves it (a violation ROW on the node, a `reward_hacks` entry keyed to
    the node's current generation) rather than by poking a derived field, because
    `unreliable_metric_ids` reads both through the SAME helpers the fold does.
    """
    state = RunState()
    node = Node(id=3, operator="draft", idea=Idea(operator="draft", params={"x": 1.0}),
                parent_id=None, status=NodeStatus.evaluated, metric=0.74, attempt=0)
    node.violations = list(violations)
    state.nodes = {3: node}
    state.trust_gate = trust_gate
    state.reward_hacks = [{"node_id": nid, "generation": 0,
                           "signals": [{"signal": "grader_access"}]} for nid in flagged]
    return state


def test_a_clean_node_still_ratifies_the_claim():
    """The positive control: nothing about the new rung may narrow the ordinary path."""
    evidence, reason = finalize_verified_evidence(_claim([3]), _verdict([3]), _state())
    assert reason == ""
    assert evidence is not None and evidence["node_ids"] == [3]


def test_a_salvaged_and_unadmitted_node_cannot_ground_a_cross_run_claim():
    """`metric_salvaged` means the value was recovered from a FAILED eval by the operator's declared
    reader and never admitted — nobody measured it. The run already refuses to make such a node
    champion or to breed from it; publishing a claim grounded in it sends the same unmeasured number
    into the shared store, where a later run reads it as evidence."""
    state = _state(violations=[{"name": SALVAGE_VIOLATION, "detail": "no metric"}])
    from looplab.engine.memory import unreliable_metric_ids
    assert unreliable_metric_ids(state) == {3}, "the shared predicate does not see this node"

    evidence, reason = finalize_verified_evidence(_claim([3]), _verdict([3]), state)
    assert evidence is None
    assert "refuses to select on" in reason
    # A DISTINCT reason from the lifecycle one: "the run moved under the verdict" and "the run does
    # not trust this number" have different remedies, and one wording for both hides the second.
    assert "lifecycle" not in reason


def test_a_hard_flagged_node_under_an_ENFORCING_gate_cannot_ground_one_either():
    state = _state(flagged=(3,), trust_gate="gate")
    from looplab.engine.memory import unreliable_metric_ids
    assert 3 in unreliable_metric_ids(state)
    evidence, reason = finalize_verified_evidence(_claim([3]), _verdict([3]), state)
    assert evidence is None and "refuses to select on" in reason


def test_the_same_flag_under_audit_still_ratifies_because_the_operator_did_not_enforce_it():
    """The rung never enforces a gate the operator turned off — it only refuses to publish across
    runs what THIS run already refuses to select on. `unreliable_metric_ids` owns that rule and this
    site must not restate it."""
    state = _state(flagged=(3,), trust_gate="audit")
    from looplab.engine.memory import unreliable_metric_ids
    assert unreliable_metric_ids(state) == set()
    evidence, reason = finalize_verified_evidence(_claim([3]), _verdict([3]), state)
    assert reason == "" and evidence is not None


def test_a_constraint_violation_keeps_its_place_because_its_number_was_measured():
    """Deliberately NOT `promotion_eligible_nodes`. A node that breached a hard CONSTRAINT is
    infeasible, but its metric WAS measured by the scoring path and its exclusion is a fact about the
    bound — `engine/lessons_distill.py` has held that stance since the first salvage fix, and routing
    this through promotion eligibility would silently retire it."""
    state = _state(violations=[{"name": "constraint", "detail": "max_params"}])
    from looplab.engine.memory import unreliable_metric_ids
    assert unreliable_metric_ids(state) == set()
    evidence, reason = finalize_verified_evidence(_claim([3]), _verdict([3]), state)
    assert reason == "" and evidence is not None


@pytest.mark.parametrize("kwargs", [
    {"violations": [{"name": SALVAGE_VIOLATION}]},
    {"flagged": (3,), "trust_gate": "block"},
])
def test_the_refusal_reaches_the_finalize_caller_as_an_unverified_claim(kwargs):
    """What the engine does with the answer: `engine/lessons.py` persists a cross-run claim only on a
    non-None evidence set, so a refusal here is exactly "this claim is not ratified"."""
    evidence, reason = finalize_verified_evidence(_claim([3]), _verdict([3]), _state(**kwargs))
    assert (evidence, bool(reason)) == (None, True)


def test_a_state_the_predicate_cannot_read_refuses_rather_than_ratifies():
    """The containment here is the OPPOSITE decision from `unreliable_metric_ids`' own, on purpose.

    That predicate has no `except` because swallowing there returns the EMPTY set — "everything is
    reliable" about a state nobody could read, the one wrong answer. Here the answer runs the other
    way: a state this cannot be evaluated over is not permission to ratify.

    It is contained at ALL because the alternative was measured: an incomplete state (a partial
    duck-type reaching `store_research_claims`) raised out of this function into finalize, and the
    claim was persisted with no `verification` key whatsoever — no verdict, no reason, nothing an
    operator could read. A refusal with a stated reason is strictly better than that in both
    directions.
    """
    partial = SimpleNamespace(nodes={3: _state().nodes[3]}, aborted_nodes=[])   # no trust_gate
    evidence, reason = finalize_verified_evidence(_claim([3]), _verdict([3]), partial)
    assert evidence is None
    assert "could not establish" in reason and "trusts" in reason
