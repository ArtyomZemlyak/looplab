"""Was this run STARVED? — eval occupancy over the durable log, with bootstrap separated.

MEASURED, and the reason this exists: answering "were the GPUs busy" took an ad-hoc script three
times in one day, and the first two answers were WRONG IN THE SAME WAY. Reporting "36 % of the run
had zero evaluations" counts the BOOTSTRAP — the stretch before the first build can possibly have
finished — as starvation. No run can evaluate before it has built something, so that time is not a
starved lane and folding it into one number hides the thing worth seeing.

Separated, over the two runs on this box:

    v9   run 24.74 h | bootstrap 1.09 h | after that 23.66 h span, 6.61 h dead (28 %)
           dead 12.28h -> 13.69h (1.41 h) and 13.76h -> 18.96h (5.20 h)
    v10  run  4.32 h | bootstrap 1.50 h | after that  2.82 h span, 0.00 h dead (0 %)

Same engine, same `eval_parallel: 2`, opposite outcomes — which is the comparison the single
percentage could not make. The 5.20 h window is the one that costs GPU hours and it is what
`OPEN`-tracked work is chasing; the bootstrap is not a defect and must not be reported as one.

**WHY THE DURABLE LOG AND NOT `spans.jsonl`.** `looplab timings` charges span durations, and a
sidecar replay never rebuilds can be cleared or torn. Occupancy is a question about the RUN, so it
is folded from `node_eval_started` and the node terminals — rows the engine appends under its own
invariants, which survive a cleared trace. It also means this answers on a run whose tracing was off.

**WHAT AN OPEN INTERVAL MEANS.** A node that started and has no terminal is counted as busy up to
the last event in the log — for a live run that is "still running", which is true, and for a killed
one it is the last thing anyone can prove. Both are stated rather than guessed at.
"""
from __future__ import annotations

_EVAL_STARTED = "node_eval_started"
_TERMINALS = ("node_evaluated", "node_failed")


def _field(row, name):
    """One field of an event row, whichever shape the caller has.

    BOTH SHAPES ARE LEGITIMATE AND THE FIRST CUT ACCEPTED ONLY ONE: `EventStore.read_all()` yields
    EVENT OBJECTS, while a reader that walked `events.jsonl` with `json.loads` holds dicts. A
    `isinstance(row, dict)` filter therefore dropped every row the CLI passed it and the fold
    returned a clean, empty, WRONG answer — no exception, no warning, just a section that silently
    declined to print. An occupancy reader that reports "nothing to say" about a busy run is worse
    than one that raises, so it accepts either and the tests drive both.
    """
    if isinstance(row, dict):
        return row.get(name)
    return getattr(row, name, None)


def _ts(row) -> float | None:
    try:
        return float(_field(row, "ts"))
    except (TypeError, ValueError):
        return None


def eval_occupancy(events, width: int | None = None) -> dict:
    """Fold durable rows into an occupancy report. `events` is any iterable of event dicts.

    Returns ``{run_seconds, bootstrap_seconds, span_seconds, dead_seconds, dead_share, dead_windows,
    concurrency, intervals, open_intervals, width}``:

    * `bootstrap_seconds` — first event to the first `node_eval_started`. NOT starvation.
    * `span_seconds` — first eval start to the last event: the window in which starvation is even
      a meaningful question.
    * `dead_windows` — `[(start, end)]` offsets from the run's first event where NO evaluation was
      running, strictly inside `span_seconds`. This is the list an operator actually wants.
    * `concurrency` — seconds spent with 0, 1, 2, … evals in flight, capped at `width` when given so
      a run cannot report more lanes busy than it declared.

    Every duration is seconds; every window offset is relative to the run's first event, because a
    reader comparing two runs needs the same origin and absolute epochs make that arithmetic by
    hand. `dead_share` is over `span_seconds`, never over the whole run — dividing by the run would
    reintroduce the bootstrap error this module was written for.
    """
    rows = [e for e in events if isinstance(e, dict) or hasattr(e, "ts")]
    stamps = [t for t in (_ts(e) for e in rows) if t is not None]
    if not stamps:
        return {"run_seconds": 0.0, "bootstrap_seconds": 0.0, "span_seconds": 0.0,
                "dead_seconds": 0.0, "dead_share": 0.0, "dead_windows": [], "concurrency": {},
                "intervals": [], "open_intervals": 0, "width": width}
    t0, last = min(stamps), max(stamps)
    starts: dict = {}
    ends: dict = {}
    for e in rows:
        stamp = _ts(e)
        if stamp is None:
            continue
        data = _field(e, "data")
        node = (data or {}).get("node_id") if isinstance(data, dict) else _field(e, "node_id")
        if node is None:
            continue
        kind = _field(e, "type")
        if kind == _EVAL_STARTED:
            starts.setdefault(node, stamp)          # FIRST start wins: a re-append is not a re-run
        elif kind in _TERMINALS:
            ends.setdefault(node, stamp)            # FIRST terminal wins, as the fold does
    intervals = []
    open_intervals = 0
    for node in sorted(starts, key=lambda n: (starts[n], str(n))):
        begin = starts[node]
        finish = ends.get(node)
        if finish is None:
            finish = last                           # still running, or the log stops here
            open_intervals += 1
        if finish >= begin:
            intervals.append((begin, finish, node))
    if not intervals:
        # A run with no evaluation at all: the whole thing is bootstrap, and there is no span in
        # which starvation could be measured. Saying "100 % dead" here would be the same category
        # error as counting the bootstrap.
        return {"run_seconds": last - t0, "bootstrap_seconds": last - t0, "span_seconds": 0.0,
                "dead_seconds": 0.0, "dead_share": 0.0, "dead_windows": [], "concurrency": {},
                "intervals": [], "open_intervals": 0, "width": width}
    first_start = min(a for a, _b, _n in intervals)
    span = last - first_start
    # MERGE, then read the holes. Merging is what makes overlapping evals one busy stretch rather
    # than two, which is the whole point on a multi-lane run.
    ordered = sorted((a, b) for a, b, _n in intervals)
    merged = [list(ordered[0])]
    for begin, finish in ordered[1:]:
        if begin <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], finish)
        else:
            merged.append([begin, finish])
    dead_windows = [(merged[i][1] - t0, merged[i + 1][0] - t0) for i in range(len(merged) - 1)]
    dead = sum(b - a for a, b in dead_windows)
    # Concurrency over the same interval set, on the boundaries where it can change.
    points = sorted({x for a, b, _n in intervals for x in (a, b)} | {t0, last})
    concurrency: dict = {}
    for x, y in zip(points, points[1:]):
        live = sum(1 for a, b, _n in intervals if a <= x and y <= b)
        if width is not None:
            live = min(live, int(width))
        concurrency[live] = concurrency.get(live, 0.0) + (y - x)
    return {
        "run_seconds": last - t0,
        "bootstrap_seconds": first_start - t0,
        "span_seconds": span,
        "dead_seconds": dead,
        "dead_share": (dead / span) if span > 0 else 0.0,
        "dead_windows": dead_windows,
        "concurrency": concurrency,
        "intervals": [(a - t0, b - t0, n) for a, b, n in intervals],
        "open_intervals": open_intervals,
        "width": width,
    }
