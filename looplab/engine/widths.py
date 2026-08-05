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


# Where a re-entering surface's width ACTUALLY comes from. The refusal below names a file to edit,
# and naming the wrong one is worse than naming none: `looplab run` never reads
# `config.snapshot.json` (it WRITES it, from the launch settings), so telling an operator to put the
# width back there sends them to edit a file that has no effect on the command they just ran.
# `resume`/`finalize` are the mirror image — they restore the run's settings FROM that snapshot
# (`cli/__init__.py::load_run_settings`), so there the snapshot is exactly the right answer.
SETTLED_WIDTH_SOURCES = {
    "run": ("this command's launch settings (`-s {axis}=...`, the config file's `settings:` block, "
            "or LOOPLAB_{ENV}) — note `looplab run` WRITES config.snapshot.json from those settings "
            "and never reads it back"),
    "resume": "this run's config.snapshot.json, which `resume` restores the run's settings from",
    None: "this run's config.snapshot.json / launch settings",
}


def settled_width_refusal(axis: str, *, resolved, recorded: int, source: str | None = None) -> str:
    """The ONE wording of a refused width re-entry, so every surface says something true.

    ``source`` selects the remedy from ``SETTLED_WIDTH_SOURCES``; ``None`` is the generic phrasing a
    library ``Engine(...)`` caller gets, where the engine genuinely cannot know which knob was turned.

    The `null` note is not padding.  ``eval_parallel: null`` is the natural JSON spelling of "no
    opinion" and is the one an operator reaches for after reading "0 = AUTO adopts the pin", but it
    means something else entirely: ``None`` is the durable LEGACY mode (fall back to
    ``max_parallel``/``parallel_build``, both defaulting to 1), so it produces an explicitly spelled 1
    and refuses again.  That semantic cannot change — every snapshot written before the 2026-08-04
    default flip spells ``null``, and promoting it to AUTO would silently re-derive a box-shaped width
    for exactly the runs this pin exists to protect — so the refusal has to say so instead.
    """
    remedy = SETTLED_WIDTH_SOURCES.get(source, SETTLED_WIDTH_SOURCES[None])
    remedy = remedy.format(axis=axis, ENV=axis.upper())
    return (
        # "re-enter", not "resume": `looplab run <existing dir>` reaches this too (it REOPENS the
        # log), and telling that operator their `resume` was refused sends them looking for a command
        # they did not type.
        f"cannot re-enter this run at {axis}={resolved}: run_started pinned {recorded}. "
        "The run-start record owns the width its log was written under (engine invariant #6) — a "
        "change here would splice two execution treatments into one log with nothing recording the "
        f"change. Put {axis} back to {recorded} (or to 0 = AUTO, which adopts the pin; `null` is NOT "
        f"AUTO — it is the legacy fallback and resolves to 1) in {remedy}. Or change the width "
        "durably with a `budget_extend` control event, which the log DOES record."
    )
