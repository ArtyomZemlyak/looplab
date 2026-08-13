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


def fit_rows(header, rows, *, receipt: str = "", cap: int = RESULT_CAP,
             omitted: str = "... ({receipt}{n} more omitted to fit the result cap)") -> str:
    """Assemble `header` + `rows` (+ a trailing `receipt`) so the whole result fits under `cap`.

    Drop whole ROWS from the end, and say in the receipt how many the cap itself removed. The agent
    loop cuts an over-cap tool result from the HEAD, which silently eats whatever is at the END — and
    for a listing the end is exactly the receipt that says the result is partial ("… (+K more)",
    "capped at N hits"). A long listing therefore arrived looking complete.

    `reposcout._fit_rows` and `memory_tools._bounded_result` were this function written twice with
    different marker wording and different header types (doc 25 TO-08). `header` accepts either a
    string (used verbatim — the caller owns its trailing newline) or a sequence of lines (joined, and
    the omission marker is appended as one more line). `omitted` keeps the per-site wording as a
    parameter: `{n}` is the dropped-row count and `{receipt}` the caller's own receipt with a
    separator, empty when there is none.
    """
    lines = header if isinstance(header, str) else "\n".join(header)
    joiner = "" if isinstance(header, str) else "\n"
    tail = f"\n{receipt}" if receipt else ""
    body = "\n".join(rows)
    if len(lines) + len(joiner if rows else "") + len(body) + len(tail) <= cap:
        return lines + (joiner if rows else "") + body + tail
    # Reserve room for the AMENDED marker before deciding how many rows survive: a marker added after
    # the fit decision is exactly what pushes the receipt back past the cap.
    dropped, kept = 0, list(rows)
    while kept:
        marker = "\n" + omitted.format(n=dropped, receipt=f"{receipt}; " if receipt else "")
        body = "\n".join(kept)
        if len(lines) + len(joiner) + len(body) + len(marker) <= cap:
            return lines + joiner + body + marker
        kept.pop()
        dropped += 1
    return lines + (f"\n({receipt})" if receipt else "\n(nothing fits the result cap)")


def clip(text: str, cap: int, *, keep: str = "head", note: str = "", reserve: int = 0,
         line_boundary: bool = False) -> str:
    """Bound one STRING under `cap`, saying so when it actually cuts (doc 25 TO-08).

    Five providers wrote this separately, each with its own marker, so a model had to learn five
    receipts for one event. The differences that survive are parameters because they are real:

    * `keep` — `"tail"` for a log or command output (the end is where the error and the final metric
      line are; the marker then goes in FRONT), `"head"` for a reply or a listing.
    * `line_boundary` — cut back to the last newline so no half-line/half-hit shows.
    * `reserve` — whether the marker is charged AGAINST `cap`. Most callers pass a cap that already
      carries headroom and let the marker sit on top; a caller handed the loop's raw `RESULT_CAP` has
      no headroom, and a result landing EXACTLY on the cap is one the loop's own marker also skips —
      a cut answer byte-indistinguishable from a complete one.
    * `note` — the marker itself, formatted with `{n}` = characters dropped. Empty means no marker,
      which is only honest when the caller adds its own.
    """
    if len(text) <= cap:
        return text
    budget = max(0, cap - reserve)
    if keep == "tail":
        cut = text[len(text) - budget:]
        if line_boundary and "\n" in cut:
            cut = cut[cut.index("\n") + 1:]
        return note.format(n=len(text) - len(cut)) + cut
    cut = text[:budget]
    if line_boundary and "\n" in cut:
        cut = cut[:cut.rfind("\n")]
    return cut + note.format(n=len(text) - len(cut))


# Per-STREAM tail budgets for a two-stream (stdout/stderr) command result. The agent loop caps the
# COMBINED result at RESULT_CAP (head-keep), so giving each stream ~RESULT_CAP alone let a verbose
# stdout push the whole stderr section — the traceback, i.e. the REASON the command failed — past the
# cap, where the loop silently dropped it. The MINIMUM below holds even when both streams are long;
# when one stream is short, its unused budget flows to the other (a stderr-only failure gets ~the
# whole cap for its traceback, not half — a fixed 50/50 split truncated exactly the frames the repair
# needed). Headroom (-400) covers the exit-code head + section labels + notes.
STDOUT_TAIL = RESULT_CAP // 2 - 200
# stderr's own guaranteed minimum is DERIVED in `stream_tails` as `avail - STDOUT_TAIL`,
# deliberately not a second constant that could drift away from it.


def stream_tails(out: str, err: str) -> tuple[int, int]:
    """Per-call tail budgets: each stream is guaranteed its minimum share, and whatever one stream
    leaves unused flows to the other (stderr first — the exception lives there). Sum always fits
    under RESULT_CAP with the -400 label/head headroom.

    Lives HERE, beside `clip`/`fit_rows`, rather than in `shell_tools`: the assistant's `run_command`
    and the Developer's `run_probe` are two surfaces reporting the SAME two-stream shape, and a
    second copy is how the two would come to disagree about which half of a failure survives
    (doc 25 TO-08 — five providers had written `clip` separately before it moved here)."""
    avail = RESULT_CAP - 400
    err_take = min(len(err), avail - min(len(out), STDOUT_TAIL))
    out_take = min(len(out), avail - err_take)
    return out_take, err_take


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
