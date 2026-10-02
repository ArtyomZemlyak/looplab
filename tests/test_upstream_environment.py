"""Bind the declared environment that executes the real old/new SGD pipelines."""
import sys

import pytest

from looplab.core.errors import UpstreamRefusal
from tests.test_upstream_lane import fixture, TRAIN, SOURCE, GENERAL


@pytest.mark.parametrize("layer", ["task", "settings", "stage"])
def test_distribution_change_on_declared_pythonpath_invalidates_cas(tmp_path, layer):
    library = tmp_path / "dependency"
    library.mkdir()
    (library / "gate_dependency.py").write_text("MARKER = 'actual imported dependency'\n", encoding="utf8")
    metadata = library / "gate_dependency-1.0.dist-info"
    metadata.mkdir()
    manifest = metadata / "METADATA"
    manifest.write_text("Metadata-Version: 2.1\nName: gate-dependency\nVersion: 1.0\n", encoding="utf8")
    env = {"PYTHONPATH": str(library)}
    prefix = "from gate_dependency import MARKER\nassert MARKER == 'actual imported dependency'\n"
    lane, store, generation, proposal = fixture(tmp_path, base_train=prefix + TRAIN,
        source_files={"train.py": prefix + SOURCE, "recipe.env": "MOMENTUM=0.2\n"}, eval_env=env)
    proposal["files"]["train.py"] = prefix + GENERAL
    if layer != "task":
        lane.task.eval.env = {}
        if layer == "settings":
            lane.settings.eval_env = env
        else:
            for stage in lane.task.eval.stages:
                stage["env"] = env
            # The operator's standalone regression also imports the dependency.
            # A stage-specific override differs from the run-level environment.
            lane.settings.eval_env = {"PYTHONPATH": str(tmp_path)}
            lane.task.upstream["regressions"][0]["command"] = [sys.executable, "-c",
                f"import sys,runpy;sys.path.insert(0,{str(library)!r});runpy.run_path('train.py')"]
            events = store.read_all()
            events[0].data["upstream"] = lane.task.upstream
            store.path.write_text("".join(e.model_dump_json() + "\n" for e in events), encoding="utf8")
    (lane.rd / "task.snapshot.json").write_text(lane.task.model_dump_json(), encoding="utf8")
    (lane.rd / "config.snapshot.json").write_text(lane.settings.model_dump_json(), encoding="utf8")
    made = lane.propose(proposal)
    request = {"expected_generation": generation, "action_id": "environment", "proposal_id": made["proposal_id"]}
    checked = lane.check(request)
    assert checked["status"] == "succeeded", checked
    manifest.write_text("Metadata-Version: 2.1\nName: gate-dependency\nVersion: 2.0\n", encoding="utf8")
    before = len(store.read_all())
    assert lane.check(request) == checked and len(store.read_all()) == before
    with pytest.raises(UpstreamRefusal) as refusal:
        lane.advance({"expected_generation": generation, "action_id": "changed-env", "proposal_id": made["proposal_id"],
            "expected_base_revision": proposal["expected_base_revision"], "evidence_token": checked["evidence_token"]})
    assert refusal.value.code == "upstream_evidence_changed"
    assert not any(e.type == "base_advanced" for e in store.read_all())
    fresh = lane.check({**request, "action_id": "new-environment"})
    assert fresh["status"] == "succeeded", fresh
