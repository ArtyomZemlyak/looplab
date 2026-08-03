"""The BELIEF view over the card board — a derived projection, not folded state (doc 25 CO-11).

Lived on `RunState` until this move, which put a 60-line pure projection inside the repo's largest
module and inside the model layer, where nothing else derived is. It reads `RunState` and returns
plain dicts, exactly like the other views in this package.

Kept computation-identical across the move. In particular the verdict roll-up stays a per-member
LABEL MAX rather than a call to `_evidence_verdict` over the unioned evidence: those agree (each
node's supported/pending/evaluated class is intrinsic, so the predicates are OR-composable across
members and the precedence-max reproduces the union recompute), and the label max is what the
existing tests pin. What changed is only that the choice is now a choice — on `RunState` it was
forced, because calling the helper would have crossed the core -> events layer the wrong way.
"""
from __future__ import annotations

from looplab.core.models import RunState, hypothesis_id, hypothesis_statement_digest


def grouped_beliefs(st: RunState) -> list[dict]:
    """Additive BELIEF projection (peer review): the board keeps ``1 card = 1 work item`` — two
    native actions that reuse the exact hypothesis wording stay DISTINCT cards — but they are ONE
    belief, and the removed statement-keyed ledger read their evidence together. Group the research
    cards by their immutable seed-statement DIGEST so a consumer (verdicts / lessons / a belief view)
    can read a belief's evidence and verdict as a whole instead of fragmented across work items.

    Pure, deterministic, and strictly additive: it NEVER mutates a card, a per-card verdict, or any
    folded field — the card identities and their own verdicts are untouched. Each group is
    ``{seed_hash, seed_digest, seed_statement, card_ids, evidence, statements, verdict}`` in
    first-seen order, with ``evidence`` the ordered union of the NON-ABANDONED members' evidence and
    ``verdict`` a strength roll-up of the members' own verdicts (supported > testing > tested > open,
    matching ``_evidence_verdict``'s precedence on that same non-abandoned evidence; ``abandoned``
    only when EVERY member is abandoned). `card_ids` still lists every member (abandoned included)."""
    # Precedence == `_evidence_verdict`'s status order: supported > testing > tested > open (peer
    # review — `testing` OUTRANKS `tested`, so a still-running experiment is NOT hidden by a
    # finished-no-improve sibling). A per-card-label max is EXACTLY the union recompute here: each
    # node's supported/pending/evaluated class is intrinsic (independent of how cards are grouped),
    # so those predicates are OR-composable across members and the precedence-max reproduces
    # `_evidence_verdict(union)` without crossing the core→events layer to call the helper.
    _RANK = {"supported": 4, "testing": 3, "tested": 2, "open": 1, "abandoned": 0}
    groups: "dict[str, dict]" = {}
    order: list[str] = []
    for card in st.research_cards():
        seed = (card.seed_statement or "").strip()
        if not seed:
            continue
        # Key by the FULL normalized-statement digest, not the short `hypothesis_id` (peer review):
        # two distinct statements can share a short id (test_short_hash_collision_*), and keying on it
        # would silently merge unrelated beliefs + their evidence. The short hash rides only as a
        # display alias.
        key = hypothesis_statement_digest(seed)
        group = groups.get(key)
        if group is None:
            group = {"seed_hash": hypothesis_id(seed), "seed_digest": key, "seed_statement": seed,
                     "card_ids": [], "evidence": [], "statements": [], "_verdicts": []}
            groups[key] = group
            order.append(key)
        group["card_ids"].append(card.id)
        verdict = card.verdict or "open"
        # Union evidence from NON-abandoned members only, so `evidence` and `verdict` describe the
        # SAME (live) set (peer review): the verdict roll-up drops an abandoned member's stance, so
        # folding its evidence would make the two disagree — and the label-max == _evidence_verdict
        # equivalence holds only over the non-abandoned members whose labels the roll-up keeps.
        if verdict != "abandoned":
            for node_id in (card.evidence or []):
                if node_id not in group["evidence"]:
                    group["evidence"].append(node_id)
        statement = (card.statement or "").strip()
        if statement and statement not in group["statements"]:
            group["statements"].append(statement)
        group["_verdicts"].append(verdict)
    out: list[dict] = []
    for key in order:
        group = groups[key]
        verdicts = group.pop("_verdicts")
        non_abandoned = [v for v in verdicts if v != "abandoned"]
        group["verdict"] = (max(non_abandoned, key=lambda v: _RANK.get(v, 1))
                            if non_abandoned else "abandoned")
        out.append(group)
    return out
