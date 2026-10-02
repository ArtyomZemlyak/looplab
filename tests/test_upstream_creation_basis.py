"""Only replay-applied authoring events can change a pending overlay's basis."""
import sys

import pytest

from looplab.engine.upstream_workspace import materialization_plan
from looplab.events.replay import fold
from looplab.runtime.command_eval import run_command_eval
from tests.test_upstream_multibase import evaluate, materialize, twice


@pytest.mark.parametrize("ignored", ["generation", "parent", "idea", "terminal"])
def test_ignored_creation_cannot_change_a_scientific_overlay_basis(tmp_path, ignored):
    # The terminal case records a genuine experiment on base one before the
    # second measured promotion; the others retain the pending base-one overlay.
    before_second = (lambda lane, store: evaluate(lane, store, 5)) if ignored == "terminal" else None
    lane, store, _, _, _, second = twice(tmp_path, before_second=before_second)
    # The terminal case is ignored while settled; reset then retains its files.
    original = fold(store.read_all()).nodes[5].model_dump()
    accepted_seq = fold(store.read_all()).nodes[5].creation_event_seq
    body = {"node_id": 5, "generation": 0, "operator": "draft", "parent_ids": [],
        "idea": {"operator": "draft", "title": "Ignored worker reply"}, "files": {"train.py": "ignored\n"}}
    if ignored == "generation":
        body["generation"] = 1
    elif ignored == "parent":
        body["parent_ids"], body["parent_generations"] = [0], {"0": 999}
    elif ignored == "idea":
        body["idea"] = "invalid authoring response"
    store.append("node_created", body)
    assert fold(store.read_all()).nodes[5].model_dump() == original
    assert fold(store.read_all()).nodes[5].creation_event_seq == accepted_seq
    assert "creation_event_seq" not in original
    if ignored == "terminal":
        store.append("node_reset", {"node_id": 5})
    events = store.read_all()
    node = fold(events).nodes[5]
    before = store.path.read_bytes()
    spec, rebased, receipt = materialization_plan(lane.task.repo_spec(), node, events)
    assert receipt["status"] == "rebased"
    assert spec["effective_seed_base"] == second[0]["selector"]
    assert "LEARNING_RATE" in rebased.files["train.py"]
    assert "range(20)" in rebased.files["train.py"]
    assert store.path.read_bytes() == before
    work, actual = materialize(lane, store, 5)
    assert actual["digest"] == second[0]["selector"]["digest"]
    assert (work / "train.py").read_bytes() == rebased.files["train.py"].encode()
    # A real experiment then uses the rebased authored bytes, with no ignored
    # response resurfacing in its training or terminal score.
    result = run_command_eval([sys.executable, "score.py"], str(work), 10,
        lane.task.eval_spec()["metric"], stages=lane.task.eval_spec()["stages"])
    assert result.exit_code == 0 and result.metric is not None
    assert b"ignored" not in (work / "train.py").read_bytes()


@pytest.mark.parametrize("generation", [None, 1])
def test_accepted_in_place_recreation_retains_its_current_base(tmp_path, generation):
    lane, store, _, _, _, second = twice(tmp_path)
    store.append("node_reset", {"node_id": 5})
    authored = dict(fold(store.read_all()).nodes[5].files)
    body = {"node_id": 5, "operator": "draft", "parent_ids": [],
        "idea": {"operator": "draft", "title": "Compare earlier implementation"}, "files": authored}
    if generation is not None:
        body["generation"] = generation
    accepted = store.append("node_created", body)
    node = fold(store.read_all()).nodes[5]
    assert node.attempt == 1
    assert node.creation_event_seq == accepted.seq
    spec, rebased, receipt = materialization_plan(lane.task.repo_spec(), node, store.read_all())
    assert spec["effective_seed_base"] == second[0]["selector"]
    assert receipt["status"] == "unchanged" and rebased.files == authored
    work, _ = materialize(lane, store, 5)
    assert (work / "train.py").read_bytes() == authored["train.py"].encode()
