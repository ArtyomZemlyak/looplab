"""Tool-using Researcher (ADR-16): a bounded multi-turn agent loop where the LLM may
call retrieval tools (grep / kb_search / read) before emitting its final structured
Idea. Realizes "the agent chooses lexical-nav vs semantic" — retrieval is a toolset
the model drives, not a fixed pipeline.

Drops in behind the same `Researcher` Protocol as the plain LLMResearcher, so the
orchestrator is unchanged.
"""
from __future__ import annotations

import itertools
import json
import time
from typing import Optional

from .models import Idea, Node, RunState
from .parse import ParseError, parse_structured
from .prompts import PromptStore, render
from .roles import _clamp_fill, _state_brief


class CompositeTools:
    """Merge several tool providers (each with .specs()/.execute()) into one toolset,
    so the Researcher can use knowledge + skills + memory tools together."""

    def __init__(self, providers: list):
        self.providers = providers
        self._route: dict[str, object] = {}
        for p in providers:
            for spec in p.specs():
                self._route[spec["function"]["name"]] = p

    def specs(self) -> list[dict]:
        return [s for p in self.providers for s in p.specs()]

    def execute(self, name: str, args: dict) -> str:
        p = self._route.get(name)
        return p.execute(name, args) if p else f"(unknown tool: {name})"

    def bind_state(self, state, parent=None) -> None:
        """Forward the live run to any run-aware provider (RunTools/DataTools); others ignore it."""
        for p in self.providers:
            if hasattr(p, "bind_state"):
                p.bind_state(state, parent)


def _force_emit(client, messages: list, emit_spec: dict) -> Optional[dict]:
    """Force the model to return the structured emit via a forced `tool_choice`, returning the parsed
    args dict — or None when the client/endpoint can't force a tool call (a fake without
    `complete_tool`, an endpoint that ignores tool_choice, or a transport blip). Used when the model
    answered in prose instead of calling a tool: rather than nudge-and-hope (a reasoning model often
    keeps replying in prose — the bug that left the boss "talking but not acting"), we make ONE
    forced-emit call so we deterministically get a structured result. `complete_tool` always names
    the forced tool `emit` and returns `calls[0].arguments`, so it works for any emit schema
    regardless of `emit_spec`'s function name."""
    schema = (emit_spec.get("function") or {}).get("parameters") or {}
    try:
        return client.complete_tool(list(messages), schema)
    except Exception:  # noqa: BLE001 - no complete_tool / endpoint ignored tool_choice / transport
        return None


def drive_tool_loop(client, tools, messages: list, emit_spec: dict, *,
                    max_turns: int = 0, context_budget_chars: int = 0,
                    time_budget_s: float = 0.0, finalize=None, fallback=None):
    """Multi-turn tool loop shared by every tool-using agent (Researcher, unified-agent pilot/triage,
    Boss, genesis scout, cross-run report). The model MAY call the provided retrieval tools across
    turns; when it calls the emit function (named in `emit_spec`), `finalize(args)` is returned. If
    the loop ends without an emit, `fallback(messages)` is returned. `tools` may be None (emit-only).

    Limits are caller-supplied (and ultimately config-driven, NOT hardcoded), and default to
    UNLIMITED so the agent is never cut off mid-reasoning:
      - `max_turns` (0 = unlimited): max number of tool turns before falling through to `fallback`.
      - `time_budget_s` (0 = off): WALL-CLOCK ceiling across turns — a new turn is not started once
        exceeded (a turn already in flight isn't interrupted — that's the LLM client's per-call
        timeout's job). Set it to bound an interactive request behind a proxy gateway timeout.

    Termination under "unlimited": when the model answers WITHOUT calling a tool (it considers
    itself done), we FORCE the structured emit immediately (`_force_emit`) and finish — so a prose
    reply becomes a real result instead of looping forever. If the client can't force a tool call,
    we fall back to a bounded nudge-and-retry (two consecutive prose turns ⇒ stop) so the loop
    always terminates regardless of `max_turns`.

    Pure mechanics: callers own prompt construction, the emit schema, and result coercion —
    so the SAME loop drives an Idea emit, a code emit, an action choice, or a strategy emit.
    """
    emit_name = emit_spec["function"]["name"]
    tool_specs = ((tools.specs() if tools is not None else []) + [emit_spec])
    started = time.monotonic()
    stalls = 0                          # consecutive prose turns we couldn't turn into a forced emit
    turns = itertools.count() if max_turns is None or max_turns <= 0 else range(max_turns)
    for _ in turns:
        if time_budget_s and (time.monotonic() - started) > time_budget_s:
            break                       # out of wall-clock budget -> finalize from what we have
        if context_budget_chars:        # H4: middle-truncate stale tool output if too long
            from .context_budget import truncate_history
            messages = truncate_history(messages, context_budget_chars)
        msg = client.chat(messages, tool_specs, tool_choice="auto")
        calls = msg.get("tool_calls") or []
        if not calls:
            # Model replied in prose instead of calling a tool — it's done exploring. Force the
            # emit now so we always get a structured result; only if that's unsupported do we nudge
            # and retry (bounded, so an unlimited loop can't spin forever on a model that won't emit).
            messages.append({"role": "assistant", "content": msg.get("content") or ""})
            forced = _force_emit(client, messages, emit_spec)
            if forced is not None:
                return finalize(forced)
            stalls += 1
            if stalls >= 2:
                break
            messages.append({"role": "user", "content": f"Now call `{emit_name}` with your final answer."})
            continue
        stalls = 0
        messages.append({"role": "assistant", "content": msg.get("content") or "",
                         "tool_calls": calls})
        for tc in calls:
            fn = tc.get("function", {})
            name = fn.get("name", "")
            raw = fn.get("arguments") or "{}"
            # A small/junk model can emit malformed JSON arguments; never let that crash the
            # run — treat an unparseable tool call as empty args (emit then falls back to a
            # safe result; a retrieval call just gets {}).
            try:
                args = json.loads(raw) if isinstance(raw, str) else (raw or {})
            except (json.JSONDecodeError, TypeError):
                args = {}
            if name == emit_name:
                return finalize(args)
            result = tools.execute(name, args) if tools is not None else f"(unknown tool: {name})"
            messages.append({"role": "tool", "tool_call_id": tc.get("id", ""),
                             "name": name, "content": str(result)[:4000]})
    return fallback(messages)


