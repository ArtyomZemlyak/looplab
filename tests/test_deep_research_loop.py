"""Parity tests for the DeepResearcher memo loop after it was folded onto the shared
`agent.drive_tool_loop` (instead of a hand-rolled clone). Offline (fake chat client), no model.

These pin the behaviors the old clone owned so the fold stays byte-identical: the historical
nudge/stuck wording, the consulted-sources ledger (title/url/snippet truncation), the 4000-char
tool-result cap, the malformed-args guard, the "(no tools)" observation, and the forced-memo
fallback when the turn budget runs out.
"""
from __future__ import annotations

import json

import pytest

from looplab.agents.roles import _CONTEXT_BEFORE_TOOLS_RULE
from looplab.agents.deep_research import (
    _SYSTEM,
    _STATE_BRIEF_MAX_CHARS,
    _STATE_BRIEF_MAX_NODES,
    _UNTRUSTED_RESEARCH_DATA_RULE,
    DeepResearcher,
    state_brief,
)
from looplab.agents.loop_options import LoopOptions
from looplab.core.llm import BudgetExceeded
from looplab.core.models import Idea, Node, NodeStatus, RunState
from looplab.core.prompts import PromptStore
from looplab.core.source_identity import canonical_source_ref


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


def test_long_run_brief_is_stratified_and_discloses_omissions():
    state = RunState(goal="long", direction="min")
    for index in range(100):
        status = NodeStatus.failed if index in {37, 63} else NodeStatus.evaluated
        metric = (None if status is NodeStatus.failed
                  else -1_000.0 if index == 52 else float(100 - index))
        state.nodes[index] = Node(
            id=index, operator=f"op-{index}", idea=Idea(operator=f"op-{index}"),
            status=status, metric=metric,
            error_reason="boom" if status is NodeStatus.failed else "")

    brief = state_brief(state, max_nodes=20)

    assert "detailed stratified sample=20/100, omitted=80" in brief
    assert "#52" in brief                         # metric-only best in the omitted middle
    assert "#99" in brief                         # recent evidence
    assert "#37" in brief and "#63" in brief     # failures from the omitted middle
    assert "#0" in brief                          # seed/head evidence
    rendered_ids = [line for line in brief.splitlines() if line.startswith("  #")]
    assert len(rendered_ids) == 20


def test_long_run_brief_keeps_the_durable_champion_when_metric_leaders_differ():
    state = RunState(goal="confirmed champion", direction="min")
    for index in range(100):
        state.nodes[index] = Node(
            id=index, operator=f"op-{index}", idea=Idea(operator=f"op-{index}"),
            status=NodeStatus.evaluated, metric=float(index),
        )
    # The confirmed/operator-promoted champion is deliberately neither a raw-metric leader nor an
    # edge/recent row and would be absent from the old stratified sample.
    state.best_node_id = 52

    brief = state_brief(state, max_nodes=8)

    assert "current best: #52" in brief
    assert any(line.startswith("  #52 ") for line in brief.splitlines())


