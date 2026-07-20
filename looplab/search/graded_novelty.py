"""Graded novelty + failed-direction re-examination (PART IV D3, §21.4/§21.10/§21.12 — Phase 1c, advisory).

**Why this exists.** The live novelty gate (`engine/novelty.py::_llm_novelty_gate`) asks exactly ONE
question — *is this a near-duplicate of an experiment tried in THIS run?* — and has no path to RE-OPEN a
wrongly-killed direction. node_63 is the archetype: a correct DIRECTION (false-negative handling) died
from one bad IMPLEMENTATION (a loss-side hack), and the loop had no way to say "the direction is sound,
the implementation was wrong — re-research and retry differently". Worse, blind single-shot judging of
whether to re-examine is HIGH-VARIANCE (it flipped verdicts across identical runs, §21.12) and, unblinded,
it reproduced the loop's original mistake (§21.10). The fix that measured correct: **grade novelty over
the concept graph** (so the gate can tell "this DCL tweak" from "the whole DCL branch"), and make
re-examination **grounded** (D1 assets / prior art) and **repeated** (the 0c verifier), never a blind call.

**What this is (advisory library, not the live gate).** Two pure/audit pieces plus one verifier-backed one:
  * `grade_novelty(...)` — DETERMINISTIC multi-level classifier of a proposed idea vs the run's history,
    using the concept graph for the branch-vs-leaf distinction (the §21.4 levels 1–5). No LLM.
  * `failed_directions(...)` — DETERMINISTIC: concept-graph directions every experiment touching them
    FAILED — the re-examination candidates (node_63's `negatives/false-neg-handling` is one).
  * `reexamine_failed_direction(...)` — the 0c verifier (`reexamination_criteria`) grounded in the D1
    asset brief, deciding implementation-bound (re-open) vs direction-bound (leave closed). Best-effort.

**Discipline.** Phase 1c is offline→ADVISORY (§21.13): this module computes the graded decision the engine
COULD consult, and is validated offline; it does NOT change `_llm_novelty_gate`'s live behavior (that is
Phase 2b). Deterministic parts are pure; the re-examination is best-effort and degrades without a client.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from looplab.core.models import Idea, NodeStatus, RunState
from looplab.search.concept_graph import (ConceptGraph, _experiment_nodes, tag_nodes_heuristic,
                                          tag_text, tag_text_llm)


# --------------------------------------------------------------------------- #
# Tagging a single (proposed) idea
# --------------------------------------------------------------------------- #

def _idea_text(idea: Idea) -> str:
    # CODEX AGENT: concepts are authored by the proposal being admitted. They remain display metadata,
    # not classifier input; otherwise a proposal can self-assign a shared/failed concept and earn L4/L5.
    # Include the search-SPACE key names alongside params (as concept_graph._node_text does), so the idea
    # tagger and the node tagger describe the SAME experiment by the same structural fields — a sweep whose
    # only signal is in `space` (params={}, space={"temperature": [...]}) otherwise tags empty as an idea
    # but tags "hyperparameter/temperature" as the executed node. These are structural dimension names, not
    # the self-assertable `concepts` field the CODEX note excludes, so this adds no admission-gaming surface.
    parts = [getattr(idea, "theme", "") or "", getattr(idea, "rationale", "") or "",
             getattr(idea, "hypothesis", "") or "", getattr(idea, "operator", "") or "",
             " ".join(str(k) for k in (getattr(idea, "params", None) or {})),
             " ".join(str(k) for k in (getattr(idea, "space", None) or {}))]
    return " ".join(parts).lower()


def tag_idea(idea: Idea, graph: ConceptGraph) -> frozenset[str]:
    """Concept tags for a single proposed idea (deterministic, alias/lineage — the shared `tag_text`)."""
    return tag_text(_idea_text(idea), graph)


def tag_idea_llm(idea: Idea, graph: ConceptGraph, client, *, parser: str = "tool_call") -> frozenset[str]:
    """AGENTIC single-idea tagger (§21.4 F2): tags the proposed idea with the LLM against the graph's grown
    vocabulary — CONSISTENT with the cached node tags — via the shared `tag_text_llm` (grow=False; respects
    an empty 'novel' verdict; heuristic `tag_text` fallback on no client / all-unknown / failure). The idea's
    text is `_idea_text(idea)`, so the heuristic fallback equals `tag_idea(idea, graph)`."""
    return tag_text_llm(_idea_text(idea), graph, client, parser=parser)


def _params_identical(a: dict, b: dict, *, tol: float = 1e-9) -> bool:
    """Same param KEYS and values (within tol). An empty-vs-empty pair is identical (a structural idea)."""
    if set(a or {}) != set(b or {}):
        return False
    for k in (a or {}):
        try:
            if abs(float(a[k]) - float(b[k])) > tol:
                return False
        except (TypeError, ValueError):
            if a[k] != b[k]:
                return False
    return True


def _params_close(a: dict, b: dict, *, rel: float = 0.15) -> bool:
    """Same keys and every value within `rel` relative distance — a near-duplicate parameter point (a
    trivially-close variant). Different key sets are NOT close (a structural change)."""
    if set(a or {}) != set(b or {}) or not a:
        return False
    for k in a:
        try:
            av, bv = float(a[k]), float(b[k])
        except (TypeError, ValueError):
            if a[k] != b[k]:
                return False
            continue
        scale = max(abs(av), abs(bv), 1e-9)
        if abs(av - bv) / scale > rel:
            return False
    return True


# --------------------------------------------------------------------------- #
# Failed directions (re-examination candidates)
# --------------------------------------------------------------------------- #

@dataclass
class FailedDirection:
    concept: str
    node_ids: list[int]              # the experiments that touched it (all failed / never-won)
    reason: str                      # a compact why-it-looks-failed


def failed_directions(state: RunState, graph: ConceptGraph,
                      tags: Optional[dict[int, frozenset[str]]] = None) -> list[FailedDirection]:
    """Concept-graph directions where EVERY experiment that touched them failed to produce a win — the
    wrongly-abandoned re-examination candidates (§21.4). Pure/deterministic. A direction 'won' if any
    touching node is feasible with a metric that beat its parent (or, absent a parent, just has a metric);
    a direction all of whose nodes are failed/never-improved is a candidate. Concepts touched by a real
    win are excluded."""
    nodes = _experiment_nodes(state)
    if tags is None:
        tags = tag_nodes_heuristic(state, graph)
    by_concept: dict[str, list] = {}
    for nd in nodes:
        for cid in tags.get(nd.id, frozenset()):
            by_concept.setdefault(cid, []).append(nd)
    out: list[FailedDirection] = []
    for cid in sorted(by_concept):
        touching = by_concept[cid]
        won = any(_node_is_win(state, nd) for nd in touching)
        if won:
            continue
        failed = [nd for nd in touching if nd.status is NodeStatus.failed]
        if not failed:
            continue                          # tried but merely un-improved (not a clear failure) -> skip
        n_failed = len(failed)
        reason = (f"{n_failed} experiment(s) touching '{cid}' failed and none won"
                  + (f"; e.g. #{failed[0].id} ({failed[0].error_reason or 'error'})"))
        out.append(FailedDirection(cid, [nd.id for nd in touching], reason))
    return out


def _node_is_win(state: RunState, node) -> bool:
    """Did this node produce a usable, improving result? (feasible, has a metric, and — when it has a
    scored parent — beat it)."""
    if node.status is not NodeStatus.evaluated or not node.feasible or node.metric is None:
        return False
    pm = [state.nodes[p].metric for p in node.parent_ids
          if p in state.nodes and state.nodes[p].metric is not None]
    if not pm:
        return True                           # a seed/orphan with a metric counts as a win
    base = max(pm) if state.direction == "max" else min(pm)
    return state.is_better(node.metric, base)


# --------------------------------------------------------------------------- #
# Graded novelty classifier (§21.4 levels 1–5)
# --------------------------------------------------------------------------- #

_LEVELS = {
    1: "identical",                 # same params as a tried node -> reject hard
    2: "near_duplicate_in_run",     # same concepts + trivially-close params -> re-propose once
    3: "tried_across_runs",         # concept seen in a PRIOR run's index -> surface prior outcome
    4: "same_direction_new_impl",   # shares a concept branch but a materially different impl -> ALLOW
    5: "wrongly_abandoned",         # concept is a FAILED direction here -> re-examine (not reject)
}
_RECO = {1: "reject", 2: "repropose", 3: "surface_prior", 4: "allow", 5: "reexamine"}


@dataclass
class NoveltyGrade:
    level: int
    name: str
    recommendation: str             # reject | repropose | surface_prior | allow | reexamine | allow (novel)
    near_node: Optional[int]
    shared_concepts: list[str]
    rationale: str


def grade_novelty(state: RunState, idea: Idea, graph: ConceptGraph, *,
                  tags: Optional[dict[int, frozenset[str]]] = None,
                  idea_tags: Optional[frozenset] = None,
                  prior_concepts: Optional[set[str]] = None) -> NoveltyGrade:
    """Grade a PROPOSED idea against the run's history over the concept graph (§21.4). Deterministic given
    its tags. The key advance over the flat gate: it distinguishes 'this DCL tweak' (near-dup / same-impl)
    from 'the whole DCL branch' (same-direction-new-impl -> ALLOW) using concept membership, and it
    recognizes a proposal that RE-OPENS a wrongly-abandoned failed direction (-> reexamine, not reject).

    Agentic-first (§21.15 correction): pass `tags` (the LLM-built node tags from `build_concept_map`) and
    `idea_tags` (the LLM-built concept set for THIS proposed idea) so the branch-vs-leaf decision uses the
    agent's tagging, consistent with the LLM novelty gate. Both default to the deterministic alias tagger
    only as the no-LLM FALLBACK. `prior_concepts`: concept ids tried in EARLIER runs (cross-run memory).
    """
    nodes = _experiment_nodes(state)
    if tags is None:
        tags = tag_nodes_heuristic(state, graph)
    idea_concepts = idea_tags if idea_tags is not None else tag_idea(idea, graph)

    # 1) identical params to a tried node -> reject
    for nd in nodes:
        if _params_identical(idea.params, nd.idea.params) and (idea.params or nd.idea.params):
            return NoveltyGrade(1, _LEVELS[1], _RECO[1], nd.id, sorted(idea_concepts),
                                f"same parameters as tried experiment #{nd.id}")

    # Concept overlap analysis. The deferred axis-ancestor refinement is not implemented: this is
    # EXACT concept-id
    # intersection. It is coarse ON PURPOSE — the tagger keys on lineage FAMILY (all `dcl-*` map to the one
    # `loss/decoupled-contrastive` concept, §21.10 refinement 1), so genuine sibling-method variants already
    # share a concept id here. A finer AXIS-ANCESTOR projection (two different leaves under one branch also
    # counting as overlap) would strengthen the branch-vs-leaf signal further; deferred as a refinement,
    # and it must be applied to EVERY consumer consistently if adopted.
    def overlap(nd) -> set[str]:
        return set(idea_concepts) & set(tags.get(nd.id, frozenset()))

    # "full concept-set" here means the idea's COMPLETE set is already subsumed by a tried node
    # (idea_concepts ⊆ node tags, i.e. `overlap == idea_concepts`) — the idea introduces no concept the
    # node lacks. Deliberately containment, NOT set-equality: with trivially-close params that is a
    # near-duplicate to re-propose (level 2, which the live precheck safely DEFERS to the flat gate), so it
    # must not fall through to the level-4 ALLOW short-circuit. An idea that INTRODUCES a new concept is a
    # different profile and correctly reaches level 4 below.
    same_concept_nodes = [nd for nd in nodes if idea_concepts and overlap(nd) == set(idea_concepts)]

    # 2) same full concept-set AND trivially-close params -> near-duplicate in run -> re-propose
    for nd in same_concept_nodes:
        if _params_close(idea.params, nd.idea.params):
            return NoveltyGrade(2, _LEVELS[2], _RECO[2], nd.id, sorted(idea_concepts),
                                f"same concepts and near-identical params as #{nd.id}")

    # 5) the proposed direction is a FAILED direction here -> re-examine (don't reject a sound direction).
    # Reached only AFTER the near-duplicate check above, so a proposal that merely REPEATS the failed
    # experiment is already handled as a near-dup; here it is a MATERIALLY-DIFFERENT retry of the same
    # (wrongly-abandoned) direction — the node_63 archetype (loss-side hack failed -> try the data side).
    failed = {fd.concept: fd for fd in failed_directions(state, graph, tags)}
    reopen = [c for c in idea_concepts if c in failed]
    if reopen:
        fd = failed[sorted(reopen)[0]]
        failed_ids = [node_id for node_id in fd.node_ids
                      if state.nodes[node_id].status is NodeStatus.failed]
        return NoveltyGrade(5, _LEVELS[5], _RECO[5], (failed_ids[0] if failed_ids else None),
                            sorted(idea_concepts),
                            f"re-opens wrongly-abandoned direction '{fd.concept}' ({fd.reason})")

    # 3) concept tried in a PRIOR run -> surface the prior outcome (materially-different check is the caller's)
    if prior_concepts and (idea_concepts & set(prior_concepts)):
        return NoveltyGrade(3, _LEVELS[3], _RECO[3], None, sorted(idea_concepts),
                            "concept(s) tried in an earlier run — surface the prior outcome")

    # 4) shares a concept BRANCH with a tried node -> same direction, different implementation -> ALLOW.
    # ANY concept overlap qualifies: a FULL-profile near-duplicate with close params was already caught by
    # level 2, so a proposal reaching here either has different params (a valid variant) or a DIFFERENT
    # concept profile (it introduces a new concept alongside the shared one) — both are "same direction,
    # new implementation", NOT novel. (Requiring not-close-params here wrongly sent a partial-overlap
    # close-params proposal all the way to `novel`.)
    for nd in nodes:
        if overlap(nd):
            return NoveltyGrade(4, _LEVELS[4], _RECO[4], nd.id, sorted(overlap(nd)),
                                f"same direction as #{nd.id} but a different implementation — allow")

    # otherwise: no concept overlap -> a genuinely new region
    return NoveltyGrade(0, "novel", "allow", None, sorted(idea_concepts),
                        "no overlap with tried directions — a new region of the space")


# --------------------------------------------------------------------------- #
# Grounded, repeated re-examination (the 0c verifier)
# --------------------------------------------------------------------------- #

def reexamine_failed_direction(state: RunState, node_id: int, graph: ConceptGraph, *,
                               client=None, asset_brief: str = "", samples: int = 3,
                               parser: str = "tool_call") -> dict:
    """Should a failed direction be RE-OPENED? Runs the 0c verifier (`reexamination_criteria`) over the
    failed node's real outcome PLUS the D1 prior-art brief (grounding is what flipped the blind critic
    from wrong to correct, §21.10), repeated to tame the single-shot variance (§21.12). Returns:

      available            - False without a client
      implementation_bound - verifier score in [0,1]: high = the IMPLEMENTATION failed, not the direction
      reexamine            - verifier score in [0,1]: high = worth re-opening with a different impl
      agreement            - cross-sample stability (low => still too noisy to act on)
      n_samples            - usable verifier samples (a caller can require a strict parsed majority)
      requested_samples    - requested repetition count
      recommendation       - 'reexamine' | 'leave_closed' | 'unavailable'

    Advisory / best-effort — never raises."""
    from looplab.trust.verifier import reexamination_criteria, verify
    requested_samples = samples if type(samples) is int else 1
    n = state.nodes.get(node_id)
    if n is None:
        return {"available": False, "recommendation": "unavailable", "reason": f"no node #{node_id}",
                "n_samples": 0, "requested_samples": requested_samples, "agreement": 0.0}
    concepts = sorted(tag_idea(n.idea, graph)) if n.idea else []
    outcome = (f"FAILED ({n.error_reason or 'error'})" if n.status is NodeStatus.failed
               else f"metric={n.metric}")
    subject = (f"The direction touching {concepts or 'this experiment'} was tried in experiment #{n.id} "
               f"and did not succeed. Should it be re-opened with a different implementation?")
    evidence = (f"Experiment #{n.id} ({n.operator}) {outcome}. What it did: "
                f"{' '.join((n.idea.rationale or '').split())[:200]}\n"
                f"Triage: {' '.join((n.triage_rationale or '').split())[:200] or '(none)'}")
    if asset_brief:
        evidence += f"\n\nPRIOR ART / AVAILABLE ASSETS:\n{asset_brief[:1500]}"
    if client is None:
        return {"available": False, "recommendation": "unavailable", "node_id": node_id,
                "concepts": concepts, "n_samples": 0, "requested_samples": requested_samples,
                "agreement": 0.0}
    rep = verify(subject, evidence, reexamination_criteria(), client=client, samples=samples,
                 parser=parser)
    ib = rep.per_criterion.get("implementation_bound", {}).get("mean")
    rx = rep.per_criterion.get("reexamine", {}).get("mean")
    reco = "unavailable"
    if ib is not None and rx is not None:
        reco = "reexamine" if (ib >= 0.6 and rx >= 0.6) else "leave_closed"
    return {"available": True, "node_id": node_id, "concepts": concepts,
            "implementation_bound": ib, "reexamine": rx, "agreement": rep.agreement,
            "n_samples": rep.n_samples, "requested_samples": rep.requested_samples,
            "recommendation": reco}
