"""The foresight panel's candidates 2..K are ALTERNATIVES that continue candidate 1's session.

WP-F (2026-09-29, `Settings.foresight_alternatives`). The panel (`search/foresight.py::
ForesightPanelResearcher`) used to ask its base for K candidates with K full `propose` calls — for the
agentic Researcher, K research sessions from one identical prompt. On MiniOneRec inf13 one proposal
cost 86 + 100 minutes for two candidates the ranker called "effectively the same bet". Now candidate
1 is one session (`agents/agent.py::ToolUsingResearcher.propose_with_session`) and each further
candidate CONTINUES it (`propose_alternative`): one more user turn, the same tools, fence, emit spec
and validator, a turn cap, and a bounce for a copy of an earlier candidate.

Every test here drives the real chain — the tool-using Researcher, the unified facade the shipped
default wraps, the panel — against a scripted model that records each request AS SENT, and the
numbers in the names are the critic's (review of WP-F, 2026-09-29): (1) one continuation request
whose messages start with candidate 1's final request byte for byte; (2) a strict endpoint that 400s
on an unanswered or orphaned tool_call id, over the emitted, salvaged and stuck exits; (3) a failed
continuation is None, never a `fallback (…)` Idea, and never a pause; (4) the spend ceiling and a
cancelled phase propagate; (5) no stale session is continued; (6) no handoff notes and exactly one
summary; (7) the turn cap and the CHOSEN candidate's budget receipt; (8) board binding and the new
rotation rate; (9) a duplicate is bounced; (10) the switch's defaults and its one reader; (11) the
guard registries.
"""
from __future__ import annotations

import ast
import copy
import json

import pytest

from looplab.agents.agent import (ALTERNATIVE_MAX_TURNS, LoopOptions, ProposalSession,
                                  ToolUsingResearcher, handoff_scope, phase_cancel_scope)
from looplab.agents.roles import (RESEARCHER_ACTION_ATTRS, is_researcher_fallback,
                                  next_board_prompt_cards, researcher_budget_exhausted)
from looplab.agents.tool_loop import _handoff_ctx
from looplab.agents.unified_agent import UnifiedAgent
from looplab.core.config import (LEGACY_CONFIG_SNAPSHOT_DEFAULTS, Settings,
                                 settings_from_snapshot)
from looplab.core.errors import BudgetExceeded, LLMError, PhaseCancelled
from looplab.core.evidence import EVIDENCE_CONSUMERS, EVIDENCE_LABEL, FENCED, fence_untrusted
from looplab.core.models import Card, CardSelectionProvenance, Idea, RunState
from looplab.search.foresight import ForesightPanelResearcher
from looplab.search.researcher_stack import with_foresight_panel, wrap_researcher
from looplab.search.surrogate import SurrogateResearcher

# A phrase of the continuation turn's default text (`agents/agent.py::_ALTERNATIVE_TURN`).
_ALT_MARK = "ONE ALTERNATIVE experiment"
_NOTES_MARK = "UNTRUSTED_EARLIER_PHASE_NOTES"


# ------------------------------------------------------------------ the scripted model and chain

def _call(cid: str, name: str, args: dict) -> dict:
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def _turn(*calls, content: str = "") -> dict:
    return {"content": content, "tool_calls": list(calls)}


def _read(cid: str, path: str = "service/latency_engine.py") -> dict:
    return _call(cid, "read_file", {"path": path})


def _emission(what: str, x: float = 1.0, **extra) -> dict:
    return {"operator": "improve", "params": {"x": x}, "rationale": f"change: {what}",
            "hypothesis": f"{what} lowers latency", "concept_mode": "full",
            "concepts": ["latency/cache"], **extra}


def _emit(cid: str, what: str, x: float = 1.0, **extra) -> dict:
    return _call(cid, "emit", _emission(what, x, **extra))


def _refuse_like_a_strict_endpoint(messages) -> None:
    """HTTP 400 the way a strict OpenAI-compatible endpoint answers it: every `tool_call_id` an
    assistant turn opens is answered by `tool` messages before anything else follows, and a `tool`
    message may only answer a call that is still open."""
    open_ids: set = set()
    for message in messages:
        if message.get("role") == "tool":
            if message.get("tool_call_id") not in open_ids:
                raise LLMError(f"HTTP 400: orphaned tool_call_id {message.get('tool_call_id')!r}")
            open_ids.discard(message.get("tool_call_id"))
            continue
        if open_ids:
            raise LLMError(f"HTTP 400: tool_call_id(s) {sorted(open_ids)} were never answered")
        if message.get("role") == "assistant":
            open_ids = {call["id"] for call in message.get("tool_calls") or []}
    if open_ids:
        raise LLMError(f"HTTP 400: tool_call_id(s) {sorted(open_ids)} were never answered")


class _Model:
    """A scripted model on every channel the chain uses, keeping every request AS SENT.

    `chat`: `script` is a list of replies popped in order, or a function of the request; a reply
    that is an exception is RAISED. `complete_tool` answers the ranker's schema (`order`; a BOARD
    ranking abstains, so the board is never re-ordered under a test) and a forced emit with the next
    of `forced` (None = the endpoint cannot force). `complete_text` counts handoff summaries."""

    def __init__(self, script, *, order=(1, 0), forced=(), strict=False):
        self.script = script if callable(script) else list(script)
        self.chats: list[list] = []
        self.order = list(order)
        self.forced = list(forced)
        self.forced_requests: list[list] = []
        self.rank_requests: list[list] = []
        self.summaries = 0
        self.summarized: list[str] = []         # the transcript text each handoff summary was given
        self.log: list[str] = []                # "chat" / "rank" / "forced" / "summary", in call order
        self.strict = strict

    def chat(self, messages, tools=None, tool_choice="auto", **_kw):
        if self.strict:
            _refuse_like_a_strict_endpoint(messages)
        self.chats.append(copy.deepcopy(messages))
        self.log.append("chat")
        reply = self.script(messages) if callable(self.script) else self.script.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply

    def complete_tool(self, messages, schema):
        if self.strict:
            _refuse_like_a_strict_endpoint(messages)
        if "order" in (schema.get("properties") or {}):
            if "untested HYPOTHESES" in str(messages[0].get("content")):
                return {"order": [], "confidence": 0.5, "reason": "board: abstain"}
            self.rank_requests.append(copy.deepcopy(messages))
            self.log.append("rank")
            return {"order": list(self.order), "confidence": 0.8, "reason": "ranked"}
        self.forced_requests.append(copy.deepcopy(messages))
        self.log.append("forced")
        return self.forced.pop(0) if self.forced else None

    def complete_text(self, messages):
        if str(messages[0].get("content", "")).startswith("You are handing off from"):
            self.summaries += 1
            self.summarized.append(str(messages[1].get("content", "")))
            self.log.append("summary")
            return "- the per-depth scorer lives in service/latency_engine.py"
        return "no json here"


class _Tools:
    """One real reader whose page is long enough that a handoff summary is worth its call."""

    def specs(self):
        return [{"type": "function", "function": {
            "name": "read_file", "description": "Read a file.",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}}]

    def execute(self, name, args):
        return f"# {args.get('path')}\n" + "def scorer(depth):\n    return depth * 2\n" * 80


class _Developer:
    def __init__(self, client):
        self.client = client
        self.last_files: dict = {}

    def implement(self, _idea):
        return "print(1)"


def _chain(script, *, order=(1, 0), forced=(), strict=False, unified=True, tools=None,
           envelope=False, loop_opts=None, k=2):
    """The shipped shape by default: panel -> UnifiedAgent -> ToolUsingResearcher."""
    model = _Model(script, order=order, forced=forced, strict=strict)
    researcher = ToolUsingResearcher(model, tools if tools is not None else _Tools(),
                                     evidence_envelope=envelope, loop_opts=loop_opts)
    base = UnifiedAgent(researcher=researcher, developer=_Developer(model)) if unified else researcher
    panel = ForesightPanelResearcher(base, k=k, client=model, alternatives=True)
    return model, researcher, base, panel


