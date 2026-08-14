"""The cascade over the wire: the opt-in flag, the preview it is consented to, and the ordering.

`test_memory_cascade.py` drives the attribution rules directly. This drives the two facts only the
route can be wrong about:

* the cascade is OPT-IN and part of the validated body, so a client that does not ask keeps every
  row — the historical behaviour, and the right default for a store whose whole purpose is to
  outlive the run;
* the purge runs only AFTER the run is durably gone. Purging first would mean a deletion that later
  refuses (an active engine, a changed generation, an unavailable lock) has already destroyed the
  evidence of a run the operator still has — the one outcome no retry can undo.
"""
from __future__ import annotations

import orjson
import pytest
from fastapi.testclient import TestClient

from looplab.events.eventstore import EventStore
from looplab.serve.server import make_app
from looplab.serve.run_commands import run_generation_token

GONE = "demo"
KEPT = "surviving-run"
OPERATION = "11111111-1111-4111-8111-111111111111"


def _run(tmp_path, run_id=GONE):
    run_dir = tmp_path / run_id
    run_dir.mkdir()
    (run_dir / "events.jsonl").write_text(
        '{"seq":0,"type":"run_started","data":{}}\n', encoding="utf-8")
    return run_dir


def _memory(monkeypatch, tmp_path):
    d = tmp_path / "xmem"
    d.mkdir()
    monkeypatch.setenv("LOOPLAB_MEMORY_DIR", str(d))
    (d / "lessons.jsonl").write_bytes(b"".join(orjson.dumps(row) + b"\n" for row in [
        {"run_id": GONE, "task_id": "t", "statement": "mine alone", "outcome": "won"},
        {"run_id": GONE, "task_id": "t", "statement": "folded", "outcome": "won",
         "evidence_count": 3},
        {"run_id": KEPT, "task_id": "t", "statement": "theirs", "outcome": "won"},
    ]))
    return d


def _identity(run_dir):
    events = EventStore(run_dir / "events.jsonl").read_all()
    return {"operation_id": OPERATION,
            "expected_generation": run_generation_token(events),
            "expected_seq": events[-1].seq if events else -1}


def _statements(memory):
    return [orjson.loads(line)["statement"]
            for line in (memory / "lessons.jsonl").read_bytes().splitlines() if line.strip()]


def test_the_preview_is_readable_before_anything_is_deleted(tmp_path, monkeypatch):
    """A number that only appears in a receipt is a number nobody consented to."""
    _run(tmp_path)
    memory = _memory(monkeypatch, tmp_path)
    client = TestClient(make_app(tmp_path))
    report = client.get(f"/api/runs/{GONE}/memory-attribution").json()
    assert report["available"] is True
    assert report["deletable"] == 1 and report["kept"] == 1
    assert _statements(memory) == ["mine alone", "folded", "theirs"], "a preview deletes nothing"


def test_deleting_without_the_flag_keeps_every_memory_row(tmp_path, monkeypatch):
    run_dir = _run(tmp_path)
    memory = _memory(monkeypatch, tmp_path)
    client = TestClient(make_app(tmp_path))
    response = client.post(f"/api/runs/{GONE}/deletions", json=_identity(run_dir))
    assert response.status_code == 200 and response.json()["status"] == "succeeded"
    assert not run_dir.exists()
    assert "memory" not in response.json(), "silence, not an empty receipt, when nothing was asked"
    assert _statements(memory) == ["mine alone", "folded", "theirs"]


def test_the_flag_purges_this_runs_own_rows_and_reports_what_it_kept(tmp_path, monkeypatch):
    run_dir = _run(tmp_path)
    memory = _memory(monkeypatch, tmp_path)
    client = TestClient(make_app(tmp_path))
    response = client.post(f"/api/runs/{GONE}/deletions",
                           json={**_identity(run_dir), "delete_memory": True})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "succeeded" and not run_dir.exists()
    assert body["memory"]["ok"] is True and body["memory"]["deleted"] == 1
    # The consolidated row carries a surviving run's support; the other run's row was never ours.
    assert _statements(memory) == ["folded", "theirs"]


def test_a_refused_deletion_never_reaches_the_memory_store(tmp_path, monkeypatch):
    """The ordering property, driven rather than asserted about the source: a stale identity is
    refused by the transaction, and the run's memory must be exactly as it was."""
    run_dir = _run(tmp_path)
    memory = _memory(monkeypatch, tmp_path)
    client = TestClient(make_app(tmp_path))
    stale = {**_identity(run_dir), "expected_seq": 99}
    response = client.post(f"/api/runs/{GONE}/deletions",
                           json={**stale, "delete_memory": True})
    assert response.status_code >= 400
    assert run_dir.exists(), "the run itself was not deleted"
    assert _statements(memory) == ["mine alone", "folded", "theirs"]


def test_the_flag_must_be_a_boolean_and_unknown_keys_are_still_refused(tmp_path):
    run_dir = _run(tmp_path)
    client = TestClient(make_app(tmp_path))
    identity = _identity(run_dir)
    assert client.post(f"/api/runs/{GONE}/deletions",
                       json={**identity, "delete_memory": "yes"}).status_code == 400
    assert client.post(f"/api/runs/{GONE}/deletions",
                       json={**identity, "delete_memories": True}).status_code == 400, (
        "a misspelled flag must refuse, not read as False and silently keep everything")
    assert run_dir.exists()


