"""Operator probes cannot replace new implementation helpers in the candidate."""
import sys

import pytest

from benchmarks._upstream_sgd import GENERAL
from looplab.core.errors import UpstreamRefusal
from tests.test_upstream_lane import fixture


@pytest.mark.parametrize("mask", ["none", "probe", "documentation_probe", "recipe", "documentation_recipe"])
def test_regression_cannot_mask_an_added_capability_helper(tmp_path, mask):
    helper = "runner_support.py"
    good = "# MOMENTUM flag\ndef momentum(value): return float(value)\n"
    bad = "# MOMENTUM flag\ndef momentum(value): return 0.8 if float(value) == 0.0 else float(value)\n"
    policy = {"repeats": 2,
        "tests": [{"name": "syntax", "command": [sys.executable, "-m", "py_compile", "train.py"]}],
        "regressions": [{"name": "old_recipe", "command": [sys.executable, "train.py"],
            "files": {helper: good} if "probe" in mask else {}, "artifacts": ["predictions.json"]}]}
    lane, store, generation, body = fixture(tmp_path, upstream_policy=policy)
    lane.task.edit_surface.append(helper)
    (lane.rd / "task.snapshot.json").write_text(lane.task.model_dump_json(), encoding="utf8")
    body["files"].update({helper: bad, "train.py": GENERAL.replace(
        'MOMENTUM = float(settings.get("MOMENTUM", "0.0"))',
        'from runner_support import momentum\nMOMENTUM = momentum(settings.get("MOMENTUM", "0.0"))')})
    if "documentation" in mask:
        body["documentation_path"] = helper
    if "recipe" in mask:
        body["recipe_files"][helper] = good
    before = store.path.read_bytes()
    try:
        made = lane.propose(body)
    except UpstreamRefusal as refusal:
        expected = "upstream_capability_not_absorbed" if "recipe" in mask else "upstream_probe_masks_capability"
        assert mask != "none" and refusal.code == expected, refusal
        assert store.path.read_bytes() == before and not (lane.rd / "upstream").exists()
        return
    checked = lane.check({"expected_generation": generation, "action_id": "helper-gate",
        "proposal_id": made["proposal_id"]})
    assert mask == "none", f"Probe replaced the new helper and bought a {checked['status']} gate"
    assert helper in made["capability_paths"]
    assert checked["status"] == "failed"
    assert next(c for c in checked["result"]["checks"] if c["kind"] == "regression")["passed"] is False
    assert checked["result"]["checks"][-1]["passed"] is True
