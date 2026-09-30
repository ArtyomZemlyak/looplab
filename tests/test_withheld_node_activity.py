"""A WITHHELD EVALUATION READS `queued`, NOT `evaluating` (doc 69 69.12b).

A pause that withholds a lifecycle's evaluation work (a passed canary whose full eval it refused to
start, a failed attempt it stopped repairing, ADMIT on a halted run with its receipt already
durable) returns with no terminal and writes one DIAGNOSTIC `eval_attempt_withheld` row
(doc 69 69.12a). The public activity is a projection of the FOLD, which never sees that row, so it
kept calling the node `evaluating` — "Training / evaluating" on a run that was not running it —
until the re-dispatch's terminal. Folding the row would put a position-sensitive row inside the Card
elections' fences; the projection reads it off the log instead
(`events/eval_occupancy.py::withheld_lifecycles`, the occupancy pairing's own rows and rules), and
the next launch row makes the node `evaluating` again.
"""
from __future__ import annotations

import pytest

from looplab.events.eval_occupancy import withheld_lifecycles
from looplab.events.eventstore import EventStore

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from looplab.serve.server import make_app  # noqa: E402


def _row(kind, node=0, generation=0):
    return {"type": kind, "ts": 1.0, "data": {"node_id": node, "generation": generation}}


# ----------------------------------------------------------------------------------- the rule
def test_a_withheld_lifecycle_is_the_one_whose_latest_launch_row_is_the_withhold():
    started = _row("node_eval_started")
    withheld = _row("eval_attempt_withheld")
    assert withheld_lifecycles([started, withheld]) == {(0, 0)}
    for relaunch in ("eval_canary_started", "eval_invocation_claimed", "node_eval_started"):
        assert withheld_lifecycles([started, withheld, _row(relaunch)]) == set(), relaunch
    for terminal in ("node_evaluated", "node_failed"):
        assert withheld_lifecycles([started, withheld, _row(terminal)]) == set(), terminal
    # The FIRST terminal ends the lifecycle: a withheld row after it names nothing.
    assert withheld_lifecycles([started, _row("node_failed"), withheld]) == set()
    # A relaunch row counts only once the lifecycle has started — the pairing's own rule — so one
    # the log holds before any start takes nothing up. MUTATION: drop the `started` guard.
    assert withheld_lifecycles([_row("eval_canary_started"), started, withheld]) == {(0, 0)}
    assert withheld_lifecycles([withheld, _row("eval_canary_started")]) == {(0, 0)}
    # Keyed by LIFECYCLE: a reset's new generation is not the withheld one, nor is a sibling.
    assert withheld_lifecycles([started, withheld, _row("node_eval_started", generation=1)]) == {
        (0, 0)}
    assert withheld_lifecycles([started, withheld, _row("node_eval_started", node=1),
                                _row("eval_attempt_withheld", node=1),
                                _row("eval_canary_started", node=1)]) == {(0, 0)}
    assert withheld_lifecycles([]) == set()


# ------------------------------------------------------------------------------ the server
def _run(tmp_path):
    rd = tmp_path / "withheld"
    rd.mkdir()
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": "withheld", "task_id": "t", "goal": "g",
                                 "direction": "min"})
    store.append("node_created", {
        "node_id": 0, "generation": 0, "parent_ids": [], "operator": "draft",
        "idea": {"operator": "draft", "params": {}, "rationale": ""},
        "eval_start_boundary": True})
    store.append("node_eval_started", {"node_id": 0, "generation": 0})
    return rd, store


def _activity(client, *, detail=False, seq=None, rd=None):
    if detail:
        url = "/api/runs/withheld/nodes/0"
        if seq is not None:
            from tests.factories import log_run_generation
            url += f"?seq={seq}&expected_generation={log_run_generation(rd)}"
        return client.get(url).json()["activity"]
    return client.get("/api/runs/withheld/state").json()["state"]["nodes"]["0"]["activity"]


