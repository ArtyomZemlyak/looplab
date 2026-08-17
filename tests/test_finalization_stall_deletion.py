"""A finalization interrupted by a DEAD engine must not read as a permanent refusal.

The shape under test is `runs/live-deps4-0804`'s, measured on this box 2026-08-17: `run_finished`
+ `report_generated` + EIGHT `finalize_step` rows (`begun … reflection`) in an open finalize scope,
no `finalization_finished`, engine gone since 2026-08-04. `DELETE`/`POST …/deletions` answered
`409 run_finalization_incomplete` with the sentence "Finish terminal projections before deleting
this run." and NO remediation — and the operator read that, correctly, as a state nothing on the
box could clear, because the other two rungs of `refuse_unless_quiescent` refuse states that DO
clear on their own (a command reaches a terminal; a launching engine takes the lock or stops
claiming it) while this one does not.

It is not unresolvable. `looplab finalize <run_dir>` completes the wrap-up already on disk. The
refusal now says so, and these tests hold BOTH halves of the line: the dead run becomes deletable
through the documented command, and a run whose engine is ALIVE or LAUNCHING still refuses.

Tier 1 of the guard-test ladder throughout — a real event log, a real server, the real endpoint,
the real `looplab finalize`, and the deletion's own answer observed. Nothing here asserts that a
call was made; every case reads what the run's record and the HTTP answer actually became.
"""
from __future__ import annotations

import fcntl
import os
import uuid
from pathlib import Path

import pytest

from looplab.core.run_deletion import run_deletion_snapshot_token
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.serve.run_commands import run_generation_token

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from looplab.serve.server import make_app  # noqa: E402

# The eight steps `runs/live-deps4-0804` actually recorded, in the order it recorded them. The
# engine died between `reflection` and the `llm_cost`/`complete` tail, so the scope stays OPEN.
_STALLED_STEPS = ("begun", "report_begun", "report", "budget", "diversity", "case",
                  "reflection_begun", "reflection")
_SCOPE = "finalize:afd1fbd4d8baf1bb7fb87c42465db1ac"


def _half_finalized_run(root: Path, name: str = "live-deps4") -> Path:
    """A run frozen exactly where the real one froze: finished, wrapped up part-way, unowned."""
    rd = root / name
    rd.mkdir(parents=True, exist_ok=True)
    store = EventStore(rd / "events.jsonl")
    store.append("run_started",
                 {"run_id": name, "task_id": "t", "goal": "g", "direction": "min"})
    store.append("finalize_step", {
        "scope": _SCOPE, "step": "begun",
        "finish_data": {"reason": "no_eligible_candidate"}, "finish_report_planned": True})
    store.append("finalize_step", {"scope": _SCOPE, "step": "report_begun"})
    store.append("report_generated", {"content": {"headline": "Run halted", "summary": ""}})
    store.append("run_finished", {"reason": "no_eligible_candidate",
                                  "finalization_required": True, "finalize_scope": _SCOPE})
    store.append("finalize_step", {"scope": _SCOPE, "step": "report", "outcome": "completed"})
    for step in _STALLED_STEPS[3:]:
        store.append("finalize_step", {"scope": _SCOPE, "step": step})
    (rd / "task.snapshot.json").write_text("{}", encoding="utf-8")
    (rd / "config.snapshot.json").write_text("{}", encoding="utf-8")

    # The fixture is only worth anything if it really is the stalled state.
    events = store.read_all()
    assert [e.data["step"] for e in events if e.type == "finalize_step"] == list(_STALLED_STEPS)
    assert not any(e.type == "finalization_finished" for e in events)
    state = fold(events)
    assert state.finished and state.finalization_pending()
    return rd


def _delete(client: TestClient, root: Path, name: str, token: str):
    events = EventStore((root / name) / "events.jsonl").read_all()
    body = {
        "operation_id": str(uuid.uuid4()),
        "expected_generation": run_deletion_snapshot_token(
            (root / name) / "events.jsonl", run_generation_token(events)),
        "expected_seq": events[-1].seq,
    }
    return client.post(f"/api/runs/{name}/deletions", json=body,
                       headers={"X-LoopLab-Token": token})


def _detail(response) -> dict:
    payload = response.json()
    detail = payload.get("detail", payload)
    return detail if isinstance(detail, dict) else {"message": str(detail)}


@pytest.fixture()
def served(tmp_path, monkeypatch):
    token = "test-owner-token"
    monkeypatch.setenv("LOOPLAB_UI_TOKEN", token)
    root = tmp_path / "runs"
    root.mkdir()
    return TestClient(make_app(root)), root, token


class _HeldEngineLock:
    """A REAL exclusive flock on `engine.lock` — what a live engine holds for its whole lifetime.

    `engine_proc._engine_liveness` probes that lock with `LOCK_EX | LOCK_NB` from this same
    process, and flock is per-OPEN-FILE-DESCRIPTION: a second descriptor on the same inode in the
    same process is refused exactly as another process's would be, which is what makes this a real
    liveness fixture and not a stub.
    """

    def __init__(self, rd: Path):
        self._path = rd / "engine.lock"
        self._fd = None

    def __enter__(self):
        self._fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:  # pragma: no cover - a busy lock here is a broken fixture
            os.close(self._fd)
            raise AssertionError(f"could not take the engine lock: {exc}") from exc
        return self

    def __exit__(self, *_exc):
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None


def _assert_lock_is_observable(rd: Path) -> None:
    """Skip rather than lie if the tmp filesystem cannot express the property under test."""
    from looplab.serve.engine_proc import _engine_liveness
    if _engine_liveness(rd) is not True:
        pytest.skip("flock is not observable on this filesystem; liveness cannot be driven here")


