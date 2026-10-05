"""Deterministic with/without concept contrasts, not a causal attribution model.

Match the COMPLETE remaining concept set and the recorded evaluation ruler. Prefer
single-parent contrasts, then pair observations in node order without reusing a node
for the same concept. Equal-weight context means prevent a heavily sampled recipe
from dominating the estimate. Missing tags are unknown, never negative controls.
Tags do not prove that only one implementation detail changed: all estimates remain
observational. No confidence interval is invented for dependent search experiments.
"""
from __future__ import annotations

import math

from looplab.core.idea_report import idea_not_tested
from looplab.core.models import NODE_CONCEPT_PROVENANCE_CLASSIFIER
from looplab.engine.comparability import comparability_status, record_of
from looplab.events.replay_selection import promotion_eligible_nodes
from looplab.search.concept_projection import current_concept_projection

MAX_EFFECT_WORK = 200_000
MAX_EFFECT_PAIRS = 8


def empty_effect(reason: str, *, status: str = "insufficient") -> dict:
    return {"v": 1, "method": "matched_concept_contrast", "status": status,
            "reason": reason, "estimate": None, "mean": None, "low": None, "high": None,
            "n_pairs": 0, "n_contexts": 0, "n_with": 0, "n_without": 0,
            "n_unknown": 0, "positive": 0, "negative": 0, "neutral": 0,
            "has_advisory_warnings": False,
            "pairs": [], "pairs_omitted": 0}


def valid_effect(value) -> bool:
    """Strict read-side validation for durable capsule additions (old rows have none)."""
    if (not isinstance(value, dict) or type(value.get("v")) is not int or value["v"] != 1
            or value.get("method") != "matched_concept_contrast"):
        return False
    if value.get("status") not in ("matched", "insufficient", "unavailable"):
        return False
    if type(value.get("has_advisory_warnings", False)) is not bool:
        return False
    counts = ("n_pairs", "n_contexts", "n_with", "n_without", "n_unknown", "positive",
              "negative", "neutral", "pairs_omitted")
    if any(type(value.get(key)) is not int or value[key] < 0 for key in counts):
        return False
    numbers = ("estimate", "mean", "low", "high")
    if any(value.get(key) is not None and (type(value[key]) not in (int, float)
                                          or not math.isfinite(value[key])) for key in numbers):
        return False
    pairs = value.get("pairs")
    if not isinstance(pairs, list) or len(pairs) > MAX_EFFECT_PAIRS:
        return False
    used = set()
    for pair in pairs:
        if (not isinstance(pair, dict)
                or any(type(pair.get(key)) is not int or pair[key] < 0
                       for key in ("with_node", "without_node", "with_attempt", "without_attempt"))
                or pair["with_node"] == pair["without_node"]
                or type(pair.get("delta")) not in (int, float) or not math.isfinite(pair["delta"])
                or pair.get("kind") not in ("parent", "matched")
                or pair.get("phase") not in ("search", "confirmed")):
            return False
        if pair["with_node"] in used or pair["without_node"] in used:
            return False
        used.update((pair["with_node"], pair["without_node"]))
    n = value["n_pairs"]
    if (len(pairs) + value["pairs_omitted"] != n
            or value["positive"] + value["negative"] + value["neutral"] != n
            or n > min(value["n_with"], value["n_without"])
            or value["n_contexts"] > n):
        return False
    if value["status"] == "matched":
        return (n > 0 and value["n_contexts"] > 0
                and all(value.get(key) is not None for key in numbers)
                and value["low"] <= value["estimate"] <= value["high"])
    return n == 0 and all(value.get(key) is None for key in numbers)


