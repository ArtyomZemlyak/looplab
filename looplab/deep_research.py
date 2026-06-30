"""Deep-Research stage (Phase 2): a bounded agentic step that reads across ALL results so far +
the literature/web, then writes a strategic `ResearchMemo` to steer the next batch of experiments.

This is the "go think hard" stage the search loop otherwise lacks: the ordinary Researcher proposes
one local Idea per node, whereas the DeepResearcher takes a run-wide view (every metric, every
failure) and grounds it in external sources (arXiv via `LiteratureTools`, the web via `WebTools`,
local notes via `KnowledgeTools`). It reuses the same multi-turn tool-calling shape as
`agent.ToolUsingResearcher`: the model MAY call tools, then calls `emit` once with the memo.

Recorded as an audit-only `research_completed` event (folded into `RunState.research`), NEVER into
the search DAG — so best-selection/policies are untouched. Degrades gracefully: any transport/parse
failure (or no model) yields a minimal memo rather than crashing the run.
"""
from __future__ import annotations

import json
from typing import Optional

from pydantic import BaseModel, Field

from .models import NodeStatus, ResearchMemo, RunState


class _MemoOut(BaseModel):
    """Structured shape the LLM fills via `emit` (assembled into a ResearchMemo, validated again)."""
    summary: str = ""
    reasoning: str = ""
    findings: list[str] = Field(default_factory=list)
    recommended_directions: list[str] = Field(default_factory=list)


_SYSTEM = (
    "You are a senior ML researcher doing a DEEP-RESEARCH review of an ongoing automated experiment "
    "run. You see every experiment tried so far with its metric/outcome. Think broadly: what is "
    "working, what keeps failing, what's untried. You MAY call the search/fetch tools to ground your "
    "thinking in real techniques, datasets and write-ups. Then call `emit` exactly once with: a "
    "`summary` (your conclusion in a short paragraph), `findings` (concrete observations), and "
    "`recommended_directions` (specific next experiments to try). Put your detailed deliberation in "
    "`reasoning`. Be concrete and grounded in the actual results, not generic advice."
)


