"""Portable bases come from recorded receipts; export never fills gaps from live code."""
import json
import os

import pytest

from looplab.core.atomicio import rmtree_readonly_aware
from looplab.engine.bundle import RO_CRATE_METADATA, export_bundle, verify_bundle
from looplab.engine.bundle_bases import BASE_INDEX
from looplab.engine.seed_archive import verified_seed_archive
from test_seeded_base_revision import _seeder, _node


def _recorded(tmp_path):
    src, run, engine, seeder = _seeder(tmp_path)
    engine.store.append("run_started", {"run_id": "archive", "task_id": "repo", "goal": "archive", "direction": "min"})
    engine.store.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                                        "idea": {"operator": "draft"}, "files": {}, "code": ""})
    wd = run / "nodes" / "node_0"
    seed = seeder.materialize(_node(), wd)
    receipt = {**seed, "node_id": 0, "generation": 0}
    engine.store.append("node_evaluated", {"node_id": 0, "generation": 0, "metric": 1., "eval_seconds": 0.,
        "violations": [], "metric_provenance": {"base_revision": receipt}})
    return src, run, engine, seeder, receipt


def _index(out):
    return json.loads((out / BASE_INDEX).read_text(encoding="utf8"))


def test_bundle_retains_bases_after_source_and_workdir_loss(tmp_path):
    src, run, _, _, receipt = _recorded(tmp_path)
    rmtree_readonly_aware(src)
    rmtree_readonly_aware(run / "nodes")
    out = tmp_path / "bundle"
    meta = export_bundle(run, out)
    rows = _index(out)["entries"]
    assert len(rows) == 2 and all(row["status"] == "exported" for row in rows)
    assert {row["event_type"] for row in rows} == {"workspace_seeded", "node_evaluated"}
    assert rows[1]["receipt"] == receipt
    assert (out / "events.jsonl").read_bytes() == (run / "events.jsonl").read_bytes()
    assert (verified_seed_archive(out, receipt) / "experiment.env").read_text() == "VALUE=old\n"
    ids = [e["@id"] for e in meta["@graph"] if e.get("@type") == "File"]
    assert len(ids) == len(set(ids)) and BASE_INDEX in ids
    root = next(e for e in meta["@graph"] if e["@id"] == "./")
    assert root["looplab:seed_archives"] == {"recorded_receipts": 2, "exported_receipts": 2,
                                            "unavailable_receipts": 0, "archives": 1}
    assert verify_bundle(out) == []


def test_reset_history_exports_both_bases_without_inventing_current_scores(tmp_path):
    src, run, engine, seeder, old = _recorded(tmp_path)
    engine.store.append("node_reset", {"node_id": 0, "generation": 1})
    (src / "experiment.env").write_text("new base")
    new = seeder.materialize(_node(), run / "nodes" / "node_0")
    out = tmp_path / "bundle"
    export_bundle(run, out)
    rows = _index(out)["entries"]
    assert len(rows) == 3 and rows[1]["generation"] == 0
    assert {row["receipt"]["digest"] for row in rows} == {old["digest"], new["digest"]}
    assert verified_seed_archive(out, old) is not None and verified_seed_archive(out, new) is not None
    assert verify_bundle(out) == []


@pytest.mark.parametrize("fault", ["missing", "corrupt", "unknown"])
def test_unavailable_bases_are_reported_without_source_fallback(tmp_path, fault):
    src, run, engine, _, receipt = _recorded(tmp_path)
    archive = verified_seed_archive(run, receipt)
    if fault == "missing":
        rmtree_readonly_aware(archive)
    elif fault == "corrupt":
        (archive / "score.py").write_text("bad")
    else:
        bad = {**receipt, "complete": False, "digest": None}
        engine.store.append("workspace_seeded", {"node_id": 1, "materialized": [], "base_revision": bad})
    (src / "experiment.env").write_text("live fallback forbidden")
    out = tmp_path / "bundle"
    meta = export_bundle(run, out)
    rows = _index(out)["entries"]
    omitted = [row for row in rows if row["status"] == "unavailable"]
    assert len(omitted) == (1 if fault == "unknown" else 2)
    assert all(row["path"] is None and row["reason"] for row in omitted)
    if fault != "unknown":
        assert not any(e.get("@id", "").endswith("/score.py") for e in meta["@graph"])
    assert verify_bundle(out) == []  # explicit omissions, not a verified available base


