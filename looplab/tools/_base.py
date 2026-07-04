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
    - `bind_state(state)` (optional) — run-aware providers (e.g. `RunTools`) implement this
      so the agent loop can point them at the current `RunState` each turn. Providers that
      don't need run state simply omit it (`CompositeTools` forwards it only where present),
      hence the no-op default here.
    """

    def specs(self) -> list[dict]: ...

    def execute(self, name: str, args: dict) -> str: ...

    def bind_state(self, state) -> None:  # optional hook — default is a no-op
        return None
