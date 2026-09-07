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
`inspect_cmds.py`, and every function here takes what it prints as an argument.
"""
from __future__ import annotations

import math
from pathlib import Path

import typer

from looplab.events.eventstore import EventStore


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
            typer.echo(f"\neval occupancy (from events.jsonl, not spans):")
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
