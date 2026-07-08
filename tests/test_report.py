"""Agent-authored run report (Workstream A): the generator degrades offline, the `report_generated`
event folds into RunState.report, the engine regenerates on a node-count cadence + at finish, and the
manual `/report_refresh` endpoint generates inline (soft-failing when no model is reachable).
"""
from __future__ import annotations

from pathlib import Path

import anyio
import pytest

from looplab.core.models import Event
from looplab.events.replay import fold
from looplab.serve.report import generate_report, make_report_writer

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
    from looplab.core.config import Settings
    assert make_report_writer(Settings(), client=None) is None


# ---- engine integration (needs the [ui] extra for the toy run harness parity) ----
pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from looplab.engine.orchestrator import Engine  # noqa: E402
from looplab.search.policy import GreedyTree  # noqa: E402
from looplab.runtime.sandbox import SubprocessSandbox  # noqa: E402
from looplab.serve.server import make_app  # noqa: E402
from looplab.adapters.toytask import ToyTask  # noqa: E402


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
    import looplab.serve.report as report_mod
    monkeypatch.setattr(report_mod, "generate_report",
                        lambda st, c, **kw: {"headline": "live", "at_node": len(st.nodes),
                                             "trigger": kw.get("trigger", "")})
    monkeypatch.setattr("looplab.serve.server.make_llm_client", lambda s: object())
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
    monkeypatch.setattr("looplab.serve.server.make_llm_client", _boom)
    r = client.post("/api/runs/demo/report_refresh")
    assert r.status_code == 200 and r.json()["ok"] is False  # soft-fail, no crash


def test_report_refresh_async_job_path(tmp_path, monkeypatch):
    """report_refresh runs as a BACKGROUND JOB so a slow/large regen can't 504 behind a proxy: with the
    inline wait forced to 0 the POST hands back a job_id, the report generates + appends in the worker
    thread, and GET /api/jobs/{id} returns the SAME {ok, seq, content} contract — folding into
    state.report (so the live UI updates) exactly as the inline path did."""
    import time as _t
    monkeypatch.setenv("LOOPLAB_JOB_INLINE_WAIT", "0")          # always take the async path
    _build_run(tmp_path, "demo", writer=None)
    import looplab.serve.report as report_mod
    monkeypatch.setattr(report_mod, "generate_report",
                        lambda st, c, **kw: {"headline": "live", "at_node": len(st.nodes),
                                             "trigger": kw.get("trigger", "")})
    monkeypatch.setattr("looplab.serve.server.make_llm_client", lambda s: object())
    client = TestClient(make_app(tmp_path))                     # reads the inline wait at construction
    r = client.post("/api/runs/demo/report_refresh").json()
    assert r["status"] == "running" and r["job_id"]            # handed back a job, didn't block
    job = None
    for _ in range(100):                                        # poll the background regen to completion
        job = client.get(f"/api/jobs/{r['job_id']}").json()
        if job.get("status") == "done":
            break
        _t.sleep(0.05)
    assert job and job["status"] == "done" and job["ok"] is True
    assert job["content"]["headline"] == "live" and "seq" in job   # full contract preserved
    # the worker thread appended report_generated -> it folded into state.report
    st = client.get("/api/runs/demo/state").json()["state"]
    assert st["report"]["headline"] == "live"


def _read_events(rd: Path):
    from looplab.events.eventstore import iter_jsonl
    return [Event(**o) for o in iter_jsonl(rd / "events.jsonl")]


# ---- Workstream C: chat action-router (/command) ----
def test_command_to_action_mapping():
    from looplab.serve.server import _Action, _action_to_control

    class _S:
        best_node_id = 9
    s = _S()
    assert _action_to_control(_Action(action="confirm", node_id=5), s)["type"] == "force_confirm"
    assert _action_to_control(_Action(action="fork", node_id=4), s)["data"] == {"from_node_id": 4}
    assert _action_to_control(_Action(action="approve"), s)["data"] == {"node_id": 9}  # defaults to best
    # 3-verb operator control: stop = freeze (pause), finalize = wrap up (run_abort), resume
    assert _action_to_control(_Action(action="stop"), s)["type"] == "pause"
    assert _action_to_control(_Action(action="finalize"), s)["type"] == "run_abort"
    assert _action_to_control(_Action(action="resume"), s)["type"] == "resume"
    assert _action_to_control(_Action(action="advise"), s) is None  # not actionable -> chat reply
    # guidance steers the search via a hint (not a forced node): text carries the researcher directive
    h = _action_to_control(_Action(action="hint", text="use log1p targets + domain features"), s)
    assert h["type"] == "hint" and h["data"]["text"] == "use log1p targets + domain features"
    assert _action_to_control(_Action(action="hint", text=""), s) is None  # empty hint -> not actionable
    assert _action_to_control(_Action(action="deep_research"), s)["type"] == "deep_research"


