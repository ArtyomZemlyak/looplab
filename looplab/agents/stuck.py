"""B1 · No-progress / stuck detection for the shared agent tool-loop.

Mirrors OpenHands' StuckDetector: an agent that calls the SAME tool with the SAME
arguments over and over — or ping-pongs between two calls, or keeps hitting the SAME
error — is making no progress. Abort that loop gracefully instead of spinning forever.

This is the safety net that makes "unlimited turns" safe. The turn ceiling
(`agent_max_turns`) and the wall-clock budget (`agent_time_budget_s`) are only
*backstops* — they fire after the waste has happened. No-progress detection is what
actually stops a stuck loop, on the cheapest possible signal (a repeated call).

Design notes:
  - PURE + deterministic. Feed it ``push(tool_name, args, observation)`` per executed
    tool call; it returns a human-readable reason string the first time the recent
    window shows a pathological repeat, else ``None``.
  - Compares the *content* of a call (tool name + canonical args) and of an observation,
    ignoring ids/timestamps — so it flags truly repetitive behaviour, not superficial
    differences.
  - Reading DIFFERENT files, or running ONE long command, never trips it: only an
    identical action+observation repeated `repeat_threshold` times (or a strict two-call
    ping-pong) does. That deliberately avoids OpenHands' early bug of killing an agent
    that was simply waiting on a single long-running process, and avoids flagging a tool
    that legitimately returns the same observation for DIFFERENT arguments.
  - Scope (matching OpenHands' core): catches 1-cycles (identical pair) and 2-cycles
    (A B A B). Exotic longer cycles fall to the turn/time-budget backstops by design.
"""
from __future__ import annotations

import json
from collections import deque
from typing import Optional


def _canonical(obj) -> str:
    """Stable string for a tool's args / an observation, robust to unserializable values."""
    try:
        return json.dumps(obj, sort_keys=True, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(obj)


class StuckDetector:
    """Sliding-window detector over (action_signature, observation_signature) pairs.

    Thresholds default to OpenHands-like values. ``enabled=False`` makes ``push`` a
    no-op that always returns ``None`` (the loop then relies on the turn/time ceilings).
    """

    def __init__(self, *, enabled: bool = True, repeat_threshold: int = 4,
                 alternate_threshold: int = 4, window: int = 20):
        self.enabled = enabled
        # An identical action+observation pair repeated this many times in a row => stuck
        # (this is the "same call, same error/result, no progress" case).
        self.repeat_threshold = max(2, int(repeat_threshold))
        # Two distinct actions ping-ponging for this many cycles (A B A B ...) => stuck.
        self.alternate_threshold = max(2, int(alternate_threshold))
        # Keep enough history to see the longest pattern we look for.
        size = max(int(window), 2 * self.alternate_threshold, self.repeat_threshold)
        self._actions: deque[str] = deque(maxlen=size)
        self._pairs: deque[str] = deque(maxlen=size)

    def push(self, tool_name: str, args, observation=None) -> Optional[str]:
        """Record one executed tool call; return a reason string if the loop now looks
        stuck, else None. Callers should stop (force the final emit) on a non-None reason."""
        if not self.enabled:
            return None
        action = f"{tool_name}({_canonical(args)})"
        obs_sig = _canonical(observation) if observation is not None else ""
        self._actions.append(action)
        self._pairs.append(action + " => " + obs_sig)
        return self._repeated_pair() or self._alternating_actions()

    # --- patterns -----------------------------------------------------------------
    def _repeated_pair(self) -> Optional[str]:
        # k identical action+observation pairs in a row: the same call returning the same
        # thing over and over. Keyed on the PAIR so a tool that returns the same observation
        # for DIFFERENT args isn't falsely flagged (the action part differs).
        k = self.repeat_threshold
        if len(self._pairs) < k:
            return None
        last = list(self._pairs)[-k:]
        if len(set(last)) == 1:
            return f"repeated the same call+result {self._actions[-1][:160]} {k} times with no progress"
        return None

    def _alternating_actions(self) -> Optional[str]:
        # Keyed on the action+observation PAIR (not the bare action): a legitimate fixed two-step
        # loop (e.g. poll A / wait B with constant args) whose OBSERVATIONS evolve is making progress
        # and must NOT be flagged — only a true ping-pong where both calls AND their results repeat is.
        k = self.alternate_threshold
        need = 2 * k
        if len(self._pairs) < need:
            return None
        last = list(self._pairs)[-need:]
        evens, odds = set(last[0::2]), set(last[1::2])
        if len(evens) == 1 and len(odds) == 1 and evens != odds:
            return (f"alternating between two calls ({last[0][:80]} / {last[1][:80]}) "
                    f"for {k} cycles with no progress")
        return None