def test_both_public_surfaces_say_the_withheld_node_waits_and_a_relaunch_says_it_runs(tmp_path):
    """MUTATIONS: drop the withheld set from either caller, or the branch from the projection ->
    that surface reads `evaluating` over a pause that refused to run the evaluation."""
    rd, store = _run(tmp_path)
    client = TestClient(make_app(tmp_path))
    assert _activity(client)["status"] == "evaluating"
    started_seq = store.read_all()[-1].seq
    store.append("pause", {"reason": "operator"})
    store.append("eval_attempt_withheld", {"node_id": 0, "generation": 0, "attempt": 0,
                                           "at": "after_canary", "reason": "paused",
                                           "eval_seconds": 1.5})
    waiting = {"schema": 1, "status": "queued", "generation": 0,
               "evidence": "eval_attempt_withheld"}
    assert _activity(client) == waiting
    assert _activity(client, detail=True) == waiting
    # A historical view BEFORE the withhold still shows what was true then, on both surfaces.
    # MUTATION: read the withheld set off the whole log -> the past reads `queued`.
    assert _activity(client, detail=True, seq=started_seq, rd=rd)["status"] == "evaluating"
    past = client.get(f"/api/runs/withheld/state?seq={started_seq}").json()
    assert past["state"]["nodes"]["0"]["activity"]["status"] == "evaluating"
    store.append("resume", {})
    assert _activity(client) == waiting, "resumed, but nothing has relaunched it yet"
    store.append("eval_invocation_claimed", {"node_id": 0, "generation": 0})
    assert _activity(client)["status"] == "evaluating"
    assert _activity(client, detail=True)["status"] == "evaluating"
    store.append("node_evaluated", {"node_id": 0, "generation": 0, "metric": 0.5})
    assert _activity(client)["status"] == "evaluated"


def test_a_real_paused_canary_leaves_the_node_waiting_on_the_state(tmp_path):
    """End to end: the engine withholds the full eval after a passing canary on a paused run, and
    the owner's `/state` says the node waits for its evaluation — not that it trains."""
    from test_a_withheld_attempt_is_charged import _receipted_seed, _slow_script
    from test_eval_canary import _PAUSE, _Dev, _after_canary, _engine, _evaluate, _terminals

    rd = tmp_path / "withheld"
    code = _slow_script(tmp_path / "ledger.txt", canary_sleep=0.2)
    eng = _engine(rd, _Dev(code))
    _receipted_seed(eng, code)
    _after_canary(eng, _PAUSE)
    assert _terminals(_evaluate(eng)) == []
    client = TestClient(make_app(tmp_path))
    assert _activity(client) == {"schema": 1, "status": "queued", "generation": 0,
                                 "evidence": "eval_attempt_withheld"}


def test_the_withhold_is_the_lifecycle_s_not_the_node_s(tmp_path):
    """A reset's new generation, admitted, is evaluating whatever its predecessor's withhold said.
    MUTATION: key the projection on the node id alone -> the new lifecycle reads `queued`."""
    rd, store = _run(tmp_path)
    store.append("eval_attempt_withheld", {"node_id": 0, "generation": 0, "attempt": 0,
                                           "at": "decide_repair", "reason": "paused",
                                           "eval_seconds": 2.0})
    store.append("node_reset", {"node_id": 0, "generation": 0, "from_stage": "eval"})
    store.append("resume_served", {"engine_owner_boundary": True})
    store.append("node_eval_started", {"node_id": 0, "generation": 1})
    client = TestClient(make_app(tmp_path))
    now = _activity(client)
    assert (now["status"], now["generation"]) == ("evaluating", 1), now


def test_after_an_owner_boundary_the_node_waits_for_admission_not_for_its_withhold(tmp_path):
    """The next owner has not admitted the lifecycle yet, and THAT is what the evidence names; the
    withhold is history. MUTATION: ask the withheld set before the admission check -> evidence
    `eval_attempt_withheld` for a lifecycle no current owner holds (critic 2026-09-30, N4)."""
    rd, store = _run(tmp_path)
    store.append("eval_attempt_withheld", {"node_id": 0, "generation": 0, "attempt": 0,
                                           "at": "after_canary", "reason": "paused",
                                           "eval_seconds": 1.0})
    store.append("resume_served", {"engine_owner_boundary": True})
    client = TestClient(make_app(tmp_path))
    assert _activity(client) == {"schema": 1, "status": "queued", "generation": 0,
                                 "evidence": "node_created_boundary"}


