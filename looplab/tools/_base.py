"""Shared plumbing for the tool subsystem (ADR-7 tool protocol).

Every toolset in `looplab/tools/` is a **tool provider**: a plain object the agent loop
(`looplab.agents.agent.drive_tool_loop` / `CompositeTools`) can interrogate for OpenAI-format
function schemas and dispatch tool calls to. There is no registry and no base class — the
contract is duck-typed (see `ToolProvider` below), so a provider is trivially unit-testable
and composable: `CompositeTools([...])` merges any number of providers into one.

This module holds the two pieces every provider shares:

- `fn_spec(...)` — the one place the OpenAI function/tool schema shape lives, so every
  provider's `specs()` builds identical JSON.
- `ToolProvider` — the Protocol documenting the provider contract itself.
"""
from __future__ import annotations

from typing import Optional, Protocol

# The agent loop's hard per-result bound: `drive_tool_loop` (agents/agent.py) caps EVERY tool result
# at this many chars before it reaches the model, replacing the tail with an explicit truncation
# marker. Providers must derive their own page/tail budgets FROM this constant (cap minus their
# header/marker overhead) instead of hard-coding free-standing ~4000s — so the loop cap and every
# provider budget move together, and a provider's own honest truncation (not the loop's blunt cut)
# is what decides which content is dropped. Canonical home: core/context_budget.py (runtime/ sits
# BELOW tools/ in the layering and needs it too); re-exported here for the providers.
from looplab.core.context_budget import RESULT_CAP  # noqa: F401  (re-export, see comment above)


def fn_spec(name: str, description: str, props: dict, required: Optional[list] = None) -> dict:
    """Build one OpenAI-format function/tool schema. Shared by every tool provider so the
    schema shape lives in one place."""
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": props, "required": required or []}}}


class ToolProvider(Protocol):
    """The duck-typed tool-provider contract (structural — no provider inherits this).

    A provider exposes:

    - `specs() -> list[dict]` — the OpenAI function/tool schemas it offers (built with
      `fn_spec`). May be empty (e.g. a provider whose backing directory is unconfigured);
      an empty provider simply contributes no tools.
    - `execute(name, args) -> str` — run one tool call and return the result as a STRING.
      Soft-fail rule: `execute` returns an error message string, it never raises — a junk
      tool call from the model must not crash the run. Long output is additionally
      truncated by the agent layer (~4000 chars), so providers should tail/clip smartly.
    - `bind_state(state, parent=None)` (optional) — run-aware providers (e.g. `RunTools`)
      implement this so the agent loop can point them at the current `RunState` (and the
      node's parent, when the loop knows one) each turn. The loop CALLS it with BOTH
      arguments — `bind_state(state, parent)` (`agents/agent.py`) — so a provider must
      accept the second one (default it to None), or it raises TypeError at dispatch.
      Providers that don't need run state simply omit the hook (`CompositeTools` forwards
      it only where present), hence the no-op default here.
    """

    def specs(self) -> list[dict]: ...

    def execute(self, name: str, args: dict) -> str: ...

    def bind_state(self, state, parent=None) -> None:  # optional hook — default is a no-op
        return None