@pytest.mark.parametrize("configured", [False, True])
def test_no_memory_dir_is_reported_rather_than_failing_the_deletion(
        tmp_path, monkeypatch, configured):
    """The run is already gone by the time the purge runs. An unreachable memory store is a reason
    to tell the operator to try again, never a reason to report the deletion as failed."""
    run_dir = _run(tmp_path)
    monkeypatch.setenv("LOOPLAB_MEMORY_DIR", str(tmp_path / "xmem") if configured else "")
    client = TestClient(make_app(tmp_path))
    response = client.post(f"/api/runs/{GONE}/deletions",
                           json={**_identity(run_dir), "delete_memory": True})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "succeeded" and not run_dir.exists()
    assert body["memory"]["ok"] is False and body["memory"]["failures"]


def test_the_purge_refuses_a_store_this_server_does_not_manage(tmp_path, monkeypatch):
    """The body names the STORE, and that path reaches an irreversible rewrite.

    `memory_dir` has to be accepted from the caller — after the run is deleted its own record is
    gone and the receipt is the only place the value survives. But it arrived unchecked, while every
    other path input on this router goes through the deletion transaction's containment guard. With
    `run_uid` omitted the ownership test degrades to matching a row's bare `run_id`, so a body naming
    any directory on the box could delete another operator's legacy rows from a store this server was
    never configured with. A containment guard would not have caught it: an absolute path to somebody
    else's store contains no traversal, which is why the check is membership, not shape.

    Driven, not pinned: the foreign store is real, it is populated with a row the purge WOULD match,
    and the assertion is that the bytes are still there afterwards.
    """
    memory = _memory(monkeypatch, tmp_path)
    foreign = tmp_path / "someone-elses-memory"
    foreign.mkdir()
    (foreign / "lessons.jsonl").write_bytes(
        orjson.dumps({"run_id": GONE, "task_id": "t", "statement": "not yours", "outcome": "won"})
        + b"\n")
    before = (foreign / "lessons.jsonl").read_bytes()

    client = TestClient(make_app(tmp_path))
    response = client.post(f"/api/runs/{GONE}/memory-purge",
                           json={"run_uid": "", "memory_dir": str(foreign)})

    assert response.status_code == 400, response.text
    assert response.json()["detail"]["code"] == "memory_purge_store_unknown"
    assert (foreign / "lessons.jsonl").read_bytes() == before, "the foreign store was rewritten"
    # And the server's OWN store is still reachable through the same endpoint, so the guard refuses a
    # foreign path rather than the feature.
    assert client.post(f"/api/runs/{GONE}/memory-purge",
                       json={"run_uid": "", "memory_dir": str(memory)}).status_code == 200


def test_a_store_a_surviving_run_names_is_managed_even_when_it_is_not_the_global(
        tmp_path, monkeypatch):
    """The allow-list is not "the server's current global" — that would break the case the receipt
    echo exists for. `memory_dir` is a per-RUN setting, so a run launched with `--memory-dir` keeps
    its rows somewhere the global never names; a cross-run store is shared by construction, so any
    surviving run that cites it is enough to make it nameable."""
    _memory(monkeypatch, tmp_path)
    other = tmp_path / "project-store"
    other.mkdir()
    (other / "lessons.jsonl").write_bytes(
        orjson.dumps({"run_id": GONE, "task_id": "t", "statement": "mine alone", "outcome": "won"})
        + b"\n")
    kept = _run(tmp_path, KEPT)
    (kept / "config.snapshot.json").write_text(
        orjson.dumps({"memory_dir": str(other)}).decode(), encoding="utf-8")

    client = TestClient(make_app(tmp_path))
    response = client.post(f"/api/runs/{GONE}/memory-purge",
                           json={"run_uid": "", "memory_dir": str(other)})

    assert response.status_code == 200, response.text
    assert response.json()["deleted"] == 1
    assert response.json()["memory_dir"] == str(other), "the receipt names the store it ran against"


def test_a_retry_after_the_workspace_is_gone_finishes_the_cascade_uid_keyed(tmp_path, monkeypatch):
    """The identity sidecar, driven end to end.

    `run_uid` and the run's own `memory_dir` live ONLY inside the run directory, and the purge runs
    after that directory is gone. So a deletion that completes on a RETRY — the four documented
    "left the workspace but pending" phases, or a crash mid-transaction — used to lose the uid
    permanently and answer `identity: "unknown"`, while telling the operator to pass back fields no
    receipt and no 202 body ever carried. The UI's retry button then posted two empty strings
    forever. Here the first attempt parks the identity beside its receipt and the retry reads it.
    """
    run_dir = _run(tmp_path)
    (run_dir / "events.jsonl").write_text(
        '{"seq":0,"type":"run_started","data":{"run_uid":"uid-gone-1"}}\n', encoding="utf-8")
    memory = _memory(monkeypatch, tmp_path)
    identity = _identity(run_dir)
    client = TestClient(make_app(tmp_path))

    # Simulate the shape the sidecar exists for: the workspace is already gone when the request
    # that COMPLETES the operation reads the identity.
    first = client.post(f"/api/runs/{GONE}/deletions", json={**identity, "delete_memory": True})
    assert first.status_code == 200 and not run_dir.exists()
    assert first.json()["memory"]["ok"] is True

    # The retry of THIS EXACT operation re-runs the idempotent purge — and must still be uid-keyed
    # rather than degrading to the bare-name matching that takes another checkout's rows.
    again = client.post(f"/api/runs/{GONE}/deletions", json={**identity, "delete_memory": True})
    assert again.status_code == 200, again.text
    memory_receipt = again.json()["memory"]
    assert memory_receipt["identity"] == "run_uid", (
        "the retry must not report 'unknown' — nor fall back to matching the directory NAME")
    assert memory_receipt["run_uid"] == "uid-gone-1"
    assert memory_receipt["memory_dir"] == str(memory)
    assert memory_receipt["ok"] is True
    assert _statements(memory) == ["folded", "theirs"], "the purge stays idempotent"
