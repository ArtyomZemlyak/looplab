"""Parity tests for the DeepResearcher memo loop after it was folded onto the shared
`agent.drive_tool_loop` (instead of a hand-rolled clone). Offline (fake chat client), no model.

These pin the behaviors the old clone owned so the fold stays byte-identical: the historical
nudge/stuck wording, the consulted-sources ledger (title/url/snippet truncation), the 4000-char
tool-result cap, the malformed-args guard, the "(no tools)" observation, and the forced-memo
fallback when the turn budget runs out.
"""
from __future__ import annotations

import json

from looplab.agents.deep_research import _SYSTEM, DeepResearcher, state_brief
from looplab.core.models import RunState
from looplab.trust.source_identity import canonical_source_ref


class _FakeTools:
    """One search tool; records executed calls so tests can assert the parsed args."""

    def __init__(self, result: str = "ridge regression survey"):
        self.result = result
        self.calls: list[tuple] = []

    def specs(self):
        return [{"type": "function", "function": {
            "name": "search", "description": "search",
            "parameters": {"type": "object",
                           "properties": {"query": {"type": "string"}}}}}]

    def execute(self, name, args):
        self.calls.append((name, args))
        return self.result


class _FakeChatClient:
    """Scripts assistant messages; records the messages and tool specs it received each turn.
    Deliberately has NO `complete_tool`, so `_force_emit` fails over to the nudge path — the
    same shape the offline fakes in test_agentic_retrieval.py exercise."""

    def __init__(self, scripted):
        self.scripted = list(scripted)
        self.turns: list[list[dict]] = []
        self.specs_seen: list[list[str]] = []

    def chat(self, messages, tools, tool_choice="auto"):
        self.turns.append([dict(m) for m in messages])
        self.specs_seen.append([t["function"]["name"] for t in tools])
        return self.scripted.pop(0)


class _ForcingClient(_FakeChatClient):
    """Adds `complete_tool`, so the forced-emit paths (in-loop stuck stop, and the `_forced`
    fallback via parse_structured's tool_call parser) return a scripted memo dict."""

    def __init__(self, scripted, forced):
        super().__init__(scripted)
        self.forced = forced
        self.forced_calls: list[list[dict]] = []

    def complete_tool(self, messages, json_schema):
        self.forced_calls.append([dict(m) for m in messages])
        return self.forced


def _tool_call(name, args, call_id="c1"):
    return {"content": "", "tool_calls": [
        {"id": call_id, "function": {"name": name, "arguments": json.dumps(args)}}]}


def test_tool_then_stall_then_nudge_then_emit_message_parity():
    """tool call -> result -> prose stall -> nudge -> final emit: memo fields, sources ledger,
    and the EXACT message sequence sent to the client all match the pre-fold loop."""
    tools = _FakeTools(result="R" * 5000)               # long result: capped at 4000 in the trace
    emit_args = {"summary": "s", "reasoning": "r", "findings": ["f1"],
                 "claims": [{"statement": "c", "node_ids": [1], "urls": ["http://x"]}],
                 "recommended_directions": ["d1"]}
    client = _FakeChatClient([
        _tool_call("search", {"query": "ridge", "url": "http://a"}),
        {"content": "let me think in prose"},           # no complete_tool -> force fails -> nudge
        _tool_call("emit", emit_args, call_id="c2"),
    ])
    state = RunState(goal="g")
    memo = DeepResearcher(client, tools).research(state, trigger="cadence")

    assert memo.summary == "s" and memo.reasoning == "r"
    assert memo.findings == ["f1"] and memo.recommended_directions == ["d1"]
    claim_ref = canonical_source_ref("http://x")
    source_ref = canonical_source_ref("http://a")
    assert memo.claims == [{"statement": "c", "node_ids": [1],
                            "urls": [claim_ref.display_url],
                            "url_identities": [claim_ref.identity]}]
    assert memo.trigger == "cadence" and memo.at_node == 0
    # sources ledger: same (title, url, snippet) shape with the same [:200] snippet truncation
    assert memo.sources == [{"title": "search(ridge)", "url": source_ref.display_url,
                             "url_identity": source_ref.identity,
                             "snippet": "R" * 200}]
    assert tools.calls == [("search", {"query": "ridge", "url": "http://a"})]
    # only the wired tools + emit are offered — no self-plan tool injected into this stage
    assert client.specs_seen[0] == ["search", "emit"]

    # Turn 1: the memo prompts, byte-identical.
    assert client.turns[0] == [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": state_brief(state) +
            "\nReview the run. Consult sources if useful, then emit your memo."}]
    # Final turn: system, user, assistant(tool_calls), tool result, prose, historical nudge.
    final = client.turns[2]
    assert [m["role"] for m in final] == ["system", "user", "assistant", "tool", "assistant", "user"]
    # 4000-char tool-result cap preserved — now WITH the explicit truncation marker (P3,
    # docs/PROMPT_REVIEW.md) instead of a silent head-cut.
    from looplab.agents.agent import _cap_tool_result
    capped = _cap_tool_result("R" * 5000)
    assert len(capped) <= 4000 and capped.endswith("re-request a narrower range]")
    assert final[3] == {"role": "tool", "tool_call_id": "c1", "name": "search", "content": capped}
    assert final[4] == {"role": "assistant", "content": "let me think in prose"}
    assert final[5] == {"role": "user", "content": "Now call `emit` with your memo."}


