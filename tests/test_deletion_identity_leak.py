"""A REFUSED deletion does not leave its parked identity behind.

The cascade identity sidecar is written BEFORE the transaction can refuse, and it has to be: the two
facts the cross-run purge needs (`run_uid` and the run's OWN `memory_dir`) live only inside the run
directory, so they are read while it still exists, and parking them is exactly what makes a crash
BETWEEN that read and the deletion recoverable.

What it cost: a refused deletion parked one too. A wrong generation, a stale tail, a run another
operation already owns — each left a sidecar holding that run's identity with nothing that would ever
read it, and nothing removed it. Measured by `service_reaper`'s own audit, 10 of the 54 sidecars on
that deployment belong to two runs that STILL EXIST, i.e. to deletions that never happened; every
re-press of a refused deletion added another.

THE DISCRIMINATOR IS THE RECEIPT, not the exception class. A receipt is what makes an operation
resumable, so a sidecar with one beside it is live state a retry reads and must be kept, whatever
this attempt failed on.
"""
from __future__ import annotations

import orjson
import pytest
from fastapi.testclient import TestClient

from looplab.events.eventstore import EventStore
from looplab.serve.deletion_transaction import (
    deletion_identity_path, discard_deletion_identity, load_deletion_identity,
    save_deletion_identity)
from looplab.serve.run_commands import run_generation_token
from looplab.serve.server import make_app

RUN = "demo"
OPERATION = "11111111-1111-4111-8111-111111111111"


def _run(tmp_path, run_id=RUN):
    run_dir = tmp_path / run_id
    run_dir.mkdir()
    (run_dir / "events.jsonl").write_text(
        '{"seq":0,"type":"run_started","data":{"run_id":"demo","run_uid":"uid-1"}}\n',
        encoding="utf-8")
    return run_dir


def _identity(run_dir, **over):
    events = EventStore(run_dir / "events.jsonl").read_all()
    body = {"operation_id": OPERATION,
            "expected_generation": run_generation_token(events),
            "expected_seq": events[-1].seq if events else -1,
            "delete_memory": True}
    body.update(over)
    return body


def _sidecars(srv, run_dir):
    from looplab.serve.deletion_transaction import DELETE_IDENTITY_PREFIX
    return sorted(p.name for p in srv.root.resolve().iterdir()
                  if p.name.startswith(DELETE_IDENTITY_PREFIX))


def test_a_refused_deletion_parks_NOTHING(tmp_path, monkeypatch):
    """THE DEFECT. A stale `expected_seq` is refused, and until 2026-09-03 the sidecar it had
    already written stayed on disk holding a live run's identity.

    MUTATION: drop the `except` that discards it -> this leaks one file per refused press.
    """
    monkeypatch.setenv("LOOPLAB_MEMORY_DIR", str(tmp_path / "xmem"))
    run_dir = _run(tmp_path)
    app = make_app(tmp_path)
    srv = app.state.looplab
    client = TestClient(app)

    response = client.post(f"/api/runs/{RUN}/deletions",
                           json=_identity(run_dir, expected_seq=999))
    assert response.status_code >= 400, "this body must be refused"
    assert run_dir.exists(), "and nothing may have been deleted"
    assert _sidecars(srv, run_dir) == [], "a refused deletion left its identity parked"


def test_a_SUCCESSFUL_deletion_is_unchanged(tmp_path, monkeypatch):
    """The whole change is on the refusal path; the sidecar a real deletion parks is what the
    memory purge and every retry read, and the reaper collects it with its receipt."""
    monkeypatch.setenv("LOOPLAB_MEMORY_DIR", str(tmp_path / "xmem"))
    run_dir = _run(tmp_path)
    app = make_app(tmp_path)
    client = TestClient(app)

    response = client.post(f"/api/runs/{RUN}/deletions", json=_identity(run_dir))
    assert response.status_code == 200 and response.json()["status"] == "succeeded"
    assert not run_dir.exists()
    assert _sidecars(app.state.looplab, run_dir), "the successful path still parks its identity"


def test_the_discard_KEEPS_a_sidecar_whose_receipt_exists(tmp_path):
    """The rule that makes this safe. A receipt is what makes an operation resumable, so its
    sidecar is live state a retry will read.

    MUTATION: unlink unconditionally -> a retry of a genuinely in-flight deletion loses the uid
    permanently, which is the failure the sidecar was introduced to fix.
    """
    from looplab.serve.deletion_transaction import (
        deletion_receipt_path, prepare_deletion_receipt, save_deletion_receipt)

    run_dir = _run(tmp_path)
    srv = make_app(tmp_path).state.looplab
    save_deletion_identity(srv, run_dir, OPERATION,
                           {"run_id": RUN, "run_uid": "uid-1", "memory_dir": str(tmp_path)})
    save_deletion_receipt(
        deletion_receipt_path(srv, run_dir, OPERATION),
        prepare_deletion_receipt(srv, run_dir, operation_id=OPERATION,
                                 expected_generation="a" * 64, expected_seq=0))

    assert discard_deletion_identity(srv, run_dir, OPERATION) is False
    assert load_deletion_identity(srv, run_dir, OPERATION)["run_uid"] == "uid-1"