def concept_effects(state, concept_ids, *, subtree: bool = False,
                    classifier_only: bool = False) -> dict:
    """Same estimator for direct rows, path-subtree rows, capsules and agent tools.

    Only current eligible measured outcomes with complete canonical membership enter.
    An explicit empty membership is a control; an absent or partial row is not.
    Search and confirmation never mix; confirmation needs its recorded ruler.
    Unknown or different comparability is counted but contributes no numeric effect.
    Work is bounded before matching; exceeding the budget abstains for the whole
    projection rather than selecting a convenient prefix of experiments.
    """
    ids = sorted(set(concept_ids))
    if state.direction not in ("min", "max"):
        return {cid: empty_effect("direction_unknown", status="unavailable") for cid in ids}
    projection = current_concept_projection(state)
    if projection.global_reasons:
        return {cid: empty_effect("membership_source_incomplete", status="unavailable")
                for cid in ids}
    eligible = promotion_eligible_nodes(state)
    if len(ids) * len(eligible) > MAX_EFFECT_WORK:
        return {cid: empty_effect("analysis_limit", status="unavailable") for cid in ids}
    provenance = getattr(state, "node_concept_provenance", {}) or {}
    warned = {row.get("node_id") for row in (getattr(state, "reward_hacks", []) or [])
              if isinstance(row, dict) and type(row.get("node_id")) is int and row.get("signals")}
    samples = []
    for node in sorted(eligible, key=lambda n: n.id):
        if projection.node_status(node.id)[0] != "complete" or idea_not_tested(node, state.nodes):
            continue
        if classifier_only and (provenance.get(node.id) != NODE_CONCEPT_PROVENANCE_CLASSIFIER
                                or (getattr(state, "node_concepts_at_pending", {}) or {}).get(node.id, 0)):
            continue
        value = node.robust_metric
        if type(value) not in (int, float) or not math.isfinite(value):
            continue
        phase = "confirmed" if node.confirmed_mean is not None else "search"
        ruler = node.confirmed_ruler if phase == "confirmed" else None
        samples.append((node, frozenset(projection.memberships[node.id]), value, phase, ruler))
    sign = -1 if state.direction == "min" else 1
    result = {}
    work = len(ids) * len(eligible)
    for cid in ids:
        out = empty_effect("no_matched_controls")
        groups = {}
        for sample in samples:
            node, tags, _value, phase, ruler = sample
            focal = {tag for tag in tags if tag == cid or (subtree and tag.startswith(cid + "/"))}
            # Keep evaluation families separate, but ALWAYS ask the pairwise authority before
            # subtracting. Equal/missing inferred keys are never a certification of sameness.
            key = (tuple(sorted(tags - focal)), phase, ruler)
            groups.setdefault(key, [[], []])[bool(focal)].append(sample)
            out["n_with" if focal else "n_without"] += 1
        deltas, contexts, pairs = [], [], []
        representatives = []
        mixed = False
        for key in sorted(groups, key=repr):
            controls, treatments = groups[key]
            used = set()
            context_deltas = []
            # Direct lineage is more informative than an arbitrary distant search observation.
            # Multiple-parent merges are not a single-parent intervention.
            by_id = {s[0].id: s for s in controls + treatments}
            treatment_ids = {s[0].id for s in treatments}
            proposed = []
            for sample in controls + treatments:
                node = sample[0]
                if len(node.parent_ids) == 1 and node.parent_ids[0] in by_id:
                    parent = by_id[node.parent_ids[0]]
                    if (node.id in treatment_ids) != (parent[0].id in treatment_ids):
                        proposed.append((sample, parent) if node.id in treatment_ids else (parent, sample))
            # The fallback scan stops at a hard work budget, with NO partial numerical claim.
            def candidates():
                yield from ((a, b, "parent") for a, b in proposed)
                for treatment in treatments:
                    for control in controls:
                        yield treatment, control, "matched"
            for treatment, control, kind in candidates():
                a, b = treatment[0], control[0]
                work += 1
                if work > MAX_EFFECT_WORK:
                    return {id_: empty_effect("analysis_limit", status="unavailable") for id_ in ids}
                if a.id in used or b.id in used:
                    continue
                comparable = comparability_status(record_of(a), record_of(b))
                if comparable != "same" or (key[1] == "confirmed" and not key[2]):
                    out["n_unknown"] += 1
                    continue
                # Pairwise-compatible observations in DIFFERENT evaluation families cannot be
                # pooled into one raw-unit effect. Abstain rather than averaging smoke/full or
                # dataset scales. Existing objective retargeting is already applied by the fold.
                for previous in representatives:
                    work += 1
                    if work > MAX_EFFECT_WORK:
                        return {id_: empty_effect("analysis_limit", status="unavailable") for id_ in ids}
                    if (previous[1:] != key[1:]
                            or comparability_status(record_of(previous[0]), record_of(a)) != "same"):
                        mixed = True
                delta = sign * (treatment[2] - control[2])
                if not math.isfinite(delta):
                    continue
                used.update((a.id, b.id))
                out["has_advisory_warnings"] |= a.id in warned or b.id in warned
                representatives.append((a, key[1], key[2]))
                deltas.append(delta)
                context_deltas.append(delta)
                if len(pairs) < MAX_EFFECT_PAIRS:
                    pairs.append({"with_node": a.id, "without_node": b.id,
                                  "with_attempt": a.attempt, "without_attempt": b.attempt,
                                  "delta": delta, "kind": kind, "phase": key[1]})
            if out["status"] == "unavailable":
                break
            if context_deltas:
                contexts.append(math.fsum(d / len(context_deltas) for d in context_deltas))
        if mixed:
            out = empty_effect("mixed_evaluation_conditions")
            result[cid] = out
            continue
        if out["status"] != "unavailable" and deltas:
            ordered = sorted(contexts)
            mid = len(ordered) // 2
            estimate = (ordered[mid] if len(ordered) % 2 else
                        ordered[mid - 1] / 2 + ordered[mid] / 2)
            out.update(status="matched", reason="observational_not_causal",
                       estimate=estimate, mean=math.fsum(d / len(contexts) for d in contexts),
                       low=min(deltas), high=max(deltas), n_pairs=len(deltas), n_contexts=len(contexts),
                       positive=sum(d > 0 for d in deltas), negative=sum(d < 0 for d in deltas),
                       neutral=sum(d == 0 for d in deltas), pairs=pairs,
                       pairs_omitted=len(deltas) - len(pairs))
        elif out["status"] != "unavailable":
            out["reason"] = ("no_without_concept" if not out["n_without"] else
                             "no_with_concept" if not out["n_with"] else
                             "comparability_unknown_or_different" if out["n_unknown"] else
                             "no_matching_context_or_phase")
        result[cid] = out
    return result
