"""MCP `start_run`: the external agent creates and starts its own run (doc 82 П3).

The composite composes the existing launch funnel — `/api/start/preflight`, `/api/start` with its
validation token and a key derived from the caller's, `/api/start/{run}/status`, `/state` — and saves
the launch body on the client before the POST, so a repeat after a lost reply resends it exactly.
"""
from __future__ import annotations

import json
import socket
import sys
from contextlib import closing
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from looplab.harness import composite
from looplab.harness.mcp_server import HarnessAPI

ROOT = Path(__file__).resolve().parents[1]
GEN = "c" * 64
TASK = {"goal": "minimize (x-3)^2", "direction": "min", "benchmark": "quadratic"}


def _progress():
    from tests.test_harness_connection import progress
    return progress() | {"generation": GEN}


class _Server:
    """A scripted launch funnel: what each route answers, and every request it saw."""

    def __init__(self, *, preflight=None, start_status=200, lose_start=False, startup="succeeded"):
        self.seen, self.starts = [], 0
        self.preflight = preflight
        self.start_status, self.lose_start, self.startup = start_status, lose_start, startup

    def __call__(self, request):
        self.seen.append(request)
        path = request.url.path
        if path == "/api/runs":
            return httpx.Response(200, json={"runs": []})
        if path == "/api/start/preflight":
            if self.preflight is not None:
                return httpx.Response(self.preflight[0], json=self.preflight[1])
            body = json.loads(request.content)
            return httpx.Response(200, json={"ok": True, "validation_token": "tok-1",
                                             "preview": {"run_id": body["run_id"],
                                                         "settings": body["settings"]},
                                             "warnings": []})
        if path == "/api/start":
            self.starts += 1
            if self.lose_start and self.starts == 1:
                raise httpx.ReadError("connection lost after the server accepted")
            body = json.loads(request.content)
            return httpx.Response(self.start_status, json={"ok": True, "run_id": body["run_id"]})
        if path.endswith("/status"):
            return httpx.Response(200, json={"ok": True, "status": self.startup, "started": True,
                                             "run_id": path.split("/")[3]})
        if path.endswith("/state"):
            return httpx.Response(200, json={"generation": GEN, "state": {}})
        if path.endswith("/harness-progress"):
            return httpx.Response(200, json=_progress())
        return httpx.Response(404, json={"detail": "unexpected"})


def _api(server, tmp_path):
    return HarnessAPI("http://127.0.0.1:8765", transport=httpx.MockTransport(server),
                      request_dir=tmp_path / "requests")


def test_start_run_is_preflight_start_status_state_and_says_where_to_look(tmp_path):
    server = _Server()
    result = composite.start_run(_api(server, tmp_path), task=TASK, key="exp-1", wait_seconds=0)
    assert result["ok"], result
    assert [s["step"] for s in result["steps"]] == ["preflight", "start", "startup", "read_state",
                                                    "read_progress"]
    assert result["generation"] == GEN and result["next_step"]["code"] == "choose_direction"
    assert result["ui_url"] == f"http://127.0.0.1:8765/#/run/{result['run_id']}"
    assert result["run_id"].startswith("minimize-x-3-2-")
    start = next(r for r in server.seen if r.url.path == "/api/start")
    body = json.loads(start.content)
    assert body["settings"] == {"external_harness": True}       # always external, nothing else
    assert body["validation_token"] == "tok-1" and body["idempotency_key"] == "exp-1:start"
    status = next(r for r in server.seen if r.url.path.endswith("/status"))
    assert status.headers["Idempotency-Key"] == "exp-1:start"


def test_same_key_repeat_resends_the_saved_launch_without_a_new_preflight(tmp_path):
    first = _Server()
    one = composite.start_run(_api(first, tmp_path), task=TASK, key="exp-2", wait_seconds=0)
    again = _Server(preflight=(500, {"detail": "a repeat must not re-validate"}))
    two = composite.start_run(_api(again, tmp_path), task=TASK, key="exp-2", wait_seconds=0)
    assert two["ok"] and two["run_id"] == one["run_id"]
    assert not any(r.url.path == "/api/start/preflight" for r in again.seen)
    resent = json.loads(next(r for r in again.seen if r.url.path == "/api/start").content)
    original = json.loads(next(r for r in first.seen if r.url.path == "/api/start").content)
    assert resent == original


def test_a_lost_start_reply_is_observed_from_the_durable_record_not_resent(tmp_path):
    server = _Server(lose_start=True)
    result = composite.start_run(_api(server, tmp_path), task=TASK, key="exp-3", wait_seconds=0)
    assert result["ok"], result
    assert server.starts == 1                                   # no automatic second POST
    assert result["steps"][1] == {"step": "start", "ok": True, "outcome": "unknown",
                                  "message": "acknowledgement lost; observing the startup record"}


