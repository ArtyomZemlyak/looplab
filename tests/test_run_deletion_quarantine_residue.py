"""A file created between the quarantine move's SCAN and its END, and the wedge it used to be.

THE INCIDENT (`runs/rubertlite-dr-unified-v5`, 2026-08-13). Its deletion had moved 2.7 GB into
`.looplab-delete-quarantine-<key>.<uuid>` and then stopped, leaving both the quarantine and a source
directory holding SIX files — five `*.pyc.<n>` (CPython's own temp name for a bytecode write) and one
92 MB `.tmp*` (`tempfile`'s), every one a partial atomic write left by a process killed mid-flight.
On this deployment a directory rename is not one syscall: the runs live on a `fuse.geesefs` mount,
where moving a directory is a per-object walk, and anything created after that walk's scan is simply
left where it was.

Every retry then answered `409 delete_quarantine_conflict` — **`retryable: true`, and no number of
retries could ever clear it**, because nothing in the server removed those files. The operation owned
the run forever and the operator could not finish it from the UI at all.

The refusal itself was right and stays: 88 MB of real data sat in that source, and blindly removing
it is the worse bug by a distance. What was wrong is that a residue this operation could simply
FINISH MOVING produced a permanently unresolvable state, and that the state lied about being
retryable. So there are two properties here and they are different:

* the residue is ABSORBED — carried into this operation's own quarantine, which is where everything
  this deletion owns already lives. Nothing is deleted to make that true, and nothing is replaced;
* what genuinely cannot be absorbed still refuses, and now says `retryable: false` and names the
  exact paths, because a retry that cannot possibly progress must not ask to be pressed again.

`tests/test_durable_no_replace_rename.py` owns the rename primitive; this owns what the deletion
service does with a move that did not finish.
"""
from __future__ import annotations

import errno
import os
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from looplab.core.atomicio import durable_no_replace_rename
from looplab.events.eventstore import EventStore
from looplab.serve import deletion_service
from looplab.serve.deletion_service import _absorb_quarantine_residue
from looplab.serve.deletion_transaction import DELETE_QUARANTINE_PREFIX
from looplab.serve.run_commands import run_generation_token
from looplab.serve.server import make_app


RUN = "demo"
OPERATION = "3f2c1b0a-1111-4111-8111-1111111111aa"

# The six names from the incident, verbatim in shape: CPython's bytecode temp and `tempfile`'s.
RESIDUE = (
    "node_0/vectorsearch/__pycache__/__init__.cpython-312.pyc.140558024057008",
    "node_4/vectorsearch/__pycache__/config.cpython-312.pyc.139885358841680",
    "node_0/vectorsearch/experiments/final/.tmpFJRFTG",
)


def _run(tmp_path: Path) -> Path:
    run_dir = tmp_path / RUN
    (run_dir / "node_0" / "vectorsearch" / "__pycache__").mkdir(parents=True)
    (run_dir / "node_0" / "vectorsearch" / "experiments" / "final").mkdir(parents=True)
    (run_dir / "node_4" / "vectorsearch" / "__pycache__").mkdir(parents=True)
    (run_dir / "events.jsonl").write_text(
        '{"seq":0,"type":"run_started","data":{}}\n', encoding="utf-8")
    # The real files the move DOES carry — the ones the temp names above are partial writes of.
    for whole in ("node_0/vectorsearch/__pycache__/__init__.cpython-312.pyc",
                  "node_4/vectorsearch/__pycache__/config.cpython-312.pyc",
                  "node_0/vectorsearch/experiments/final/model.safetensors"):
        (run_dir / whole).write_text("the bytes that arrived", encoding="utf-8")
    return run_dir


def _identity(run_dir: Path) -> dict:
    events = EventStore(run_dir / "events.jsonl").read_all()
    return {"operation_id": OPERATION,
            "expected_generation": run_generation_token(events),
            "expected_seq": events[-1].seq if events else -1}