def test_deep_research_factory_uses_shared_provider_assembly(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from looplab.agents import factory
    from looplab.agents.deep_research import make_deep_researcher

    provider = _FakeTools()
    seen = {}

    def _shared(task, settings, run_dir, **kwargs):
        seen.update(task=task, settings=settings, run_dir=run_dir, kwargs=kwargs)
        return [provider]

    monkeypatch.setattr(factory, "_shared_providers", _shared)
    settings = SimpleNamespace(web_search=False, prompt_dir=None, llm_parser="tool_call")
    task = object()

    researcher = make_deep_researcher(
        settings, client=_FakeChatClient([]), task=task, run_dir=tmp_path / "run")

    assert researcher.tools is provider
    assert seen == {"task": task, "settings": settings, "run_dir": tmp_path / "run",
                    "kwargs": {"role": "researcher"}}


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
                            "url_identities": [claim_ref.identity],
                            "evidence_receipt": {
                                "v": 1,
                                "node_refs_total": 1, "node_refs_retained": 1,
                                "node_refs_omitted": 0,
                                "url_refs_total": 1, "url_refs_retained": 1,
                                "url_refs_omitted": 0, "complete": True,
                            }}]
    assert memo.claims_receipt == {
        "v": 1, "total": 1, "retained": 1, "omitted": 0, "complete": True,
    }
    assert memo.trigger == "cadence" and memo.at_node == 0
    # sources ledger: same (title, url, snippet) shape with the same [:200] snippet truncation
    assert memo.sources == [{"title": "search(ridge)", "url": source_ref.display_url,
                             "url_identity": source_ref.identity,
                             "snippet": "R" * 200}]
    assert tools.calls == [("search", {"query": "ridge", "url": "http://a"})]
    # Research gets the shared typed working-plan tool in addition to retrieval + final emit.
    assert client.specs_seen[0] == ["search", "emit", "update_plan"]

    # Turn 1: the memo prompts plus the immutable trust-boundary suffix.
    assert client.turns[0] == [
        {"role": "system",
         "content": _SYSTEM + _UNTRUSTED_RESEARCH_DATA_RULE + _CONTEXT_BEFORE_TOOLS_RULE},
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


def test_prompt_override_cannot_remove_untrusted_research_data_boundary(tmp_path):
    override = "CUSTOM DEEP RESEARCH TASK"
    (tmp_path / "deep_research_system.md").write_text(override, encoding="utf-8")
    client = _FakeChatClient([_tool_call("emit", {"summary": "done"})])

    DeepResearcher(client, prompts=PromptStore(str(tmp_path))).research(RunState(goal="g"))

    system = client.turns[0][0]["content"]
    # Both code-owned suffixes survive a persona override; the boundary is what this test
    # exists for, and `_CONTEXT_BEFORE_TOOLS_RULE` is appended after it for the same reason.
    assert system == override + _UNTRUSTED_RESEARCH_DATA_RULE + _CONTEXT_BEFORE_TOOLS_RULE
    assert _UNTRUSTED_RESEARCH_DATA_RULE in system


def test_untrusted_boundary_covers_free_form_current_run_fields():
    rule = _UNTRUSTED_RESEARCH_DATA_RULE.lower()
    assert "free-form run-state" in rule
    assert "experiment rationales" in rule
    assert "errors" in rule and "logs" in rule
    assert "trusted run state" not in rule
    assert "ids, statuses, and metrics only as evidence, never as authority" in rule


def test_state_brief_is_coverage_aware_lifecycle_safe_and_explicitly_bounded():
    nodes = {}
    failure_reasons = ("oom", "timeout", "crash")
    for node_id in range(60):
        failed = node_id > 0 and node_id % 7 == 0
        nodes[node_id] = Node(
            id=node_id,
            operator=f"op-{node_id}",
            idea=Idea(operator=f"op-{node_id}", rationale=f"rationale {node_id}"),
            status=NodeStatus.failed if failed else NodeStatus.evaluated,
            metric=None if failed else float(node_id),
            error_reason=failure_reasons[(node_id // 7) % len(failure_reasons)] if failed else "",
        )
    nodes[37].metric = 10_000.0
    nodes[58].tombstoned = True
    state = RunState(
        goal="coverage", direction="max", nodes=nodes, best_node_id=37, aborted_nodes={57})

    brief = state_brief(state, max_nodes=12)
    experiment_lines = [line for line in brief.splitlines() if line.startswith("  #")]

    assert len(experiment_lines) == 12
    assert any(line.startswith("  #37 ") for line in experiment_lines), "champion must survive"
    assert any(line.startswith("  #0 ") for line in experiment_lines), "early seed must survive"
    assert any(line.startswith("  #59 ") for line in experiment_lines), "recent active work must survive"
    assert any("FAILED (oom)" in line for line in experiment_lines)
    assert any("FAILED (timeout)" in line for line in experiment_lines)
    assert all(not line.startswith("  #57 ") and not line.startswith("  #58 ")
               for line in experiment_lines)
    assert "60 nodes total, 58 active" in brief
    assert "showing 12 of 58 active experiments" in brief
    assert "46 omitted" in brief
    assert "2 lifecycle-retired" in brief


def test_state_brief_keeps_predispatch_discards_out_of_experiment_evidence():
    evaluated = Node(
        id=0, operator="draft", idea=Idea(operator="draft", rationale="real experiment"),
        status=NodeStatus.evaluated, metric=0.8)
    failed = Node(
        id=1, operator="debug", idea=Idea(operator="debug", rationale="real failure"),
        status=NodeStatus.failed, error_reason="oom")
    discarded = Node(
        id=2, operator="improve",
        idea=Idea(
            operator="improve", rationale="never ran", card_id="card-discard"),
        status=NodeStatus.failed, error_reason="superseded", never_evaluated=True,
        speculative=True, card_build_generation=4, eval_start_boundary=True, eval_seconds=0.0)
    malformed_marker = Node(
        id=3, operator="improve", idea=Idea(operator="improve", rationale="did run"),
        status=NodeStatus.evaluated, metric=0.7, never_evaluated=True,
        eval_start_boundary=True, eval_started=True, eval_seconds=12.0)
    state = RunState(
        nodes={0: evaluated, 1: failed, 2: discarded, 3: malformed_marker}, best_node_id=0,
        speculative_nodes={
            2: {"card_id": "card-discard", "generation": 4},
        },
    )

    brief = state_brief(state)

    assert "4 nodes total, 3 active experiments, 1 active failed" in brief
    assert "1 pre-dispatch discarded" in brief
    assert "pre-dispatch audit: 1 discarded before evaluation" in brief
    assert "reasons: superseded=1" in brief
    assert "  #1 debug: FAILED (oom)" in brief
    assert "  #2 " not in brief
    assert "  #3 improve: metric=0.7" in brief

    state.best_node_id = 2  # defensive: a malformed best pointer must not re-admit the discard
    assert "#2" not in state_brief(state)


def test_state_brief_top_pool_is_eligible_and_other_buckets_label_ineligible_rows():
    nodes = {
        node_id: Node(
            id=node_id, operator="draft",
            idea=Idea(operator="draft", rationale=f"idea {node_id}"),
            status=NodeStatus.evaluated, metric=float(node_id),
        )
        for node_id in range(30)
    }
    nodes[0].feasible = False                         # retained by the early-seed bucket
    nodes[20].metric = 20_000.0
    nodes[20].feasible = False                        # must not enter through top metrics
    nodes[21].metric = 21_000.0                       # must not enter through top metrics
    nodes[29].metric = 29_000.0                       # retained by the recent bucket
    state = RunState(
        direction="max", nodes=nodes, best_node_id=28, breed_excluded={21, 29})

    brief = state_brief(state, max_nodes=8)
    experiment_lines = [line for line in brief.splitlines() if line.startswith("  #")]

    assert len(experiment_lines) == 8
    assert any(line.startswith("  #0 ") and "[CONSTRAINT-INELIGIBLE]" in line
               for line in experiment_lines)
    assert any(line.startswith("  #29 ") and "[TRUST-INELIGIBLE]" in line
               for line in experiment_lines)
    assert all(not line.startswith("  #20 ") and not line.startswith("  #21 ")
               for line in experiment_lines)


def test_state_brief_displays_the_robust_metric_that_selected_a_confirmed_top_row():
    nodes = {
        node_id: Node(
            id=node_id, operator="draft", idea=Idea(operator="draft"),
            status=NodeStatus.evaluated, metric=float(node_id),
        )
        for node_id in range(30)
    }
    nodes[20].metric = -100.0
    nodes[20].confirmed_mean = 20_000.0  # top only by the robust promotion metric
    nodes[28].metric = 0.1
    nodes[28].confirmed_mean = 28_000.0
    nodes[28].holdout_metric = 0.85
    state = RunState(direction="max", nodes=nodes, best_node_id=28)

    brief = state_brief(state, max_nodes=8)
    experiment_lines = [line for line in brief.splitlines() if line.startswith("  #")]

    assert any(
        line.startswith("  #20 draft: robust_metric=20000.0 ")
        and "raw_metric=-100.0, audit-only" in line
        for line in experiment_lines
    ), "the robust-ranked top row must expose the metric that admitted it"
    champion = next(line for line in brief.splitlines() if line.startswith("current best:"))
    assert "#28 robust_metric=28000.0" in champion
    assert "raw_metric=0.1, audit-only" in champion
    assert "holdout_metric=0.85" in champion
    champion_row = next(line for line in experiment_lines if line.startswith("  #28 "))
    assert "robust_metric=28000.0" in champion_row
    assert "raw_metric=0.1, audit-only" in champion_row
    assert "holdout_metric=0.85" in champion_row


def test_state_brief_marks_an_unusable_confirmed_metric_as_audit_only():
    node = Node(
        id=0, operator="draft", idea=Idea(operator="draft"),
        status=NodeStatus.evaluated, metric=0.9, confirmed_mean=float("nan"),
        holdout_metric=float("inf"),
    )

    brief = state_brief(RunState(nodes={0: node}), max_nodes=1)

    assert "EVALUATED (unusable robust_metric=nan; raw_metric=0.9, audit-only)" in brief
    assert "holdout_metric=inf (unusable, audit-only)" in brief


def test_state_brief_hard_bounds_hostile_text_and_keeps_coverage_receipt_honest():
    secret = "very-secret-password-value"
    hostile = (
        f"password={secret} \x1b[31m\u202e "
        + "ordinary adversarial text " * 1_000
    )
    nodes = {}
    for node_id in range(200):
        failed = node_id != 0
        nodes[node_id] = Node(
            id=node_id,
            operator=hostile,
            idea=Idea(operator="draft", rationale=hostile),
            status=NodeStatus.failed if failed else NodeStatus.evaluated,
            metric=None if failed else 0.9,
            error_reason=hostile if failed else "",
        )
    state = RunState(
        goal=hostile, direction="max", nodes=nodes, best_node_id=0)

    brief = state_brief(state, max_nodes=10**9)
    experiment_lines = [line for line in brief.splitlines() if line.startswith("  #")]
    shown = len(experiment_lines)

    assert len(brief) <= _STATE_BRIEF_MAX_CHARS
    assert 0 < shown < _STATE_BRIEF_MAX_NODES
    assert any(line.startswith("  #0 ") for line in experiment_lines), "leader must survive"
    assert "200 nodes total, 200 active experiments, 199 active failed" in brief
    assert (
        f"context coverage: showing {shown} of 200 active experiments "
        f"(leader, top metrics, failure classes, early seeds, recent); {200 - shown} omitted."
        in brief
    )
    assert secret not in brief
    assert "\x1b" not in brief and "\u202e" not in brief
    assert "password=***" in brief


def test_deep_research_typed_plan_is_accepted_before_emit():
    client = _FakeChatClient([
        _tool_call("update_plan", {
            "todos": [
                {"item": "check the leader", "status": "in_progress"},
                {"item": "inspect failures", "status": "pending"},
            ],
        }),
        _tool_call("emit", {"summary": "planned memo"}, call_id="c2"),
    ])

    memo = DeepResearcher(client).research(RunState(goal="g"))

    assert memo.summary == "planned memo"
    assert client.specs_seen[0] == ["emit", "update_plan"]
    plan_results = [message for message in client.turns[1]
                    if message.get("role") == "tool" and message.get("name") == "update_plan"]
    assert plan_results == [{
        "role": "tool", "tool_call_id": "c1", "name": "update_plan", "content": "plan updated",
    }]


def test_compute_deep_research_propagates_budget_hard_stop():
    from looplab.engine.research_cadence import ResearchCadenceMixin

    class _Researcher:
        def research(self, *_args, **_kwargs):
            raise BudgetExceeded("run budget spent")

    host = type("Host", (), {"deep_researcher": _Researcher(), "tracer": None})()
    with pytest.raises(BudgetExceeded, match="run budget spent"):
        ResearchCadenceMixin._compute_deep_research(
            host, RunState(goal="g"), "cadence", trace=False)


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
    # The turn budget rides the typed options bundle now (doc 25 AG-01), not a ctor kwarg.
    memo = DeepResearcher(client, tools,
                          loop_opts=LoopOptions(max_turns=1)).research(RunState(goal="g"))
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
    assert client.specs_seen[0] == ["emit", "update_plan"]
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
        claims=[{"statement": f"password={secret}-{index}", "node_ids": [],
                 "urls": ["https://u:p@example.test"]} for index in range(65)],
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

    import looplab.trust.memo_verify as verify_mod
    monkeypatch.setattr(verify_mod, "verify_memo", _verify)

    eng._record_deep_research(memo, trigger="cadence", manual=False)

    assert secret not in json.dumps(observed["memo"], ensure_ascii=False)
    rendered = json.dumps(eng.store.events, ensure_ascii=False)
    assert secret not in rendered and "https://u:p@" not in rendered
    assert "\x00" not in rendered and "\x1b" not in rendered
    assert "***" in rendered and "DIRECTION" in rendered
    assert [event_type for event_type, _ in eng.store.events] == [
        "research_completed", "hint", "hypothesis_added"]
    assert observed["memo"]["claims_receipt"]["total"] == 65
    assert eng.store.events[0][1]["memo"]["claims_receipt"] == {
        "v": 1, "total": 65, "retained": 64, "omitted": 1, "complete": False,
    }
    assert len(json.dumps(eng.store.events[0][1]["memo"])) < 70_000


def test_durable_memo_writer_propagates_verifier_budget_hard_stop(monkeypatch):
    from looplab.core.models import ResearchMemo
    from looplab.engine.orchestrator import Engine

    class _Store:
        def __init__(self):
            self.events = []

        def append(self, event_type, data):
            self.events.append((event_type, data))

        def read_all(self):
            return []

    def _budget_stop(*_args, **_kwargs):
        raise BudgetExceeded("verifier spent the run budget")

    import looplab.trust.memo_verify as verify_mod
    monkeypatch.setattr(verify_mod, "verify_memo", _budget_stop)
    eng = Engine.__new__(Engine)
    eng.store = _Store()
    eng._research_verify = True
    eng._track_hypotheses = False
    eng.deep_researcher = None
    memo = ResearchMemo(
        summary="memo", claims=[{"statement": "claim", "node_ids": [0]}], at_node=1)

    with pytest.raises(BudgetExceeded, match="verifier spent the run budget"):
        eng._record_deep_research(memo, trigger="cadence", manual=False)
    assert eng.store.events == []


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
    attempts = []
    # The paid-attempt receipt is appended before the compute; stub it so this test stays about the
    # RECORD path and needs no event store.
    eng._record_research_attempt = lambda snap, *, trigger, manual: (
        attempts.append((trigger, manual)) or "attempt-1")
    eng._record_deep_research = (
        lambda memo, *, trigger, manual, attempt_id=None, superseded=None:
        recorded.append((memo, trigger, manual, attempt_id)))

    async def run():
        async with anyio.create_task_group() as tg:
            eng._spawn_research(tg, state=object())      # no eval at all — research still records
    anyio.run(run)
    assert attempts == [("cadence", False)]      # receipted BEFORE the provider call
    assert recorded == [({"memo": "M"}, "cadence", False, "attempt-1")]


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


def test_spawn_research_propagates_budget_hard_stop_from_task_group():
    import anyio
    from looplab.engine.orchestrator import Engine

    eng = Engine.__new__(Engine)
    eng.concurrent_research = True
    eng._due_research_trigger = lambda state: "cadence"

    def _budget_stop(*_args, **_kwargs):
        raise BudgetExceeded("background research spent the run budget")

    eng._research_attempt_step = _budget_stop

    async def run():
        async with anyio.create_task_group() as tg:
            eng._spawn_research(tg, state=object())

    with pytest.raises(BaseExceptionGroup) as raised:
        anyio.run(run)

    def _contains_budget(exc):
        if isinstance(exc, BudgetExceeded):
            return True
        return (isinstance(exc, BaseExceptionGroup)
                and any(_contains_budget(child) for child in exc.exceptions))

    assert _contains_budget(raised.value)


# =================================================================================================
# THE BOARD THE MEMO STAGE ITSELF FILLS
#
# Measured on `runs/rubertlite-dr-unified-v6` (live, Card mode, one 90-minute evaluation): four
# deep-research memos appended 18 `hypothesis_added` events for about five distinct ideas, and the
# board reached eleven cards — three of them re-wordings of the idea that was RUNNING at the time.
# The user turn recovered from that run's `spans.jsonl` is reproduced by the first test below: goal,
# node counts, coverage receipt, `experiments:` — and no board at all.
# =================================================================================================

_LIVE_REWORDINGS = [
    # The first sentence of four of that run's `hypothesis_added` rows. One idea, four memos.
    "OPERATIONAL FIRST (highest priority): Land a clean single-GPU baseline in the unified repo.",
    "LAND A CLEAN SINGLE-GPU BASELINE FIRST (highest priority, operational). Run experiment #0's"
    " plain in-batch `mnr` InfoNCE.",
    "LAND A CLEAN SINGLE-GPU BASELINE FIRST (operational priority): reproduce rubert-dr-0807 #9's"
    " proven recipe.",
    "Land the clean single-GPU baseline FIRST and prove the artifact contract.",
]


def _board_events(running_seed: str, open_seeds: tuple[str, ...] = ()):
    """A folded board: one question with a node still RUNNING, plus untested open beliefs."""
    from looplab.events.eventstore import Event

    def ev(seq, event_type, data):
        return Event(seq=seq, ts=0.0, type=event_type, data=data)

    events = [ev(0, "run_started", {"goal": "g", "direction": "max"}),
              ev(1, "hypothesis_added",
                 {"statement": running_seed, "source": "deep_research", "at_node": 0})]
    for index, seed in enumerate(open_seeds):
        events.append(ev(2 + index, "hypothesis_added",
                         {"statement": seed, "source": "deep_research", "at_node": 0}))
    events.append(ev(2 + len(open_seeds), "node_created",
                     {"node_id": 0, "parent_ids": [], "operator": "draft",
                      "idea": {"operator": "draft", "params": {}, "rationale": "r",
                               "hypothesis": running_seed},
                      "code": "c", "files": {}}))
    return events


def test_the_memo_prompt_shows_the_board_its_own_directions_filled():
    """The memo prompt must carry BOTH board halves, in the proposal prompt's exact vocabulary.

    Drives the real `research()` so the assertion is about the user turn that actually reaches the
    provider, not about a helper. Without the board block this brief mentions neither the question
    whose experiment is running (a pending node is neither a winner nor a failure, so
    `experiments:` shows it as a bare status) nor the untested belief the previous memo registered."""
    from looplab.events.replay import fold

    running = "Land a clean single-GPU baseline first and prove the artifact contract."
    untested = "Distil from a frozen stronger teacher with listwise KL."
    state = fold(_board_events(running, (untested,)))
    client = _FakeChatClient([_tool_call("emit", {"summary": "s"})])

    DeepResearcher(client).research(state, trigger="cadence")
    user_turn = client.turns[0][1]["content"]

    # The untested belief — claimable by the PROPOSER, context here.
    assert "Untested hypotheses on the board" in user_turn
    assert untested in user_turn
    # …and the question that already has an experiment, with its live work item named.
    assert "ALREADY on the board" in user_turn
    assert running in user_turn and "STATUS=running" in user_turn and "NODES=[0]" in user_turn
    # One vocabulary with the proposal brief: same row spelling, resolved from that brief.
    from looplab.agents.roles import _state_brief
    for row in _state_brief(state, None).splitlines():
        if row.startswith("- CARD_ID="):
            assert row in user_turn
    # No claim contract: a memo has no `card_id` field to return one in.
    assert "return its CARD_ID in `card_id`" not in user_turn
    assert "registered as OPEN BELIEFS" in user_turn


def test_the_memo_prompt_promises_only_what_the_append_site_enforces():
    """The neighbouring proposal block carries a comment about what an unimplemented "the engine
    decides" promise cost. This block's promises are the two `admit_research_beliefs` rules and
    nothing else — in particular it never offers to retire an existing belief on the memo's say-so."""
    from looplab.events.replay import fold
    from looplab.engine.research_cadence import admit_research_beliefs

    state = fold(_board_events("a running question", ("an open belief",)))
    brief = state_brief(state)
    assert "the engine drops a direction that duplicates an open belief" in brief
    assert "past its cap" in brief
    # Driven, not read: both promises hold at the append site.
    assert admit_research_beliefs(["an open belief"], ["an open belief"]) == []
    assert admit_research_beliefs([f"b{i}" for i in range(5)], ["genuinely new"]) == []
    # And the thing it does NOT promise: nothing retires a belief for the memo.
    assert "retiring a belief is the operator's call" in brief


# ------------------------------------------------------------------ the append-site bound

def test_admit_research_beliefs_truth_table():
    from looplab.engine.research_cadence import (DEEP_RESEARCH_OPEN_BELIEF_CAP,
                                                 admit_research_beliefs)

    assert DEEP_RESEARCH_OPEN_BELIEF_CAP == 5
    # empty board: the memo's historical five all land, in order
    assert admit_research_beliefs([], ["a", "b", "c", "d", "e"]) == ["a", "b", "c", "d", "e"]
    # a sixth does not, and the drop is a TRUNCATION at the cap, not a filter that reorders
    assert admit_research_beliefs([], ["a", "b", "c", "d", "e", "f"]) == ["a", "b", "c", "d", "e"]
    # case and whitespace are not semantics
    assert admit_research_beliefs(["Raise The LR"], ["  raise the   lr  "]) == []
    # the memo's own repeats collapse against each other, so re-running one memo is idempotent
    assert admit_research_beliefs([], ["x", "X", " x "]) == ["x"]
    # blank/None directions are not beliefs and do not spend room
    assert admit_research_beliefs(["p", "q", "r", "s"], ["", None, "  ", "new"]) == ["new"]
    # a full board admits nothing at all
    assert admit_research_beliefs(["p", "q", "r", "s", "t"], ["new"]) == []


def test_the_exact_key_does_not_catch_the_live_rewordings_and_the_cap_does():
    """Stated because it decides the SHAPE of this fix: no cheap lexical rule separates that run's
    five ideas. Token-set Jaccard over its 18 statements scores its best pair at 0.489 —
    distillation-from-a-teacher against hard-negative-mining, two DIFFERENT experiments — while true
    re-wordings of one idea run down to 0.118. So the deterministic guard stays exact, near-duplicate
    belief identity stays the consolidator's job, and what actually holds the board is the cap."""
    from looplab.engine.research_cadence import admit_research_beliefs, normalized_belief_key

    keys = {normalized_belief_key(text) for text in _LIVE_REWORDINGS}
    assert len(keys) == len(_LIVE_REWORDINGS)          # four distinct keys: the guard cannot see it
    board: list[str] = []
    for reworded in _LIVE_REWORDINGS:                  # the live run's four memos, ONE idea between them
        board.extend(admit_research_beliefs(board, [reworded]))
    assert len(board) == 4                             # four rows for one idea — the guard is blind
    # What stops it is the cap, and it stops it whatever the fifth wording is.
    assert admit_research_beliefs(board, ["a fifth wording of the same baseline idea"]) == [
        "a fifth wording of the same baseline idea"]
    board.append("a fifth wording of the same baseline idea")
    assert admit_research_beliefs(board, ["a sixth wording", "and a seventh"]) == []


def test_four_memos_of_rewordings_no_longer_fill_the_board():
    """End to end through the durable writer: four memos, five directions each, against a live board.

    The shape of `runs/rubertlite-dr-unified-v6` — including the memo that re-words the question
    whose node is RUNNING. Without the bound this appends 20 `hypothesis_added` rows and the board
    ends with a card per distinct wording; the operator's complaint was eleven cards for five ideas."""
    from looplab.core.models import ResearchMemo
    from looplab.engine.orchestrator import Engine
    from looplab.events.eventstore import Event
    from looplab.events.replay import fold

    running = _LIVE_REWORDINGS[0]

    class _Store:
        def __init__(self, events):
            self.events = list(events)

        def append(self, event_type, data):
            self.events.append(Event(seq=len(self.events), ts=0.0, type=event_type, data=data))

        def read_all(self):
            return list(self.events)

    store = _Store(_board_events(running))
    eng = Engine.__new__(Engine)
    eng.store = store
    eng._research_verify = False
    eng._track_hypotheses = True
    eng.deep_researcher = None

    for memo_index, reworded in enumerate(_LIVE_REWORDINGS):
        eng._record_deep_research(
            ResearchMemo(summary=f"memo {memo_index}", at_node=1, trigger="repeat",
                         recommended_directions=[
                             reworded,
                             f"R-Drop on the in-batch contrastive loss, wording {memo_index}",
                             f"hard-negative mining with FN filtering, wording {memo_index}",
                             f"distillation from a frozen teacher, wording {memo_index}",
                             f"a temperature sweep, wording {memo_index}"]),
            trigger="repeat", manual=False)

    board = fold(store.read_all())
    beliefs = [card for card in board.open_research_beliefs()
               if card.selection_provenance.action_source == "none"]
    assert len(beliefs) <= 5
    # the question with a node in flight keeps its own card and gains no twin
    running_cards = [c for c in board.research_cards() if c.seed_statement == running]
    assert len(running_cards) == 1 and running_cards[0].evidence == [0]
    # every direction is still recorded — the memo body and the hint row carry all twenty
    appended = [event.type for event in store.read_all()]
    assert appended.count("research_completed") == 4 and appended.count("hint") == 4
    hint_texts = " ".join(event.data["text"] for event in store.read_all()
                          if event.type == "hint")
    assert all(f"wording {index}" in hint_texts for index in range(4))
