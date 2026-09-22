"""Containment made countable: `contain(reason, exc)` — the one way to say "this was swallowed".

The house exception posture is contain-and-continue: 670 handlers under `looplab/` catch
`Exception`/`BaseException` without re-raising, most of them argued locally in a `# noqa: BLE001 —
<why>` reason (doc 52 row 14; doc 50 XP-03). What the posture never had was a COUNT. A contained
failure left no mark on the span it happened in, so `looplab timings` could not say how many of a
run's tool calls, generations or watchdog ticks ended in a swallowed error, and the judge bench could
not tell a judge that answered from one that was contained into its fallback. The measured cost sat
at the seams — the `_AshaStub` incident (an AttributeError swallowed into a watchdog that silently
stopped producing verdicts), a run dropped from `/api/runs` on a fold error, an outage that read as a
clean "no blind spots" verdict.

`contain()` is called INSIDE a blind handler and does three things, none of which can raise into the
caller: it stamps the enclosing span (`contained` = the count so far, plus a `contained` event
carrying the reason and the exception type, both through `SpanHandle`'s sanitizers so a secret in an
error message never reaches `spans.jsonl`), it bumps a process-wide counter tests and diagnostics can
read (`containment_counts`), and it logs at DEBUG. It ANNOTATES: the handler's own fallback is still
the handler's, and nothing here reaches a metric, a champion, a violation or a selection.

THE ONE THING IT REFUSES TO CONTAIN is `core/llm.py::BudgetExceeded`. That exception is the
operator's spend ceiling doing its job; a handler that swallows it lets the run keep billing past the
limit set to stop it (`agents/tool_loop.py::resilient` states the rule; `trust/verifier.py::verify`
swallowed it at a SELECTION site until doc 50 AG-01). A handler that reaches `contain()` with one
re-raises it, so adopting the helper at a site is also adopting the funnel — and
`tests/test_containment_census.py` pins, by AST, that every blind handler around a paid call in the
run path re-raises it first.

"WITH ONE" MEANS ANYWHERE INSIDE IT (review 2026-09-22, CORE-07). The ceiling rarely reaches a blind
handler bare: an `anyio` task group hands it over wrapped in an `ExceptionGroup`, and a middle layer
that translates a failure chains it under its own exception. The test here was `isinstance`, so both
shapes were absorbed into the caller's fallback while the CLI (`run_finished` =
`budget_exhausted`) and the eval drain — which ask `core/errors.py::budget_stop_leaf` — recognised
the same object as the ceiling. The refusal now asks that same function, and re-raises the object in
flight (group and chain intact) rather than a leaf torn out of it. `refuse_budget_stop` is the
refusal alone, for a containment site that must not stamp a span.

Adoption is deliberately opportunistic (doc 25 AG-06's rule for `resilient`): the existing
why-comments are load-bearing and are not churned. New sites and the seams named above call it.
"""
from __future__ import annotations

import logging
import threading
from collections import Counter

from looplab.core import tracing
from looplab.core.errors import budget_stop_leaf

log = logging.getLogger(__name__)

_COUNTS: Counter = Counter()
_COUNTS_LOCK = threading.Lock()
# The span attribute and event name, one spelling: `cli/inspect_cmds.py::timings` reads both.
CONTAINED_ATTR = "contained"
CONTAINED_EVENT = "contained"
_REASON_CAP = 120


def contain(reason: str, exc: BaseException | None = None) -> None:
    """Record that `exc` (or an unnamed failure) was contained here, for `reason`.

    Call it from inside the handler. Never raises for an ordinary exception — a broken observer
    must not become a broken agent — and ALWAYS re-raises a `BudgetExceeded`, because a spend stop
    is not a failure to contain (bare, grouped or chained: see `refuse_budget_stop`).
    """
    refuse_budget_stop(exc)
    why = str(reason or "unstated")[:_REASON_CAP]
    kind = type(exc).__name__ if exc is not None else ""
    with _COUNTS_LOCK:
        _COUNTS[why] += 1
    try:
        handle = tracing.current_span_handle()
        if handle is not None:
            attrs = handle.attributes
            handle.set(CONTAINED_ATTR, int(attrs.get(CONTAINED_ATTR, 0) or 0) + 1)
            handle.event(CONTAINED_EVENT, reason=why, exc=kind)
    except Exception:  # noqa: BLE001 — the stamp is telemetry; it must never escalate a contained failure
        pass
    try:
        log.debug("contained (%s): %s%s", why, kind, f": {exc}" if exc is not None else "")
    except Exception:  # noqa: BLE001 — a logging failure is not this helper's to surface
        pass


def refuse_budget_stop(exc: BaseException | None) -> None:
    """Re-raise `exc` when the run's spend ceiling is anywhere inside it; a no-op otherwise.

    The refusal half of `contain`, for a containment site that must stay blind to every OTHER
    failure but has no business stamping a span (`core/parse.py::forced_structured`'s salvage, whose
    telemetry is the parse span's own). Called — never spelled as a `raise` at the site — so the
    site stays what it is in the census: one blind handler, with the funnel inside it.
    """
    if exc is not None and _is_budget_stop(exc):
        raise exc


def _is_budget_stop(exc: BaseException) -> bool:
    # `budget_stop_leaf`, NOT `isinstance`: the ceiling in a task group's `ExceptionGroup`, or
    # chained under a translating layer's exception, is still the ceiling (review 2026-09-22,
    # CORE-07). `core/errors.py` is the light half of the provider vocabulary, so this no longer
    # needs the deferred import of the heavy `core/llm.py` it used to carry.
    return budget_stop_leaf(exc) is not None


def contained_summary(spans) -> tuple[int, int, list[tuple[str, int]]]:
    """`(total, stamped_spans, top_reasons)` over span records as `spans.jsonl` holds them.

    The READ side of the stamp, beside the stamp: `cli/inspect_cmds.py::timings` prints it, and a
    second reader (the judge bench, a report) gets the same rule rather than a second reading of
    the attribute. `top_reasons` is `(reason (ExcType), count)`, most frequent first, at most eight.
    """
    total = 0
    stamped = 0
    reasons: dict[str, int] = {}
    for sp in spans or ():
        if not isinstance(sp, dict):
            continue
        attrs = sp.get("attributes") or {}
        n = attrs.get(CONTAINED_ATTR)
        if type(n) is int and n > 0:
            total += n
            stamped += 1
        for ev in (sp.get("events") or []):
            if isinstance(ev, dict) and ev.get("name") == CONTAINED_EVENT:
                key = str(ev.get("reason") or "unstated")
                exc = str(ev.get("exc") or "")
                label = f"{key} ({exc})" if exc else key
                reasons[label] = reasons.get(label, 0) + 1
    top = sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0]))[:8]
    return total, stamped, top


def containment_counts() -> dict[str, int]:
    """A snapshot of the process-wide count by reason (tests and diagnostics)."""
    with _COUNTS_LOCK:
        return dict(_COUNTS)


def reset_containment_counts() -> None:
    with _COUNTS_LOCK:
        _COUNTS.clear()
