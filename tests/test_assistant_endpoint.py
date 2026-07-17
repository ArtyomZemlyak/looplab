"""The general assistant (P0): session persistence, the read-only tool turn, and the HTTP routes.

Uses a scripted fake chat client (like tests/test_agentic_retrieval.py) so nothing hits a network.
"""
from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from looplab.serve.assistant import (  # noqa: E402
    SessionStore, expand_mentions, normalize_mode, run_turn, safe_assistant_failure,
    sanitize_assistant_message)
from looplab.serve.server import make_app  # noqa: E402


# --------------------------------------------------------------------------- scripted fake client
class _FakeChatClient:
    """Scripts assistant messages (a queue of chat() return dicts); records what it received."""
    def __init__(self, scripted):
        self.scripted = list(scripted)
        self.turns = []

    def chat(self, messages, tools, tool_choice="auto"):
        self.turns.append(list(messages))
        return self.scripted.pop(0)


def _call(name, args):
    return {"content": "", "tool_calls": [
        {"id": "c1", "function": {"name": name, "arguments": json.dumps(args)}}]}


def _final(reply):
    return _call("final_answer", {"reply": reply})


# --------------------------------------------------------------------------- SessionStore
def test_session_store_crud_and_fork(tmp_path):
    st = SessionStore(tmp_path)
    assert st.list() == []
    m = st.create(title="hello", mode="plan", now=1.0)
    assert m["id"] and m["mode"] == "plan" and m["title"] == "hello"
    st.append(m["id"], {"role": "user", "content": "hi"})
    st.append(m["id"], {"role": "assistant", "content": "yo"})
    got = st.get(m["id"])
    assert [t["content"] for t in got["messages"]] == ["hi", "yo"]
    # fork clones the transcript into a new child pointing back at the source
    child = st.fork(m["id"], now=2.0)
    assert child["id"] != m["id"] and child["parent"] == m["id"]
    assert [t["content"] for t in st.get(child["id"])["messages"]] == ["hi", "yo"]
    assert {s["id"] for s in st.list()} == {m["id"], child["id"]}


def test_session_store_canonicalizes_legacy_provider_failures_before_prompt_or_fork(tmp_path):
    st = SessionStore(tmp_path)
    source = st.create(title="legacy")
    leak = (
        "Couldn't reach the model (request to "
        "https://api.provider.example/v1/messages model=private-model acct=acct-42)"
    )
    st.append(source["id"], {"role": "assistant", "content": leak})

    loaded = st.messages(source["id"])
    child = st.fork(source["id"])
    forked = st.messages(child["id"])

    for transcript in (loaded, forked):
        rendered = json.dumps(transcript)
        assert transcript[0]["error_kind"] == "unavailable"
        for fragment in ("provider.example", "private-model", "acct-42", "v1/messages"):
            assert fragment not in rendered


def test_session_store_rejects_traversal(tmp_path):
    st = SessionStore(tmp_path)
    for bad in ("../evil", "a/b", ".."):
        try:
            st._sdir(bad)
            assert False, f"expected traversal guard to reject {bad!r}"
        except ValueError:
            pass


def test_expand_mentions(tmp_path):
    # @run:<id> grounds on a real run; @file:<path> injects file contents; both are reported in refs.
    rd = tmp_path / "demo"; rd.mkdir()
    (rd / "events.jsonl").write_text(
        '{"seq":0,"type":"run_started","data":{"run_id":"demo","task_id":"t","goal":"g","direction":"max"}}\n',
        encoding="utf-8")
    f = tmp_path / "note.md"; f.write_text("hello file")
    text, refs = expand_mentions(f"look at @run:demo and @file:{f}", tmp_path,
                                 alive_fn=lambda p: False, roots=[tmp_path])
    assert "[@run:demo]" in text and "hello file" in text
    kinds = {r["type"] for r in refs}
    assert kinds == {"run", "file"}
    # an unknown run is left as-is (no crash, no ref)
    text2, refs2 = expand_mentions("see @run:ghost", tmp_path)
    assert refs2 == [] and "@run:ghost" in text2


def test_normalize_mode():
    assert normalize_mode(None) == "plan"
    assert normalize_mode("bogus") == "plan"
    assert normalize_mode("auto") == "auto"


# --------------------------------------------------------------------------- run_turn
def test_run_turn_uses_read_tool_then_answers(tmp_path):
    # A run on disk so list_runs has something to find.
    rd = tmp_path / "demo"; rd.mkdir()
    (rd / "events.jsonl").write_text(
        '{"seq":0,"type":"run_started","data":{"run_id":"demo","task_id":"t","goal":"g","direction":"max"}}\n',
        encoding="utf-8")
    client = _FakeChatClient([_call("list_runs", {}), _final("I see one run: demo.")])
    res = run_turn(client, tmp_path, [], "what runs exist?", "plan", alive_fn=lambda p: False)
    assert res["ok"] and res["reply"] == "I see one run: demo."
    # the tool step was recorded and the tool actually ran (list_runs result reached the model)
    assert any(s["tool"] == "list_runs" for s in res["steps"])
    tool_msgs = [m for turn in client.turns for m in turn if m.get("role") == "tool"]
    assert any("demo" in (m.get("content") or "") for m in tool_msgs)


