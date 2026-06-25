"""Agent-authored run report (Workstream A): the generator degrades offline, the `report_generated`
event folds into RunState.report, the engine regenerates on a node-count cadence + at finish, and the
manual `/report_refresh` endpoint generates inline (soft-failing when no model is reachable).
"""
from __future__ import annotations

from pathlib import Path

import anyio
import pytest

from looplab.models import Event
from looplab.replay import fold
from looplab.report import generate_report, make_report_writer

ROOT = Path(__file__).resolve().parents[1]
TASK = ROOT / "examples" / "toy_task.json"


def test_generate_report_degrades_offline():
    """No usable client -> a minimal report (never raises), with at_node/trigger stamped."""
    st = fold([Event(seq=0, type="run_started",
                     data={"run_id": "r", "task_id": "t", "goal": "g", "direction": "min"})])
    content = generate_report(st, client=None, parser="tool_call", trigger="manual")
    assert content["headline"] == "(report unavailable)"
    assert content["at_node"] == 0 and content["trigger"] == "manual"
    # all the structured keys are present so the UI can render unconditionally
    for k in ("verdict", "champion_summary", "what_worked", "learnings", "what_didnt",
              "next_directions", "caveats"):
        assert k in content


def test_report_generated_folds_latest_wins():
    evs = [
        Event(seq=0, type="run_started", data={"run_id": "r", "task_id": "t", "direction": "min"}),
        Event(seq=1, type="report_generated", data={"content": {"headline": "first", "at_node": 1}}),
        Event(seq=2, type="report_generated", data={"content": {"headline": "second", "at_node": 2}}),
    ]
    st = fold(evs)
    assert st.report == {"headline": "second", "at_node": 2}


def test_make_report_writer_offline_is_none():
    from looplab.config import Settings
    assert make_report_writer(Settings(), client=None) is None


# ---- engine integration (needs the [ui] extra for the toy run harness parity) ----
pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from looplab.orchestrator import Engine  # noqa: E402
from looplab.policy import GreedyTree  # noqa: E402
from looplab.sandbox import SubprocessSandbox  # noqa: E402
from looplab.server import make_app  # noqa: E402
from looplab.toytask import ToyTask  # noqa: E402


class _FakeWriter:
    """A report writer that never touches an LLM — returns a deterministic dict and counts calls."""
    def __init__(self):
        self.calls = 0

    def generate(self, state, trigger=""):
        self.calls += 1
        return {"headline": f"report#{self.calls}", "verdict": "ok", "at_node": len(state.nodes),
                "trigger": trigger}


def _build_run(root: Path, name: str, writer=None, report_every: int = 0):
    task = ToyTask.load(TASK)
    r, d = task.build_roles()
    eng = Engine(root / name, task=task, researcher=r, developer=d,
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=2, max_nodes=4),
                 report_writer=writer, report_every=report_every)
    return anyio.run(eng.run)


def test_engine_writes_report_on_cadence_and_finish(tmp_path):
    writer = _FakeWriter()
    st = _build_run(tmp_path, "demo", writer=writer, report_every=1)
    # the cadence + finish hooks ran the writer at least once and the latest folded into state.report
    assert writer.calls >= 1
    assert st.report is not None and st.report["headline"].startswith("report#")
    # at least one report_generated event is in the log, and a finish-trigger report was written
    evs = [e for e in _read_events(tmp_path / "demo") if e.type == "report_generated"]
    assert evs, "expected report_generated events"
    assert any(e.data.get("trigger") == "finish" for e in evs)


def test_engine_no_writer_no_report(tmp_path):
    st = _build_run(tmp_path, "demo2", writer=None, report_every=3)
    assert st.report is None  # off without a writer -> deterministic-only, no event


def test_report_refresh_endpoint(tmp_path, monkeypatch):
    _build_run(tmp_path, "demo", writer=None)
    client = TestClient(make_app(tmp_path))
    # Happy path: stub the generator so the route appends a report_generated event without an LLM.
    import looplab.report as report_mod
    monkeypatch.setattr(report_mod, "generate_report",
                        lambda st, c, **kw: {"headline": "live", "at_node": len(st.nodes),
                                             "trigger": kw.get("trigger", "")})
    monkeypatch.setattr("looplab.server.make_llm_client", lambda s: object())
    r = client.post("/api/runs/demo/report_refresh").json()
    assert r["ok"] is True and r["content"]["headline"] == "live"
    # it folded into state.report
    st = client.get("/api/runs/demo/state").json()["state"]
    assert st["report"]["headline"] == "live"


def test_report_refresh_soft_fails_offline(tmp_path, monkeypatch):
    _build_run(tmp_path, "demo", writer=None)
    client = TestClient(make_app(tmp_path))

    def _boom(_s):
        raise RuntimeError("no model")
    monkeypatch.setattr("looplab.server.make_llm_client", _boom)
    r = client.post("/api/runs/demo/report_refresh")
    assert r.status_code == 200 and r.json()["ok"] is False  # soft-fail, no crash


def _read_events(rd: Path):
    from looplab.eventstore import iter_jsonl
    return [Event(**o) for o in iter_jsonl(rd / "events.jsonl")]


# ---- Workstream C: chat action-router (/command) ----
def test_command_to_action_mapping():
    from looplab.server import _Command, _command_to_action

    class _S:
        best_node_id = 9
    s = _S()
    assert _command_to_action(_Command(action="confirm", node_id=5), s)["type"] == "force_confirm"
    assert _command_to_action(_Command(action="fork", node_id=4), s)["data"] == {"from_node_id": 4}
    assert _command_to_action(_Command(action="approve"), s)["data"] == {"node_id": 9}  # defaults to best
    assert _command_to_action(_Command(action="stop"), s)["type"] == "run_abort"
    assert _command_to_action(_Command(action="advise"), s) is None  # not actionable -> chat reply


def test_command_endpoint_returns_action(tmp_path, monkeypatch):
    _build_run(tmp_path, "demo", writer=None)
    client = TestClient(make_app(tmp_path))
    from looplab.server import _Command
    monkeypatch.setattr("looplab.server.make_llm_client", lambda s: object())
    monkeypatch.setattr("looplab.parse.parse_structured", lambda *a, **k: _Command(action="promote", node_id=2))
    r = client.post("/api/runs/demo/command", json={"instruction": "promote node 2"}).json()
    assert r["ok"] is True and r["action"]["type"] == "promote" and r["action"]["data"]["node_id"] == 2


def test_command_endpoint_soft_fails_offline(tmp_path, monkeypatch):
    _build_run(tmp_path, "demo", writer=None)
    client = TestClient(make_app(tmp_path))

    def _boom(_s):
        raise RuntimeError("no model")
    monkeypatch.setattr("looplab.server.make_llm_client", _boom)
    r = client.post("/api/runs/demo/command", json={"instruction": "confirm 1"})
    assert r.status_code == 200 and r.json()["ok"] is False
