"""The ARTIFACT CONSUMER FENCE's two halves beside the pin (doc 73 §1.4).

THE PIN is the authority (`Node.uses_attempts`, round 3): each artifact a node `uses` is pinned to
the producer LIFECYCLE it was accepted against — an inject's intake, inherited by improve / merge
children, re-pinned by a consumer rebuild, and for a Researcher-proposed `Idea.uses` the producer's
lifecycle when the node was created (`events/replay.py::_use_pins`). Before every launch a pinned use
counts only if evaluated in exactly that lifecycle with its workdir stamp intact, else the consumer
ends `artifact_unavailable` (`engine/evaluate.py::EvaluateMixin._refuse_unusable_artifacts`): a
lifecycle generation only grows, so a reset producer can never be produced again in the pinned one.

WHAT THIS MODULE ADDS, and only that:

* WAITING — the one case the refusal above would get wrong. A use pinned to a lifecycle the producer
  is STILL IN and still producing (pending, not deleted, not aborted) — a Researcher proposed the
  preparation and its consumer back to back — can still be produced. The consumer is not dispatched
  meanwhile (`defer_waiting_consumers`, applied to every turn's selected actions), and ADMIT returns
  without a terminal if one reaches it anyway (`engine/evaluate.py::_eval_admit`).
* THE RECEIPT — a consumer's `node_evaluated.metric_provenance.uses` records, per producer, the
  lifecycle (`generation`) and code (`code`, the producer's workdir stamp) it was measured on.
  `stale_uses` compares it with the producer's CURRENT lifecycle; a champion whose receipt went stale
  — the dataset was re-produced, failed or deleted SINCE the number was measured — carries the
  champion caveat `stale_artifact` (`engine/champion_caveats.py`).

An UNPINNED use (a log written before pins) keeps the historical existence-only rule; nothing here
waits on it.

PURE: no I/O.
"""
from __future__ import annotations


def _status(node) -> str:
    return str(getattr(getattr(node, "status", None), "value", getattr(node, "status", "")) or "")


def _pins(node) -> dict:
    pins = getattr(node, "uses_attempts", None)
    return pins if isinstance(pins, dict) else {}


def uses_waiting(state, node) -> bool:
    """A use is pinned to a lifecycle its producer is still in and still producing."""
    nodes = getattr(state, "nodes", None) or {}
    aborted = getattr(state, "aborted_nodes", None) or ()
    for key, pin in _pins(node).items():
        try:
            pid = int(key)
        except (TypeError, ValueError):
            continue
        producer = nodes.get(pid)
        if (producer is not None and not getattr(producer, "tombstoned", False)
                and pid not in aborted and type(pin) is int
                and getattr(producer, "attempt", None) == pin and _status(producer) == "pending"):
            return True
    return False


def uses_receipt(state, node) -> dict:
    """`{str(producer_id): {"generation": int, "code": str}}` for every artifact the node uses that
    is evaluated in its current lifecycle — what the consumer is admitted with. `{}` otherwise."""
    from looplab.engine.evaluate import workdir_manifest_digest
    out: dict = {}
    nodes = getattr(state, "nodes", None) or {}
    for pid in getattr(node, "uses", None) or []:
        producer = nodes.get(pid) if type(pid) is int else None
        if producer is None or getattr(producer, "tombstoned", False) or _status(producer) != "evaluated":
            continue
        out[str(pid)] = {"generation": int(getattr(producer, "attempt", 0) or 0),
                         "code": workdir_manifest_digest(producer)[:16]}
    return out


def stale_uses(state, node) -> list[int]:
    """The producers whose CURRENT lifecycle is not the one this node's metric was measured on —
    re-produced, failed or deleted since. `[]` when the node recorded no receipt (it uses nothing, or
    its row predates the receipt: silence is not staleness)."""
    prov = getattr(node, "metric_provenance", None)
    receipt = prov.get("uses") if isinstance(prov, dict) else None
    if not isinstance(receipt, dict) or not receipt:
        return []
    nodes = getattr(state, "nodes", None) or {}
    out = []
    for key, row in receipt.items():
        try:
            pid = int(key)
        except (TypeError, ValueError):
            continue
        producer = nodes.get(pid)
        generation = row.get("generation") if isinstance(row, dict) else None
        if (producer is None or getattr(producer, "tombstoned", False)
                or _status(producer) != "evaluated"
                or getattr(producer, "attempt", None) != generation):
            out.append(pid)
    return sorted(out)


def defer_waiting_consumers(state, actions: list) -> tuple[list, int]:
    """`actions` less the `evaluate` actions of nodes `uses_waiting` holds back, and how many were
    deferred. Everything else passes through untouched, in order."""
    nodes = getattr(state, "nodes", None) or {}
    kept, deferred = [], 0
    for a in actions or []:
        node = nodes.get(a.get("node_id")) if isinstance(a, dict) and a.get("kind") == "evaluate" \
            else None
        if node is not None and _pins(node) and uses_waiting(state, node):
            deferred += 1
            continue
        kept.append(a)
    return kept, deferred