def test_run_turn_soft_fails_on_client_error(tmp_path):
    class _Boom:
        def chat(self, *a, **k):
            raise RuntimeError("no endpoint")
    res = run_turn(_Boom(), tmp_path, [], "hi", "plan")
    assert res["ok"] is False
    assert res["error_kind"] == res["error"] == "provider_error"
    assert "no endpoint" not in res["reply"]


def test_assistant_failure_never_persists_provider_payload():
    failure = safe_assistant_failure(RuntimeError(
        "429 Client Error https://provider.example/model/private user_id=secret-user"))
    assert failure["error_kind"] == failure["error"] == "rate_limit"
    rendered = json.dumps(failure)
    assert "provider.example" not in rendered
    assert "secret-user" not in rendered

    legacy = sanitize_assistant_message({
        "role": "assistant",
        "content": "Couldn't reach the model (AuthenticationError secret-key user-77)",
    })
    assert legacy["error_kind"] == "credentials"
    assert "secret-key" not in legacy["content"]
    assert "user-77" not in legacy["content"]


def test_shared_route_sanitizes_legacy_provider_failure():
    """The PUBLIC (untokened) share projection must strip provider metadata from a legacy failure
    bubble exactly like the owner GET does. redact_secrets alone leaves the request URL / routed
    model / account id, so the share route must compose sanitize_assistant_message before its
    read-only projection (the owner route already did; the share route did not)."""
    from looplab.serve.routers.assistant import _shared_message
    legacy = {
        "role": "assistant",
        "content": ("Couldn't reach the model (APIConnectionError: request to "
                    "https://api.provider.example/v1/messages model=claude-secret acct=acct-42)"),
    }
    out = _shared_message(sanitize_assistant_message(legacy))
    rendered = json.dumps(out)
    for leak in ("provider.example", "claude-secret", "acct-42", "v1/messages"):
        assert leak not in rendered, leak


def test_run_turn_write_with_approval(tmp_path):
    target = tmp_path / "new.txt"
    client = _FakeChatClient([_call("write_file", {"path": str(target), "content": "hi"}), _final("done")])
    res = run_turn(client, tmp_path, [], "make a file", "default", approver=lambda a: "allow_once")
    assert res["ok"] and target.read_text() == "hi"
    assert res["applied"] and res["applied"][0]["tool"] == "write_file"


def test_run_turn_write_declined(tmp_path):
    target = tmp_path / "no.txt"
    client = _FakeChatClient([_call("write_file", {"path": str(target), "content": "x"}), _final("ok")])
    res = run_turn(client, tmp_path, [], "make a file", "default", approver=lambda a: "deny")
    assert res["ok"] and not target.exists() and not res["applied"]


def test_run_turn_threads_commands_into_run_control_tools(tmp_path):
    """The Assistant tool loop submits lifecycle work and never appends the intent itself."""
    import uuid
    from looplab.events.eventstore import EventStore
    from looplab.serve.run_commands import run_generation_token

    rd = tmp_path / "demo"; rd.mkdir()
    events = rd / "events.jsonl"
    events.write_text(
        '{"seq":0,"type":"run_started","data":{"run_id":"demo","task_id":"t",'
        '"goal":"g","direction":"max"}}\n', encoding="utf-8")
    before = events.read_bytes()

    class Commands:
        def __init__(self):
            self.calls = []

        def run_generation(self, rd):
            return run_generation_token(EventStore(rd / "events.jsonl").read_all())

        def submit(self, rd, idempotency_key, event_type, data, *, expected_generation):
            self.calls.append(
                (rd.name, event_type, data, idempotency_key, expected_generation))
            return {"id": "assistant-cmd", "status": "executing", "event_type": event_type}

    commands = Commands()
    client = _FakeChatClient([_call("finalize_run", {"run_id": "demo"}),
                              _final("Finalization was requested.")])
    res = run_turn(client, tmp_path, [], "finalize demo", "auto",
                   alive_fn=lambda _p: False, command_service=commands)
    assert res["ok"] and commands.calls[0][0:3] == (
        "demo", "run_abort", {"reason": "finalized"})
    assert uuid.UUID(commands.calls[0][3])
    assert commands.calls[0][4] == commands.run_generation(rd)
    assert events.read_bytes() == before
    tool_messages = [m for turn in client.turns for m in turn if m.get("role") == "tool"]
    result = "\n".join(m.get("content") or "" for m in tool_messages)
    assert "requested/pending" in result and "completed" not in result


