"""Two measured SGD capabilities, real Git bases and successive node migrations."""
import sys
import time
from types import SimpleNamespace

import pytest

from benchmarks._upstream_sgd import GENERAL, SOURCE
from looplab.core.atomicio import atomic_write_bytes
from looplab.core.errors import ConfigRefusal, UpstreamRefusal
from looplab.core.models import Idea
from looplab.engine.seed_base import selected_seed_base
from looplab.engine.upstream_workspace import materialization_plan
from looplab.engine.workspace import WorkspaceSeeder
from looplab.events.replay import fold
from looplab.runtime.command_eval import run_command_eval
from tests.test_upstream_lane import fixture


RATE_SOURCE = GENERAL.replace("w -= 0.2*velocity", "w -= 0.3*velocity")
RATE_GENERAL = GENERAL.replace("w -= 0.2*velocity", 'w -= float(settings.get("LEARNING_RATE", "0.2"))*velocity')
RATE_RECIPE = "MOMENTUM=0.2\nLEARNING_RATE=0.3\n"


def create(store, nid, files, *, deleted=()):
    store.append("node_created", {"node_id": nid, "operator": "improve", "parent_ids": [0],
        "idea": Idea(operator="improve", title="SGD variant").model_dump(),
        "files": files, "deleted": list(deleted)})


def materialize(lane, store, nid, *, directory="nodes", name=None):
    engine = SimpleNamespace(run_dir=lane.rd, tracer=None, _repo_spec=lane.task.repo_spec(),
        _seed_mode="auto", _assets={}, store=store)
    seeder = WorkspaceSeeder(engine)
    for method in ("seed_workspace", "seed_repo_tree", "link_input", "write_node_files", "write_assets"):
        setattr(engine, "_" + method, getattr(seeder, method))
    node = fold(store.read_all()).nodes[nid]
    work = lane.rd / directory / (name or ("node_" + str(nid)))
    receipt = seeder.materialize(node, work)
    return work, receipt


def evaluate(lane, store, nid):
    work, receipt = materialize(lane, store, nid)
    node = fold(store.read_all()).nodes[nid]
    receipt["node_id"], receipt["generation"] = nid, node.attempt
    start = time.perf_counter()
    result = run_command_eval([sys.executable, "score.py"], str(work), 10,
        lane.task.eval_spec()["metric"], stages=lane.task.eval_spec()["stages"])
    assert result.exit_code == 0 and result.metric is not None
    store.append("node_evaluated", {"node_id": nid, "generation": node.attempt,
        "metric": result.metric, "task_metric": result.metric, "violations": [],
        "eval_seconds": time.perf_counter() - start, "metric_provenance": {"base_revision": receipt}})
    return result.metric


def promote(lane, generation, body, key):
    made = lane.propose(body)
    check = {"expected_generation": generation, "action_id": key + ":gate", "proposal_id": made["proposal_id"]}
    checked = lane.check(check)
    assert checked["status"] == "succeeded", checked
    assert len(checked["result"]["executions"]) == 7
    advance = {"expected_generation": generation, "action_id": key + ":advance",
        "proposal_id": made["proposal_id"], "expected_base_revision": body["expected_base_revision"],
        "evidence_token": checked["evidence_token"]}
    advanced = lane.advance(advance)
    assert advanced["status"] == "succeeded"
    return made, check, checked, advance, advanced