def test_malformed_tool_args_degrade_to_empty_dict():
    """A junk model emitting unparseable JSON arguments never crashes the memo: the tool runs
    with {} and the source is still recorded (empty label/url), exactly as before."""
    tools = _FakeTools(result="ok")
    client = _FakeChatClient([
        {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "search", "arguments": "{not json"}}]},
        _tool_call("emit", {"summary": "done"}, call_id="c2"),
    ])
    memo = DeepResearcher(client, tools).research(RunState(goal="g"))
    assert tools.calls == [("search", {})]
    assert memo.sources == [{"title": "search()", "url": "", "url_identity": "",
                             "snippet": "ok"}]
    assert memo.summary == "done"


def test_turn_budget_exhaustion_forces_memo_fallback():
    """Out of turns without an emit -> the loop's salvage pass forces ONE structured emit from the
    accumulated history (instead of discarding the whole investigation to the caller's fallback)."""
    tools = _FakeTools(result="partial evidence")
    client = _ForcingClient(
        [_tool_call("search", {"query": "q"})],         # one tool turn, then the budget is gone
        forced={"summary": "forced memo", "findings": ["from history"]})
    memo = DeepResearcher(client, tools, max_turns=1).research(RunState(goal="g"))
    assert memo.summary == "forced memo" and memo.findings == ["from history"]
    assert memo.sources == [{"title": "search(q)", "url": "", "url_identity": "",
                             "snippet": "partial evidence"}]
    forced_msgs = client.forced_calls[-1]
    assert forced_msgs[-1]["role"] == "user"
    assert "Out of turn/time budget" in forced_msgs[-1]["content"]
    # the trace it synthesized from still holds the executed tool round
    assert any(m.get("role") == "tool" and m.get("content") == "partial evidence"
               for m in forced_msgs)


def test_stuck_detector_stops_with_historical_wording():
    """B1: four identical call+result rounds trip the stuck detector; the stop message keeps the
    stage's historical wording, and the forced emit is taken from complete_tool."""
    tools = _FakeTools(result="same")
    call = _tool_call("search", {"query": "loop"})
    client = _ForcingClient([call, call, call, call], forced={"summary": "unstuck memo"})
    memo = DeepResearcher(client, tools).research(RunState(goal="g"))
    assert memo.summary == "unstuck memo"
    assert len(memo.sources) == 4                       # every consulted round is still recorded
    reason = 'repeated the same call+result search({"query": "loop"}) 4 times with no progress'
    assert client.forced_calls[-1][-1] == {
        "role": "user",
        "content": f"Stop: you appear to be stuck ({reason}). Call `emit` with your memo now."}


