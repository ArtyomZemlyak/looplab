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


# ------------------------------------------------------------------ review g8: the four write faults

def test_a_torn_tailed_jsonl_is_not_recopied_every_pass(tmp_path):
    """The row records the CUT as `size` and the source's own as `source_size`; the change check
    compared the cut with the source's `st_size`, so a torn tail meant a re-copy on every pass."""
    rd = _fake_run(tmp_path / "src")
    with open(rd / "events.jsonl", "a", encoding="utf-8") as fh:
        fh.write('{"seq":1,"type":"pa')                         # a writer that died mid-line
    cfg = _cfg(tmp_path)
    assert mirror_pass(cfg).runs["r1"]["copied"] == 3
    assert mirror_pass(cfg).runs["r1"]["copied"] == 0
    with open(rd / "events.jsonl", "a", encoding="utf-8") as fh:
        fh.write('use"}\n')                                      # ...and it moves again
    assert mirror_pass(cfg).runs["r1"]["copied"] == 1


def test_a_pass_that_dies_before_the_manifest_rewrites_it_on_the_next_pass(tmp_path, monkeypatch):
    """The state recorded the new copy, the manifest kept the old SHA, and no later pass rewrote
    the manifest (nothing had "changed"): `run-mirror-restore` refused that file forever."""
    rd = _fake_run(tmp_path / "src")
    cfg = _cfg(tmp_path, op_timeout_s=0.2)
    mirror_pass(cfg)
    with open(rd / "events.jsonl", "a", encoding="utf-8") as fh:
        fh.write('{"seq":1,"type":"pause","data":{}}\n')
    gate = threading.Event()
    real = run_mirror._write_json_atomic

    def manifest_never_lands(path, data):
        if str(path).startswith(str(cfg.dst_root)):
            gate.wait(5)                                         # a FUSE call that never returns
            return None                                          # ...and the write is lost
        return real(path, data)

    monkeypatch.setattr(run_mirror, "_write_json_atomic", manifest_never_lands)
    assert mirror_pass(cfg).stuck
    gate.set()
    monkeypatch.setattr(run_mirror, "_write_json_atomic", real)
    mirror_pass(cfg)
    report = restore_run(cfg.dst_root, "r1", cfg.src_root, as_name="r1-back")
    assert not report["failures"]
    assert ((cfg.src_root / "r1-back" / "events.jsonl").read_bytes()
            == (rd / "events.jsonl").read_bytes())


def test_a_deletion_only_pass_rewrites_the_manifest_and_restore_does_not_resurrect(tmp_path):
    """weekly-mirror-deletion (review 2026-10-15): a pass whose ONLY change was a deleted file
    popped its row from the state and skipped the manifest write (nothing copied, nothing newly
    skipped), so `restore_run` brought back a deleted `gpu_gate.json` with its old `hold=true`."""
    rd = _fake_run(tmp_path / "src")
    (rd / "gpu_gate.json").write_text('{"hold": true}', encoding="utf-8")
    cfg = _cfg(tmp_path)
    mirror_pass(cfg)
    manifest = cfg.dst_root / "r1" / MANIFEST
    assert "gpu_gate.json" in json.loads(manifest.read_text())["files"]
    (rd / "gpu_gate.json").unlink()                             # the operator released the gate
    report = mirror_pass(cfg)
    assert report.runs["r1"]["copied"] == 0                     # a deletion-only pass ...
    assert "gpu_gate.json" not in json.loads(manifest.read_text())["files"]   # ... rewrote it
    restored = restore_run(cfg.dst_root, "r1", cfg.src_root, as_name="r1-back")
    assert not restored["failures"]
    assert not (cfg.src_root / "r1-back" / "gpu_gate.json").exists()