def test_the_discard_REMOVES_one_with_no_receipt(tmp_path):
    """The complement, driven directly: no receipt means no operation ever claimed it, so nothing
    can read it — a retry re-reads the run directory, which is still there."""
    run_dir = _run(tmp_path)
    srv = make_app(tmp_path).state.looplab
    save_deletion_identity(srv, run_dir, OPERATION,
                           {"run_id": RUN, "run_uid": "uid-1", "memory_dir": str(tmp_path)})
    assert deletion_identity_path(srv, run_dir, OPERATION).exists()

    assert discard_deletion_identity(srv, run_dir, OPERATION) is True
    assert not deletion_identity_path(srv, run_dir, OPERATION).exists()


def test_the_discard_never_raises(tmp_path):
    """`save_deletion_identity`'s rule inverted: failing to CLEAN UP must not turn a refusal the
    operator can act on into a 500 they cannot."""
    run_dir = _run(tmp_path)
    srv = make_app(tmp_path).state.looplab
    assert discard_deletion_identity(srv, run_dir, OPERATION) is False        # nothing there
    assert discard_deletion_identity(srv, run_dir, "not-a-uuid") is False     # invalid id


def test_a_run_that_is_already_gone_parks_nothing_to_leak(tmp_path, monkeypatch):
    """The other refusal shape: the identity read finds no run directory, so nothing was written
    and the discard has nothing to do — it must not report otherwise."""
    monkeypatch.setenv("LOOPLAB_MEMORY_DIR", str(tmp_path / "xmem"))
    app = make_app(tmp_path)
    srv = app.state.looplab
    client = TestClient(app)
    response = client.post("/api/runs/never-existed/deletions", json={
        "operation_id": OPERATION, "expected_generation": "a" * 64,
        "expected_seq": 0, "delete_memory": True})
    assert response.status_code >= 400
    assert _sidecars(srv, tmp_path) == []


# ------------------------------------------------------------------ the two routes no test spoke
# Review 2026-09-22, SRV2-12 / doc 50 SR-09: `GET .../deletions/{operation_id}` had no HTTP test,
# and the route inventory (`tests/test_route_coverage.py`) found the bodyless `DELETE` tombstone
# beside it.

def test_a_deletion_is_observable_by_its_operation_id_and_only_under_its_run(tmp_path):
    """The read a client whose deletion POST lost its response makes to learn how it ended —
    without repeating an irreversible request."""
    run_dir = _run(tmp_path)
    client = TestClient(make_app(tmp_path))
    deleted = client.post(f"/api/runs/{RUN}/deletions",
                          json=_identity(run_dir, delete_memory=False))
    assert deleted.status_code == 200 and not run_dir.exists()

    observed = client.get(f"/api/runs/{RUN}/deletions/{OPERATION}")
    assert observed.status_code == 200, observed.text
    for key in ("status", "operation_id", "run_id"):
        assert observed.json()[key] == deleted.json()[key], key
    assert observed.json()["status"] == "succeeded"

    unknown = "22222222-2222-4222-8222-222222222222"
    missing = client.get(f"/api/runs/{RUN}/deletions/{unknown}")
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "delete_operation_not_found"
    assert missing.json()["detail"]["operation_id"] == unknown
    malformed = client.get(f"/api/runs/{RUN}/deletions/not-a-uuid")
    assert malformed.status_code == 404
    assert malformed.json()["detail"]["code"] == "delete_operation_not_found"
    # A receipt answers only under the run it deleted.
    assert client.get(f"/api/runs/another-run/deletions/{OPERATION}").status_code == 404


def test_the_bodyless_delete_is_a_tombstone_that_deletes_nothing(tmp_path):
    """Deprecated (SRV2-09) and kept only to refuse: a bodyless request could otherwise destroy a
    replacement generation the caller never inspected."""
    run_dir = _run(tmp_path)
    refused = TestClient(make_app(tmp_path)).delete(f"/api/runs/{RUN}")
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "deletion_identity_required"
    assert (run_dir / "events.jsonl").is_file(), "a refusal must delete nothing"
