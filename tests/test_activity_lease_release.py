"""An activity lease whose release FAILED is not a live lease (review 2026-09-22, SRV1-04 / SC-03).

`RunCommandService.run_activity` leases a run generation for server-side work that can append while
a Replay or deletion is possible (a paid UI call, a chat append): it writes
`.commands/.activity_<token>.json` naming this process, and unlinks it when the work is done. The
unlink is best-effort — `OSError` is contained — and nothing ever retried it. A lease file that
outlived its context names THIS server's pid and creation identity, so every liveness reader saw an
exactly-alive owner: `_active_command_ids` listed it, delete/Replay/trace-clear refused with
"active command(s)", and the operator's escape hatch refused too
(`active_claim_owner_alive` — "never clear its live claim") — until the process exited. One transient
EIO on a FUSE mount was enough (the reviewer's `review/SRV1/activity_leak.py`, as these tests).

The fix is the one fact the file cannot carry: whether this process is still INSIDE the context.
The live tokens are held in memory (`_live_activity_tokens`), filled before the lease is published
and emptied when the context exits whether or not the unlink worked, so an own-process lease whose
token is no longer live is an orphan, retired on sight.
"""
from __future__ import annotations

import errno
import json
import os
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi import HTTPException  # noqa: E402

from looplab.events.eventstore import EventStore  # noqa: E402
from looplab.serve import run_commands as rc  # noqa: E402
from looplab.serve.server import make_app  # noqa: E402

PHRASE = rc.ACTIVE_CLAIM_HATCH.phrase


@pytest.fixture
def served(tmp_path):
    rd = tmp_path / "demo"
    rd.mkdir()
    EventStore(rd / "events.jsonl").append(
        "run_started", {"run_id": "demo", "task_id": "t", "goal": "g", "direction": "min"})
    srv = make_app(tmp_path).state.looplab
    return srv.commands, rd


@pytest.fixture
def failing_activity_unlink(monkeypatch):
    """Make unlinking `.activity_*` files fail while `state["on"]` — a transient EIO, e.g. FUSE."""
    real_unlink = Path.unlink
    state = {"on": False}

    def unlink(self, *args, **kwargs):
        if state["on"] and self.name.startswith(".activity_"):
            raise OSError(errno.EIO, "EIO (a transient FUSE failure)")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)
    return state


def _leak_one_lease(commands, rd, failing) -> Path:
    """Run one activity to completion while its release cannot unlink the lease file."""
    with commands.run_activity(rd, "ui_llm", generation=commands.run_generation(rd)):
        (lease,) = (rd / ".commands").glob(".activity_*.json")
        # The negative control: while the work runs, the lease IS live and blocks destructive work.
        assert commands._active_command_ids(rd) == [lease.stem]
        failing["on"] = True
    failing["on"] = False                      # storage is healthy again
    assert lease.exists(), "the premise: the release could not unlink the lease"
    assert json.loads(lease.read_text(encoding="utf-8"))["pid"] == os.getpid()
    return lease


def test_a_lease_whose_release_failed_no_longer_blocks_delete(served, failing_activity_unlink):
    commands, rd = served
    lease = _leak_one_lease(commands, rd, failing_activity_unlink)

    assert commands._active_command_ids(rd) == []
    assert not lease.exists(), "the orphan is retired on sight, not merely ignored"
    with commands.destructive_guard(rd, "delete run") as canonical:
        assert canonical == rd.resolve()


def test_the_escape_hatch_retires_an_orphaned_lease_without_the_phrase(
        served, failing_activity_unlink):
    """No operator judgement is needed: the process that wrote it says it is not inside it."""
    commands, rd = served
    lease = _leak_one_lease(commands, rd, failing_activity_unlink)

    assert commands.resolve_active_claims(rd) == {
        "ok": True, "resolved": True, "count": 1, "reason": "owners_definitively_gone"}
    assert not lease.exists()


