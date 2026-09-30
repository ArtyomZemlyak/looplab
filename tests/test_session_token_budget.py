"""The agent session's TOKEN ceiling (doc 69 §3.2, 69.2): `drive_tool_loop(token_budget=)`.

The run this answers: `minionerec-backbones-v10` ran on an unpriced gateway (`cost = 0`), so the
money ceiling could not fire; its seven plan phases committed 95.4 M tokens, 48 % of the run, while a
cached prefix kept turns at ~2 s and the 1200 s wall never bit. The ceiling is the money ceiling's
rule in tokens, counted on the loop's own THREAD — the run's accountant is shared by every
concurrent session, so a delta over it would count a parallel build's tokens against this one.

Offline: fake clients that commit their usage through a REAL `CostAccountant`, no model.
"""
from __future__ import annotations

import json
import threading

import pytest

from looplab.agents.agent import drive_tool_loop, loop_opts_from_settings
from looplab.core.config import Settings
from looplab.core.errors import BudgetExceeded
from looplab.core.llm import CostAccountant
from looplab.core.llm_budget import note_committed_tokens, thread_committed_tokens
from looplab.core.phase_events import PHASE_COMPLETED, PHASE_STARTED, phase_sink_scope

_EMIT = {"type": "function", "function": {
    "name": "emit", "description": "final",
    "parameters": {"type": "object", "properties": {"answer": {"type": "string"}}}}}


class _Tools:
    def specs(self):
        return [{"type": "function", "function": {
            "name": "read_file", "description": "",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}}]

    def execute(self, name, args):
        return f"contents of {args.get('path')}"


class _Reader:
    """A model that reads a DIFFERENT file every turn (so no stuck rule ever fires) and emits on
    turn `emit_at`; every call commits `per_call` tokens through the accountant it was handed, as
    `OpenAICompatibleClient._post` does. `complete_tool` is the forced emit a cutoff salvages with."""

    def __init__(self, accountant, *, per_call: int, emit_at: int = 10_000, gate=None):
        self.accountant = accountant
        self.per_call, self.emit_at, self.gate = per_call, emit_at, gate
        self.turn = 0
        self.forced = 0

    def _bill(self):
        self.accountant.add(0.0, usage={"prompt_tokens": self.per_call - 1,
                                        "completion_tokens": 1, "total_tokens": self.per_call})

    def chat(self, messages, tools, tool_choice="auto"):
        if self.gate is not None:
            self.gate(self.turn)
        self._bill()
        self.turn += 1
        if self.turn >= self.emit_at:
            return {"content": "", "tool_calls": [{"id": f"e{self.turn}", "function": {
                "name": "emit", "arguments": json.dumps({"answer": "considered"})}}]}
        return {"content": "", "tool_calls": [{"id": f"r{self.turn}", "function": {
            "name": "read_file", "arguments": json.dumps({"path": f"src/m{self.turn}.py"})}}]}

    def complete_tool(self, messages, schema):
        self._bill()
        self.forced += 1
        return {"answer": "salvaged"}


def _drive(client, **opts):
    cutoffs: list[dict] = []
    rows: list = []
    with phase_sink_scope(lambda etype, data: rows.append((etype, data))):
        out = drive_tool_loop(client, _Tools(), [{"role": "user", "content": "go"}], _EMIT,
                              finalize=lambda a: a.get("answer"), fallback=lambda _m: "fallback",
                              on_budget=cutoffs.append, self_plan=False, **opts)
    started = [d for e, d in rows if e == PHASE_STARTED]
    completed = [d for e, d in rows if e == PHASE_COMPLETED]
    return out, cutoffs, started, completed


# ------------------------------------------------------------------------------ the counter
def test_the_counter_belongs_to_the_thread_that_committed():
    before = thread_committed_tokens()
    seen: dict = {}

    def _other():
        at = thread_committed_tokens()
        note_committed_tokens(50)
        seen["other"] = thread_committed_tokens() - at

    note_committed_tokens(100)
    t = threading.Thread(target=_other)
    t.start()
    t.join()
    assert thread_committed_tokens() - before == 100, "another thread's commit leaked into this one"
    assert seen["other"] == 50


@pytest.mark.parametrize("junk", [0, -5, None, "x", float("nan"), float("inf")])
def test_the_counter_ignores_what_is_not_a_positive_count(junk):
    before = thread_committed_tokens()
    note_committed_tokens(junk)                   # never raises: a counter may not fail a paid call
    assert thread_committed_tokens() == before


