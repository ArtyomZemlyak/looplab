"""An operator's EXPLICIT launch width (`looplab run -s max_parallel=1`) outranks the Strategist's.

Follow-up to `tests/test_operator_width_pin.py` (a `budget_extend` of a width axis pins it). Before
this, `run_started` carried only resolved values, so the engine could not tell an explicit
`-s max_parallel=1` from a default and a Strategist `strategy_decision{eval_parallel: 2}` widened a
run the operator had launched at one eval (incident `runs/minionerec-backbones-v10`).

Pinned here: the launch surfaces record the explicit setting NAMES in `run_started`
(`explicit_settings`, absent when none), the fold carries them, and an explicitly launched width
axis voids the Strategist's grant for it — live, on resume (from the log, not live config), and not
at all on an old log that has no record.
"""
from __future__ import annotations

import asyncio

from looplab.core.models import Idea, Node, NodeStatus
from looplab.engine.widths import operator_width_axes
from looplab.events.replay import fold, fold_run_start
from tests.factories import make_engine


class _WidenStub:
    def __init__(self):
        self.calls = 0

    def decide(self, state, ctx):
        self.calls += 1
        return {"policy": "mcts", "eval_parallel": 2, "llm_parallel": 4,
                "source": "rule", "rationale": "widen"}


def _started(eng, **extra):
    eng.store.append("run_started", {"run_id": "r", "task_id": "toy", "goal": "g",
                                     "direction": "min", **extra})


def _consultable(state):
    state.nodes = {0: Node(id=0, operator="draft", idea=Idea(operator="draft"),
                           status=NodeStatus.evaluated, metric=1.0)}
    return state


def test_operator_width_axes_unions_launch_names_with_budget_extend():
    assert operator_width_axes({}, ["max_parallel"]) == {"eval_parallel"}
    assert operator_width_axes({}, ["eval_parallel", "max_nodes"]) == {"eval_parallel"}
    assert operator_width_axes({}, ["parallel_build"]) == {"llm_parallel"}
    assert operator_width_axes({"llm_parallel": 2}, ["max_parallel"]) == {
        "eval_parallel", "llm_parallel"}
    assert operator_width_axes({}, ["max_nodes", "policy"]) == frozenset()
    # Malformed records degrade to "no launch pins", never raise.
    assert operator_width_axes({}, None) == frozenset()
    assert operator_width_axes({}, "max_parallel") == frozenset()


def test_engine_records_explicit_names_in_run_started_and_the_fold_reads_them(tmp_path):
    eng = make_engine(tmp_path / "rec", max_nodes=1, explicit_settings=("max_parallel", "max_nodes"))
    asyncio.run(eng.run())
    started = [e for e in eng.store.read_all() if e.type == "run_started"]
    assert started[0].data["explicit_settings"] == ["max_nodes", "max_parallel"]
    st = fold(eng.store.read_all())
    assert st.explicit_settings == ["max_nodes", "max_parallel"]
    assert fold_run_start(eng.store.read_all()).explicit_settings == st.explicit_settings
    # Names never reach the public projection (old logs keep their exact shape).
    assert "explicit_settings" not in st.model_dump()


def test_no_explicit_settings_keeps_the_run_started_payload_unchanged(tmp_path):
    eng = make_engine(tmp_path / "none", max_nodes=1)
    asyncio.run(eng.run())
    started = [e for e in eng.store.read_all() if e.type == "run_started"][0]
    assert "explicit_settings" not in started.data
    assert fold(eng.store.read_all()).explicit_settings == []


def test_launch_max_parallel_pins_the_eval_width_against_a_strategist_decision(tmp_path):
    stub = _WidenStub()
    eng = make_engine(tmp_path / "live", strategist=stub, strategist_every=1, eval_parallel=1)
    _started(eng, explicit_settings=["max_parallel"])
    state = fold(eng.store.read_all())
    eng._apply_control_overrides(state)
    eng._maybe_consult_strategist(_consultable(state))
    assert stub.calls == 1
    assert eng._eval_parallel == 1
    recorded = [e for e in eng.store.read_all()
                if e.type == "strategy_decision"][0].data["strategy"]
    assert recorded["policy"] == "mcts"
    assert "eval_parallel" not in recorded and "max_parallel" not in recorded
    assert recorded["llm_parallel"] == 4          # the axis the operator did not spell stays granted
    assert eng._llm_parallel == 4


