"""The CARD LADDER instrument, and the one TRIGGER the undercut rule is waiting on.

Four propagation rules govern the direction -> experiment ladder (docs/BACKLOG.md). Three of them
already hold: support does not flow up and refutation does not flow up (by construction — each
card's verdict is `card_ledger.py::_evidence_verdict` over its OWN evidence and nothing else), and a
broad claim has no verdict of its own but a TALLY of its children (`core/cards.py::card_child_rollup`).
Rule 3 — refutation flows DOWN as UNDERCUT, never as refutation — is the unbuilt one, and it was
deferred for a measured reason rather than a stylistic one: folding every `runs/**/events.jsonl` on
2026-08-26 found 691 cards, a ladder depth histogram of `{0: 690, 1: 1}` and ZERO parents carrying
own-level evidence. On that corpus no card can reach the state rule 3 fires from, so building the
derivation would ship machinery with zero possible firings.

THE TRIGGER WAS STATED SO NOBODY HAS TO RE-DERIVE IT, and this module evaluates exactly that
sentence over a real runs root: **build rule 3 when a fold produces a card with BOTH
`child_card_ids` and a non-empty `evidence`.** Nothing here implements the undercut rule, marks a
card, or changes a verdict — it answers "is it time yet?" with the corpus's own numbers, so the
question stops needing a hand re-count.

Why both halves are load-bearing, restated on the predicate rather than left in the entry: a card
with children but no evidence of its own is a research DIRECTION nobody ran an experiment against
at its own generality, so there is nothing to refute and nothing to push down; a card with evidence
but no children has nobody to undercut. Only the conjunction is a parent whose own experiments can
fail underneath a family that inherits the doubt.

Pure and deterministic (engine invariant 5): it reads ALREADY FOLDED `RunState`s — one `fold` per
run, the caller's — does no I/O, calls no model, and imports only `core`. Depth is walked over
`Card.parent_card_id`, which `card_ledger.py::_apply_card_lineage` already guarantees is a forest
(it refuses an edge that would close a cycle); the walk is nevertheless bounded by
`CARD_LINEAGE_MAX_DEPTH` and by a seen-set, because a hand-edited or future log folds through the
same code and an instrument must not hang on one.
"""
from __future__ import annotations

from typing import Iterable

from looplab.core.cards import CARD_LINEAGE_MAX_DEPTH
from looplab.core.run_identity import run_ref

# The sentence the backlog entry wrote down, kept in ONE place and printed with every report — a
# trigger a reader has to reconstruct from a table of counts is a trigger that gets re-litigated.
UNDERCUT_TRIGGER_RULE = (
    "build the undercut rule (refutation flows DOWN) when a fold produces a card with BOTH "
    "`child_card_ids` and a non-empty `evidence` — a parent whose own experiments can fail while "
    "children hang off it. Neither half alone is that state.")

_STATEMENT_CHARS = 160
_CHILDREN_SHOWN = 8
_EVIDENCE_SHOWN = 12


def _depth(cid: str, cards: dict) -> int:
    """How many parent edges separate this card from its root, bounded and cycle-proof."""
    depth, seen, cursor = 0, {cid}, cards.get(cid)
    while depth <= CARD_LINEAGE_MAX_DEPTH:
        parent = getattr(cursor, "parent_card_id", None) if cursor is not None else None
        if not isinstance(parent, str) or parent not in cards or parent in seen:
            return depth
        seen.add(parent)
        cursor = cards[parent]
        depth += 1
    return depth


def _evidence(card) -> list[int]:
    return [int(n) for n in (getattr(card, "evidence", None) or [])
            if isinstance(n, int) and not isinstance(n, bool)]


def _children(card) -> list[str]:
    return [str(k) for k in (getattr(card, "child_card_ids", None) or []) if isinstance(k, str) and k]


def card_ladder_report(runs: Iterable[tuple[str, object]]) -> dict:
    """Ladder shape + the undercut trigger over a corpus of `(label, RunState)` pairs.

    The caller owns the fold and the walk of the runs root; this module never opens a file.
    """
    histogram: dict[str, int] = {}
    fired: list[dict] = []
    per_run: list[dict] = []
    runs_seen = cards_seen = edges = parents = parents_without_evidence = 0
    max_depth = 0

    for label, st in runs:
        runs_seen += 1
        cards = dict(getattr(st, "cards", None) or {})
        ref = run_ref(getattr(st, "run_uid", "") or "", getattr(st, "run_id", "") or str(label))
        run_edges = run_parents = run_trigger = 0
        run_max_depth = 0
        for cid, card in sorted(cards.items()):
            cards_seen += 1
            depth = _depth(str(cid), cards)
            histogram[str(depth)] = histogram.get(str(depth), 0) + 1
            max_depth = max(max_depth, depth)
            run_max_depth = max(run_max_depth, depth)
            parent = getattr(card, "parent_card_id", None)
            if isinstance(parent, str) and parent in cards:
                edges += 1
                run_edges += 1
            kids = _children(card)
            if not kids:
                continue
            parents += 1
            run_parents += 1
            evidence = _evidence(card)
            if not evidence:
                # A direction with children and no own-level experiments — the state the whole corpus
                # was in when the rule was deferred. Counted so "no parent carries evidence" is a
                # number rather than the absence of a row.
                parents_without_evidence += 1
                continue
            run_trigger += 1
            fired.append({
                "run": str(label), "run_ref": ref, "card_id": str(cid),
                "statement": str(getattr(card, "seed_statement", "")
                                 or getattr(card, "statement", "") or "")[:_STATEMENT_CHARS],
                "card_kind": str(getattr(card, "card_kind", "") or ""),
                "verdict": str(getattr(card, "verdict", "") or ""),
                "status": str(getattr(card, "status", "") or ""),
                "children": len(kids), "child_card_ids": kids[:_CHILDREN_SHOWN],
                "evidence_nodes": len(evidence), "evidence": evidence[:_EVIDENCE_SHOWN],
                "depth": _depth(str(cid), cards),
            })
        per_run.append({
            "run": str(label), "run_ref": ref, "cards": len(cards), "edges": run_edges,
            "parents": run_parents, "max_depth": run_max_depth, "trigger_cards": run_trigger,
        })

    return {
        "rule": UNDERCUT_TRIGGER_RULE,
        "runs": runs_seen, "cards": cards_seen, "edges": edges, "parents": parents,
        "parents_without_own_evidence": parents_without_evidence,
        "max_depth": max_depth,
        "depth_histogram": {k: histogram[k] for k in sorted(histogram, key=int)},
        "trigger": {
            "rule": UNDERCUT_TRIGGER_RULE,
            # FIRED means the corpus can now reach the state rule 3 acts on — never that the rule
            # should be written a particular way. The design decision stays with the operator.
            "fired": bool(fired), "cards": len(fired), "rows": fired,
        },
        "runs_detail": per_run,
    }
