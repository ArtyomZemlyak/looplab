"""Read-only run diagnostics: `replay` / `speculation-gate` / `timings` / `inspect` / `readmodel` /
`tensorboard` / `comparability`.

Split verbatim out of the flat `looplab/cli.py` (docs/15 §P5.2), then split again (doc 25 CT-01):
the module had grown to 1700 lines across three unrelated domains while its docstring still claimed
four commands. What remains here is what the docstring always described — pure folds of a single
run's event log plus viewers over that run's sidecars. The Part IV concept/novelty diagnostics moved
to `concept_cmds.py`; the cross-run governance commands (durable writes and paid LLM stewards) moved
to `governance_cmds.py`, where the fact that they MUTATE is stated in the header rather than buried
200 lines below a "read-only" claim.

`comparability` is the one command here that takes MORE THAN ONE run directory, and it stays in this
group because every fact it prints is folded from each named run's OWN event log — no model call, no
write, no cross-run store. What it adds is a REFUSAL over what it read, and a refusal derived from two
runs' own logs is a diagnostic rather than a durable claim.

Read-only EXCEPT two, and each says which run it touches: `speculation-gate` atomically writes a
local quality receipt without changing any source run, and `readmodel` atomically republishes the
named run's OWN `readmodel.sqlite` — a derived sidecar the engine never reads back, rebuilt in place
because a live or crashed run otherwise has none at all.
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Optional

import orjson
import typer

from looplab.core.run_deletion import (
    RunDeletionFenceError, RunDeletionStorageError, assert_run_deletion_write_allowed)
from looplab.core.run_reset import (
    RunResetFenceError, RunResetStorageError, assert_run_reset_write_allowed)
from looplab.events.eventstore import EventStore
from looplab.events.readmodel import (
    STATUS_CURRENT, coverage_watermark, publish_readmodel, read_watermark, readmodel_status)
from looplab.events.replay import fold
from looplab.events.types import EV_BUDGET
from looplab.engine.comparability import record_of as comparability_record_of
from looplab.trust.scan_receipt import trust_scan_summary
from looplab.cli import (
    _RUN_DIR_HINT, _echo_log_integrity, _print_result, _require_run_dir, app)


@app.command()
def replay(run_dir: Path = typer.Argument(...)):
    """Pure fold of the event log -> current state (read-only)."""
    store = _require_run_dir(run_dir)
    state = fold(store.read_all())
    typer.echo(orjson.dumps(state.model_dump(mode="json"),
                            option=orjson.OPT_INDENT_2).decode())


@app.command(name="speculation-gate")
def speculation_gate(
    run_dirs: list[Path] = typer.Argument(
        ...,
        help=("Exactly three alternating BASELINE TREATMENT pairs (six directories), one "
              "pair for each fixed seed 0, 1 and 2. Each baseline uses depth 0 and each "
              "treatment the same positive depth."),
    ),
    output: Path = typer.Option(
        Path("speculation-quality.receipt.json"),
        "--output", "-o",
        help="Local receipt path to write atomically after every fixed gate passes.",
    ),
):
    """Run scorer-fidelity plus paired real-GPU search-quality gates for Card speculation."""
    if len(run_dirs) != 6:
        typer.echo(
            "speculation-gate requires exactly three alternating BASELINE TREATMENT pairs",
            err=True,
        )
        raise typer.Exit(2)

    from looplab.search.speculation_quality import (
        publish_speculation_gate_receipt,
        speculation_quality_gate,
    )

    pairs = list(zip(run_dirs[0::2], run_dirs[1::2]))
    report = speculation_quality_gate(pairs, require_gpu=True)
    if report.get("passed") is not True:
        typer.echo(orjson.dumps(report, option=orjson.OPT_INDENT_2).decode())
        typer.echo("speculation quality gate failed; no receipt was written", err=True)
        raise typer.Exit(2)
    try:
        # PUBLISH the report just computed rather than calling `write_speculation_gate_receipt`,
        # which would run the whole gate a second time — six run directories re-parsed and the
        # scorer matrix re-executed, purely to reproduce a body this frame is already holding
        # (doc 25 SE-01). The published bytes are byte-identical either way.
        receipt = publish_speculation_gate_receipt(output, report)
    except (OSError, ValueError) as exc:
        typer.echo(f"could not publish speculation gate receipt: {exc}", err=True)
        raise typer.Exit(2) from exc
    typer.echo(orjson.dumps({
        "passed": True,
        "receipt": str(output.resolve()),
        "self_digest": receipt["self_digest"],
        "implementation_digest": receipt["implementation_digest"],
        "environment_sha256": receipt["environment_sha256"],
        "gpu_inventory": receipt["gpu_inventory"],
        "policy_scope": receipt["policy_scope"],
        "workload_scope": receipt["workload_scope"],
        "calibration_seeds": receipt["calibration_seeds"],
        "task_profile_sha256": receipt["task_profile_sha256"],
        "admitted_depth": receipt["admitted_depth"],
        "admitted_max_nodes": receipt["admitted_max_nodes"],
        "runtime_scope_sha256": receipt["runtime_scope_sha256"],
        "calibration_profile_digest": receipt["calibration_profile_digest"],
        "aggregates": receipt["aggregates"],
    }, option=orjson.OPT_INDENT_2).decode())


def _span_category(sp: dict) -> str:
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


def _span_seconds(value) -> float:
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


def _traced_seconds(intervals: list) -> float:
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


def _minutes(seconds: float) -> float:
    return round(seconds / 60, 1)


def _echo_section(title: str, cats: dict, note: str = "") -> None:
    """One `node N`/`run-level` block: total, then its rows biggest-first with a share of the block."""
    total = sum(v[0] for v in cats.values()) or 1.0
    typer.echo(f"\n{title} — {_minutes(total)} min:" + (f"   {note}" if note else ""))
    for cat, (secs, n) in sorted(cats.items(), key=lambda x: -x[1][0]):
        typer.echo(f"  {cat:10} {_minutes(secs):>6} min  ({n} spans, {round(100*secs/total)}%)")


@app.command()
def tokens(run_dir: Path = typer.Argument(...),
           top: int = typer.Option(0, help="only the N largest phases (0 = all)")):
    """Where the TOKENS went, by phase — the token twin of `timings`' wall-clock breakdown.

    The TOTAL and the SPLIT come from different files on purpose. `llm_usage` in `events.jsonl` is
    the durable, replayable ledger and knows the true total but carries no phase; the `generation`
    spans in `spans.jsonl` carry `phase` but live in a sidecar replay never rebuilds. So the ledger
    is the denominator, the spans supply the attribution, and the gap between them is PRINTED.

    A generation with no phase is bucketed, never dropped. Needs `spans.jsonl` for the breakdown;
    without it the ledger total is still reported, then exit 2.
    """
    import json as _json

    from looplab.events.eventstore import read_jsonl_lenient_with_health
    from looplab.events.token_spend import (CARD_UNATTRIBUTED, token_spend_by_build,
                                            token_spend_by_card, token_spend_by_phase)

    ev_path = run_dir / "events.jsonl"
    sp_path = run_dir / "spans.jsonl"
    if not ev_path.exists() and not sp_path.exists():
        typer.echo(f"no run found at {run_dir} (no events.jsonl or spans.jsonl). {_RUN_DIR_HINT}")
        raise typer.Exit(2)

    # The DENOMINATOR: the durable ledger's own sum. Read first so a run with tracing off still
    # learns what it spent, even though it can never learn where.
    ledger_total = None
    state = None
    if ev_path.exists():
        store = EventStore(ev_path)
        _echo_log_integrity(store, run_dir)
        # THROUGH THE FOLD, not a hand sum over `llm_usage` rows. `replay._on_llm_usage` carries two
        # rules a `sum(...)` does not, and both change the number this command reconciles against:
        # it DE-DUPLICATES by `usage_id` — `engine/costs.py` appends from an outbox drain and a
        # reconcile retry, so duplicate rows are expected by design and the fold's dedup is the proof
        # — and it starts from the legacy `EV_LLM_COST` compatibility base, without which a
        # pre-ledger run reports `ledger: 0` and a residual that is entirely negative. Over-counting
        # here does not merely misprint the denominator: `residual` is the whole point of the
        # command, and this is its subtrahend.
        state = fold(store.read_all())
        ledger_total = int((state.llm_cost or {}).get("total_tokens") or 0)

    if not sp_path.exists():
        typer.echo(f"ledger total: {ledger_total or 0:,} tokens")
        typer.echo("no spans.jsonl — the ledger records totals only, so the split is unavailable.")
        raise typer.Exit(2)

    # THE SAME READER SETTINGS `timings` USES on the same file, and the `errors` one is not
    # cosmetic: `jsonlio.read_jsonl_lenient`'s own docstring names it ("the spans reader uses
    # 'replace' — a mid-file mojibake byte must cost one span, not the whole timings report").
    # Under the default "strict" a single invalid UTF-8 byte raises `UnicodeDecodeError`, which the
    # reader quarantines — so a generation span `timings` reads and charges, `tokens` dropped, and
    # its tokens came back out as `residual … unattributed by any span`, i.e. reported as a fact
    # about the ledger rather than about this command's own reader.
    rows, health = read_jsonl_lenient_with_health(sp_path, loads=_json.loads, errors="replace")
    out = token_spend_by_phase(rows, ledger_total=ledger_total)
    unreadable = int(health.get("invalid_lines") or 0)
    if not out["rows"]:
        # SAY WHAT WAS LOST BEFORE GIVING UP. A torn `spans.jsonl` — the routine shape after a
        # killed engine process — parses to zero rows, and exiting here printed neither the ledger
        # total nor the damage count, so the one message the operator got ("no generation spans
        # found") named the record rather than the reader and read identically to a run that simply
        # never traced. Both facts are already in hand at this point.
        typer.echo(f"ledger total: {ledger_total or 0:,} tokens")
        if unreadable or out["damaged"]:
            typer.echo(f"no generation spans could be read; {unreadable + out['damaged']} damaged "
                       f"span row(s) stepped over — the split is unavailable because spans.jsonl is "
                       f"unreadable, not because the run made no calls.")
        else:
            typer.echo("no generation spans found; nothing to attribute.")
        raise typer.Exit(2)

    shown = out["rows"][:top] if top and top > 0 else out["rows"]
    typer.echo(f"{'tokens':>14}  {'share':>6}  {'calls':>6}  {'prompt':>13}  {'completion':>11}  phase")
    for row in shown:
        typer.echo(f"{row['tokens']:>14,}  {100 * row['share']:>5.1f}%  {row['calls']:>6,}  "
                   f"{row['prompt']:>13,}  {row['completion']:>11,}  {row['phase']}")
    if len(shown) < len(out["rows"]):
        rest = out["rows"][len(shown):]
        typer.echo(f"{sum(r['tokens'] for r in rest):>14,}  "
                   f"{100 * sum(r['share'] for r in rest):>5.1f}%  "
                   f"{sum(r['calls'] for r in rest):>6,}  "
                   f"{'':>13}  {'':>11}  ({len(rest)} more phase(s), --top {len(shown)})")

    typer.echo("")
    typer.echo(f"attributed : {out['attributed']:>14,} tokens over {out['calls']:,} generation spans")
    if out["ledger_total"] is None:
        typer.echo("ledger     :            n/a  (no readable events.jsonl — no denominator)")
    else:
        typer.echo(f"ledger     : {out['ledger_total']:>14,} tokens (llm_usage, the durable record)")
        # SIGNED and never clamped: a retried provider call opens two spans against one billed row,
        # so a negative residual is a real state the operator should see rather than a rounding hide.
        typer.echo(f"residual   : {out['residual']:>14,} tokens "
                   f"({'spans over-attribute' if out['residual'] < 0 else 'unattributed by any span'})")

    # THE PER-CARD HALF. Phase answers "which KIND of work spent it"; this answers "which EXPERIMENT
    # spent it, and was that experiment ever evaluated" — the question a run cannot otherwise ask,
    # because the durable ledger carries no card and no node. Printed by DEFAULT rather than behind a
    # flag: the defect this closes is that nobody could see it, and an opt-in view is not seen.
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
    # `read_jsonl_lenient_with_health` returns a plain DICT, and its damage key is `invalid_lines`.
    # `getattr(health, "damaged", 0)` was therefore always 0 by two independent routes — a dict has
    # no such attribute and there is no such key — so an unreadable `spans.jsonl` (the routine shape
    # after a killed engine process leaves a torn tail) printed no damage line at all and its lost
    # tokens were reported as `residual … unattributed by any span`, i.e. as a fact about the ledger
    # rather than about the reader. `token_spend_by_phase` counts only the rows it was HANDED, so it
    # cannot see a line that never parsed; this is the half that can. (`unreadable` is resolved up
    # beside the read itself, because the zero-rows exit owes the operator the same disclosure and
    # used to return before ever reaching this line.)
    # TWO POPULATIONS, and summing them printed a false sentence about one of them. A line that
    # never parsed and a row that was not a span at all really were stepped over and contribute
    # nothing; a generation span with a torn `attributes` map is COUNTED in `calls` above and only
    # its phase and usage are lost. Reporting the sum as "stepped over" claimed the third kind had
    # been skipped while the `attributed … over N generation spans` line was already counting it.
    if out["damaged"] or unreadable:
        typer.echo(f"damaged span rows stepped over: {out['damaged'] + unreadable}")
    if out.get("torn_attributes"):
        typer.echo(f"generation spans with unreadable attributes: {out['torn_attributes']} "
                   f"(counted as calls above, attributed to no phase)")


@app.command()
def timings(run_dir: Path = typer.Argument(...),
            node: Optional[int] = typer.Option(None, help="only this node id")):
    """Where the wall-clock went: per node, then RUN-LEVEL, reconciled against the run's real duration.

    Rows are LLM generations vs eval vs repair vs tools, charged from each span's `duration_s` in
    `spans.jsonl`. The run-level section is the work no node owns — Researcher, Strategist, lesson
    passes, run report, Card-build producer. The reconciliation prints `attributed` (the rows above,
    which can exceed 100% under concurrency), `traced` (wall clock with any span open) and
    `untraced`, the honestly-unattributable remainder.

    Needs `spans.jsonl` for the breakdown; without it the run's duration is still reported, then
    exit 2. Damaged span lines are stepped over and counted.
    """
    # Two things this used to get wrong, both measured on real runs — kept here rather than in the
    # docstring, which Typer prints as `--help`:
    #
    # * It DROPPED every span with no `node_id`. That is not a rare edge — it is all the work that
    #   belongs to the RUN rather than to any one node (a Card producer's node does not exist yet,
    #   which is exactly why per-node attribution is wrong for it). This charges by the span's OWN
    #   `node_id` and deliberately does NOT follow the `build_trace` claim the trace surfaces added
    #   in 2026-08-14 (`events/traceview.py::claimed_build_traces`): the two answer different
    #   questions. "What is in this node's story" reaches the build that produced it; "who owns this
    #   wall clock" must keep charging a producer turn to the run, because it ran before the node
    #   existed and could have ended without minting one. On a preserved 27.9-minute run
    #   this hid 143 of 174 spans: the report totalled 2.7 minutes and its LLM rows 0.2 minutes
    #   against a cost ledger of 139 calls / 780k tokens.
    # * It reconciled against nothing, so nobody could see what it was still missing. The denominator
    #   is now the run's own wall clock from `events.jsonl` (`run_wall_clock_seconds` — correct
    #   across a stop-then-`finalize` process boundary, unlike `budget.elapsed_s` on an old log), and
    #   the residual is PRINTED rather than left implicit. It is real: work with no span at all,
    #   engine bookkeeping, provider waits, and the idle gap of a stopped run awaiting its wrap-up.
    #
    # `spans.jsonl` is a high-volume sidecar (the reason `events/span_index.py` exists). It is read
    # WHOLE, so peak memory tracks the file: the accelerated index is deliberately not used here
    # because building it WRITES `spans.index.jsonl`, and this command is read-only.
    import json as _json
    from collections import defaultdict

    from looplab.events.eventstore import read_jsonl_lenient_with_health
    from looplab.events.replay import run_wall_clock_seconds

    ev_path = run_dir / "events.jsonl"
    sp_path = run_dir / "spans.jsonl"
    # Deliberately NOT `_require_run_dir` (doc 25 CT-13): this is the one read command whose input is
    # EITHER file — it reported timings for a spans-only directory before the event log became its
    # denominator, and requiring `events.jsonl` would retire that. It borrows the shared `_RUN_DIR_HINT`
    # so the ADVICE is still spelled once, and keeps the shared message shape.
    if not ev_path.exists() and not sp_path.exists():
        typer.echo(f"no run found at {run_dir} (no events.jsonl or spans.jsonl). {_RUN_DIR_HINT}")
        raise typer.Exit(2)

    # The DENOMINATOR, and the one number here that does not come from spans: the log's own first-to-
    # last timestamp. Read before the spans so a run with tracing off still learns how long it took.
    wall = None
    budget_elapsed = None
    if ev_path.exists():
        store = EventStore(ev_path)
        # The event log is this command's DENOMINATOR (first->last ts). A truncated prefix makes every
        # percentage here a percentage of a run that is not the one on disk — on
        # `rubertlite-dense-retrieval` the readable prefix ends 9 days before the log's last row — so
        # the receipt is stated before any number derived from it.
        _echo_log_integrity(store, run_dir)
        events = store.read_all()
        wall = run_wall_clock_seconds(events)
        for e in events:                      # the durable receipt, for cross-check (last one wins)
            if e.type == EV_BUDGET:
                budget_elapsed = (e.data or {}).get("elapsed_s")
    if wall is not None:
        cross = ""
        # A pre-fix log's receipt measured the finalizing PROCESS, so it can be orders of magnitude
        # short. Showing both is what makes that visible instead of leaving two numbers to disagree
        # in different places. Only flagged when they actually differ by more than rounding.
        if type(budget_elapsed) in (int, float) and math.isfinite(budget_elapsed):
            cross = (f"   (budget.elapsed_s says {_minutes(float(budget_elapsed))} min)"
                     if abs(float(budget_elapsed) - wall) > 1.0
                     else "   (budget.elapsed_s agrees)")
        typer.echo(f"run wall clock {_minutes(wall)} min "
                   f"({round(wall, 1)} s, events.jsonl first -> last timestamp)" + cross)
    elif ev_path.exists():
        typer.echo("run wall clock unknown (no event carries a usable timestamp).")

    if not sp_path.exists():
        typer.echo(f"no spans.jsonl at {run_dir} (tracing off or pre-tracing run).")
        raise typer.Exit(2)

    # skip-and-continue (not iter_jsonl's stop-at-first-bad): a mid-file corrupt span line must
    # cost one span, not truncate every later span out of the report. Keep dicts_only=True (the
    # default): a valid-JSON-but-NON-dict corrupt line (e.g. a bare `123`) must be SKIPPED like any
    # other damaged line — with dicts_only=False it'd survive and the `sp.get(...)` accesses below
    # would raise AttributeError, crashing the whole command (worse than the truncation this avoids).
    # `_with_health` is the same read plus the receipt for what it stepped over: this command now
    # publishes a residual, and an unreported dropped line would be charged to the run as idle time.
    spans, health = read_jsonl_lenient_with_health(sp_path, loads=_json.loads, errors="replace")
    # An operation span's recorded duration INCLUDES every nested span (create_node ⊃ implement ⊃
    # stages/plan ⊃ the generations inside them), so summing raw durations counted the nested
    # phases twice or thrice and skewed every percentage. Charge each op its SELF time only
    # (duration minus its DIRECT children); leaf generations/tools keep their full duration.
    child_sum: dict = defaultdict(float)
    for sp in spans:
        parent = sp.get("parent_id")
        if isinstance(parent, str) and parent:   # a non-string id is damage, not a parent link
            child_sum[parent] += _span_seconds(sp.get("duration_s"))

    per_node: dict = defaultdict(lambda: defaultdict(lambda: [0.0, 0]))
    # A separate dict, NOT a sentinel node id: `-1` is a REAL node id here (the run-setup span uses
    # it), so any in-band marker would silently merge run-level work into a node's row.
    run_level: dict = defaultdict(lambda: [0.0, 0])
    attributed = 0.0
    intervals: list = []
    no_start = 0
    for sp in spans:
        raw = _span_seconds(sp.get("duration_s"))
        dur = raw
        if sp.get("kind") == "operation":
            span_id = sp.get("span_id")
            dur = max(0.0, raw - child_sum.get(span_id if isinstance(span_id, str) else "", 0.0))
        attributed += dur
        # The union needs the span's absolute placement, which is `start` (a wall clock, while
        # `duration_s` is a monotonic delta — see core/tracing.py), and its RAW duration: the span
        # was genuinely open that long, and its children's coverage is already inside that window.
        # An old/degraded span without a `start` still contributes its self time above; it just
        # cannot say WHEN, so it is counted and named rather than quietly dropped from both.
        start = _span_seconds(sp.get("start"))
        if start:
            intervals.append((start, start + raw))
        else:
            no_start += 1
        nid = (sp.get("attributes") or {}).get("node_id")
        try:
            nid = int(nid)
        except (TypeError, ValueError):
            nid = None
        if nid is None:
            if node is None:
                cell = run_level[_span_category(sp)]
                cell[0] += dur
                cell[1] += 1
            continue
        if node is not None and nid != node:
            continue
        cell = per_node[nid][_span_category(sp)]
        cell[0] += dur
        cell[1] += 1

    for nid in sorted(per_node):
        _echo_section(f"node {nid}", per_node[nid])
    if run_level:
        _echo_section("run-level", run_level,
                      note="(no node owns it: researcher / strategist / card producer / wrap-up)")

    if node is not None:
        typer.echo(f"\n(--node {node}: run-level work and the reconciliation below are run-scope "
                   f"and are omitted; drop --node to see them.)")
        return
    if health.get("invalid_lines"):
        typer.echo(f"\nspans.jsonl: {health['invalid_lines']} of {health['source_lines']} lines "
                   f"unreadable and skipped — their time falls into `untraced` below.")
    if no_start:
        typer.echo(f"spans.jsonl: {no_start} spans carry no `start` — counted in `attributed`, "
                   f"absent from `traced`.")
    if wall is None or wall <= 0:
        return
    traced = min(_traced_seconds(intervals), wall)
    untraced = max(0.0, wall - traced)
    typer.echo(f"\nreconciliation vs {_minutes(wall)} min wall clock:")
    typer.echo(f"  attributed {_minutes(attributed):>6} min  ({round(100*attributed/wall)}%)  "
               f"sum of the rows above; overlaps under concurrency")
    typer.echo(f"  traced     {_minutes(traced):>6} min  ({round(100*traced/wall)}%)  "
               f"wall clock with at least one span open")
    typer.echo(f"  untraced   {_minutes(untraced):>6} min  ({round(100*untraced/wall)}%)  "
               f"no span open — not attributable from spans.jsonl")


@app.command()
def inspect(run_dir: Path = typer.Argument(...)):
    """Show the raw launch config snapshot + the run's current folded best result.

    Seven selection-treatment settings are committed by ``run_started`` and can therefore differ from
    an old or hand-edited snapshot. The owner config API overlays those effective folded values;
    this diagnostic deliberately prints the on-disk snapshot verbatim for inspection.
    """
    snap = run_dir / "config.snapshot.json"
    events = run_dir / "events.jsonl"
    # Tolerate a run that crashed after writing config.snapshot.json but before its first event: still
    # show the config. Only error when the dir is neither — a typo'd path, not a real (if partial) run.
    if not snap.exists() and not events.exists():
        typer.echo(f"no run found at {run_dir} (no config.snapshot.json or events.jsonl).")
        raise typer.Exit(2)
    if snap.exists():
        typer.echo(snap.read_text(encoding="utf-8"))
    if events.exists():
        # This command cannot route through `_require_run_dir` (its input may be a config snapshot
        # with no event log at all), so it states the SAME receipt from the store it opens anyway —
        # `EventStore.__init__` has already scanned, so this costs nothing. Without it `inspect`
        # printed a folded "current best result" derived from a 20-record prefix of a 1,624-record
        # log and called it the run's result.
        store = EventStore(events)
        _echo_log_integrity(store, run_dir)
        all_events = store.read_all()
        state = fold(all_events)
        _print_result(state)
        # WHAT THIS RUN'S LOG CAN AND CANNOT SAY ABOUT ITS TRUST SCANS. `looplab inspect` is where
        # someone goes to ask what a run actually did, and until the `trust_scan` receipt existed the
        # honest answer here was unobtainable: a clean scan wrote nothing, so silence covered
        # "scanned, nothing found", "every detector was off" and "the call was deleted" alike. The
        # summary states the UNKNOWN bucket first for exactly that reason — every log written before
        # 2026-08-19, which is every log on this box, lands there and must not read as clean.
        typer.echo(trust_scan_summary(all_events, [n.id for n in state.evaluated_nodes()]))
        # WHAT THIS RUN'S BEST NUMBER MAY BE RANKED AGAINST. `_print_result` above prints
        # "BEST node N: metric=…" and, until 2026-08-20, nothing whatsoever about what that number
        # was measured against — which is how four recall@100 values from more than one test set came
        # to be compared out loud on this box. Stated for BOTH answers, like the trust-scan summary
        # one line up and for the identical reason: an absent key means NOBODY CAN SAY, never "the
        # same as the other runs", and a line printed only when a key exists would make its absence
        # invisible on exactly the runs where it matters most. The pairwise refusal lives in
        # `looplab comparability`, which is where an operator asks about more than one run.
        _best = state.best()
        _record = comparability_record_of(_best) if _best is not None else None
        if _record:
            _keys = " ".join(f"{name}={value}" for name, value in sorted(_record["keys"].items()))
            typer.echo(f"comparability: {_record['authority']} {_keys}")
        else:
            typer.echo(
                "comparability: UNKNOWN — this run records no key for what its metric was measured "
                "against, so its number may not be ranked against any other run's. Declare "
                "`eval.inputs` on the task (or a `comparison_contract`) to make it decidable.")


@app.command()
def readmodel(
    run_dir: Path = typer.Argument(..., help=_RUN_DIR_HINT),
    check: bool = typer.Option(
        False, "--check",
        help=("Report what the existing readmodel.sqlite covers and exit 1 unless it is current. "
              "Writes nothing."),
    ),
):
    """Rebuild (or check) a run's `readmodel.sqlite` from its event log.

    Until 2026-08-14 the read model was built at ONE moment — the end of `finalize_run` — so a run
    that was still going, or that crashed, had none at all, and a control event appended after a run
    finished left the published file behind with nothing on it saying so. This is the other half:
    the same projection, reachable on demand.

    It is safe against the engine invariants because a read model is a derived SIDECAR, not an
    event. Nothing here appends to `events.jsonl`, so invariant #1's "the engine is the sole writer
    of domain events" is untouched; and nothing in `looplab/` ever opens the database, so the
    artefact cannot become the cached derived state invariant #4 forbids — the engine keeps
    observing state only through `fold(store.read_all())`, including in the same process that just
    wrote this file. Building it for a LIVE run is therefore a read of the log and a write of a file
    the run does not consult; the watermark is what makes the resulting snapshot honest about being
    a prefix, since the run appends more the moment it is written.

    The two durable fences the engine's own writers respect are re-checked here, because this is a
    second process writing into someone else's run directory: a run whose deletion or reset is
    unresolved is refused rather than having a sidecar resurrected underneath the transaction.
    """
    # `healthy=True` on the writing path only: a mid-file divergence means the log this projection
    # would claim to cover is not one prefix, and publishing over it would be the silent-stale case
    # the watermark exists to remove. `--check` reads whatever is there and says so.
    store = _require_run_dir(run_dir, healthy=not check)
    events = store.read_all()
    path = run_dir / "readmodel.sqlite"
    want = coverage_watermark(events)

    if check:
        status = readmodel_status(path, events)
        stored = read_watermark(path)
        typer.echo(f"readmodel={path}")
        typer.echo(f"status={status}")
        typer.echo("covers=" + (f"seq<={stored.covered_seq} events={stored.event_count}"
                                if stored is not None else "unknown (no usable watermark)"))
        typer.echo("log=" + (f"seq<={want.covered_seq} events={want.event_count}"
                             if want is not None else "unknown"))
        if status != STATUS_CURRENT:
            raise typer.Exit(1)
        return

    try:
        assert_run_deletion_write_allowed(run_dir)
        assert_run_reset_write_allowed(run_dir)
    # The STORAGE errors are in the tuple deliberately: a fence that cannot be read is not an
    # absent fence, and treating it as one is how a sidecar gets written into a run whose deletion
    # is in flight. Fail closed on "cannot prove this is allowed", not only on "proven disallowed".
    except (RunDeletionFenceError, RunDeletionStorageError,
            RunResetFenceError, RunResetStorageError) as exc:
        typer.echo(f"refusing to write a read model into a fenced run: {exc}", err=True)
        raise typer.Exit(2)

    publish_readmodel(events, path)
    # Re-read the published watermark rather than echoing what was intended: the operator is told
    # what the file on disk actually says, which is the same question `--check` answers.
    stored = read_watermark(path)
    typer.echo(f"readmodel={path}")
    typer.echo("covers=" + (f"seq<={stored.covered_seq} events={stored.event_count}"
                            if stored is not None else "unknown (no usable watermark)"))


@app.command()
def tensorboard(
    run_dir: Path = typer.Argument(..., help="Run dir; its nodes/ hold each experiment's training logs."),
    port: int = typer.Option(6006, help="Port to serve on."),
    host: str = typer.Option(
        "127.0.0.1",
        help="Bind address. Defaults to localhost — TensorBoard has NO auth, so an experiment's "
             "training logs (and any secret a script printed into them) must not be exposed on all "
             "interfaces by default. Pass --host 0.0.0.0 explicitly to bind all interfaces."),
):
    """Serve TensorBoard over a run's per-node training logs — online curves for ALL metrics the
    training framework logged (loss, recall@k, grad norms, lr, …), one comparable run per experiment.
    RepoTask training scripts (e.g. PyTorch Lightning's TensorBoardLogger) write event files under each
    node's workdir; this points TensorBoard at nodes/ so every node shows up."""
    import shutil
    import subprocess
    import sys
    logdir = run_dir / "nodes"
    if not logdir.exists():
        logdir = run_dir
    exe = shutil.which("tensorboard")
    cmd = ([exe] if exe else [sys.executable, "-m", "tensorboard.main"]) + \
          ["--logdir", str(logdir), "--port", str(port), "--host", host]
    typer.echo(f"Serving TensorBoard for {run_dir} on http://{host}:{port}  (logdir={logdir})")
    try:
        subprocess.run(cmd)
    except KeyboardInterrupt:
        pass


@app.command(name="landlock-check")
def landlock_check(run_dir: Path = typer.Argument(..., help=_RUN_DIR_HINT),
                   probe: bool = typer.Option(
                       True, help="Also fork a child, apply the ruleset, and prove that a read "
                                  "inside the allow-list succeeds and one outside it is refused.")):
    """Print the KERNEL read allow-list this run would grant, and prove the ruleset applies.

    THIS IS THE VALIDATION PATH FOR `Settings.landlock`, which ships `off`. The one unretired unknown
    in the design it belongs to is whether a real GPU eval survives a Landlock ruleset at all: the
    enforcement and the +2.1 %/open cost are measured, but only on `open`/`read` microbenchmarks —
    torch, CUDA, NCCL, `/dev/nvidia*`, `/dev/shm`, `/sys/class` and the geesefs read surfaces were
    never exercised, and a ruleset missing one of those does not degrade, it refuses mid-training.

    So the procedure before anyone flips the default is: run this against a real run directory, read
    the allow-list it prints, confirm `skipped: 0`, then run ONE real eval with
    `LOOPLAB_LANDLOCK=enforce` and check it completed. The evidence that justifies the flip is that
    eval, not this command — this command only tells you the ruleset is well-formed and applies.

    **IT FAILS CLOSED WHEN IT CANNOT SEE WHAT IT IS SUPPOSED TO VERIFY.** A gate whose whole job is
    "the operator's declared mounts are in the kernel allow-list" must never print a green
    `skipped: 0` about a list it built without them: what follows a green light here is
    `LOOPLAB_LANDLOCK=enforce` on a real GPU eval, and a missing mount does not degrade — the eval
    dies mid-training on a `PermissionError` reading the dataset. It read `task.get("repo")` until
    2026-08-15, which is the repo ROOT PATH and not a spec: censused over all 46 preserved
    `runs/*/task.snapshot.json`, that key is `None` 45 times and a `str` once, and never a dict. So
    every run either had its mounts silently dropped (`rubert-dr-0807` declares two real ones —
    `/home/jovyan/data/datasets/dense-retrieval/rubertlite` and the base model — and the command
    printed 16 rules holding neither, then `skipped: 0`), or, for the `str` shape that ships in
    `examples/repo_composable_task.json`, crashed in `read_allowlist.mount_sources`.

    The spec now comes from where the ENGINE gets it — `TaskAdapter.repo_spec()`, over the same
    re-validated snapshot `resume`/`finalize` rebuild from (`existing_run=True`, so a validation rule
    added since the run started cannot refuse it) — because `engine/resources.py::_landlock_allow`
    passes exactly `self._repo_spec` and a gate that derives its input differently from the thing it
    gates is not a gate. A snapshot that is missing, unreadable or no longer a valid task is a
    `ConfigRefusal`: the answer "no mounts" and the answer "I could not look" must not print the same.

    Read-only: it derives the list from the run's own `task.snapshot.json` and touches nothing.
    """
    import os

    from looplab.adapters.tasks import load_task
    from looplab.core.errors import ConfigRefusal
    from looplab.runtime import landlock, read_allowlist

    abi = landlock.abi_version()
    typer.echo(f"landlock ABI: {abi if abi is not None else 'UNAVAILABLE'}")
    reason = landlock.unavailable_reason()
    if reason:
        typer.echo(f"unavailable: {reason}")
        raise typer.Exit(2)
    snap = run_dir / "task.snapshot.json"
    if not snap.exists():
        raise ConfigRefusal(
            f"{snap} does not exist, so this command cannot see the mounts it exists to verify. "
            "Point it at a run directory created by `looplab run --out <dir>`; do NOT set "
            "LOOPLAB_LANDLOCK=enforce on the strength of a list derived without the task.")
    try:
        # `existing_run=True` for the same reason `resume` uses it: a rule added after this run
        # started must not make its own snapshot unreadable to a read-only diagnostic.
        task = load_task(snap, existing_run=True)
    except Exception as exc:            # noqa: BLE001 — every load failure is the same refusal here
        raise ConfigRefusal(
            f"cannot rebuild the task from {snap} ({type(exc).__name__}: {exc}), so this command "
            "cannot see the mounts it exists to verify. Fix the snapshot (or the task file it came "
            "from) before trusting any allow-list — a ruleset built without a declared mount does "
            "not degrade, it refuses the read mid-eval.") from exc
    spec_fn = getattr(task, "repo_spec", None)
    spec = spec_fn() if callable(spec_fn) else None
    mounts = read_allowlist.mount_sources(spec)
    if spec is None:
        # A SEEN answer, not a blind one: a toy/dataset task has no repo spec and therefore declares
        # no mount, and the default tiers ARE the allow-list such a run would get. It says which kind
        # it read, so this line can never be mistaken for the "I could not look" case above.
        typer.echo(f"task kind {getattr(task, 'kind', '?')!r} declares no repo spec, so it declares "
                   "no mounts — showing the default tiers only")
    else:
        # Printed BEFORE the allow-list and counted, because `mounts: 0` on a repo task is the fact
        # the operator has to check against their own `data:`/`references:` block. An omission that
        # is not printed is exactly how the previous version read as a pass.
        typer.echo(f"declared mounts from task.snapshot.json ({len(mounts)}):"
                   + ("" if mounts else "  (none — this task declares no data:/references: mount)"))
        for path, mode in mounts:
            here = "" if os.path.exists(os.path.expanduser(str(path))) else "   <- NOT on this box"
            typer.echo(f"  {mode:<9} {path}{here}")
    allow = read_allowlist.derive(workdir=None, run_dir=str(run_dir), repo_spec=spec)
    typer.echo(f"allow-list ({len(allow)} rules; the node workdir is added per launch):")
    for path, mode in allow:
        typer.echo(f"  {mode:<9} {path}")
    fd, added, skipped = landlock.build_ruleset(allow)
    os.close(fd)
    typer.echo(f"added: {len(added)}   skipped: {len(skipped)}")
    for path, why in skipped:
        # A skipped rule is a DENIAL under an allow-list, never a no-op — this is the half that must
        # never be silent (211 candidate rules produced 55 accepted ones in the measurement that
        # motivated the derived list).
        typer.echo(f"  SKIPPED {path}: {why}  <- this path would be DENIED")
    if skipped:
        raise typer.Exit(1)
    if not probe:
        return
    # The probe forks so the irreversible `restrict_self` cannot touch this CLI process.
    #
    # The OUTSIDE path is the user's home DIRECTORY, chosen because it is the one path that is
    # reliably not in the list while a subpath of it (`~/.cache`) is — which also proves the rules are
    # path-BENEATH grants and not a prefix match on a string. A run directory that IS the home
    # directory would make the probe vacuous, so it says so instead of reporting a pass.
    home = os.path.realpath(os.path.expanduser("~"))
    granted = {p for p, _m in allow}
    if home in granted or home == os.path.realpath(os.sep):
        typer.echo("probe: skipped — the home directory is itself allow-listed here, so there is no "
                   "outside path to test against")
        return
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        try:
            landlock.apply(allow)
            inside = os.path.isdir(str(run_dir))
            try:
                os.listdir(home)
                denied = False
            except OSError:
                denied = True
            os.write(w, f"inside_ok={inside} outside_refused={denied} (outside={home})".encode())
        except BaseException as exc:            # noqa: BLE001 — report, never traceback from a fork
            os.write(w, f"probe failed: {type(exc).__name__}: {exc}".encode()[:500])
        finally:
            os._exit(0)
    os.close(w)
    with os.fdopen(r, "rb") as fh:
        typer.echo("probe: " + fh.read().decode("utf-8", "replace"))
    os.waitpid(pid, 0)


# --- STAGE DUPLICATION -------------------------------------------------------------------------
# The reporter half of `runtime/stage_identity.py`, and the reason that module exists at all. It
# answers the two questions nobody could answer without a 20-minute sha256 sweep over 20 GB of
# preserved workdirs — which is why the wrong reuse key (the node's DECLARED mining params, 4 wrong
# of 7 on this corpus) looked obviously right and shipped nowhere near a measurement.
#
#   1. DUPLICATED WORK — stages whose recorded output identity is byte-identical. An OBSERVED fact
#      about what was produced, never a prediction from what a node declared about itself.
#   2. WOULD-BE REUSE — stages whose recorded input KEY is equal, split into the ones whose outputs
#      then agreed (a correct hit a cache would have made) and the ones whose outputs DID NOT (a
#      WRONG hit). The second number is the one that decides whether a cache may ever ship, and it
#      is deliberately reported even when it is zero, because "no wrong hits over N candidates" and
#      "no candidates" are different states with the same headline.
#
# Read-only, no model, no fold of anything but `stage_finished`. A row from before this instrument
# shipped carries neither field and is counted as UNKEYED rather than as evidence either way.
def _stage_identity_rows(store) -> list:
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


def _output_fingerprint(outputs) -> Optional[str]:
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


@app.command(name="repair-candidates")
def repair_candidates(run_dir: Path = typer.Argument(..., help=_RUN_DIR_HINT),
                      show_nodes: bool = typer.Option(
                          False, "--nodes", help="List the node ids behind each row.")):
    """Which files this run's nodes had to fix, ranked by how many DISTINCT nodes fixed them.

    THE LEDGER EXISTED AND NOTHING SHOWED IT. `RunState.repair_candidates()` has been derived on
    every fold since the repair ledger landed and had no CLI, no route and no panel — so the one
    question it answers ("what belongs in the source repo rather than in one node?") could only be
    asked by opening a Python REPL over the event log.

    Why DISTINCT NODES and not repair count, in the model's own words: one node repairing the same
    file four times is one discovery, and four nodes repairing it once each is a property of the
    repo. Measured by running this command on `runs/e5small-dr-unified-v4`: SIX nodes repaired
    `looplab_stages.json` and FIVE repaired `vectorsearch/configs/config.yaml`, four of those for
    `oom`, across 19 repair rows. A node inherits its PARENT's files
    and can never inherit a fix a SIBLING found, because a node becomes a parent only by winning on
    metric, so the same fix is paid for again on every branch.

    It RANKS and decides nothing, exactly as the model says. Promoting a fix into the source repo
    moves the substrate every later node is measured on: that is the operator's call, and it has to
    be recorded as an event or the comparability key cannot tell nodes on either side of it apart.
    """
    store = _require_run_dir(run_dir)
    _echo_log_integrity(store, run_dir)
    state = fold(store.read_all())
    rows = state.repair_candidates()
    if not rows:
        # Two very different facts, and an operator acting on this must not confuse them: a run
        # whose nodes never needed a repair, and a log written before the ledger existed.
        typer.echo("no repaired files recorded in this run"
                   + ("." if state.repair_ledger else
                      " (and the repair ledger is empty — a pre-ledger log records none)."))
        return
    typer.echo(f"repaired files: {len(rows)}  repair rows: {len(state.repair_ledger)}")
    for row in rows:
        reasons = ", ".join(f"{k}x{v}" for k, v in row["reasons"].items())
        typer.echo(f"  {row['node_count']:>2} node(s)  {row['path']}"
                   + (f"  [{reasons}]" if reasons else ""))
        if show_nodes and row["nodes"]:
            typer.echo("       nodes: " + ", ".join(str(n) for n in row["nodes"]))
    top = rows[0]
    if top["node_count"] >= 2:
        typer.echo("")
        typer.echo(f"{top['node_count']} separate experiments fixed {top['path']}. A fix rediscovered "
                   "per branch is one the repo owes them; promoting it is an operator decision and "
                   "must be recorded so later comparisons know which side of it a node ran on.")


@app.command(name="stage-dups")
def stage_dups(run_dir: Path = typer.Argument(..., help=_RUN_DIR_HINT)):
    """Duplicated stage work in a run, and what a cross-node reuse key would have done (read-only).

    Two independent reports over the same rows, because they answer different questions and the
    corpus that motivated this had them disagree: the DECLARED-parameter grouping that looked like
    12 duplicated `mine` stages on `rubertlite-dr-unified-v8` is 4 groups of provably-identical
    output, and a key built on that declaration would have been wrong 4 times in 7.
    """
    store = _require_run_dir(run_dir)
    _echo_log_integrity(store, run_dir)
    rows = _stage_identity_rows(store)
    if not rows:
        typer.echo("no stage rows in this run.")
        return
    done = [r for r in rows if r["status"] == "ok"]
    keyed = [r for r in done if r["key"]]
    typer.echo(f"stage rows: {len(rows)}  completed: {len(done)}  with an input key: {len(keyed)}")
    if len(keyed) < len(done):
        reasons: dict = {}
        for r in done:
            if not r["key"]:
                reasons[r["key_reason"] or "unrecorded"] = reasons.get(r["key_reason"] or
                                                                       "unrecorded", 0) + 1
        typer.echo("  unkeyed: " + ", ".join(f"{k}={v}" for k, v in sorted(reasons.items())))

    # 1. OBSERVED duplication — identical output identity, whatever anybody declared.
    by_fp: dict = {}
    for r in done:
        fp = _output_fingerprint(r["outputs"])
        if fp:
            by_fp.setdefault((r["name"], fp), []).append(r)
    dup_seconds = 0.0
    typer.echo("\nidentical outputs (observed, not predicted):")
    for (name, _fp), group in sorted(by_fp.items(), key=lambda kv: -len(kv[1])):
        if len(group) < 2:
            continue
        # The FIRST one is work that had to happen; every later one is the duplication.
        repeat = sum(g["seconds"] for g in group[1:])
        dup_seconds += repeat
        nodes = ", ".join(str(g["node"]) for g in group)
        typer.echo(f"  {name}: {len(group)} stages produced the same bytes "
                   f"(nodes {nodes}) — {repeat / 3600.0:.2f} h after the first")
    if dup_seconds <= 0:
        typer.echo("  none.")
    else:
        typer.echo(f"  total duplicated: {dup_seconds / 3600.0:.2f} h")

    # 2. WHAT THE KEY WOULD HAVE DONE. First-wins, exactly as a cache would: the earliest completed
    # stage under a key is the source, everything after it is a candidate hit.
    seen: dict = {}
    hits = wrong = 0
    saved = 0.0
    for r in keyed:
        src = seen.get(r["key"])
        if src is None:
            seen[r["key"]] = r
            continue
        hits += 1
        saved += r["seconds"]
        a, b = _output_fingerprint(src["outputs"]), _output_fingerprint(r["outputs"])
        if a is None or b is None or a != b:
            wrong += 1
            typer.echo(f"  WRONG HIT: {r['name']} node {r['node']} would have reused node "
                       f"{src['node']}'s artifact, which is NOT what it produced")
    typer.echo(f"\ncross-node reuse key: {hits} candidate hit(s), {wrong} of them WRONG, "
               f"{saved / 3600.0:.2f} h")
    typer.echo("  a wrong hit is a stale artifact silently feeding the next stage — the number "
               "that decides whether a cache may ship, not the hours.")


@app.command(name="parser-stats")
def parser_stats(run_dir: Path = typer.Argument(..., help="A run directory (holds spans.jsonl)."),
                 as_json: bool = typer.Option(False, "--json", help="Emit the tally as JSON.")):
    """How the structured-output parser actually behaved on THIS box, per role.

    `core/parse.py::parse_structured` walks a fallback order (`tool_call` -> `baml`), and a failure
    of the first parser used to be silent: the caller gets a validated object either way, so a
    native function-call collapse that a SECOND provider call rescued left no trace anywhere. This
    reads the `structured_parse` observations and answers the three questions that decide
    `Settings.llm_parser`:

      * how often the FIRST parser answered (`attempts == 1`) — the number
        `docs/BACKLOG.md` H2 quotes at ~20% for native FC on small models, from a different
        deployment and from before H1's `guided_json` shipped;
      * how often the winner only validated after schema-aligned REPAIR (`repaired`) — a native call
        that nearly collapsed is not a clean win, and counting it as one hides the signal;
      * how often the whole walk failed.

    A default that touches every model call in the system is not a coin to flip on someone else's
    benchmark. This is how the flip earns its evidence here.
    """
    import json as _json
    from collections import defaultdict

    spans = Path(run_dir) / "spans.jsonl"
    if not spans.is_file():
        message = f"{spans}: no spans.jsonl — nothing to tally"
        typer.echo(_json.dumps({"error": message}) if as_json else message)
        raise typer.Exit(2)

    tally: dict = defaultdict(lambda: {"asks": 0, "first_try": 0, "repaired": 0, "failed": 0,
                                       "won": defaultdict(int), "damaged": 0})
    damaged = 0
    for line in spans.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            row = _json.loads(line)
        except ValueError:
            damaged += 1
            continue
        if not isinstance(row, dict) or row.get("name") != "structured_parse":
            continue
        attrs = row.get("attributes") if isinstance(row.get("attributes"), dict) else {}
        # The PHASE is the role: a Researcher's ask and a Developer's ask hit different models and a
        # single run-wide number would average them into something no operator can act on.
        bucket = tally[str(attrs.get("phase") or "run")]
        bucket["asks"] += 1
        if attrs.get("failed"):
            bucket["failed"] += 1
            continue
        if attrs.get("attempts") == 1:
            bucket["first_try"] += 1
        if attrs.get("repaired"):
            bucket["repaired"] += 1
        bucket["won"][str(attrs.get("parser_used") or "?")] += 1

    out = {phase: {**row, "won": dict(row["won"])} for phase, row in sorted(tally.items())}
    if as_json:
        typer.echo(_json.dumps({"run": str(run_dir), "damaged_lines": damaged, "phases": out},
                               indent=2))
        raise typer.Exit(0)
    if not out:
        typer.echo(f"{run_dir}: no structured_parse observations "
                   "(a run recorded before 2026-08-19, or nothing was traced)")
        raise typer.Exit(2)
    typer.echo(f"{run_dir}: structured-output parser, per phase")
    for phase, row in out.items():
        asks = row["asks"] or 1
        typer.echo(f"  {phase:<22} asks {row['asks']:>5}   first-try "
                   f"{100.0 * row['first_try'] / asks:5.1f}%   repaired "
                   f"{100.0 * row['repaired'] / asks:5.1f}%   failed {row['failed']:>4}"
                   f"   won: {', '.join(f'{k}={v}' for k, v in sorted(row['won'].items()))}")
    if damaged:
        typer.echo(f"  ({damaged} damaged span line(s) stepped over)")


@app.command()
def comparability(
    run_dirs: list[Path] = typer.Argument(..., help="One or more run directories to compare."),
):
    """What each run's champion number MAY be ranked against — and a refusal when they may not.

    THE COMMAND THIS BOX NEEDED. `runs/` holds recall@100 values of 0.8776, 0.793426, 0.792082 and
    0.774207 and they have been compared out loud all day. Some were measured on one test set and
    some on another; some against one product index and some against a bigger one, which makes
    recall@100 strictly harder. Nothing in any record said which, and no surface refused. This one
    does, and it exits NON-ZERO when it refuses so a script cannot ignore it.

    Three answers, and the middle one is the whole point:

      SAME      — the runs recorded the same comparability key at an authority that may certify it
                  (`measured`: the eval's declared inputs bound to their content digests; or
                  `declared`: an operator-written `ComparisonContract`). Ranking them is a fact.
      DIFFERENT — they recorded provably different keys. **REFUSED**, exit 3. The values are each
                  true of their own measurement; the ordering between them never was.
      UNKNOWN   — at least one recorded no key, or they agree only at the `inferred` authority
                  (two task files that merely look alike, which is exactly what the four values
                  above are). NOT an assent. Exit 4, because a caller that wanted a ranking did not
                  get one, and the one thing this command may never do is let silence read as yes.

    Read-only: it folds each log and prints. It writes nothing and touches no memory store.
    """
    from looplab.engine.comparability import (
        DIFFERENT, SAME, UNKNOWN, comparability_notice, comparability_status, record_of)

    rows = []
    for run_dir in run_dirs:
        events = run_dir / "events.jsonl"
        if not events.exists():
            typer.echo(f"{run_dir.name}: no event log — nothing to read a key from.")
            rows.append((run_dir.name, None, None))
            continue
        state = fold(EventStore(events).read_all())
        best = state.best()
        record = record_of(best) if best is not None else None
        rows.append((run_dir.name, best, record))
        if record is None:
            # NAME THE FIX, not just the state. `not_declared` is the state every run on this box is
            # in, so an operator reading this line needs the one edit that changes it — the same rule
            # `metric_subject.UNBOUND_MESSAGES` follows for the subject side.
            typer.echo(f"{run_dir.name}: metric="
                       f"{'—' if best is None else best.robust_metric} "
                       "comparability=UNKNOWN (no key recorded; declare `eval.inputs` on the task, "
                       "or a `comparison_contract`, so what this number was measured against is on "
                       "the record).")
        else:
            keys = ", ".join(f"{name}={value}" for name, value in sorted(record["keys"].items()))
            typer.echo(f"{run_dir.name}: metric="
                       f"{'—' if best is None else best.robust_metric} "
                       f"authority={record['authority']} {keys}")

    if len(rows) < 2:
        return
    # PAIRWISE, and every pair is stated. A single "these runs are comparable" verdict would hide
    # which pair failed, and on a portfolio the operator's next question is always WHICH.
    worst = SAME
    for index, (name, _best, record) in enumerate(rows):
        for other_name, _other_best, other_record in rows[index + 1:]:
            status = comparability_status(record, other_record)
            if status == SAME:
                typer.echo(f"  {name} vs {other_name}: SAME evaluation — ranking these is a fact.")
                continue
            worst = DIFFERENT if (status == DIFFERENT or worst == DIFFERENT) else UNKNOWN
            typer.echo(f"  {name} vs {other_name}: {status.upper()} — "
                       + comparability_notice(record, other_record, other_run_id=other_name))
    if worst == DIFFERENT:
        raise typer.Exit(3)
    if worst == UNKNOWN:
        raise typer.Exit(4)