def _quarantine(tmp_path: Path) -> Path:
    hits = [p for p in tmp_path.iterdir() if p.name.startswith(DELETE_QUARANTINE_PREFIX)]
    assert len(hits) == 1, f"expected exactly one quarantine, found {[p.name for p in hits]}"
    return hits[0]


def _interrupted_geesefs_move(monkeypatch, *, plant=RESIDUE, body="partial atomic write",
                              raising=True):
    """The move as this deployment actually performs it, interrupted the way the incident was.

    geesefs renames a directory object by object. This walks the source ONCE, moves what the walk
    saw, plants files that were not in it (a live process finishing an atomic write), and then either
    raises — a process killed mid-flight, which is the incident — or simply RETURNS, which is the
    same missed entry reported as a success. Both leave the same filesystem: a populated quarantine
    and a source that is not empty.
    """
    real = durable_no_replace_rename
    state = {"done": False}

    def _move(source, destination, *, label, unique_destination=False, cross_directory=False):
        source, destination = Path(source), Path(destination)
        if state["done"] or not source.is_dir():
            return real(source, destination, label=label,
                        unique_destination=unique_destination, cross_directory=cross_directory)
        state["done"] = True
        scanned = sorted(path.relative_to(source) for path in source.rglob("*"))
        destination.mkdir()
        for relative in scanned:
            child = source / relative
            if child.is_dir():
                (destination / relative).mkdir(parents=True, exist_ok=True)
            else:
                (destination / relative).parent.mkdir(parents=True, exist_ok=True)
                os.rename(child, destination / relative)
        for relative in plant:                       # created AFTER the scan, before the move ends
            target = source / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body, encoding="utf-8")
        for relative in sorted(scanned, key=lambda p: len(p.parts), reverse=True):
            child = source / relative
            if child.is_dir():
                try:
                    child.rmdir()
                except OSError:
                    pass                             # the planted files hold their parents open
        if raising:
            raise OSError(errno.EIO, "the mount dropped the connection mid-move",
                          str(destination))
        # ...or it simply RETURNS. A per-object move that missed an entry reports success.

    monkeypatch.setattr(deletion_service, "durable_no_replace_rename", _move)


# ---------------------------------------------------------------- the reproduction, end to end


def test_a_file_created_between_the_scan_and_the_move_no_longer_wedges_the_deletion(
        tmp_path, monkeypatch):
    """THE incident, built rather than asserted, and driven to a terminal state with no human
    touching the filesystem."""
    run_dir = _run(tmp_path)
    _interrupted_geesefs_move(monkeypatch)
    client = TestClient(make_app(tmp_path))
    body = _identity(run_dir)

    first = client.post(f"/api/runs/{RUN}/deletions", json=body)
    assert first.status_code == 202 and first.json()["status"] == "pending"
    assert first.json()["phase"] == "quarantining"
    assert run_dir.exists(), "the interrupted move is the whole point of the fixture"
    assert sorted(str(p.relative_to(run_dir)) for p in run_dir.rglob("*") if p.is_file()) == sorted(
        str(Path(name)) for name in RESIDUE)
    assert _quarantine(tmp_path).is_dir()

    second = client.post(f"/api/runs/{RUN}/deletions", json=body)

    assert second.status_code == 200, second.text
    assert second.json()["status"] == "succeeded" and second.json()["phase"] == "succeeded"
    assert not run_dir.exists(), "the run is gone"
    assert not any(p.name.startswith(DELETE_QUARANTINE_PREFIX) for p in tmp_path.iterdir())