def _state() -> RunState:
    return RunState(goal="lower the inference latency", direction="min")


def _alt_request(model) -> list:
    """The continuation's FIRST request (the one that carries the alternative turn)."""
    return next(req for req in model.chats if _ALT_MARK in str(req[-1].get("content")))


def _asks_for_alternative(messages) -> bool:
    return any(m.get("role") == "user" and _ALT_MARK in str(m.get("content")) for m in messages)


# ------------------------------------------------------------------ (1) ONE continuation request

def test_1_the_unified_chain_continues_candidate_1s_session_in_ONE_request():
    """Through the stack the CLI builds (`wrap_researcher` over the unified facade): candidate 2 is
    one more request on candidate 1's transcript — its messages START with candidate 1's final
    request byte for byte (the provider's cached prefix), there is one system root, and no note was
    spliced in. MUTATION: continue from a fresh `propose` -> a second system root and no prefix."""
    model = _Model([_turn(_read("r1")), _turn(_emit("e1", "cache the per-depth scorer")),
                    _turn(_emit("e2", "batch the shared prompt pages", x=2.0))], order=(1, 0))
    researcher = ToolUsingResearcher(model, _Tools())
    agent = UnifiedAgent(researcher=researcher, developer=_Developer(model))
    panel, developer = wrap_researcher(agent, agent, settings=Settings(
        backend="llm", foresight_verify=False), tools=None)
    assert isinstance(panel, ForesightPanelResearcher) and panel is developer
    assert panel.alternatives is True, "the product default is ON"

    chosen = panel.propose(_state(), None)

    assert len(model.chats) == 3, "two turns for candidate 1, ONE request for the alternative"
    first_final, alternative = model.chats[1], model.chats[2]
    assert json.dumps(alternative[:len(first_final)]) == json.dumps(first_final), (
        "the continuation must be candidate 1's final request plus what followed it")
    assert [m["role"] for m in alternative].count("system") == 1
    assert not any(_NOTES_MARK in str(m.get("content")) for m in alternative)
    tail = alternative[len(first_final):]
    assert [m["role"] for m in tail] == ["assistant", "tool", "user"]
    assert tail[0]["tool_calls"][0]["id"] == "e1" and tail[1]["tool_call_id"] == "e1"
    assert _ALT_MARK in tail[2]["content"] and "cache the per-depth scorer" in tail[2]["content"]
    assert chosen.params == {"x": 2.0} and "[foresight: predicted best of 2" in chosen.rationale
    assert panel.last_foresight["alternatives"] == [False, True]
    assert panel.last_foresight["n"] == 2


def test_1_a_third_candidate_continues_the_ALREADY_continued_session():
    """K = 3: the second alternative continues the transcript the first one left — its request
    starts with the first continuation's final request byte for byte, the first alternative's emit
    is answered (never a dangling id), and the turn lists BOTH candidates so far."""
    model, _researcher, _base, panel = _chain([
        _turn(_read("r1")), _turn(_emit("e1", "cache the per-depth scorer")),
        _turn(_emit("e2", "batch the shared prompt pages", x=2.0)),
        _turn(_emit("e3", "prune the beam early", x=3.0))], order=(2, 1, 0), k=3, strict=True)
    chosen = panel.propose(_state(), None)

    assert len(model.chats) == 4
    first_alt, second_alt = model.chats[2], model.chats[3]
    assert json.dumps(second_alt[:len(first_alt)]) == json.dumps(first_alt)
    tail = second_alt[len(first_alt):]
    assert [m["role"] for m in tail] == ["assistant", "tool", "user"]
    assert tail[1]["tool_call_id"] == "e2" and tail[1]["content"].startswith("(recorded:")
    assert "1. [improve]" in tail[2]["content"] and "2. [improve]" in tail[2]["content"]
    assert "batch the shared prompt pages" in tail[2]["content"]
    assert chosen.params == {"x": 3.0}
    assert panel.last_foresight["alternatives"] == [False, True, True]


# ------------------------------------------------------------------ (2) a strict endpoint

def _strict_script(first_exit: str):
    """Candidate 1 ends by `first_exit`; the continuation then emits a different experiment."""
    reads = [0]

    def reply(messages):
        if _asks_for_alternative(messages):
            return _turn(_emit("e2", "a second, different mechanism", x=2.0))
        if first_exit == "emitted":
            # The emit is FIRST and a sibling read follows it in the same turn: the loop returns at
            # the emit, leaving both ids unanswered.
            if not any(m.get("role") == "tool" for m in messages):
                return _turn(_read("r1"))
            return _turn(_emit("e1", "cache the per-depth scorer"), _read("r2", "b.py"))
        if first_exit == "salvaged":
            if not any(m.get("role") == "tool" for m in messages):
                return _turn(_read("r1"))
            return {"content": "I would cache the per-depth scorer.", "tool_calls": []}
        reads[0] += 1                                     # stuck: the same read, again and again
        return _turn(_read(f"r{reads[0]}"))

    return reply


@pytest.mark.parametrize("first_exit", ["emitted", "salvaged", "stuck"])
def test_2_a_strict_endpoint_accepts_every_continuation(first_exit):
    """An accepted emit leaves its `tool_call_id` (and a sibling's) unanswered, which a strict
    endpoint refuses with a 400; a SALVAGED emission was forced on a copy and is not in the
    transcript at all. The continuation answers the open calls — the emit with a record, the sibling
    with a stub — and restates the candidates in its turn. MUTATION: drop `_answer_open_calls` ->
    the emitted case 400s and the alternative is lost."""
    forced = [] if first_exit == "emitted" else [_emission("cache the per-depth scorer")]
    model, _researcher, _base, panel = _chain(_strict_script(first_exit), forced=forced,
                                              strict=True)
    chosen = panel.propose(_state(), None)

    assert panel.last_foresight is not None, "the alternative was lost — nothing was ranked"
    assert panel.last_foresight["alternatives"] == [False, True]
    assert chosen.params == {"x": 2.0}
    request = _alt_request(model)
    assert "cache the per-depth scorer" in request[-1]["content"], "the candidates are restated"
    answers = {m["tool_call_id"]: m["content"] for m in request if m.get("role") == "tool"}
    if first_exit == "emitted":
        assert answers["e1"].startswith("(recorded:") and answers["r2"].startswith("(not executed")
    else:
        assert model.forced_requests, "candidate 1 was salvaged by a forced emit"
        assert not any(call["function"]["name"] == "emit" for m in request
                       for call in m.get("tool_calls") or []), (
            "a salvaged emission is not in the transcript — it is restated, never invented")


def test_2_only_the_FIRST_emit_of_a_turn_is_recorded_as_a_candidate():
    """A turn carrying TWO emits: the loop walked the calls in order and returned at the first one it
    accepted, so the second was never looked at. Recording both would tell the model a proposal
    nobody accepted is a candidate. MUTATION: answer every open emit with the record -> e1b reads
    `(recorded: …)`."""
    model, _researcher, _base, panel = _chain([
        _turn(_read("r1")),
        _turn(_emit("e1", "cache the per-depth scorer"), _emit("e1b", "something else", x=9.0)),
        _turn(_emit("e2", "batch the shared prompt pages", x=2.0))], order=(0, 1), strict=True)
    chosen = panel.propose(_state(), None)

    answers = {m["tool_call_id"]: m["content"] for m in _alt_request(model) if m.get("role") == "tool"}
    assert answers["e1"].startswith("(recorded:")
    assert answers["e1b"].startswith("(not executed"), answers["e1b"]
    assert chosen.params == {"x": 1.0}, "candidate 1 is the FIRST emit, the one the loop accepted"


# ------------------------------------------------------------------ (3) a failure is None

