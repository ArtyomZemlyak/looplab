"""The base receipt describes the copied seed, not the source at terminal time."""
import subprocess
from types import SimpleNamespace

import pytest

from looplab.engine.workspace import WorkspaceSeeder
from looplab.engine import workspace_seed
from looplab.events.eventstore import EventStore


def _seeder(tmp_path, *, git=False, mode="auto"):
    src = tmp_path / "source"
    src.mkdir()
    (src / "experiment.env").write_text("VALUE=old\n", encoding="utf8")
    (src / "score.py").write_text("print(1)\n", encoding="utf8")
    if git:
        for args in (["init", "-q"], ["add", "."],
                     ["-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "base"]):
            subprocess.run(["git", "-C", str(src), *args], check=True, capture_output=True)
    run = tmp_path / "run"
    run.mkdir()
    engine = SimpleNamespace(run_dir=run, tracer=None, _seed_mode=mode,
        _repo_spec={"editables": [{"name": ".", "path": str(src), "protect": ["score.py"]}],
                    "protected_names": ["score.py"]}, _assets={}, store=EventStore(run / "events.jsonl"))
    seeder = WorkspaceSeeder(engine)
    for name in ("seed_workspace", "seed_repo_tree", "link_input", "write_node_files", "write_assets"):
        setattr(engine, "_" + name, getattr(seeder, name))
    return src, run, engine, seeder


def _node(files=None):
    return SimpleNamespace(id=0, attempt=0, files=files or {}, deleted=[])


@pytest.mark.parametrize("directory", ["nodes/node_19", "confirm/node_19_g2_seed_7", "noise/node_19_g2_seed_8"])
def test_seed_attribution_uses_the_actual_lifecycle_not_the_path_suffix(tmp_path, directory):
    _, run, engine, seeder = _seeder(tmp_path)
    node = _node()
    node.id, node.attempt = 19, 2
    seeder.materialize(node, run / directory)
    seed = engine.store.read_all()[-1]
    assert seed.data["node_id"] == 19 and seed.data["generation"] == 2


@pytest.mark.parametrize("git,mode", [(True, "auto"), (True, "tracked"), (False, "auto"), (False, "all")])
def test_receipt_precedes_overlay_and_survives_source_drift(tmp_path, git, mode):
    src, run, engine, seeder = _seeder(tmp_path, git=git, mode=mode)
    wd = run / "nodes" / "node_0"
    receipt = seeder.materialize(_node({"experiment.env": "VALUE=recipe\n"}), wd)
    assert isinstance(receipt, dict), "materialize must retain the seed's receipt"
    assert receipt["complete"] and len(receipt["digest"]) == 64
    seed = engine.store.read_all()[-1]
    assert seed.data["base_revision"]["digest"] == receipt["digest"]
    assert receipt["seed_event_seq"] == seed.seq
    assert (wd / "experiment.env").read_text() == "VALUE=recipe\n"
    (src / "experiment.env").write_text("VALUE=operator-new\n")
    old_digest = receipt["digest"]
    seeder.write_node_files(_node({"experiment.env": "VALUE=repair\n"}), wd)
    assert receipt["digest"] == old_digest
    assert len(engine.store.read_all()) == 1, "repair does not seed again"
    fresh = seeder.materialize(_node(), wd)
    assert fresh["digest"] != old_digest and fresh["seed_event_seq"] > receipt["seed_event_seq"]


def test_dirty_deleted_and_untracked_protected_files_belong_to_the_effective_seed(tmp_path):
    src, run, engine, seeder = _seeder(tmp_path, git=True)
    (src / "experiment.env").unlink()
    (src / "score.py").write_text("print(2)\n")
    (src / "grader.secret").write_text("operator-only\n")
    engine._repo_spec["editables"][0]["protect"].append("grader.secret")
    receipt = seeder.materialize(_node(), run / "nodes" / "node_0")
    assert receipt["complete"] and receipt["file_count"] == 2
    assert "operator-only" not in str(engine.store.read_all())


def test_mounts_and_task_assets_do_not_enter_the_base_digest(tmp_path):
    src, run, engine, seeder = _seeder(tmp_path)
    first = seeder.materialize(_node(), run / "nodes" / "node_0")
    data = tmp_path / "data"
    data.mkdir()
    (data / "huge.bin").write_bytes(b"x" * 10000)
    engine._repo_spec["data"] = {"data": {"path": str(data), "mount": False}}
    engine._assets = {"asset.txt": "task asset"}
    second = seeder.materialize(_node(), run / "nodes" / "node_0")
    assert first["digest"] == second["digest"] and first["bytes"] == second["bytes"]
    assert (run / "nodes" / "node_0" / "data" / "huge.bin").exists()


@pytest.mark.parametrize("limit,reason", [("MAX_BASE_REVISION_BYTES", "too_many_bytes"),
                                        ("MAX_BASE_REVISION_ENTRIES", "too_many_entries")])
def test_over_budget_keeps_evaluation_possible_without_a_partial_digest(tmp_path, monkeypatch, limit, reason):
    _, run, engine, seeder = _seeder(tmp_path)
    monkeypatch.setattr(workspace_seed, limit, 1, raising=False)
    receipt = seeder.materialize(_node(), run / "nodes" / "node_0")
    assert receipt["complete"] is False and receipt["digest"] is None
    assert receipt["reason"] == reason
    assert (run / "nodes" / "node_0" / "score.py").exists()


def test_two_editables_are_both_included(tmp_path):
    _, run, engine, seeder = _seeder(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    (other / "runner.env").write_text("X=1")
    engine._repo_spec["editables"].append({"name": "other", "path": str(other)})
    first = seeder.materialize(_node(), run / "nodes" / "node_0")
    (other / "runner.env").write_text("X=2")
    second = seeder.materialize(_node(), run / "nodes" / "node_0")
    assert first["digest"] != second["digest"]


def test_entry_budget_stops_directory_enumeration(tmp_path, monkeypatch):
    from contextlib import contextmanager
    src = tmp_path / "seed"
    src.mkdir()
    for name in ("a", "b", "c"):
        (src / name).write_bytes(b"x")
    real = workspace_seed.os.scandir
    visited = []
    @contextmanager
    def scan(path):
        with real(path) as entries:
            def bounded():
                for entry in entries:
                    visited.append(entry.name)
                    assert len(visited) <= 2, "entry cap must stop enumeration, not just hashing"
                    yield entry
            yield bounded()
    monkeypatch.setattr(workspace_seed.os, "scandir", scan)
    monkeypatch.setattr(workspace_seed, "MAX_BASE_REVISION_ENTRIES", 2)
    receipt = workspace_seed.seeded_base_revision(src)
    assert receipt["digest"] is None and receipt["reason"] == "too_many_entries"
    assert len(visited) == 2


@pytest.mark.parametrize("fault,reason", [("unreadable", "unreadable_seed"), ("unstable", "unstable_seed")])
def test_a_failed_read_cannot_publish_a_partial_identity(tmp_path, monkeypatch, fault, reason):
    from looplab.core import node_evidence
    src, _, _, _ = _seeder(tmp_path)
    real = node_evidence.read_bounded_regular_file
    def read(path, limit):
        data = real(path, limit)
        if fault == "unreadable":
            return None
        path.write_bytes(b"new bytes")
        return data
    monkeypatch.setattr(node_evidence, "read_bounded_regular_file", read)
    receipt = workspace_seed.seeded_base_revision(src)
    assert receipt["complete"] is False and receipt["digest"] is None and receipt["reason"] == reason


def test_middle_bytes_of_a_large_file_are_not_sampled(tmp_path):
    src = tmp_path / "seed"
    src.mkdir()
    file = src / "weights.binary"
    file.write_bytes(b"a" * (2 * 1024 * 1024))
    first = workspace_seed.seeded_base_revision(src)
    with file.open("r+b") as fh:
        fh.seek(1024 * 1024)
        fh.write(b"b")
    second = workspace_seed.seeded_base_revision(src)
    assert first["complete"] and second["complete"] and first["digest"] != second["digest"]


def test_unsupported_link_is_not_followed(tmp_path):
    src = tmp_path / "seed"
    src.mkdir()
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside seed")
    try:
        (src / "link").symlink_to(outside)
    except OSError as exc:
        if getattr(exc, "winerror", None) != 1314:
            raise
        pytest.skip("Windows symlink privilege unavailable")
    receipt = workspace_seed.seeded_base_revision(src)
    assert receipt["complete"] is False and receipt["digest"] is None
    assert receipt["reason"] == "unsupported_seed_entry"