def test_no_tools_hallucinated_call_gets_no_tools_observation():
    """tools=None: only `emit` is offered, and a hallucinated tool call observes the stage's
    historical "(no tools)" string (not the shared loop's "(unknown tool: …)" wording)."""
    client = _FakeChatClient([
        _tool_call("search", {"query": "q"}),
        _tool_call("emit", {"summary": "s"}, call_id="c2"),
    ])
    memo = DeepResearcher(client, tools=None).research(RunState(goal="g"))
    assert memo.summary == "s"
    assert memo.sources == [{"title": "search(q)", "url": "", "url_identity": "",
                             "snippet": "(no tools)"}]
    assert client.specs_seen[0] == ["emit"]
    tool_msgs = [m for m in client.turns[1] if m["role"] == "tool"]
    assert tool_msgs and tool_msgs[0]["content"] == "(no tools)"


def test_two_prose_stalls_fall_back_to_forced_memo():
    """Two consecutive un-forceable prose turns end the loop (with exactly one nudge in between);
    with no forcing client at all, the fallback degrades to the historical empty-memo summary."""
    client = _FakeChatClient([
        {"content": "prose one"},
        {"content": "prose two"},
    ])
    memo = DeepResearcher(client, tools=_FakeTools()).research(RunState(goal="g"))
    assert memo.summary == "(deep research produced no memo)"
    assert memo.sources == []
    nudges = [m for m in client.turns[1] if m["role"] == "user"
              and m["content"] == "Now call `emit` with your memo."]
    assert len(nudges) == 1


def test_memo_source_ledger_redacts_secrets_controls_and_caps_text():
    secret = "hunter2secret"
    bearer = "abcdef0123456789ABCDEF"
    api_key = "sk-abcdefghijklmnopqrstuvwxyz012345"
    tools = _FakeTools(result=f"Authorization: Bearer {bearer}\x1b[31mPOISON")
    client = _FakeChatClient([
        _tool_call("search", {
            "query": f"password={secret}\x00QUERY",
            "url": f"https://token={bearer}@example.test/\x1b[2J",
        }),
        _tool_call("emit", {
            "summary": f"api_key={api_key}\x1b[2J" + "x" * 10_000,
            "recommended_directions": [f"password={secret}\x00DIR"],
        }, call_id="c2"),
    ])

    memo = DeepResearcher(client, tools).research(RunState(goal="g"))
    rendered = json.dumps(memo.model_dump(mode="json"), ensure_ascii=False)
    assert secret not in rendered and bearer not in rendered and api_key not in rendered
    assert "\x00" not in rendered and "\x1b" not in rendered
    assert "***" in rendered and "POISON" in rendered
    assert len(memo.summary) <= 4_000


def test_memo_sanitizer_globally_bounds_numeric_trees_and_large_integers():
    from looplab.agents.deep_research import sanitize_research_memo_payload

    nested = [[[list(range(64)) for _ in range(20)] for _ in range(20)] for _ in range(20)]
    short_secret = "abc"
    clean = sanitize_research_memo_payload({
        "proposed_ideas": [{"ａｐｉ＿ｋｅｙ": short_secret, "huge": 10**400, "tree": nested}],
        "verification": {"tree": nested},
        "claims": [{"statement": "bounded ids", "node_ids": [1, 10**400]}],
        "at_node": 10**400,
    })
    rendered = json.dumps(clean, ensure_ascii=False)
    assert len(rendered) < 20_000
    assert short_secret not in rendered and '"api_key": "***"' in rendered
    assert "original_chars=401" in rendered
    assert clean["claims"][0]["node_ids"] == [1] and clean["at_node"] is None


