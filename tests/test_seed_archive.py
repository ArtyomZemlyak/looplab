"""Seed bytes survive source/workdir loss; corruption never grants a usable archive."""
from concurrent.futures import ThreadPoolExecutor
import copy
import os
import subprocess
import sys

import pytest

from looplab.core.atomicio import rmtree_readonly_aware
from looplab.engine import seed_archive, workspace_seed
from looplab.engine.seed_archive import capture_seed_archive, verified_seed_archive
from test_seeded_base_revision import _seeder, _node


def test_base_is_usable_after_source_and_node_workdir_are_removed(tmp_path):
    src, run, _, seeder = _seeder(tmp_path, git=True)
    (src / "experiment.env").write_text("VALUE=dirty\n")
    wd = run / "nodes" / "node_0"
    receipt = seeder.materialize(_node({"experiment.env": "VALUE=recipe\n"}), wd)
    rmtree_readonly_aware(src)
    rmtree_readonly_aware(wd)
    archive = verified_seed_archive(run, receipt)
    assert archive is not None and receipt["archive"]["status"] == "stored"
    assert (archive / "experiment.env").read_text() == "VALUE=dirty\n"
    result = subprocess.run([sys.executable, str(archive / "score.py")],
                            capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "1"


def test_identical_seed_is_deduplicated_and_drift_retains_both_bases(tmp_path):
    src, run, _, seeder = _seeder(tmp_path)
    wd = run / "nodes" / "node_0"
    first = seeder.materialize(_node(), wd)
    second = seeder.materialize(_node({"experiment.env": "overlay"}), wd)
    assert first["archive"] == second["archive"]
    (src / "experiment.env").write_text("changed")
    third = seeder.materialize(_node(), wd)
    assert third["archive"]["path"] != first["archive"]["path"]
    assert len(list((run / "base_snapshots").iterdir())) == 2
    assert (verified_seed_archive(run, first) / "experiment.env").read_text() == "VALUE=old\n"
    assert (verified_seed_archive(run, third) / "experiment.env").read_text() == "changed"


def test_concurrent_identical_publications_preserve_one_verified_base(tmp_path):
    src, run, _, _ = _seeder(tmp_path)
    root = run / "base_snapshots"
    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(lambda _: capture_seed_archive(src, root), range(2)))
    assert all(verified_seed_archive(run, receipt) is not None for receipt in receipts)
    assert len(list(root.iterdir())) == 1


def test_nested_regular_files_are_archived_before_overlay(tmp_path):
    src, run, _, seeder = _seeder(tmp_path)
    nested = src / "pipeline" / "prep" / "run.env"
    nested.parent.mkdir(parents=True)
    nested.write_bytes(b"BASE=nested\n")
    receipt = seeder.materialize(_node({"pipeline/prep/run.env": "recipe"}),
                                 run / "nodes" / "node_0")
    archive = verified_seed_archive(run, receipt)
    assert archive is not None
    assert (archive / "pipeline" / "prep" / "run.env").read_bytes() == b"BASE=nested\n"


def test_occupied_archive_root_does_not_change_operator_file(tmp_path):
    _, run, _, seeder = _seeder(tmp_path)
    blocker = run / "base_snapshots"
    blocker.write_text("operator file")
    receipt = seeder.materialize(_node(), run / "nodes" / "node_0")
    assert receipt["complete"] and receipt["archive"]["status"] == "unavailable"
    assert blocker.read_text() == "operator file"


@pytest.mark.skipif(os.name != "nt", reason="native Win32 rename error shape")
@pytest.mark.parametrize("label", ["seed archive root", "seed base archive"])
def test_native_already_exists_error_recovers_a_verified_winner(tmp_path, monkeypatch, label):
    _, run, _, seeder = _seeder(tmp_path)
    real = seed_archive.durable_no_replace_rename
    def rename(source, destination, **kwargs):
        real(source, destination, **kwargs)
        if kwargs["label"] == label:
            raise OSError(183, "Already exists")
    monkeypatch.setattr(seed_archive, "durable_no_replace_rename", rename)
    receipt = seeder.materialize(_node(), run / "nodes" / "node_0")
    assert verified_seed_archive(run, receipt) is not None


def test_unknown_seed_publishes_no_partial_archive(tmp_path, monkeypatch):
    _, run, _, seeder = _seeder(tmp_path)
    monkeypatch.setattr(workspace_seed, "MAX_BASE_REVISION_BYTES", 1)
    receipt = seeder.materialize(_node(), run / "nodes" / "node_0")
    assert not receipt["complete"] and receipt["archive"]["status"] == "unavailable"
    assert receipt["archive"]["reason"] == "seed_identity_unavailable"
    assert not list((run / "base_snapshots").iterdir())


@pytest.mark.parametrize("seam,reason", [("strict_fsync", "archive_storage_unavailable"),
                                      ("durable_no_replace_rename", "archive_publication_unavailable")])
def test_storage_failure_keeps_seed_and_materialization_available(tmp_path, monkeypatch, seam, reason):
    _, run, _, seeder = _seeder(tmp_path)
    (run / "base_snapshots").mkdir()
    def fail(*args, **kwargs):
        raise OSError("storage failure")
    monkeypatch.setattr(seed_archive, seam, fail)
    wd = run / "nodes" / "node_0"
    receipt = seeder.materialize(_node({"experiment.env": "recipe"}), wd)
    assert receipt["complete"] and receipt["digest"]
    assert receipt["archive"]["status"] == "unavailable" and receipt["archive"]["reason"] == reason
    assert (wd / "experiment.env").read_text() == "recipe"
    assert not list((run / "base_snapshots").iterdir())


def test_corrupt_existing_archive_is_refused_without_replacement(tmp_path):
    _, run, _, seeder = _seeder(tmp_path)
    wd = run / "nodes" / "node_0"
    first = seeder.materialize(_node(), wd)
    archive = verified_seed_archive(run, first)
    (archive / "score.py").write_text("corrupted")
    assert verified_seed_archive(run, first) is None
    second = seeder.materialize(_node(), wd)
    assert second["complete"] and second["archive"]["reason"] == "archive_conflict"
    assert (archive / "score.py").read_text() == "corrupted"
    assert len(list((run / "base_snapshots").iterdir())) == 1


@pytest.mark.parametrize("field,value", [("path", "../source"), ("version", 2),
                                      ("version", True), ("status", "unavailable")])
def test_untrusted_archive_reference_grants_no_path(tmp_path, field, value):
    _, run, _, seeder = _seeder(tmp_path)
    receipt = seeder.materialize(_node(), run / "nodes" / "node_0")
    bad = copy.deepcopy(receipt)
    bad["archive"][field] = value
    assert verified_seed_archive(run, bad) is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable-bit semantics")
def test_archive_preserves_executable_bits_with_restrictive_umask(tmp_path):
    src, run, _, seeder = _seeder(tmp_path)
    (src / "score.py").chmod(0o755)
    original = os.umask(0o077)
    try:
        receipt = seeder.materialize(_node(), run / "nodes" / "node_0")
    finally:
        os.umask(original)
    archive = verified_seed_archive(run, receipt)
    assert archive is not None and (archive / "score.py").stat().st_mode & 0o111 == 0o111
