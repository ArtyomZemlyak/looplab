"""Regression tests for the mega-review fixes (LLM client, parsing, budgeting, tools).

Each test pins a concrete defect the review found so it can't silently come back. Grouped by module.
"""
from __future__ import annotations

import json

import pytest
from pydantic import BaseModel

from looplab.core.llm import (
    OpenAICompatibleClient,
    _extract_native_tool_calls,
    _tool_call_slot,
)
from looplab.core.parse import ParseError, _coerce_value, parse_structured
from looplab.core.prompts import _strip_frontmatter
from looplab.core.context_budget import _msg_chars, compact_history


# --------------------------------------------------------------------------- llm: native tool-call
def test_native_tool_call_quoted_prose_is_not_executed():
    """Un-fenced prose that merely QUOTES the tool syntax must NOT be lifted into a real tool call
    (it would execute e.g. delete_file on documentation text)."""
    txt = ('To call a tool you write: invoke name="delete_file" with parameter name="arguments">'
           '{"path": "main.py"}</parameter> and close with </invoke>. That is the syntax.')
    calls, _clean = _extract_native_tool_calls(txt)
    assert calls is None


def test_native_tool_call_real_leak_is_recovered():
    """A genuine leaked native block (opening tag present) is still recovered."""
    leaked = ('<｜DSML｜invoke name="emit"><｜DSML｜parameter name="arguments">'
              '{"x": 1}</｜DSML｜parameter></｜DSML｜invoke>')
    calls, _clean = _extract_native_tool_calls(leaked)
    assert calls and calls[0]["function"]["name"] == "emit"
    assert json.loads(calls[0]["function"]["arguments"]) == {"x": 1}


# --------------------------------------------------------------------------- llm: stream reassembly
class _FakeResp:
    """A minimal iterable/closable stand-in for a urllib SSE response."""
    def __init__(self, lines):
        self._lines = lines
        self.fp = None

    def __iter__(self):
        return iter(self._lines)

    def close(self):
        pass


def _sse(*chunks):
    lines = [("data: " + json.dumps(c) + "\n").encode() for c in chunks]
    lines.append(b"data: [DONE]\n")
    return _FakeResp(lines)