def test_action_labels_are_human_readable():
    """Every applied-row `label` is what the human reads in the chat timeline, so it must be a plain
    sentence — never a Python dict/`repr` leaking braces/quotes (the readability regression we fixed)."""
    from looplab.serve.server import _Action, _action_to_control

    class _S:
        best_node_id = 9
    s = _S()
    cases = [
        _Action(action="strategy", policy="ucb", fidelity="low"),
        _Action(action="inject", operator="improve", params={"lr": 0.1, "depth": 3}),
        _Action(action="budget", nodes=10),
        _Action(action="confirm", node_id=5),
        _Action(action="note", node_id=3, text="try log1p"),
    ]
    for c in cases:
        lab = _action_to_control(c, s)["label"]
        assert lab and "{" not in lab and "}" not in lab and "'" not in lab, lab
    # the two that used to leak a dict repr now read as key=value
    assert _action_to_control(_Action(action="strategy", policy="ucb", fidelity="low"), s)["label"] \
        == "Switch strategy → policy=ucb fidelity=low"
    assert "lr=0.1" in _action_to_control(_Action(action="inject", params={"lr": 0.1}), s)["label"]


def test_command_endpoint_returns_action(tmp_path, monkeypatch):
    _build_run(tmp_path, "demo", writer=None)
    client = TestClient(make_app(tmp_path))
    from looplab.serve.server import _Action, _Plan
    monkeypatch.setattr("looplab.serve.server.make_llm_client", lambda s: object())
    monkeypatch.setattr("looplab.core.parse.parse_structured",
                        lambda *a, **k: _Plan(actions=[_Action(action="promote", node_id=2)]))
    r = client.post("/api/runs/demo/command", json={"instruction": "promote node 2"}).json()
    assert r["ok"] is True and r["actions"][0]["type"] == "promote" and r["actions"][0]["data"]["node_id"] == 2


def test_command_endpoint_guidance_becomes_steering_hint(tmp_path, monkeypatch):
    """A guiding chat message must STEER the search (a hint the researcher follows), not just reply.
    The router classifies guidance as a hint with the researcher directive in `text` + a friendly
    human ack in `rationale`; the endpoint surfaces both so the boss applies it as a control event."""
    _build_run(tmp_path, "demo", writer=None)
    client = TestClient(make_app(tmp_path))
    from looplab.serve.server import _Action, _Plan
    monkeypatch.setattr("looplab.serve.server.make_llm_client", lambda s: object())
    monkeypatch.setattr("looplab.core.parse.parse_structured",
        lambda *a, **k: _Plan(reply="Got it — I'll have the researcher try those.", actions=[
            _Action(action="hint", text="use log1p-transformed targets + volume features",
                    rationale="Got it — I'll have the researcher try those.")]))
    r = client.post("/api/runs/demo/command",
                    json={"instruction": "look up how people win nomad2018 and try it"}).json()
    assert r["ok"] and r["actions"][0]["type"] == "hint"
    assert "log1p" in r["actions"][0]["data"]["text"]              # the researcher directive
    assert "researcher" in r["actions"][0]["rationale"]            # the human-facing acknowledgement


def test_pending_hint_reaches_researcher_prompt(monkeypatch):
    """End of the steering chain: a folded `hint` actually appears as an 'Operator directive' in the
    proposal prompt the researcher sends — this is HOW a chat guidance message changes the search."""
    from looplab.agents.roles import LLMResearcher
    from looplab.core.models import Idea, RunState
    captured = {}

    def fake_parse(client, messages, schema, parser):
        captured["messages"] = messages
        return Idea(operator="improve", params={"degree": 3.0}, rationale="r")

    monkeypatch.setattr("looplab.agents.roles.parse_structured", fake_parse)
    r = LLMResearcher(client=object(), space_hint="space")
    st = RunState(goal="min", direction="min")
    st.pending_hints = [{"text": "use log1p targets + domain features"}]
    r.propose(st, None)
    user = next(m["content"] for m in captured["messages"] if m["role"] == "user")
    # "Operator directive" matches both the single-hint ("…directive (follow it):") and the
    # multi-hint ("…directives, oldest first…") renderings from looplab.agents.hints.
    assert "Operator directive" in user and "log1p targets + domain features" in user


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

    monkeypatch.setattr("looplab.serve.server.make_llm_client", lambda s: _Cap(s))
    r = client.post("/api/runs/demo/chat", json={"messages": [{"role": "user", "content": "hi"}]}).json()
    assert captured["model"] == "deepseek/deepseek-v4-flash"     # the RUN's model, not the UI env
    assert r["trace"]["model"] == "deepseek/deepseek-v4-flash"   # and the trace reports it honestly


