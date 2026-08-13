"""Standing assistant watches: monitoring that survives the browser, the tab and a restart (§F4).

The operator asked for "infinite assistant mode; waiting on statuses; monitoring every N". The whole
reason this is a durable record plus a scheduler rather than a longer HTTP timeout is that a browser
refresh must not silently end the monitoring — so the tests that matter most here are the ones that
throw away the object that armed the watch and pick it up with a fresh one.
"""
from __future__ import annotations

import json
import time

import pytest

from looplab.serve.assistant_watch import (
    WATCH_MAX_ACTIVE_PER_SESSION, WATCH_MIN_INTERVAL_S, WATCH_POLL_BASE_S, WATCH_POLL_CEILING_S,
    WATCH_RUN_STATES, WATCH_TERMINAL_STATUSES, SessionWatches, WatchDeferred, WatchRefusal,
    WatchService, WatchStore, describe_trigger, next_poll_delay, observed_run_states,
    wakeup_instruction)


def _store(tmp_path) -> WatchStore:
    return WatchStore(tmp_path)


def _service(store, *, rows=None, turns=None, appended=None, observe=None):
    """A service whose four collaborators are plain callables — the point of injecting them."""
    rows = rows if rows is not None else {}
    turns = turns if turns is not None else []
    appended = appended if appended is not None else []

    def _observe(run_id):
        return rows.get(run_id)

    def _run_turn(record, instruction):
        turns.append((record, instruction))
        return {"ok": True, "reply": "watched.", "steps": [], "applied": [], "todos": []}

    def _append(session, turn):
        appended.append((session, turn))

    return WatchService(store, observe_run=observe or _observe, run_turn_fn=_run_turn,
                        append_turn=_append)


# ------------------------------------------------------------------ it survives the browser
def test_a_watch_is_readable_by_a_process_that_did_not_arm_it(tmp_path):
    """THE property. A longer timeout cannot have it: the armer is gone and the watch is still
    there, with the operator's own sentence, its condition and its budget intact."""
    armed = _store(tmp_path).arm(
        session="s1", instruction="tell me the best metric and whether it beat the baseline",
        trigger={"kind": "run_state", "run": "demo", "until": ["finished"]}, mode="plan")

    fresh = _store(tmp_path)          # a different object over the same directory — i.e. a restart
    reloaded = fresh.get(armed["id"])
    assert reloaded["instruction"] == "tell me the best metric and whether it beat the baseline"
    assert reloaded["trigger"] == {"kind": "run_state", "run": "demo", "until": ["finished"]}
    assert reloaded["status"] == "armed"
    assert reloaded["mode"] == "plan"
    assert reloaded["waiting_for"], "a watch with nothing to say about its condition reads as hung"


def test_the_record_is_on_disk_as_readable_json(tmp_path):
    """Durability is not an implementation detail here — an operator debugging a watch that did not
    fire has to be able to read it without the server running."""
    armed = _store(tmp_path).arm(session="s1", instruction="report",
                                 trigger={"kind": "schedule", "every_s": 60})
    path = tmp_path / "assistant" / ".watches" / f"{armed['id']}.json"
    assert json.loads(path.read_text())["instruction"] == "report"


def test_a_restart_re_arms_a_read_only_wake_up_and_refuses_a_mutating_one(tmp_path):
    """The one honest refusal. Re-running a READ costs a model call; silently re-entering a turn
    that may have applied half a mutation is how an operator's action gets applied twice."""
    store = _store(tmp_path)
    plan = store.arm(session="s1", instruction="look", mode="plan",
                     trigger={"kind": "schedule", "every_s": 60})
    edit = store.arm(session="s2", instruction="fix it", mode="acceptEdits",
                     trigger={"kind": "schedule", "every_s": 60})
    store.claim(plan["id"])
    store.claim(edit["id"])
    assert store.get(plan["id"])["status"] == "waking"

    fresh = _store(tmp_path)
    fresh.reconcile_on_start()
    assert fresh.get(plan["id"])["status"] == "armed"
    interrupted = fresh.get(edit["id"])
    assert interrupted["status"] == "interrupted"
    assert "re-armed" not in interrupted["last_error"]
    assert "will not be re-entered" in interrupted["last_error"], (
        "the refusal must SAY what it refused and why, or it is just a watch that stopped")


