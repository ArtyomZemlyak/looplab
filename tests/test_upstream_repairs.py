"""Real failing recipe repair, generalized opt-in runner and old-fail/new-pass gate."""
import sys

import pytest

from looplab.core.errors import UpstreamRefusal
from looplab.core.models import Idea
from looplab.engine.upstream_state import events_for
from looplab.events.replay import fold
from tests.test_upstream_lane import fixture, GENERAL


def repair_fixture(tmp_path):
    original = GENERAL.replace('w, velocity =', 'assert MOMENTUM >= 0, "negative momentum unsupported"\nw, velocity =')
    trigger = {"recipe.env": "MOMENTUM=-2\nALLOW_SENTINEL=1\n"}
    policy = {"repeats": 2, "tests": [{"name": "syntax", "command": [sys.executable, "-m", "py_compile", "train.py"]}],
        "regressions": [{"name": "prior_recipe", "command": [sys.executable, "train.py"], "artifacts": ["predictions.json"]}],
        "repair_probes": [{"name": "negative_recipe", "command": [sys.executable, "train.py"], "files": trigger, "artifacts": ["predictions.json"]}]}
    lane, store, generation, proposal = fixture(tmp_path, base_train=original,
        source_files={"recipe.env": "MOMENTUM=0.2\n"}, repair_from={"recipe.env": "MOMENTUM=-2\n"}, upstream_policy=policy)
    store.append("node_created", {"node_id": 1, "operator": "improve", "parent_ids": [0],
        "idea": Idea(operator="improve").model_dump(), "files": trigger})
    rows = lane.read(generation)["candidates"]["rows"]
    row, = rows
    assert row["origin"] == "repair" and row["classification"] == "capability"
    assert row["pending_trigger_nodes"] == [1] and row["trigger_tokens"] == {"recipe.env": ["MOMENTUM=-2"]}
    generalized = original.replace('assert MOMENTUM >= 0, "negative momentum unsupported"',
        'if settings.get("ALLOW_SENTINEL", "0") == "1" and MOMENTUM == -2: MOMENTUM = 0.2\nassert MOMENTUM >= 0, "negative momentum unsupported"')
    proposal.update(hunk_hashes=[row["hunk_hash"]], files={"train.py": generalized, "README.md": "ALLOW_SENTINEL: old default 0; 1 enables the documented -2 momentum sentinel.\n"},
        recipe_files=trigger, flag={"name": "ALLOW_SENTINEL", "default": "0", "enabled": "1"})
    return lane, store, generation, proposal


def test_environment_repair_reaches_base_only_after_actual_trigger_and_equivalence(tmp_path):
    lane, store, generation, proposal = repair_fixture(tmp_path)
    made = lane.propose(proposal)
    checked = lane.check({"expected_generation": generation, "action_id": "repair-check", "proposal_id": made["proposal_id"]})
    assert checked["status"] == "succeeded", checked
    repair, = [r for r in checked["result"]["checks"] if r["kind"] == "repair"]
    assert repair["passed"]
    old, new = [r for r in checked["result"]["executions"] if "negative_recipe" in r["label"]]
    assert old["exit_code"] != 0 and new["exit_code"] == 0 and new["artifacts"]
    assert len(checked["result"]["executions"]) == 9
    lane.advance({"expected_generation": generation, "action_id": "repair-advance", "proposal_id": made["proposal_id"],
        "expected_base_revision": proposal["expected_base_revision"], "evidence_token": checked["evidence_token"]})
    from looplab.engine.upstream_workspace import materialization_plan
    spec, pending, receipt = materialization_plan(lane.task.repo_spec(), fold(store.read_all()).nodes[1], events_for(lane.rd))
    assert spec["effective_seed_base"] == made["selector"] and receipt["status"] == "rebased"
    assert pending.files == {"recipe.env": "MOMENTUM=-2\nALLOW_SENTINEL=1\n"}
    assert (tmp_path / "owner" / "train.py").read_text().find('if settings.get("ALLOW_SENTINEL"') == -1


def test_canary_without_recorded_trigger_is_refused_before_claim(tmp_path):
    lane, store, generation, proposal = repair_fixture(tmp_path)
    # A new launched declaration is required; simulate that separate task fixture.
    lane.task.upstream["repair_probes"][0]["files"] = {"recipe.env": "MOMENTUM=0.2\nALLOW_SENTINEL=1\n"}
    events = store.read_all()
    events[0].data["upstream"] = lane.task.upstream
    # Store fixture rewrite only; production policy remains pinned and rejects edits.
    store.path.write_text("".join(e.model_dump_json() + "\n" for e in events), encoding="utf8")
    before = store.path.read_bytes()
    with pytest.raises(UpstreamRefusal, match="recorded failing pending recipe trigger"):
        lane.propose(proposal)
    assert store.path.read_bytes() == before


def test_post_terminal_repair_cannot_nominate_an_unchanged_scientific_recipe(tmp_path):
    lane, store, generation, _ = fixture(tmp_path)
    store.append("node_repaired", {"node_id": 0, "generation": 0, "attempt": 2,
        "files": {"recipe.env": "MOMENTUM=0.9\n"}, "rationale": "Ignored post-terminal garbage"})
    store.append("node_created", {"node_id": 2, "operator": "improve", "parent_ids": [],
        "idea": Idea(operator="improve").model_dump(), "files": {"recipe.env": "MOMENTUM=0.2\n"}})
    row, = [r for r in lane.read(generation)["candidates"]["rows"] if r["path"] == "recipe.env"]
    assert row["origin"] == "idea" and row["classification"] == "recipe" and row["pending_trigger_nodes"] == []
def test_unused_identical_repair_probe_cannot_change_a_regression(tmp_path):
    """Probe membership is its declared lane, not dictionary equality across lists."""
    probe = {"name": "old_recipe", "command": [sys.executable, "train.py"], "artifacts": ["predictions.json"]}
    lane, store, generation, proposal = fixture(tmp_path, upstream_policy={"repeats": 2,
        "tests": [{"name": "syntax", "command": [sys.executable, "-m", "py_compile", "train.py"]}],
        "regressions": [probe], "repair_probes": [dict(probe)]})
    made = lane.propose(proposal)
    assert made["repair_trigger_nodes"] == []
    checked = lane.check({"expected_generation": generation, "action_id": "separate-probe-lanes", "proposal_id": made["proposal_id"]})
    assert checked["status"] == "succeeded", checked
    assert [c["kind"] for c in checked["result"]["checks"]] == ["test", "regression", "equivalence"]
