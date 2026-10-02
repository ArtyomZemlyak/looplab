"""Corrupt real measured gate replies; a digest alone is not complete evidence."""
from copy import deepcopy
import json
import math
import statistics
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
    elif case == "artifacts":
        for row in result["executions"]:
            if row["label"] in ("old-old_recipe", "new-old_recipe"):
                row["artifacts"] = {}
    elif case == "proposal":
        receipt["proposal_id"] = "up_" + "0" * 24
    elif case == "version":
        receipt["version"] = True
    # The wire still has a consistent hash: semantic completeness must be checked.
    receipt["evidence_token"] = digest(result)


@pytest.mark.parametrize("case", ["equivalence", "test", "regression", "samples", "statistics", "mean",
    "source", "profile", "execution", "stages", "stage_fields", "stage_failed", "stage_timeout", "valid_timeout", "artifacts", "proposal", "version"])
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


@pytest.mark.parametrize("missing", ["statistics", "artifacts"])
def test_incomplete_gate_history_is_unavailable(measured, missing):
    lane, store, request, receipt = measured
    page = deepcopy(lane.read(request["expected_generation"]))
    gate = next(r for r in page["history"] if r["type"] == "upstream_gate_finished")
    damage(gate, missing)
    api = HarnessAPI("http://localhost", transport=httpx.MockTransport(lambda r: httpx.Response(200, json=page)))
    try:
        result = api.upstream_status("run", request["expected_generation"])
        assert result["outcome"] == "unavailable" and "body" not in result, result
    finally:
        api.client.close()


@pytest.mark.parametrize("case", ["cost_sum_overflow", "metric_overflow", "extra_nonfinite"])
def test_extreme_wire_numbers_refuse_without_tool_crash_or_retry(measured, case):
    lane, store, request, original = measured
    receipt = deepcopy(original)
    result = receipt["result"]
    if case == "cost_sum_overflow":
        for row in result["executions"]:
            row["seconds"] = 10 ** 308
        result["eval_seconds"] = 10 ** 308
        receipt["evidence_token"] = digest(result)
    else:
        # 1e309 is valid JSON syntax, but decoding it yields a nonfinite float.
        # Unknown extensions remain readable only if canonicalization is safe.
        target = result["executions"][0] if case == "metric_overflow" else result
        target["metric" if case == "metric_overflow" else "extra"] = "WIRE_OVERFLOW"
    wire = json.dumps(receipt).replace('"WIRE_OVERFLOW"', '1e309')
    before, calls = store.path.read_bytes(), []
    api = HarnessAPI("http://localhost", transport=httpx.MockTransport(
        lambda r: (calls.append(r), httpx.Response(200, content=wire))[1]))
    try:
        reply = api.upstream_write("run", "check", request)
        assert reply.get("outcome") == "unknown" and "body" not in reply, reply
        page = deepcopy(lane.read(request["expected_generation"]))
        saved = next(row for row in page["history"] if row["type"] == "upstream_gate_finished")
        saved.update({k: receipt[k] for k in ("result", "evidence_token")})
        api.client.close()
        wire = json.dumps(page).replace('"WIRE_OVERFLOW"', '1e309')
        api = HarnessAPI("http://localhost", transport=httpx.MockTransport(
            lambda r: httpx.Response(200, content=wire)))
        reply = api.upstream_status("run", request["expected_generation"])
        assert reply.get("outcome") == "unavailable" and "body" not in reply, reply
    finally:
        api.client.close()
    assert len(calls) == 1 and store.path.read_bytes() == before
    assert lane.check(request) == original


