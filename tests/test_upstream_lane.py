"""Actual CPU SGD through the complete measured upstream transaction, not mock metrics."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
from benchmarks._upstream_sgd import TRAIN, SOURCE, GENERAL, SCORE

from looplab.adapters.repo_task import RepoTask, EvalSpec
from looplab.core.config import Settings
from looplab.core.errors import UpstreamRefusal
from looplab.core.models import Idea
from looplab.engine.seed_archive import capture_seed_archive
from looplab.engine.upstream import UpstreamLane
from looplab.engine.upstream_state import active_base, events_for, source_node
from looplab.engine.upstream_workspace import materialization_plan, write_overlay
from looplab.engine.workspace_seed import seed_candidate_workspace
from looplab.engine.workspace import WorkspaceSeeder
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.events.run_generation import run_generation_token
from looplab.runtime.command_eval import run_command_eval







def fixture(tmp_path, *, base_train=TRAIN, source_files=None, upstream_policy=None, repair_from=None, base_files=None, eval_env=None):
    src, origin, rd = (tmp_path / name for name in ("owner", "origin", "run"))
    for p in (src, origin, rd):
        p.mkdir()
    for name, text in {"train.py": base_train, "score.py": SCORE, "recipe.env": "MOMENTUM=0.0\n", "README.md": "Runner\n", **(base_files or {})}.items():
        (src / name).write_text(text, encoding="utf8")
    base = capture_seed_archive(src, origin / "base_snapshots")
    origin_store = EventStore(origin / "events.jsonl")
    origin_store.append("run_started", {"run_id": "origin", "task_id": "repo", "goal": "SGD", "direction": "min"})
    seed = origin_store.append("workspace_seeded", {"node_id": None, "materialized": [], "base_revision": base})
    selector = {"run_dir": str(origin), "event_seq": seed.seq, "digest": base["digest"]}
    task = RepoTask(goal="SGD", direction="min", editable_path=str(src), seed_base=selector,
        edit_surface=["train.py", "recipe.env", "README.md"],
        eval=EvalSpec(command=[sys.executable, "score.py"], timeout=10,
            env=eval_env or {},
            stages=[{"name": "train", "command": [sys.executable, "train.py"], "timeout": 10},
                    {"name": "score", "command": [sys.executable, "score.py"], "timeout": 10}],
            metric={"kind": "stdout_json", "key": "metric"}, scorer_boundary={"files": ["score.py"]}),
        upstream=upstream_policy or {"repeats": 2, "tests": [{"name": "syntax", "command": [sys.executable, "-m", "py_compile", "train.py"]}],
                  "regressions": [{"name": "old_recipe", "command": [sys.executable, "train.py"], "artifacts": ["predictions.json"]}]})
    settings = Settings(backend="toy", external_harness=True)
    (rd / "task.snapshot.json").write_text(task.model_dump_json(), encoding="utf8")
    (rd / "config.snapshot.json").write_text(settings.model_dump_json(), encoding="utf8")
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": "run", "task_id": "repo", "goal": "SGD", "direction": "min", "seed_base": selector, "upstream": task.upstream})
    files = source_files if source_files is not None else {"train.py": SOURCE, "recipe.env": "MOMENTUM=0.2\n"}
    store.append("node_created", {"node_id": 0, "operator": "draft", "parent_ids": [], "idea": Idea(operator="draft", title="Momentum SGD", hypothesis="Momentum converges faster").model_dump(), "files": repair_from if repair_from is not None else files})
    work = rd / "nodes" / "node_0"
    rows = seed_candidate_workspace(task.repo_spec(), work, capture_base_revision=True, base_archive_dir=rd / "base_snapshots")
    receipt = rows[0]["base_revision"]
    seed = store.append("workspace_seeded", {"node_id": 0, "materialized": rows, "base_revision": receipt})
    receipt["seed_event_seq"] = seed.seq
    receipt["node_id"], receipt["generation"] = 0, 0
    if repair_from is not None:
        write_overlay(work, repair_from)
        failed = run_command_eval([sys.executable, "score.py"], str(work), 10, task.eval_spec()["metric"], stages=task.eval_spec()["stages"], env=eval_env)
        assert failed.exit_code != 0 and failed.metric is None
        store.append("node_repaired", {"node_id": 0, "generation": 0, "attempt": 1,
            "files": files, "deleted": [], "changed": True, "error_in": "train", "triage_action": "repair",
            "stages_passed": [], "rationale": "Negative momentum unsupported by the original runner", "eval_seconds": 0.1})
    write_overlay(work, files)
    result = run_command_eval([sys.executable, "score.py"], str(work), 10, task.eval_spec()["metric"], stages=task.eval_spec()["stages"], env=eval_env)
    assert result.exit_code == 0 and result.metric is not None
    store.append("node_evaluated", {"node_id": 0, "metric": result.metric, "task_metric": result.metric,
        "eval_seconds": 0.1, "violations": [], "metric_provenance": {"base_revision": receipt}})
    store.append("pause", {})
    lane = UpstreamLane(rd, task, settings)
    generation = run_generation_token(store.read_all())
    status = lane.read(generation)
    hunks = [r["hunk_hash"] for r in status["candidates"]["rows"] if r["path"] == "train.py"]
    proposal = {"expected_generation": generation, "action_id": "proposal-1", "source_node_id": 0,
        "expected_base_revision": status["active_base"]["revision"], "hunk_hashes": hunks,
        "files": {"train.py": GENERAL, "README.md": "MOMENTUM: default 0.0; set MOMENTUM=0.2 in recipe.env.\n"},
        "deleted": [], "recipe_files": {"recipe.env": "MOMENTUM=0.2\n"}, "recipe_deleted": [],
        "flag": {"name": "MOMENTUM", "default": "0.0", "enabled": "0.2"}, "documentation_path": "README.md",
        "summary": "Generalize momentum through recipe.env, retaining the original no-momentum default",
        "critic": {"verdict": "pass", "reason": "No node-specific paths, old default preserved, shared runner", "reviewer": "independent critic"}}
    return lane, store, generation, proposal


def test_real_sgd_gate_advance_overlay_rebase_and_exact_retries(tmp_path, monkeypatch):
    # The entire transaction must observe the engine's single fold seam, including
    # protected gate execution and workspace rebasing. A direct replay import bypasses it.
    from looplab.engine import orchestrator
    real_fold, callers = orchestrator.fold, set()

    def observed_fold(events):
        frame = sys._getframe(1)
        if frame.f_code.co_name == "engine_fold":
            callers.add(frame.f_back.f_globals["__name__"])
        return real_fold(events)

    monkeypatch.setattr(orchestrator, "fold", observed_fold)
    lane, store, generation, body = fixture(tmp_path)
    original_score = fold(store.read_all()).nodes[0].metric
    proposed = lane.propose(body)
    assert proposed["status"] == "succeeded" and lane.propose(body) == proposed
    check = {"expected_generation": generation, "action_id": "check-1", "proposal_id": proposed["proposal_id"]}
    checked = lane.check(check)
    assert checked["status"] == "succeeded", checked
    assert checked["result"]["checks"][-1]["delta"] == 0
    assert len(checked["result"]["executions"]) == 7  # test + two old-recipe checks + 2x2 paired full train+score
    before = len(store.read_all())
    assert lane.check(check) == checked and len(store.read_all()) == before
    for nid, files in ((2, {"train.py": SOURCE, "recipe.env": "MOMENTUM=0.3\n"}),
                       (3, {"train.py": SOURCE.replace("range(30)", "range(20)")}),
                       (4, {})):
        store.append("node_created", {"node_id": nid, "operator": "improve", "parent_ids": [0], "idea": Idea(operator="improve").model_dump(), "files": files})
    advance = {"expected_generation": generation, "action_id": "advance-1", "proposal_id": proposed["proposal_id"],
        "expected_base_revision": body["expected_base_revision"], "evidence_token": checked["evidence_token"]}
    advanced = lane.advance(advance)
    assert lane.advance(advance) == advanced
    state = fold(store.read_all())
    assert state.nodes[0].metric == original_score
    assert state.eval_seconds_by_kind["upstream"] > 0 and state.upstream_base["selector"] == proposed["selector"]
    # A new lifecycle uses the capability once and keeps its scientific recipe.
    store.append("node_created", {"node_id": 1, "operator": "improve", "parent_ids": [0], "idea": Idea(operator="improve", title="Use shared momentum").model_dump(), "files": {"recipe.env": "MOMENTUM=0.2\n"}})
    node = fold(store.read_all()).nodes[1]
    spec, rebased, receipt = materialization_plan(lane.task.repo_spec(), node, events_for(lane.rd))
    assert receipt["status"] == "unchanged" and spec["effective_seed_base"] == proposed["selector"]
    work = lane.rd / "nodes" / "node_1"
    seed_candidate_workspace(spec, work)
    write_overlay(work, rebased.files, rebased.deleted)
    result = run_command_eval([sys.executable, "score.py"], str(work), 10, lane.task.eval_spec()["metric"], stages=lane.task.eval_spec()["stages"])
    assert result.metric == original_score
    assert (tmp_path / "owner" / "train.py").read_text() == TRAIN
    # Exact inherited implementations disappear, a novel conflicting edit keeps
    # the whole old base, and an empty recipe retains the original default.
    nodes = fold(store.read_all()).nodes
    inherited_spec, inherited, migrated = materialization_plan(lane.task.repo_spec(), nodes[2], events_for(lane.rd))
    assert inherited.files == {"recipe.env": "MOMENTUM=0.3\n"} and migrated["absorbed_paths"] == ["train.py"]
    conflict_spec, conflict, reason = materialization_plan(lane.task.repo_spec(), nodes[3], events_for(lane.rd))
    assert reason["status"] == "conflict" and conflict.files == nodes[3].files
    assert conflict_spec["effective_seed_base"] == lane.task.seed_base
    _, empty, _ = materialization_plan(lane.task.repo_spec(), nodes[4], events_for(lane.rd))
    assert empty.files == {}, "Source recipe must never silently become the new default"
    engine = SimpleNamespace(run_dir=lane.rd, tracer=None, _repo_spec=lane.task.repo_spec(), _seed_mode="auto", _assets={}, store=store)
    seeder = WorkspaceSeeder(engine)
    for name in ("seed_workspace", "seed_repo_tree", "link_input", "write_node_files", "write_assets"):
        setattr(engine, "_" + name, getattr(seeder, name))
    actual = seeder.materialize(nodes[2], lane.rd / "nodes" / "node_2")
    assert actual["digest"] == proposed["selector"]["digest"]
    assert fold(store.read_all()).nodes[2].files == {"recipe.env": "MOMENTUM=0.3\n"}
    # Re-materializing the same attempt does not resurrect the removed runner.
    again = seeder.materialize(fold(store.read_all()).nodes[2], lane.rd / "nodes" / "node_2")
    assert again["digest"] == actual["digest"]
    assert {"looplab.engine.upstream", "looplab.engine.upstream_gate",
            "looplab.engine.upstream_state", "looplab.engine.upstream_workspace"} <= callers


@pytest.mark.parametrize("fault", ["scorer", "recipe", "nomination", "critic"])
def test_invalid_proposal_refuses_before_work_and_claim(tmp_path, fault):
    lane, store, _, body = fixture(tmp_path)
    if fault == "scorer": body["files"]["score.py"] = "print(100)"
    if fault == "recipe": body["recipe_files"]["recipe.env"] = "MOMENTUM=0.9\n"
    if fault == "nomination": body["hunk_hashes"] = ["0" * 64]
    if fault == "critic": body["critic"]["verdict"] = "fail"
    before = store.path.read_bytes()
    with pytest.raises(UpstreamRefusal): lane.propose(body)
    assert store.path.read_bytes() == before and not (lane.rd / "upstream").exists()


def test_changed_body_and_stale_source_cannot_recover_or_advance(tmp_path):
    lane, store, generation, body = fixture(tmp_path)
    proposed = lane.propose(body)
    with pytest.raises(UpstreamRefusal, match="different exact body"):
        lane.propose({**body, "summary": "changed"})
    checked = lane.check({"expected_generation": generation, "action_id": "check", "proposal_id": proposed["proposal_id"]})
    assert checked["status"] == "succeeded"
    store.append("node_reset", {"node_id": 0})
    with pytest.raises(UpstreamRefusal):
        lane.advance({"expected_generation": generation, "action_id": "advance", "proposal_id": proposed["proposal_id"],
            "expected_base_revision": body["expected_base_revision"], "evidence_token": checked["evidence_token"]})
    assert not any(e.type == "base_advanced" for e in store.read_all())