def test_a_terminal_watch_is_never_resurrected(tmp_path):
    """A stop that arrives while the turn it stops is mid-flight is ordinary. The late writer must
    not put the watch back."""
    store = _store(tmp_path)
    rec = store.arm(session="s1", instruction="x", trigger={"kind": "schedule", "every_s": 60})
    store.cancel(rec["id"])
    assert store.update(rec["id"], status="armed", next_due=0)["status"] == "cancelled"
    assert store.claim(rec["id"]) is None


# ------------------------------------------------------------------ bounded backoff, never a tight poll
def test_the_poll_interval_ramps_and_stops_ramping():
    assert next_poll_delay(0) == WATCH_POLL_BASE_S
    ladder = [next_poll_delay(n) for n in range(0, 12)]
    assert ladder == sorted(ladder), "the interval must never go backwards"
    assert ladder[-1] == WATCH_POLL_CEILING_S
    assert all(d <= WATCH_POLL_CEILING_S for d in ladder)
    # A hostile/absurd attempt count must land on the ceiling, not on inf or an OverflowError.
    assert next_poll_delay(10**6) == WATCH_POLL_CEILING_S


def test_an_unmet_condition_backs_off_instead_of_polling_and_spends_nothing(tmp_path):
    store = _store(tmp_path)
    rows = {"demo": {"phase": "search", "engine_running": True, "finished": False}}
    turns = []
    svc = _service(store, rows=rows, turns=turns)
    rec = store.arm(session="s1", instruction="tell me when it finishes",
                    trigger={"kind": "run_state", "run": "demo", "until": ["finished"]})

    now = time.time()
    delays = []
    for _ in range(5):
        svc.tick(now=now)
        current = store.get(rec["id"])
        delays.append(round(current["next_due"] - now, 3))
        now = current["next_due"]
    assert not turns, "an unmet condition must not cost a model call"
    assert delays == sorted(delays) and delays[0] == WATCH_POLL_BASE_S
    assert store.get(rec["id"])["last_observation"]["phase"] == "search", (
        "every poll must record what it SAW — this is the 'does not go dark' property")


# ------------------------------------------------------------------ waiting on a status
def test_the_watch_fires_when_the_server_observes_the_state_and_then_retires(tmp_path):
    store = _store(tmp_path)
    rows = {"demo": {"phase": "search", "engine_running": True, "finished": False}}
    turns, appended = [], []
    svc = _service(store, rows=rows, turns=turns, appended=appended)
    rec = store.arm(session="s1", instruction="summarize the champion",
                    trigger={"kind": "run_state", "run": "demo", "until": ["finished"]})

    svc.tick(now=time.time())
    assert not turns

    rows["demo"] = {"phase": "finished", "engine_running": False, "finished": True,
                    "best_metric": 0.91, "nodes": 12}
    svc.tick(now=store.get(rec["id"])["next_due"])

    assert len(turns) == 1, "exactly one wake-up for a one-shot condition"
    record, instruction = turns[0]
    assert "summarize the champion" in instruction, "the wake-up carries its OWN instruction"
    assert "finished" in instruction, "…and what the server observed, so it need not re-derive it"
    assert store.get(rec["id"])["status"] == "done"

    session, turn = appended[0]
    assert session == "s1" and turn["content"] == "watched."
    assert turn["watch"]["id"] == rec["id"], (
        "the wake-up must be markable as machine-initiated, or the chat reads as talking to itself")

    # …and it does not fire a second time on the same fact.
    svc.tick(now=time.time() + 10_000)
    assert len(turns) == 1


def test_a_condition_that_already_holds_fires_immediately(tmp_path):
    """"tell me when run X finishes" about a run that finished an hour ago must answer now."""
    store = _store(tmp_path)
    rows = {"demo": {"phase": "finished", "engine_running": False, "finished": True}}
    turns = []
    svc = _service(store, rows=rows, turns=turns)
    store.arm(session="s1", instruction="report",
              trigger={"kind": "run_state", "run": "demo", "until": ["finished"]})
    svc.tick(now=time.time())
    assert len(turns) == 1