def test_3_a_failed_continuation_is_absent_never_a_degraded_idea_and_never_a_pause(tmp_path):
    """`propose`'s own `except` degrades to `_fallback`, a `fallback (…)` Idea — which, ranked
    first, makes `_refuse_degraded_proposal` PAUSE the run. A continuation that fails is simply
    absent: the panel is left with one candidate, ranks nothing and returns candidate 1. Driven
    through the engine's proposal funnel, which is where a degraded Idea would pause the run."""
    from looplab.engine.orchestrator import Engine  # noqa: F401  (engine import order)
    from looplab.events.replay import fold
    from tests.factories import make_engine

    model, _researcher, _base, panel = _chain([
        _turn(_read("r1")), _turn(_emit("e1", "cache the per-depth scorer")),
        LLMError("HTTP 503: upstream unavailable after retries")])
    engine = make_engine(tmp_path / "run", researcher=panel)
    engine._novelty_mode = "off"
    idea = engine._prepare_node_idea({"kind": "draft"}, fold(engine.store.read_all()),
                                     researcher=panel, prospective_node_id=0, source="researcher")

    assert idea is not None and idea.params == {"x": 1.0}, "candidate 1 is the proposal"
    assert not is_researcher_fallback(idea)
    assert model.rank_requests == [], "one candidate: nothing to rank, no paid ranking call"
    assert panel.last_foresight is None
    assert not getattr(engine, "_pending_create_pause", None), "no provider pause was requested"
    assert not [e for e in engine.store.read_all() if e.type == "pause"]


class _OneShotResearcher:
    """A researcher with NO session (the one-shot `LLMResearcher` shape): behind the facade the panel
    samples it independently. Its second proposal is a dead provider's degraded FALLBACK."""

    def __init__(self, degraded_from: int = 2):
        self.calls = 0
        self.degraded_from = degraded_from

    def propose(self, _state, _parent):
        self.calls += 1
        if self.calls >= self.degraded_from:
            return Idea(operator="draft", params={},
                        rationale="fallback (agent parse failed: HTTP 503 upstream)")
        return Idea(operator="improve", params={"x": 1.0}, rationale="a real proposal")


def test_3_a_degraded_candidate_is_dropped_before_ranking_and_only_a_lone_one_is_returned():
    """Ranked first, a `fallback (…)` candidate pauses the run although a real proposal sat beside
    it. Under the switch it is dropped BEFORE ranking; only when nothing else is left is it returned,
    so a genuinely dead provider still reaches the circuit breaker. MUTATION: drop the
    `is_researcher_fallback` filter -> the ranker is asked to rank the degraded candidate."""
    model = _Model([], order=(1, 0))
    panel = ForesightPanelResearcher(
        UnifiedAgent(researcher=_OneShotResearcher(), developer=_Developer(model)),
        k=2, client=model, alternatives=True)
    chosen = panel.propose(_state(), None)
    assert chosen.rationale == "a real proposal" and model.rank_requests == []

    dead = ForesightPanelResearcher(
        UnifiedAgent(researcher=_OneShotResearcher(degraded_from=1), developer=_Developer(model)),
        k=2, client=model, alternatives=True)
    assert is_researcher_fallback(dead.propose(_state(), None))
    assert model.rank_requests == [] and dead.last_foresight is None


def test_3_a_continuation_that_never_emits_makes_no_forced_parse_of_its_own():
    """The continuation's loop fallback returns None — no `forced_structured` re-ask, which is the
    paid call `_fallback` makes and the degraded Idea it can return. Only the loop's own salvage
    (one forced emit, here refused by the endpoint) is attempted."""
    paths = iter(f"f{i}.py" for i in range(100))

    def reply(messages):
        if _asks_for_alternative(messages):
            return _turn(_read(f"a{len(messages)}", next(paths)))
        if not any(m.get("role") == "tool" for m in messages):
            return _turn(_read("r1"))
        return _turn(_emit("e1", "cache the per-depth scorer"))

    model, researcher, _base, panel = _chain(reply)
    chosen = panel.propose(_state(), None)

    assert chosen.params == {"x": 1.0} and panel.last_foresight is None
    assert len(model.forced_requests) == 1, "the loop's own salvage only — no degraded re-ask"


# ------------------------------------------------------------------ (4) stops propagate

def test_4_the_spend_ceiling_propagates_and_buys_no_replacement_propose():
    model, researcher, _base, panel = _chain([
        _turn(_read("r1")), _turn(_emit("e1", "cache the per-depth scorer")),
        BudgetExceeded("spent 1.00 >= budget 1.00")])
    with pytest.raises(BudgetExceeded):
        panel.propose(_state(), None)
    assert len(model.chats) == 3, "no full `propose` was bought to replace the alternative"
    assert researcher._board_prompt_attempt == 1


def test_4_a_cancelled_phase_propagates_out_of_the_panel():
    """The owner's cancel token (`tool_loop.py::phase_cancel_scope`) firing DURING the continuation
    ends it as `PhaseCancelled`, which the panel does not turn into a smaller candidate set."""
    cancelled = [False]

    def reply(messages):
        if _asks_for_alternative(messages):
            cancelled[0] = True
            return _turn(_read("a1", "other.py"))
        if not any(m.get("role") == "tool" for m in messages):
            return _turn(_read("r1"))
        return _turn(_emit("e1", "cache the per-depth scorer"))

    model, _researcher, _base, panel = _chain(reply)
    with phase_cancel_scope(lambda: cancelled[0]):
        with pytest.raises(PhaseCancelled):
            panel.propose(_state(), None)
    assert model.rank_requests == []


# ------------------------------------------------------------------ (5) no stale session

def test_5_a_candidate_1_that_raises_is_never_continued():
    model, _researcher, _base, panel = _chain([BudgetExceeded("spent 1.00 >= budget 1.00")])
    with pytest.raises(BudgetExceeded):
        panel.propose(_state(), None)
    assert len(model.chats) == 1


class _SessionSpyResearcher:
    """A session-holding researcher whose SECOND proposal makes no provider call — the warmed-up
    surrogate's shape — and so has no session to hand back."""

    def __init__(self):
        self.calls = 0
        self.continued: list = []

    def propose(self, _state, _parent):
        self.calls += 1
        return Idea(operator="improve", params={"x": float(self.calls)}, rationale=f"p{self.calls}")

    def propose_with_session(self, state, parent):
        idea = self.propose(state, parent)
        return idea, (ProposalSession(exit="emitted") if self.calls == 1 else None)

    def propose_alternative(self, _state, _parent, session, _prior):
        self.continued.append(session)
        return Idea(operator="improve", params={"x": 9.0}, rationale="an alternative")


def test_5_no_session_from_an_earlier_call_is_ever_continued():
    """The session is PER CALL: a proposal that made no call hands back no session, and the panel
    samples independently instead of reaching for the previous call's transcript."""
    model = _Model([], order=(0, 1))
    inner = _SessionSpyResearcher()
    panel = ForesightPanelResearcher(UnifiedAgent(researcher=inner, developer=_Developer(model)),
                                     k=2, client=model, alternatives=True)
    panel.propose(_state(), None)
    first = list(inner.continued)
    panel.propose(_state(), None)

    assert len(first) == 1 and inner.continued == first, "the second call continued nothing"
    assert inner.calls == 3, "call 1: one session; call 2: two independent proposals"


def test_5_the_surrogate_is_not_a_session_and_a_dead_session_is_refused():
    """The critic's D1 carve-out: `SurrogateResearcher` forwards neither method (past warm-up its
    point makes no call), so a panel over it samples independently. And a session that ended in a
    fallback or a raise is refused before any call."""
    model = _Model([_turn(_read("r1")), _turn(_emit("e1", "one")),
                    _turn(_read("r2")), _turn(_emit("e2", "two", x=2.0))])
    researcher = ToolUsingResearcher(model, _Tools())
    surrogate = SurrogateResearcher({}, fallback=researcher)
    assert getattr(surrogate, "propose_with_session", None) is None
    assert getattr(surrogate, "propose_alternative", None) is None
    ForesightPanelResearcher(surrogate, k=2, client=model, alternatives=True).propose(_state(), None)
    assert len(model.chats) == 4 and not any(_asks_for_alternative(r) for r in model.chats)

    for dead in ("fallback", "error", ""):
        before = len(model.chats)
        assert researcher.propose_alternative(
            _state(), None, ProposalSession(messages=[{"role": "system", "content": "s"}],
                                            exit=dead), []) is None
        assert len(model.chats) == before, f"a `{dead or 'never ran'}` session was continued"