def test_run_turn_propose_run(tmp_path):
    # propose_run validates the task before proposing (so an unrunnable card is bounced) — a dataset
    # task's data path must EXIST, so point it at a real file under tmp_path.
    data = tmp_path / "train.csv"; data.write_text("a,b,y\n1,2,0\n")
    spec = {"run_id": "titanic-baseline", "task": {"kind": "dataset", "goal": "predict survival",
            "direction": "max", "data_path": str(data)}, "settings": {"max_nodes": 20},
            "setup_steps": ["Confirm the target column", "", "Pin the data version"]}
    client = _FakeChatClient([_call("propose_run", spec), _final("Proposed a titanic run.")])
    res = run_turn(client, tmp_path, [], "start a titanic run", "plan")
    assert res["ok"] and res["proposals"]
    p = res["proposals"][0]
    assert p["run_id"] == "titanic-baseline" and p["task"]["kind"] == "dataset"
    assert p["settings"]["max_nodes"] == 20
    assert uuid.UUID(p["proposal_id"])
    assert p["setup_steps"] == ["Confirm the target column", "Pin the data version"]


def test_plan_mode_has_no_write_tool(tmp_path):
    # In plan mode the mutating tools are dropped from the schema, so a write attempt is unknown.
    client = _FakeChatClient([_call("write_file", {"path": str(tmp_path / "x"), "content": "y"}),
                              _final("can't in plan mode")])
    res = run_turn(client, tmp_path, [], "write x", "plan")
    assert res["ok"] and not (tmp_path / "x").exists()
    tool_msgs = [m for turn in client.turns for m in turn if m.get("role") == "tool"]
    assert any("unknown tool" in (m.get("content") or "") for m in tool_msgs)


def test_cross_run_reads_present_in_every_mode_when_memory_configured(tmp_path):
    # The owner assistant gets the §22 cross-run concept/claims/atlas reads (advisory, portfolio-wide)
    # in EVERY mode when memory_dir + cross_run_read_tools are on — including read-only plan.
    from types import SimpleNamespace

    from looplab.serve.assistant import build_tools

    settings = SimpleNamespace(memory_dir=str(tmp_path / "mem"), cross_run_read_tools=True)
    for mode in ("plan", "auto"):
        tools = build_tools(tmp_path, mode=mode, settings=settings)
        assert "CrossRunTools" in [type(p).__name__ for p in tools.providers]
        names = {spec["function"]["name"] for spec in tools.specs()}
        assert {"cross_run_atlas", "cross_run_prior_attempts", "cross_run_claims"} <= names


def test_cross_run_reads_absent_when_flag_off_or_no_memory(tmp_path):
    from types import SimpleNamespace

    from looplab.serve.assistant import build_tools

    for settings in (
        SimpleNamespace(memory_dir=str(tmp_path / "mem"), cross_run_read_tools=False),  # flag off
        SimpleNamespace(cross_run_read_tools=True),                                      # no memory_dir
        None,                                                                            # no settings
    ):
        tools = build_tools(tmp_path, mode="plan", settings=settings)
        assert "CrossRunTools" not in [type(p).__name__ for p in tools.providers]


def test_concept_governance_tools_wired_read_in_plan_edit_in_mutating(tmp_path):
    # PART V §22.4 Phase 2: the owner assistant edits the shared concept taxonomy. Read (concept_taxonomy)
    # is present in plan; the mutation verbs appear only in mutating modes. Gated on memory_dir.
    from types import SimpleNamespace

    from looplab.serve.assistant import build_tools

    settings = SimpleNamespace(memory_dir=str(tmp_path / "mem"))
    auto = build_tools(tmp_path, mode="auto", settings=settings)
    assert "ConceptGovernanceTools" in [type(p).__name__ for p in auto.providers]
    anames = {s["function"]["name"] for s in auto.specs()}
    assert {"concept_taxonomy", "concept_merge", "concept_purge", "concept_split",
            "concept_edit_clear"} <= anames
    plan = build_tools(tmp_path, mode="plan", settings=settings)
    pnames = {s["function"]["name"] for s in plan.specs()}
    assert "concept_taxonomy" in pnames                                   # inspect the taxonomy in plan
    assert "concept_merge" not in pnames and "concept_purge" not in pnames  # but never edit it
    none = build_tools(tmp_path, mode="auto", settings=SimpleNamespace())  # no memory_dir
    assert "ConceptGovernanceTools" not in [type(p).__name__ for p in none.providers]


def test_recovered_turn_exposes_only_reads_todo_and_journaled_run_control(tmp_path):
    """Lost model traces cannot safely replay any mutator outside the durable run-command journal."""
    from types import SimpleNamespace

    from looplab.serve.assistant import build_tools

    tools = build_tools(
        tmp_path, mode="auto", client=object(), subagents=True, mcp=True,
        settings=SimpleNamespace(knowledge_dir=str(tmp_path / "kb")),
        command_service=object(), command_key_namespace="session-a:turn-a",
        mutation_journal_path=tmp_path / "turn-a.json", mutation_recovery=True)

    assert [type(provider).__name__ for provider in tools.providers] == [
        "RepoScoutTools", "MachineRunsTools", "RunControlTools", "TodoTools"]
    names = {spec["function"]["name"] for spec in tools.specs()}
    assert {"read_file", "list_runs", "write_todos", "extend_budget"} <= names
    assert names.isdisjoint({
        "propose_run", "write_file", "edit_file", "apply_patch", "delete_file",
        "run_command", "run_tests", "git_add", "git_commit", "git_checkout",
        "remember", "task",
    })


