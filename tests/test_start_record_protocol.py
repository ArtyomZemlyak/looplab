"""The durable start record's protocol, driven WITHOUT an ASGI app (doc 25 SR-01, variant 5).

`_reconcile_start` and `_inspect_keyed_start` were `build_router` closures, so every branch of a
crash-window state machine — an observed-dead claim retired, a pre-Popen namespace released, an
idempotency key re-used for a different proposal, a `run_started` that names another run — was
reachable only by building the whole server and driving HTTP. That is the same reason
`serve/trace_clear.py` was extracted for variant (4), and this file is the instrument the move buys:
a stub `srv` with three methods, real files on disk, no `make_app`, no `TestClient`.

The spec's own truth table is here too. `StartRecordSpec` states what used to be brace literals
repeated across seven closures, and the two invariants it refuses (a started status that is not
established; a retryable status that is) are the shapes that would advertise a second launch for a
startup that already owns the run name.
"""
from __future__ import annotations

import json

import pytest
from fastapi import HTTPException

from looplab.serve import start_record as sr
from looplab.serve.paid_ledger import FAIL_CLOSED, FIRST_TERMINAL_WINS


class _Commands:
    """Only what the protocol actually asks of the command service."""

    def __init__(self, evidence="absent", record=None):
        self.evidence = evidence
        self.record = record
        self.saved: list[dict] = []
        self.observed: list[str] = []

    def observe_external_spawn(self, _rd, owner):
        self.observed.append(owner)
        return self.evidence

    def load_start_record(self, _rd):
        return dict(self.record) if self.record is not None else None

    def save_start_record(self, _rd, record):
        self.saved.append(dict(record))


class _Srv:
    def __init__(self, root, commands):
        self.root = root
        self.commands = commands


def _run_dir(tmp_path, name="demo"):
    rd = tmp_path / name
    rd.mkdir(parents=True)
    return rd


def _meta(rd, start_id, task_file=None):
    payload = {"task_file": str(task_file or rd / "task.input.json")}
    if start_id:
        payload["start_id"] = start_id
    (rd / "ui_meta.json").write_text(json.dumps(payload), encoding="utf-8")


def _log(rd, *rows):
    (rd / "events.jsonl").write_bytes(b"".join(json.dumps(r).encode() + b"\n" for r in rows))


def _row(seq, etype, data):
    return {"v": 1, "seq": seq, "ts": 1.0, "type": etype, "data": data}


@pytest.fixture(autouse=True)
def _no_engine(monkeypatch):
    """Liveness is the router's fail-closed verdict, not this protocol's; pin it per test."""
    monkeypatch.setattr(sr, "_engine_liveness", lambda _rd: False)
    monkeypatch.setattr(sr, "_engine_alive", lambda _rd: False)


# ---------------------------------------------------------------- the spec itself


def test_the_shipped_spec_is_the_record_stores_fail_closed_one():
    assert sr.START_RECORD.conflict_policy == FAIL_CLOSED
    assert sr.START_RECORD.fails_closed is True
    assert sr.START_RECORD.request_digest_field == "request_digest"


def test_a_spec_with_an_unknown_policy_is_refused():
    with pytest.raises(ValueError, match="conflict policy"):
        sr.StartRecordSpec(
            key_digest_field="k", request_digest_field=None, conflict_policy="whatever",
            spawn_crossed_phases=frozenset(), pre_spawn_phases=frozenset(),
            established_statuses=frozenset(), started_statuses=frozenset(),
            retryable_statuses=frozenset())


def test_a_started_status_that_is_not_established_is_refused():
    """It would publish `started: true` beside `ok: false` — a client cannot act on that pair."""
    with pytest.raises(ValueError, match="subset"):
        sr.StartRecordSpec(
            key_digest_field="k", request_digest_field=None, conflict_policy=FIRST_TERMINAL_WINS,
            spawn_crossed_phases=frozenset(), pre_spawn_phases=frozenset(),
            established_statuses=frozenset({"accepted"}),
            started_statuses=frozenset({"executing"}), retryable_statuses=frozenset())


def test_an_established_status_may_not_also_be_retryable():
    """The double launch this whole protocol exists to prevent, expressed as a spec."""
    with pytest.raises(ValueError, match="never retryable"):
        sr.StartRecordSpec(
            key_digest_field="k", request_digest_field=None, conflict_policy=FAIL_CLOSED,
            spawn_crossed_phases=frozenset(), pre_spawn_phases=frozenset(),
            established_statuses=frozenset({"accepted"}),
            started_statuses=frozenset(), retryable_statuses=frozenset({"accepted"}))


# ---------------------------------------------------------------- the public projection


@pytest.mark.parametrize("status,ok,started,can_retry", [
    ("preparing", False, False, False),
    ("accepted", True, False, False),      # Popen returned; the CHILD is not observed yet
    ("executing", True, True, False),
    ("succeeded", True, True, False),
    ("not_started", False, False, True),
    ("failed", False, False, True),
    ("uncertain", False, False, False),
])
def test_the_public_projection_is_the_specs_three_status_families(status, ok, started, can_retry):
    public = sr.start_public({"status": status, "run_id": "demo", "id": "start_x"})
    assert (public["ok"], public["started"], public["can_retry"]) == (ok, started, can_retry)


