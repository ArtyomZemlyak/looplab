"""What the UI may OFFER for a stalled finalization, proved against the real command lifecycle.

`ui/src/runIndex.js` diagnoses `run_finished` + no `finalization_finished` + a dead engine as
`finalization-stalled`, and Dock's transport row and the empty-canvas card both offered
**"Reattach finalization"**. That control submits a durable `run_abort` command — and on a run whose
finalization is already pending the server does not START one, it ATTACHES to one already in the
log (`control_validation.py::_decide_run_abort` -> `attach`; `run_commands.py::submit` ->
`_pending_finalize_intent`). A run that finished NATURALLY never recorded a `run_abort`, so there is
nothing to attach to and the command is rejected `command_intent_missing` — on precisely the runs
whose card offered it. That was `runs/live-deps4-0804`, dead 2026-08-04 with 8 of 13 finalize steps;
`looplab finalize <run_dir>` cleared it, and the button never could.

These tests are tier 1 of the guard-test ladder: a real event log in the real stalled shape, the real
server, the real `/commands` endpoint, and the record it actually returns. They also pin the two
`/state` fields the browser decides on, so `ui/test/runIndex.test.js`'s fixture is this server's own
projection rather than a hand-written object that could drift away from it.

The NEGATIVE CONTROL is the other half and matters as much: a run that DOES carry a pending
`run_abort` must keep the behaviour it has today, so its command must still be admitted — and the
spawn recorder shows what admission means there, which is that a driver process is launched.
"""
from __future__ import annotations

import time
import uuid
from pathlib import Path

import pytest

from looplab.events.eventstore import EventStore
from looplab.events.replay import fold

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from looplab.serve.run_commands import RunCommandService  # noqa: E402
from looplab.serve.server import make_app  # noqa: E402
from tests.test_finalization_stall_deletion import _half_finalized_run  # noqa: E402

# The exact intent the Reattach control submits — `ui/src/Dock.jsx::TRANSPORT_INTENTS.finalize`
# and `ui/src/api.js::CONTROL.finalize`. Both spellings are pinned here because the whole defect is
# about what THIS payload does to THIS state.
_UI_FINALIZE_INTENT = {"type": "run_abort", "data": {"reason": "finalized"}}


class _DeadEngineWithSpawnRecorder:
    """No engine owns the run, and any spawn the command service attempts is recorded, not run."""

    def __init__(self):
        self.spawns = []

    def is_alive(self, _rd):
        return False

    def is_process_alive(self, _pid):
        return True

    def get_process_identity(self, _pid):
        return "child-generation"

    def spawn(self, args, **kwargs):
        self.spawns.append(list(args))
        return 4242


@pytest.fixture()
def served(tmp_path, monkeypatch):
    token = "test-owner-token"
    monkeypatch.setenv("LOOPLAB_UI_TOKEN", token)
    root = tmp_path / "runs"
    root.mkdir()
    app = make_app(root)
    srv = app.state.looplab
    driver = _DeadEngineWithSpawnRecorder()
    srv.commands = RunCommandService(
        srv, engine_alive=driver.is_alive, spawn_engine=driver.spawn,
        process_alive=driver.is_process_alive,
        process_identity=driver.get_process_identity,
        startup_timeout=0.05, command_timeout=0.2, poll_interval=0.01,
        max_observation_timeout=0.4)
    return TestClient(app), root, token, driver


def _state(client: TestClient, name: str, token: str) -> tuple[dict, str]:
    """The folded projection the browser holds as `live`, plus the generation it fences commands on."""
    response = client.get(f"/api/runs/{name}/state", headers={"X-LoopLab-Token": token})
    assert response.status_code == 200, response.text
    payload = response.json()
    return payload["state"], payload["generation"]


def _submit_reattach(client: TestClient, name: str, token: str, generation: str) -> dict:
    body = dict(_UI_FINALIZE_INTENT)
    body["expected_generation"] = generation
    response = client.post(
        f"/api/runs/{name}/commands", json=body,
        headers={"X-LoopLab-Token": token, "Idempotency-Key": str(uuid.uuid4())})
    assert response.status_code == 200, response.text
    return response.json()


