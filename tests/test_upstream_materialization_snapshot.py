"""A stale worker must not replace workspaces of the replay-current experiment."""
from types import SimpleNamespace

import pytest

from looplab.core.errors import UpstreamRefusal
from looplab.engine.workspace import WorkspaceSeeder
from looplab.events.replay import fold
from tests.test_upstream_lane import fixture
from tests.test_upstream_multibase import create, evaluate


@pytest.mark.parametrize("change", ["reset", "terminal", "repair", "recreation", "same_manifest", "tombstone", "abort"])
def test_stale_materialization_refuses_before_cleanup_or_event_write(tmp_path, change):
    lane, store, _, _ = fixture(tmp_path)
    create(store, 2, dict(fold(store.read_all()).nodes[0].files))
    stale = fold(store.read_all()).nodes[2]
    if change == "reset":
        store.append("node_reset", {"node_id": 2})
    elif change == "terminal":
        evaluate(lane, store, 2)
    elif change == "repair":
        store.append("node_repaired", {"node_id": 2, "generation": 0,
            "files": {"recipe.env": "MOMENTUM=0.9\n"}, "changed": True,
            "rationale": "New authored experiment replaces the stale worker's manifest"})
    elif change == "same_manifest":
        store.append("node_created", {"node_id": 2, "generation": 0, "operator": stale.operator, "parent_ids": [],
            "idea": stale.idea.model_dump(), "files": stale.files})
    elif change == "tombstone":
        store.append("node_tombstoned", {"node_ids": [2]})
    elif change == "abort":
        store.append("node_abort", {"node_id": 2})
    else:
        store.append("node_created", {"node_id": 2, "generation": 0, "operator": "draft", "parent_ids": [],
            "idea": {"operator": "draft"}, "files": {"recipe.env": "MOMENTUM=0.9\n"}})
    state = fold(store.read_all())
    if change == "tombstone":
        assert state.nodes[2].tombstoned
    if change == "abort":
        assert 2 in state.aborted_nodes
    work = lane.rd / "nodes" / "node_2"
    work.mkdir(parents=True, exist_ok=True)
    (work / "retained-evidence.txt").write_bytes(b"Do not delete another worker's evidence")
    before_files = {str(p.relative_to(work)): p.read_bytes() for p in work.rglob("*") if p.is_file()}
    before_events = store.path.read_bytes()
    engine = SimpleNamespace(run_dir=lane.rd, tracer=None, _repo_spec=lane.task.repo_spec(),
        _seed_mode="auto", _assets={}, store=store)
    seeder = WorkspaceSeeder(engine)
    for method in ("seed_workspace", "seed_repo_tree", "link_input", "write_node_files", "write_assets"):
        setattr(engine, "_" + method, getattr(seeder, method))
    with pytest.raises(UpstreamRefusal) as refused:
        seeder.materialize(stale, work)
    assert refused.value.code == "upstream_source_changed"
    assert {str(p.relative_to(work)): p.read_bytes() for p in work.rglob("*") if p.is_file()} == before_files
    assert store.path.read_bytes() == before_events
