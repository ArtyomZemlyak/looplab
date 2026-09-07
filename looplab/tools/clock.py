"""The agent's own clock: `remaining_time`, and the loop-side deadline note it pairs with.

A Developer or repair session is tree-killed at its wall budget, a Researcher's propose at
`agent_time_budget_s`, a triage judge at `triage_time_budget_s` — and until doc 52 row 15 every one
of them was told its budget ONCE, in prose, at the start (`repo_developer._time_budget_note`,
`proposal_cues._cue_experiment_time_budget`), and then never again. No tool answered "how long have I
been at this and how much is left", and the loop injected no warning as the wall approached; the
first sign of the deadline was the kill. EurekAgent's roles read their clock (a time helper plus a
deadline warning) and that is what this is.

TWO HALVES, ONE CLOCK. `drive_tool_loop` owns the clock — it is the thing that started the wall
and will end it — and publishes it through a ContextVar right before EVERY tool execution
(`set_current_clock`), so a nested loop (a Developer session's `run_phase` inside a build) reads
its own clock while it runs and the outer loop re-publishes its own before its next call; no scope
and no `finally` are needed, because the value is only ever read from inside a tool execution the
loop itself started. `ClockTools.remaining_time` is the PULL half: elapsed, remaining, the turn
count, in one line. The PUSH half is the loop's deadline note (`tool_loop._deadline_note`),
appended once to a tool result when the remaining wall drops under a fifth of the budget or two
minutes, whichever is larger — like the identical-result repeat note, it rides the result the
model is already reading rather than a new turn it has to be charged for.

What this is NOT: a budget the tool can extend. It reads; the loop decides, and the kill is still
the kill. It reaches no metric, champion, selectability or violation (docs/36).
"""
from __future__ import annotations

import contextvars
import time
from dataclasses import dataclass
from typing import Optional

from looplab.tools._base import fn_spec

REMAINING_TIME_TOOL = "remaining_time"


@dataclass
class LoopClock:
    """One tool loop's wall clock: when it started, what it may spend, where it is."""
    started: float                # `time.monotonic()` at loop start
    time_budget_s: float = 0.0    # 0 = no wall-clock ceiling
    max_turns: int = 0            # 0 = no turn ceiling
    turn: int = 0                 # the turn currently executing (0-based)

    def elapsed(self, now: Optional[float] = None) -> float:
        return max(0.0, (time.monotonic() if now is None else now) - self.started)

    def remaining(self, now: Optional[float] = None) -> Optional[float]:
        if not self.time_budget_s or self.time_budget_s <= 0:
            return None
        return max(0.0, self.time_budget_s - self.elapsed(now))

    def describe(self, now: Optional[float] = None) -> str:
        elapsed = self.elapsed(now)
        remaining = self.remaining(now)
        turns = (f"turn {self.turn + 1} of {self.max_turns}" if self.max_turns > 0
                 else f"turn {self.turn + 1} (no turn ceiling)")
        if remaining is None:
            return (f"elapsed {elapsed:.0f}s; this session has no wall-clock ceiling; {turns}. "
                    "Other bounds still end it (the stuck detector, the emit ceiling).")
        return (f"elapsed {elapsed:.0f}s of a {self.time_budget_s:.0f}s wall-clock budget — "
                f"{remaining:.0f}s remain; {turns}. Past the budget no new turn starts and the "
                "answer is salvaged from what you have, so finish and emit before then.")


_CLOCK: contextvars.ContextVar[Optional[LoopClock]] = contextvars.ContextVar(
    "looplab_loop_clock", default=None)


def set_current_clock(clock: Optional[LoopClock]) -> None:
    _CLOCK.set(clock)


def current_clock() -> Optional[LoopClock]:
    return _CLOCK.get()


class ClockTools:
    """One tool, `remaining_time`, answering from the enclosing loop's own clock."""

    def specs(self) -> list[dict]:
        return [fn_spec(
            REMAINING_TIME_TOOL,
            "How long this session has been running and how much of its wall-clock budget "
            "remains (plus the turn count). Free and instant; call it before starting anything long.",
            {}, [])]

    def execute(self, name: str, args: dict) -> str:
        if name != REMAINING_TIME_TOOL:
            return f"(unknown tool: {name})"
        clock = current_clock()
        if clock is None:
            return "(no loop clock is published here — this call was made outside a tool loop)"
        return clock.describe()