def twice(tmp_path, *, before_second=None):
    lane, store, generation, first_body = fixture(tmp_path)
    create(store, 2, {"train.py": SOURCE, "recipe.env": "MOMENTUM=0.3\n"})
    create(store, 3, {"train.py": SOURCE.replace("range(30)", "range(20)")})
    create(store, 4, {})
    first = promote(lane, generation, first_body, "first")
    materialize(lane, store, 2)  # effective overlay is now based on base one
    create(store, 5, {"train.py": GENERAL.replace("range(30)", "range(20)"), "recipe.env": "MOMENTUM=0.4\n"})
    materialize(lane, store, 5)
    create(store, 1, {"train.py": RATE_SOURCE, "recipe.env": RATE_RECIPE})
    evaluate(lane, store, 1)
    status = lane.read(generation)
    second_body = {**first_body, "action_id": "proposal-2", "source_node_id": 1,
        "expected_base_revision": status["active_base"]["revision"],
        "hunk_hashes": [r["hunk_hash"] for r in status["candidates"]["rows"] if r["node_id"] == 1 and r["path"] == "train.py"],
        "files": {"train.py": RATE_GENERAL, "README.md": "MOMENTUM default 0.0; LEARNING_RATE default 0.2. Set both in recipe.env.\n"},
        "recipe_files": {"recipe.env": RATE_RECIPE},
        "flag": {"name": "LEARNING_RATE", "default": "0.2", "enabled": "0.3"},
        "summary": "Shared learning rate flag with the existing momentum capability and original rate default"}
    create(store, 6, {"train.py": RATE_SOURCE, "recipe.env": "MOMENTUM=0.4\nLEARNING_RATE=0.3\n"})
    if before_second is not None:
        before_second(lane, store)
    second = promote(lane, generation, second_body, "second")
    return lane, store, generation, first_body, first, second


def test_two_real_advancements_preserve_all_recipe_bases_and_retries(tmp_path):
    lane, store, _, first_body, first, second = twice(tmp_path)
    events = store.read_all()
    before = store.path.read_bytes()
    assert lane.propose(first_body) == first[0]
    assert lane.check(first[1]) == first[2]
    assert lane.advance(first[3]) == first[4]
    assert store.path.read_bytes() == before
    assert len([e for e in events if e.type == "base_advanced"]) == 2
    assert len([e for e in events if e.type == "upstream_execution"]) == 14
    state = fold(events)
    source_scores = {nid: state.nodes[nid].metric for nid in (0, 1)}
    for nid, recipe in ((2, "MOMENTUM=0.3\n"), (6, "MOMENTUM=0.4\nLEARNING_RATE=0.3\n")):
        work, receipt = materialize(lane, store, nid)
        assert work == lane.rd / "nodes" / f"node_{nid}"
        assert receipt["digest"] == second[0]["selector"]["digest"]
        assert (work / "train.py").read_bytes() == RATE_GENERAL.encode()
        assert (work / "recipe.env").read_bytes() == recipe.encode()
        assert fold(store.read_all()).nodes[nid].files == {"recipe.env": recipe}
        _, again = materialize(lane, store, nid)
        assert again["digest"] == receipt["digest"]
    # A conflict from the original base cannot become a mixture of three bases.
    spec, conflicted, outcome = materialization_plan(lane.task.repo_spec(), fold(store.read_all()).nodes[3], store.read_all())
    assert outcome["status"] == "conflict" and spec["effective_seed_base"] == lane.task.seed_base
    assert conflicted.files["train.py"] == SOURCE.replace("range(30)", "range(20)")
    empty_work, _ = materialize(lane, store, 4)
    assert (empty_work / "recipe.env").read_bytes() == (tmp_path / "owner" / "recipe.env").read_bytes()
    # Independent scientific edits merge onto the second base, without inheriting its source recipe.
    work, receipt = materialize(lane, store, 5)
    assert receipt["digest"] == second[0]["selector"]["digest"]
    assert (work / "train.py").read_bytes() == RATE_GENERAL.replace("range(30)", "range(20)").encode()
    assert (work / "recipe.env").read_bytes() == b"MOMENTUM=0.4\n"
    store.append("node_reset", {"node_id": 2})
    work, receipt = materialize(lane, store, 2)
    assert fold(store.read_all()).nodes[2].attempt == 1
    assert receipt["digest"] == second[0]["selector"]["digest"]
    assert (work / "recipe.env").read_bytes() == b"MOMENTUM=0.3\n"
    assert {nid: fold(store.read_all()).nodes[nid].metric for nid in (0, 1)} == source_scores


