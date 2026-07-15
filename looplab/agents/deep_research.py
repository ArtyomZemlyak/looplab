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

from typing import Optional

from pydantic import BaseModel, Field

from looplab.agents.agent import drive_tool_loop
from looplab.core.llm import BudgetExceeded
from looplab.core.models import NodeStatus, ResearchMemo, RunState
from looplab.core.prompts import PromptStore, render


class _ClaimOut(BaseModel):
    """D8: one claim with its provenance — which experiments (node ids) and/or sources back it."""
    statement: str = ""
    node_ids: list[int] = Field(default_factory=list)
    urls: list[str] = Field(default_factory=list)


class _MemoOut(BaseModel):
    """Structured shape the LLM fills via `emit` (assembled into a ResearchMemo, validated again)."""
    summary: str = ""
    reasoning: str = ""
    findings: list[str] = Field(default_factory=list)
    claims: list[_ClaimOut] = Field(default_factory=list)
    recommended_directions: list[str] = Field(default_factory=list)


_SYSTEM = (
    "You are a senior ML researcher doing a DEEP-RESEARCH review of an ongoing automated experiment "
    "run. You see every experiment tried so far with its metric/outcome. "
    # 4.5: explicit sub-question planning — one-shot review misses dependent questions (the
    # deep-research surveys' tree-decomposition finding, prompt-level form).
    "FIRST break the review into 2-4 concrete sub-questions (e.g. 'why do X nodes fail', 'is the "
    "leader overfit', 'what technique is untried'), then work through them one by one — you MAY "
    "call the search/fetch tools per sub-question to ground your thinking in real techniques, "
    "datasets and write-ups. Then call `emit` exactly once with: a `summary` (your conclusion in "
    "a short paragraph), `findings` (concrete observations), `claims` — EVERY substantive claim "
    "as {statement, node_ids, urls} citing the experiment ids and/or source urls it rests on "
    "(a claim with no evidence will be flagged by the verifier) — and `recommended_directions` "
    "(specific next experiments to try). Put your detailed deliberation in `reasoning`. Be "
    "concrete and grounded in the actual results, not generic advice."
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


class _NoTools:
    """Tool-less stand-in handed to `drive_tool_loop` when no grounding tools are wired: the model
    only sees `emit` (specs() is empty), and a hallucinated tool call gets the same "(no tools)"
    observation this stage has always returned (drive_tool_loop's own no-tools reply differs)."""

    def specs(self) -> list[dict]:
        return []

    def execute(self, name: str, args: dict) -> str:
        return "(no tools)"


class DeepResearcher:
    """Run-wide agentic research step. `tools` is any object with .specs()/.execute(); None = no
    external grounding (the memo is then formed from the results summary alone)."""

    def __init__(self, client, tools=None, parser: str = "tool_call", max_turns: int = 0,
                 context_budget_chars: int | None = None, time_budget_s: float = 0.0,
                 stuck_detection: bool = True, stuck_repeat: int = 4, stuck_alternate: int = 4,
                 auto_summary: bool = True, prompts=None,
                 emit_after: int = 300, emit_force: int = 500):
        self.client = client
        self.tools = tools
        self.parser = parser
        self.prompts = prompts              # hot-reloadable PromptStore (I18, ADR-8); None = inline default
        self.max_turns = max_turns          # 0 = unlimited (config-driven via Settings.agent_max_turns)
        self.context_budget_chars = context_budget_chars
        self.time_budget_s = time_budget_s  # 0 = no wall-clock cap (Settings.agent_time_budget_s)
        # B1: no-progress guard so this "think hard" loop can't spin forever on repeated searches.
        self.stuck_detection = stuck_detection
        self.stuck_repeat = stuck_repeat
        self.stuck_alternate = stuck_alternate
        self.auto_summary = auto_summary    # C2: summarize the stale middle when the memo trace grows
        # G soft-convergence: a model that issues ever-DIFFERENT web/literature searches never trips
        # the StuckDetector (repeats only), so with the shipped defaults max_turns=0 / time_budget=0 it
        # would run unbounded ("one idea, then ~200 more reads"). These nudge/force the memo emit.
        self.emit_after = emit_after
        self.emit_force = emit_force

    def _emit_spec(self) -> dict:
        return {"type": "function", "function": {
            "name": "emit", "description": "Emit the final research memo.",
            "parameters": _MemoOut.model_json_schema()}}

    def research(self, state: RunState, trigger: str = "") -> ResearchMemo:
        memo = ResearchMemo(at_node=len(state.nodes), trigger=trigger)
        if self.tools is not None and hasattr(self.tools, "bind_state"):
            self.tools.bind_state(state)     # let run-aware tools read the current search
        messages = [
            {"role": "system", "content": render(self.prompts, "deep_research_system", _SYSTEM)},
            {"role": "user", "content": state_brief(state) +
                "\nReview the run. Consult sources if useful, then emit your memo."},
        ]
        sources: list[dict] = []

        def _record(name: str, args: dict, result: str) -> None:
            # Record which sources were consulted (the query/url + a snippet) for the memo.
            sources.append({"title": f"{name}({_arg_label(args)})",
                            "url": _arg_url(args), "snippet": str(result)[:200]})

        try:
            # The shared loop owns the mechanics this stage used to reimplement (prose-stall
            # force-emit + bounded nudge, malformed-args guard, B1 stuck detection, C2 history
            # compaction, turn/time budgets); this stage keeps only what is genuinely its own:
            # the memo prompts, the consulted-sources ledger (`on_tool_result`), its historical
            # nudge wording (prompt strings are contracts), and the no-tools observation text
            # (truthiness on purpose, matching the pre-fold `if self.tools else` guards).
            # `self_plan` stays OFF: the memo review never had an update_plan tool.
            return drive_tool_loop(
                self.client, self.tools if self.tools else _NoTools(), messages, self._emit_spec(),
                max_turns=self.max_turns,               # 0 = unlimited (config-driven)
                context_budget_chars=self.context_budget_chars,
                time_budget_s=self.time_budget_s,       # out of wall-clock budget -> memo from what we have
                finalize=lambda args: self._finalize(args, memo, sources),
                # Ran out of turns without an emit — force a structured memo from the accumulated context.
                fallback=lambda msgs: self._forced(msgs, memo, sources),
                stuck_detection=self.stuck_detection,   # B1: stop searching in circles -> force the memo
                stuck_repeat=self.stuck_repeat, stuck_alternate=self.stuck_alternate,
                emit_after=self.emit_after, emit_force=self.emit_force,   # G: bound ever-different searches
                auto_summary=self.auto_summary, self_plan=False,
                on_tool_result=_record,
                nudge_prompt="Now call `emit` with your memo.",
                stuck_prompt="Stop: you appear to be stuck ({reason}). Call `emit` with your memo now.")
        except BudgetExceeded:      # a hard budget stop must end the run, not be swallowed as a memo
            raise
        except Exception as e:  # noqa: BLE001 — research is best-effort; never crash the run
            memo.summary = f"(deep research unavailable: {e})"
            memo.sources = sources
            return memo

    def _assemble(self, out: _MemoOut, memo: ResearchMemo, sources: list[dict]) -> ResearchMemo:
        memo.summary = out.summary
        memo.reasoning = out.reasoning
        memo.findings = out.findings
        memo.claims = [c.model_dump() for c in out.claims]   # D8 evidence ledger
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
        from looplab.core.parse import ParseError, parse_structured
        try:
            out = parse_structured(
                self.client, messages + [{"role": "user", "content": "Emit the memo now."}],
                _MemoOut, self.parser)
            return self._assemble(out, memo, sources)
        except BudgetExceeded:      # a hard budget stop must end the run, not be swallowed as a memo
            raise
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
        from looplab.tools.run_tools import DataTools, RunTools
        providers.append(RunTools())
        providers.append(DataTools(task))
    if getattr(settings, "knowledge_dir", None):
        from looplab.tools.knowledge_tools import KnowledgeTools
        providers.append(KnowledgeTools(settings.knowledge_dir))
    if getattr(settings, "memory_dir", None) and getattr(settings, "cross_run_read_tools", False):
        from looplab.tools.cross_run_tools import CrossRunTools   # PART V §22 — portfolio unknowns/contradictions
        providers.append(CrossRunTools(settings.memory_dir, role="researcher"))
    if getattr(settings, "literature_search", False):
        from looplab.tools.literature import LiteratureTools
        providers.append(LiteratureTools(enabled=True))
    if getattr(settings, "web_search", False):
        from looplab.tools.web import WebTools
        providers.append(WebTools(enabled=True))
    tools = None
    if providers:
        from looplab.agents.agent import CompositeTools
        tools = providers[0] if len(providers) == 1 else CompositeTools(providers)
    # Deliberately NOT `loop_opts_from_settings(settings)`: that bundle also carries `self_plan`
    # (default ON — this stage never exposes an update_plan tool) and the D11 `summary_client`
    # (compressor_model — this stage has always compacted with its own client), so spreading it
    # would change the memo loop's behavior. Keep the explicit per-setting kwargs instead.
    # Hot-reloadable prompt store (I18, ADR-8): lets `deep_research_system.md` override the
    # built-in system prompt; no prompt_dir (or no file) keeps the inline default byte-identical.
    prompts = (PromptStore(settings.prompt_dir)
               if getattr(settings, "prompt_dir", None) else None)
    return DeepResearcher(client, tools, parser=getattr(settings, "llm_parser", "tool_call"),
                          prompts=prompts,
                          context_budget_chars=getattr(settings, "context_budget_chars", None),
                          max_turns=getattr(settings, "agent_max_turns", 0),
                          time_budget_s=getattr(settings, "agent_time_budget_s", 0.0),
                          stuck_detection=bool(getattr(settings, "agent_stuck_detection", True)),
                          stuck_repeat=int(getattr(settings, "agent_stuck_repeat", 4)),
                          stuck_alternate=int(getattr(settings, "agent_stuck_alternate", 4)),
                          auto_summary=bool(getattr(settings, "agent_auto_summary", True)),
                          emit_after=int(getattr(settings, "agent_emit_after", 300)),
                          emit_force=int(getattr(settings, "agent_emit_force", 500)))