def test_a_real_commit_feeds_the_counter_even_when_the_ceiling_then_raises():
    """`CostAccountant.add` notes the call before its own ceiling can raise, like the span: the
    tokens were committed and billed whether or not the run then stops. MUTATION: move the note
    after the raise -> the over-ceiling call's tokens vanish from the session's count."""
    acct = CostAccountant()
    before = thread_committed_tokens()
    acct.add(0.0, usage={"prompt_tokens": 70, "completion_tokens": 30, "total_tokens": 100})
    assert thread_committed_tokens() - before == 100
    capped = CostAccountant(limit=0.01)
    with pytest.raises(BudgetExceeded):
        capped.add(0.02, usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10})
    assert thread_committed_tokens() - before == 110


# ------------------------------------------------------------------------------ the ceiling
def test_a_session_past_its_token_ceiling_is_salvaged_and_says_so():
    """250 tokens a turn against 1,000: turns 0-3 commit 1,000 (not past it), turn 4 commits 1,250
    and the NEXT turn does not start — the forced emit answers from what was gathered."""
    client = _Reader(CostAccountant(), per_call=250)
    out, cutoffs, _started, completed = _drive(client, token_budget=1_000)
    assert out == "salvaged" and client.forced == 1
    assert client.turn == 5, "the ceiling is checked before each turn, on the money ceiling's `>`"
    assert [c["kind"] for c in cutoffs] == ["tokens"]
    assert cutoffs[0]["detail"] == "1,250 of 1,000 tokens for this session"
    (row,) = completed
    assert row["exit"] == "salvaged" and row["cutoff"] == "tokens"
    assert row["tokens"] == 1_500, "the forced emit is the session's too"


def test_off_by_default_and_at_zero():
    client = _Reader(CostAccountant(), per_call=10_000, emit_at=6)
    out, cutoffs, started, completed = _drive(client)
    assert out == "considered" and cutoffs == [] and client.forced == 0
    assert "token_budget" not in started[0], "an unbounded phase writes its old started row"
    assert completed[0]["tokens"] == 60_000 and "cutoff" not in completed[0]
    client = _Reader(CostAccountant(), per_call=10_000, emit_at=6)
    out, cutoffs, started, _completed = _drive(client, token_budget=0)
    assert out == "considered" and cutoffs == [] and "token_budget" not in started[0]


def test_the_ceiling_is_named_on_the_started_row_when_set():
    client = _Reader(CostAccountant(), per_call=1, emit_at=2)
    _out, _cutoffs, started, _completed = _drive(client, token_budget=5_000)
    assert started[0]["token_budget"] == 5_000


def test_a_session_under_its_ceiling_emits_on_its_own_terms():
    client = _Reader(CostAccountant(), per_call=100, emit_at=4)
    out, cutoffs, _started, completed = _drive(client, token_budget=400)
    # 3 reads + the emitting turn = 400 tokens, never PAST the ceiling before a turn starts.
    assert out == "considered" and cutoffs == [] and completed[0]["tokens"] == 400


def test_a_client_with_no_accountant_is_not_bounded_and_records_no_figure():
    """Off by absence, like the money ceiling: nothing such a client calls is committed anywhere, so
    a 0 would read as a session that used nothing. MUTATION: count it anyway -> `tokens: 0`."""

    class _NoAcct(_Reader):
        def _bill(self):
            note_committed_tokens(self.per_call)   # a commit this client's session cannot own

    client = _NoAcct(None, per_call=10_000, emit_at=5)
    del client.accountant
    out, cutoffs, _started, completed = _drive(client, token_budget=1)
    assert out == "considered" and cutoffs == []
    assert "tokens" not in completed[0]