def test_a_possibly_escaped_paid_effect_is_never_offered_a_retry():
    """`failed` is retryable; `failed` with an unresolved paid effect is not."""
    assert sr.start_public({"status": "failed"})["can_retry"] is True
    assert sr.start_public({"status": "failed", "paid_effect_unknown": True})["can_retry"] is False
    assert sr.start_public(
        {"status": "failed", "namespace_released": False})["can_retry"] is False


# ---------------------------------------------------------------- reconciliation


def test_a_pre_spawn_record_with_no_claim_resolves_to_not_started(tmp_path):
    rd = _run_dir(tmp_path)
    commands = _Commands(evidence="absent")
    srv = _Srv(tmp_path, commands)

    updated, public = sr.reconcile_start(
        srv, rd, {"id": "start_a", "status": "preparing", "phase": "reserved"})

    assert updated["status"] == "not_started" and updated["paid_effect_unknown"] is False
    assert public["can_retry"] is True
    assert commands.saved, "a transition must be published to the record store"


def test_a_pre_spawn_record_with_an_unreadable_claim_stays_uncertain(tmp_path):
    """Fail closed: `uncertain` evidence means a child MAY exist, and a retry would buy a second."""
    rd = _run_dir(tmp_path)
    srv = _Srv(tmp_path, _Commands(evidence="uncertain"))

    updated, public = sr.reconcile_start(
        srv, rd, {"id": "start_a", "status": "preparing", "phase": "materialized"})

    assert updated["status"] == "uncertain" and updated["error_code"] == "start_uncertain"
    assert public["can_retry"] is False and public["paid_effect_unknown"] is True


def test_a_spawn_crossed_record_that_died_is_failed_after_spawn_not_retryable(tmp_path):
    """Popen may already have crossed the provider boundary before the child died."""
    rd = _run_dir(tmp_path)
    _meta(rd, "start_a")
    srv = _Srv(tmp_path, _Commands(evidence="dead_or_cleared"))

    updated, public = sr.reconcile_start(
        srv, rd, {"id": "start_a", "status": "executing", "phase": "popen_pending"})

    assert updated["phase"] == "failed_after_spawn" and updated["paid_effect_unknown"] is True
    assert public["can_retry"] is False


def test_a_correlated_run_started_event_is_what_makes_a_startup_succeeded(tmp_path):
    rd = _run_dir(tmp_path)
    _meta(rd, "start_a")
    _log(rd, _row(0, "run_started", {"run_id": "demo"}))
    srv = _Srv(tmp_path, _Commands(evidence="absent"))

    updated, _public = sr.reconcile_start(
        srv, rd, {"id": "start_a", "status": "executing", "phase": "popen_returned"})

    assert updated["status"] == "succeeded" and updated["phase"] == "event_observed"


def test_an_engine_lock_without_the_start_id_correlation_is_not_this_startup(tmp_path):
    """`ui_meta.start_id` is the durable correlation between this sidecar and this directory; a
    lock without it may belong to a manually replaced incarnation."""
    rd = _run_dir(tmp_path)
    _meta(rd, "someone_else")
    _log(rd, _row(0, "run_started", {"run_id": "demo"}))
    srv = _Srv(tmp_path, _Commands(evidence="live"))

    updated, _public = sr.reconcile_start(
        srv, rd, {"id": "start_a", "status": "executing", "phase": "popen_returned"})

    assert updated["status"] == "uncertain" and updated["error_code"] == "start_uncertain"


def test_reconciliation_is_observational_and_a_repeat_poll_publishes_nothing(tmp_path):
    """Stable polling must not mint a new `updated_at` for the same evidence; a client watching the
    timestamp would read every GET as a state change."""
    rd = _run_dir(tmp_path)
    srv = _Srv(tmp_path, _Commands(evidence="absent"))
    settled, _ = sr.reconcile_start(
        srv, rd, {"id": "start_a", "status": "preparing", "phase": "reserved"})
    srv.commands.saved.clear()

    again, _ = sr.reconcile_start(srv, rd, settled)

    assert again == settled
    assert srv.commands.saved == [], "an unchanged record must not be re-saved"


def test_a_failed_pre_spawn_record_releases_only_its_own_pristine_namespace(tmp_path):
    """The recorded `namespace_released: False` is the durable fact that lets a LATER reconciliation
    finish a cleanup this process died during."""
    rd = _run_dir(tmp_path)
    _meta(rd, "start_a")
    (rd / "task.input.json").write_text("{}", encoding="utf-8")
    srv = _Srv(tmp_path, _Commands(evidence="absent"))

    updated, _public = sr.reconcile_start(srv, rd, {
        "id": "start_a", "status": "failed", "phase": "failed_before_spawn",
        "paid_effect_unknown": False, "namespace_released": False})

    assert updated["namespace_released"] is True
    assert not rd.exists(), "the reserved run name is given back"


