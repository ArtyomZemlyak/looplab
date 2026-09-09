"""The concept-AUTHORSHIP instrument: how much of what the proposer said a node was about survived
the classifier's own answer.

THE RECORD THIS READS EXISTS BECAUSE THE CLASSIFIER REWRITES. `_on_node_concepts` REPLACES a node's
membership — it never merges — so a proposer-authored `idea.concepts` set is gone from every read
surface the moment a classifier row lands, and `events/digest.py::_folded_axes` (rightly) forbids
resurrecting the frozen authoring into any axis. Measured by hand on the v8 run `docs/BACKLOG.md`
records, before this record existed: **2 of 24 authored ids survive into their own node's row
(91.7 % replaced)**, node 0's authored set and its folded set share nothing, and node 3's
exactly-curated `regularization/r-drop` was replaced by the invented `regularization/rdrop`.

The replacement is DESIGNED — the proposer must not certify its own taxonomy, and the authored
regime was measured self-confirming — so this instrument judges nothing. It makes that hand
measurement a command: which nodes' authored ids survived, which were replaced, and by whom.
`RunState.node_concepts_authored` is the fold record it reads; a run whose log predates that record
answers `authored: 0`, which is the honest reading of a log that never carried the claim rather than
"nothing was authored".

BOTH SIDES ARE RESOLVED THROUGH THE CONSOLIDATION RENAME MAP before they are compared, and that is
the only interpretation this module applies. A rename is retroactive and run-wide (the fold applies
it backwards to every stored membership), so an authored id that a later merge renamed and a folded
id under its new spelling ARE the same concept — comparing raw strings would report a replacement
that consolidation, not the classifier, performed. Everything else is set arithmetic: no model, no
I/O, no fold change.
"""
from __future__ import annotations

from typing import Any, Optional

from looplab.core.concepts import resolve_concept_set
from looplab.core.models import NODE_CONCEPT_PROVENANCE_AUTHORED, authored_node_concepts


def _resolved(values, renames: dict) -> set:
    """One side of the comparison, canonicalized through the run's rename map.

    `core.concepts.resolve_concept_set` and not a local loop over a one-hop dictionary lookup: a
    rename is a bounded CHAIN, the id normalization is the same one every membership went through,
    and a fifth spelling of that resolution is the drift shape doc 25 §0.8 measured. Its reason
    envelope is dropped on purpose — a malformed or cyclic id simply is not in either set here, and
    this instrument reports arithmetic over what resolved, never a receipt.
    """
    return set(resolve_concept_set(list(values or []), renames)[0])


def concept_authorship_report(state: Any, *, limit: int = 0) -> dict:
    """Per-node and run-level authorship survival for one folded `RunState`.

    Shape::

        {"nodes": [{node_id, provenance, authored: [...], folded: [...],
                    survived: [...], replaced: [...]}],
         "authored_nodes": int,      # nodes whose proposer authored a full set
         "reclassified_nodes": int,  # …of those, the ones another producer now owns
         "authored_ids": int, "survived_ids": int,
         "survival_rate": float | None,   # None when nothing was authored — never 0.0, which is
                                          # the claim "everything the proposer said was replaced"
         "by_producer": {provenance: nodes}}

    `limit` bounds only the per-node LIST; every count is over the whole run, so a truncated report
    still states the true totals (`card_ledger.py::_apply_card_lineage`'s rule for a clipped parent).
    """
    renames = getattr(state, "concept_consolidation", None)
    renames = renames if isinstance(renames, dict) else {}
    memberships = getattr(state, "node_concepts", None) or {}
    provenance = getattr(state, "node_concept_provenance", None) or {}
    authored_map = getattr(state, "node_concepts_authored", None) or {}

    rows: list[dict] = []
    authored_ids = survived_ids = reclassified = 0
    by_producer: dict[str, int] = {}
    for node_id in sorted(authored_map):
        authored = _resolved(authored_node_concepts(state, node_id), renames)
        folded = _resolved(memberships.get(node_id), renames)
        owner = str(provenance.get(node_id) or "")
        survived = authored & folded
        authored_ids += len(authored)
        survived_ids += len(survived)
        by_producer[owner] = by_producer.get(owner, 0) + 1
        if owner and owner != NODE_CONCEPT_PROVENANCE_AUTHORED:
            reclassified += 1
        rows.append({"node_id": node_id, "provenance": owner,
                     "authored": sorted(authored), "folded": sorted(folded),
                     "survived": sorted(survived), "replaced": sorted(authored - folded)})
    listed = rows if limit <= 0 else rows[:limit]
    rate: Optional[float] = (survived_ids / authored_ids) if authored_ids else None
    return {"nodes": listed, "authored_nodes": len(rows), "reclassified_nodes": reclassified,
            "authored_ids": authored_ids, "survived_ids": survived_ids,
            "survival_rate": rate, "by_producer": by_producer,
            "truncated": len(rows) - len(listed)}
