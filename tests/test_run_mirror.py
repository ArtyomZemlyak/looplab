"""The built-in run-directory mirror (`events/run_mirror.py`, doc 80 P2): only changed files, no
torn JSONL line, a verified restore that resumes to the same state, a hung destination that stops
the pass instead of the caller, and no destination listing during a pass."""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import anyio
import pytest

from looplab.events import run_mirror
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.events.run_mirror import (MANIFEST, MirrorConfig, MirrorStuck, mirror_loop,
                                       mirror_pass, mirror_status, restore_missing, restore_run)
from factories import make_engine


def _cfg(tmp_path, **kw) -> MirrorConfig:
    return MirrorConfig(src_root=tmp_path / "src", dst_root=tmp_path / "dst",
                        max_bytes_per_s=0, max_files_per_s=0, **kw)


def _fake_run(root: Path, name: str = "r1") -> Path:
    rd = root / name
    (rd / "nodes" / "node_0").mkdir(parents=True)
    (rd / "events.jsonl").write_text('{"seq":0,"type":"run_started","data":{}}\n', encoding="utf-8")
    (rd / "task.snapshot.json").write_text("{}", encoding="utf-8")
    (rd / "nodes" / "node_0" / "train.py").write_text("print(1)\n", encoding="utf-8")
    (rd / "engine.lock").write_text("", encoding="utf-8")
    (rd / "readmodel.sqlite").write_bytes(b"x")
    (rd / "code" / "repo.git").mkdir(parents=True)
    (rd / "code" / "repo.git" / "HEAD").write_text("ref", encoding="utf-8")
    return rd


def test_a_pass_copies_the_run_and_skips_what_is_rebuildable(tmp_path):
    rd = _fake_run(tmp_path / "src")
    cfg = _cfg(tmp_path)
    report = mirror_pass(cfg)
    assert report.stuck is None and not report.errors
    out = tmp_path / "dst" / "r1"
    assert (out / "events.jsonl").read_bytes() == (rd / "events.jsonl").read_bytes()
    assert (out / "nodes" / "node_0" / "train.py").exists()
    for skipped in ("engine.lock", "readmodel.sqlite", "code/repo.git/HEAD"):
        assert not (out / skipped).exists(), skipped
    manifest = json.loads((out / MANIFEST).read_text())
    assert set(manifest["files"]) == {"events.jsonl", "task.snapshot.json", "nodes/node_0/train.py"}
    assert (os.stat(out / "events.jsonl").st_mtime_ns
            == os.stat(rd / "events.jsonl").st_mtime_ns)


def test_only_changed_files_are_copied_again(tmp_path):
    rd = _fake_run(tmp_path / "src")
    cfg = _cfg(tmp_path)
    mirror_pass(cfg)
    second = mirror_pass(cfg)
    assert second.runs["r1"]["copied"] == 0
    with open(rd / "events.jsonl", "a", encoding="utf-8") as fh:
        fh.write('{"seq":1,"type":"pause","data":{}}\n')
    third = mirror_pass(cfg)
    assert third.runs["r1"]["copied"] == 1


def test_a_torn_last_jsonl_line_is_not_mirrored(tmp_path):
    rd = _fake_run(tmp_path / "src")
    with open(rd / "events.jsonl", "a", encoding="utf-8") as fh:
        fh.write('{"seq":1,"type":"pa')                         # an append caught mid-write
    mirror_pass(_cfg(tmp_path))
    copied = (tmp_path / "dst" / "r1" / "events.jsonl").read_text()
    assert copied.endswith("\n") and '"pa' not in copied
    row = json.loads((tmp_path / "dst" / "r1" / MANIFEST).read_text())["files"]["events.jsonl"]
    assert row["size"] < row["source_size"]


def test_a_file_over_the_cap_is_named_not_copied(tmp_path):
    rd = _fake_run(tmp_path / "src")
    (rd / "nodes" / "node_0" / "model.bin").write_bytes(b"\0" * 4096)
    mirror_pass(_cfg(tmp_path, max_file_bytes=1024))
    manifest = json.loads((tmp_path / "dst" / "r1" / MANIFEST).read_text())
    assert "nodes/node_0/model.bin" in manifest["skipped"]
    assert not (tmp_path / "dst" / "r1" / "nodes" / "node_0" / "model.bin").exists()


