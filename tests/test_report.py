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
    # guidance steers the search via a hint (not a forced node): text carries the researcher directive
    h = _command_to_action(_Command(action="hint", text="use log1p targets + domain features"), s)
    assert h["type"] == "hint" and h["data"]["text"] == "use log1p targets + domain features"
    assert _command_to_action(_Command(action="hint", text=""), s) is None  # empty hint -> not actionable
    assert _command_to_action(_Command(action="deep_research"), s)["type"] == "deep_research"


def test_command_endpoint_returns_action(tmp_path, monkeypatch):
    _build_run(tmp_path, "demo", writer=None)
    client = TestClient(make_app(tmp_path))
    from looplab.server import _Command
    monkeypatch.setattr("looplab.server.make_llm_client", lambda s: object())
    monkeypatch.setattr("looplab.parse.parse_structured", lambda *a, **k: _Command(action="promote", node_id=2))
    r = client.post("/api/runs/demo/command", json={"instruction": "promote node 2"}).json()
    assert r["ok"] is True and r["action"]["type"] == "promote" and r["action"]["data"]["node_id"] == 2


def test_command_endpoint_guidance_becomes_steering_hint(tmp_path, monkeypatch):
    """A guiding chat message must STEER the search (a hint the researcher follows), not just reply.
    The router classifies guidance as a hint with the researcher directive in `text` + a friendly
    human ack in `rationale`; the endpoint surfaces both so the boss applies it as a control event."""
    _build_run(tmp_path, "demo", writer=None)
    client = TestClient(make_app(tmp_path))
    from looplab.server import _Command
    monkeypatch.setattr("looplab.server.make_llm_client", lambda s: object())
    monkeypatch.setattr("looplab.parse.parse_structured",
        lambda *a, **k: _Command(action="hint", text="use log1p-transformed targets + volume features",
                                 rationale="Got it — I'll have the researcher try those."))
    r = client.post("/api/runs/demo/command",
                    json={"instruction": "look up how people win nomad2018 and try it"}).json()
    assert r["ok"] and r["action"]["type"] == "hint"
    assert "log1p" in r["action"]["data"]["text"]              # the researcher directive
    assert "researcher" in r["action"]["rationale"]            # the human-facing acknowledgement


def test_pending_hint_reaches_researcher_prompt(monkeypatch):
    """End of the steering chain: a folded `hint` actually appears as an 'Operator directive' in the
    proposal prompt the researcher sends — this is HOW a chat guidance message changes the search."""
    from looplab.roles import LLMResearcher
    from looplab.models import Idea, RunState
    captured = {}

    def fake_parse(client, messages, schema, parser):
        captured["messages"] = messages
        return Idea(operator="improve", params={"degree": 3.0}, rationale="r")

    monkeypatch.setattr("looplab.roles.parse_structured", fake_parse)
    r = LLMResearcher(client=object(), space_hint="space")
    st = RunState(goal="min", direction="min")
    st.pending_hints = [{"text": "use log1p targets + domain features"}]
    r.propose(st, None)
    user = next(m["content"] for m in captured["messages"] if m["role"] == "user")
    assert "Operator directives" in user and "log1p targets + domain features" in user


def test_chat_uses_the_runs_snapshot_model_not_ui_env(tmp_path, monkeypatch):
    """One source of truth: when a run records its model in config.snapshot.json, the UI chat/command
    speak with THAT model (reproducible, honest trace) — even if the UI server's own env points at a
    different model. (Fixes the gap where a DeepSeek run's chat replied via the UI's gpt-oss model.)"""
    import json as _json
    _build_run(tmp_path, "demo", writer=None)
    # the run was launched on a specific model -> recorded in its snapshot (overwrite the test default)
    (tmp_path / "demo" / "config.snapshot.json").write_text(_json.dumps(
        {"llm_model": "deepseek/deepseek-v4-flash", "llm_base_url": "https://openrouter.ai/api/v1"}))
    # the UI server env, by contrast, is on a different model
    monkeypatch.setenv("LOOPLAB_LLM_MODEL", "openai/gpt-oss-120b:free")
    client = TestClient(make_app(tmp_path))
    captured = {}

    class _Cap:
        def __init__(self, s): self.model = s.llm_model; captured["model"] = s.llm_model
        def complete_text(self, msgs): return "ok"

    monkeypatch.setattr("looplab.server.make_llm_client", lambda s: _Cap(s))
    r = client.post("/api/runs/demo/chat", json={"messages": [{"role": "user", "content": "hi"}]}).json()
    assert captured["model"] == "deepseek/deepseek-v4-flash"     # the RUN's model, not the UI env
    assert r["trace"]["model"] == "deepseek/deepseek-v4-flash"   # and the trace reports it honestly


def test_command_endpoint_soft_fails_offline(tmp_path, monkeypatch):
    _build_run(tmp_path, "demo", writer=None)
    client = TestClient(make_app(tmp_path))

    def _boom(_s):
        raise RuntimeError("no model")
    monkeypatch.setattr("looplab.server.make_llm_client", _boom)
    r = client.post("/api/runs/demo/command", json={"instruction": "confirm 1"})
    assert r.status_code == 200 and r.json()["ok"] is False