# ------------------------------------------------------------------ (6) no notes, one summary

def test_6_inside_a_handoff_scope_no_note_is_spliced_and_one_summary_is_bought():
    """The serial build opens `handoff_scope`, so after candidate 1 the ledger holds candidate 1's
    OWN brief. Spliced into the continuation (run_phase's index-1 insert) it would sit inside its
    own transcript's prefix — a second copy, and a changed prefix that re-bills the cache — and a
    second summary would be bought for nothing. MUTATION: drop `inject_notes=False` -> the notes
    appear at index 1; drop `handoff=False` -> two summaries."""
    model, researcher, _base, panel = _chain([
        _turn(_read("r1")), _turn(_emit("e1", "cache the per-depth scorer")),
        _turn(_emit("e2", "batch the shared prompt pages", x=2.0))])
    assert researcher.handoff is True
    with handoff_scope():
        panel.propose(_state(), None)
        ledger = list(_handoff_ctx.get())

    assert model.summaries == 1 and len(ledger) == 1, "exactly candidate 1's handoff brief"
    request = _alt_request(model)
    assert not any(_NOTES_MARK in str(m.get("content")) for m in request)
    assert json.dumps(request[:len(model.chats[1])]) == json.dumps(model.chats[1])


@pytest.mark.parametrize("order, alternative_won", [((1, 0), True), ((0, 1), False)])
def test_6_the_one_handoff_brief_is_the_CHOSEN_candidates_made_after_the_pick(order, alternative_won):
    """Summarized as soon as candidate 1 finished (`run_phase(handoff=True)`), the node's only brief
    described candidate 1 even when the ranker then picked the alternative — and the Developer, which
    reads the brief, builds the alternative. The summary is DEFERRED to after the pick and distilled
    from the session up to the CHOSEN candidate's end: the alternative's reads are in it exactly
    when the alternative won. Still one summary call. MUTATION: keep `handoff=True` for a session ->
    the summary runs before the ranking and never sees the alternative."""
    model, _researcher, _base, panel = _chain([
        _turn(_read("r1")), _turn(_emit("e1", "cache the per-depth scorer")),
        _turn(_read("a1", "service/prompt_pages.py")),
        _turn(_emit("e2", "batch the shared prompt pages", x=2.0))], order=order)
    with handoff_scope():
        chosen = panel.propose(_state(), None)
        ledger = list(_handoff_ctx.get())

    assert model.summaries == 1 and len(ledger) == 1
    assert model.log.index("summary") > model.log.index("rank"), "summarized AFTER the pick"
    assert ("service/prompt_pages.py" in model.summarized[0]) is alternative_won
    assert "service/latency_engine.py" in model.summarized[0], "candidate 1's reads, either way"
    assert chosen.params == ({"x": 2.0} if alternative_won else {"x": 1.0})


def test_6_a_session_whose_candidate_1_stands_alone_still_contributes_its_brief():
    """When nothing is ranked (the continuation failed), the brief is candidate 1's, as it always
    was — the deferral never loses the node's brief."""
    model, _researcher, _base, panel = _chain([
        _turn(_read("r1")), _turn(_emit("e1", "cache the per-depth scorer")),
        LLMError("HTTP 503: upstream unavailable after retries")])
    with handoff_scope():
        panel.propose(_state(), None)
        ledger = list(_handoff_ctx.get())
    assert model.summaries == 1 and len(ledger) == 1 and model.rank_requests == []


# ------------------------------------------------------------------ (7) the cap and the receipt

def _capped_script():
    paths = iter(f"src/f{i}.py" for i in range(1000))

    def reply(messages):
        if _asks_for_alternative(messages):
            return _turn(_read(f"a{len(messages)}", next(paths)))   # never emits: the cap ends it
        if not any(m.get("role") == "tool" for m in messages):
            return _turn(_read("r1"))
        return _turn(_emit("e1", "cache the per-depth scorer"))
    return reply


@pytest.mark.parametrize("order, receipt", [((0, 1), ""), ((1, 0), "turns")])
def test_7_the_alternative_is_capped_and_the_CHOSEN_candidates_receipt_is_published(order, receipt):
    """`ALTERNATIVE_MAX_TURNS` through `LoopOptions.replace`, so it WINS over an operator's larger
    `agent_max_turns` in the bundle; the cut is announced (`last_budget_exhausted = "turns"`). The
    engine reads the receipt off the panel (`roles.researcher_budget_exhausted`), and it must be the
    CHOSEN candidate's: candidate 1 converged, so picking it must not log TRUNCATED because the
    alternative after it was cut. MUTATION: drop `_chosen`'s publish -> the (0, 1) case reads
    "turns"."""
    model, researcher, agent, panel = _chain(
        _capped_script(), order=order, forced=[_emission("salvaged alternative", x=3.0)],
        loop_opts=LoopOptions(max_turns=50))
    chosen = panel.propose(_state(), None)

    alt_start = model.chats.index(_alt_request(model))
    assert len(model.chats) - alt_start == ALTERNATIVE_MAX_TURNS
    assert researcher.loop_opts["max_turns"] == 50, "the operator's bundle is untouched"
    assert researcher_budget_exhausted(agent) == "turns", "the LAST call was the capped one"
    assert researcher_budget_exhausted(panel) == receipt
    assert chosen.params == ({"x": 1.0} if receipt == "" else {"x": 3.0})


def test_7_a_new_call_never_reads_the_previous_calls_published_receipt():
    """The panel's own receipt is dropped at the top of every `propose`; a later call that takes a
    path which publishes none reads through to the base again."""
    model, _researcher, agent, panel = _chain(_capped_script(), order=(1, 0),
                                              forced=[_emission("salvaged", x=3.0)])
    panel.propose(_state(), None)
    assert researcher_budget_exhausted(panel) == "turns"
    panel.k = 1                                     # the pass-through path publishes nothing
    model.script = lambda _m: _turn(_emit("e9", "converged at once", x=4.0))
    panel.propose(_state(), None)
    assert "last_propose_budget_exhausted" not in vars(panel)
    assert researcher_budget_exhausted(panel) == researcher_budget_exhausted(agent) == ""


def test_7_the_cap_never_RAISES_an_operators_smaller_turn_limit():
    """`agent_max_turns` 3 means an alternative gets 3 turns, never the 8 of
    `ALTERNATIVE_MAX_TURNS`: the cap is `min(configured, 8)` when one is configured. MUTATION:
    `replace(max_turns=ALTERNATIVE_MAX_TURNS)` unconditionally -> 8 continuation requests."""
    model, _researcher, _agent, panel = _chain(
        _capped_script(), order=(0, 1), forced=[_emission("salvaged alternative", x=3.0)],
        loop_opts=LoopOptions(max_turns=3))
    panel.propose(_state(), None)
    alt_start = model.chats.index(_alt_request(model))
    assert len(model.chats) - alt_start == 3


# ------------------------------------------------------------------ (8) board binding + rotation

def _board(n: int = 6, size: int = 3_900) -> RunState:
    """A board whose rotated windows differ in MEMBERSHIP: six ~4k seeds against the 20k window."""
    st = _state()
    for i in range(n):
        seed = f"belief-{i} " + "y" * (size - 9)
        st.cards[f"card-{i}"] = Card(
            id=f"card-{i}", seed_statement=seed, statement=seed, verdict="open", status="proposed",
            evidence=[], selection_provenance=CardSelectionProvenance(
                action_source="card_added", action_owner_count=1, action_complete=True,
                freshness="current"))
    return st