def test_a_hung_destination_stops_the_pass_and_is_recorded(tmp_path, monkeypatch):
    _fake_run(tmp_path / "src")
    cfg = _cfg(tmp_path, op_timeout_s=0.2)
    gate = threading.Event()
    real = run_mirror._write_json_atomic

    def hang(path, data):
        if str(path).startswith(str(cfg.dst_root)):
            gate.wait(5)                                   # a FUSE call that does not return
        return real(path, data)

    real_copy = run_mirror.copy_one

    def hang_copy(*a, **kw):
        def stuck():
            gate.wait(5)
        run_mirror._deadline_call(stuck, cfg.op_timeout_s, "writing x")
        return real_copy(*a, **kw)

    monkeypatch.setattr(run_mirror, "copy_one", hang_copy)
    t0 = time.monotonic()
    code = mirror_loop(cfg, passes=1, exit_on_stuck=True, log=lambda _l: None)
    gate.set()
    assert code == 3 and time.monotonic() - t0 < 4
    status = mirror_status(cfg)
    assert status["stuck"] and "did not return" in status["stuck"]["detail"]


def test_a_pass_never_lists_the_destination(tmp_path, monkeypatch):
    """The metadata-storm rule: the state file is the memory of what was copied."""
    _fake_run(tmp_path / "src")
    cfg = _cfg(tmp_path)
    mirror_pass(cfg)
    listed = []
    real_listdir, real_scandir = os.listdir, os.scandir
    dst = str(cfg.dst_root)

    def watch_listdir(p="."):
        if str(p).startswith(dst):
            listed.append(str(p))
        return real_listdir(p)

    def watch_scandir(p="."):
        if str(p).startswith(dst):
            listed.append(str(p))
        return real_scandir(p)

    monkeypatch.setattr(os, "listdir", watch_listdir)
    monkeypatch.setattr(os, "scandir", watch_scandir)
    with open(cfg.src_root / "r1" / "events.jsonl", "a", encoding="utf-8") as fh:
        fh.write('{"seq":1}\n')
    mirror_pass(cfg)
    assert listed == []


def test_a_lost_state_is_seeded_from_the_manifest_not_a_full_reupload(tmp_path):
    _fake_run(tmp_path / "src")
    cfg = _cfg(tmp_path)
    mirror_pass(cfg)
    cfg.state_path.unlink()                                  # the pod restarted: tmpfs is empty
    again = mirror_pass(cfg)
    assert again.runs["r1"]["copied"] == 0


def test_restore_refuses_a_corrupted_event_log_and_an_existing_run(tmp_path):
    _fake_run(tmp_path / "src")
    cfg = _cfg(tmp_path)
    mirror_pass(cfg)
    with pytest.raises(FileExistsError):
        restore_run(cfg.dst_root, "r1", cfg.src_root)
    (cfg.dst_root / "r1" / "events.jsonl").write_text("garbage\n")
    with pytest.raises(RuntimeError, match="events.jsonl"):
        restore_run(cfg.dst_root, "r1", cfg.src_root, as_name="r1-copy")
    assert not (cfg.src_root / "r1-copy").exists()
    assert not any(p.name.startswith(".r1-copy") for p in cfg.src_root.iterdir())


def test_a_real_run_survives_mirror_wipe_restore_and_folds_identically(tmp_path):
    src = tmp_path / "src"
    eng = make_engine(src / "run", n_seeds=2, max_nodes=3)
    anyio.run(eng.run)
    before = fold(EventStore(src / "run" / "events.jsonl").read_all())
    cfg = _cfg(tmp_path)
    assert mirror_pass(cfg).stuck is None
    import shutil
    kept = {q.relative_to(src / "run").as_posix(): q.read_bytes()
            for q in (src / "run").rglob("*") if q.is_file()
            and run_mirror._skipped(q.relative_to(src / "run").as_posix(), cfg) is None}
    shutil.rmtree(src / "run")                               # the restart wipes tmpfs
    cfg.state_path.unlink()
    restored = restore_missing(cfg.dst_root, cfg.src_root)
    assert [r.get("run") for r in restored] == ["run"] and not restored[0]["failures"]
    after = fold(EventStore(src / "run" / "events.jsonl").read_all())
    assert after.model_dump() == before.model_dump()
    back = {q.relative_to(src / "run").as_posix(): q.read_bytes()
            for q in (src / "run").rglob("*") if q.is_file()}
    assert back == kept