def test_recovered_turn_without_journal_identity_has_no_run_control(tmp_path):
    """Recovery never degrades from a missing fence identity to an ordinary mutable provider."""
    from looplab.serve.assistant import build_tools

    for kwargs in (
        {"command_key_namespace": "session-a:turn-a"},
        {"mutation_journal_path": tmp_path / "turn-a.json"},
    ):
        tools = build_tools(tmp_path, mode="auto", mutation_recovery=True, **kwargs)
        assert [type(provider).__name__ for provider in tools.providers] == [
            "RepoScoutTools", "MachineRunsTools", "TodoTools"]
        names = {spec["function"]["name"] for spec in tools.specs()}
        assert "extend_budget" not in names and "write_file" not in names


# --------------------------------------------------------------------------- HTTP routes
def test_assistant_endpoints_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr("looplab.serve.server.make_llm_client",
                        lambda s: _FakeChatClient([_call("list_runs", {}), _final("done — no runs.")]))
    client = TestClient(make_app(tmp_path))

    # create + list
    sid = client.post("/api/assistant/sessions", json={"title": "t"}).json()["id"]
    assert any(s["id"] == sid for s in client.get("/api/assistant/sessions").json()["sessions"])

    # a turn: fast fake -> returns inline (not a job_id)
    r = client.post(f"/api/assistant/sessions/{sid}/message",
                    json={"instruction": "hello", "mode": "plan"}).json()
    assert r.get("ok") and r["reply"] == "done — no runs."

    # persisted: user + assistant turns
    msgs = client.get(f"/api/assistant/sessions/{sid}").json()["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[1]["content"] == "done — no runs."

    # fork + delete
    child = client.post(f"/api/assistant/sessions/{sid}/fork").json()
    assert child["parent"] == sid
    assert client.delete(f"/api/assistant/sessions/{sid}").json()["ok"]
    assert client.get(f"/api/assistant/sessions/{sid}").status_code == 404


def test_assistant_router_passes_the_app_command_service(tmp_path, monkeypatch):
    """Both HTTP turn layers share the app-owned command service with ``run_turn``."""
    monkeypatch.setattr("looplab.serve.server.make_llm_client", lambda _s: _FakeChatClient([]))
    seen = []
    namespaces = []

    def fake_run_turn(_client, _root, _history, _instruction, mode, **kwargs):
        seen.append(kwargs.get("command_service"))
        namespaces.append(kwargs.get("command_key_namespace"))
        return {"ok": True, "reply": "ok", "steps": [], "applied": [], "proposals": [],
                "todos": [], "refs": [], "mode": mode}

    monkeypatch.setattr("looplab.serve.routers.assistant._assistant_run_turn", fake_run_turn)
    client = TestClient(make_app(tmp_path))
    sid = client.post("/api/assistant/sessions", json={"mode": "plan"}).json()["id"]
    result = client.post(f"/api/assistant/sessions/{sid}/message",
                         json={"instruction": "hello", "mode": "plan"}).json()
    assert result.get("ok") is True
    streamed = client.post(f"/api/assistant/sessions/{sid}/message_stream",
                           json={"instruction": "again", "mode": "plan"})
    assert streamed.status_code == 200 and "event: done" in streamed.text
    assert len(seen) == 2 and seen[0] is seen[1] and seen[0] is not None
    assert all(value and value.startswith(f"{sid}:") for value in namespaces)
    assert namespaces[0] != namespaces[1]


def test_dangling_user_turn_reuses_command_namespace_without_duplicate_append(tmp_path, monkeypatch):
    monkeypatch.setattr("looplab.serve.server.make_llm_client", lambda _s: _FakeChatClient([]))
    observed = []

    def fake_run_turn(_client, _root, history, instruction, mode, **kwargs):
        observed.append((history, instruction, kwargs.get("command_key_namespace"),
                         kwargs.get("mutation_journal_path"), kwargs.get("mutation_recovery")))
        return {"ok": True, "reply": "recovered", "steps": [], "applied": [],
                "proposals": [], "todos": [], "refs": [], "mode": mode}

    monkeypatch.setattr("looplab.serve.routers.assistant._assistant_run_turn", fake_run_turn)
    client = TestClient(make_app(tmp_path))
    sid = client.post("/api/assistant/sessions", json={"mode": "auto"}).json()["id"]
    SessionStore(tmp_path).append(sid, {
        "role": "user", "content": "extend the budget", "mode": "auto", "turn_id": "fixed-turn",
    })

    result = client.post(
        f"/api/assistant/sessions/{sid}/message",
        json={"instruction": "extend the budget", "mode": "auto"}).json()

    assert result["ok"] is True
    expected_journal = SessionStore(tmp_path).mutation_journal_path(sid, "fixed-turn")
    assert observed == [([], "extend the budget", f"{sid}:fixed-turn", expected_journal, True)]
    messages = client.get(f"/api/assistant/sessions/{sid}").json()["messages"]
    assert [message["role"] for message in messages] == ["user", "assistant"]


def test_dangling_turn_pins_persisted_raw_instruction_and_mode(tmp_path, monkeypatch):
    monkeypatch.setattr("looplab.serve.server.make_llm_client", lambda _s: _FakeChatClient([]))
    observed = []

    def fake_run_turn(_client, _root, history, instruction, mode, **kwargs):
        observed.append((history, instruction, mode, kwargs.get("mutation_recovery")))
        return {"ok": True, "reply": "recovered", "steps": [], "applied": [],
                "proposals": [], "todos": [], "refs": [], "mode": mode}

    monkeypatch.setattr("looplab.serve.routers.assistant._assistant_run_turn", fake_run_turn)
    client = TestClient(make_app(tmp_path))
    sid = client.post("/api/assistant/sessions", json={"mode": "auto"}).json()["id"]
    raw = "[persisted hidden context]\nextend the budget"
    SessionStore(tmp_path).append(sid, {
        "role": "user", "content": "extend the budget", "raw": raw,
        "mode": "default", "turn_id": "fixed-raw-turn",
    })

    result = client.post(
        f"/api/assistant/sessions/{sid}/message",
        json={"instruction": raw, "display": "extend the budget"}).json()

    assert result["ok"] is True
    assert observed == [([], raw, "default", True)]
    assert client.get(f"/api/assistant/sessions/{sid}").json()["meta"]["mode"] == "default"


def test_dangling_turn_rejects_raw_or_mode_mismatch(tmp_path, monkeypatch):
    monkeypatch.setattr("looplab.serve.server.make_llm_client", lambda _s: _FakeChatClient([]))
    called = []

    def fake_run_turn(*_args, **_kwargs):
        called.append(True)
        raise AssertionError("a mismatched recovery must not reach the model")

    monkeypatch.setattr("looplab.serve.routers.assistant._assistant_run_turn", fake_run_turn)
    client = TestClient(make_app(tmp_path))
    original_raw = "[context-v1]\nextend the budget"

    sid_raw = client.post("/api/assistant/sessions", json={"mode": "default"}).json()["id"]
    SessionStore(tmp_path).append(sid_raw, {
        "role": "user", "content": "extend the budget", "raw": original_raw,
        "mode": "default", "turn_id": "fixed-raw-turn",
    })
    raw_response = client.post(
        f"/api/assistant/sessions/{sid_raw}/message",
        json={"instruction": "[context-v2]\nextend the budget",
              "display": "extend the budget", "mode": "default"})
    assert raw_response.status_code == 409
    assert raw_response.json()["detail"] == {
        "code": "assistant_turn_recovery_mismatch", "field": "instruction",
        "message": "Recovery must use the exact persisted instruction and permission mode.",
    }

    sid_mode = client.post("/api/assistant/sessions", json={"mode": "default"}).json()["id"]
    SessionStore(tmp_path).append(sid_mode, {
        "role": "user", "content": "extend the budget", "raw": original_raw,
        "mode": "default", "turn_id": "fixed-mode-turn",
    })
    mode_response = client.post(
        f"/api/assistant/sessions/{sid_mode}/message",
        json={"instruction": original_raw, "display": "extend the budget", "mode": "auto"})
    assert mode_response.status_code == 409
    assert mode_response.json()["detail"]["code"] == "assistant_turn_recovery_mismatch"
    assert mode_response.json()["detail"]["field"] == "mode"
    assert called == []


def test_dangling_user_turn_blocks_different_next_message(tmp_path, monkeypatch):
    """U2 cannot turn an unanswered, possibly-mutating U1 into ordinary model history."""
    monkeypatch.setattr("looplab.serve.server.make_llm_client", lambda _s: _FakeChatClient([]))
    called = []

    def fake_run_turn(*_args, **_kwargs):
        called.append(True)
        raise AssertionError("a different U2 must not reach the model")

    monkeypatch.setattr("looplab.serve.routers.assistant._assistant_run_turn", fake_run_turn)
    client = TestClient(make_app(tmp_path))
    sid = client.post("/api/assistant/sessions", json={"mode": "auto"}).json()["id"]
    SessionStore(tmp_path).append(sid, {
        "role": "user", "content": "extend the budget", "mode": "auto", "turn_id": "fixed-turn",
    })

    response = client.post(
        f"/api/assistant/sessions/{sid}/message",
        json={"instruction": "do something else", "mode": "auto"})

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "assistant_turn_recovery_required"
    assert called == []
    messages = client.get(f"/api/assistant/sessions/{sid}").json()["messages"]
    assert [message["content"] for message in messages] == ["extend the budget"]


def test_cancel_keeps_turn_slot_until_worker_releases(tmp_path, monkeypatch):
    """Stop marks a turn as stopping; an immediate U2 is 409 until the old worker's finally runs."""
    import threading
    import time

    monkeypatch.setenv("LOOPLAB_JOB_INLINE_WAIT", "0.01")
    monkeypatch.setattr("looplab.serve.server.make_llm_client", lambda _s: _FakeChatClient([]))
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def fake_run_turn(_client, _root, _history, instruction, mode, **_kwargs):
        calls.append(instruction)
        entered.set()
        assert release.wait(timeout=5)
        return {"ok": True, "reply": "stopped safely", "steps": [], "applied": [],
                "proposals": [], "todos": [], "refs": [], "mode": mode}

    monkeypatch.setattr("looplab.serve.routers.assistant._assistant_run_turn", fake_run_turn)
    client = TestClient(make_app(tmp_path))
    sid = client.post("/api/assistant/sessions", json={"mode": "auto"}).json()["id"]
    try:
        first = client.post(
            f"/api/assistant/sessions/{sid}/message",
            json={"instruction": "extend by ten", "mode": "auto"}).json()
        assert first.get("status") == "running"
        assert entered.wait(timeout=2)
        assert client.post(f"/api/assistant/sessions/{sid}/cancel").json()["cancelling"] is True

        second = client.post(
            f"/api/assistant/sessions/{sid}/message",
            json={"instruction": "now do something else", "mode": "auto"})
        assert second.status_code == 409
        assert calls == ["extend by ten"]
        messages = client.get(f"/api/assistant/sessions/{sid}").json()["messages"]
        assert [message["content"] for message in messages] == ["extend by ten"]
    finally:
        release.set()

    for _ in range(100):
        messages = client.get(f"/api/assistant/sessions/{sid}").json()["messages"]
        if [message["role"] for message in messages] == ["user", "assistant"]:
            break
        time.sleep(0.02)
    assert [message["role"] for message in messages] == ["user", "assistant"]


def test_assistant_permission_pause_resume(tmp_path, monkeypatch):
    """default mode: a write blocks on a permission request; resolving it unblocks the turn thread and
    the file is written (true mid-loop human-in-the-loop, not an 'assume applied' buffer)."""
    import time
    monkeypatch.setenv("LOOPLAB_JOB_INLINE_WAIT", "0.3")     # return {running} fast; the turn blocks on approval
    target = tmp_path / "made.txt"
    monkeypatch.setattr("looplab.serve.server.make_llm_client",
                        lambda s: _FakeChatClient([_call("write_file", {"path": str(target), "content": "yo"}),
                                                   _final("wrote it")]))
    client = TestClient(make_app(tmp_path))
    sid = client.post("/api/assistant/sessions", json={"mode": "default"}).json()["id"]
    resp = client.post(f"/api/assistant/sessions/{sid}/message",
                       json={"instruction": "make it", "mode": "default"}).json()
    assert resp.get("status") == "running" and resp.get("job_id")

    # a permission request appears; approve it
    req = None
    for _ in range(100):
        pend = client.get(f"/api/assistant/permissions?session={sid}").json()["pending"]
        if pend:
            req = pend[0]; break
        time.sleep(0.1)
    assert req and req["action"]["tool"] == "write_file"
    assert client.post(f"/api/assistant/permissions/{req['id']}", json={"decision": "allow_once"}).json()["ok"]

    # the job now completes and the file is on disk
    result = None
    for _ in range(100):
        j = client.get(f"/api/jobs/{resp['job_id']}").json()
        if j.get("status") == "done":
            result = j; break        # GET /api/jobs spreads the result at top level
        time.sleep(0.1)
    assert result and result["reply"] == "wrote it"
    assert target.read_text() == "yo"


def test_stale_permission_resolve_is_rejected(tmp_path, monkeypatch):
    """arch-review §3 P0-6 CAS: once a permission request is resolved (approved, or auto-denied on
    cancel), a stale/duplicate resolve returns 409 instead of overwriting the decision — so a resolve
    racing a cancel can't flip a denied request back to allow and fire the cancelled mutation."""
    import time
    monkeypatch.setenv("LOOPLAB_JOB_INLINE_WAIT", "0.3")
    target = tmp_path / "made2.txt"
    monkeypatch.setattr("looplab.serve.server.make_llm_client",
                        lambda s: _FakeChatClient([_call("write_file", {"path": str(target), "content": "z"}),
                                                   _final("done")]))
    client = TestClient(make_app(tmp_path))
    sid = client.post("/api/assistant/sessions", json={"mode": "default"}).json()["id"]
    client.post(f"/api/assistant/sessions/{sid}/message",
                json={"instruction": "make it", "mode": "default"})
    req = None
    for _ in range(100):
        pend = client.get(f"/api/assistant/permissions?session={sid}").json()["pending"]
        if pend:
            req = pend[0]; break
        time.sleep(0.1)
    assert req
    # first resolve succeeds
    assert client.post(f"/api/assistant/permissions/{req['id']}",
                       json={"decision": "allow_once"}).status_code == 200
    # a second (stale) resolve is rejected — the decision is already committed
    assert client.post(f"/api/assistant/permissions/{req['id']}",
                       json={"decision": "deny"}).status_code == 409


def test_allow_always_is_exact_scope_mode_and_current_turn_only(tmp_path, monkeypatch):
    """A remembered grant bypasses only the same action/scope; a new target asks again."""
    import time

    monkeypatch.setenv("LOOPLAB_JOB_INLINE_WAIT", "0.01")
    first = tmp_path / "same.txt"
    second = tmp_path / "other.txt"
    first.write_text("same", encoding="utf-8")
    second.write_text("same", encoding="utf-8")
    scripted = [
        _call("write_file", {"path": str(first), "content": "same"}),
        _call("write_file", {"path": str(first), "content": "same"}),
        _call("write_file", {"path": str(second), "content": "same"}),
        _final("done"),
    ]
    monkeypatch.setattr(
        "looplab.serve.server.make_llm_client", lambda _s: _FakeChatClient(scripted))
    client = TestClient(make_app(tmp_path))
    sid = client.post("/api/assistant/sessions", json={"mode": "default"}).json()["id"]
    submitted = client.post(
        f"/api/assistant/sessions/{sid}/message",
        json={"instruction": "write twice then another", "mode": "default"}).json()
    assert submitted["status"] == "running"

    first_req = None
    for _ in range(100):
        pending = client.get(f"/api/assistant/permissions?session={sid}").json()["pending"]
        if pending:
            first_req = pending[0]
            break
        time.sleep(0.02)
    assert first_req
    action = first_req["action"]
    assert action["risk"] == "REVERSIBLE" and action["rememberable"] is True
    assert len(action["scope_digest"]) == 64 and action["scope"]["path"]
    assert first_req["mode"] == "default" and len(first_req["epoch"]) == 16
    assert first_req["expires_at"] > first_req["created"]
    assert first_req["grant_ttl_seconds"] == 600
    assert client.post(
        f"/api/assistant/permissions/{first_req['id']}",
        json={"decision": "allow_always"}).status_code == 200

    # The exact second call consumes the grant without a card. The different path must surface one.
    mismatch = None
    for _ in range(100):
        pending = client.get(f"/api/assistant/permissions?session={sid}").json()["pending"]
        if pending and pending[0]["id"] != first_req["id"]:
            mismatch = pending[0]
            break
        time.sleep(0.02)
    assert mismatch and mismatch["action"]["scope_digest"] != action["scope_digest"]
    assert client.post(
        f"/api/assistant/permissions/{mismatch['id']}",
        json={"decision": "deny"}).status_code == 200

    for _ in range(100):
        result = client.get(f"/api/jobs/{submitted['job_id']}").json()
        if result.get("status") == "done":
            break
        time.sleep(0.02)
    assert result.get("status") == "done"


def test_high_action_asks_in_auto_and_cannot_be_remembered(tmp_path, monkeypatch):
    import time

    monkeypatch.setenv("LOOPLAB_JOB_INLINE_WAIT", "0.01")
    target = tmp_path / "delete-me.txt"
    target.write_text("x", encoding="utf-8")
    monkeypatch.setattr(
        "looplab.serve.server.make_llm_client",
        lambda _s: _FakeChatClient([
            _call("delete_file", {"path": str(target)}), _final("deleted")]))
    client = TestClient(make_app(tmp_path))
    sid = client.post("/api/assistant/sessions", json={"mode": "auto"}).json()["id"]
    submitted = client.post(
        f"/api/assistant/sessions/{sid}/message",
        json={"instruction": "delete it", "mode": "auto"}).json()
    assert submitted["status"] == "running"

    req = None
    for _ in range(100):
        pending = client.get(f"/api/assistant/permissions?session={sid}").json()["pending"]
        if pending:
            req = pending[0]
            break
        time.sleep(0.02)
    assert req and req["action"]["risk"] == "HIGH"
    assert req["action"]["rememberable"] is False
    forbidden = client.post(
        f"/api/assistant/permissions/{req['id']}", json={"decision": "allow_always"})
    assert forbidden.status_code == 400
    assert forbidden.json()["detail"]["code"] == "permission_not_rememberable"
    assert client.get(f"/api/assistant/permissions?session={sid}").json()["pending"]
    assert client.post(
        f"/api/assistant/permissions/{req['id']}",
        json={"decision": "allow_once"}).status_code == 200

    for _ in range(100):
        result = client.get(f"/api/jobs/{submitted['job_id']}").json()
        if result.get("status") == "done":
            break
        time.sleep(0.02)
    assert result.get("status") == "done" and not target.exists()


def test_unknown_action_cannot_be_remembered_and_malformed_resolve_is_400(tmp_path, monkeypatch):
    import time

    monkeypatch.setenv("LOOPLAB_JOB_INLINE_WAIT", "0.01")
    monkeypatch.setattr("looplab.serve.server.make_llm_client", lambda _s: _FakeChatClient([]))

    def fake_run_turn(_client, _root, _history, _instruction, mode, **kwargs):
        verdict = kwargs["approver"]({
            "tool": "future_action", "tool_kind": "unregistered_provider",
            "label": "unknown action", "preview": "opaque capability",
            "scope": {"target": "external-service", "api_key": "sk-abcdefghijklmnopq"}})
        return {"ok": True, "reply": verdict, "steps": [], "applied": [], "proposals": [],
                "todos": [], "refs": [], "mode": mode}

    monkeypatch.setattr("looplab.serve.routers.assistant._assistant_run_turn", fake_run_turn)
    client = TestClient(make_app(tmp_path))
    sid = client.post("/api/assistant/sessions", json={"mode": "auto"}).json()["id"]
    submitted = client.post(
        f"/api/assistant/sessions/{sid}/message",
        json={"instruction": "try future action", "mode": "auto"}).json()
    assert submitted["status"] == "running"

    req = None
    for _ in range(100):
        pending = client.get(f"/api/assistant/permissions?session={sid}").json()["pending"]
        if pending:
            req = pending[0]
            break
        time.sleep(0.02)
    assert req and req["action"]["risk"] == "UNKNOWN"
    assert req["action"]["scope"]["target"] == "external-service"
    assert "api_key" not in req["action"]["scope"]
    assert "sk-abcdefghijklmnopq" not in json.dumps(req)
    malformed = client.post(
        f"/api/assistant/permissions/{req['id']}",
        content="[]", headers={"Content-Type": "application/json"})
    assert malformed.status_code == 400
    forbidden = client.post(
        f"/api/assistant/permissions/{req['id']}", json={"decision": "allow_always"})
    assert forbidden.status_code == 400
    assert client.post(
        f"/api/assistant/permissions/{req['id']}",
        json={"decision": "allow_once"}).status_code == 200


def test_assistant_message_soft_fails_offline(tmp_path, monkeypatch):
    def _boom(s):
        raise RuntimeError("connection refused")
    monkeypatch.setattr("looplab.serve.server.make_llm_client", _boom)
    client = TestClient(make_app(tmp_path))
    sid = client.post("/api/assistant/sessions", json={}).json()["id"]
    r = client.post(f"/api/assistant/sessions/{sid}/message", json={"instruction": "hi"}).json()
    assert r["ok"] is False and r["error_kind"] == r["error"] == "unavailable"
    assert "connection refused" not in r["reply"]
    # the failure reply is still persisted so the transcript isn't lost
    msgs = client.get(f"/api/assistant/sessions/{sid}").json()["messages"]
    assert msgs[-1]["role"] == "assistant"
    assert msgs[-1]["error_kind"] == "unavailable"
    assert "connection refused" not in msgs[-1]["content"]


# --- SessionStore concurrency + subagent cancel (mega-review fixes) -------------------------------

def test_append_if_len_rejects_stale_reply(tmp_path):
    from looplab.serve.assistant import SessionStore
    store = SessionStore(tmp_path)
    meta = store.create(title="t")
    sid = meta["id"]
    store.append(sid, {"role": "user", "content": "u1"})
    # Transcript now has 1 message; a reply expecting len==1 appends...
    assert store.append_if_len(sid, {"role": "assistant", "content": "a1"}, expected_len=1) is True
    # ...but a late reply that still expects len==1 is rejected (a newer turn advanced the length).
    assert store.append_if_len(sid, {"role": "assistant", "content": "stale"}, expected_len=1) is False
    contents = [m["content"] for m in store.messages(sid)]
    assert contents == ["u1", "a1"]                            # stale reply not interleaved


def test_update_meta_is_serialized(tmp_path):
    """Concurrent meta writes must not drop each other's fields (share flag vs updated-ts race)."""
    import threading
    from looplab.serve.assistant import SessionStore
    store = SessionStore(tmp_path)
    sid = store.create(title="t")["id"]

    def _bump():
        for _ in range(50):
            store.update_meta(sid, updated=1.0)

    def _share():
        for _ in range(50):
            store.update_meta(sid, shared=True)

    ts = [threading.Thread(target=_bump), threading.Thread(target=_share)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert store._read_meta(sid).get("shared") is True         # share flag survived the ts bumps


def test_subagent_task_honors_cancel(tmp_path):
    """Stop must short-circuit a delegated `task` subagent, not let it run its full budget."""
    from looplab.serve.assistant import SubagentTools

    class _Client:  # never actually called — cancel fires first
        def chat(self, *a, **k):
            raise AssertionError("subagent ran despite cancel")

    st = SubagentTools(_Client(), tmp_path, cancel_check=lambda: True)
    out = st.execute("task", {"prompt": "do something big"})
    assert "cancel" in out.lower()


def test_assistant_and_genesis_reject_non_object_body_with_400(tmp_path):
    """R8-A6: a valid-JSON but NON-object body (a bare list) must yield a clean 400, not a 500 from a
    later ``body.get(...)`` — the guard the sibling permission-resolve endpoint already had, now shared
    across the assistant turn/revert/session-create and genesis research/genesis endpoints."""
    client = TestClient(make_app(tmp_path))
    sid = client.post("/api/assistant/sessions", json={"title": "t"}).json()["id"]
    for method, url in [
        ("post", f"/api/assistant/sessions/{sid}/message"),
        ("post", "/api/assistant/revert"),
        ("post", "/api/assistant/sessions"),
        ("post", "/api/research"),
        ("post", "/api/genesis"),
    ]:
        r = client.post(url, json=[])           # valid JSON, not an object
        assert r.status_code == 400, f"{url} -> {r.status_code} (expected 400): {r.text[:200]}"