def test_8_an_alternative_binds_to_candidate_1s_window_and_the_board_rotates_once():
    """Only candidate 1 renders the board, so the base's rotation (`_board_prompt_attempt`, the tail
    slot of `next_board_prompt_cards`) advances ONCE per proposal instead of K times — and an
    alternative's Card claim resolves against the window candidate 1 was SHOWN (the session's), not
    the next rotation. MUTATION: make `propose_alternative` re-render the board (advance
    `_board_prompt_attempt`, bind against the next window) -> the claim on a card only candidate 1
    saw is nulled, and the rotation count doubles."""
    st = _board()
    shown, rotated = next_board_prompt_cards(st, attempt=0), next_board_prompt_cards(st, attempt=1)
    only_shown = [c.id for c in shown if c.id not in {r.id for r in rotated}]
    assert only_shown, "the fixture must make the two windows differ"
    claim = only_shown[0]

    def reply(messages):
        if _asks_for_alternative(messages):
            return _turn(_emit("e2", "a second mechanism", x=2.0, card_id=claim))
        return _turn(_emit(f"e{len(messages)}", "cache the per-depth scorer"))

    model, researcher, _agent, panel = _chain(reply, order=(1, 0))
    chosen = panel.propose(st, None)
    assert chosen.card_id == claim, "the claim on a card candidate 1 was shown must bind"
    assert researcher._board_prompt_attempt == 1
    panel.propose(st, None)
    assert researcher._board_prompt_attempt == 2, "one rotation step per proposal, not K"

    # The historical path, for contrast: K independent sessions rotate K times per proposal.
    independent = ForesightPanelResearcher(panel.base, k=2, client=model, alternatives=False)
    independent.propose(st, None)
    assert researcher._board_prompt_attempt == 4


def test_8_the_session_keeps_THIS_calls_window_when_the_shared_instance_moves_on():
    """The Researcher is the shared primary: another call can publish its own
    `_visible_board_cards` while this call's loop runs. The session — and candidate 1's own claim —
    use the window computed at the top of THIS call. MUTATION: hold `self._visible_board_cards` ->
    the session carries the other call's (empty) window and every alternative's claim is nulled."""
    st = _board()
    shown = next_board_prompt_cards(st, attempt=0)
    claim = shown[0].id
    model = _Model([])
    researcher = ToolUsingResearcher(model, _Tools())

    def reply(_messages):
        researcher._visible_board_cards = []        # another call published its window meanwhile
        return _turn(_emit("e1", "cache the per-depth scorer", card_id=claim))

    model.script = reply
    idea, session = researcher.propose_with_session(st, None)
    assert idea.card_id == claim
    assert [card.id for card in session.visible_board_cards] == [card.id for card in shown]


# ------------------------------------------------------------------ (9) a copy is bounced

def test_9_an_alternative_that_copies_candidate_1_is_bounced_then_replaced():
    replies = iter([_turn(_emit("e2", "cache the per-depth scorer")),       # a copy of e1
                    _turn(_emit("e3", "batch the shared prompt pages", x=2.0))])

    def reply(messages):
        if _asks_for_alternative(messages):
            return next(replies)
        return _turn(_emit("e1", "cache the per-depth scorer"))

    model, _researcher, _base, panel = _chain(reply, order=(1, 0))
    chosen = panel.propose(_state(), None)

    bounce = next(m["content"] for m in model.chats[-1] if m.get("tool_call_id") == "e2")
    assert "SAME experiment" in bounce, "the copy was bounced with the reason"
    assert chosen.params == {"x": 2.0} and panel.last_foresight["alternatives"] == [False, True]


def test_9_a_model_that_keeps_copying_yields_no_alternative():
    """Past `emit_retries` the loop accepts whatever it gets; a copy is still not an alternative."""
    model, _researcher, _base, panel = _chain(
        lambda messages: _turn(_emit(f"e{len(messages)}", "cache the per-depth scorer")))
    chosen = panel.propose(_state(), None)
    assert chosen.params == {"x": 1.0} and panel.last_foresight is None
    assert model.rank_requests == []


# ------------------------------------------------------------------ (10) defaults, one reader

def test_10_constructor_off_product_on_legacy_off_and_one_settings_reader():
    base = ToolUsingResearcher(_Model([]), _Tools())
    assert ForesightPanelResearcher(base, k=2).alternatives is False, "OFF at the constructor"
    bare = ForesightPanelResearcher.__new__(ForesightPanelResearcher)
    bare.__dict__["base"] = type("_B", (), {"alternatives": True})()
    assert bare.alternatives is False, "a CLASS default: never the wrapped role's attribute"

    assert Settings().foresight_alternatives is True, "the product default is ON"
    assert LEGACY_CONFIG_SNAPSHOT_DEFAULTS["foresight_alternatives"] is False
    resumed = settings_from_snapshot({"max_nodes": 8, "direction": "min", "backend": "toy"})
    assert resumed.foresight_alternatives is False, "a pre-field snapshot resumes independent"
    kept = settings_from_snapshot(Settings(foresight_alternatives=True).masked_snapshot())
    assert kept.foresight_alternatives is True, "a snapshot that carries the key is untouched"

    assert with_foresight_panel(base, Settings(), None).alternatives is True
    assert with_foresight_panel(base, Settings(foresight_alternatives=False),
                                None).alternatives is False

    from _source_scan import PKG, iter_trees

    readers = []
    for path, tree in iter_trees():
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "foresight_alternatives":
                readers.append(path.relative_to(PKG).as_posix())
            elif (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "getattr"
                  and len(node.args) >= 2 and isinstance(node.args[1], ast.Constant)
                  and node.args[1].value == "foresight_alternatives"):
                readers.append(path.relative_to(PKG).as_posix())
    assert readers == ["search/researcher_stack.py"], readers


# ------------------------------------------------------------------ (11) the guard registries

def test_11_the_seams_are_registered():
    """The duck-typed seam, the evidence registry and the containment census, each for the new
    code specifically (the suites that own them check the whole tree)."""
    assert {"propose_with_session", "propose_alternative"} <= set(RESEARCHER_ACTION_ATTRS)
    for name in ("propose_with_session", "propose_alternative"):
        assert name not in vars(ForesightPanelResearcher), (
            "the panel CONSUMES these off its base; defining one would shadow the proxy")
        assert callable(getattr(ToolUsingResearcher, name))
        assert callable(getattr(UnifiedAgent, name))

    row = EVIDENCE_CONSUMERS["agents/agent.py::ToolUsingResearcher.propose_alternative -> run_phase"]
    assert row.status == FENCED
    assert row.proof.endswith("::test_an_alternative_reads_the_run_fenced_when_the_envelope_is_on")

    from _source_scan import function_tree

    handlers = [h for h in ast.walk(function_tree(ToolUsingResearcher.propose_alternative))
                if isinstance(h, ast.ExceptHandler)]
    names = [{getattr(e, "id", None) for e in (h.type.elts if isinstance(h.type, ast.Tuple)
                                                  else [h.type])} for h in handlers]
    assert names[0] == {"BudgetExceeded", "PhaseCancelled"}, "the stops are named FIRST"
    blind = handlers[names.index({"Exception"})]
    assert any(isinstance(n, ast.Call) and getattr(n.func, "id", None) == "contain"
               for n in ast.walk(blind)), "the blind handler routes through `contain`"


@pytest.mark.parametrize("envelope", [True, False], ids=["on", "off"])
def test_an_alternative_reads_the_run_fenced_when_the_envelope_is_on(envelope):
    """The `EVIDENCE_CONSUMERS` proof for `ToolUsingResearcher.propose_alternative -> run_phase`:
    the continuation reads the run with the SAME toolset, and what a tool returns arrives inside the
    run's evidence fence exactly when the envelope is on — as does the candidate list its turn
    restates (the model's own emissions, written from what its tools returned). OFF is the bare
    bytes. MUTATION: drop `**fence_kwargs(...)` from the continuation's call -> the ON case reads
    the payload bare."""
    from looplab.tools.run_tools import readonly_run_tools
    from tests.test_evidence_consumer_fences import _expected, _run_state

    state = _run_state()

    def reply(messages):
        if not _asks_for_alternative(messages):
            return _turn(_emit("e1", "move x toward the optimum", x=3.0))
        if messages[-1].get("role") == "user":
            return _turn(_call("t1", "read_code", {"node_id": 0}))
        return _turn(_emit("e2", "a different mechanism", x=2.0))

    model, _researcher, _base, panel = _chain(reply, tools=readonly_run_tools(state),
                                              envelope=envelope)
    panel.propose(state, None)

    last = model.chats[-1]
    assert [m["content"] for m in last if m.get("tool_call_id") == "t1"] == [
        _expected(state, envelope)]
    turn = next(m["content"] for m in last if m.get("role") == "user"
                and _ALT_MARK in str(m.get("content")))
    listed = "1. [improve] move x toward the optimum lowers latency — change: move x toward the " \
             "optimum (params: x=3.0)"
    assert (fence_untrusted(listed, EVIDENCE_LABEL) in turn) is envelope
    assert listed in turn