def test_a_namespace_holding_anything_unexpected_is_left_alone(tmp_path):
    """Fail closed: an entry this startup did not write may be somebody's run."""
    rd = _run_dir(tmp_path)
    _meta(rd, "start_a")
    (rd / "events.jsonl").write_bytes(b"")
    srv = _Srv(tmp_path, _Commands(evidence="absent"))

    released = sr.release_unspawned_start_namespace(
        srv, rd, start_id="start_a", task_file=rd / "task.input.json")

    assert released is False and rd.exists()


# ---------------------------------------------------------------- keyed replay


def test_no_record_is_not_a_conflict(tmp_path):
    srv = _Srv(tmp_path, _Commands(record=None))
    assert sr.inspect_keyed_start(srv, _run_dir(tmp_path), "a" * 64, "b" * 64) == (None, None, False)


def test_the_same_key_for_a_DIFFERENT_launch_request_is_refused(tmp_path):
    """The whole point of binding `request_digest`: a key reused for an edited proposal must not
    replay the earlier startup's answer, because the operator asked for something else."""
    rd = _run_dir(tmp_path)
    srv = _Srv(tmp_path, _Commands(evidence="absent", record={
        "id": "start_a", "status": "preparing", "phase": "reserved",
        "idempotency_key_digest": "k" * 64, "request_digest": "r" * 64}))

    with pytest.raises(HTTPException) as caught:
        sr.inspect_keyed_start(srv, rd, "k" * 64, "OTHER" + "r" * 59)

    assert caught.value.status_code == 409
    assert caught.value.detail["code"] == "idempotency_key_reused"
    assert srv.commands.saved == [], "a refused replay must not transition the record"


def test_another_key_on_the_same_run_name_reconciles_but_is_not_the_same_startup(tmp_path):
    rd = _run_dir(tmp_path)
    srv = _Srv(tmp_path, _Commands(evidence="absent", record={
        "id": "start_a", "status": "preparing", "phase": "reserved",
        "idempotency_key_digest": "k" * 64, "request_digest": "r" * 64}))

    _record, public, same_key = sr.inspect_keyed_start(srv, rd, "z" * 64, "r" * 64)

    assert same_key is False and public["status"] == "not_started"


# ---------------------------------------------------------------- the refusals


def test_a_run_name_owned_by_another_startup_is_a_run_id_conflict():
    with pytest.raises(HTTPException) as caught:
        sr.raise_existing_start({"status": "not_started", "start_id": "s"}, same_key=False)
    assert caught.value.detail["code"] == "run_id_conflict"


def test_an_uncertain_startup_refuses_before_it_offers_a_second_launch():
    with pytest.raises(HTTPException) as caught:
        sr.raise_existing_start({"status": "uncertain", "start_id": "s"}, same_key=True)
    assert caught.value.detail["code"] == "start_uncertain"

    with pytest.raises(HTTPException) as caught:
        sr.raise_existing_start(
            {"status": "failed", "paid_effect_unknown": True, "start_id": "s"}, same_key=True)
    assert caught.value.detail["code"] == "start_uncertain"


def test_an_established_startup_is_simply_the_answer():
    for status in sorted(sr.START_RECORD.established_statuses):
        assert sr.raise_existing_start({"status": status}, same_key=True) is None


def test_a_startup_that_established_nothing_says_so():
    with pytest.raises(HTTPException) as caught:
        sr.raise_existing_start({"status": "failed", "start_id": "s"}, same_key=True)
    assert caught.value.detail["code"] == "start_not_completed"


# ---------------------------------------------------------------- the identity walk


def test_the_identity_walk_accepts_both_engine_layouts(tmp_path):
    older = _run_dir(tmp_path, "older")
    _log(older, _row(0, "run_started", {"run_id": "older"}))
    assert sr.has_first_run_started(older) is True

    current = _run_dir(tmp_path, "current")
    _log(current, _row(0, "setup_started", {}), _row(1, "setup_step", {}),
         _row(2, "run_started", {"run_id": "current"}))
    assert sr.has_first_run_started(current) is True


@pytest.mark.parametrize("rows,why", [
    ([_row(0, "run_started", {"run_id": "somebody_else"})], "a run id that names another directory"),
    ([_row(0, "node_created", {}), _row(1, "run_started", {"run_id": "demo"})],
     "an unrelated pre-identity event"),
    ([_row(0, "setup_started", {}), _row(2, "run_started", {"run_id": "demo"})], "a sequence gap"),
    ([{"type": "run_started", "data": {"run_id": "demo"}}], "a merely parseable envelope"),
])
def test_the_identity_walk_fails_closed(tmp_path, rows, why):
    """A parseable `{"type": "run_started"}` is not process evidence."""
    rd = _run_dir(tmp_path)
    _log(rd, *rows)
    assert sr.has_first_run_started(rd) is False, why


def test_a_torn_first_line_is_not_process_evidence(tmp_path):
    rd = _run_dir(tmp_path)
    (rd / "events.jsonl").write_bytes(b'{"v":1,"seq":0,"ts":1.0,"type":"run_star')
    assert sr.has_first_run_started(rd) is False