# --------------------------------------------------------------- the dead run becomes deletable


def test_stalled_finalization_refuses_with_a_remedy_that_names_the_command(served):
    client, root, token = served
    _half_finalized_run(root)

    response = _delete(client, root, "live-deps4", token)

    assert response.status_code == 409
    detail = _detail(response)
    assert detail["code"] == "run_finalization_incomplete"
    # The refusal must name a way forward. "Refresh and try again" is what the operator did for
    # thirteen days; the whole cost of this defect was a refusal that stated a condition and no
    # command, so the remediation naming `looplab finalize` IS the fix and is pinned as such.
    remediation = detail.get("remediation", "")
    assert "looplab finalize" in remediation
    assert "live-deps4" in remediation
    # And it must not be spelled as a transient wait — the state does not clear on its own.
    assert "does NOT clear by itself" in remediation


def test_documented_repair_makes_the_dead_half_finalized_run_deletable(served, monkeypatch):
    """The property, end to end: 409 -> run the command the refusal names -> the run deletes.

    The repair is the REAL `looplab finalize` command function, not a hand-written
    `finalization_finished` append: what is under test is that the documented remedy clears the
    exact state the guard refuses on, so writing the marker ourselves would test nothing.
    """
    from tests.test_finalization_recovery import _install_cli_engine
    import looplab.cli.run_cmds as cmds

    client, root, token = served
    rd = _half_finalized_run(root)

    refused = _delete(client, root, "live-deps4", token)
    assert refused.status_code == 409
    assert _detail(refused)["code"] == "run_finalization_incomplete"
    assert rd.is_dir()

    _install_cli_engine(monkeypatch, rd)
    cmds.finalize(rd)

    # The wrap-up completed on the run's OWN record: the scope closed and the finish was acked.
    events = EventStore(rd / "events.jsonl").read_all()
    assert any(e.type == "finalization_finished" for e in events)
    state = fold(events)
    assert not state.finalization_pending()
    # No new lifecycle boundary was invented to get there — still exactly one finish, no resume.
    assert len([e for e in events if e.type == "run_finished"]) == 1
    assert not any(e.type in ("resume", "run_abort", "run_reopened") for e in events)

    deleted = _delete(client, root, "live-deps4", token)

    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["status"] == "succeeded"
    assert not rd.exists()


# ------------------------------------------------------- and a run that can still be written to


def test_live_engine_mid_finalization_still_refuses(served):
    """An engine that is ALIVE and part-way through its wrap-up must still be refused."""
    client, root, token = served
    rd = _half_finalized_run(root)

    with _HeldEngineLock(rd):
        _assert_lock_is_observable(rd)
        response = _delete(client, root, "live-deps4", token)

    assert response.status_code == 409
    assert _detail(response)["code"] == "run_finalization_incomplete"
    assert rd.is_dir() and (rd / "events.jsonl").exists()


def test_live_engine_with_a_complete_finalization_still_refuses(served, monkeypatch):
    """The load-bearing negative: completing the wrap-up must not open a hole under a live engine.

    This is the case a change to the finalize rung could plausibly break — probes 1-3 all pass, and
    the ONLY thing left between the request and an irreversible delete is the lifecycle-lock
    liveness gate. It must refuse, and it must say so retryably.
    """
    from tests.test_finalization_recovery import _install_cli_engine
    import looplab.cli.run_cmds as cmds

    client, root, token = served
    rd = _half_finalized_run(root)
    _install_cli_engine(monkeypatch, rd)
    cmds.finalize(rd)
    assert not fold(EventStore(rd / "events.jsonl").read_all()).finalization_pending()

    with _HeldEngineLock(rd):
        _assert_lock_is_observable(rd)
        response = _delete(client, root, "live-deps4", token)

    assert response.status_code == 409
    detail = _detail(response)
    assert detail["code"] == "engine_running"
    assert detail["retryable"] is True
    assert rd.is_dir() and (rd / "events.jsonl").exists()


def test_launching_engine_still_refuses_and_stays_retryable(served):
    """The launch window keeps its own vocabulary, and `retryable` is part of that contract.

    `engine_launching` is the one rung whose `true` is a PROMISE that pressing again can move the
    operation, and the UI branches on it. A real PID-less spawn lease is installed — the crash-safe
    window `begin_external_spawn` exists for — rather than a patched probe.
    """
    client, root, token = served
    rd = _half_finalized_run(root)

    srv = client.app.state.looplab
    with srv.commands.sequence(rd):
        srv.commands.begin_external_spawn(rd, "test-launcher")
    assert srv.commands._recent_spawn_claim(rd) is True

    response = _delete(client, root, "live-deps4", token)

    assert response.status_code == 409
    detail = _detail(response)
    assert detail["code"] == "engine_launching"
    assert detail["retryable"] is True
    assert rd.is_dir() and (rd / "events.jsonl").exists()


def test_the_ladder_still_refuses_every_rung_it_refused_before(served):
    """The probe ORDER is the contract: a launching engine outranks an incomplete finalization."""
    client, root, token = served
    rd = _half_finalized_run(root)

    srv = client.app.state.looplab
    with srv.commands.sequence(rd):
        srv.commands.begin_external_spawn(rd, "test-launcher")

    # Both conditions hold at once; the answer must be the EARLIER rung's, not the finalize one.
    assert srv.commands._finalize_incomplete(rd) is True
    detail = _detail(_delete(client, root, "live-deps4", token))
    assert detail["code"] == "engine_launching"
