"""Read-only lineage and score comparison projections from folded result evidence."""


def public_parent_comparison(node):
    """Never replace a recorded parent lifecycle with the parent's current attempt."""
    if node is None or len(node.parent_ids) != 1:
        return None
    parent_id = node.parent_ids[0]
    attempt = node.parent_generations.get(str(parent_id))
    if (isinstance(parent_id, bool) or not isinstance(parent_id, int) or parent_id < 0
            or isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 0):
        return None
    return {"version": 1, "node_id": parent_id, "attempt": attempt}


def public_parent_edit(node, state):
    """First parent's recorded overlay, only while its creation-bound attempt remains readable.

    This is source comparison, not score comparison or materialization of inherited base files.
    A reset must not silently substitute the parent's new implementation for the original one.
    Historical callers pass their prefix fold, so no current workdir or sidecar is read here.
    """
    if node is None or not node.parent_ids:
        return None
    parent_id = node.parent_ids[0]
    parent = state.nodes.get(parent_id)
    attempt = node.parent_generations.get(str(parent_id))
    if (parent is None or parent.tombstoned or type(attempt) is not int
            or attempt < 0 or parent.attempt != attempt):
        return None
    return {"version": 1, "scope": "node_edit_overlay", "node_id": parent.id,
            "attempt": parent.attempt, "code": parent.code or "", "files": dict(parent.files),
            "deleted": list(parent.deleted),
            "base_revision": (parent.metric_provenance or {}).get("base_revision")}


def completion_parents(node, state) -> list[dict]:
    """The parents a completion receipt may compare against: the first 8, each still evaluated,
    live, not aborted, and at the attempt the child was created from (a reset parent cannot
    rewrite the child's historical comparison). The ONE eligibility rule shared by the result
    notice and the run report, so the two can never disagree about which parent counts.
    """
    from looplab.engine.comparability import comparability_status, record_of

    parents = []
    for pid in node.parent_ids[:8]:
        parent = state.nodes.get(pid)
        if (parent is None or parent.tombstoned or parent.status != "evaluated"
                or pid in state.aborted_nodes
                or node.parent_generations.get(str(pid)) != parent.attempt):
            continue
        parents.append({"node_id": pid, "attempt": parent.attempt,
                        "comparability": comparability_status(record_of(node), record_of(parent))})
    return parents


def completion_score_comparison(node, parents, state, flagged):
    """Describe whether primary scores support a single-parent comparison.

    Input identity alone says nothing about eligibility or the copied code base.
    This is a current-evidence read; no filesystem verification or engine action.
    """
    from looplab.core.fitness import counts_toward_best, is_usable_metric
    from looplab.engine.seed_archive import seed_archive_digest

    count = len(node.parent_ids)
    status = "no_parent" if count == 0 else "multiple_parents" if count > 1 else "parent_unavailable"
    reference = public_parent_comparison(node)
    if count == 1 and len(parents) == 1 and reference == {
            "version": 1, "node_id": parents[0]["node_id"], "attempt": parents[0]["attempt"]}:
        parent = state.nodes[parents[0]["node_id"]]
        if state.objective_key:
            status = "retargeted"
        elif state.direction not in ("min", "max") or any(
                n.status != "evaluated" or n.tombstoned
                or not counts_toward_best(n, flagged=flagged, aborted=state.aborted_nodes)
                or not is_usable_metric(n.metric)
                or (isinstance(n.metric_provenance, dict) and n.metric_provenance.get("salvaged"))
                for n in (node, parent)):
            status = "ineligible"
        else:
            status = parents[0]["comparability"]
            receipts = [(n.metric_provenance or {}).get("base_revision") for n in (node, parent)]
            if state.upstream_enabled or any(r is not None for r in receipts):
                digests = []
                for n, receipt in zip((node, parent), receipts):
                    digest = seed_archive_digest(receipt)
                    if (not digest or receipt.get("complete") is not True
                            or any(type(receipt.get(k)) is not int or not 0 <= receipt[k] <= 2**53 - 1
                                   for k in ("node_id", "generation", "seed_event_seq", "file_count", "bytes"))
                            or receipt["node_id"] != n.id or receipt["generation"] != n.attempt):
                        digest = None
                    digests.append(digest)
                if not all(digests):
                    status = "base_unknown"
                elif digests[0] != digests[1]:
                    status = "base_different"
            if status == "same" and not is_usable_metric(node.metric - parent.metric):
                status = "ineligible"
    return {"version": 1, "parent_count": count, "status": status}