def test_the_retry_is_the_operators_ONLY_step(tmp_path, monkeypatch):
    """The operator's exit was "verify each residual file by name and delete it by hand". Pinned as
    a negative: between the two requests nothing may touch the tree, so the second request has to be
    the whole remedy."""
    run_dir = _run(tmp_path)
    _interrupted_geesefs_move(monkeypatch)
    client = TestClient(make_app(tmp_path))
    body = _identity(run_dir)
    client.post(f"/api/runs/{RUN}/deletions", json=body)
    stranded = [run_dir / name for name in RESIDUE]
    assert all(path.is_file() for path in stranded), "the wedge the operator had to clear by hand"

    # Nothing between these two lines. That IS the property.
    assert client.post(f"/api/runs/{RUN}/deletions", json=body).json()["status"] == "succeeded"

    assert not any(path.exists() for path in stranded) and not run_dir.exists()
    assert not any(p.name.startswith(DELETE_QUARANTINE_PREFIX) for p in tmp_path.iterdir())


def test_a_move_that_RETURNS_is_not_proof_the_source_is_gone(tmp_path, monkeypatch):
    """The same missed entry without the interruption. A per-object move that skipped a file still
    reports success, and the code used to trust `source_exists` read BEFORE the move — advancing to
    `quarantined`, purging the quarantine, retiring the fence, and leaving an orphan run directory
    holding the only surviving copy of what was not carried. It is re-read instead, and absorbed."""
    run_dir = _run(tmp_path)
    _interrupted_geesefs_move(monkeypatch, raising=False)
    client = TestClient(make_app(tmp_path))

    response = client.post(f"/api/runs/{RUN}/deletions", json=_identity(run_dir))

    assert response.json()["status"] == "succeeded", response.text
    assert not run_dir.exists(), "no orphan run directory beside a purged quarantine"
    assert not any(p.name.startswith(DELETE_QUARANTINE_PREFIX) for p in tmp_path.iterdir())


# ---------------------------------------------------------------- what absorption may and may not do