def test_finite_result_extensions_remain_readable_and_hash_bound(measured):
    lane, store, request, original = measured
    receipt = deepcopy(original)
    receipt["result"]["extra"] = {"future_observation": 1.5}
    receipt["evidence_token"] = digest(receipt["result"])
    before = store.path.read_bytes()
    api = HarnessAPI("http://localhost", transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json=receipt)))
    try:
        reply = api.upstream_write("run", "check", request)
        assert not reply.get("code") and reply["body"] == receipt, reply
        receipt["evidence_token"] = original["evidence_token"]
        reply = api.upstream_write("run", "check", request)
        assert reply.get("outcome") == "unknown" and "body" not in reply, reply
    finally:
        api.client.close()
    assert store.path.read_bytes() == before and lane.check(request) == original


@pytest.mark.parametrize("case", ["zero", "boundary", "negative", "false_failure"])
def test_rounding_cannot_change_the_sample_bound_verdict(measured, case):
    lane, store, request, original = measured
    receipt = deepcopy(original)
    result = receipt["result"]
    eq = result["checks"][-1]
    old = eq["values"][0]
    # Corrupt only the transport reply. Keep matching executions and exact hash:
    # a numeric-consistency epsilon must not become a scientific allowance.
    shift = 0.0 if case == "false_failure" else -5e-13 if case == "negative" else 5e-13
    eq["values"][1] = [v + shift for v in old]
    eq["means"] = [statistics.mean(v) for v in eq["values"]]
    eq["sem"] = [statistics.stdev(v) / math.sqrt(len(v)) for v in eq["values"]]
    actual_delta = eq["means"][1] - eq["means"][0]
    eq["tolerance"] = abs(actual_delta) - 1e-13 if case == "boundary" else 0.0
    eq["delta"] = 5e-13 if case == "false_failure" else eq["tolerance"]
    eq["source_reproduced"] = True
    eq["passed"] = result["passed"] = case != "false_failure"
    receipt["status"] = "succeeded" if result["passed"] else "failed"
    for row in result["executions"]:
        if row["label"].startswith("new-source"):
            row["metric"] = eq["values"][1][int(row["label"].removeprefix("new-source"))]
    receipt["evidence_token"] = digest(result)
    before, calls = store.path.read_bytes(), []
    api = HarnessAPI("http://localhost", transport=httpx.MockTransport(
        lambda r: (calls.append(r), httpx.Response(200, json=receipt))[1]))
    try:
        reply = api.upstream_write("run", "check", request)
        assert reply.get("outcome") == "unknown" and "body" not in reply, reply
        page = deepcopy(lane.read(request["expected_generation"]))
        saved = next(row for row in page["history"] if row["type"] == "upstream_gate_finished")
        saved.update({k: receipt[k] for k in ("result", "evidence_token")})
        api.client.close()
        api = HarnessAPI("http://localhost", transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json=page)))
        reply = api.upstream_status("run", request["expected_generation"])
        assert reply.get("outcome") == "unavailable" and "body" not in reply, reply
    finally:
        api.client.close()
    assert len(calls) == 1 and store.path.read_bytes() == before
    assert lane.check(request) == original


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


def test_actual_failed_artifact_gate_remains_readable_through_mcp(tmp_path):
    lane, store, generation, proposal = fixture(tmp_path, upstream_policy={"repeats": 2,
        "tests": [{"name": "syntax", "command": [sys.executable, "-m", "py_compile", "train.py"]}],
        "regressions": [{"name": "old_recipe", "command": [sys.executable, "train.py"],
            "artifacts": ["predictions.json", "missing.json"]}]})
    made = lane.propose(proposal)
    request = {"expected_generation": generation, "action_id": "missing-artifact-check",
        "proposal_id": made["proposal_id"]}
    original = lane.check(request)
    assert original["status"] == "failed"
    assert len(original["result"]["executions"]) == 7
    assert original["result"]["checks"][-1]["passed"]  # actual full source SGD still reproduced
    before = store.path.read_bytes()
    api = HarnessAPI("http://localhost", transport=httpx.MockTransport(lambda r: httpx.Response(200, json=original)))
    try:
        result = api.upstream_write("run", "check", request)
        assert result["body"] == original and result["body"]["status"] == "failed", result
    finally:
        api.client.close()
    assert store.path.read_bytes() == before
