"""The canary gate (doc 73 §4.2 G5, `Settings.upstream_verify`): on a task that declares `eval.canary`
the upstream gate's equivalence is ONE old/new pair under the canary, not `upstream.repeats` paired full
evaluations — and the engine, not the result, decides which profile a pass must carry.

Real CPU SGD through the doc-72 fixture; the runner reads its epoch count from the environment, so the
canary (`EPOCHS=3`) is visibly a different, cheaper slice than the node's own measurement.
"""
from __future__ import annotations

import pytest

from benchmarks._upstream_sgd import GENERAL, SOURCE, TRAIN
from looplab.adapters.repo_task import CanarySpec
from looplab.core.config import LEGACY_CONFIG_SNAPSHOT_DEFAULTS, Settings
from looplab.core.upstream_evidence import gate, gate_matches_policy
from looplab.engine.upstream_gate import gate_profile, upstream_verify_setting
from looplab.events.replay import fold
from tests.test_upstream_lane import fixture

_LOOP = "for epoch in range(30):"
_ENV_LOOP = 'import os\nfor epoch in range(int(os.environ.get("EPOCHS", "30"))):'


def _canary_run(tmp_path, *, verify="canary"):
    lane, store, generation, body = fixture(
        tmp_path, base_train=TRAIN.replace(_LOOP, _ENV_LOOP),
        source_files={"train.py": SOURCE.replace(_LOOP, _ENV_LOOP), "recipe.env": "MOMENTUM=0.2\n"})
    lane.task.eval.canary = CanarySpec(env={"EPOCHS": "3"}, timeout=10)
    lane.settings = lane.settings.model_copy(update={"upstream_verify": verify})
    (lane.rd / "config.snapshot.json").write_text(lane.settings.model_dump_json(), encoding="utf8")
    body["files"]["train.py"] = GENERAL.replace(_LOOP, _ENV_LOOP)
    return lane, store, generation, body


def test_the_setting_has_one_reader_defaults_to_canary_and_resumes_full():
    assert Settings().upstream_verify == "canary"
    assert upstream_verify_setting(Settings(upstream_verify="full")) == "full"
    assert upstream_verify_setting(object()) == "full"
    assert LEGACY_CONFIG_SNAPSHOT_DEFAULTS["upstream_verify"] == "full"


def test_a_task_without_a_canary_keeps_the_full_gate(tmp_path):
    lane, store, generation, body = fixture(tmp_path)
    assert gate_profile(Settings(), lane.task) == "full"
    lane.task.eval.canary = CanarySpec(timeout=10)
    assert gate_profile(Settings(), lane.task) == "canary"
    assert gate_profile(Settings(upstream_verify="full"), lane.task) == "full"


def test_one_canary_pair_measures_and_advances(tmp_path):
    lane, store, generation, body = _canary_run(tmp_path)
    made = lane.propose(body)
    checked = lane.check({"expected_generation": generation, "action_id": "canary-check",
                          "proposal_id": made["proposal_id"]})
    assert checked["status"] == "succeeded", checked
    result = checked["result"]
    eq = result["checks"][-1]
    assert eq["profile"] == "canary" and len(eq["values"][0]) == 1 == len(eq["values"][1])
    assert [r["label"] for r in result["executions"]][-2:] == ["old-canary0", "new-canary0"]
    assert len(result["executions"]) == 5, "test + old/new regression + ONE canary pair"
    source = fold(store.read_all()).nodes[0].task_metric
    assert eq["values"][0][0] != source, "the canary is its own (3-epoch) slice, not the node's run"
    assert gate(result)
    assert gate_matches_policy(result, lane.task.upstream, source, profile="canary")
    assert not gate_matches_policy(result, lane.task.upstream, source), "the engine expects full: refused"
    advanced = lane.advance({"expected_generation": generation, "action_id": "canary-advance",
                             "proposal_id": made["proposal_id"],
                             "expected_base_revision": body["expected_base_revision"],
                             "evidence_token": checked["evidence_token"]})
    assert advanced["status"] == "succeeded" and advanced["event_type"] == "base_advanced"


def test_full_is_the_opt_in_strict_gate(tmp_path):
    lane, store, generation, body = _canary_run(tmp_path, verify="full")
    made = lane.propose(body)
    checked = lane.check({"expected_generation": generation, "action_id": "full-check",
                          "proposal_id": made["proposal_id"]})
    assert checked["status"] == "succeeded", checked
    assert checked["result"]["checks"][-1]["profile"] == "full"
    assert len(checked["result"]["executions"]) == 7, "test + regression pair + 2x2 paired full runs"


def test_a_tampered_canary_verdict_is_not_a_pass(tmp_path):
    lane, store, generation, body = _canary_run(tmp_path)
    made = lane.propose(body)
    checked = lane.check({"expected_generation": generation, "action_id": "canary-check",
                          "proposal_id": made["proposal_id"]})
    result = checked["result"]
    eq = dict(result["checks"][-1])
    eq["values"] = [eq["values"][0], [eq["values"][1][0] + 1.0]]
    tampered = {**result, "checks": result["checks"][:-1] + [eq]}
    assert not gate(tampered)
    source = fold(store.read_all()).nodes[0].task_metric
    looser = {**result, "checks": result["checks"][:-1] + [{**result["checks"][-1], "tolerance": 10.0}]}
    assert not gate_matches_policy(looser, lane.task.upstream, source, profile="canary"), \
        "the tolerance is recomputed from the declaration"


@pytest.mark.parametrize("values", [[[], []], [[1.0], []]])
def test_a_canary_pair_that_did_not_run_fails_readably(values):
    from looplab.core.upstream_evidence import equivalence
    assert equivalence({"kind": "equivalence", "profile": "canary", "values": values, "passed": False})
    assert not equivalence({"kind": "equivalence", "profile": "canary", "values": values, "passed": True})