# ------------------------------------------------------ the run's stop inside the alternatives path

@pytest.mark.parametrize("stop_after_chats, expect_alternative_request", [(2, False), (3, True)])
def test_the_runs_stop_ends_the_alternatives_before_a_further_member_or_the_ranking(
        stop_after_chats, expect_alternative_request):
    """WP-STOP x WP-F (merged 2026-09-30): the continuation path asks the run's stop exactly as the
    independent path does — a stop that lands after candidate 1 buys no alternative, and one that
    lands after the alternative buys no ranking (and no verifier, no deferred brief); candidate 1
    comes back and nothing is recorded as a foresight pick. MUTATION: drop the loop's `run_halted()`
    break -> the first case pays an alternative request; drop the pre-rank check -> the second case
    pays a ranking."""
    from looplab.core.phase_events import run_halt_scope
    model, _researcher, _base, panel = _chain(
        [_turn(_read("r1")), _turn(_emit("e1", "cache the per-depth scorer")),
         _turn(_emit("e2", "batch the shared prompt pages", x=2.0))], order=(1, 0))
    with run_halt_scope(lambda: len(model.chats) >= stop_after_chats):
        idea = panel.propose(_state(), None)
    assert "cache the per-depth scorer" in (idea.rationale or "")
    assert any(_asks_for_alternative(req) for req in model.chats) is expect_alternative_request
    assert model.rank_requests == []
    assert model.summaries == 0
    assert panel.last_foresight is None


# ------------------------------------------------------------------ (12) the session's own ceilings
# The critic, 2026-09-30: 69.2's `token_budget` bounds a SESSION, and a continuation of candidate 1's
# session was handed a fresh one — candidate 1 cut at 400 tokens under a 250 budget, then the
# alternative spent 400 more re-sending the same transcript.
class _Billed(_Model):
    """`_Model` with the run's accountant: every chat commits `per` tokens on this thread."""

    def __init__(self, script, *, per=100, **kw):
        super().__init__(script, **kw)
        from looplab.core.llm import CostAccountant
        self.accountant, self.per = CostAccountant(), per

    def chat(self, messages, tools=None, tool_choice="auto", **kw):
        self.accountant.add(0.0, usage={"prompt_tokens": self.per - 1, "completion_tokens": 1,
                                        "total_tokens": self.per})
        return super().chat(messages, tools, tool_choice, **kw)


def _billed_chain(script, *, budget, forced=()):
    from looplab.agents.agent import ToolUsingResearcher
    model = _Billed(script, order=(0, 1), forced=list(forced))
    researcher = ToolUsingResearcher(model, _Tools(), loop_opts=LoopOptions(token_budget=budget))
    panel = ForesightPanelResearcher(researcher, k=2, client=model, alternatives=True)
    return model, researcher, panel


def test_12_a_session_its_token_ceiling_cut_is_not_continued():
    """MUTATION: drop the spend clause from `continuable` -> an alternative request is sent."""
    def reply(messages):
        return _turn(_read(f"r{len(messages)}", f"src/f{len(messages)}.py"))   # reads until cut
    model, researcher, panel = _billed_chain(reply, budget=250,
                                             forced=[_emission("salvaged at the ceiling")])
    panel.propose(_state(), None)
    assert researcher_budget_exhausted(researcher) == "tokens"
    assert not any(_ALT_MARK in str(req[-1].get("content")) for req in model.chats), (
        "a session cut by its token ceiling was continued under a fresh one")


def test_12_a_continued_session_runs_on_what_is_left_of_its_token_ceiling():
    """Candidate 1 commits 200 tokens (a read, an emit) of a 1,000 budget; the alternative's own
    loop is started with the 800 left. MUTATION: hand it `self.loop_opts` unchanged -> 1,000."""
    from looplab.core.phase_events import PHASE_STARTED, phase_sink_scope
    model, _researcher, panel = _billed_chain(_capped_script(), budget=1000)
    rows = []
    with phase_sink_scope(lambda t, d: rows.append((t, d))):
        panel.propose(_state(), None)
    started = {d.get("label"): d for t, d in rows if t == PHASE_STARTED}
    assert started["Researcher·propose"]["token_budget"] == 1000
    assert started["Researcher·alternative"]["token_budget"] == 800, started


def test_12_no_continuation_is_measured_from_another_thread():
    """The allowance is read off the thread that ran candidate 1; from another thread it cannot be,
    and the continuation is refused rather than handed a fresh ceiling."""
    import threading
    model, researcher, _panel = _billed_chain(_capped_script(), budget=1000)
    idea, session = researcher.propose_with_session(_state(), None)
    assert idea is not None and session.continuable
    out = []
    worker = threading.Thread(
        target=lambda: out.append(researcher.propose_alternative(_state(), None, session, [idea])))
    worker.start()
    worker.join()
    assert out == [None]


def test_12_the_brief_is_distilled_from_the_transcript_as_it_stood(monkeypatch):
    """The live list is compacted IN PLACE by later turns; the chosen candidate's brief reads the
    snapshot taken when it finished. MUTATION: index into the live list -> the alternative's turn
    (or the summary that replaced candidate 1's reads) reaches the brief."""
    import looplab.agents.agent as agent_mod
    seen = []
    monkeypatch.setattr(agent_mod, "_contribute_brief",
                        lambda client, prefix, **kw: seen.append(list(prefix)))
    session = ProposalSession()
    live = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"},
            {"role": "tool", "tool_call_id": "r1", "content": "candidate 1's read"}]
    session.hold(live, [], "fallback")
    original = list(live)
    live[:] = [live[0], {"role": "user", "content": "[summary of earlier turns]"},
               {"role": "user", "content": "ONE ALTERNATIVE experiment, please"}]
    session.pending_brief = (object(), "Researcher·propose", "the Developer", object())
    session.publish_brief(0)
    assert seen == [original]


# ------------------------------------------------------------------ a session holds ITS cutoff

def _another_call_writes(monkeypatch, researcher, value: str) -> None:
    """A `run_phase` after which ANOTHER proposal on the same shared Researcher writes its cutoff:
    the card lane and the offloaded serial build propose through one instance, and the attribute is
    whichever call wrote last (the critic drove the same interleaving on two threads)."""
    import looplab.agents.agent as agent_mod
    real = agent_mod.run_phase

    def run_phase(*a, **kw):
        out = real(*a, **kw)
        researcher.last_budget_exhausted = value
        return out

    monkeypatch.setattr(agent_mod, "run_phase", run_phase)


def test_a_clean_session_is_not_handed_another_call_s_cutoff(monkeypatch):
    """crit_v45 M2: `session.hold(cutoff=self.last_budget_exhausted)` read the SHARED attribute — a
    session that emitted cleanly held the other call's `tokens` and lost its alternative. MUTATION:
    hold the attribute at `propose` or at `propose_alternative` -> red."""
    model = _Model([_turn(_read("r1")), _turn(_emit("e1", "cache the per-depth scorer")),
                    _turn(_emit("e2", "batch the shared prompt pages", x=2.0))])
    researcher = ToolUsingResearcher(model, _Tools())
    _another_call_writes(monkeypatch, researcher, "tokens")
    idea, session = researcher.propose_with_session(_state(), None)
    assert session.cutoff == "" and session.continuable, "a clean emit stays continuable"
    assert researcher.propose_alternative(_state(), None, session, [idea]) is not None
    assert session.cutoff == "", "the continuation holds its own cutoff too"