def test_boss_grounds_on_digest_and_uses_run_tools_then_acts(tmp_path, monkeypatch):
    """The boss now decides WITH context: its prompt carries the experiments digest, and it MAY call
    the run-introspection tools before emitting an action (not blind, single-best-node routing)."""
    import json as _json
    _build_run(tmp_path, "demo", writer=None)
    client = TestClient(make_app(tmp_path))

    def _tc(name, args):
        return {"content": "", "tool_calls": [{"id": "c1", "function": {"name": name, "arguments": _json.dumps(args)}}]}

    class _FakeBoss:
        model = "fake"
        def __init__(self):
            # turn 1: consult a tool; turn 2: emit a PLAN whose one step is a steering hint
            self.script = [_tc("list_experiments", {"sort": "best"}),
                           _tc("emit", {"reply": "on it",
                                        "actions": [{"action": "hint", "text": "try log1p targets", "rationale": "ok"}]})]
            self.seen = []
        def chat(self, messages, tools, tool_choice="auto"):
            self.seen.append(messages)
            return self.script.pop(0)
        def complete_text(self, msgs):
            return "advice"

    fake = _FakeBoss()
    monkeypatch.setattr("looplab.serve.server.make_llm_client", lambda s: fake)
    r = client.post("/api/runs/demo/command", json={"instruction": "focus on feature engineering"}).json()
    assert r["ok"] and r["actions"][0]["type"] == "hint" and "log1p" in r["actions"][0]["data"]["text"]
    # the boss's prompt was grounded on the digest (the working set), not just the single best node
    assert "Search so far" in fake.seen[0][0]["content"]
    # and it actually consulted a tool first — a tool result was fed back before the emit
    assert any(any(m.get("role") == "tool" for m in msgs) for msgs in fake.seen)


def test_boss_context_includes_the_run_report(tmp_path, monkeypatch):
    """Regression (review-found dead code): the agent report is a _ReportOut dump (headline/verdict/
    next_directions — NOT a 'content' key), so it must be stitched into the boss/chat context, not
    silently dropped by reading a non-existent rep['content']."""
    from looplab.events.eventstore import EventStore
    _build_run(tmp_path, "demo", writer=None)
    EventStore(tmp_path / "demo" / "events.jsonl").append("report_generated", {"content": {
        "headline": "Quadratic solved near-optimally", "verdict": "metric improved a lot",
        "champion_summary": "x=3, y=-1", "next_directions": ["try a finer sweep around the optimum"]}})
    client = TestClient(make_app(tmp_path))
    captured = {}

    class _Cap:
        def __init__(self, s): self.model = s.llm_model
        def complete_text(self, msgs): captured["sys"] = msgs[0]["content"]; return "ok"

    monkeypatch.setattr("looplab.serve.server.make_llm_client", lambda s: _Cap(s))
    client.post("/api/runs/demo/chat", json={"messages": [{"role": "user", "content": "hi"}]})
    assert "Latest run report" in captured["sys"]
    assert "Quadratic solved near-optimally" in captured["sys"]       # the headline reached the boss
    assert "try a finer sweep around the optimum" in captured["sys"]  # a next_directions item too


def test_chat_compact_summarizes_and_reports_tokens(tmp_path, monkeypatch):
    """Compaction folds a stretch of older turns into ONE recap and reports the token cost — so the UI
    can append a durable `summary` turn and the header running-total stays honest."""
    _build_run(tmp_path, "demo", writer=None)
    client = TestClient(make_app(tmp_path))
    captured = {}

    class _Cap:
        def __init__(self, s):
            self.model = s.llm_model
            self.accountant = type("A", (), {"prompt_tokens": 120, "completion_tokens": 30,
                                             "total_tokens": 150, "calls": 1})()

        def complete_text(self, msgs):
            captured["user"] = msgs[-1]["content"]
            return "recap: agreed to try MLPs; budget raised by 10."

    monkeypatch.setattr("looplab.serve.server.make_llm_client", lambda s: _Cap(s))
    r = client.post("/api/runs/demo/chat-compact", json={"messages": [
        {"role": "user", "content": "try some neural nets"},
        {"role": "assistant", "content": "added two MLP baselines"}]}).json()
    assert r["ok"] and r["summary"].startswith("recap:")
    assert r["tokens"]["total"] == 150                     # token cost surfaced for the header total
    assert "try some neural nets" in captured["user"]      # the folded turns were actually summarized


def test_chat_compact_empty_is_noop(tmp_path, monkeypatch):
    """No turns to fold -> empty recap, no model call (and no crash)."""
    _build_run(tmp_path, "demo", writer=None)
    client = TestClient(make_app(tmp_path))

    def _boom(_s):
        raise AssertionError("must not call the model when there's nothing to compact")

    monkeypatch.setattr("looplab.serve.server.make_llm_client", _boom)
    r = client.post("/api/runs/demo/chat-compact", json={"messages": []}).json()
    assert r["ok"] and r["summary"] == ""