def test_a_refused_task_returns_its_field_errors_and_starts_nothing(tmp_path):
    detail = {"code": "invalid_task", "message": "task is invalid",
              "field_errors": {"task.direction": "must be max or min"}}
    server = _Server(preflight=(422, {"detail": detail}))
    result = composite.start_run(_api(server, tmp_path), task=TASK, key="exp-4", wait_seconds=0)
    assert result["ok"] is False and result["stopped_at"] == "preflight"
    assert result["field_errors"] == {"task.direction": "must be max or min"}
    assert result["code"] == "invalid_task"
    assert server.starts == 0
    # Nothing was saved either: the corrected task under the same key validates afresh.
    fixed = _Server()
    assert composite.start_run(_api(fixed, tmp_path), task=TASK, key="exp-4", wait_seconds=0)["ok"]
    assert any(r.url.path == "/api/start/preflight" for r in fixed.seen)


def test_dry_run_validates_and_starts_nothing(tmp_path):
    server = _Server()
    result = composite.start_run(_api(server, tmp_path), task=TASK, key="exp-5", dry_run=True)
    assert result["ok"] and result["dry_run"] and server.starts == 0
    assert result["preview"]["settings"] == {"external_harness": True}


def test_a_pending_startup_says_to_repeat_with_the_same_key(tmp_path):
    result = composite.start_run(_api(_Server(startup="executing"), tmp_path), task=TASK,
                                 key="exp-6", wait_seconds=0)
    assert result["ok"] is False and result["stopped_at"] == "startup"
    assert "same key" in result["message"].lower()
    assert "run_started" in result["steps"][-1]["message"]


@pytest.mark.parametrize("bad", [{"task": {}}, {"key": ""}, {"key": "x" * 191},
                                 {"settings": ["external_harness"]}])
def test_malformed_arguments_are_refused_before_any_request(tmp_path, bad):
    server = _Server()
    with pytest.raises(ValueError):
        composite.start_run(_api(server, tmp_path), **({"task": TASK, "key": "k"} | bad))
    assert server.seen == []


def test_the_tool_is_advertised_with_the_assistants_task_schema_and_instructions_lead_with_it():
    pytest.importorskip("mcp")
    import anyio
    from looplab.core.run_proposal import COMPOSABLE_TASK_GUIDE
    from looplab.harness.mcp_server import MCP_INSTRUCTIONS, build_server
    from looplab.tools.run_launcher_tools import RunLauncherTools
    api = HarnessAPI("http://127.0.0.1:8765", transport=httpx.MockTransport(
        lambda request: pytest.fail("listing tools contacted the UI")))
    tool = next(t for t in anyio.run(build_server(api).list_tools) if t.name == "start_run")
    assert tool.description.endswith(COMPOSABLE_TASK_GUIDE)
    assert COMPOSABLE_TASK_GUIDE in RunLauncherTools().specs()[0]["function"]["description"]
    assert tool.model_dump(by_alias=True)["inputSchema"]["required"] == ["task", "key"]
    head = MCP_INSTRUCTIONS[:400]
    assert head.index("start_run") < head.index("run_progress") < head.index("submit_experiment")


def test_the_mcp_tool_call_reaches_the_composite(tmp_path):
    """Through the MCP server's own `call_tool`, not the composite directly: the tool's body runs."""
    pytest.importorskip("mcp")
    import anyio
    from looplab.harness.mcp_server import build_server
    server = _Server()
    result = anyio.run(build_server(_api(server, tmp_path)).call_tool,
                       "start_run", {"task": TASK, "key": "via-tool"})
    payload = json.loads(result.content[0].text if hasattr(result, "content") else result[0].text)
    assert payload["ok"] and payload["generation"] == GEN
    assert server.starts == 1