def test_without_explicit_settings_the_strategist_keeps_its_grant(tmp_path):
    stub = _WidenStub()
    eng = make_engine(tmp_path / "free", strategist=stub, strategist_every=1, eval_parallel=1)
    _started(eng, explicit_settings=["max_nodes"])    # explicit, but not a width
    state = fold(eng.store.read_all())
    eng._apply_control_overrides(state)
    eng._maybe_consult_strategist(_consultable(state))
    assert eng._eval_parallel == 2
    recorded = [e for e in eng.store.read_all() if e.type == "strategy_decision"][0].data["strategy"]
    assert recorded["eval_parallel"] == 2


def test_resume_keeps_the_launch_pin_from_the_log_not_live_config(tmp_path):
    """A recorded Strategist width re-applied on re-entry must not widen a launch-pinned axis, and the
    resumed engine's own (empty) launch names change nothing: the log's `run_started` decides."""
    eng = make_engine(tmp_path / "resume", eval_parallel=1, explicit_settings=("max_parallel",))
    _started(eng, explicit_settings=["max_parallel"])
    eng.store.append("strategy_decision", {"strategy": {"eval_parallel": 2, "policy": "greedy",
                                                        "source": "agent", "_pinned": []},
                                           "at_node": 1, "ctx": None})
    resumed = make_engine(tmp_path / "resume", eval_parallel=1)       # no launch names this time
    resumed._reentry_repin()
    assert resumed._eval_parallel == 1
    assert resumed._operator_width_axes == {"eval_parallel"}
    resumed._apply_control_overrides(fold(resumed.store.read_all()))
    assert resumed._eval_parallel == 1


def test_an_old_log_without_the_field_replays_exactly_as_before(tmp_path):
    """No `explicit_settings` in `run_started` = no launch pins: the recorded Strategist width is
    re-applied on re-entry just as it was before the record existed."""
    eng = make_engine(tmp_path / "old", eval_parallel=1)
    _started(eng)
    eng.store.append("strategy_decision", {"strategy": {"eval_parallel": 2, "policy": "greedy",
                                                        "source": "agent", "_pinned": []},
                                           "at_node": 1, "ctx": None})
    st = fold(eng.store.read_all())
    assert st.explicit_settings == []
    resumed = make_engine(tmp_path / "old", eval_parallel=1, explicit_settings=("max_parallel",))
    resumed._reentry_repin()
    assert resumed._operator_width_axes == frozenset()
    assert resumed._eval_parallel == 2


def test_malformed_record_folds_to_no_pins(tmp_path):
    eng = make_engine(tmp_path / "bad")
    _started(eng, explicit_settings="max_parallel")
    assert fold(eng.store.read_all()).explicit_settings == []
    eng2 = make_engine(tmp_path / "bad2")
    _started(eng2, explicit_settings=["max_parallel", 3, "", None, "x" * 500])
    assert fold(eng2.store.read_all()).explicit_settings == ["max_parallel"]


def test_cli_explicit_names_are_typed_flags_and_set_keys_but_not_nulls():
    import typer
    import pytest
    from looplab.cli.run_cmds import _explicit_setting_names

    assert _explicit_setting_names({"max_nodes": 3}, {"max_parallel": 1, "llm_parallel": None},
                                   ["eval_parallel"]) == ("eval_parallel", "max_nodes",
                                                          "max_parallel")
    assert _explicit_setting_names({}, {}, []) == ()
    with pytest.raises(typer.BadParameter):
        _explicit_setting_names({}, {}, ["max_node"])          # typo refused like -s
    with pytest.raises(typer.BadParameter):
        _explicit_setting_names({}, {}, ["llm_api_key"])       # never a credential name


def test_cli_run_records_the_set_keys(tmp_path):
    from typer.testing import CliRunner
    from looplab.cli import app

    out = tmp_path / "cli"
    res = CliRunner().invoke(app, [
        "run", "--no-genesis", "--kind", "quadratic", "--goal", "min (x-3)^2", "--direction", "min",
        "--backend", "toy", "--out", str(out), "--max-nodes", "1", "-s", "max_parallel=1"])
    assert res.exit_code == 0, res.output
    from looplab.events.eventstore import EventStore
    events = EventStore(out / "events.jsonl").read_all()
    started = [e for e in events if e.type == "run_started"][0]
    assert started.data["explicit_settings"] == ["backend", "max_nodes", "max_parallel"]
    assert operator_width_axes({}, fold(events).explicit_settings) == {"eval_parallel"}