def test_streamed_tool_calls_without_index_stay_separate():
    """Providers that omit `index` (one whole call per delta) must not collapse both calls into slot 0."""
    c = OpenAICompatibleClient("m", stream=True)
    resp = _sse(
        {"choices": [{"delta": {"tool_calls": [
            {"id": "a", "type": "function", "function": {"name": "f1", "arguments": '{"x":1}'}}]}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"id": "b", "type": "function", "function": {"name": "f2", "arguments": '{"y":2}'}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    )
    out = c._read_stream(resp)
    calls = out["choices"][0]["message"]["tool_calls"]
    assert [(t["function"]["name"], t["function"]["arguments"]) for t in calls] == [
        ("f1", '{"x":1}'), ("f2", '{"y":2}')]


def test_streamed_single_call_fragments_stay_merged():
    """Fragments of ONE call (id/name only on the first delta) must not split into two calls."""
    c = OpenAICompatibleClient("m", stream=True)
    resp = _sse(
        {"choices": [{"delta": {"tool_calls": [
            {"id": "a", "function": {"name": "f1", "arguments": '{"x":'}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"function": {"arguments": '1}'}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    )
    out = c._read_stream(resp)
    calls = out["choices"][0]["message"]["tool_calls"]
    assert len(calls) == 1 and calls[0]["function"]["arguments"] == '{"x":1}'


def test_tool_call_slot_prefers_explicit_index():
    assert _tool_call_slot({}, {"index": 3}) == 3
    assert _tool_call_slot({0: {"id": "a", "function": {"name": "f", "arguments": []}}},
                           {"index": 0}) == 0


def test_streamed_single_call_with_echoed_name_stays_merged():
    """A provider that ECHOES function.name on every continuation delta (no index/id) must not split
    one call into invalid-JSON fragments."""
    c = OpenAICompatibleClient("m", stream=True)
    resp = _sse(
        {"choices": [{"delta": {"tool_calls": [{"function": {"name": "emit", "arguments": '{"x":'}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"function": {"name": "emit", "arguments": '1}'}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    )
    calls = c._read_stream(resp)["choices"][0]["message"]["tool_calls"]
    assert len(calls) == 1 and calls[0]["function"]["arguments"] == '{"x":1}'


def test_streamed_two_indexless_noid_calls_split_on_complete_args():
    """Two distinct index-less, id-less calls (each with complete JSON args) must NOT merge into one."""
    c = OpenAICompatibleClient("m", stream=True)
    resp = _sse(
        {"choices": [{"delta": {"tool_calls": [{"function": {"name": "f1", "arguments": '{"a":1}'}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"function": {"name": "f2", "arguments": '{"b":2}'}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    )
    calls = c._read_stream(resp)["choices"][0]["message"]["tool_calls"]
    assert len(calls) == 2


# --------------------------------------------------------------------------- prompts: frontmatter
def test_frontmatter_keeps_markdown_horizontal_rules():
    """`---` used as Markdown rules must NOT be treated as a frontmatter fence (Section A vanished)."""
    t = "Rules for the researcher.\n---\nSection A: do X.\n---\nSection B: do Y.\n"
    out = _strip_frontmatter(t)
    assert "Section A" in out and "Section B" in out


def test_frontmatter_strips_real_leading_block():
    fm = "---\ntitle: x\nauthor: y\n---\nBody starts here.\n"
    assert _strip_frontmatter(fm).strip() == "Body starts here."


# --------------------------------------------------------------------------- parse: coercion safety
def test_coerce_infinite_float_to_int_does_not_raise():
    # round(inf) raises OverflowError; the coercer must return the raw value, not crash.
    assert _coerce_value("1e400", int) == "1e400"


def test_parse_structured_infinite_int_raises_parse_error_not_overflow():
    class M(BaseModel):
        choice: int

    class _Fake:
        def complete_tool(self, messages, schema):
            return {"choice": "1e400"}

        def complete_text(self, messages):
            return '{"choice": "1e400"}'

    with pytest.raises(ParseError):
        parse_structured(_Fake(), [{"role": "user", "content": "x"}], M, "tool_call")


# --------------------------------------------------------------------------- context budget
def test_budget_counts_tool_call_arguments():
    """A file-writing assistant turn holds its payload in tool_calls, not content — it must be counted."""
    m = {"role": "assistant", "content": "",
         "tool_calls": [{"function": {"name": "write_file", "arguments": '{"content":"' + "x" * 5000 + '"}'}}]}
    assert _msg_chars(m) >= 5000


def test_truncate_history_triggers_on_tool_call_heavy_trace():
    big = {"role": "assistant", "content": "",
           "tool_calls": [{"function": {"name": "write_file", "arguments": "A" * 4000}}]}
    msgs = [{"role": "system", "content": "task"},
            big, {"role": "tool", "content": "wrote"},
            big, {"role": "tool", "content": "wrote"},
            {"role": "user", "content": "recent"}]
    # Compaction summarizes the tool-call-heavy middle; the note is a de-privileged user message.
    out = compact_history(msgs, max_chars=2000, summarize=lambda _t: "SUMMARY", keep_last=2)
    note = next((m for m in out if "SUMMARY" in str(m.get("content"))), None)
    assert note is not None and note["role"] == "user"


# --------------------------------------------------------------------------- shell: git config creds
def test_git_config_env_drops_extraheader_credential_and_keeps_indices_contiguous(monkeypatch):
    from looplab.tools.shell_tools import git_config_env
    for k in list(__import__("os").environ):
        if k.startswith("GIT_CONFIG") or k.startswith("GIT_AUTHOR"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "3")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "credential.interactive")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "false")
    monkeypatch.setenv("GIT_CONFIG_KEY_1", "http.extraheader")
    monkeypatch.setenv("GIT_CONFIG_VALUE_1", "Authorization: Bearer SECRETTOKEN")
    monkeypatch.setenv("GIT_CONFIG_KEY_2", "url.https://github.com/.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_2", "git@github.com:")
    env = git_config_env()
    assert "SECRETTOKEN" not in json.dumps(env)                 # credential dropped
    n = int(env["GIT_CONFIG_COUNT"])
    assert n == 2                                               # count reflects survivors
    assert all(f"GIT_CONFIG_KEY_{i}" in env for i in range(n))  # indices contiguous (git needs this)
    keys = {env[f"GIT_CONFIG_KEY_{i}"] for i in range(n)}
    assert "credential.interactive" in keys                     # safe non-credential config kept


# --------------------------------------------------------------------------- sandbox: byte cap
def test_clamp_tail_bytes_respects_byte_budget_on_multibyte():
    from looplab.runtime.sandbox import _clamp_tail_bytes
    s = "世" * 100                                              # 300 UTF-8 bytes
    out = _clamp_tail_bytes(s, 90)
    assert len(out.encode("utf-8")) <= 90                       # a plain [-90:] would keep 270 bytes


# --------------------------------------------------------------------------- assistant: atomic append
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


# --------------------------------------------------------------------------- agent: loop resilience
def test_tool_researcher_propose_survives_transport_failure():
    """A transport failure (LLMError after retries) on the agentic path degrades to a safe Idea,
    it does NOT crash the run."""
    from looplab.agents.agent import ToolUsingResearcher
    from looplab.core.llm import LLMError
    from looplab.core.models import Idea, RunState

    class _DeadClient:
        def chat(self, *a, **k):
            raise LLMError("endpoint down")

        def complete_tool(self, *a, **k):
            raise LLMError("endpoint down")

        def complete_text(self, *a, **k):
            raise LLMError("endpoint down")

    class _Tools:
        def specs(self):
            return []

        def execute(self, name, args):
            return "(none)"

    r = ToolUsingResearcher(_DeadClient(), _Tools())
    idea = r.propose(RunState(run_id="r"), None)
    assert isinstance(idea, Idea)                              # degraded, not crashed


def test_force_emit_coerces_non_object_to_dict():
    """A forced emit returning a valid-but-non-object JSON ("[…]") must become {} so finalize's .get
    can't AttributeError."""
    from looplab.agents.agent import _force_emit

    class _Client:
        def complete_tool(self, messages, schema):
            return ["not", "a", "dict"]

    out = _force_emit(_Client(), [], {"function": {"parameters": {}}})
    assert out == {}


def test_force_emit_preserves_none_for_couldnt_force():
    """A client that RETURNS None (couldn't force a tool call) must stay None so the loop nudges +
    retries, not finalize on empty args."""
    from looplab.agents.agent import _force_emit

    class _Client:
        def complete_tool(self, messages, schema):
            return None

    assert _force_emit(_Client(), [], {"function": {"parameters": {}}}) is None


def test_git_config_env_shadows_stale_value_for_valueless_survivor(monkeypatch):
    """A renumbered survivor with no original value must emit an EMPTY VALUE so it shadows a stale
    GIT_CONFIG_VALUE_i (a dropped credential) the child inherits from the host env."""
    from looplab.tools.shell_tools import git_config_env
    for k in list(__import__("os").environ):
        if k.startswith("GIT_CONFIG") or k.startswith("GIT_AUTHOR"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "2")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "http.extraheader")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "Authorization: Bearer TOKEN")
    monkeypatch.setenv("GIT_CONFIG_KEY_1", "user.name")   # survivor WITHOUT a paired VALUE
    env = git_config_env()
    assert "TOKEN" not in json.dumps(env)
    n = int(env["GIT_CONFIG_COUNT"])
    for i in range(n):                                     # every kept index emits an authoritative VALUE
        assert f"GIT_CONFIG_VALUE_{i}" in env
