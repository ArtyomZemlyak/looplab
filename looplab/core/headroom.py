"""HEADROOM: how far a run's best closed the gap between the task's baseline and its target (doc 67
67.14). REPORTING, not agent quality — nothing here reaches a selection, a prompt or a gate.

WHY. A task declared neither a baseline nor a reference score, so the RSI survey's headroom-closed
index, AIRS-Bench's normalized score and Agora's "share of the gap closed" were not computable, and
a portfolio could not be compared ACROSS tasks: 0.79 recall on one corpus and 8.5 squared error on
another say nothing side by side. A task may now declare `reference_score: {baseline: {value,
source}, target?: {value, source}}` — MEASURED numbers with where each came from, never a guess
(`adapters/repo_task.py::ReferenceScoreSpec`) — the engine pins it on `run_started`, and the run row
carries `headroom(best, reference, direction)`.

THE ARITHMETIC. `gain` is the best's improvement over the baseline in the run's own direction
(positive = better). `gap_closed = (best - baseline) / (target - baseline)`: 0 at the baseline, 1 at
the target, above 1 past it and below 0 when the run did worse than the baseline — the two
directions share the formula because the sign of the denominator carries the direction. It is
`None` when no target is declared, when the target is not BETTER than the baseline in the run's
direction (the declaration contradicts the objective, and a ratio over it would be read backwards —
`target_not_better` says so), or when there is no best. `best` rides the result: the number the gain
was measured FROM (the champion's robust metric), so a row that also shows the raw metric cannot be
read as measuring a different one (critic 2026-09-26, driven: a confirmed champion's row showed
10.394 beside a gain implying 10.991).

FINITE OR NONE. Two finite, accepted marks can still overflow — a target 1e-310 from the baseline
makes the share infinite, magnitudes near 1e308 make the gain so — and a non-finite float is not
JSON: the run list answered 500 for EVERY run while one row held it (critic 2026-09-26, driven). A
quantity that is not a finite number is `None`, and the CLI line says so.

Layering: `core`, pure arithmetic over already-normalized JSON; the fold normalizes the pinned
payload through `normalized_reference`, so `events/replay.py` never imports the adapter models.
"""
from __future__ import annotations

import math
from typing import Optional

# A source is a citation — a paper, a leaderboard row, the run that measured it — not an essay.
_SOURCE_MAX_CHARS = 500


def _mark(raw) -> Optional[dict]:
    if not isinstance(raw, dict):
        return None
    value = raw.get("value")
    source = raw.get("source")
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(float(value))):
        return None
    if not isinstance(source, str) or not source.strip():
        return None
    return {"value": float(value), "source": source.strip()[:_SOURCE_MAX_CHARS]}


def normalized_reference(raw) -> Optional[dict]:
    """The pinned `run_started.reference_score` as the fold keeps it: a baseline mark (a finite value
    and a non-empty source) and an optional target mark, or None — never a partial guess. A
    malformed target drops the target, a malformed baseline drops the whole reference."""
    if not isinstance(raw, dict):
        return None
    baseline = _mark(raw.get("baseline"))
    if baseline is None:
        return None
    target = _mark(raw.get("target"))
    return {"baseline": baseline, **({"target": target} if target is not None else {})}


def headroom(best, reference, direction: str) -> Optional[dict]:
    """`{baseline, target, gain, gap_closed[, target_not_better]}` for a run whose best is `best`,
    or None when there is no best or no usable reference. See the module docstring."""
    reference = normalized_reference(reference)
    if reference is None or best is None or isinstance(best, bool):
        return None
    try:
        best = float(best)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(best):
        return None
    baseline = reference["baseline"]["value"]
    target_mark = reference.get("target")
    sign = 1.0 if direction == "max" else -1.0
    out = {"baseline": reference["baseline"], "target": target_mark, "best": best,
           "gain": _finite(sign * (best - baseline)), "gap_closed": None}
    if target_mark is not None:
        span = target_mark["value"] - baseline
        if sign * span > 0:
            out["gap_closed"] = _finite((best - baseline) / span)
        else:
            out["target_not_better"] = True
    return out


def _finite(value: float) -> Optional[float]:
    return value if math.isfinite(value) else None


def headroom_line(room: dict) -> str:
    """The one line `looplab run`/`resume`/`inspect` print under the champion when a reference is
    declared (`cli/__init__.py::_print_result`)."""
    baseline = room["baseline"]
    gain = room.get("gain")
    line = ((f"headroom: {gain:+.6g}" if gain is not None
             else "headroom: a gain that is not a finite number")
            + f" over the baseline {baseline['value']:.6g} ({baseline['source']})")
    target = room.get("target")
    if room.get("gap_closed") is not None:
        line += (f"; {room['gap_closed']:.1%} of the gap to the target {target['value']:.6g} "
                 f"({target['source']})")
    elif room.get("target_not_better"):
        line += "; the declared target is not better than the baseline in this run's direction"
    elif target is not None:
        line += (f"; the share of the gap to the target {target['value']:.6g} is not a finite "
                 "number")
    return line
