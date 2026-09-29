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
  - Scope: OpenHands' core — 1-cycles (identical pair) and 2-cycles (A B A B) — plus, since
    2026-09-29, a cycle of ANY longer length, on its own rule (`_stale_cycle`, doc 69 §3.2): a
    streak of calls in a row, every one of which re-ran a call+result ALREADY SEEN in this loop.
    `minionerec-backbones-v10`'s card-4 plan phase read seven `config/rl_*.yaml` files round and
    round for its last 104 calls (each 14-16 times, the context 27k -> 217k); the two short
    rules saw no 1- or 2-cycle, `_REPEAT_NOTE` went out 91 times and `_READ_LOOP_NOTE` 65 times
    without effect, and only `agent_emit_after` at 300 turns stopped it. Replayed over that run's
    spans, "12 results in a row byte-identical to ones already seen this phase" fired in exactly
    two phases, saved 32.4 M tokens (16 % of the run) and fired on nothing healthy.

    The rule here is that one made STRICTER in two ways, so it fires no earlier than the replayed
    one did and never in a phase where that one did not: keyed on the PAIR (call + result, like
    the two short rules — a tool that answers different arguments alike is not repeating), and
    firing only once the streak holds at least THREE distinct pairs and has re-run each of them
    TWICE on average (`run >= max(threshold, 2 * distinct)`). The distinct floor leaves 1- and
    2-cycles to their own thresholds, so an operator who raised `stuck_repeat` keeps that number;
    the average leaves alone a model re-reading, ONCE each, a dozen files its compacted history
    lost — the second full pass is the cycle. It is not the per-pair serve count refuted beside
    `Settings.triage_time_budget_s` ("same call+result served m times" fired on 259 of the 586
    sessions of 40+ calls): a single NEW pair ends the streak, so re-reads scattered through real
    work never accumulate. `update_plan` (the caller's `neutral_tools`) neither extends nor breaks
    a streak: a model rewriting its checklist between laps of the same seven files is still
    lapping.
"""
from __future__ import annotations

import hashlib
import json
from collections import deque
from typing import Iterable, Optional


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
                 alternate_threshold: int = 4, window: int = 20, stale_threshold: int = 12,
                 neutral_tools: Iterable[str] = ()):
        self.enabled = enabled
        # An identical action+observation pair repeated this many times in a row => stuck
        # (this is the "same call, same error/result, no progress" case).
        self.repeat_threshold = max(2, int(repeat_threshold))
        # Two distinct actions ping-ponging for this many cycles (A B A B ...) => stuck.
        self.alternate_threshold = max(2, int(alternate_threshold))
        # Calls in a row that each re-ran an already-seen call+result, over >= 3 distinct pairs
        # (see `_stale_cycle`) => stuck. 0 = off, and then nothing is remembered at all.
        self.stale_threshold = max(0, int(stale_threshold))
        self._neutral = frozenset(neutral_tools)
        # Keep enough history to see the longest pattern we look for.
        size = max(int(window), 2 * self.alternate_threshold, self.repeat_threshold)
        self._actions: deque[str] = deque(maxlen=size)
        self._pairs: deque[str] = deque(maxlen=size)
        # The long-cycle ledger: a DIGEST of every pair this loop has seen (a loop's pairs carry
        # capped tool results, so digests keep it at 16 bytes a call), and the current stale
        # streak as {digest: times re-run inside it} — its length is the sum, its breadth the
        # size.
        self._seen: set[bytes] = set()
        self._streak: dict[bytes, int] = {}
        self._streak_len = 0

    def push(self, tool_name: str, args, observation=None) -> Optional[str]:
        """Record one executed tool call; return a reason string if the loop now looks
        stuck, else None. Callers should stop (force the final emit) on a non-None reason."""
        if not self.enabled:
            return None
        action = f"{tool_name}({_canonical(args)})"
        obs_sig = _canonical(observation) if observation is not None else ""
        pair = action + " => " + obs_sig
        self._actions.append(action)
        self._pairs.append(pair)
        # Charged on EVERY push, before the short rules answer: an `or` chain would skip the
        # ledger on the push where one of them fires. They still win the tie — their reason
        # names the exact call, which is the more useful sentence to hand the model.
        long_cycle = self._stale_cycle(tool_name, pair)
        return (self._repeated_pair() or self._alternating_actions()
                or long_cycle)

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

    def _stale_cycle(self, tool_name: str, pair: str) -> Optional[str]:
        # A pair never seen before in this loop is progress and ENDS the streak; a seen one extends
        # it. See the module docstring for why the two floors (three distinct pairs, two re-runs
        # each on average) sit on top of the replayed "N in a row" rule.
        k = self.stale_threshold
        if k <= 0 or tool_name in self._neutral:
            return None
        digest = hashlib.blake2b(pair.encode("utf-8", "surrogatepass"), digest_size=16).digest()
        if digest not in self._seen:
            self._seen.add(digest)
            self._streak.clear()
            self._streak_len = 0
            return None
        self._streak[digest] = self._streak.get(digest, 0) + 1
        self._streak_len += 1
        distinct = len(self._streak)
        if distinct < 3 or self._streak_len < max(k, 2 * distinct):
            return None
        return (f"re-ran {self._streak_len} calls in a row whose call AND result were each already "
                f"seen this phase — a cycle over {distinct} distinct calls (last: "
                f"{self._actions[-1][:160]}) with no progress")
