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


def echo_edit_types(state) -> None:
    """`looplab edit-types`' whole body: classify every parent->child diff and print the tally.

    Extracted 2026-09-08 for the same cap this module was created under
    (`tests/test_cli_command_groups.py::test_no_group_is_a_god_module_again`), when
    `workspace-bytes` needed room in `inspect_cmds.py` — an extraction, which is the norm that
    guard's own docstring states, rather than the fourth raise it refused. Moved VERBATIM: the
    command keeps its docstring (which the CLI reference is written against) and its fold, and
    every why-comment below is the one it shipped with.
    """
    from looplab.tools.node_diff import (EDIT_TYPES, classify_edits, node_record,
                                         reintroduced_lines)

    minimize = str(getattr(state, "direction", "min") or "min").lower() != "max"
    pairs = []
    unreadable = 0
    for node_id in sorted(state.nodes):
        node = state.nodes[node_id]
        parents = [p for p in (getattr(node, "parent_ids", None) or ())
                   if isinstance(p, int) and not isinstance(p, bool) and p in state.nodes]
        if not parents:
            continue
        left, right = node_record(state, parents[0]), node_record(state, node_id)
        if left is None or right is None:
            continue
        counts = classify_edits(left, right)
        if not counts["recoverable"]:
            unreadable += 1        # a missing file set is not a pair that made no edit
            continue
        if not counts["files"]:
            continue
        parent_metric, metric = state.nodes[parents[0]].metric, node.metric
        gain = None
        if isinstance(parent_metric, (int, float)) and isinstance(metric, (int, float)):
            gain = (parent_metric - metric) if minimize else (metric - parent_metric)
        pairs.append({"node": node_id, "parent": parents[0], "counts": counts, "gain": gain})
    if not pairs:
        typer.echo("no parent->child pair in this run carries two comparable file sets — "
                   "nothing to classify. (A run of seeds only has no edits to measure.)")
        return

    scored = [p for p in pairs if p["gain"] is not None]
    typer.echo(f"edit types over {len(pairs)} parent->child pair(s), {len(scored)} with both "
               f"metrics (direction={'min' if minimize else 'max'})")
    if unreadable:
        typer.echo(f"  {unreadable} pair(s) NOT classified — a file set is missing from the "
                   "record, which is not the same as a pair that changed nothing.")
    typer.echo(f"{'type':<16}{'+lines':>8}{'-lines':>8}{'pairs':>7}{'improved':>10}{'mean gain':>13}")
    for kind in EDIT_TYPES:
        added = sum(p["counts"]["added"].get(kind, 0) for p in pairs)
        removed = sum(p["counts"]["removed"].get(kind, 0) for p in pairs)
        if not added and not removed:
            continue
        # A pair is COUNTED under every type it touches, and that is the honest reading: a diff that
        # changes a hyperparameter and a control-flow line is evidence about both, and splitting the
        # gain between them would invent an attribution the record cannot support.
        touching = [p for p in scored
                    if p["counts"]["added"].get(kind) or p["counts"]["removed"].get(kind)]
        improved = sum(1 for p in touching if p["gain"] > 0)
        mean = (sum(p["gain"] for p in touching) / len(touching)) if touching else None
        typer.echo(f"{kind:<16}{added:>8}{removed:>8}{len(touching):>7}"
                   f"{(str(improved) + '/' + str(len(touching))) if touching else '—':>10}"
                   f"{(f'{mean:+.6g}' if mean is not None else '—'):>13}")
    typer.echo("  a pair is counted under EVERY type its diff touches; the gain is the pair's, not "
               "the type's share of it — this ranks kinds, it does not attribute a metric to one.")

    total_added = total_back = 0
    cycling = []
    for pair in pairs:
        cycle = reintroduced_lines(state, pair["node"])
        total_added += cycle["added"]
        total_back += cycle["count"]
        if cycle["count"]:
            cycling.append((pair["node"], cycle))
    share = (100.0 * total_back / total_added) if total_added else 0.0
    typer.echo(f"\nre-introduced lines: {total_back} of {total_added} substantive added lines "
               f"({share:.0f}%) had already been deleted earlier in the same lineage")
    for node_id, cycle in cycling[:8]:
        node_share = 100.0 * cycle["count"] / max(cycle["added"], 1)
        typer.echo(f"  node {node_id}: {cycle['count']}/{cycle['added']} ({node_share:.0f}%), "
                   f"lineage depth {cycle['depth']}")
        for example in cycle["examples"][:2]:
            typer.echo(f"      {example['path']}: {example['line'][:100]}  "
                       f"(deleted at {example['deleted_between']})")
    if not cycling:
        typer.echo("  none — no line this run added had been deleted by its own ancestry.")