def test_the_node_detail_reads_the_log_once_for_the_withheld_scan(tmp_path, monkeypatch):
    """The Inspector polls this route every 4 s while a node evaluates, and the scan re-parsed the
    whole log on each poll beside the fold that had just read it (critic 2026-09-30: 654 -> 1207 ms
    on 30,900 events). The live path reads through the run's incremental store and a historical
    one reuses its prefix, so the scan adds no `events()` read to a detail that needs no scan.
    MUTATION: scan `srv.events(rd, seq)` again -> one read more than the baseline, both paths."""
    from looplab.serve import appstate
    from tests.factories import log_run_generation

    rd, store = _run(tmp_path)
    store.append("node_created", {
        "node_id": 1, "generation": 0, "parent_ids": [], "operator": "draft",
        "idea": {"operator": "draft", "params": {}, "rationale": ""}})
    store.append("node_evaluated", {"node_id": 1, "generation": 0, "metric": 0.3})
    store.append("eval_attempt_withheld", {"node_id": 0, "generation": 0, "attempt": 0,
                                           "at": "after_canary", "reason": "paused",
                                           "eval_seconds": 1.0})
    reads: list = []
    real = appstate.AppState.events

    def _counting(self, rd_, upto_seq=None):
        reads.append(upto_seq)
        return real(self, rd_, upto_seq)

    monkeypatch.setattr(appstate.AppState, "events", _counting)
    client = TestClient(make_app(tmp_path))
    seq = store.read_all()[-1].seq
    historical = f"?seq={seq}&expected_generation={log_run_generation(rd)}"
    for suffix in ("", historical):
        reads.clear()
        client.get(f"/api/runs/withheld/nodes/1{suffix}").raise_for_status()
        baseline = len(reads)
        reads.clear()
        body = client.get(f"/api/runs/withheld/nodes/0{suffix}").json()
        assert body["activity"]["evidence"] == "eval_attempt_withheld", suffix
        assert len(reads) == baseline, (suffix or "live", reads, baseline)


def test_stop_wait_does_not_call_a_withheld_evaluation_running(tmp_path):
    """`looplab stop --wait` printed "waiting for the engine to finish 1 evaluation(s) already
    running (node 0)" while the public activity said the node waits (critic 2026-09-30). The tail
    fold carries the withheld set off the same events it folds; a relaunch makes it run again.
    MUTATION: drop the subtraction -> [0] while withheld."""
    from looplab.cli.run_cmds import _in_flight_node_ids, _TailFold

    rd, store = _run(tmp_path)
    current = _TailFold(store)
    assert _in_flight_node_ids(current(), current.withheld) == [0]
    store.append("eval_attempt_withheld", {"node_id": 0, "generation": 0, "attempt": 0,
                                           "at": "after_canary", "reason": "paused",
                                           "eval_seconds": 1.0})
    assert _in_flight_node_ids(current(), current.withheld) == []
    store.append("eval_invocation_claimed", {"node_id": 0, "generation": 0})
    assert _in_flight_node_ids(current(), current.withheld) == [0]


def test_stop_wait_prints_neither_line_about_a_withheld_evaluation(tmp_path, monkeypatch):
    """Driven through the CLI: the line `stop --wait` prints at the stop and the line it repeats
    while it waits both read the same withheld set, and the control — no withhold — still names the
    running node in both. MUTATION: drop `current.withheld` at either site -> "node 0"."""
    from typer.testing import CliRunner

    from looplab.cli import app, run_cmds
    from looplab.engine import run_lifecycle

    def _stop(rd):
        said: list = []

        def _await(target, *, describe, **_kw):
            said.append(describe())
            return "exited", ""

        monkeypatch.setattr(run_lifecycle, "engine_liveness", lambda _rd: True)
        monkeypatch.setattr(run_cmds, "await_engine_exit", _await)
        out = CliRunner().invoke(app, ["stop", str(rd), "--wait"])
        assert out.exit_code == 0, out.output
        return out.output, said

    (tmp_path / "running").mkdir()
    rd, _store = _run(tmp_path / "running")
    output, said = _stop(rd)
    assert "already running (node 0)" in output and said == ["still waiting: node 0 evaluating"]

    (tmp_path / "withheld").mkdir()
    rd, store = _run(tmp_path / "withheld")
    store.append("eval_attempt_withheld", {"node_id": 0, "generation": 0, "attempt": 0,
                                           "at": "after_canary", "reason": "paused",
                                           "eval_seconds": 1.0})
    output, said = _stop(rd)
    assert "already running" not in output, output
    assert said == ["still waiting: no evaluation running, the engine is finishing its turn"]