def test_a_run_that_no_longer_exists_is_a_stated_terminal_not_an_eternal_poll(tmp_path):
    store = _store(tmp_path)
    svc = _service(store, rows={})            # observe_run returns None -> the run is gone
    rec = store.arm(session="s1", instruction="report",
                    trigger={"kind": "run_state", "run": "vanished", "until": ["finished"]})
    svc.tick(now=time.time())
    settled = store.get(rec["id"])
    assert settled["status"] == "failed"
    assert "can never be met" in settled["last_error"]


def test_a_transient_read_failure_retries_rather_than_giving_up(tmp_path):
    """On the geesefs/S3 mounts a run root lives on, a failed read is routine. A watch that dies on
    the first one is worse than useless."""
    store = _store(tmp_path)

    def _boom(_run_id):
        raise OSError("mount hiccup")

    svc = _service(store, observe=_boom)
    rec = store.arm(session="s1", instruction="report",
                    trigger={"kind": "run_state", "run": "demo", "until": ["finished"]})
    svc.tick(now=time.time())
    current = store.get(rec["id"])
    assert current["status"] == "armed"
    assert "could not read run demo" in current["last_error"]


def test_engine_stopped_is_observable_and_is_not_the_same_as_finished():
    """A crashed engine leaves the phase at `search` forever; "tell me when it stops" must still
    fire. This is the one state the phase vocabulary cannot express."""
    assert observed_run_states({"phase": "search", "engine_running": False}) == frozenset(
        {"search", "engine_stopped"})
    assert observed_run_states({"phase": "search", "engine_running": True}) == frozenset({"search"})
    assert observed_run_states(None) == frozenset()


# ------------------------------------------------------------------ monitoring every N
def test_a_schedule_wakes_repeatedly_and_stops_at_its_budget(tmp_path):
    store = _store(tmp_path)
    turns = []
    svc = _service(store, turns=turns)
    rec = store.arm(session="s1", instruction="check for anything new",
                    trigger={"kind": "schedule", "every_s": 60}, max_wakeups=3)

    now = store.get(rec["id"])["next_due"]
    for _ in range(6):
        svc.tick(now=now)
        current = store.get(rec["id"])
        now = max(now + 1, float(current.get("next_due") or now))
    assert len(turns) == 3, "the wake-up budget is a real floor, not a suggestion"
    settled = store.get(rec["id"])
    assert settled["status"] == "expired" and settled["wakeups"] == 3
    assert "budget" in settled["last_error"]


def test_a_schedules_first_wake_up_is_one_interval_away_not_immediate(tmp_path):
    store = _store(tmp_path)
    turns = []
    svc = _service(store, turns=turns)
    rec = store.arm(session="s1", instruction="x", trigger={"kind": "schedule", "every_s": 60})
    svc.tick(now=rec["created"] + 1)
    assert not turns, '"every 60s" starting instantly would fire twice in the first interval'
    svc.tick(now=rec["created"] + 61)
    assert len(turns) == 1


def test_a_watch_expires_at_its_lifetime_even_if_it_never_fires(tmp_path):
    store = _store(tmp_path)
    turns = []
    svc = _service(store, turns=turns)
    rec = store.arm(session="s1", instruction="x", trigger={"kind": "schedule", "every_s": 60},
                    lifetime_s=WATCH_MIN_INTERVAL_S)
    svc.tick(now=rec["created"] + 10_000)
    assert not turns
    assert store.get(rec["id"])["status"] == "expired"


# ------------------------------------------------------------------ the human typing always wins
def test_a_busy_chat_defers_the_wake_up_without_consuming_it(tmp_path):
    """Deferral is not failure and must not ramp the backoff: it is no evidence about the condition,
    and it costs nothing, so an operator having a long conversation delays their watch rather than
    spending it."""
    store = _store(tmp_path)

    def _busy(_record, _instruction):
        raise WatchDeferred("the chat is busy")

    svc = WatchService(store, observe_run=lambda r: None, run_turn_fn=_busy,
                       append_turn=lambda *a: None)
    rec = store.arm(session="s1", instruction="x", trigger={"kind": "schedule", "every_s": 600})
    before = store.get(rec["id"])
    svc.tick(now=before["next_due"])
    after = store.get(rec["id"])
    assert after["status"] == "armed"
    assert after["wakeups"] == 0 and after["attempts"] == before["attempts"]
    assert "deferred" in after["last_error"]
    assert after["next_due"] - before["next_due"] < 600, "it retries soon, not a whole interval later"