def test_start_run_against_the_open_app_creates_an_agent_minimal_toy_run(tmp_path, monkeypatch):
    """End to end over the real launch funnel (engine spawn replaced by a stand-in that writes what a
    started engine writes first): the run is external, `backend=toy`, profile `agent-minimal`, and
    its contract asks only for the operator's declared checks."""
    from looplab.core.config import Settings
    from looplab.events.eventstore import EventStore
    from looplab.serve.server import make_app
    monkeypatch.delenv("LOOPLAB_UI_TOKEN", raising=False)
    monkeypatch.delenv("LOOPLAB_HARNESS_TOKEN", raising=False)

    def fake_popen(cmd, **kw):
        rd = Path(cmd[cmd.index("--out") + 1])
        doc = json.loads((rd / "task.input.json").read_text())
        (rd / "config.snapshot.json").write_text(json.dumps(
            Settings(**doc["settings"]).masked_snapshot()))
        (rd / "task.snapshot.json").write_text(json.dumps(doc["task"]))
        EventStore(rd / "events.jsonl").append("run_started", {
            "run_id": rd.name, "run_uid": "u1", "task_id": "t", "goal": "g", "direction": "min"})
        return type("P", (), {"pid": None, "poll": lambda self: None})()
    monkeypatch.setattr("looplab.serve.engine_proc.subprocess.Popen", fake_popen)
    client = TestClient(make_app(tmp_path / "runs"))

    def via_app(request):
        reply = client.request(request.method, request.url.raw_path.decode(),
                               headers=dict(request.headers), content=request.content)
        return httpx.Response(reply.status_code, content=reply.content,
                              headers={"content-type": reply.headers.get("content-type", "")})

    from looplab.serve.run_commands import RunCommandService
    observed = []
    real_observe = RunCommandService.observe_external_spawn

    def observe(self, rd, owner):
        observed.append(owner)
        return real_observe(self, rd, owner)
    monkeypatch.setattr(RunCommandService, "observe_external_spawn", observe)
    task = json.loads((ROOT / "examples" / "toy_task.json").read_text())
    api = HarnessAPI("http://127.0.0.1:8765", transport=httpx.MockTransport(via_app),
                     request_dir=tmp_path / "requests")
    result = composite.start_run(api, task=task, key="toy-1", wait_seconds=5)
    assert result["ok"], result
    # A started run's status read hands its spawn lease to the canonical observer, which retires it
    # once the engine lock shows — or the agent's first command is refused engine_start_uncertain.
    assert observed and all(owner.startswith("start:start_") for owner in observed)
    rd = tmp_path / "runs" / result["run_id"]
    settings = json.loads((rd / "task.input.json").read_text())["settings"]
    assert settings["external_harness"] is True and settings["backend"] == "toy"
    assert settings["profile"] == "agent-minimal"
    contract = client.get(f"/api/runs/{result['run_id']}/harness-contract").json()
    assert {k for k, v in contract["phase_obligations"].items() if v.get("required")} == {"stage_check"}
    # The same key again is the server's keyed replay of the same run, not a second one.
    again = composite.start_run(api, task=task, key="toy-1", wait_seconds=5)
    assert again["ok"] and again["run_id"] == result["run_id"]
    assert sorted(p.name for p in (tmp_path / "runs").iterdir()
                  if p.is_dir() and not p.name.startswith(".")) == [result["run_id"]]


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_ensure_server_prefers_the_default_port_and_a_second_call_reuses_it(tmp_path, monkeypatch):
    """The MCP harness's own UI takes the default port when it is free, so the next session finds
    it there instead of starting another one."""
    from looplab.serve import tui_format
    port = _free_port()
    monkeypatch.setenv("LOOPLAB_UI_PORT", str(port))
    url, child = tui_format.ensure_server(None, str(tmp_path / "runs"), prefer_default_port=True)
    try:
        assert url == f"http://127.0.0.1:{port}" and child is not None
        again, second = tui_format.ensure_server(None, str(tmp_path / "runs"),
                                                 prefer_default_port=True)
        assert again == url and second is None
    finally:
        tui_format.stop_server(child)
    assert child.poll() is not None


def test_ensure_reachable_autostarts_only_without_an_explicit_url(tmp_path, monkeypatch):
    refused = httpx.MockTransport(lambda r: (_ for _ in ()).throw(httpx.ConnectError("refused")))
    explicit = HarnessAPI("http://127.0.0.1:8765", transport=refused)
    assert explicit.ensure_reachable()["code"] == "api_unreachable"
    calls = []

    def fake_ensure(base_url, run_root, **kw):
        calls.append((base_url, run_root, kw))
        return "http://127.0.0.1:9911", None
    monkeypatch.setattr("looplab.serve.tui_format.ensure_server", fake_ensure)
    api = HarnessAPI("http://127.0.0.1:8765", transport=refused, request_dir=tmp_path / "rq")
    api.autostart_root = tmp_path / "runs"
    assert api.ensure_reachable() == {"ok": True, "autostarted": "http://127.0.0.1:9911"}
    assert calls == [(None, str(tmp_path / "runs"), {"prefer_default_port": True})]
    assert str(api.client.base_url) == "http://127.0.0.1:9911/"
    assert api.requests.server == api.starts.server == "http://127.0.0.1:9911/"


def test_run_stdio_without_url_or_token_arms_autostart_over_the_cwd_runs(tmp_path, monkeypatch):
    import looplab.harness.mcp_server as module
    for name in ("LOOPLAB_HARNESS_URL", "LOOPLAB_HARNESS_TOKEN", "LOOPLAB_UI_TOKEN", "LOOPLAB_RUN_ROOT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LOOPLAB_HARNESS_REQUEST_DIR", str(tmp_path / "rq"))
    monkeypatch.chdir(tmp_path)
    seen = []

    class Server:
        def __init__(self, api):
            self.api = api

        def run(self, transport):
            seen.append(self.api.autostart_root)

    monkeypatch.setattr(module, "build_server", Server)
    module.run_stdio()
    monkeypatch.setenv("LOOPLAB_HARNESS_URL", "http://127.0.0.1:8765")
    module.run_stdio()          # an explicit URL is a server the operator runs: never autostart
    assert seen == [(tmp_path / "runs").resolve(), None]
