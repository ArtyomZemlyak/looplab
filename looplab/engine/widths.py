"""The concurrency-width settling rule, once (doc 25 ES-09 + EC-11).

Four loops — two in `_apply_control_overrides` (operator `budget_extend` controls) and two in
`_apply_strategy` (Strategist decisions) — had written out the same validator: reject a bool, reject
a non-integral or non-finite float, coerce to int, bound it, then settle 0 to serial width 1. Only
the upper bound and the target attribute differed.

Each of the four rules is load-bearing, and each fails in a different direction if a copy drifts:

* **bool is not a width.** `True` is an `int` subclass, so a JSON `true` from a UI/API type slip
  would set the width to ONE. The run then looks configured and simply serializes, with nothing in
  the log to say why.
* **a non-integral float is not a width.** `2.5` truncating to 2 is a guess about what the caller
  meant; refusing keeps the previous width, which is the value the operator last actually chose.
* **the bound is a REFUSAL, not a clamp.** An out-of-range 100_000 clamped to the ceiling would look
  accepted and quietly reshape the run; skipping leaves the current width and the next control can
  correct it.
* **0 settles to 1, it does not mean AUTO.** AUTO belongs to launch-time `Settings`, where it can
  read the hardware and the settled eval width. A LIVE 0 arriving mid-run has no such context, so it
  means "serial" — treating it as AUTO would re-derive a width the operator never asked for.
"""
from __future__ import annotations

import math
from typing import Optional


def settle_width(raw, upper: int) -> Optional[int]:
    """Validate a live concurrency width, or return None to leave the current one alone.

    Returns the settled width in ``1..upper``. ``None`` means "this value is not a width" — every
    caller's loop treats that as `continue`, so a poison control never changes the running envelope.
    """
    if isinstance(raw, bool):
        return None
    if isinstance(raw, float) and (not math.isfinite(raw) or not raw.is_integer()):
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    if not 0 <= value <= upper:
        return None
    return max(1, value)


# The two bounds, named so a call site reads as the axis it settles rather than as a magic number.
EVAL_WIDTH_MAX = 1024        # concurrent EVALS: bounded by the box, not by provider concurrency
LLM_WIDTH_MAX = 64           # concurrent BUILDS / provider calls: bounded by the shared LLM broker