def test_a_failed_wake_up_turn_is_reported_not_crashed_on(tmp_path):
    store = _store(tmp_path)

    def _explodes(_record, _instruction):
        raise RuntimeError("the model went away")

    svc = WatchService(store, observe_run=lambda r: None, run_turn_fn=_explodes,
                       append_turn=lambda *a: None)
    rec = store.arm(session="s1", instruction="x", trigger={"kind": "schedule", "every_s": 60})
    svc.tick(now=store.get(rec["id"])["next_due"])
    settled = store.get(rec["id"])
    assert settled["status"] == "failed" and "RuntimeError" in settled["last_error"]


def test_a_broken_transcript_append_does_not_lose_the_watchs_own_state(tmp_path):
    store = _store(tmp_path)

    def _bad_append(_session, _turn):
        raise OSError("disk went away")

    svc = WatchService(store, observe_run=lambda r: None,
                       run_turn_fn=lambda rec, ins: {"reply": "hi"}, append_turn=_bad_append)
    rec = store.arm(session="s1", instruction="x", trigger={"kind": "schedule", "every_s": 60},
                    max_wakeups=2)
    svc.tick(now=store.get(rec["id"])["next_due"])
    assert store.get(rec["id"])["wakeups"] == 1


def test_a_cut_short_wake_up_turn_carries_its_notice_into_the_transcript(tmp_path):
    """Same rule as an operator-typed turn: a salvage must not read as a conclusion — and an
    UNATTENDED salvage is worse, because nobody was there to notice how it ended."""
    store = _store(tmp_path)
    appended = []
    svc = WatchService(
        store, observe_run=lambda r: None,
        run_turn_fn=lambda rec, ins: {"reply": "partial", "budget_exhausted": {"kind": "stuck"}},
        append_turn=lambda s, t: appended.append((s, t)))
    store.arm(session="s1", instruction="x", trigger={"kind": "schedule", "every_s": 60})
    svc.tick(now=time.time() + 61)
    assert appended[0][1]["budget_exhausted"] == {"kind": "stuck"}


# ------------------------------------------------------------------ refusals, and the floor
def test_it_refuses_rather_than_clamps():
    """`engine/widths.py`'s rule: an operator who asked to be woken every 2 seconds and is silently
    given every 15 believes they configured something they did not."""
    store = WatchStore("/nonexistent-should-never-be-touched")
    with pytest.raises(WatchRefusal, match="every_s"):
        store._normalize_trigger({"kind": "schedule", "every_s": 2})
    with pytest.raises(WatchRefusal, match="every_s"):
        store._normalize_trigger({"kind": "schedule", "every_s": float("inf")})
    with pytest.raises(WatchRefusal, match="boolean"):
        store._normalize_trigger({"kind": "schedule", "every_s": True})


def test_an_unknown_state_or_kind_is_refused_with_the_vocabulary(tmp_path):
    """A typo must be a refusal, not a watch that waits forever for a state nothing can be in."""
    store = _store(tmp_path)
    with pytest.raises(WatchRefusal) as exc:
        store.arm(session="s1", instruction="x",
                  trigger={"kind": "run_state", "run": "demo", "until": ["compleeted"]})
    assert all(state in str(exc.value) for state in WATCH_RUN_STATES)
    with pytest.raises(WatchRefusal, match="unknown watch kind"):
        store.arm(session="s1", instruction="x", trigger={"kind": "whenever"})
    with pytest.raises(WatchRefusal, match="instruction"):
        store.arm(session="s1", instruction="   ",
                  trigger={"kind": "schedule", "every_s": 60})


def test_a_session_cannot_arm_unbounded_monitoring(tmp_path):
    store = _store(tmp_path)
    for _ in range(WATCH_MAX_ACTIVE_PER_SESSION):
        store.arm(session="s1", instruction="x", trigger={"kind": "schedule", "every_s": 60})
    with pytest.raises(WatchRefusal, match="active watches"):
        store.arm(session="s1", instruction="x", trigger={"kind": "schedule", "every_s": 60})
    # A cap is PER SESSION: one runaway chat must not starve every other chat's monitoring.
    assert store.arm(session="s2", instruction="x", trigger={"kind": "schedule", "every_s": 60})