def test_a_session_cut_short_stays_cut_when_another_call_resets_the_attribute(monkeypatch):
    """The reverse interleaving: this call hit its turn cap, the other call's `propose` reset the
    attribute to "" before the session was held — so the session recorded no cutoff at all (the
    critic's driven case was a WALL-CLOCK cut, a spend cutoff, and that session was then continued
    past its own ceiling). MUTATION: hold the attribute at `propose` -> the cutoff is lost."""
    model = _Model(lambda messages: _turn(_read(f"r{len(messages)}")),
                   forced=[_emission("salvaged at the cap")])
    researcher = ToolUsingResearcher(model, _Tools(), loop_opts=LoopOptions(max_turns=1))
    _another_call_writes(monkeypatch, researcher, "")
    _idea, session = researcher.propose_with_session(_state(), None)
    assert (session.exit, session.cutoff) == ("salvaged", "turns")


def test_a_continuation_cut_short_holds_its_own_cutoff():
    """crit_v51 survivor B4: `propose_alternative`'s own cutoff must reach the session it holds, not
    only the shared attribute. Candidate 1 and the continuation both hit a one-turn cap; the session's
    cutoff is cleared between them so only the continuation's write can set it. MUTATION: drop
    `cutoff[0] = ...` from the continuation's `_note_cutoff` -> ""."""
    model = _Model(lambda messages: _turn(_read(f"r{len(messages)}")),
                   forced=[_emission("first at the cap"), _emission("second at the cap", x=2.0)])
    researcher = ToolUsingResearcher(model, _Tools(), loop_opts=LoopOptions(max_turns=1))
    idea, session = researcher.propose_with_session(_state(), None)
    assert session.cutoff == "turns" and session.continuable, "premise: a turn cut may continue"
    session.cutoff = ""
    assert researcher.propose_alternative(_state(), None, session, [idea]) is not None
    assert session.cutoff == "turns"


def test_the_prompt_shows_the_window_this_call_binds_against(monkeypatch):
    """crit_v51 F5: the brief rendered the SHARED `_visible_board_cards`, the emit binds against this
    call's own window — another proposal publishing its window between the two left the model looking
    at cards its claim could not bind to. MUTATION: render `self._visible_board_cards` -> the other
    call's card is in the prompt."""
    import looplab.agents.agent as agent_mod
    mine = [Card(id=f"card-{i}", statement=f"mine {i}") for i in range(2)]
    theirs = [Card(id="card-9", statement="theirs 9")]
    monkeypatch.setattr(agent_mod, "next_board_prompt_cards", lambda *a, **k: list(mine))
    model = _Model([_turn(_emit("e1", "cache the per-depth scorer"))])
    researcher = ToolUsingResearcher(model, _Tools())
    real_offers = agent_mod.offers_tool

    def another_call_publishes(*args, **kwargs):
        researcher._visible_board_cards = list(theirs)
        return real_offers(*args, **kwargs)

    monkeypatch.setattr(agent_mod, "offers_tool", another_call_publishes)
    researcher.propose_with_session(_state(), None)
    brief = next(m["content"] for m in model.chats[0] if m["role"] == "user")
    assert "card-0" in brief and "card-1" in brief, "premise: the board reached the brief"
    assert "card-9" not in brief


class _GoalTools:
    """A run-aware provider: `bind_state` REBINDS it, as `RunTools` does."""

    def __init__(self):
        self.state = None

    def bind_state(self, state, parent=None):
        self.state = state

    def specs(self):
        return [{"type": "function", "function": {
            "name": "run_goal", "description": "The bound run's goal.",
            "parameters": {"type": "object", "properties": {}}}}]

    def execute(self, name, args):
        return f"goal of the bound run: {self.state.goal}"


def test_a_proposal_s_tools_answer_about_its_own_state(monkeypatch):
    """crit_v51 F4: `propose` rebound the SHARED toolset (`bind_state` mutates a provider), so a
    second proposal on the same instance rebinding it mid-loop made the first call's `run_goal`
    answer the OTHER run's goal. Each call now runs on its own view (`tool_loop.py::bound_toolset`),
    and the continuation keeps candidate 1's. MUTATION: bind `self.tools` and run on it -> 'B'."""
    import looplab.agents.agent as agent_mod
    shared = _GoalTools()
    model = _Model([_turn(_call("g1", "run_goal", {})),
                    _turn(_emit("e1", "cache the per-depth scorer")),
                    _turn(_call("g2", "run_goal", {})),
                    _turn(_emit("e2", "batch the shared prompt pages", x=2.0))])
    researcher = ToolUsingResearcher(model, shared)
    real = agent_mod.run_phase

    def another_call_rebinds(*args, **kwargs):
        shared.bind_state(RunState(goal="B: maximise recall", direction="max"))
        return real(*args, **kwargs)

    monkeypatch.setattr(agent_mod, "run_phase", another_call_rebinds)
    mine = RunState(goal="A: minimise latency", direction="min")
    idea, session = researcher.propose_with_session(mine, None)
    assert researcher.propose_alternative(mine, None, session, [idea]) is not None
    answers = [m["content"] for m in session.messages if m.get("role") == "tool"
               and "goal of the bound run" in str(m.get("content"))]
    assert len(answers) == 2 and all("A: minimise latency" in a for a in answers), answers


@pytest.mark.parametrize("order", [(0, 1), (1, 0)])
def test_the_panel_publishes_the_chosen_candidate_s_own_receipt(monkeypatch, order):
    """crit_v51 F3: the panel read each candidate's receipt off the base's SHARED attribute, so a
    concurrent proposal cut by its ceiling made a converged pick read `tokens` (the engine then
    logged it truncated). It reads the session's own cutoff now — candidate 1's and the
    continuation's alike (both picks). MUTATION: read `researcher_budget_exhausted(self.base)` at
    either site -> 'tokens'."""
    model, researcher, _base, panel = _chain(
        [_turn(_read("r1")), _turn(_emit("e1", "cache the per-depth scorer")),
         _turn(_emit("e2", "batch the shared prompt pages", x=2.0))], order=order)
    _another_call_writes(monkeypatch, researcher, "tokens")
    panel.propose(_state(), None)
    assert panel.last_foresight is not None, "premise: two candidates were ranked"
    assert panel.last_propose_budget_exhausted == ""


def test_every_reader_of_one_proposal_reads_the_same_bound_view(monkeypatch):
    """crit_v53 N3: the run-tools offer, the inventory block, the workspace token and the loop each
    read the toolset, and a reader left on the SHARED provider reads an unbound one — driven, the
    inventory block of an unbound `RunTools` rendered '' where the view's named the run's rows. The
    continuation runs on candidate 1's very view. MUTATIONS: any of the four reads `self.tools`; the
    continuation binds a fresh view (or the session does not keep the one it ran on)."""
    import looplab.agents.agent as agent_mod

    seen: dict = {"offers": [], "inventory": [], "workspace": [], "loop": []}
    real = {name: getattr(agent_mod, name) for name in
            ("offers_tool", "answered_by_context", "_researcher_workspace", "run_phase")}
    monkeypatch.setattr(agent_mod, "offers_tool", lambda tools, name: (
        seen["offers"].append(tools), real["offers_tool"](tools, name))[1])
    monkeypatch.setattr(agent_mod, "answered_by_context", lambda tools: (
        seen["inventory"].append(tools), real["answered_by_context"](tools))[1])
    monkeypatch.setattr(agent_mod, "_researcher_workspace", lambda store, tools=None: (
        seen["workspace"].append(tools), real["_researcher_workspace"](store, tools))[1])
    monkeypatch.setattr(agent_mod, "run_phase", lambda client, tools, *a, **kw: (
        seen["loop"].append(tools), real["run_phase"](client, tools, *a, **kw))[1])
    shared = _GoalTools()
    model = _Model([_turn(_emit("e1", "cache the per-depth scorer")),
                    _turn(_emit("e2", "batch the shared prompt pages", x=2.0))])
    researcher = ToolUsingResearcher(model, shared)
    state = RunState(goal="A: minimise latency", direction="min")
    idea, session = researcher.propose_with_session(state, None)
    assert researcher.propose_alternative(state, None, session, [idea]) is not None
    view = session.tools
    assert view is not shared and view.state is state and shared.state is None
    assert [len(seen[k]) for k in ("offers", "inventory", "workspace", "loop")] == [1, 1, 1, 2]
    assert all(tools is view for calls in seen.values() for tools in calls), seen


