"""The RENDERING half of the run-diagnostic commands (`timings`, `tokens`).

Extracted 2026-09-07 because `inspect_cmds.py` crossed the line cap
`tests/test_cli_command_groups.py::test_no_group_is_a_god_module_again` holds it to, and that guard
had already NAMED this extraction as the one to do when the headroom was spent: "the `tokens`
command's rendering half (the per-card / per-build table echoes) is a coherent unit that can move
beside `events/token_spend.py`'s pure folds into a cli-side helper module". Raising the cap instead
would have bought the room by inviting the deletion of the why-comments, which is the trade that
raise refused.

What lives here is the printing and the span vocabulary the two commands SHARE — nothing decides
anything. The folds stay where they were (`events/token_spend.py`, `engine/*`), the commands stay in
`inspect_cmds.py`, and every function here takes what it prints as an argument. The two exceptions
are pure DERIVATIONS a single run's own record answers and no other caller wants — `stage-dups`'
stage-identity rows (moved here 2026-09-07, its reason stated at the block) and the run-opening
split — which live beside the echo that renders them for the same cap argument this header opens
with; each is testable without the command, and neither decides anything either.

ONE rendering module, not two. Both sides of the 2026-09-07 merge extracted one for this same cap,
citing this same guard docstring — master this file, the branch a `cli/token_report.py` holding
`echo_card_and_build_tables` — and the merge kept both, so two siblings had overlapping charters and
this header claimed a `tokens` half that lived in the other file. `token_report.py` is folded in
here; a second rendering module would need a charter this one does not already cover.
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Optional

import typer

from looplab.events.eventstore import EventStore
from looplab.events.token_spend import (CARD_UNATTRIBUTED, token_spend_by_build,
                                        token_spend_by_card)


def span_category(sp: dict) -> str:
    """The report row a span belongs to. Same vocabulary for the per-node and the run-level section,
    deliberately: an operator reading `LLM` / `tools` / `eval` / `repair` / `op:<name>` under a node
    must not have to learn a second vocabulary to read the run-level block below it."""
    k = sp.get("kind")
    if k == "generation":
        return "LLM"
    if k == "tool":
        return "tools"
    if k == "operation":
        nm = str(sp.get("name") or "")
        if "eval" in nm:
            return "eval"
        if "repair" in nm:
            return "repair"
        return f"op:{nm}" if nm else "op"
    return k or "other"


def span_seconds(value) -> float:
    """A span's `duration_s`/`start` as a usable non-negative float, else 0.0.

    A span line can be a well-formed JSON object with a junk field — `spans.jsonl` is written by a
    tracer that promises never to raise into the operation it observes (`default=str` in its
    exporter), so a stray non-numeric duration reaches a reader intact. A bare `float()` on it would
    take down the whole report over one bad span, which is the failure this command's lenient read
    exists to prevent.
    """
    if type(value) not in (int, float):        # `type`, not isinstance: `float(True)` is 1.0
        return 0.0
    return float(value) if math.isfinite(value) and value > 0 else 0.0


def traced_seconds(intervals: list) -> float:
    """Wall-clock seconds during which AT LEAST ONE span was open — the union of `[start, start+dur)`.

    Not the sum: the engine builds, evaluates and researches concurrently (`llm_parallel`,
    `eval_parallel`, the background research task), so summing self-time can exceed the run's own
    wall clock and would make the residual below go negative. A union cannot, so `wall - traced` is
    always a real, non-negative quantity: the time no span covered.
    """
    total = 0.0
    cur_start = cur_end = None
    for start, end in sorted(intervals):
        if cur_start is None:
            cur_start, cur_end = start, end
        elif start > cur_end:                 # a genuine gap — bank the block and open a new one
            total += cur_end - cur_start
            cur_start, cur_end = start, end
        else:
            cur_end = max(cur_end, end)
    if cur_start is not None:
        total += cur_end - cur_start
    return total


def minutes(seconds: float) -> float:
    return round(seconds / 60, 1)


def echo_section(title: str, cats: dict, note: str = "") -> None:
    """One `node N`/`run-level` block: total, then its rows biggest-first with a share of the block."""
    total = sum(v[0] for v in cats.values()) or 1.0
    typer.echo(f"\n{title} — {minutes(total)} min:" + (f"   {note}" if note else ""))
    for cat, (secs, n) in sorted(cats.items(), key=lambda x: -x[1][0]):
        typer.echo(f"  {cat:10} {minutes(secs):>6} min  ({n} spans, {round(100*secs/total)}%)")


def echo_containments(spans: list) -> None:
    """HOW MANY OF THIS RUN'S SPANS SWALLOWED A FAILURE (doc 52 row 14): the read side of
    `core/containment.py::contain`, printed only when something was contained so a run with no
    stamp — every pre-row-14 run on disk included — keeps the historical report byte for byte."""
    from looplab.core.containment import contained_summary
    total, stamped, top = contained_summary(spans)
    if not total:
        return
    typer.echo(f"\ncontained failures: {total} across {stamped} span(s) — swallowed by a handler that "
               f"stamped the span (core/containment.py::contain); not an error count, an honesty count")
    for key, count in top:
        typer.echo(f"  {count:>4} × {key}")


def echo_reconciliation(*, wall, intervals: list, attributed: float, durable_events) -> None:
    """The run-level tail of `looplab timings`: the wall-clock reconciliation, and under it the
    complementary eval-occupancy question folded from the DURABLE log.

    EXTRACTED 2026-09-07, at the merge with master, and it is the unit BOTH sides' cap
    docstrings had already named as the next one to move (`tests/test_cli_command_groups.py`:
    "the extraction to do when THAT is spent is `timings`' own reconciliation block"). The merge
    put `inspect_cmds` at 1258 against a cap of 1200 — each side having independently spent its
    own headroom — and the answer prescribed there is an extraction, not a fourth raise.

    Every echo is verbatim; `wall is None or wall <= 0` still means "print nothing", which the
    caller used to express as an early `return` from the command.
    """
    from looplab.events.eval_occupancy import eval_occupancy

    if wall is None or wall <= 0:
        return
    traced = min(traced_seconds(intervals), wall)
    untraced = max(0.0, wall - traced)
    typer.echo(f"\nreconciliation vs {minutes(wall)} min wall clock:")
    typer.echo(f"  attributed {minutes(attributed):>6} min  ({round(100*attributed/wall)}%)  "
               f"sum of the rows above; overlaps under concurrency")
    typer.echo(f"  traced     {minutes(traced):>6} min  ({round(100*traced/wall)}%)  "
               f"wall clock with at least one span open")
    typer.echo(f"  untraced   {minutes(untraced):>6} min  ({round(100*untraced/wall)}%)  "
               f"no span open — not attributable from spans.jsonl")

    # WAS THIS RUN STARVED? The rows above charge wall clock to WORK; this asks the complementary
    # question — how much of the run had no evaluation running at all — and it is folded from the
    # DURABLE log rather than from spans, so it answers on a run whose trace was cleared or never
    # written. Printed here because an operator reading "where did the time go" is one line away
    # from "and how much of it bought nothing".
    #
    # THE BOOTSTRAP IS SEPARATED AND THAT IS THE WHOLE POINT: answering this by hand three times in
    # one day produced two wrong numbers the same way, by counting the stretch before the first
    # build could possibly have finished as starvation. Measured across this box's two runs — v9
    # 6.61 h dead of a 23.66 h span (28 %) in two windows, v10 0.00 h of 2.82 h — same engine, same
    # eval_parallel=2, opposite outcomes, and the single all-run percentage could not tell them
    # apart. `dead_share` is over the SPAN, never over the run.
    if durable_events is not None:
        # No `width` is passed: capping concurrency needs the run's SETTLED eval width, and this
        # command deliberately does not fold state — the raw count is the honest answer, and a run
        # showing more concurrent evals than it declared is itself worth seeing.
        occ = eval_occupancy(durable_events)
        if occ["span_seconds"] > 0:
            typer.echo("\neval occupancy (from events.jsonl, not spans):")
            typer.echo(f"  bootstrap  {minutes(occ['bootstrap_seconds']):>6} min  "
                       f"before the first evaluation could start — not starvation")
            typer.echo(f"  dead       {minutes(occ['dead_seconds']):>6} min  "
                       f"({round(100*occ['dead_share'])}% of the {minutes(occ['span_seconds'])} min "
                       f"since) with NO evaluation running")
            for start, end in occ["dead_windows"]:
                typer.echo(f"    idle {minutes(start):>6}-{minutes(end):<6} min "
                           f"({minutes(end - start)} min)")
            busy = ", ".join(f"{k}: {minutes(v)} min"
                             for k, v in sorted(occ["concurrency"].items()))
            typer.echo(f"  concurrent evaluations — {busy}")
            if occ["open_intervals"]:
                typer.echo(f"  {occ['open_intervals']} evaluation(s) still open at the last event — "
                           f"counted busy to there, which is true of a live run and is the most a "
                           f"killed one can prove")

# ------------------------------------------------------ the head of the run (`looplab timings`)
# WHY THIS EXISTS AND WHY IT IS AN INSTRUMENT AND NOT A FIX
# (`first-propose-runs-with-every-gpu-idle`). The opening `propose` is systemically the longest
# phase of a run — over the seven runs measured on 2026-08-25 it is the run's MAXIMUM in four of
# them, at 1.6-6.4x that run's own median, and
# `e5small-dr-unified-v3` spent 2 h 18 min there before dying with three nodes and no metric. What
# makes it expensive is WHEN: at `n == 0` no node exists, so nothing is evaluating and every GPU the
# box has is idle for the whole phase. The prescription attached to that item says to MEASURE THE
# SPLIT between the run-opening think (`engine/research_cadence.py::_ground_run_start`) and the first
# propose before choosing an overlap, because those minutes were only ever recorded as their SUM —
# and the split could not be reconstructed afterwards: it is a property of each run's own
# `spans.jsonl`, a sidecar `fold` never rebuilds, and not one of the seven runs' span files survives.
# So the split is made a number a run PRODUCES, at the boundaries the engine already writes, rather
# than one somebody reconstructs from a corpus that may not be there when the question is asked.
#
# THE BOUNDARIES COME FROM THE DURABLE LOG WHEREVER THE LOG CAN SAY, for the reason
# `events/eval_occupancy.py` records: a span sidecar can be cleared, torn or switched off, and a
# question about the RUN must still have an answer. Only the propose itself has no durable boundary
# pair of its own (a node id is reserved AFTER the proposal is final — see
# `orchestrator.py::_prepare_node_idea` — so `node_building` bounds it from above but does not say
# when the model finished), and that one row is read from the `propose` span: the same population the
# 2026-08-25 table was built from.


def _row_field(row, name):
    """One field of an event row, whichever shape the caller holds.

    BOTH SHAPES ARE LEGITIMATE, and `events/eval_occupancy.py::_field` carries the measurement for
    why this is not an `isinstance(row, dict)` filter: `EventStore.read_all()` yields Event OBJECTS
    while a reader that walked `events.jsonl` with `json.loads` holds dicts, and a fold that accepts
    only one of them returns a clean, empty, WRONG answer — a report that silently declines to print
    about a run that has plenty to say.
    """
    if isinstance(row, dict):
        return row.get(name)
    return getattr(row, name, None)


def _row_stamp(row):
    """A row's usable wall-clock timestamp as a float, or None.

    `events/replay.py::event_timestamp` is the ONE spelling of "is this timestamp usable" — it
    rejects `0.0` (the `Event.ts` default, i.e. "no timestamp", not 1970), a JSON `true`, a string
    and a date past 9999 — and this must not grow a second, drifting copy of that rule, so a dict row
    is handed to it in the attribute shape it reads rather than re-tested here.
    """
    from types import SimpleNamespace

    from looplab.events.replay import event_timestamp
    return event_timestamp(row if not isinstance(row, dict)
                           else SimpleNamespace(ts=row.get("ts")))


def _first_row(rows, kind, where=None):
    """The EARLIEST row of one type (optionally matching a payload predicate), as `(ts, data)`.

    Earliest by TIMESTAMP, not by position: every other reader of the log is order-tolerant
    (invariant #5) and a head-of-run measurement that trusted iteration order would be the one place
    a spliced background row could move a published number.
    """
    best = None
    for row in rows:
        if _row_field(row, "type") != kind:
            continue
        stamp = _row_stamp(row)
        if stamp is None:
            continue
        data = _row_field(row, "data")
        data = data if isinstance(data, dict) else {}
        if where is not None and not where(data):
            continue
        if best is None or stamp < best[0]:
            best = (stamp, data)
    return best


def _operation_spans(spans, name):
    """Every `operation` span of one name, as `(start, end)` absolute wall clock, earliest first.

    A span with no usable `start` cannot be PLACED, only counted, so it is skipped here exactly as
    `timings`' own union skips it (and says how many it skipped). `span_seconds` on both fields for
    the reason it exists: `spans.jsonl` is written by a tracer that promises never to raise into the
    operation it observes, so a junk `duration_s` must cost this row, not the report.
    """
    out = []
    for span in spans or ():
        if not isinstance(span, dict):
            continue
        if span.get("kind") != "operation" or span.get("name") != name:
            continue
        start = span_seconds(span.get("start"))
        if not start:
            continue
        out.append((start, start + span_seconds(span.get("duration_s"))))
    return sorted(out)


def run_opening_split(events, spans) -> dict:
    """Where the head of the run went, split at the boundaries it already writes.

    The window is the run's first event to its first `node_eval_started` — by construction the
    stretch in which NO evaluation of this run was running, so a GPU-shaped run holds its lease and
    burns nothing for the whole of it (`engine/resources.py`; an offline adapter takes no lease at
    all, which is why the report says "no evaluation was running" — what the log can prove — rather
    than naming a device it cannot see).

    Returns ``{available, opening_seconds, open, phases, unattributed_seconds, to_think_seconds,
    to_propose_seconds, think_to_propose_seconds, propose_outside_opening, notes}``. `phases` is an
    ORDERED list of disjoint `{name, seconds, boundary, source}` rows; `unattributed_seconds` is
    the rest of the window, SIGNED like every other residual this command prints, so a negative
    value is shown as an overlap rather than clamped into a false zero.

    THE TWO HEADLINES ARE THE ITEM'S OWN QUESTION: `to_think_seconds` is run start -> the run-opening
    think complete, `to_propose_seconds` is run start -> the first propose complete, and their
    difference is the half of the sum that the 2026-08-25 propose table could not separate.

    ABSENCE IS REPORTED, NEVER RENDERED AS ZERO — the discipline `trust/scan_receipt.py` states for
    the same class of question. A run with no run-opening think (deep research off, no researcher
    wired, or a log written before 2026-08-12) yields `to_think_seconds=None` and a note saying so;
    a run whose trace was cleared yields `to_propose_seconds=None` and a different one; a run whose
    first node was built on a path that opens no `propose` span at all yields the same None and a
    THIRD note, because "this run cannot answer" is not "turn tracing on".
    """
    rows = list(events or ())
    stamps = [t for t in (_row_stamp(row) for row in rows) if t is not None]
    if not stamps:
        return {"available": False, "opening_seconds": None, "open": False, "phases": [],
                "unattributed_seconds": 0.0, "to_think_seconds": None, "to_propose_seconds": None,
                "think_to_propose_seconds": None, "propose_outside_opening": False,
                "notes": ["no row carries a usable timestamp"]}
    t0, last = min(stamps), max(stamps)
    setup_started = _first_row(rows, "setup_started")
    setup_finished = _first_row(rows, "setup_finished")
    # By the trigger the DECIDING site writes, imported rather than spelled here: see
    # `engine/research_cadence.py::RUN_START_TRIGGER`.
    from looplab.engine.research_cadence import RUN_START_TRIGGER
    attempted = _first_row(rows, "research_attempted",
                           lambda d: d.get("trigger") == RUN_START_TRIGGER)
    thought = _first_row(rows, "research_completed",
                         lambda d: d.get("trigger") == RUN_START_TRIGGER)
    building = _first_row(rows, "node_building")
    created = _first_row(rows, "node_created")
    dispatched = _first_row(rows, "node_eval_started")

    close = dispatched[0] if dispatched is not None else last
    # THE OPENING'S propose, not the run's first traced one — measured on a real offline run, which
    # is why this is a window and not a `min()`. The seed/batch path that mints node 0 there opens no
    # `propose` span at all, so the earliest one in the file belongs to a LATER node and starts after
    # the first evaluation was already burning: charging it here would have published a number for
    # this phase that was measured somewhere else entirely. A span that does not fit the window is
    # not the opening's, and the absence is then stated (see the notes below) rather than filled in.
    candidates = _operation_spans(spans, "propose")
    inside = [pair for pair in candidates if pair[1] <= close]
    propose = inside[0] if inside else None
    notes: list[str] = []
    phases: list[dict] = []

    def _phase(name: str, begin, end, boundary: str, source: str) -> None:
        if begin is None or end is None or end < begin:
            return
        phases.append({"name": name, "seconds": end - begin, "boundary": boundary,
                       "source": source})

    _phase("setup", (setup_started or (t0, {}))[0],
           setup_finished[0] if setup_finished else None,
           "setup_started -> setup_finished", "events.jsonl")
    _phase("run-start think", attempted[0] if attempted else None,
           thought[0] if thought else None,
           f"research_attempted -> research_completed, trigger={RUN_START_TRIGGER}", "events.jsonl")
    if propose is not None:
        _phase("first propose", propose[0], propose[1],
               "the first `propose` span inside the window", "spans.jsonl")
    # The build that CONSUMES that proposal. From the propose's own end when the span placed it, and
    # otherwise from the reservation the log does carry — `node_building` is appended once the Idea is
    # final, so on a spans-less run it is the closest durable stand-in for "the model stopped talking".
    _phase("first build",
           propose[1] if propose is not None else (building[0] if building else None),
           created[0] if created else None,
           ("propose end -> node_created" if propose is not None
            else "node_building -> node_created"), "events.jsonl")
    _phase("dispatch", created[0] if created else None,
           dispatched[0] if dispatched else None,
           "node_created -> node_eval_started", "events.jsonl")

    if thought is None:
        notes.append("no run-opening think in this log — deep research is off, no researcher was "
                     "wired, or the run predates `_ground_run_start` (2026-08-12). NOT zero.")
    if propose is None and not candidates:
        notes.append("no `propose` span in spans.jsonl — tracing was off or the trace was cleared, "
                     "so the propose cannot be separated from the build it fed.")
    if propose is None and candidates:
        notes.append(f"{len(candidates)} `propose` span(s) in this run, none of them inside the "
                     "opening window — the first node was built on a path that opens none (the seed "
                     "batch does not), so this run cannot price its opening propose.")
    if dispatched is None:
        notes.append("this run never dispatched an evaluation, so the opening window is the whole "
                     "log: every number below is a lower bound on a run that got no further.")

    opening = close - t0
    named = sum(p["seconds"] for p in phases)
    to_propose = (propose[1] - t0) if propose is not None else None
    to_think = (thought[0] - t0) if thought is not None else None
    return {
        "available": True,
        "opening_seconds": opening,
        "open": dispatched is None,
        "phases": phases,
        "unattributed_seconds": opening - named,
        "to_think_seconds": to_think,
        "to_propose_seconds": to_propose,
        "think_to_propose_seconds": (None if (to_think is None or to_propose is None)
                                     else to_propose - to_think),
        # The run traced a propose, and none of them is this phase's. Reported as its own fact
        # because it is NOT "tracing was off" and must not read as it: the propose that fed node 0
        # happened, it was simply made on a path that opens no span. Which of the two absences a run
        # has is the difference between "turn tracing on" and "this run cannot answer".
        "propose_outside_opening": bool(propose is None and candidates),
        "notes": notes,
    }


def echo_run_opening(events, spans, *, wall=None) -> None:
    """WHERE THE BOOTSTRAP WENT: the run-opening split, printed under `timings`' occupancy block.

    Reads best directly below `eval occupancy`, which names the bootstrap — the stretch before the
    first evaluation could start — and until now could not say what was IN it. Prints nothing at all
    when the log carries no usable timestamp, so a spans-only directory keeps its historical report.
    """
    if events is None:
        return
    split = run_opening_split(events, spans)
    if not split["available"] or (split["opening_seconds"] or 0) <= 0:
        return
    share = f", {round(100 * split['opening_seconds'] / wall)}% of the run" if wall else ""
    # UNCONDITIONAL, because the window makes it true rather than the phases inside it: it ends at
    # the run's FIRST `node_eval_started`, so there is no earlier evaluation for it to contain.
    idle = " — no evaluation was running for any of it"
    typer.echo(f"\nrun opening — {minutes(split['opening_seconds'])} min{share}{idle}:")
    for phase in split["phases"]:
        typer.echo(f"  {phase['name']:<16} {minutes(phase['seconds']):>6} min  "
                   f"{phase['boundary']} ({phase['source']})")
    residual = split["unattributed_seconds"]
    label = "unattributed" if residual >= 0 else "overlap"
    typer.echo(f"  {label:<16} {minutes(abs(residual)):>6} min  "
               f"{'covered by no boundary above' if residual >= 0 else 'the rows above overlap'}")
    if split["to_think_seconds"] is not None:
        typer.echo(f"  run start -> the run-opening think complete: "
                   f"{minutes(split['to_think_seconds'])} min")
    if split["to_propose_seconds"] is not None:
        after = (f" ({minutes(split['think_to_propose_seconds'])} min of it after the think)"
                 if split["think_to_propose_seconds"] is not None else "")
        typer.echo(f"  run start -> the first propose complete:     "
                   f"{minutes(split['to_propose_seconds'])} min{after}")
    for note in split["notes"]:
        typer.echo(f"  ({note})")


# The two pure derivations `stage-dups` reports from, moved here 2026-09-07 for the reason
# `echo_reconciliation` above was: `inspect_cmds` is the run-diagnostics COMMAND group, and a
# fold over `stage_finished` rows plus a comparable fingerprint over what a stage wrote are
# derivations, not commands — the same split `events/token_spend.py` and `cli/token_report.py`
# already draw one module over.
def stage_identity_rows(store) -> list:
    """Every `stage_finished` row that carries a stage-identity record, oldest first."""
    from looplab.runtime.stage_identity import (STAGE_INPUT_KEY, STAGE_KEY_REASON,
                                                STAGE_OUTPUTS_KEY)
    out: list = []
    for ev in store.read_all():
        if getattr(ev, "type", None) != "stage_finished":
            continue
        data = getattr(ev, "data", None) or {}
        out.append({"node": data.get("node_id"), "name": data.get("name"),
                    "status": data.get("status"), "seconds": float(data.get("seconds") or 0.0),
                    "key": data.get(STAGE_INPUT_KEY), "key_reason": data.get(STAGE_KEY_REASON),
                    "outputs": data.get(STAGE_OUTPUTS_KEY)})
    return out


def output_fingerprint(outputs) -> Optional[str]:
    """The `(path, digest)` set a completed stage recorded, as one comparable string, or None.

    The PATH travels with the digest on purpose. Two stages that wrote identical bytes to different
    declared paths did the same work and are duplication worth reporting; two that wrote different
    bytes to the same path are not. Folding either into the other loses one of those facts, so the
    fingerprint carries both and the report says which question it is answering.
    """
    if not isinstance(outputs, list) or not outputs:
        return None
    parts = []
    for row in outputs:
        if not isinstance(row, dict) or not row.get("bound"):
            return None                       # an unbound output names nothing anybody may compare
        parts.append(f"{row.get('path')}={row.get('digest_mode')}:{row.get('digest')}")
    return "|".join(sorted(parts))


def echo_card_and_build_tables(rows, *, state, ev_path: Path, ledger_total: Optional[int]) -> None:
    """Print the per-card table and, under it, the two prices a card table alone cannot show.

    The folds were injected as three parameters when this lived in its own module, on the reasoning
    that the caller and the renderer must not read two different `events/token_spend.py` surfaces.
    They cannot: an import resolves to the same module object, so the seam bought a paragraph of
    justification and nothing else. `ledger_total` stays an argument because it IS the caller's —
    the same denominator the phase table one screen up was reconciled against.
    """
    # Suppressed when no card resolves at all, which is every serial-path run — a lone `(no card)`
    # row states nothing and would push the phase table off a terminal for no reader's benefit.
    card_nodes = {}
    if state is not None:
        from looplab.core.models import is_unevaluated_speculative_discard
        for node in (state.nodes or {}).values():
            card = getattr(getattr(node, "idea", None), "card_id", None)
            if not isinstance(card, str) or not card.strip():
                continue
            owned = card_nodes.setdefault(card, {"nodes": [], "discarded": []})
            owned["nodes"].append(node.id)
            # The run's SINGLE answer to "did this node spend budget", not a second spelling of it.
            if is_unevaluated_speculative_discard(state, node):
                owned["discarded"].append(node.id)
    by_card = token_spend_by_card(rows, card_nodes=card_nodes, ledger_total=ledger_total)
    real = [r for r in by_card["rows"] if r["card"] != CARD_UNATTRIBUTED]
    if real:
        typer.echo("")
        typer.echo(f"{'tokens':>14}  {'share':>6}  {'calls':>6}  nodes                 card")
        for row in by_card["rows"]:
            nodes = ",".join(str(n) for n in row["nodes"]) or "-"
            if row["wholly_discarded"]:
                nodes += " DISCARDED"
            typer.echo(f"{row['tokens']:>14,}  {100 * row['share']:>5.1f}%  {row['calls']:>6,}  "
                       f"{nodes:<21} {row['card']}")
        # A build that minted NO node is invisible to the rule above, which needs the card to OWN
        # one — measured on v9, that hid 40.1M tokens (card-2's first build and card-5's only one,
        # both `skipped: stale`), while card-2's row read as a healthy 97.6M. Priced from the
        # durable log's own `card_build_requested` -> `card_build_done` windows rather than by
        # widening `wholly_discarded`, which answers a different question and answers it correctly.
        builds = []
        if state is not None:
            open_req = {}
            for ev in EventStore(ev_path).read_all():
                kind = getattr(ev, "type", None) or (ev.get("type") if isinstance(ev, dict) else None)
                data = getattr(ev, "data", None) or (ev.get("data") if isinstance(ev, dict) else None) or {}
                ts = getattr(ev, "ts", None) or (ev.get("ts") if isinstance(ev, dict) else None)
                cid = data.get("card_id")
                if kind == "card_build_requested":
                    open_req[cid] = ts
                elif kind == "card_build_done":
                    builds.append({"card": cid, "start": open_req.pop(cid, None), "end": ts,
                                   "skipped": data.get("skipped"), "node_id": data.get("node_id")})
        by_build = token_spend_by_build(rows, builds)
        if by_build["skipped_builds"]:
            share = (100 * by_build["skipped_tokens"] / by_card["attributed"]) if by_card["attributed"] else 0.0
            typer.echo(f"{by_build['skipped_tokens']:>14,}  {share:>5.1f}%  {'':>6}  "
                       f"built and SKIPPED as stale, minting no node "
                       f"({by_build['skipped_builds']} of {by_build['builds']} builds)")
        lost = [r for r in by_card["rows"] if r["wholly_discarded"]]
        if lost:
            spent = sum(r["tokens"] for r in lost)
            share = 100 * sum(r["share"] for r in lost)
            # Stated as what it IS — a build that was paid for and never evaluated — and NOT as
            # waste: the freshness gate discards a prefetch whose selection no longer holds, which
            # is the machinery working. What the number buys the operator is the ability to weigh
            # that trade, which until now had no visible price at all.
            typer.echo(f"{spent:>14,}  {share:>5.1f}%  {'':>6}  "
                       f"built and never evaluated ({len(lost)} card(s) discarded before dispatch)")
