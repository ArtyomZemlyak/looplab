"""The lighter gate for a FIX (doc 73 §2.3, track 1): `upstream.repair_gate: probes`.

A proposal promoting what a repair changed had no metric before the fix, so the paired full-source
repetitions compare a crashing recipe with a running one. Under the operator's `probes` the gate is
its tests, both regression sides, the repair probe (old fails, new passes) and the unchanged scorer —
and the CAS still recomputes the waiver from the declaration, never from the row.
"""
from __future__ import annotations

import pytest

from looplab.core.upstream_evidence import gate, gate_matches_policy
from looplab.engine.upstream_spec import normalize_upstream
from tests.test_upstream_repairs import repair_fixture


def test_the_declaration_keeps_full_out_of_the_pinned_shape():
    base = {"regressions": [{"name": "r", "command": ["x"], "artifacts": ["a"]}]}
    assert "repair_gate" not in normalize_upstream(base)
    assert normalize_upstream({**base, "repair_gate": "probes"})["repair_gate"] == "probes"
    with pytest.raises(ValueError):
        normalize_upstream({**base, "repair_gate": "none"})


def test_a_repair_promoted_under_probes_skips_the_repetitions_and_advances(tmp_path):
    lane, store, generation, proposal = repair_fixture(tmp_path, repair_gate="probes")
    made = lane.propose(proposal)
    checked = lane.check({"expected_generation": generation, "action_id": "fix-check",
                          "proposal_id": made["proposal_id"]})
    assert checked["status"] == "succeeded", checked
    result = checked["result"]
    assert [c["kind"] for c in result["checks"]] == ["test", "regression", "repair", "equivalence_waived"]
    assert len(result["executions"]) == 5, "1 test + 2 regression + 2 repair, no source repetitions"
    assert gate(result)
    advanced = lane.advance({"expected_generation": generation, "action_id": "fix-advance",
                             "proposal_id": made["proposal_id"],
                             "expected_base_revision": proposal["expected_base_revision"],
                             "evidence_token": checked["evidence_token"]})
    assert advanced["event_type"] == "base_advanced"


def test_the_waiver_is_the_declarations_never_the_rows(tmp_path):
    lane, store, generation, proposal = repair_fixture(tmp_path, repair_gate="probes")
    made = lane.propose(proposal)
    result = lane.check({"expected_generation": generation, "action_id": "fix-check",
                         "proposal_id": made["proposal_id"]})["result"]
    full = {k: v for k, v in lane.task.upstream.items() if k != "repair_gate"}
    assert gate_matches_policy(result, lane.task.upstream, 0.0, repair_required=True)
    assert not gate_matches_policy(result, full, 0.0, repair_required=True), "full demands repetitions"
    assert not gate_matches_policy(result, lane.task.upstream, 0.0, repair_required=False), (
        "a waiver on a proposal that promotes no repair grants nothing")
