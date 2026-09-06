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

Adoption is deliberately opportunistic (doc 25 AG-06's rule for `resilient`): the existing
why-comments are load-bearing and are not churned. New sites and the seams named above call it.
"""
from __future__ import annotations

import logging
import threading
from collections import Counter

from looplab.core import tracing

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
    is not a failure to contain.
    """
    if exc is not None and _is_budget_stop(exc):
        raise exc
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


def _is_budget_stop(exc: BaseException) -> bool:
    # Deferred: `core/llm.py` is the heavy provider module and this helper is called from
    # everywhere, including code paths that never touch a model.
    from looplab.core.llm import BudgetExceeded
    return isinstance(exc, BudgetExceeded)


def containment_counts() -> dict[str, int]:
    """A snapshot of the process-wide count by reason (tests and diagnostics)."""
    with _COUNTS_LOCK:
        return dict(_COUNTS)


def reset_containment_counts() -> None:
    with _COUNTS_LOCK:
        _COUNTS.clear()