def _staged(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / RUN
    quarantine = tmp_path / f"{DELETE_QUARANTINE_PREFIX}abc.{OPERATION}"
    (source / "node_0" / "sub").mkdir(parents=True)
    (quarantine / "node_0" / "sub").mkdir(parents=True)
    (quarantine / "node_0" / "sub" / "carried.pyc").write_text("moved already", encoding="utf-8")
    return source, quarantine


def test_the_residue_is_MOVED_into_the_quarantine_and_never_deleted(tmp_path):
    """The bytes are the property. A deletion is allowed to purge its own quarantine; it is not
    allowed to unlink something out from under a refusal that exists to protect it, so the residue
    ARRIVES at the mirrored path rather than vanishing on the way."""
    source, quarantine = _staged(tmp_path)
    (source / "node_0" / "sub" / "carried.pyc.140558024057008").write_text(
        "92 MB in the incident", encoding="utf-8")
    (source / "node_0" / "deep" / "final").mkdir(parents=True)
    (source / "node_0" / "deep" / "final" / ".tmpFJRFTG").write_text("checkpoint", encoding="utf-8")

    assert _absorb_quarantine_residue(source, quarantine) == []

    assert not source.exists(), "an absorbed source leaves nothing behind"
    assert (quarantine / "node_0" / "sub" / "carried.pyc.140558024057008").read_text(
        encoding="utf-8") == "92 MB in the incident"
    assert (quarantine / "node_0" / "deep" / "final" / ".tmpFJRFTG").read_text(
        encoding="utf-8") == "checkpoint", "a directory the move never created is created here"
    assert (quarantine / "node_0" / "sub" / "carried.pyc").read_text(
        encoding="utf-8") == "moved already", "what was already there is untouched"


def test_a_name_the_quarantine_already_holds_is_refused_rather_than_replaced(tmp_path):
    """THE discriminator, and the reason this is not "delete whatever is left".

    A destination that already exists is proof the entry is not residue of this move — the move would
    have carried it. That is what keeps a directory recreated at the run's name with real content of
    its own from becoming bytes this operation purges. It refuses, and it refuses WHOLE: nothing is
    absorbed and nothing is removed.
    """
    source, quarantine = _staged(tmp_path)
    (source / "node_0" / "sub" / "carried.pyc").write_text("a DIFFERENT file", encoding="utf-8")
    (source / "node_0" / "sub" / "carried.pyc.999").write_text("residue", encoding="utf-8")

    blocked = _absorb_quarantine_residue(source, quarantine)

    assert any("carried.pyc" in reason and "already holds this name" in reason
               for reason in blocked), blocked
    assert (source / "node_0" / "sub" / "carried.pyc").read_text(
        encoding="utf-8") == "a DIFFERENT file", "the source survives a refused absorption"
    assert (quarantine / "node_0" / "sub" / "carried.pyc").read_text(
        encoding="utf-8") == "moved already", "and the quarantine was not replaced"


def test_absorption_removes_directories_with_rmdir_and_never_with_rmtree(tmp_path, monkeypatch):
    """The safety property stated as the API it is allowed to use.

    `rmtree`/`unlink` would make "an entry appeared while we were walking" into a silent deletion.
    `rmdir` makes it an ENOTEMPTY refusal, which is the only answer this code is entitled to give.
    """
    source, quarantine = _staged(tmp_path)
    (source / "node_0" / "sub" / "residue.pyc.1").write_text("x", encoding="utf-8")
    monkeypatch.setattr(shutil, "rmtree", lambda *a, **k: pytest.fail("absorption used rmtree"))
    monkeypatch.setattr(deletion_service.os, "unlink",
                        lambda *a, **k: pytest.fail("absorption unlinked a file"))

    assert _absorb_quarantine_residue(source, quarantine) == []

    # ...and a real mid-walk arrival: a writer lands a file in a directory whose entries were already
    # carried. `rmdir` refuses it, the file stays, and the operation reports instead of deleting.
    source2 = tmp_path / "second"
    quarantine2 = tmp_path / f"{DELETE_QUARANTINE_PREFIX}abc.{OPERATION}2"
    (source2 / "d").mkdir(parents=True)
    quarantine2.mkdir()
    (source2 / "d" / "residue.pyc.1").write_text("carried", encoding="utf-8")
    real = deletion_service.durable_no_replace_rename

    def _then_a_writer_lands(*args, **kwargs):
        real(*args, **kwargs)
        (source2 / "d" / "arrived-mid-walk").write_text("nobody authorized this", encoding="utf-8")

    monkeypatch.setattr(deletion_service, "durable_no_replace_rename", _then_a_writer_lands)
    blocked = _absorb_quarantine_residue(source2, quarantine2)

    assert any("could not be removed" in reason for reason in blocked), blocked
    assert (source2 / "d" / "arrived-mid-walk").read_text(
        encoding="utf-8") == "nobody authorized this"


def test_a_link_or_a_device_is_named_rather_than_carried(tmp_path):
    """Neither is something a directory move left behind, and following one would move bytes from
    outside the fenced run into a container this operation is about to purge."""
    source, quarantine = _staged(tmp_path)
    outside = tmp_path / "somebody-elses-file"
    outside.write_text("not ours", encoding="utf-8")
    (source / "node_0" / "sub" / "link").symlink_to(outside)

    blocked = _absorb_quarantine_residue(source, quarantine)

    assert any("is a link" in reason for reason in blocked), blocked
    assert outside.read_text(encoding="utf-8") == "not ours"
    assert (source / "node_0" / "sub" / "link").is_symlink()


def test_a_reparse_point_at_the_runs_name_is_never_walked_THROUGH(tmp_path):
    """`_strict_existing_run` runs on a fresh preflight, not on this resume, and the shell rung
    answers a plain False for a link exactly as it does for real files. Absorbing through one would
    empty somebody else's directory into a container that is about to be purged."""
    source, quarantine = _staged(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "node_0").mkdir(parents=True)
    (elsewhere / "node_0" / "theirs.txt").write_text("not ours", encoding="utf-8")
    shutil.rmtree(source)
    source.symlink_to(elsewhere)

    blocked = _absorb_quarantine_residue(source, quarantine)

    assert blocked == ["the fenced run directory is not a canonical service directory"]
    assert (elsewhere / "node_0" / "theirs.txt").read_text(encoding="utf-8") == "not ours"


def test_an_engine_lock_is_left_where_the_shell_rung_can_prove_ownership(tmp_path):
    """The quarantine already has an `engine.lock` and its purge must ACQUIRE that name, so absorbing
    one would be moving a lock file on top of the proof the purge depends on. It is left for
    `_purge_recreated_writer_shell`, which checks nobody owns it before removing it."""
    source, quarantine = _staged(tmp_path)
    (quarantine / "engine.lock").write_text("", encoding="utf-8")
    (source / "engine.lock").write_text("", encoding="utf-8")
    (source / "node_0" / "sub" / "residue.pyc.1").write_text("x", encoding="utf-8")

    assert _absorb_quarantine_residue(source, quarantine) == [], "the lock is not a refusal"

    assert (source / "engine.lock").exists() and not (source / "node_0").exists()
    assert deletion_service._purge_recreated_writer_shell(source), (
        "what absorption leaves must be exactly what the shell rung can finish")
    assert not source.exists()


# ---------------------------------------------------------------- what a REAL dead end reports


def test_an_unabsorbable_residue_reports_retryable_false_and_names_the_paths(
        tmp_path, monkeypatch):
    """The other half of the fix. `retryable: true` is a promise that pressing again can change the
    answer; for this state nothing in the server ever touches the blocking paths, so the promise was
    the defect. The refusal stays — it is correct — and it now carries the evidence that makes the
    manual step checkable instead of a blind `rm -rf` of a fenced directory."""
    run_dir = _run(tmp_path)
    # A residue name the quarantine will already hold: not this move's leftover, by construction.
    _interrupted_geesefs_move(
        monkeypatch, plant=("node_0/vectorsearch/__pycache__/__init__.cpython-312.pyc",),
        body="a DIFFERENT file with the same name")
    client = TestClient(make_app(tmp_path))
    body = _identity(run_dir)
    client.post(f"/api/runs/{RUN}/deletions", json=body)

    wedged = client.post(f"/api/runs/{RUN}/deletions", json=body).json()

    assert wedged["code"] == "delete_quarantine_conflict"
    assert wedged["retryable"] is False, "no retry can move this, and it must stop saying otherwise"
    assert wedged["phase"] == "quarantining", (
        "the monotonic index does not move, and this is NOT quarantine_ambiguous")
    assert any("__init__.cpython-312.pyc" in entry for entry in wedged["blocking_entries"])
    assert "Retrying before that cannot change the answer" in wedged["remediation"]

    again = client.post(f"/api/runs/{RUN}/deletions", json=body).json()
    assert again["retryable"] is False and again["phase"] == "quarantining", (
        "the same answer twice is the evidence that `retryable: true` was a lie")
    assert run_dir.exists(), "and the 88 MB the refusal exists to protect is still there"


def test_the_absorbing_quarantine_ambiguous_phase_is_untouched():
    """The load-bearing constraint this fix is NOT allowed to relax: deletion's lattice is a
    monotonic index with ONE absorbing off-index state, and a residue refusal must not become a
    second spelling of it (nor make it resumable)."""
    from looplab.serve import deletion_transaction as dt

    receipt = {"phase": "quarantine_ambiguous"}
    with pytest.raises(dt.DeletionReceiptError, match="manual storage recovery"):
        dt._check_transition(receipt, {"phase": "quarantining"})
    with pytest.raises(dt.DeletionReceiptError, match="manual storage recovery"):
        dt._check_transition(receipt, {"phase": "succeeded"})
    source = deletion_service.__dict__["_wedged"].__doc__
    assert "quarantine_ambiguous" in source and "does not move" in source