def _resumable(rd: Path) -> Path:
    """A task snapshot the spawn ladder can actually launch from.

    `_half_finalized_run` writes an empty `{}` — enough for the DELETION guard it was built for, and
    not enough here: without a loadable task the spawn fails before `spawn_engine` is reached, so the
    negative control would report "no driver launched" for a reason that has nothing to do with the
    behaviour under test.
    """
    (rd / "task.snapshot.json").write_text(
        '{"kind":"quadratic","goal":"g","direction":"min"}', encoding="utf-8")
    return rd


def _pending_finalize(rd: Path) -> None:
    """Append the `run_abort` an operator FINALIZE leaves behind, the shape Reattach was built for."""
    EventStore(rd / "events.jsonl").append("run_abort", {"reason": "finalized"})
    assert fold(EventStore(rd / "events.jsonl").read_all()).stop_requested == "finalized"


# ------------------------------------------------------- what the browser is allowed to conclude


def test_state_publishes_the_two_fields_the_stalled_card_decides_on(served):
    """The provenance of `ui/test/runIndex.test.js`'s fixture: this is the projection it replays.

    `phase: finalizing` + `engine_running: false` is what makes `runIndex.js::runLifecycle` say
    `finalization-stalled`, and `stop_requested` is what says whether anything can be reattached.
    A naturally-finished run has the first pair and not the second — which is the whole defect.
    """
    client, root, token, _driver = served
    _half_finalized_run(root)

    state, _generation = _state(client, "live-deps4", token)

    assert state["finished"] is True
    assert state["phase"] == "finalizing"
    assert state["finalization_incomplete"] is True
    assert state["engine_running"] is False
    # No `run_abort` was ever appended, so there is no pending finalize intent to reattach to.
    assert not state.get("stop_requested")

    _pending_finalize(root / "live-deps4")
    reattachable, _reattachable_generation = _state(client, "live-deps4", token)
    assert reattachable["phase"] == "finalizing"
    assert reattachable["engine_running"] is False
    assert reattachable["stop_requested"] == "finalized"


# ------------------------------------------------------------------ the control itself, driven


def test_reattach_intent_is_rejected_on_a_naturally_finished_stalled_run(served):
    client, root, token, driver = served
    # Resumable on purpose: "nothing was launched" must be a fact about the DECISION, not a side
    # effect of a run the spawn ladder could not have launched anyway.
    _resumable(_half_finalized_run(root))
    _live, generation = _state(client, "live-deps4", token)

    record = _submit_reattach(client, "live-deps4", token, generation)

    assert record["status"] == "rejected"
    assert record["error"]["code"] == "command_intent_missing"
    assert record["error"]["retryable"] is False
    # Nothing was appended and nothing was launched: the control is inert, not merely slow.
    assert driver.spawns == []
    events = EventStore((root / "live-deps4") / "events.jsonl").read_all()
    assert not any(event.type == "run_abort" for event in events)
    assert fold(events).finalization_pending()


def test_a_pending_finalize_still_reattaches_and_that_path_launches_a_driver(served):
    """NEGATIVE CONTROL: the shape Reattach was written for keeps working, unchanged.

    It also records what "working" costs — the command service spawns a driver process, which is
    why the fix for the OTHER shape is a command the operator runs rather than a second button.
    """
    client, root, token, driver = served
    rd = _resumable(_half_finalized_run(root))
    _pending_finalize(rd)
    _live, generation = _state(client, "live-deps4", token)

    record = _submit_reattach(client, "live-deps4", token, generation)

    assert record["status"] != "rejected", record.get("error")
    assert (record.get("error") or {}).get("code") != "command_intent_missing"
    # Admission happens on the POST; the DRIVER is launched by the command worker, so observe the
    # record the way the browser does instead of asserting on the first response.
    deadline = time.time() + 5.0
    while not driver.spawns and time.time() < deadline:
        observed = client.get(f"/api/runs/live-deps4/commands/{record['id']}",
                              headers={"X-LoopLab-Token": token})
        assert observed.status_code == 200, observed.text
        assert (observed.json().get("error") or {}).get("code") != "command_intent_missing"
        time.sleep(0.02)
    assert driver.spawns, "an accepted reattach drives the wrap-up by launching an engine"
    assert driver.spawns[0][0] == "resume"