def test_a_deletion_survives_a_mirror_restart_between_the_pop_and_the_manifest(tmp_path,
                                                                                monkeypatch):
    """The same removal across a restarted mirror process: a pass that popped the row and then hung
    on the manifest write (state saved, manifest stale), and a process that lost its state file and
    re-seeds it FROM the stale manifest — each reloads its state from disk and still rewrites the
    manifest without the deleted file."""
    rd = _fake_run(tmp_path / "src")
    (rd / "gpu_gate.json").write_text('{"hold": true}', encoding="utf-8")
    (rd / "notes.json").write_text("{}", encoding="utf-8")
    cfg = _cfg(tmp_path, op_timeout_s=0.2)
    mirror_pass(cfg)
    manifest = cfg.dst_root / "r1" / MANIFEST
    (rd / "gpu_gate.json").unlink()
    gate = threading.Event()
    real = run_mirror._write_json_atomic

    def manifest_never_lands(path, data):
        if str(path).startswith(str(cfg.dst_root)):
            gate.wait(5)
            return None
        return real(path, data)

    monkeypatch.setattr(run_mirror, "_write_json_atomic", manifest_never_lands)
    assert mirror_pass(cfg).stuck
    gate.set()
    monkeypatch.setattr(run_mirror, "_write_json_atomic", real)
    assert "gpu_gate.json" in json.loads(manifest.read_text())["files"]       # still stale
    assert "gpu_gate.json" not in run_mirror.load_state(cfg)["runs"]["r1"]["files"]
    mirror_pass(cfg)                                            # a NEW pass, state read from disk
    assert "gpu_gate.json" not in json.loads(manifest.read_text())["files"]
    # A restarted mirror whose state file is gone re-seeds from the destination manifest: a file
    # deleted while it was down is popped from the seeded rows and the manifest is rewritten.
    (rd / "notes.json").unlink()
    cfg.state_path.unlink()
    assert mirror_pass(cfg).runs["r1"]["copied"] == 0
    assert "notes.json" not in json.loads(manifest.read_text())["files"]
    back = restore_run(cfg.dst_root, "r1", cfg.src_root, as_name="r1-back")
    assert not back["failures"]
    assert not (cfg.src_root / "r1-back" / "gpu_gate.json").exists()
    assert not (cfg.src_root / "r1-back" / "notes.json").exists()


def test_run_extras_are_mirrored_kept_on_a_failed_producer_and_dropped_when_gone(tmp_path):
    """`MirrorConfig.run_extras`: files derived OUTSIDE the run (the agent-worktree backup) ride in
    the run's manifest; a producer that raises keeps the last copy listed; one that stops
    returning a file drops its row and rewrites the manifest."""
    _fake_run(tmp_path / "src")
    mode = {"now": "ok"}

    def extras(run_dir, spool):
        if mode["now"] == "boom":
            raise RuntimeError("git timed out")
        if mode["now"] == "gone":
            return {}
        spool.mkdir(parents=True, exist_ok=True)
        (spool / "b.bin").write_bytes(b"bundle")
        return {"code/agent_backup/agents.bundle": spool / "b.bin", "../escape": spool / "b.bin",
                "engine.lock": spool / "b.bin"}

    cfg = _cfg(tmp_path, run_extras=extras)
    mirror_pass(cfg)
    manifest = cfg.dst_root / "r1" / MANIFEST
    files = json.loads(manifest.read_text())["files"]
    assert "code/agent_backup/agents.bundle" in files
    assert "../escape" not in files and "engine.lock" not in files
    assert (cfg.dst_root / "r1" / "code" / "agent_backup" / "agents.bundle").read_bytes() == b"bundle"
    mode["now"] = "boom"
    report = mirror_pass(cfg)
    assert [e["path"] for e in report.errors] == ["<run_extras>"]
    assert "code/agent_backup/agents.bundle" in json.loads(manifest.read_text())["files"]
    mode["now"] = "gone"
    mirror_pass(cfg)
    assert "code/agent_backup/agents.bundle" not in json.loads(manifest.read_text())["files"]