def test_durable_memo_writer_sanitizes_before_verify_and_resanitizes_output(monkeypatch):
    from looplab.core.models import ResearchMemo
    from looplab.engine.orchestrator import Engine

    class _Store:
        def __init__(self):
            self.events = []

        def append(self, event_type, data):
            self.events.append((event_type, data))

        def read_all(self):
            return []

    secret = "hunter2secret"
    memo = ResearchMemo(
        summary=f"password={secret}\x1b[2J",
        reasoning="r" * 100_000,
        sources=[{"title": f"token={secret}\x00", "url": "https://u:p@example.test",
                  "snippet": f"api_key={secret}"}],
        claims=[{"statement": f"password={secret}", "node_ids": [],
                 "urls": ["https://u:p@example.test"]}],
        recommended_directions=[f"password={secret}\x1b[31mDIRECTION"],
        at_node=3,
        trigger="cadence",
    )
    eng = Engine.__new__(Engine)
    eng.store = _Store()
    eng._research_verify = True
    eng._track_hypotheses = True
    eng.deep_researcher = None
    observed = {}

    def _verify(clean_memo, *_args, **_kwargs):
        observed["memo"] = json.loads(json.dumps(clean_memo))
        return {"method": "llm", "unsupported": 0, "verdicts": [
            {"statement": "ok", "verdict": "supported",
             "note": f"password={secret}\x1b[31m"},
        ]}

    import looplab.trust.verify as verify_mod
    monkeypatch.setattr(verify_mod, "verify_memo", _verify)

    eng._record_deep_research(memo, trigger="cadence", manual=False)

    assert secret not in json.dumps(observed["memo"], ensure_ascii=False)
    rendered = json.dumps(eng.store.events, ensure_ascii=False)
    assert secret not in rendered and "https://u:p@" not in rendered
    assert "\x00" not in rendered and "\x1b" not in rendered
    assert "***" in rendered and "DIRECTION" in rendered
    assert [event_type for event_type, _ in eng.store.events] == [
        "research_completed", "hint", "hypothesis_added"]
    assert len(json.dumps(eng.store.events[0][1]["memo"])) < 70_000


# --- concurrent deep research records IMMEDIATELY when it finishes, independent of the eval + of max_parallel
def test_spawn_research_records_immediately_via_its_own_task():
    """`_spawn_research` records the memo from the RESEARCH task the moment it finishes — decoupled
    from the eval completing (so its directions steer the very next proposal), and independent of
    max_parallel."""
    import anyio
    from looplab.engine.orchestrator import Engine
    eng = Engine.__new__(Engine)
    eng.concurrent_research = True
    eng._due_research_trigger = lambda state: "cadence"
    eng._compute_deep_research = lambda snap, trig, trace=False: {"memo": "M"}
    recorded = []
    eng._record_deep_research = lambda memo, *, trigger, manual: recorded.append((memo, trigger, manual))

    async def run():
        async with anyio.create_task_group() as tg:
            eng._spawn_research(tg, state=object())      # no eval at all — research still records
    anyio.run(run)
    assert recorded == [({"memo": "M"}, "cadence", False)]


def test_spawn_research_noop_when_disabled_or_not_due():
    import anyio
    from looplab.engine.orchestrator import Engine
    eng = Engine.__new__(Engine)
    recorded = []
    eng._record_deep_research = lambda *a, **k: recorded.append(1)
    eng._compute_deep_research = lambda *a, **k: {"memo": "M"}

    async def run(concurrent, trig):
        eng.concurrent_research = concurrent
        eng._due_research_trigger = lambda state: trig
        async with anyio.create_task_group() as tg:
            eng._spawn_research(tg, state=object())
    anyio.run(run, False, "cadence")   # disabled -> no research
    anyio.run(run, True, None)          # not due -> no research
    assert recorded == []


def test_spawn_research_skips_record_on_none_memo():
    import anyio
    from looplab.engine.orchestrator import Engine
    eng = Engine.__new__(Engine)
    eng.concurrent_research = True
    eng._due_research_trigger = lambda state: "cadence"
    eng._compute_deep_research = lambda *a, **k: None     # compute yielded nothing
    recorded = []
    eng._record_deep_research = lambda *a, **k: recorded.append(1)

    async def run():
        async with anyio.create_task_group() as tg:
            eng._spawn_research(tg, state=object())
    anyio.run(run)
    assert recorded == []


def test_spawn_research_swallows_errors_so_it_cannot_cancel_the_eval():
    """A crash in the advisory research must NOT propagate out of the shared task group (it would
    cancel the in-flight eval). _bg swallows everything."""
    import anyio
    from looplab.engine.orchestrator import Engine
    eng = Engine.__new__(Engine)
    eng.concurrent_research = True
    eng._due_research_trigger = lambda state: "cadence"
    eng._compute_deep_research = lambda *a, **k: {"memo": "M"}
    def _boom(*a, **k):
        raise RuntimeError("record blew up")
    eng._record_deep_research = _boom

    reached_after = []
    async def run():
        async with anyio.create_task_group() as tg:
            eng._spawn_research(tg, state=object())
        reached_after.append(True)          # group exited cleanly despite the record raising
    anyio.run(run)                          # must NOT raise
    assert reached_after == [True]