def state_brief(state: RunState, max_nodes: int = 40) -> str:
    """A compact, run-wide view of every experiment for the deep-research prompt: id, operator,
    metric (or failure reason), and the Researcher's rationale. Bounded so a long run stays in
    context — keeps the best nodes plus the most recent."""
    nodes = sorted(state.nodes.values(), key=lambda n: n.id)
    if len(nodes) > max_nodes:                       # keep the head (seeds) + tail (recent)
        nodes = nodes[: max_nodes // 2] + nodes[-max_nodes // 2:]
    lines = [f"goal: {state.goal or '(unknown)'}  direction: {state.direction}"]
    best = state.best()
    if best is not None:
        lines.append(f"current best: #{best.id} metric={best.metric} ({best.operator})")
    fails = sum(1 for n in state.nodes.values() if n.status is NodeStatus.failed)
    lines.append(f"{len(state.nodes)} nodes total, {fails} failed.\nexperiments:")
    for n in nodes:
        if n.status is NodeStatus.failed:
            outcome = f"FAILED ({n.error_reason or 'error'})"
        elif n.metric is not None:
            outcome = f"metric={n.metric}"
        else:
            outcome = n.status.value
        why = (n.idea.rationale or "").strip().replace("\n", " ")[:120]
        lines.append(f"  #{n.id} {n.operator}: {outcome}" + (f" — {why}" if why else ""))
    return "\n".join(lines)


class DeepResearcher:
    """Run-wide agentic research step. `tools` is any object with .specs()/.execute(); None = no
    external grounding (the memo is then formed from the results summary alone)."""

    def __init__(self, client, tools=None, parser: str = "tool_call", max_turns: int = 0,
                 context_budget_chars: int = 0, time_budget_s: float = 0.0,
                 stuck_detection: bool = True, stuck_repeat: int = 4, stuck_alternate: int = 4,
                 auto_summary: bool = True):
        self.client = client
        self.tools = tools
        self.parser = parser
        self.max_turns = max_turns          # 0 = unlimited (config-driven via Settings.agent_max_turns)
        self.context_budget_chars = context_budget_chars
        self.time_budget_s = time_budget_s  # 0 = no wall-clock cap (Settings.agent_time_budget_s)
        # B1: no-progress guard so this "think hard" loop can't spin forever on repeated searches.
        self.stuck_detection = stuck_detection
        self.stuck_repeat = stuck_repeat
        self.stuck_alternate = stuck_alternate
        self.auto_summary = auto_summary    # C2: summarize the stale middle when the memo trace grows

    def _emit_spec(self) -> dict:
        return {"type": "function", "function": {
            "name": "emit", "description": "Emit the final research memo.",
            "parameters": _MemoOut.model_json_schema()}}

    def research(self, state: RunState, trigger: str = "") -> ResearchMemo:
        memo = ResearchMemo(at_node=len(state.nodes), trigger=trigger)
        if self.tools is not None and hasattr(self.tools, "bind_state"):
            self.tools.bind_state(state)     # let run-aware tools read the current search
        tool_specs = ((self.tools.specs() if self.tools else []) + [self._emit_spec()])
        messages = [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": state_brief(state) +
                "\nReview the run. Consult sources if useful, then emit your memo."},
        ]
        sources: list[dict] = []
        try:
            import itertools
            import time as _time
            from .agent import _force_emit, _summarizer
            from .stuck import StuckDetector
            detector = (StuckDetector(repeat_threshold=self.stuck_repeat,
                                      alternate_threshold=self.stuck_alternate)
                        if self.stuck_detection else None)
            summarize = _summarizer(self.client) if self.auto_summary else None  # build once, not per turn
            started = _time.monotonic()
            stalls = 0                  # consecutive prose turns we couldn't force into an emit
            turns = (itertools.count() if self.max_turns is None or self.max_turns <= 0
                     else range(self.max_turns))   # 0 = unlimited (config-driven)
            for _ in turns:
                if self.time_budget_s and (_time.monotonic() - started) > self.time_budget_s:
                    break               # out of wall-clock budget -> force a memo from what we have
                if self.auto_summary:
                    from .context_budget import DEFAULT_SUMMARY_CHARS, compact_history
                    messages = compact_history(
                        messages, self.context_budget_chars or DEFAULT_SUMMARY_CHARS, summarize)
                elif self.context_budget_chars:
                    from .context_budget import truncate_history
                    messages = truncate_history(messages, self.context_budget_chars)
                msg = self.client.chat(messages, tool_specs, tool_choice="auto")
                calls = msg.get("tool_calls") or []
                if not calls:
                    # Replied in prose instead of a tool call — force the memo emit now (deterministic)
                    # rather than nudge-and-hope; bounded nudge fallback if the client can't force one.
                    messages.append({"role": "assistant", "content": msg.get("content") or ""})
                    forced = _force_emit(self.client, messages, self._emit_spec())
                    if forced is not None:
                        return self._finalize(forced, memo, sources)
                    stalls += 1
                    if stalls >= 2:
                        break
                    messages.append({"role": "user", "content": "Now call `emit` with your memo."})
                    continue
                stalls = 0
                messages.append({"role": "assistant", "content": msg.get("content") or "",
                                 "tool_calls": calls})
                stuck_reason = None
                for tc in calls:
                    fn = tc.get("function", {})
                    name = fn.get("name", "")
                    raw = fn.get("arguments") or "{}"
                    try:
                        args = json.loads(raw) if isinstance(raw, str) else (raw or {})
                    except (json.JSONDecodeError, TypeError):
                        args = {}
                    if name == "emit":
                        return self._finalize(args, memo, sources)
                    result = self.tools.execute(name, args) if self.tools else "(no tools)"
                    # Record which sources were consulted (the query/url + a snippet) for the memo.
                    sources.append({"title": f"{name}({_arg_label(args)})",
                                    "url": _arg_url(args), "snippet": str(result)[:200]})
                    messages.append({"role": "tool", "tool_call_id": tc.get("id", ""),
                                     "name": name, "content": str(result)[:4000]})
                    if detector is not None:    # B1: stop searching in circles -> force the memo
                        stuck_reason = detector.push(name, args, str(result)[:4000]) or stuck_reason
                if stuck_reason:
                    messages.append({"role": "user", "content": (
                        f"Stop: you appear to be stuck ({stuck_reason}). Call `emit` with your memo now.")})
                    forced = _force_emit(self.client, messages, self._emit_spec())
                    if forced is not None:
                        return self._finalize(forced, memo, sources)
                    break
            # Ran out of turns without an emit — force a structured memo from the accumulated context.
            return self._forced(messages, memo, sources)
        except Exception as e:  # noqa: BLE001 — research is best-effort; never crash the run
            memo.summary = f"(deep research unavailable: {e})"
            memo.sources = sources
            return memo

    def _assemble(self, out: _MemoOut, memo: ResearchMemo, sources: list[dict]) -> ResearchMemo:
        memo.summary = out.summary
        memo.reasoning = out.reasoning
        memo.findings = out.findings
        memo.recommended_directions = out.recommended_directions
        memo.sources = sources
        return memo

    def _finalize(self, args: dict, memo: ResearchMemo, sources: list[dict]) -> ResearchMemo:
        try:
            return self._assemble(_MemoOut.model_validate(args), memo, sources)
        except Exception:  # noqa: BLE001 — a junk emit must not crash the run
            memo.summary = str((args or {}).get("summary", "") or "(empty memo)")[:1000]
            memo.sources = sources
            return memo

    def _forced(self, messages: list[dict], memo: ResearchMemo, sources: list[dict]) -> ResearchMemo:
        from .parse import ParseError, parse_structured
        try:
            out = parse_structured(
                self.client, messages + [{"role": "user", "content": "Emit the memo now."}],
                _MemoOut, self.parser)
            return self._assemble(out, memo, sources)
        except (ParseError, Exception):  # noqa: BLE001
            memo.summary = "(deep research produced no memo)"
            memo.sources = sources
            return memo


