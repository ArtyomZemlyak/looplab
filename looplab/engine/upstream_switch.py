"""The upstream automation's KILL SWITCH and its operator view OUTSIDE `/commands` (doc 73 §4.3).

WHY (2026-10-08). The switch (`upstream_auto_set {enabled, reason}`, doc 73 §4.2 G2) could only be
pulled through `/commands` — the UI, the API, MCP — and `looplab inspect` printed neither the switch
nor the steps a cap held back. An operator at a terminal, with no server running, had no hand on the
automation at all.

ONE RULE, TWO DOORS. `normalize_upstream_auto_set` is the switch's whole payload rule: the server's
control normalizer (`serve/control_validation.py::_normalize_upstream_auto_set`) calls it for
`/commands`, and `looplab upstream-auto` (`set_upstream_auto`) calls it before its append — the same
control event with the same payload, never a second spelling of what a valid switch is. The CLI
appends the intent itself, as `looplab stop` appends its pause: a control intent is the one thing a
process other than the engine may append beside a running engine (invariant #1), and the switch's
command policy is `NO_SPAWN` / `folded_intent` — the command service starts nothing for it either, it
only records the command around the same append.

THE VIEW. `upstream_operator_lines` is what `looplab inspect` prints from `upstream_live_view` — the
projection the UI panel reads: the switch, the steps held back at a cap (and whether each still
waits), the author's spend against its cap, and the automatic advances in the last hour against theirs.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

REASON_LIMIT = 300


class UpstreamSwitchRefusal(ValueError):
    """The switch's payload is not one the rule admits. A `ValueError`: the server answers it 400, the
    CLI wraps it in its own `ConfigRefusal`."""


def normalize_upstream_auto_set(data: dict) -> dict:
    """THE ONE RULE for an `upstream_auto_set` payload: `enabled` a real bool, `reason` optional text
    (stripped, at most `REASON_LIMIT` characters, dropped when blank)."""
    enabled = data.get("enabled")
    if type(enabled) is not bool:
        raise UpstreamSwitchRefusal("enabled must be true or false")
    reason = data.get("reason")
    if reason is not None:
        if not isinstance(reason, str):
            raise UpstreamSwitchRefusal("reason must be a string")
        reason = reason.strip()
        if len(reason) > REASON_LIMIT:
            raise UpstreamSwitchRefusal(f"reason must be at most {REASON_LIMIT} characters")
    return {"enabled": enabled, **({"reason": reason} if reason else {})}


def set_upstream_auto(run_dir, enabled: bool, reason: Optional[str] = None) -> str:
    """`looplab upstream-auto`: append the switch for `run_dir` and say what it does now."""
    from looplab.core.errors import ConfigRefusal
    from looplab.events.eventstore import EventStore
    from looplab.events.replay import fold
    from looplab.events.run_generation import run_generation_token
    from looplab.events.types import EV_UPSTREAM_AUTO_SET
    run_dir = Path(run_dir)
    path = run_dir / "events.jsonl"
    if not path.is_file():
        raise ConfigRefusal(f"upstream-auto: no run at {run_dir} (no events.jsonl)")
    store = EventStore(path)
    events = store.read_all()
    if not run_generation_token(events):
        raise ConfigRefusal(f"upstream-auto: {run_dir} has no durable run identity yet (no run_started)")
    raw = {"enabled": enabled, **({"reason": reason} if reason is not None else {})}
    try:
        data = normalize_upstream_auto_set(raw)
    except UpstreamSwitchRefusal as exc:
        raise ConfigRefusal(f"upstream-auto: {exc}") from None
    was_paused = bool(getattr(fold(events), "upstream_auto_paused", False))
    row = store.append(EV_UPSTREAM_AUTO_SET, {
        "enabled": data["enabled"], **({"reason": data["reason"]} if "reason" in data else {})})
    if enabled:
        said = ("ON — the live engine authors, checks and advances on its own again"
                + ("" if was_paused else " (it was not stopped)"))
    else:
        said = ("OFF — the live engine drafts, checks and advances nothing on its own; operations an "
                "operator queues are still served" + ("" if not was_paused else " (it was already stopped)"))
    return f"{run_dir}: upstream automation {said} (seq {row.seq})."


def upstream_operator_lines(run_dir, events, *, now: Optional[float] = None) -> list[str]:
    """The operator's lines for `looplab inspect`; [] for a run with no upstream lane."""
    from looplab.engine.upstream_serve import upstream_live_view
    view = upstream_live_view(run_dir, events, now=now)
    if view is None:
        return []
    out = [f"upstream automation: mode {view['mode']}"
           + (f" ({view['reason']})" if view["reason"] else "")
           + (" [as configured; no engine has armed it yet]" if view["configured"] else "")]
    switch = view.get("switch")
    if view["auto_paused"]:
        out.append("  switch: OFF — no automatic author, check or advance"
                   + (f" (seq {switch['seq']}: {switch['reason']})" if switch and switch.get("reason")
                      else f" (seq {switch['seq']})" if switch else ""))
    else:
        out.append("  switch: on" + (f" (seq {switch['seq']})" if switch else " (never set)"))
    cap = view.get("author_usd_cap") or 0.0
    out.append(f"  author spend: ${view['author_spent_usd']:.4f}"
               + (f" of ${cap:g} cap" if cap else " (no money cap)")
               + f"; drafts recorded: {view['authored_total']}")
    per_hour = view.get("advances_per_hour") or 0
    out.append(f"  automatic advances in the last hour: {view.get('advances_last_hour', 0)}"
               + (f" of {per_hour}/h cap" if per_hour else " (no hourly cap)"))
    held = view["held"]
    if not held:
        out.append("  held steps: none")
    for row in held:
        state = "waiting" if row.get("waiting") else "released"
        out.append(f"  held: {row['op']} {row.get('proposal_id') or ''} {row['reason']} "
                   f"(seq {row['seq']}, {state})".replace("  (", " ("))
    return out


def upstream_inspect_lines(run_dir, state, events) -> list[str]:
    """`looplab inspect`'s upstream block: the board (`core/upstream_board.py::board_lines`), then the
    automation's own lines above; [] for a run that never used the lane."""
    from looplab.core.upstream_board import board_lines
    return [*board_lines(state), *upstream_operator_lines(run_dir, events)]