def test_while_storage_still_fails_the_hatch_names_the_storage_not_a_live_owner(
        served, failing_activity_unlink):
    """If the orphan cannot be unlinked even now, the operator must be told THAT — the retirable
    claim that storage refuses to remove — and never "the exact process generation owning a
    command/activity claim is alive", which names the one thing the hatch must not override."""
    commands, rd = served
    lease = _leak_one_lease(commands, rd, failing_activity_unlink)
    row = json.loads(lease.read_text(encoding="utf-8"))
    row["created_at"] -= 3600                       # past the hatch's cold-start safety window
    lease.write_text(json.dumps(row), encoding="utf-8")
    failing_activity_unlink["on"] = True            # the storage fault is still there

    with pytest.raises(HTTPException) as refused:
        commands.resolve_active_claims(rd, confirmation=PHRASE)
    assert refused.value.status_code == 503
    assert refused.value.detail["code"] == "run_claim_unretirable"
    assert lease.exists()


def test_a_live_lease_still_cannot_be_forced(served):
    """The other direction, unchanged: inside the context the lease is exactly alive, and neither
    the census nor the operator's phrase may clear it."""
    commands, rd = served
    with commands.run_activity(rd, "chat_append", generation=commands.run_generation(rd)):
        (lease,) = (rd / ".commands").glob(".activity_*.json")
        assert commands._active_command_ids(rd) == [lease.stem]
        with pytest.raises(HTTPException) as refused:
            commands.resolve_active_claims(rd, confirmation=PHRASE)
        assert refused.value.status_code == 409
        assert refused.value.detail["code"] == "active_claim_owner_alive"
        with pytest.raises(HTTPException) as blocked:
            with commands.destructive_guard(rd, "delete run"):
                pass
        assert blocked.value.status_code == 409 and "active command" in str(blocked.value.detail)
        assert lease.exists()
    assert not lease.exists()


def test_a_lease_is_live_from_the_moment_its_file_exists(served, monkeypatch):
    """LIVE BEFORE PUBLISHED. A liveness scan that lands between the file appearing and its token
    being marked live would retire a lease whose work is about to run — and the work would then run
    unfenced against a Replay. Scan at exactly that instant: right after the lease is written."""
    commands, rd = served
    real_save = commands._save
    seen = []

    def save_then_scan(path, record):
        real_save(path, record)
        if path.name.startswith(".activity_"):
            seen.append(commands._active_command_ids(rd))

    monkeypatch.setattr(commands, "_save", save_then_scan)
    with commands.run_activity(rd, "ui_llm", generation=commands.run_generation(rd)):
        (lease,) = (rd / ".commands").glob(".activity_*.json")
    assert seen == [[lease.stem]]


def test_a_second_service_in_the_same_process_never_retires_a_running_lease(served, tmp_path):
    """Liveness is a property of the PROCESS the lease names, not of the service object that wrote
    it, so the live set is process-wide: another `RunCommandService` over the same root must see
    this one's running lease as running."""
    commands, rd = served
    other = make_app(tmp_path).state.looplab.commands
    assert other is not commands
    with commands.run_activity(rd, "ui_llm", generation=commands.run_generation(rd)):
        (lease,) = (rd / ".commands").glob(".activity_*.json")
        assert other._active_command_ids(rd) == [lease.stem]
        assert lease.exists()


def test_a_lease_this_process_did_not_write_keeps_the_generic_rule(served):
    """Only a lease PROVABLY this process's is judged by the live set. A lease carrying our pid but
    a different creation identity is another process generation's — the pre-existing identity rule
    decides it, not the absence of its token from our memory."""
    commands, rd = served
    directory = rd / ".commands"
    directory.mkdir(parents=True, exist_ok=True)
    foreign = directory / (".activity_" + "a" * 32 + ".json")
    foreign.write_text(json.dumps({
        "kind": "ui_llm", "pid": os.getpid(), "created_at": 0,
        "process_identity": "legacy-identity-from-another-generation"}), encoding="utf-8")
    # Pre-tag legacy vs source-tagged identity: incomparable, so neither alive nor gone.
    assert commands._active_command_ids(rd) == [foreign.stem]
    assert foreign.exists()
