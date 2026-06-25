"""Unified self-driving agent (NEW): ONE object that plays Researcher + Developer
(+ Strategist + action-pilot) across pipeline stages, choosing its own model/toolset per
stage. It implements BOTH the `Researcher` and `Developer` Protocols (and the `Strategist`
Protocol via `decide`), so the orchestrator wires the SAME object as `researcher`,
`developer`, and `strategist` — the engine interface is unchanged.

Design: the merge is a *facade* over the already-tested split-role backends. The agentic
loop, structured-output parsing, sweep contract, validation/best-of-N wrappers, and H3
per-role models are reused verbatim by composing the normal `make_roles` output (built with
`unified_agent=False`) and rebinding a per-stage LLM client. The genuinely-new unified
behavior — the action `pilot` (self-driving the next macro action within a pure legal-action
gate) and the absorbed `strategy` stage — lives here on top of that reused core.

Replay-safety is preserved exactly as for the split roles: every decision the agent makes is
recorded as an event (`node_created` / `strategy_decision` / `agent_decision`) and replayed
from the log, never re-invoked.
"""
from __future__ import annotations

import json
from typing import Optional

from .agent import drive_tool_loop
from .models import Idea, Node, RunState


class UnifiedAgent:
    """Facade composing per-stage role backends behind one identity.

    `researcher` drives `propose` (an Idea), `developer` drives `implement`/`repair` (code),
    `strategist` drives `decide` (a Strategy at meta-cadence). `pilot_client`/`pilot_tools`
    drive `choose_action` (the next macro action). Each backend is already bound to its own
    per-stage client (H3), so `propose` and `implement` can run on different models.
    """

    def __init__(self, *, researcher, developer, strategist=None,
                 pilot_client=None, pilot_tools=None, stage_clients=None, prompts=None):
        # Internal per-stage backends. Named `researcher`/`developer`/`strategist` (not _-prefixed)
        # so the engine's cost roll-up walk (_emit_llm_cost) descends into them and finds every
        # per-stage CostAccountant.
        self.researcher = researcher
        self.developer = developer
        self.strategist = strategist
        self._pilot_client = pilot_client
        self._pilot_tools = pilot_tools
        # Per-stage clients NOT reachable via researcher/developer (strategy, pilot) — surfaced so
        # the engine's cost roll-up can find their CostAccountants. Deduped by identity downstream.
        self.stage_clients = list(stage_clients or [])
        self.prompts = prompts
        # Raw probe developer (bypasses any ValidatingDeveloper) for ablation probes: the engine
        # reads `getattr(self.developer, "inner", self.developer)`, so expose the unwrapped inner.
        self.inner = getattr(developer, "inner", developer)
        # Forwarded so make_roles-style introspection keeps working.
        self.bounds = getattr(researcher, "bounds", None)
        self.space_hint = getattr(researcher, "space_hint", "")
        # Developer-protocol audit attributes the orchestrator reads off `self.developer`.
        self.last_files: dict = {}
        self.last_deleted: list = []
        self.last_report = None

    # ----------------------------------------------------------- Researcher
    def propose(self, state: RunState, parent: Optional[Node]) -> Idea:
        # The engine sets ephemeral hints via `setattr(self.researcher, ...)` where self.researcher
        # is THIS agent; forward them to the internal researcher that actually reads them.
        for attr in ("_complexity_hint", "_sweep_hint"):
            if hasattr(self, attr):
                setattr(self.researcher, attr, getattr(self, attr))
        return self.researcher.propose(state, parent)

    @property
    def brief(self) -> str:
        return getattr(self.developer, "brief", "")

    # ----------------------------------------------------------- Developer
    def implement(self, idea: Idea) -> str:
        code = self.developer.implement(idea)
        self._sync_dev_audit()
        return code

    def repair(self, idea: Idea, code: str, error: str) -> str:
        rep = getattr(self.developer, "repair", None)
        out = rep(idea, code, error) if callable(rep) else self.developer.implement(idea)
        self._sync_dev_audit()
        return out

    def _sync_dev_audit(self) -> None:
        self.last_files = getattr(self.developer, "last_files", {}) or {}
        self.last_deleted = getattr(self.developer, "last_deleted", []) or []
        self.last_report = getattr(self.developer, "last_report", None)

    def audit_extra(self) -> dict:
        fn = getattr(self.developer, "audit_extra", None)
        return fn() if callable(fn) else {}

    # ----------------------------------------------------------- Strategist
    def decide(self, state: RunState, ctx):
        """Strategist Protocol: delegate to the strategy-stage backend (None => keep current)."""
        if self.strategist is None:
            return None
        return self.strategist.decide(state, ctx)

    # ----------------------------------------------------------- Pilot (self-driving)
    _PILOT_SYSTEM = (
        "You are the pilot of an autonomous ML research loop. Exactly one macro action runs "
        "next. You are given the LEGAL actions (you may pick ONLY from these — the pipeline "
        "forbids anything else) and the search policy's recommended action. Consult the run if "
        "useful, then call `choose_action` exactly once with the `index` of your chosen action "
        "from the legal list and a one-sentence `rationale`."
    )

    def choose_action(self, state: RunState, legal: list[dict], recommended: Optional[dict] = None,
                      *, brief: str = "") -> dict:
        """Pick the next macro action from `legal` (the pure legal-action gate). Returns a dict
        ``{"index": int, "rationale": str}``. Structurally cannot escape `legal`: the emit schema
        constrains `index` to the legal range, and any malformed/out-of-range emit falls back to
        the policy's `recommended` (or the first legal action). The CALLER turns the index into the
        concrete action and records the `agent_decision` event — this method has no side effects."""
        n = len(legal)
        if n == 0:
            return {"index": -1, "rationale": "no legal actions"}
        default_idx = 0
        if recommended is not None:
            for i, a in enumerate(legal):
                if a.get("kind") == recommended.get("kind") and \
                        a.get("parent_id") == recommended.get("parent_id"):
                    default_idx = i
                    break
        if self._pilot_client is None:        # pilot model not wired -> take the policy recommendation
            return {"index": default_idx, "rationale": "policy recommendation (no pilot model)"}
        menu = "\n".join(
            f"  [{i}] {a.get('kind')}" + (f" parent={a['parent_id']}" if a.get("parent_id") is not None else "")
            for i, a in enumerate(legal))
        rec = ("\nPolicy recommends index "
               f"{default_idx}: {legal[default_idx].get('kind')}.") if recommended is not None else ""
        messages = [
            {"role": "system", "content": self._PILOT_SYSTEM},
            {"role": "user", "content": (brief + "\nLegal actions:\n" + menu + rec +
                                         "\nChoose the next action.").strip()},
        ]
        emit_spec = {"type": "function", "function": {
            "name": "choose_action",
            "description": "Choose the next macro action by its index in the legal list.",
            "parameters": {"type": "object", "properties": {
                "index": {"type": "integer", "minimum": 0, "maximum": n - 1,
                          "description": "Index of the chosen action in the legal list."},
                "rationale": {"type": "string"}},
                "required": ["index"]}}}

        def _finalize(args: dict) -> dict:
            try:
                idx = int((args or {}).get("index"))
            except (TypeError, ValueError):
                idx = default_idx
            if not (0 <= idx < n):           # out-of-range -> safe fallback, never escapes `legal`
                idx = default_idx
            return {"index": idx, "rationale": str((args or {}).get("rationale", ""))[:300]}

        def _fallback(_messages) -> dict:
            return {"index": default_idx, "rationale": "fallback: policy recommendation"}

        if self._pilot_tools is not None and hasattr(self._pilot_tools, "bind_state"):
            self._pilot_tools.bind_state(state, None)
        return drive_tool_loop(self._pilot_client, self._pilot_tools, messages, emit_spec,
                               max_turns=3, finalize=_finalize, fallback=_fallback)
