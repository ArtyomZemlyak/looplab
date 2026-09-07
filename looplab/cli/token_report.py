"""The per-CARD and per-BUILD token tables `looplab tokens` prints under its phase table.

EXTRACTED, not banked. `cli/inspect_cmds.py` carries a line ceiling
(`tests/test_cli_command_groups.py::test_no_group_is_a_god_module_again`) whose own docstring names
this as the unit to move when the cap is next reached — "the `tokens` command's rendering half (the
per-card / per-build table echoes) is a coherent unit that can move beside `events/token_spend.py`'s
pure folds into a cli-side helper module" — and the alternative it names, raising the cap a fourth
time, is a cap that has stopped being consulted. This is that move: the caller keeps the reading of
`spans.jsonl`, the phase table and the ledger reconciliation; this file keeps the two tables that
answer "which EXPERIMENT spent it".

It is a RENDERER over folds that already exist (`events/token_spend.py`) plus one read of the event
log for the build windows, and it decides nothing: no metric, no champion, no selection. The
comments below are the originals, moved verbatim — they carry the measurements that shaped each
line, and a move that paraphrased them would cost exactly what CLAUDE.md's "comments are
load-bearing" rule is about.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from looplab.events.eventstore import EventStore


def echo_card_and_build_tables(rows, *, state, ev_path: Path, ledger_total: Optional[int],
                               by_card_fold, by_build_fold, unattributed: str) -> None:
    """Print the per-card table and, under it, the two prices a card table alone cannot show.

    The folds and the `(no card)` sentinel travel as ARGUMENTS rather than imports so this module
    and its caller cannot end up reading two different `events/token_spend.py` surfaces — the caller
    resolved them for the phase table one screen up, and `ledger_total` is the same denominator that
    table was reconciled against.
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
    by_card = by_card_fold(rows, card_nodes=card_nodes, ledger_total=ledger_total)
    real = [r for r in by_card["rows"] if r["card"] != unattributed]
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
        by_build = by_build_fold(rows, builds)
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
