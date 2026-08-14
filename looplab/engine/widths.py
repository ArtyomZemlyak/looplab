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


def per_experiment_gpu_budget(pool, eval_parallel) -> Optional[int]:
    """How many GPUs ONE experiment may claim while ``eval_parallel`` of them still run at once.

    Stated here, beside the width settler, because it is a fact ABOUT the settled width — and
    because the alternative was for `agents/roles.py::_FOOTPRINT_GUIDANCE` to ask the Researcher for
    a `footprint.gpus` sized against a budget the prompt never named. Measured on
    `rubertlite-dr-unified-v5` (docs/29 F1b): the goal prose said "two H200 GPUs are available", both
    Cards declared `{"gpus": 2}`, `_resource_request_for_node` honoured the declaration (declared
    beats AUTO), and a run with `eval_parallel: 2` went serial at double the per-node cost.

    This is the SAME share `engine/resources.py::_resource_request_for_node` already gives an
    UNDECLARED footprint, generalized: that branch reads "pool and parallel > 1 -> one device, else
    the whole box", which is exactly ``pool // eval_parallel`` at the AUTO widths those two branches
    were written for. A declared footprint is still authoritative — this is what the Researcher is
    TOLD, not a clamp — so the two can never disagree about a value one of them refuses.

    ``None`` means "not knowable, say nothing": a caller with no settled width or no probed pool must
    print no number at all rather than a plausible wrong one. The three edge cases, each decided
    rather than inherited:

    * **pool == 0** -> ``0``, NOT ``max(1, 0 // p)``. On a GPU-less host a positive `gpus` is
      `required_unavailable` in `_resource_request_for_node` and fails admission closed; telling the
      Researcher it may have one device would produce exactly the declaration that cannot be served.
    * **eval_parallel > pool** (an explicitly spelled width the box cannot serve) -> ``1``, not the
      floor's ``0``. There ARE devices; they are oversubscribed, and the scheduler QUEUES rather than
      refusing. ``0`` would read as "CPU-only", which is a different and wrong instruction.
    * **an unsettled width** (0, a bool, a non-int, a partially built Engine with no
      ``_eval_parallel``) -> ``None``. Launch-time AUTO is spelled ``0`` and is resolved off the box
      in `Engine.__init__`; a caller that reads a ``0`` here is reading the width BEFORE it settled,
      and the fix is to move the read, never to guess.
    """
    for value, floor in ((pool, 0), (eval_parallel, 1)):
        if isinstance(value, bool) or not isinstance(value, int) or value < floor:
            return None
    if pool == 0:
        return 0
    return max(1, pool // eval_parallel)


def proposal_derived_width(pool, footprints, *, ceiling) -> Optional[int]:
    """How many experiments the PROPOSALS ask to run at once, capped by what the box can serve.

    docs/29 F1: *"if I want to run one experiment per card — who decides that and how? Ideally
    automatically, from the propose."*  Today AUTO means one experiment per detected GPU, i.e. the
    width is a fact about the BOX.  This is the fact about the WORK, derived from the same declared
    `footprint.gpus` the Researcher is already told a ceiling for by `per_experiment_gpu_budget`.

    ``footprints`` is one entry per OPEN proposal — the declared `gpus` as an int, or ``None`` for a
    card that declared nothing.  ``ceiling`` is the run's own launch treatment (`run_started`'s pinned
    eval width), never the resuming box's AUTO resolution: a resume onto a bigger host continues under
    the width its log was written under (invariant #6), so the proposals may narrow the run and may
    widen it back, but never past what the operator's launch actually authorized.

    ``None`` means "derive nothing, leave the width alone", and it is the answer far more often than
    a number is.  The three cases, each decided rather than inherited:

    * **no open proposals** -> ``None``.  An empty board is not a proposal to go serial; it is the
      ordinary state between a terminal and the next research turn, and narrowing on it would make
      the width oscillate with the Card board's own churn.
    * **pool == 0** -> ``None``.  The width shares out DEVICES.  With none, AUTO already settled to
      serial `1` (or the task is CPU-locked), there is nothing to divide, and a number derived here
      would be about the Card count alone — which is a proposal to oversubscribe the CPU, not a
      reading of the research.
    * **a malformed pool/ceiling** -> ``None``, never a guess.  Same rule as `settle_width`: a value
      that is not a width leaves the running envelope exactly as it was.

    THE RULE WHEN THE PROPOSALS ASK FOR MORE THAN THE BOX HAS: **the surplus queues; the width never
    oversubscribes the pool.**  ``demand`` is what the research wants (one experiment per open card);
    ``capacity`` is ``pool // need``, where ``need`` is the WIDEST single declared footprint.  The
    width is the minimum of demand, capacity and the launch ceiling.  Five proposals each declaring
    one GPU on a two-GPU box settle to **2**, not 5.  Three reasons, and each is a defect this
    function would otherwise introduce rather than a preference:

    1. **It would make `per_experiment_gpu_budget` announce a lie.**  That rule tells the Researcher
       it may declare ``pool // eval_parallel`` GPUs per experiment.  Capping the width at
       ``pool // need`` keeps ``pool // width >= need``, so the ceiling the next proposal is quoted is
       always at least what the open proposals already declared.  Let the width exceed the pool and
       the announcement collapses to the ``eval_parallel > pool -> 1`` edge — the engine would tell a
       Card "you may have one" one turn after admitting a Card that declared two.
    2. **An eval slot above the device count buys no parallelism.**  It buys a node holding an eval
       slot while blocked in `resources.py::_acquire_gpus`, which is the barrier docs/29 F1f is about.
    3. **`_dispatch_evals`'s aged-head escape hatch stops working above the pool.**  It compares free
       semaphore tokens against the width to detect a drained pool; a width above the batch's own
       token total makes that test unreachable and wedges the batch.

    An undeclared footprint counts as ``1``, not as zero demand: `resources.py::_resource_request_
    for_node` gives exactly one device to an undeclared footprint whenever the run is parallel and has
    a pool, so one is what such a card will actually take.  A card declaring ``gpus: 0`` is genuinely
    CPU-only and contributes no GPU demand — but it still contributes one unit of ``demand``, because
    it is still an experiment somebody proposed to run.

    ``need > pool`` (a proposal declaring more devices than exist) yields capacity 0 and therefore a
    settled ``1`` — deliberately the same answer, for the same reason, as `per_experiment_gpu_budget`'s
    oversubscribed edge: there ARE devices, the declaration is clamped at reservation
    (`_clamp_resource_footprint`), and the scheduler queues rather than refusing.
    """
    for value, floor in ((pool, 0), (ceiling, 1)):
        if isinstance(value, bool) or not isinstance(value, int) or value < floor:
            return None
    entries = list(footprints or ())
    if not entries or pool == 0:
        return None
    demand = len(entries)
    need = 1
    for declared in entries:
        # Strict, like every other reader of a Researcher-authored number: a bool/float/string in a
        # declaration is not a device count, and treating one as demand would let a malformed card
        # reshape the run's execution treatment. Such a card falls back to the undeclared `1`.
        if isinstance(declared, bool) or not isinstance(declared, int) or declared < 0:
            declared = 1
        need = max(need, declared)
    return max(1, min(demand, pool // need, ceiling))


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