def test_a_proposal_cut_by_its_budget_that_then_raised_keeps_its_receipt(monkeypatch):
    """crit_v53 N5: the loop announced its wall-clock cutoff and the phase then raised; the error
    exit held the session with no cutoff, so the panel published '' ("converged") where the bound had
    fired. MUTATION: hold the error exit without `cutoff=`."""
    import looplab.agents.agent as agent_mod

    real = agent_mod.run_phase

    def cut_then_raise(*args, **kwargs):
        if kwargs.get("label") == "Researcher·propose":
            kwargs["on_budget"]({"kind": "time", "turns": 3, "seconds": 99.0})
            raise LLMError("HTTP 502 from the salvage call")
        return real(*args, **kwargs)

    monkeypatch.setattr(agent_mod, "run_phase", cut_then_raise)
    _model, researcher, _agent, panel = _chain([_turn(_emit("e1", "x"))], forced=[_emission("f")])
    panel.propose(_state(), None)
    assert researcher_budget_exhausted(panel) == "time"


class _SlowPaid(_Model):
    """`_Model` whose every chat takes 30 s of a FAKE clock and commits $0.25 on this thread."""

    def __init__(self, script, clock, **kw):
        super().__init__(script, **kw)
        from looplab.core.llm import CostAccountant
        self.accountant, self.clock = CostAccountant(), clock

    def chat(self, messages, tools=None, tool_choice="auto", **kw):
        self.clock.now += 30.0
        self.accountant.add(0.25, usage={"prompt_tokens": 9, "completion_tokens": 1,
                                         "total_tokens": 10})
        return super().chat(messages, tools, tool_choice, **kw)


class _FakeClock:
    now = 1000.0

    def monotonic(self):
        return self.now


def _continued_with(monkeypatch, *, wall, money, spent_before=0.0):
    """Candidate 1 (a read, an emit: 60 s and $0.50 of the fake clock and accountant), then one
    continuation. Returns what the alternative's loop was handed and the model's requests.
    `spent_before` is what this THREAD committed before the session began (an earlier session)."""
    import looplab.agents.agent as agent_mod
    from looplab.core.llm_budget import note_committed_cost

    if spent_before:
        note_committed_cost(spent_before)

    clock = _FakeClock()
    monkeypatch.setattr(agent_mod, "time", clock)   # agent.py's session clock only
    seen: dict = {}
    real = agent_mod.run_phase

    def spy(*args, **kwargs):
        seen[kwargs.get("label")] = (kwargs.get("time_budget_s"), kwargs.get("cost_budget_usd"))
        return real(*args, **kwargs)

    monkeypatch.setattr(agent_mod, "run_phase", spy)
    model = _SlowPaid(_capped_script(), clock)
    researcher = ToolUsingResearcher(model, _Tools(), loop_opts=LoopOptions(
        time_budget_s=wall, cost_budget_usd=money))
    idea, session = researcher.propose_with_session(_state(), None)
    researcher.last_budget_exhausted = "marker"
    before = (len(session.messages), researcher.last_budget_exhausted)
    researcher.propose_alternative(_state(), None, session, [idea])
    return seen, model, before, (len(session.messages), researcher.last_budget_exhausted)


def test_12_a_continued_session_runs_on_what_is_left_of_its_wall_clock_and_money(monkeypatch):
    """crit_v45 L3: the continuation was handed the whole `agent_time_budget_s` and a fresh money
    ceiling after candidate 1 had spent most of both. It runs on what is LEFT, measured from where
    candidate 1 began. MUTATION: hand it `self.loop_opts`' own wall clock or money ceiling."""
    seen, _model, _before, _after = _continued_with(monkeypatch, wall=100.0, money=1.0)
    assert seen["Researcher·propose"] == (100.0, 1.0)
    assert seen["Researcher·alternative"] == (40.0, 0.5)
    # What the thread spent BEFORE the session is not the session's (crit_v57 L4 a08, MUTATION: take
    # the session's money baseline from 0 -> 0.2).
    seen, _model, _before, _after = _continued_with(monkeypatch, wall=100.0, money=1.0,
                                                    spent_before=0.3)
    assert seen["Researcher·alternative"] == (40.0, 0.5)


@pytest.mark.parametrize("wall, money", [(60.0, 5.0), (500.0, 0.5)])
def test_12_a_session_with_nothing_left_is_not_continued_and_is_left_untouched(monkeypatch, wall,
                                                                               money):
    """Nothing left of the wall clock (60 s spent of 60) or the money ($0.50 of $0.50): no
    continuation — and the refusal is decided before the transcript or the receipt is touched
    (crit_v45 NIT). MUTATIONS: `<= 0` -> `< 0`; check after appending the alternative turn."""
    seen, model, before, after = _continued_with(monkeypatch, wall=wall, money=money)
    assert "Researcher·alternative" not in seen
    assert not any(_asks_for_alternative(req) for req in model.chats)
    assert after == before


def test_the_panel_notes_the_chosen_candidate_s_receipt_last_into_the_caller_s_scope():
    """doc 69 69.37: the engine reads a proposal's receipt from the scope its call opened, and the
    panel's members note theirs there in the order they ran — so the panel notes the CHOSEN
    candidate's receipt LAST, or the scope answered with whichever member ran last. MUTATION: drop
    the note from `_chosen` -> "time" (the last member), not "" (the chosen one)."""
    from looplab.agents.propose_receipts import (note_propose_receipt, propose_receipt_scope,
                                                 scoped_budget_exhausted)

    model = _Model([], order=(1, 0))
    panel = ForesightPanelResearcher(
        UnifiedAgent(researcher=_OneShotResearcher(), developer=_Developer(model)),
        k=2, client=model, alternatives=True)
    with propose_receipt_scope() as box:
        for member in ("turns", "", "time"):          # the members, noting as they ran
            note_propose_receipt(member)
        chosen = panel._chosen(["a", "b", "c"], 1, ["turns", "", "time"])
    assert chosen == "b" and panel.last_propose_budget_exhausted == ""
    assert scoped_budget_exhausted(box, panel) == ""


def test_12_a_session_past_the_float_range_is_not_continued_and_does_not_raise(monkeypatch):
    """crit_v57 L2, driven through the panel: two float-max cost reports on candidate 1's emit turn,
    a money ceiling on, and the continuation's reading of what was left raised OverflowError — the
    already-paid candidate 1 lost with it. `tool_loop.py::_session_spend` reads that spend as `inf`
    since crit_v54 F2; this reading refuses the continuation instead. MUTATION: drop the catch."""
    import sys
    import threading
    from fractions import Fraction

    import looplab.agents.agent as agent_mod
    from looplab.agents.agent import ProposalSession

    researcher = ToolUsingResearcher(_Model([]), _Tools(), loop_opts=LoopOptions(cost_budget_usd=1.0))
    session = ProposalSession(thread=threading.get_ident(), usd_at_start=Fraction(0))
    monkeypatch.setattr(agent_mod, "thread_committed_usd_exact",
                        lambda: Fraction(sys.float_info.max) * 2)
    assert researcher._continuation_opts(session) is None
    monkeypatch.setattr(agent_mod, "thread_committed_usd_exact", lambda: Fraction(1, 4))
    assert researcher._continuation_opts(session).cost_budget_usd == 0.75
    # On another thread the session's spend cannot be measured: no continuation (crit_v57 L4 a04,
    # MUTATION: drop the thread test -> this thread's ledger is read as the session's).
    elsewhere = ProposalSession(thread=threading.get_ident() + 1, usd_at_start=Fraction(0))
    assert researcher._continuation_opts(elsewhere) is None