def test_web_start_passes_the_request_settings_names_to_the_child(tmp_path, monkeypatch):
    """The Web/API launch path: the request's own `settings` are the operator's explicit layer; the
    materialized task file carries every resolved value, so the NAMES travel as `--explicit-setting`.
    The source task file's `settings:` block is not explicit (the CLI cannot tell it from a default)."""
    import json
    import pytest
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from looplab.serve.server import make_app

    source = tmp_path / "source.json"
    source.write_text(json.dumps({
        "task": {"benchmark": "quadratic", "goal": "minimize", "direction": "min"},
        "settings": {"n_seeds": 4}}), encoding="utf-8")
    captured = {}

    def fake_spawn(args, **kwargs):
        captured["args"] = args
        return None

    monkeypatch.setattr("looplab.serve.routers.control._spawn_engine", fake_spawn)
    client = TestClient(make_app(tmp_path))
    response = client.post("/api/start", json={
        "run_id": "pinned", "task_file": str(source),
        "settings": {"max_parallel": 1, "max_nodes": 3}})
    assert response.status_code == 200, response.text
    args = captured["args"]
    names = [args[i + 1] for i, a in enumerate(args) if a == "--explicit-setting"]
    assert names == ["max_nodes", "max_parallel"]

    # A launch that spells no settings passes none, and its spawn line is the historical one.
    response = client.post("/api/start", json={
        "run_id": "plain", "task_file": str(source)})
    assert response.status_code == 200, response.text
    assert "--explicit-setting" not in captured["args"]


def test_web_validation_token_binds_which_settings_are_explicit(tmp_path):
    import pytest
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from looplab.serve.server import make_app

    client = TestClient(make_app(tmp_path))
    task = {"benchmark": "quadratic", "goal": "minimize", "direction": "min"}
    plain = client.post("/api/start/preflight", json={"run_id": "t", "task": task}).json()
    # Spelling a setting at its resolved value changes no setting but does make it explicit.
    spelled = client.post("/api/start/preflight", json={
        "run_id": "t", "task": task,
        "settings": {"n_seeds": plain["preview"]["settings"]["n_seeds"]}}).json()
    assert spelled["preview"]["settings"] == plain["preview"]["settings"]
    assert spelled["validation_token"] != plain["validation_token"]


def test_replay_relaunch_carries_the_replaced_runs_launch_pins(tmp_path):
    """UI Replay relaunches with a fresh `looplab run`; the replaced run's recorded names ride the
    receipt so the new generation's `run_started` records the same pins."""
    import json
    from looplab.events.eventstore import EventStore
    from looplab.serve import reset_route
    from looplab.serve.reset_transaction import _validate_receipt

    rd = tmp_path / "run"
    rd.mkdir()
    (rd / "task.snapshot.json").write_text(json.dumps(
        {"kind": "quadratic", "goal": "minimize", "direction": "min"}), encoding="utf-8")
    EventStore(rd / "events.jsonl").append("run_started", {
        "run_id": "run", "task_id": "toy", "explicit_settings": ["max_parallel", "llm_api_key"]})

    class _Srv:
        class commands:
            @staticmethod
            def _sequence_path(rd):
                return rd / ("b" * 64 + ".seq")

        class settings:
            @staticmethod
            def ordinary_settings_env(values):
                return {}

    op = "12345678-1234-4234-8234-123456789abc"
    record = reset_route._prepare_receipt(_Srv(), rd, expected_generation="a" * 64,
                                          operation_id=op)
    assert record["explicit_settings"] == ["max_parallel"]          # never a credential name
    spawn_args, _env, _ls = reset_route._frozen_launch(_Srv(), rd, record, operation_id=op)
    assert spawn_args[spawn_args.index("--explicit-setting") + 1] == "max_parallel"
    assert spawn_args.count("--explicit-setting") == 1
    # The receipt validator admits the optional key and refuses a malformed one.
    import pytest
    from looplab.serve.reset_transaction import ResetReceiptError
    # (The stub path fails the later name check, so "malformed" is the key-set/type verdict.)
    with pytest.raises(ResetReceiptError, match="path and operation identity"):
        _validate_receipt(record, path=rd / "x")
    for bad in ("max_parallel", [3]):
        with pytest.raises(ResetReceiptError, match="malformed"):
            _validate_receipt({**record, "explicit_settings": bad}, path=rd / "x")
