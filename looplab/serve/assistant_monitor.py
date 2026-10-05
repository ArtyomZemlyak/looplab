"""Bounded schedule edits and a wake-up's handoff, without scheduler/store authority.

A monitor may change its own cadence/instruction or finish, but never renew its lifetime,
reset its spent wake-ups, widen its mode, or create another monitor. The scheduler applies
the validated handoff only after the ordinary assistant turn returns.
"""
from __future__ import annotations

from looplab.serve.assistant_watch import (
    WATCH_MAX_INTERVAL_S, WATCH_MIN_INTERVAL_S, WatchRefusal, _bounded_float,
    _MAX_INSTRUCTION_CHARS)


def monitor_changes(body: dict) -> dict:
    if not isinstance(body, dict) or not body or set(body) - {"every_s", "instruction"}:
        raise WatchRefusal("provide every_s and/or instruction; other watch fields cannot be edited")
    changes = {}
    if "every_s" in body:
        if body["every_s"] is None:
            raise WatchRefusal("every_s must be a number of seconds")
        changes["every_s"] = _bounded_float(
            body["every_s"], low=WATCH_MIN_INTERVAL_S, high=WATCH_MAX_INTERVAL_S,
            default=300, what="every_s")
    if "instruction" in body:
        instruction = body["instruction"]
        if not isinstance(instruction, str) or not instruction.strip():
            raise WatchRefusal("instruction must be a nonempty standalone instruction")
        if len(instruction.strip()) > _MAX_INSTRUCTION_CHARS:
            raise WatchRefusal(f"the instruction is too long (max {_MAX_INSTRUCTION_CHARS} chars)")
        changes["instruction"] = instruction.strip()
    return changes


def monitor_control(body: dict) -> dict:
    if not isinstance(body, dict) or set(body) - {"stop", "reason", "every_s", "instruction"}:
        raise WatchRefusal("invalid monitor handoff fields")
    if not isinstance(body.get("stop"), bool):
        raise WatchRefusal("stop must be true or false")
    reason = body.get("reason")
    if not isinstance(reason, str) or not reason.strip() or len(reason.strip()) > 700:
        raise WatchRefusal("reason must explain the monitor decision in 1–700 characters")
    edits = {k: body[k] for k in ("every_s", "instruction") if k in body}
    if body["stop"] and edits:
        raise WatchRefusal("a stopped monitor cannot also change its schedule")
    return {"stop": body["stop"], "reason": reason.strip(),
            **(monitor_changes(edits) if edits else {})}


class MonitorControlTools:
    """One optional handoff for this scheduled wake-up only; no other watch is reachable."""

    def __init__(self):
        self.monitor_controls = []

    def bind_state(self, state=None, parent=None):
        return None

    def specs(self):
        from looplab.tools._base import fn_spec
        return [fn_spec(
            "configure_monitor",
            "Finish this monitor when its goal is done, or adjust its future interval/instruction. "
            "Only affects this monitor after this turn; never renews its lifetime or wake-up budget. "
            "Explain your decision in the chat. Omit this tool to keep the current schedule.",
            {"stop": {"type": "boolean"}, "reason": {"type": "string"},
             "every_s": {"type": "number", "minimum": WATCH_MIN_INTERVAL_S,
                         "maximum": WATCH_MAX_INTERVAL_S},
             "instruction": {"type": "string"}}, ["stop", "reason"])]

    def execute(self, name, args):
        if name != "configure_monitor":
            return f"(unknown tool: {name})"
        if self.monitor_controls:
            return "(monitor handoff already recorded; finish this turn)"
        try:
            control = monitor_control(args)
        except WatchRefusal as exc:
            return f"(watch refused: {exc})"
        self.monitor_controls.append(control)
        return "(monitor handoff recorded; it takes effect after this turn)"