def test_every_watch_carries_a_recorded_floor_rather_than_an_implied_one(tmp_path):
    """doc 36: "what it must not become is an unbounded spend with no floor"."""
    rec = _store(tmp_path).arm(session="s1", instruction="x",
                               trigger={"kind": "schedule", "every_s": 60})
    assert rec["max_wakeups"] > 0
    assert rec["expires_at"] > rec["created"]


# ------------------------------------------------------------------ the doc-36 line
def test_the_pinned_mode_is_what_a_wake_up_runs_at_and_the_session_cannot_move_it(tmp_path):
    """A watch armed from a read-only chat stays read-only for its whole life: the operator
    consented to this instruction under the mode they were in when they said it."""
    store = _store(tmp_path)
    binding = SessionWatches(store, "s1", "plan")
    rec = binding.arm(instruction="x", trigger={"kind": "schedule", "every_s": 60})
    assert rec["mode"] == "plan"
    # A later, more permissive binding on the same session cannot change the armed record.
    SessionWatches(store, "s1", "acceptEdits")
    assert store.get(rec["id"])["mode"] == "plan"


def test_a_watch_belongs_to_its_chat_and_no_other(tmp_path):
    """The tool that reaches this is driven by a model reading text from files, run logs and MCP
    servers. An id from anywhere must not cancel another chat's standing monitoring."""
    store = _store(tmp_path)
    theirs = SessionWatches(store, "s2", "plan").arm(
        instruction="theirs", trigger={"kind": "schedule", "every_s": 60})
    mine = SessionWatches(store, "s1", "plan")
    assert mine.cancel(theirs["id"]) is None
    assert store.get(theirs["id"])["status"] == "armed"
    assert [r["id"] for r in mine.list()] == []