@pytest.mark.parametrize("nid", [0, 1])
def test_confirmation_keeps_the_terminal_implementation_and_measured_base(tmp_path, nid):
    lane, store, _, _, _, _ = twice(tmp_path)
    terminal = fold(store.read_all()).nodes[nid]
    original = terminal.model_dump()
    primary = terminal.metric_provenance["base_revision"]
    work, receipt = materialize(lane, store, nid, directory="confirm", name=f"node_{nid}_g0_seed_7")
    assert work == lane.rd / "confirm" / f"node_{nid}_g0_seed_7"
    seeded = store.read_all()[-1]
    assert seeded.type == "workspace_seeded" and seeded.data["node_id"] == nid and seeded.data["generation"] == 0
    assert receipt["digest"] == primary["digest"], "Confirmation must measure the same implementation, not the latest shared runner"
    assert (work / "train.py").read_bytes() == original["files"]["train.py"].encode()
    assert (work / "recipe.env").read_bytes() == original["files"]["recipe.env"].encode()
    assert fold(store.read_all()).nodes[nid].model_dump() == original
    assert not any(e.type == "node_overlay_rebased" and e.data["node_id"] == nid for e in store.read_all())
    # This is a new real full evaluation, on the original substrate and scientific overlay.
    result = run_command_eval([sys.executable, "score.py"], str(work), 10,
        lane.task.eval_spec()["metric"], stages=lane.task.eval_spec()["stages"])
    assert result.exit_code == 0 and result.metric == original["metric"]


def test_missing_terminal_base_refuses_before_confirmation_workspace_cleanup(tmp_path):
    lane, store, _, _, _, _ = twice(tmp_path)
    primary = fold(store.read_all()).nodes[1].metric_provenance["base_revision"]
    archive, _ = selected_seed_base(primary["selection"])
    # The current base and initial origin remain healthy. Only the older measured
    # intermediate base is damaged, after both measured advancement transactions.
    atomic_write_bytes(archive / "train.py", b"Damaged historical implementation\n")
    work = lane.rd / "confirm" / "node_1"
    work.mkdir(parents=True)
    (work / "sentinel").write_bytes(b"existing confirmation evidence")
    before = store.path.read_bytes()
    with pytest.raises(ConfigRefusal, match="archive"):
        materialize(lane, store, 1, directory="confirm")
    assert (work / "sentinel").read_bytes() == b"existing confirmation evidence"
    assert store.path.read_bytes() == before


def test_late_old_generation_seed_cannot_change_a_reset_scientific_overlay(tmp_path):
    lane, store, _, _, _, second = twice(tmp_path)
    old_seed = next(e for e in store.read_all() if e.type == "workspace_seeded" and e.data.get("node_id") == 5)
    materialize(lane, store, 5)  # legitimately migrate the overlay onto base two
    store.append("node_reset", {"node_id": 5})
    ablation = {"train.py": GENERAL.replace("range(30)", "range(20)"), "recipe.env": "MOMENTUM=0.4\n"}
    store.append("node_repaired", {"node_id": 5, "generation": 1, "files": ablation,
        "deleted": [], "rationale": "Explicitly ablate the newly shared rate flag on the current base"})
    # A generation-zero copy finishes after reset. Its bytes are a real archived
    # seed; this diagnostic must not alter the generation-one overlay's basis.
    store.append("workspace_seeded", old_seed.data)
    work, receipt = materialize(lane, store, 5)
    assert receipt["digest"] == second[0]["selector"]["digest"]
    assert (work / "train.py").read_bytes() == ablation["train.py"].encode()
    assert fold(store.read_all()).nodes[5].files == ablation


@pytest.mark.parametrize("fault", ["missing", "invalid"])
def test_incomplete_terminal_selection_refuses_before_repeated_work(tmp_path, fault):
    lane, store, _, _ = fixture(tmp_path)
    events = store.read_all()
    primary = next(e for e in events if e.type == "node_evaluated").data["metric_provenance"]["base_revision"]
    if fault == "missing":
        primary.pop("selection")
    else:
        primary["selection"]["event_seq"] = False
    store.path.write_text("".join(e.model_dump_json() + "\n" for e in events), encoding="utf8")
    work = lane.rd / "confirm" / "node_0"
    work.mkdir(parents=True)
    (work / "sentinel").write_bytes(b"retained evidence")
    before = store.path.read_bytes()
    with pytest.raises(UpstreamRefusal) as refused:
        materialize(lane, store, 0, directory="confirm")
    assert refused.value.code == "upstream_source_unavailable"
    assert (work / "sentinel").read_bytes() == b"retained evidence"
    assert store.path.read_bytes() == before
