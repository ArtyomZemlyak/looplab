"""Operator probes cannot replace new implementation helpers in the candidate."""
import sys

import pytest

from benchmarks._upstream_sgd import GENERAL
from looplab.core.errors import UpstreamRefusal
from tests.test_upstream_lane import fixture


def helper_fixture(tmp_path, mask):
    helper = "runner_support.py"
    good = "# MOMENTUM flag\ndef momentum(value): return float(value)\n"
    bad = "# MOMENTUM flag\ndef momentum(value): return 0.8 if float(value) == 0.0 else float(value)\n"
    policy = {"repeats": 2,
        "tests": [{"name": "syntax", "command": [sys.executable, "-m", "py_compile", "train.py"]}],
        "regressions": [{"name": "old_recipe", "command": [sys.executable, "train.py"],
            "files": {helper.upper() if mask == "case_probe" else helper: good} if "probe" in mask else {},
            "artifacts": ["predictions.json"]}]}
    lane, store, generation, body = fixture(tmp_path, upstream_policy=policy)
    lane.task.edit_surface.append(helper)
    if mask.startswith("case_"):
        # Linux's surface matching is case-sensitive. Authorize both spellings
        # explicitly so this case reaches the portable anti-masking boundary;
        # do not widen production edit permissions to satisfy the test.
        lane.task.edit_surface.append(helper.upper())
    (lane.rd / "task.snapshot.json").write_text(lane.task.model_dump_json(), encoding="utf8")
    body["files"].update({helper: bad, "train.py": GENERAL.replace(
        'MOMENTUM = float(settings.get("MOMENTUM", "0.0"))',
        'from runner_support import momentum\nMOMENTUM = momentum(settings.get("MOMENTUM", "0.0"))')})
    if "documentation" in mask:
        body["documentation_path"] = helper
    if "recipe" in mask:
        if mask == "case_recipe_deleted":
            body["recipe_deleted"] = [helper.upper()]
        else:
            body["recipe_files"][helper.upper() if mask == "case_recipe" else helper] = good
    return lane, store, generation, body


@pytest.mark.parametrize("mask", ["none", "probe", "documentation_probe", "case_probe", "recipe",
    "documentation_recipe", "case_recipe", "case_recipe_deleted"])
def test_regression_cannot_mask_an_added_capability_helper(tmp_path, mask):
    lane, store, generation, body = helper_fixture(tmp_path, mask)
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
    assert "runner_support.py" in made["capability_paths"]
    assert checked["status"] == "failed"
    assert next(c for c in checked["result"]["checks"] if c["kind"] == "regression")["passed"] is False
    assert checked["result"]["checks"][-1]["passed"] is True


def test_a_helper_outside_the_surface_is_refused_before_any_claim(tmp_path):
    lane, store, _, body = helper_fixture(tmp_path, "probe")
    lane.task.edit_surface.remove("runner_support.py")
    before = store.path.read_bytes()
    with pytest.raises(UpstreamRefusal) as refusal:
        lane.propose(body)
    assert refusal.value.code == "upstream_patch_forbidden"
    assert "outside_surface" in str(refusal.value)
    assert store.path.read_bytes() == before and not (lane.rd / "upstream").exists()


@pytest.mark.parametrize("operation", ["check", "advance"])
def test_saved_masked_proposal_cannot_buy_new_work_or_advance_after_upgrade(tmp_path, monkeypatch, operation):
    lane, store, generation, body = helper_fixture(tmp_path, "probe")
    request = {"expected_generation": generation, "action_id": "historical-gate"}
    # Model the prior admission bug only. All training, scorer and gate evidence
    # are real; a saved false pass must not become permission after an upgrade.
    with monkeypatch.context() as old:
        old.setattr(lane, "_validate_shared_patch", lambda *args: None)
        made = lane.propose(body)
        request["proposal_id"] = made["proposal_id"]
        gate = lane.check(request)
        assert gate["status"] == "succeeded", gate
    before = store.path.read_bytes()
    assert lane.propose(body) == made and lane.check(request) == gate
    fresh = {**request, "action_id": "after-upgrade"}
    if operation == "advance":
        fresh.update(expected_base_revision=body["expected_base_revision"], evidence_token=gate["evidence_token"])
    with pytest.raises(UpstreamRefusal) as refusal:
        getattr(lane, operation)(fresh)
    assert refusal.value.code == "upstream_probe_masks_capability"
    assert store.path.read_bytes() == before