def test_source_drift_between_validation_and_copy_publishes_no_wrong_base(tmp_path, monkeypatch):
    from looplab.engine import seed_archive
    _, run, _, _, receipt = _recorded(tmp_path)
    real = seed_archive.verified_seed_archive
    def drift(root, row):
        path = real(root, row)
        if path is not None:
            (path / "score.py").write_text("changed after verification")
        return path
    monkeypatch.setattr(seed_archive, "verified_seed_archive", drift)
    out = tmp_path / "bundle"
    export_bundle(run, out)
    assert all(row["reason"] == "archive_source_changed" for row in _index(out)["entries"])
    assert list((out / "base_snapshots").iterdir()) == [out / BASE_INDEX]


@pytest.mark.parametrize("fault", ["missing", "contents", "index", "crate_member", "crate_alias"])
def test_verification_checks_bases_and_their_event_binding(tmp_path, fault):
    _, run, _, _, receipt = _recorded(tmp_path)
    out = tmp_path / "bundle"
    export_bundle(run, out)
    archive = verified_seed_archive(out, receipt)
    if fault == "missing":
        (archive / "score.py").unlink()
    elif fault == "contents":
        (archive / "score.py").write_text("bad")
    elif fault == "index":
        index = _index(out)
        index["entries"][0]["event_seq"] += 1
        (out / BASE_INDEX).write_text(json.dumps(index))
    elif fault == "crate_member":
        meta = json.loads((out / RO_CRATE_METADATA).read_text())
        meta["@graph"] = [e for e in meta["@graph"] if not e.get("@id", "").endswith("/score.py")]
        (out / RO_CRATE_METADATA).write_text(json.dumps(meta))
    else:
        meta = json.loads((out / RO_CRATE_METADATA).read_text())
        member = next(e for e in meta["@graph"] if e.get("@id", "").endswith("/score.py"))
        member["@id"] = member["@id"].replace("/score.py", "//score.py")
        (out / RO_CRATE_METADATA).write_text(json.dumps(meta))
    assert verify_bundle(out)


@pytest.mark.parametrize("where", ["run", "archive", "archive_child"])
def test_bundle_output_cannot_mutate_the_run_or_its_bases(tmp_path, where):
    _, run, _, _, receipt = _recorded(tmp_path)
    archive = verified_seed_archive(run, receipt)
    out = run if where == "run" else archive if where == "archive" else archive / "nested"
    original = (archive / "score.py").read_bytes()
    with pytest.raises(ValueError, match="must not overwrite"):
        export_bundle(run, out)
    assert (archive / "score.py").read_bytes() == original


def test_cli_names_unavailable_receipts(tmp_path):
    from typer.testing import CliRunner
    from looplab.cli import app
    _, run, _, _, receipt = _recorded(tmp_path)
    rmtree_readonly_aware(verified_seed_archive(run, receipt))
    result = CliRunner().invoke(app, ["export-bundle", str(run)])
    assert result.exit_code == 0, result.output
    assert "2 unavailable receipt(s)" in result.output


def test_unreferenced_archives_and_staging_are_not_exported(tmp_path):
    _, run, _, _, _ = _recorded(tmp_path)
    extra = run / "base_snapshots" / "unreferenced"
    extra.mkdir()
    (extra / "secret").write_text("not recorded")
    out = tmp_path / "bundle"
    export_bundle(run, out)
    assert not (out / "base_snapshots" / "unreferenced").exists()
    assert verify_bundle(out) == []


def test_unsafe_crate_member_is_rejected_before_reading(tmp_path):
    _, run, _, _, _ = _recorded(tmp_path)
    out = tmp_path / "bundle"
    export_bundle(run, out)
    meta = json.loads((out / RO_CRATE_METADATA).read_text())
    meta["@graph"].append({"@id": "../outside", "@type": "File"})
    (out / RO_CRATE_METADATA).write_text(json.dumps(meta))
    assert "unsafe file reference ../outside" in verify_bundle(out)


def test_linked_archive_output_does_not_write_outside_bundle(tmp_path):
    _, run, _, _, _ = _recorded(tmp_path)
    out, outside = tmp_path / "bundle", tmp_path / "outside"
    out.mkdir()
    outside.mkdir()
    try:
        (out / "base_snapshots").symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        if getattr(exc, "winerror", None) != 1314:
            raise
        pytest.skip("Windows symlink privilege unavailable")
    with pytest.raises(ValueError, match="linked"):
        export_bundle(run, out)
    assert list(outside.iterdir()) == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable-bit semantics")
def test_base_verification_detects_mode_loss_even_when_bytes_match(tmp_path):
    _, run, _, _, receipt = _recorded(tmp_path)
    out = tmp_path / "bundle"
    export_bundle(run, out)
    archive = verified_seed_archive(out, receipt)
    (archive / "score.py").chmod(0o700)
    assert any("executable bits mismatch" in defect for defect in verify_bundle(out))