def _arg_label(args: dict) -> str:
    return str((args or {}).get("query") or (args or {}).get("url") or "")[:60]


def _arg_url(args: dict) -> str:
    return str((args or {}).get("url") or "")


def make_deep_researcher(settings, *, client=None, task=None) -> Optional[DeepResearcher]:
    """Build a DeepResearcher when the stage is reachable: needs a client and at least one trigger
    enabled (web_search / literature_search / a cadence / manual use). Returns None when no client
    is wired (toy/offline mode) — the engine then simply never runs the stage."""
    if client is None:
        return None
    providers = []
    if getattr(settings, "researcher_tools", True):   # run-introspection (own experiments + data)
        from .run_tools import DataTools, RunTools
        providers.append(RunTools())
        providers.append(DataTools(task))
    if getattr(settings, "knowledge_dir", None):
        from .knowledge_tools import KnowledgeTools
        providers.append(KnowledgeTools(settings.knowledge_dir))
    if getattr(settings, "literature_search", False):
        from .literature import LiteratureTools
        providers.append(LiteratureTools(enabled=True))
    if getattr(settings, "web_search", False):
        from .web import WebTools
        providers.append(WebTools(enabled=True))
    tools = None
    if providers:
        from .agent import CompositeTools
        tools = providers[0] if len(providers) == 1 else CompositeTools(providers)
    return DeepResearcher(client, tools, parser=getattr(settings, "llm_parser", "tool_call"),
                          context_budget_chars=getattr(settings, "context_budget_chars", 0),
                          max_turns=getattr(settings, "agent_max_turns", 0),
                          time_budget_s=getattr(settings, "agent_time_budget_s", 0.0),
                          stuck_detection=bool(getattr(settings, "agent_stuck_detection", True)),
                          stuck_repeat=int(getattr(settings, "agent_stuck_repeat", 4)),
                          stuck_alternate=int(getattr(settings, "agent_stuck_alternate", 4)),
                          auto_summary=bool(getattr(settings, "agent_auto_summary", True)))