def test_a_concurrent_session_on_the_same_accountant_never_counts_against_this_one():
    """The reason the count is the THREAD's: two loops sharing ONE run accountant, B paused after
    its first call while A commits 3,000 tokens and is cut. A delta over the accountant would read
    B at 3,010 against its 2,500 and cut it too. MUTATION: measure `accountant.total_tokens` ->
    B is salvaged at its second turn."""
    shared = CostAccountant()
    b_paused, a_done = threading.Event(), threading.Event()
    results: dict = {}

    def _b_gate(turn):                  # B has started and billed its first call before A starts
        if turn == 1:
            b_paused.set()
            assert a_done.wait(10), "A never finished"

    def _a_gate(turn):
        if turn == 0:
            assert b_paused.wait(10), "B never reached its second turn"

    def _run(name, client, budget):
        results[name] = _drive(client, token_budget=budget)

    b = _Reader(shared, per_call=10, emit_at=5, gate=_b_gate)
    a = _Reader(shared, per_call=1_000, gate=_a_gate)
    tb = threading.Thread(target=_run, args=("b", b, 2_500))
    tb.start()
    ta = threading.Thread(target=_run, args=("a", a, 2_500))
    ta.start()
    ta.join(10)
    a_done.set()
    tb.join(10)
    a_out, a_cut, _s, a_rows = results["a"]
    b_out, b_cut, _s, b_rows = results["b"]
    assert a_out == "salvaged" and [c["kind"] for c in a_cut] == ["tokens"]
    assert b_out == "considered" and b_cut == [] and b.forced == 0
    assert b_rows[0]["tokens"] == 50 and a_rows[0]["tokens"] == 4_000
    assert shared.total_tokens == 4_050, "premise: both sessions billed the one run accountant"


def test_a_nested_loop_counts_toward_the_session_that_ran_it():
    """A tool that drives a loop of its own on the same thread: its calls are the outer session's
    volume too, so a ceiling bounds what the session actually cost."""
    acct = CostAccountant()

    class _NestingTools(_Tools):
        def execute(self, name, args):
            inner = _Reader(acct, per_call=400, emit_at=2)
            return str(drive_tool_loop(inner, _Tools(), [{"role": "user", "content": "sub"}], _EMIT,
                                       finalize=lambda a: a.get("answer"),
                                       fallback=lambda _m: "fb", self_plan=False))

    outer = _Reader(acct, per_call=100)
    cutoffs: list = []
    out = drive_tool_loop(outer, _NestingTools(), [{"role": "user", "content": "go"}], _EMIT,
                          finalize=lambda a: a.get("answer"), fallback=lambda _m: "fallback",
                          on_budget=cutoffs.append, self_plan=False, token_budget=1_500)
    # Each outer turn: 100 own + 800 in the nested loop. After two turns 1,800 > 1,500.
    assert out == "salvaged" and outer.turn == 2
    assert cutoffs[0]["detail"] == "1,800 of 1,500 tokens for this session"


def test_every_cutoff_is_named_on_the_phase_row():
    """`cutoff` is stamped by the ONE reporting path, so a firing is countable on loops without an
    `on_budget` observer too — here the turn ceiling."""
    client = _Reader(CostAccountant(), per_call=1)
    _out, cutoffs, _started, completed = _drive(client, max_turns=3)
    assert [c["kind"] for c in cutoffs] == ["turns"] and completed[0]["cutoff"] == "turns"


# ------------------------------------------------------------------------------ the wiring
def test_the_settings_knob_reaches_the_bundle_only_when_set():
    assert loop_opts_from_settings(Settings(agent_token_budget=750_000))["token_budget"] == 750_000
    assert "token_budget" not in loop_opts_from_settings(Settings())
    assert Settings().agent_token_budget == 0


def test_a_developer_session_keeps_the_configured_ceiling():
    """The plan phase this was measured on is a repo-Developer session, whose `_session_opts`
    REPLACES its turn/time/cost ceilings on the configured bundle — the token ceiling must ride
    through that replace, not be reset by it."""
    from looplab.adapters.repo_developer import LLMRepoDeveloper

    dev = LLMRepoDeveloper.__new__(LLMRepoDeveloper)
    dev.loop_opts = loop_opts_from_settings(Settings(agent_token_budget=2_000_000))
    assert dev._session_opts()["token_budget"] == 2_000_000


def test_the_operator_is_told_which_knob_helps():
    from looplab.serve.assistant import cutoff_notice

    text = cutoff_notice({"kind": "tokens", "turns": 9, "seconds": 3.0,
                          "detail": "1,250 of 1,000 tokens for this session"})
    assert "reached the token ceiling for this session" in text
    assert "1,250 of 1,000 tokens" in text and "raise `agent_token_budget`" in text
    assert "assistant_time_budget_s" not in text


def test_the_repair_judge_reads_a_token_cut_as_a_cut():
    from looplab.engine.crash_repair import _format_repair_log

    row = dict(attempt=1, error="boom", fix="f", changed=[], verified="inert",
               budget_exhausted="tokens")
    assert "THE SESSION RAN OUT OF ITS TOKEN BUDGET" in _format_repair_log([row])