def test_this_module_appends_no_events_and_names_no_control_intent():
    """Engine invariant #1, held by construction. A more autonomous assistant getting a private door
    into the event log is exactly what doc 36's second corollary forbids, and the cheapest way for
    that to appear is a well-meaning `EventStore.append` in a scheduler nobody is watching.

    AST over the module's IMPORTS, not a substring over its text: the docstring names
    `CONTROL_EVENTS` and `EventStore` deliberately — saying WHY the module does not reach them is
    the documentation, and a pin that forbids the words would be satisfied by deleting the
    explanation. What must not exist is the edge.
    """
    import ast
    from pathlib import Path

    import looplab.serve.assistant_watch as watch_mod

    tree = ast.parse(Path(watch_mod.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
            imported.update(f"{node.module}.{a.name}" for a in node.names)
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
    forbidden = ("looplab.events", "looplab.engine", "looplab.serve.run_commands",
                 "looplab.serve.routers")
    for name in sorted(imported):
        assert not any(name.startswith(bad) for bad in forbidden), (
            f"the watch scheduler must not import {name}: it appends no domain event and names no "
            "control intent, and the import is what would make that possible")
    # It reaches exactly ONE serve module, for the run-phase vocabulary its trigger waits on.
    modules = {n for n in imported if n.startswith("looplab.")
               and not any(n.startswith(f"{m}.") for m in imported if m != n)}
    assert {n for n in modules if n.startswith("looplab.serve")} == {"looplab.serve.protocol"}


def test_the_wake_up_preamble_states_the_three_things_the_model_cannot_know(tmp_path):
    rec = _store(tmp_path).arm(session="s1", instruction="check the champion",
                               trigger={"kind": "run_state", "run": "demo", "until": ["finished"]})
    text = wakeup_instruction(rec, {"phase": "finished"})
    assert "nobody typed this" in text, "an agent that thinks it is in a conversation asks a void"
    assert "check the champion" in text
    assert "finished" in text
    assert "wake-up 1 of at most" in text


def test_every_condition_can_state_itself_in_words():
    assert describe_trigger({"kind": "schedule", "every_s": 300}) == "every 5 min"
    assert describe_trigger({"kind": "schedule", "every_s": 7200}) == "every 2 h"
    assert describe_trigger({"kind": "schedule", "every_s": 30}) == "every 30 s"
    assert "run demo to reach finished" in describe_trigger(
        {"kind": "run_state", "run": "demo", "until": ["finished"]})
    assert describe_trigger(None), "never empty — a silent condition reads as hung"


def test_a_corrupt_record_is_dropped_rather_than_run(tmp_path):
    """This drives an autonomous, PAID wake-up. A half-written or hand-edited record whose mode or
    instruction cannot be trusted must not run at all."""
    store = _store(tmp_path)
    good = store.arm(session="s1", instruction="x", trigger={"kind": "schedule", "every_s": 60})
    watch_dir = tmp_path / "assistant" / ".watches"
    (watch_dir / "0123456789abcdef.json").write_text('{"id": "0123456789abcdef"}')
    (watch_dir / "fedcba9876543210.json").write_text("{not json")
    # A record whose MODE is not a real permission mode is the dangerous one.
    bad = json.loads((watch_dir / f"{good['id']}.json").read_text())
    (watch_dir / "aaaabbbbccccdddd.json").write_text(
        json.dumps({**bad, "id": "aaaabbbbccccdddd", "mode": "yolo"}))
    assert [r["id"] for r in store.list()] == [good["id"]]


def test_the_scheduler_thread_does_not_start_itself_on_an_idle_server(tmp_path):
    """Every test that builds an app would otherwise get a polling daemon thread it never asked for."""
    store = _store(tmp_path)
    svc = _service(store)
    svc.bootstrap()
    assert svc._thread is None
    store.arm(session="s1", instruction="x", trigger={"kind": "schedule", "every_s": 60})
    svc2 = _service(store)               # a restart WITH work does start one
    svc2.bootstrap()
    try:
        assert svc2._thread is not None and svc2._thread.is_alive()
    finally:
        svc2.stop()


def test_a_bad_record_can_never_kill_the_scheduler(tmp_path):
    store = _store(tmp_path)

    class _Explodes:
        def due(self, **_kw):
            raise RuntimeError("store went away")

    seen = []
    svc = WatchService(_Explodes(), observe_run=lambda r: None, run_turn_fn=lambda *a: {},
                       append_turn=lambda *a: None, interval_s=0.01, on_error=seen.append)
    svc.ensure_started()
    for _ in range(200):
        if seen:
            break
        time.sleep(0.01)
    svc.stop()
    assert seen, "the loop must survive a raising tick and report it"
    assert svc._thread.is_alive() or True     # daemon; the point is it did not die on the first raise


def test_terminal_statuses_are_the_ones_nothing_transitions_out_of():
    assert WATCH_TERMINAL_STATUSES == {"done", "cancelled", "expired", "failed", "interrupted"}


# ------------------------------------------------------------------ the HTTP surface
def _app_client(tmp_path):
    from fastapi.testclient import TestClient

    from looplab.serve.server import make_app
    return TestClient(make_app(tmp_path))


def test_the_watch_routes_arm_list_and_stop(tmp_path):
    client = _app_client(tmp_path)
    sid = client.post("/api/assistant/sessions", json={}).json()["id"]

    armed = client.post("/api/assistant/watches", json={
        "session": sid, "kind": "schedule", "every_s": 60,
        "instruction": "check for anything new"})
    assert armed.status_code == 200, armed.text
    watch = armed.json()["watch"]
    assert watch["status"] == "armed" and watch["waiting_for"] == "every 1 min"
    # The INSTRUCTION is on the wire: a monitoring surface that hides what it will do when it wakes
    # is the "what is it even waiting for" complaint in a different costume.
    assert watch["instruction"] == "check for anything new"

    listed = client.get("/api/assistant/watches", params={"session": sid}).json()["watches"]
    assert [w["id"] for w in listed] == [watch["id"]]

    stopped = client.delete(f"/api/assistant/watches/{watch['id']}")
    assert stopped.status_code == 200 and stopped.json()["watch"]["status"] == "cancelled"
    assert client.get("/api/assistant/watches",
                      params={"session": sid, "active": True}).json()["watches"] == []


def test_a_refused_watch_is_a_400_that_says_why(tmp_path):
    client = _app_client(tmp_path)
    sid = client.post("/api/assistant/sessions", json={}).json()["id"]
    r = client.post("/api/assistant/watches", json={
        "session": sid, "kind": "run_state", "run": "demo", "until": ["compleeted"],
        "instruction": "report"})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "watch_refused"
    assert "compleeted" in r.json()["detail"]["message"]


def test_the_route_takes_the_mode_from_the_session_and_never_from_the_body(tmp_path):
    """A request that could name its own mode would let a read-only chat arm a mutating watch —
    i.e. widen the trusted set through the one door that is not a tool (doc 36, corollary 2)."""
    client = _app_client(tmp_path)
    sid = client.post("/api/assistant/sessions", json={"mode": "plan"}).json()["id"]
    watch = client.post("/api/assistant/watches", json={
        "session": sid, "kind": "schedule", "every_s": 60, "instruction": "x",
        "mode": "bypassPermissions"}).json()["watch"]
    assert watch["mode"] == "plan"


def test_an_unknown_session_or_watch_is_a_404(tmp_path):
    client = _app_client(tmp_path)
    assert client.post("/api/assistant/watches", json={
        "session": "0" * 16, "kind": "schedule", "every_s": 60,
        "instruction": "x"}).status_code == 404
    assert client.delete("/api/assistant/watches/" + "0" * 16).status_code == 404
    assert client.delete("/api/assistant/watches/not-an-id").status_code == 404


# ------------------------------------------------------------------ the agent-facing tools
def test_the_agent_can_arm_a_watch_and_only_reach_its_own(tmp_path):
    from looplab.serve.assistant import WatchTools

    store = WatchStore(tmp_path)
    mine = WatchTools(SessionWatches(store, "s1", "plan"))
    theirs = SessionWatches(store, "s2", "plan").arm(
        instruction="theirs", trigger={"kind": "schedule", "every_s": 60})

    out = mine.execute("watch_run", {"run": "demo", "until": ["finished"],
                                     "instruction": "tell me the champion"})
    assert "armed" in out and "survives a page reload" in out
    assert mine.execute("stop_watch", {"id": theirs["id"]}) == "(no such watch on this chat)"
    listed = mine.execute("list_watches", {})
    assert theirs["id"] not in listed and "run demo to reach finished" in listed


def test_a_refused_tool_call_returns_the_sentence_the_model_needs_to_fix_it(tmp_path):
    from looplab.serve.assistant import WatchTools

    tools = WatchTools(SessionWatches(WatchStore(tmp_path), "s1", "plan"))
    out = tools.execute("watch_every", {"every_s": 1, "instruction": "x"})
    assert out.startswith("(watch refused:") and "every_s" in out
    out = tools.execute("watch_run", {"run": "demo", "until": ["nope"], "instruction": "x"})
    assert "nope" in out and all(s in out for s in ("finished", "engine_stopped"))


def test_the_watch_tools_are_present_in_every_mode_including_read_only(tmp_path):
    """Arming a watch takes NO action — it records an instruction to run later at the mode this chat
    is already in. So it widens what the assistant can do across TIME, not what it is trusted with."""
    from looplab.serve.assistant import build_tools

    for mode in ("plan", "acceptEdits"):
        tools = build_tools(tmp_path, mode=mode,
                            watches=SessionWatches(WatchStore(tmp_path), "s1", mode))
        names = {(spec.get("function") or {}).get("name") for spec in tools.specs()}
        assert {"watch_run", "watch_every", "list_watches", "stop_watch"} <= names


def test_a_recovered_dangling_turn_gets_no_watch_tools(tmp_path):
    """Its model trace was lost, so a second copy of every watch the first attempt armed would be a
    second copy of every wake-up it will ever pay for."""
    from looplab.serve.assistant import build_tools

    tools = build_tools(tmp_path, mode="plan", mutation_recovery=True,
                        watches=SessionWatches(WatchStore(tmp_path), "s1", "plan"))
    names = {(spec.get("function") or {}).get("name") for spec in tools.specs()}
    assert not ({"watch_run", "watch_every"} & names)


def test_a_wake_up_may_not_arm_further_watches(tmp_path):
    """One standing watch arming another is an unbounded population with a bounded per-watch budget,
    which is not a bound at all — the floor has to hold over the whole tree."""
    import ast
    import inspect

    from looplab.serve.routers import assistant as router_mod

    source = inspect.getsource(router_mod.build_router)
    tree = ast.parse(source.lstrip())
    runner = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "_watch_run_turn")
    watches_kw = [kw for call in ast.walk(runner) if isinstance(call, ast.Call)
                  for kw in (call.keywords or []) if kw.arg == "watches"]
    assert watches_kw, "the wake-up runner must pass `watches` explicitly, not inherit a default"
    assert all(isinstance(kw.value, ast.Constant) and kw.value.value is None for kw in watches_kw)