class ToolUsingResearcher:
    _SYSTEM = ("You are an ML researcher. You MAY call the retrieval tools to consult "
               "prior knowledge, then call `emit` exactly once with your final Idea "
               "(operator, params, rationale, and a short reusable `theme` slug that groups "
               "this experiment with related ones, e.g. \"loss-fn\" or \"regularization\"). "
               "If you learn something reusable — a recurring failure to avoid, or a domain fact "
               "worth keeping — record it: check what's already stored first (kb_search/read_note), "
               "then `remember` (memory) or `kb_write`/`kb_append`/`kb_edit` (knowledge base) so you "
               "extend existing notes instead of duplicating them, when those tools are available. ")

    def __init__(self, client, tools, space_hint: str = "",
                 bounds: Optional[dict] = None, parser: str = "tool_call",
                 max_turns: int = 0, prompts: Optional[PromptStore] = None,
                 context_budget_chars: int = 0, time_budget_s: float = 0.0):
        self.client = client
        self.tools = tools          # object with .specs() and .execute(name, args)
        self.space_hint = space_hint
        self.bounds = bounds
        self.parser = parser
        self.max_turns = max_turns          # 0 = unlimited (config-driven via Settings.agent_max_turns)
        self.time_budget_s = time_budget_s  # 0 = no wall-clock cap (Settings.agent_time_budget_s)
        self.prompts = prompts
        self.context_budget_chars = context_budget_chars   # H4: cap the growing tool-call history

    def _emit_spec(self) -> dict:
        return {"type": "function", "function": {
            "name": "emit", "description": "Emit the final Idea for the next experiment.",
            "parameters": Idea.model_json_schema()}}

    @staticmethod
    def _sanitize(args: dict) -> dict:
        """Coerce the model's emit args into a valid Idea shape: params must be numeric, so DROP
        any non-numeric param the model invents (e.g. {"new_metric": "linear"} on a code-edit
        task whose space is free-form) rather than letting it crash the run."""
        out = dict(args) if isinstance(args, dict) else {}
        params = out.get("params")
        if isinstance(params, dict):
            clean: dict = {}
            for k, v in params.items():
                try:
                    clean[k] = float(v)
                except (TypeError, ValueError):
                    pass
            out["params"] = clean
        else:
            out["params"] = {}
        return out

    def _finalize(self, args: dict) -> Idea:
        # Never let a malformed emit (non-numeric params, bad shape) crash the loop — sanitize,
        # then fall back to a rationale-preserving draft if validation still fails.
        try:
            return _clamp_fill(Idea.model_validate(self._sanitize(args)), self.bounds)
        except Exception:  # noqa: BLE001 - resilience: the run must survive a junk proposal
            rationale = str((args or {}).get("rationale", "") or "")[:500]
            operator = str((args or {}).get("operator") or "draft")
            return _clamp_fill(Idea(operator=operator, params={}, rationale=rationale), self.bounds)

    def _fallback(self, messages: list) -> Idea:
        # Force a structured emit from the accumulated context; if even that fails, return a
        # safe bounds-filled default so the run never crashes.
        try:
            idea = parse_structured(
                self.client, messages + [{"role": "user", "content": "Emit the Idea now."}],
                Idea, self.parser)
        except ParseError:
            idea = Idea(operator="draft", params={}, rationale="fallback (agent parse failed)")
        return _clamp_fill(idea, self.bounds)

    def propose(self, state: RunState, parent: Optional[Node]) -> Idea:
        if hasattr(self.tools, "bind_state"):    # let run-aware tools see the current search
            self.tools.bind_state(state, parent)
        from .hints import render_hint_directives
        hint_block = render_hint_directives(state.pending_hints)
        cue = getattr(self, "_complexity_hint", "")   # A0d breadth-keyed complexity cue (empty=off)
        messages = [
            {"role": "system",
             "content": render(self.prompts, "tool_researcher_system", self._SYSTEM)
                        + self.space_hint},
            {"role": "user", "content": _state_brief(state, parent) + hint_block + cue +
                "\nDecide the next experiment. Consult knowledge if useful, then emit."},
        ]
        return drive_tool_loop(
            self.client, self.tools, messages, self._emit_spec(),
            max_turns=self.max_turns, context_budget_chars=self.context_budget_chars,
            time_budget_s=self.time_budget_s,
            finalize=self._finalize, fallback=self._fallback)