def test_command_reply_carries_token_usage(tmp_path, monkeypatch):
    """An advisory boss reply returns its token cost in the trace, so the chat row + header can show it."""
    _build_run(tmp_path, "demo", writer=None)
    client = TestClient(make_app(tmp_path))

    class _Cap:
        def __init__(self, s):
            self.model = s.llm_model
            self.accountant = type("A", (), {"prompt_tokens": 200, "completion_tokens": 44,
                                             "total_tokens": 244, "calls": 2})()

        def complete_text(self, msgs):
            return "here's my take"

    # Force the advisory fallback: structured plan parse + tool-loop both unavailable.
    monkeypatch.setattr("looplab.serve.server.make_llm_client", lambda s: _Cap(s))
    monkeypatch.setattr("looplab.core.parse.parse_structured",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no structured output")))
    r = client.post("/api/runs/demo/command",
                    json={"instruction": "what should I try?", "messages": []}).json()
    assert r["ok"] and r["reply"] == "here's my take"
    assert r["trace"]["tokens"]["total"] == 244


def test_boss_context_report_handles_malformed_fields(tmp_path, monkeypatch):
    """Robustness (review-found): a report whose list-fields hold a STRING (or is nested under
    'content') must be kept whole — not iterated character-by-character, and never crash."""
    from looplab.events.eventstore import EventStore
    _build_run(tmp_path, "demo", writer=None)
    EventStore(tmp_path / "demo" / "events.jsonl").append("report_generated", {"content": {
        "headline": "h", "next_directions": "try a finer sweep",   # a STRING where a list is expected
        "what_worked": ["raw features"]}})
    client = TestClient(make_app(tmp_path))
    captured = {}

    class _Cap:
        def __init__(self, s): self.model = s.llm_model
        def complete_text(self, msgs): captured["sys"] = msgs[0]["content"]; return "ok"

    monkeypatch.setattr("looplab.serve.server.make_llm_client", lambda s: _Cap(s))
    r = client.post("/api/runs/demo/chat", json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.json()["ok"]                          # did not crash on the non-list field
    sysp = captured["sys"]
    assert "try a finer sweep" in sysp             # the string stayed whole
    assert "t; r; y" not in sysp                   # NOT char-split into "t; r; y; ..."


def test_command_endpoint_soft_fails_offline(tmp_path, monkeypatch):
    _build_run(tmp_path, "demo", writer=None)
    client = TestClient(make_app(tmp_path))

    def _boom(_s):
        raise RuntimeError("no model")
    monkeypatch.setattr("looplab.serve.server.make_llm_client", _boom)
    r = client.post("/api/runs/demo/command", json={"instruction": "confirm 1"})
    assert r.status_code == 200 and r.json()["ok"] is False


def test_command_async_job_path(tmp_path, monkeypatch):
    """A slow boss plan must NOT block the request (proxy 504): the action-router runs as a BACKGROUND
    JOB, so with the inline wait forced to 0 the POST hands back a job_id and the plan completes in the
    worker thread, fetched via GET /api/jobs/{id}. The agentic-plan contract (ok/actions) the UI's
    confirm-card flow depends on is preserved through the job result unchanged."""
    import time as _t
    monkeypatch.setenv("LOOPLAB_JOB_INLINE_WAIT", "0")          # always take the async path
    _build_run(tmp_path, "demo", writer=None)
    from looplab.serve.server import _Action, _Plan
    monkeypatch.setattr("looplab.serve.server.make_llm_client", lambda s: object())
    monkeypatch.setattr("looplab.core.parse.parse_structured",
                        lambda *a, **k: _Plan(actions=[_Action(action="promote", node_id=2)]))
    client = TestClient(make_app(tmp_path))                     # reads the inline wait at construction
    r = client.post("/api/runs/demo/command", json={"instruction": "promote node 2"}).json()
    assert r["status"] == "running" and r["job_id"]            # handed back a job, didn't block
    job = None
    for _ in range(100):                                        # poll the background plan to completion
        job = client.get(f"/api/jobs/{r['job_id']}").json()
        if job.get("status") == "done":
            break
        _t.sleep(0.05)
    assert job and job["status"] == "done" and job["ok"] is True
    assert job["actions"][0]["type"] == "promote" and job["actions"][0]["data"]["node_id"] == 2


# ---- chat-first run creation: /api/start inline task + /api/genesis (pre-run BOSS) ----
def test_start_accepts_inline_task_and_spawns(tmp_path, monkeypatch):
    """The genesis flow launches via an INLINE task (no catalogue file): /api/start validates it,
    materializes it to the run dir, and spawns the engine on that file."""
    import json as _j
    client = TestClient(make_app(tmp_path))
    calls = []
    monkeypatch.setattr("looplab.serve.server.subprocess.Popen", lambda cmd, **k: calls.append(cmd) or None)
    monkeypatch.setattr("looplab.adapters.tasks.validate_task", lambda d: d)      # don't depend on the mle-bench registry
    body = {"run_id": "g-run",
            "task": {"kind": "mlebench_real", "competition": "nomad2018-predict-transparent-conductors"},
            "settings": {"llm_model": "minimax/minimax-m3"}}
    r = client.post("/api/start", json=body).json()
    assert r["ok"] is True and r["run_id"] == "g-run"
    rd = tmp_path / "g-run"
    ti = _j.loads((rd / "task.input.json").read_text(encoding="utf-8"))
    assert ti["competition"] == "nomad2018-predict-transparent-conductors"
    meta = _j.loads((rd / "ui_meta.json").read_text(encoding="utf-8"))
    assert meta["task_file"].endswith("task.input.json")
    assert calls and "run" in calls[0]                                   # engine spawned…
    assert any("task.input.json" in str(x) for x in calls[0])           # …on the materialized file


def test_start_rejects_unknown_inline_kind(tmp_path, monkeypatch):
    client = TestClient(make_app(tmp_path))
    monkeypatch.setattr("looplab.serve.server.subprocess.Popen", lambda *a, **k: None)
    r = client.post("/api/start", json={"run_id": "bad", "task": {"kind": "definitely-not-a-kind"}})
    assert r.status_code == 400                                          # validated before any spawn


def test_start_rejects_inline_task_missing_kind(tmp_path, monkeypatch):
    """A kind-less inline task is now INFERRED from its composable fields (redesign): a bare
    `competition` reads as a Kaggle/mlebench_real task, so an UNKNOWN competition is still rejected
    (via validation), just not with a 'must declare kind' error. Nothing is materialized on reject."""
    client = TestClient(make_app(tmp_path))
    monkeypatch.setattr("looplab.serve.server.subprocess.Popen", lambda *a, **k: None)
    r = client.post("/api/start", json={"run_id": "nk", "task": {"competition": "nomad2018-x"}})
    assert r.status_code == 400
    assert not (tmp_path / "nk" / "task.input.json").exists()            # nothing materialized


def test_start_rejects_invalid_inline_task_before_spawn(tmp_path, monkeypatch):
    """A structurally-bad inline task (e.g. mlebench_real with an unknown competition) 400s synchronously
    instead of spawning a doomed detached engine."""
    client = TestClient(make_app(tmp_path))
    spawned = []
    monkeypatch.setattr("looplab.serve.server.subprocess.Popen", lambda *a, **k: spawned.append(a) or None)

    def _bad(_d):
        raise ValueError("unknown competition: nope")
    monkeypatch.setattr("looplab.adapters.tasks.validate_task", _bad)
    r = client.post("/api/start", json={"run_id": "iv", "task": {"kind": "mlebench_real", "competition": "nope"}})
    assert r.status_code == 400 and not spawned                          # validated -> rejected -> no engine
    assert not (tmp_path / "iv" / "task.input.json").exists()            # not materialized


def test_start_rejects_reserved_run_id(tmp_path, monkeypatch):
    """run_id 'reports' is reserved (the cross-run report store dir) and must be rejected."""
    client = TestClient(make_app(tmp_path))
    monkeypatch.setattr("looplab.serve.server.subprocess.Popen", lambda *a, **k: None)
    r = client.post("/api/start", json={"run_id": "reports", "task_file": str(TASK)})
    assert r.status_code == 400


def test_genesis_proposes_and_normalizes_spec(tmp_path, monkeypatch):
    """The pre-run BOSS turns a one-line goal into an editable spec: name slugified, task passed
    through, only known non-secret setting overrides kept."""
    client = TestClient(make_app(tmp_path))
    from looplab.serve.server import _GenesisSpec
    monkeypatch.setattr("looplab.serve.server.make_llm_client", lambda s: object())
    monkeypatch.setattr(
        "looplab.core.parse.parse_structured",
        lambda *a, **k: _GenesisSpec(
            run_id="Nomad Minimax!!",
            task={"kind": "mlebench_real", "competition": "nomad2018-predict-transparent-conductors"},
            settings={"llm_model": "minimax/minimax-m3", "max_nodes": 100, "llm_api_key": "DROP_ME"},
            reply="Plan: run nomad on minimax.", rationale="user asked"))
    r = client.post("/api/genesis",
                    json={"instruction": "run nomad2018 on minimax/minimax-m3, 100 nodes"}).json()
    assert r["ok"] is True and r["reply"]
    spec = r["spec"]
    assert spec["run_id"] == "nomad-minimax"                             # invented name, slugified
    assert spec["task"]["kind"] == "mlebench_real"
    assert spec["settings"]["llm_model"] == "minimax/minimax-m3"
    assert spec["settings"]["max_nodes"] == 100
    assert "llm_api_key" not in spec["settings"]                         # secret stripped by normalizer


def test_genesis_run_id_dedupes_against_existing(tmp_path, monkeypatch):
    d = tmp_path / "nomad-minimax"; d.mkdir()                            # a REAL run (has events.jsonl)
    (d / "events.jsonl").write_text('{"seq":0,"type":"run_started","data":{}}\n', encoding="utf-8")
    client = TestClient(make_app(tmp_path))
    from looplab.serve.server import _GenesisSpec
    monkeypatch.setattr("looplab.serve.server.make_llm_client", lambda s: object())
    monkeypatch.setattr("looplab.core.parse.parse_structured",
                        lambda *a, **k: _GenesisSpec(run_id="nomad-minimax",
                                                     task={"kind": "mlebench_real", "competition": "x"}))
    r = client.post("/api/genesis", json={"instruction": "again"}).json()
    assert r["spec"]["run_id"] == "nomad-minimax-2"                      # collision avoided


def test_genesis_dedup_ignores_empty_leftover_dir(tmp_path, monkeypatch):
    """A leftover EMPTY dir (e.g. a validation-failed materialization) is NOT a real run, so the name
    stays free — matches /api/start's events.jsonl-keyed 409."""
    (tmp_path / "nomad-minimax").mkdir()                                 # empty, no events.jsonl
    client = TestClient(make_app(tmp_path))
    from looplab.serve.server import _GenesisSpec
    monkeypatch.setattr("looplab.serve.server.make_llm_client", lambda s: object())
    monkeypatch.setattr("looplab.core.parse.parse_structured",
                        lambda *a, **k: _GenesisSpec(run_id="nomad-minimax",
                                                     task={"kind": "mlebench_real", "competition": "x"}))
    r = client.post("/api/genesis", json={"instruction": "again"}).json()
    assert r["spec"]["run_id"] == "nomad-minimax"                        # not bumped — name is free


def test_genesis_soft_fails_offline(tmp_path, monkeypatch):
    client = TestClient(make_app(tmp_path))

    def _boom(_s):
        raise RuntimeError("no model")
    monkeypatch.setattr("looplab.serve.server.make_llm_client", _boom)
    r = client.post("/api/genesis", json={"instruction": "anything"})
    assert r.status_code == 200 and r.json()["ok"] is False and r.json()["reply"]  # usable, no crash


def test_genesis_authors_repo_task_with_setup_steps(tmp_path, monkeypatch):
    """The main-menu boss can plan a REPO run from a text description — repo path, how to run/score it,
    edit surface — and returns an adaptation checklist. The authored task must be launch-valid."""
    (tmp_path / "myrepo").mkdir()
    client = TestClient(make_app(tmp_path))
    from looplab.serve.server import _GenesisSpec
    repo_task = {
        "kind": "repo", "goal": "maximize val accuracy", "direction": "max",
        "editable_path": str(tmp_path / "myrepo"), "edit_surface": ["**/*.py"],
        "eval": {"command": ["python", "train.py"], "cwd": ".",
                 "metric": {"kind": "stdout_json", "key": "acc"},
                 "setup": ["pip", "install", "-r", "requirements.txt"]},
    }
    monkeypatch.setattr("looplab.serve.server.make_llm_client", lambda s: object())
    monkeypatch.setattr(
        "looplab.core.parse.parse_structured",
        lambda *a, **k: _GenesisSpec(
            run_id="my-repo-run", task=repo_task,
            settings={"max_nodes": 20, "developer_backend": "opencode"},
            setup_steps=['Print one JSON line {"acc": <score>} at the end of train.py',
                         "Pin dependencies in requirements.txt", "Protect the grader/answer files"],
            reply="Plan: optimize your repo.", rationale="repo run"))
    r = client.post("/api/genesis",
                    json={"instruction": "optimize my repo, run python train.py, metric acc"}).json()
    assert r["ok"] is True
    spec = r["spec"]
    assert spec["task"]["kind"] == "repo"
    assert spec["task"]["eval"]["command"] == ["python", "train.py"]
    assert spec["task"]["eval"]["metric"]["key"] == "acc"
    assert spec["settings"]["max_nodes"] == 20 and spec["settings"]["developer_backend"] == "opencode"
    assert len(spec["setup_steps"]) == 3 and any("requirements.txt" in s for s in spec["setup_steps"])

    # the authored task is a VALID repo task — /api/start would launch it, not 400.
    from looplab.adapters.tasks import validate_task
    validate_task(spec["task"])


class _ToolBoss:
    """Fake tool-calling LLM: turn 1 reads the repo README via the scout tool, turn 2 emits the spec.
    Exercises the AGENTIC genesis path (drive_tool_loop + RepoScoutTools) end to end. Returns tool
    arguments as dicts (drive_tool_loop accepts non-string args), so no JSON plumbing is needed."""
    def __init__(self, repo):
        self.repo = repo
        self.turn = 0

    def chat(self, messages, tools=None, tool_choice=None):
        self.turn += 1
        if self.turn == 1:                         # first: actually read the repo
            return {"tool_calls": [{"id": "t1", "function": {
                "name": "read_file", "arguments": {"path": str(self.repo / "README.md")}}}]}
        return {"tool_calls": [{"id": "t2", "function": {"name": "emit", "arguments": {
            "run_id": "vec-dense",
            "task": {"kind": "repo", "goal": "maximize recall@100", "direction": "max",
                     "editable_path": str(self.repo),
                     "eval": {"command": ["python", "test_looplab.py"],
                              "metric": {"kind": "stdout_json", "key": "recall@100"}}},
            "settings": {"max_nodes": 8},
            "setup_steps": ['Create test_looplab.py that prints {"recall@100": <score>} as JSON'],
            "reply": "Read the README; here's the repo plan.", "rationale": "grounded in README"}}}]}


def test_genesis_boss_scouts_repo_before_planning(tmp_path, monkeypatch):
    """The main-menu boss now ACTS like an agent: it reads the repo (README/entry script) via the
    read-only scout tools before emitting the spec, instead of just promising to."""
    repo = tmp_path / "myrepo"; repo.mkdir()
    (repo / "README.md").write_text("BEST TRAIN: python train.py --epochs 50\n", encoding="utf-8")
    boss = _ToolBoss(repo)
    monkeypatch.setattr("looplab.serve.server.make_llm_client", lambda s: boss)
    client = TestClient(make_app(tmp_path))
    r = client.post("/api/genesis", json={"instruction": f"optimize my repo at {repo}, metric recall@100"}).json()
    assert r["ok"] is True
    assert boss.turn >= 2                          # drove a tool turn, THEN emitted (didn't single-shot)
    spec = r["spec"]
    assert spec["task"]["kind"] == "repo"
    assert spec["task"]["editable_path"].endswith("myrepo")
    assert spec["task"]["eval"]["command"] == ["python", "test_looplab.py"]
    assert spec["settings"]["max_nodes"] == 8
    assert spec["setup_steps"] and any("recall@100" in s for s in spec["setup_steps"])


def test_genesis_async_job_path(tmp_path, monkeypatch):
    """A slow agentic plan must NOT block the request (proxy 504): the POST hands back a job_id and the
    plan completes in the background, fetched via GET /api/genesis/{id}. Forced here with inline-wait=0."""
    import time as _t
    monkeypatch.setenv("LOOPLAB_GENESIS_INLINE_WAIT", "0")     # always take the async path
    repo = tmp_path / "myrepo"; repo.mkdir()
    (repo / "README.md").write_text("BEST TRAIN: python train.py\n", encoding="utf-8")
    boss = _ToolBoss(repo)
    monkeypatch.setattr("looplab.serve.server.make_llm_client", lambda s: boss)
    client = TestClient(make_app(tmp_path))
    r = client.post("/api/genesis", json={"instruction": f"optimize my repo at {repo}"}).json()
    assert r["status"] == "running" and r["job_id"]            # handed back a job, didn't block
    job = None
    for _ in range(100):                                       # poll the background plan to completion
        job = client.get(f"/api/genesis/{r['job_id']}").json()
        if job.get("status") == "done":
            break
        _t.sleep(0.05)
    assert job and job["status"] == "done" and job["ok"] is True
    assert job["spec"]["task"]["kind"] == "repo" and boss.turn >= 2
    assert client.get("/api/genesis/deadbeef0000").json()["status"] == "unknown"   # unknown id


# ---- cross-run aggregate (scope) reports: project / task / super-task, one generator ----
def test_scope_report_module_deterministic_ranks_and_degrades():
    """The generator with no client returns an honest metrics rollup, ranks by the dominant direction,
    and always carries every key so the UI renders unconditionally."""
    from looplab.serve.scope_report import generate_scope_report
    briefs = [{"run_id": "a", "direction": "min", "best_metric": 0.06, "report": None},
              {"run_id": "b", "direction": "min", "best_metric": 0.05, "report": {"headline": "h"}}]
    c = generate_scope_report({"type": "task", "id": "t", "label": "task t"}, briefs, None)
    assert c["best_runs"][0]["run_id"] == "b"            # lower-better → 0.05 ranks first
    for k in ("headline", "verdict", "best_runs", "what_worked", "what_didnt",
              "learnings", "next_directions", "caveats"):
        assert k in c
    empty = generate_scope_report({"type": "task", "id": "t", "label": "task t"}, [], None)
    assert "No runs" in empty["headline"]               # empty scope degrades, never raises


def test_scope_report_ranks_each_run_by_its_own_direction():
    """A mixed-direction scope (project/super-task spanning tasks) must NOT rank a max-objective run
    backwards under a single set-wide direction."""
    from looplab.serve.scope_report import _ranked
    briefs = [{"run_id": "loss1", "direction": "min", "best_metric": 0.10},
              {"run_id": "loss2", "direction": "min", "best_metric": 0.50},
              {"run_id": "acc", "direction": "max", "best_metric": 0.95}]   # high accuracy = genuinely best
    order = [b["run_id"] for b in _ranked(briefs)]
    assert order[0] == "acc"                             # max-run with 0.95 leads, not buried last
    assert order.index("loss1") < order.index("loss2")  # min-runs still ascending among themselves


def test_scope_report_blank_emit_falls_back_to_deterministic(monkeypatch):
    """A structurally-valid but all-empty emit_report ({}) must NOT be shown as a blank report — it
    drops through to the honest metrics rollup."""
    from looplab.serve import scope_report
    # tool loop "emits" empty args -> finalize({}) -> blank _AggReport dump (non-empty dict, all defaults)
    monkeypatch.setattr("looplab.agents.agent.drive_tool_loop",
                        lambda client, tools, messages, emit_spec, **kw: kw["finalize"]({}))
    c = scope_report.generate_scope_report(
        {"type": "task", "id": "t", "label": "task t"},
        [{"run_id": "a", "direction": "min", "best_metric": 0.05, "report": None}], object())
    assert "deterministic" in c["verdict"]              # fell back, not a blank report
    assert c["best_runs"][0]["run_id"] == "a"


def test_scope_report_forces_structured_synthesis_when_loop_doesnt_emit(monkeypatch):
    """If the agent never calls emit_report (a weaker model), we force one structured synthesis over
    the digest — a real report — instead of dropping straight to the metrics rollup."""
    from looplab.serve import scope_report
    # simulate a tool loop that exhausts without emitting -> drive_tool_loop returns fallback(messages)
    monkeypatch.setattr("looplab.agents.agent.drive_tool_loop",
                        lambda client, tools, messages, emit_spec, **kw: kw["fallback"](messages))
    monkeypatch.setattr("looplab.core.parse.parse_structured",
                        lambda *a, **k: scope_report._AggReport(headline="SYNTH", verdict="agent synthesized"))
    c = scope_report.generate_scope_report(
        {"type": "task", "id": "t", "label": "task t"},
        [{"run_id": "a", "direction": "min", "best_metric": 0.05, "report": None}], object())
    assert c["headline"] == "SYNTH" and "synthesized" in c["verdict"]   # not the deterministic rollup


def _boom_client(_s):
    raise RuntimeError("no model")


def test_scope_report_generate_and_get_task_scope(tmp_path, monkeypatch):
    _build_run(tmp_path, "r1", writer=None)
    _build_run(tmp_path, "r2", writer=None)
    client = TestClient(make_app(tmp_path))
    runs = client.get("/api/runs").json()
    task_id = runs[0]["task_id"]
    assert task_id and all(r["task_id"] == task_id for r in runs)
    # offline → the endpoint still generates + persists the deterministic rollup over BOTH runs
    monkeypatch.setattr("looplab.serve.server.make_llm_client", _boom_client)
    g = client.post(f"/api/scope-report/task/{task_id}/generate").json()
    assert g["ok"] is True and set(g["run_ids"]) == {"r1", "r2"}
    assert "runs" in g["content"]["headline"]
    got = client.get(f"/api/scope-report/task/{task_id}").json()
    assert got["exists"] is True and got["stale"] is False and got["current_run_count"] == 2


def test_scope_report_absent_then_stale_on_new_run(tmp_path, monkeypatch):
    _build_run(tmp_path, "r1", writer=None)
    client = TestClient(make_app(tmp_path))
    task_id = client.get("/api/runs").json()[0]["task_id"]
    assert client.get(f"/api/scope-report/task/{task_id}").json()["exists"] is False   # nothing yet
    monkeypatch.setattr("looplab.serve.server.make_llm_client", _boom_client)
    client.post(f"/api/scope-report/task/{task_id}/generate")
    _build_run(tmp_path, "r2", writer=None)                # a new run joins the scope
    got = client.get(f"/api/scope-report/task/{task_id}").json()
    assert got["exists"] is True and got["stale"] is True and "r2" in got["added"]


def test_scope_report_project_scope_includes_descendants(tmp_path, monkeypatch):
    """A folder report covers the project AND everything nested under it."""
    _build_run(tmp_path, "r1", writer=None)
    _build_run(tmp_path, "r2", writer=None)
    client = TestClient(make_app(tmp_path))
    parent = client.post("/api/projects", json={"name": "P"}).json()
    child = client.post("/api/projects", json={"name": "C", "parent_id": parent["id"]}).json()
    client.post("/api/runs/r1/project", json={"project_id": parent["id"]})
    client.post("/api/runs/r2/project", json={"project_id": child["id"]})
    monkeypatch.setattr("looplab.serve.server.make_llm_client", _boom_client)
    g = client.post(f"/api/scope-report/project/{parent['id']}/generate").json()
    assert set(g["run_ids"]) == {"r1", "r2"}              # nested run r2 included


def test_scope_report_empty_scope_rejected(tmp_path):
    client = TestClient(make_app(tmp_path))
    assert client.post("/api/scope-report/task/nope/generate").status_code == 400
    assert client.post("/api/scope-report/bogus/x/generate").status_code == 400  # bad scope type


def test_genesis_prompt_includes_prior_learnings(tmp_path, monkeypatch):
    """A stored scope report grounds the genesis boss: its headline shows up in the genesis prompt."""
    _build_run(tmp_path, "r1", writer=None)
    client = TestClient(make_app(tmp_path))
    task_id = client.get("/api/runs").json()[0]["task_id"]
    monkeypatch.setattr("looplab.serve.server.make_llm_client", _boom_client)
    client.post(f"/api/scope-report/task/{task_id}/generate")   # persists a report w/ a headline
    from looplab.serve.server import _GenesisSpec
    captured = {}

    def _cap_parse(_client, messages, schema, parser):
        captured["sys"] = messages[0]["content"]
        return _GenesisSpec(run_id="x", task={"kind": "mlebench_real", "competition": "y"})
    monkeypatch.setattr("looplab.serve.server.make_llm_client", lambda s: object())
    monkeypatch.setattr("looplab.core.parse.parse_structured", _cap_parse)
    client.post("/api/genesis", json={"instruction": "start something new"})
    assert "Prior cross-run learnings" in captured["sys"] and "runs" in captured["sys"]