class _FaultyWrite:
    """A destination file handle whose `write` runs `fault` first (hang or raise)."""

    def __init__(self, fh, fault):
        self.fh, self.fault = fh, fault

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return self.fh.__exit__(*exc)

    def write(self, data):
        self.fault()
        return self.fh.write(data)


def _fault_dst_writes(monkeypatch, cfg, name: str, fault, *, once: bool = True):
    """Route `open(<dst file whose name starts with name>, "wb")` inside `run_mirror` through
    `_FaultyWrite`. Patches the module's own `open` (a global lookup), never the builtin."""
    real_open, hits = open, []

    def faulty_open(path, mode="r", *a, **kw):
        fh = real_open(path, mode, *a, **kw)
        p = Path(path)
        if ("w" in mode and str(p).startswith(str(cfg.dst_root)) and p.name.startswith(name)
                and not (once and hits)):
            hits.append(p)
            return _FaultyWrite(fh, fault)
        return fh

    monkeypatch.setattr(run_mirror, "open", faulty_open, raising=False)
    return hits


def test_an_abandoned_writer_never_lands_its_stale_bytes_on_a_later_copy(tmp_path, monkeypatch):
    """Pass 1's writer hangs inside `write` and is abandoned; pass 2 copies the NEWER file. With one
    shared `<name>.mirror-tmp` pass 2 reopened the inode the abandoned writer still held, renamed it
    into place, and the late write then landed stale bytes in the published copy."""
    rd = _fake_run(tmp_path / "src")
    cfg = _cfg(tmp_path, op_timeout_s=0.3)
    cfg.max_bytes_per_s = 1e12            # the copy deadline is then op_timeout_s, not 4 s per byte
    gate = threading.Event()
    _fault_dst_writes(monkeypatch, cfg, "train.py", lambda: gate.wait(5))
    assert mirror_pass(cfg).stuck
    (rd / "nodes" / "node_0" / "train.py").write_text("print(22222)\n", encoding="utf-8")
    assert mirror_pass(cfg).stuck is None
    gate.set()                                                   # the abandoned writer wakes
    for t in threading.enumerate():
        if t.name == "looplab-mirror-op":
            t.join(5)
    out = cfg.dst_root / "r1" / "nodes" / "node_0" / "train.py"
    assert out.read_text(encoding="utf-8") == "print(22222)\n"
    assert not [p for p in out.parent.iterdir() if p.name.endswith(run_mirror.TMP_SUFFIX)]
    assert not restore_run(cfg.dst_root, "r1", cfg.src_root, as_name="r1-back")["failures"]


def test_a_streamed_large_file_never_truncates_the_last_good_copy(tmp_path, monkeypatch):
    """Over `atomic_max_bytes` the copy was streamed with `open(dst, "wb")`: a transfer that failed
    part-way left the ONLY durable copy truncated, under a manifest still vouching for it."""
    rd = _fake_run(tmp_path / "src")
    cfg = _cfg(tmp_path, atomic_max_bytes=4)
    mirror_pass(cfg)
    out = cfg.dst_root / "r1" / "nodes" / "node_0" / "train.py"
    assert out.read_text(encoding="utf-8") == "print(1)\n"      # the streamed path, healthy
    (rd / "nodes" / "node_0" / "train.py").write_text("print(22222)\n", encoding="utf-8")

    def disk_full():
        raise OSError(28, "No space left on device")

    _fault_dst_writes(monkeypatch, cfg, "train.py", disk_full)
    report = mirror_pass(cfg)
    assert [e["path"] for e in report.errors] == ["nodes/node_0/train.py"]
    assert out.read_text(encoding="utf-8") == "print(1)\n"      # the good copy is untouched
    assert not [p for p in out.parent.iterdir() if p.name.endswith(run_mirror.TMP_SUFFIX)]
    assert not restore_run(cfg.dst_root, "r1", cfg.src_root, as_name="r1-back")["failures"]
    monkeypatch.undo()
    mirror_pass(cfg)
    assert out.read_text(encoding="utf-8") == "print(22222)\n"


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
