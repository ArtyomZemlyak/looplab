"""Corrupt real measured gate replies; a digest alone is not complete evidence."""
from copy import deepcopy
import sys

import httpx
import pytest

from looplab.engine.upstream_state import digest
from looplab.core.errors import UpstreamRefusal
from looplab.harness.mcp_server import HarnessAPI
from tests.test_upstream_lane import fixture


@pytest.fixture(scope="module")
def measured(tmp_path_factory):
    lane, store, generation, proposal = fixture(tmp_path_factory.mktemp("receipts"))
    made = lane.propose(proposal)
    request = {"expected_generation": generation, "action_id": "measured-check", "proposal_id": made["proposal_id"]}
    receipt = lane.check(request)
    assert receipt["status"] == "succeeded", receipt
    return lane, store, request, receipt


def damage(receipt, case):
    result = receipt["result"]
    eq = result["checks"][-1]
    if case in ("equivalence", "test", "regression"):
        result["checks"] = [r for r in result["checks"] if r["kind"] != case]
    elif case == "samples":
        eq["values"][1].pop()
    elif case == "statistics":
        del eq["sem"]
    elif case == "mean":
        eq["means"][0] += 1
    elif case == "source":
        eq["source_reproduced"] = False
    elif case == "profile":
        eq["profile"] = "quick"
    elif case == "execution":
        result["executions"].pop()
        result["eval_seconds"] = sum(r["seconds"] for r in result["executions"])
    elif case == "stages":
        result["executions"][-1]["stages"] = "not a stage receipt"
    elif case == "stage_fields":
        del result["executions"][-1]["stages"][0]["exit_code"]
    elif case == "stage_failed":
        result["executions"][-1]["stages"][0]["status"] = "fail"
        result["executions"][-1]["stages"][0]["exit_code"] = 1
    elif case == "stage_timeout":
        result["executions"][-1]["stages"][0]["status"] = "timeout"
    elif case == "valid_timeout":
        result["executions"][0]["timed_out"] = True
    elif case == "proposal":
        receipt["proposal_id"] = "up_" + "0" * 24
    elif case == "version":
        receipt["version"] = True
    # The wire still has a consistent hash: semantic completeness must be checked.
    receipt["evidence_token"] = digest(result)


@pytest.mark.parametrize("case", ["equivalence", "test", "regression", "samples", "statistics", "mean",
    "source", "profile", "execution", "stages", "stage_fields", "stage_failed", "stage_timeout", "valid_timeout", "proposal", "version"])
def test_incomplete_passing_ack_is_unknown_and_never_retried(measured, case):
    lane, store, request, original = measured
    receipt = deepcopy(original)
    damage(receipt, case)
    before, calls = len(store.read_all()), []
    api = HarnessAPI("http://localhost", transport=httpx.MockTransport(
        lambda r: (calls.append(r), httpx.Response(200, json=receipt))[1]))
    try:
        result = api.upstream_write("run", "check", request)
    finally:
        api.client.close()
    assert result["outcome"] == "unknown" and result["reason"] == "invalid_upstream_receipt", result
    assert "body" not in result
    assert len(calls) == 1 and len(store.read_all()) == before
    assert lane.check(request) == original


def test_incomplete_gate_history_is_unavailable(measured):
    lane, store, request, receipt = measured
    page = deepcopy(lane.read(request["expected_generation"]))
    gate = next(r for r in page["history"] if r["type"] == "upstream_gate_finished")
    damage(gate, "statistics")
    api = HarnessAPI("http://localhost", transport=httpx.MockTransport(lambda r: httpx.Response(200, json=page)))
    try:
        result = api.upstream_status("run", request["expected_generation"])
        assert result["outcome"] == "unavailable" and "body" not in result, result
    finally:
        api.client.close()


@pytest.fixture(scope="module")
def advanced(measured):
    lane, store, request, checked = measured
    # pytest changes its process environment between tests; CAS must use a fresh
    # gate in this fixture's actual environment, not bypass that evidence fence.
    checked = lane.check({**request, "action_id": "measured-advance-gate"})
    assert checked["status"] == "succeeded", checked
    proposed = next(e.data for e in store.read_all() if e.type == "upstream_proposed")
    body = {"expected_generation": request["expected_generation"], "action_id": "measured-advance",
        "proposal_id": request["proposal_id"], "expected_base_revision": proposed["expected_base_revision"],
        "evidence_token": checked["evidence_token"]}
    return body, lane.advance(body)


@pytest.mark.parametrize("field", ["proposal_id", "from_revision", "evidence_token"])
def test_advance_ack_must_match_requested_cas_evidence(advanced, field):
    request, original = advanced
    receipt = deepcopy(original)
    receipt[field] = ("up_" + "0" * 24) if field == "proposal_id" else "0" * 64
    api = HarnessAPI("http://localhost", transport=httpx.MockTransport(lambda r: httpx.Response(200, json=receipt)))
    try:
        result = api.upstream_write("run", "advance", request)
        assert result["outcome"] == "unknown" and "body" not in result, result
    finally:
        api.client.close()


def test_operator_tests_are_required_before_proposal_work(tmp_path):
    lane, store, generation, proposal = fixture(tmp_path, upstream_policy={"repeats": 2, "tests": [],
        "regressions": [{"name": "old_recipe", "command": [sys.executable, "train.py"],
            "artifacts": ["predictions.json"]}]})
    before = store.path.read_bytes()
    with pytest.raises(UpstreamRefusal) as refusal:
        lane.propose(proposal)
    assert refusal.value.code == "upstream_tests_required"
    assert store.path.read_bytes() == before and not (lane.rd / "upstream").exists()
