"""The ARTIFACT CONSUMER FENCE (doc 73 §1.4, stage 2): a node that `uses` an artifact node reads
exactly the artifact it was admitted with, or does not run.

WHY (2026-10-08). An artifact node (a prepared dataset) is produced once and read by every node that
names it in `uses` (`LOOPLAB_USES_WORKDIRS`). The inject refuses a `uses` naming a producer that has
not been produced (`serve/control_validation.py`), but nothing held that true AFTER the inject: an
operator `node_reset` of the producer put it back to `pending` and re-materialized its workdir while
consumers were queued — a consumer then ran on a half-written directory, or on none (the variable
silently dropped a path), crashed, and bought a triage and a repair for a fault no code edit could
fix. And a consumer measured on generation 0 of the dataset kept its number after the producer was
rebuilt into generation 1, with nothing on the record saying which data the number was OF.

THREE RULES, each on the fold and nothing else:

* WAITING — a producer is `pending` (being produced again). The consumer is not dispatched
  (`defer_waiting_consumers`, applied to every turn's selected actions) and ADMIT returns without a
  terminal if one reaches it anyway (`engine/evaluate.py::_eval_admit`). It runs once the producer
  settles.
* UNAVAILABLE — a producer is gone, deleted, aborted, not an artifact, or failed in its current
  lifecycle. The consumer is closed at ZERO cost with the engine terminal `artifact_unavailable`
  (`core/models.py::ENGINE_TERMINAL_REASONS`, benign: nothing about the experiment was measured),
  by the same pre-start stop every lane asks (`eval_dispatch.py::_skip_if_aborted`).
* THE RECEIPT — a consumer's `node_evaluated.metric_provenance.uses` records, per producer, the
  lifecycle (`generation`) and code (`code`, the producer's workdir stamp) it was admitted with.
  `stale_uses` compares it with the producer's CURRENT lifecycle; a champion whose receipt went stale
  carries the champion caveat `stale_artifact` (`engine/champion_caveats.py`).

PURE: no I/O here. The workdir stamp check — that the producer's directory still holds the lifecycle
the fold says it does — is `eval_dispatch.py::_uses_workdirs_env`'s, at the one site that hands the
paths to the candidate.
"""
from __future__ import annotations

from typing import Optional

ARTIFACT_UNAVAILABLE_REASON = "artifact_unavailable"

READY, WAITING, UNAVAILABLE = "ready", "waiting", "unavailable"


def _status(node) -> str:
    return str(getattr(getattr(node, "status", None), "value", getattr(node, "status", "")) or "")


def uses_verdicts(state, node) -> list[tuple[int, str, str]]:
    """`[(producer_id, READY|WAITING|UNAVAILABLE, why)]` for every producer `node.uses` names, in
    declaration order; `[]` for a node that uses nothing (every node of every log before `uses`)."""
    out: list[tuple[int, str, str]] = []
    nodes = getattr(state, "nodes", None) or {}
    aborted = getattr(state, "aborted_nodes", None) or ()
    for pid in getattr(node, "uses", None) or []:
        if isinstance(pid, bool) or not isinstance(pid, int):
            continue
        producer = nodes.get(pid)
        if producer is None:
            out.append((pid, UNAVAILABLE, f"artifact #{pid} does not exist"))
        elif getattr(producer, "tombstoned", False):
            out.append((pid, UNAVAILABLE, f"artifact #{pid} was deleted"))
        elif pid in aborted:
            out.append((pid, UNAVAILABLE, f"artifact #{pid} was aborted"))
        elif getattr(producer, "kind", None) != "artifact":
            out.append((pid, UNAVAILABLE, f"#{pid} is not an artifact node"))
        elif _status(producer) == "pending":
            out.append((pid, WAITING, f"artifact #{pid} is being produced"))
        elif _status(producer) == "evaluated":
            out.append((pid, READY, ""))
        else:
            reason = str(getattr(producer, "error_reason", "") or "failed")[:80]
            out.append((pid, UNAVAILABLE, f"artifact #{pid} failed in its current lifecycle ({reason})"))
    return out


def uses_waiting(state, node) -> bool:
    """A producer this node reads is being produced, and none is unavailable (an unavailable one
    closes the node instead of making it wait for something that will not come)."""
    verdicts = uses_verdicts(state, node)
    return (any(v == WAITING for _, v, _ in verdicts)
            and not any(v == UNAVAILABLE for _, v, _ in verdicts))


def uses_unavailable(state, node) -> Optional[str]:
    """Why this node can never read what it uses in its current state, or None."""
    whys = [why for _, v, why in uses_verdicts(state, node) if v == UNAVAILABLE]
    return "; ".join(whys)[:400] if whys else None


def uses_receipt(state, node) -> dict:
    """`{str(producer_id): {"generation": int, "code": str}}` for the READY producers — what the
    consumer was admitted with. `{}` for a node that uses nothing."""
    from looplab.engine.evaluate import workdir_manifest_digest
    out: dict = {}
    nodes = getattr(state, "nodes", None) or {}
    for pid, verdict, _ in uses_verdicts(state, node):
        if verdict != READY:
            continue
        producer = nodes[pid]
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
    """`actions` less the `evaluate` actions of nodes whose producer is being produced, and how many
    were deferred. Everything else passes through untouched, in order."""
    nodes = getattr(state, "nodes", None) or {}
    kept, deferred = [], 0
    for a in actions or []:
        node = nodes.get(a.get("node_id")) if isinstance(a, dict) and a.get("kind") == "evaluate" \
            else None
        if node is not None and getattr(node, "uses", None) and uses_waiting(state, node):
            deferred += 1
            continue
        kept.append(a)
    return kept, deferred
