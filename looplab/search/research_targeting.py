"""Axis-structured deep-research targeting (PART IV D2, §21.3/§21.12 — Phase 1e, offline analytic).

**Why this exists.** The `rubertlite` deep-research memos were **state narrators** — they summarised the
current node landscape, and when they named external methods those were uniformly "new loss function"
papers, never applied-IR (§21.3). Measured (§21.12): a leader-anchored "propose the next experiment" ranked
the already-FAILED memory-bank hard-negative variant #1, whereas **axis-structured breadth** — querying
per taxonomy axis (data / negatives / loss / architecture / distillation / eval) — ranked the correct
mechanisms top-3 (cross-encoder Margin-MSE distillation, cross-encoder-mined hard negatives, gradient-
scaled cross-batch negatives). D2's measured value is **ranking quality + not re-proposing known-failed
variants**, not first discovery.

**What this is.** A pure targeting analysis over the 0a concept graph: it turns the coverage map into a
RANKED set of axis-structured research targets — the **uncovered** axes first (the blind regions the
uncovered-region alarm names), then **failed directions** re-framed as "research a DIFFERENT
implementation" (so the loop stops re-proposing the exact failed variant), then **under-covered** axes.
Each target carries a concrete, axis-scoped research query naming the specific unexplored/failed concepts,
optionally grounded in the D1 asset brief.

**Discipline.** Offline analytic (early lane): pure and deterministic over `(RunState, ConceptGraph, tags)`
— no I/O, no LLM, no wall-clock. It produces the *targets*; running deep research over them (the live
`make_deep_researcher` seam, §10 gates) is Phase 2. Writes nothing; never touches selection.
"""
from __future__ import annotations

from typing import Optional

from looplab.core.models import RunState
from looplab.search.concept_graph import ConceptGraph, concept_coverage
from looplab.search.graded_novelty import failed_directions


def research_targets(state: RunState, graph: ConceptGraph, *,
                     tags: Optional[dict[int, frozenset[str]]] = None,
                     asset_brief: str = "", max_targets: int = 8) -> dict:
    """Ranked, axis-structured deep-research targets from the coverage map (§21.3). Pure/deterministic.
    Returns:

      targets      - ranked [{axis, kind, concepts, query, rationale, priority}] (best first)
      uncovered    - axes with 0 coverage (the blind regions — highest priority)
      failed       - concept directions every experiment touching them failed (re-research differently)
      covered_axes - axes the search already entered (context)

    `kind` in {uncovered, failed-direction, under-covered}. `priority` is a small int (lower = do first).
    """
    cov = concept_coverage(state, graph, tags)
    all_axes = graph.axes()
    axis_touch = cov["axis_touch"]
    n = cov["experiments"]
    key_concepts = set(graph.key_concepts())

    def concepts_under(axis: str, *, uncovered_only: bool) -> list[str]:
        out = []
        for c in graph.concepts():
            if c.id.endswith("/*") or axis not in c.axes:
                continue
            if uncovered_only and c.id in cov["first_touch"]:
                continue
            out.append(c.id)
        return out

    ground = f"\nGROUNDING (assets already in the repo):\n{asset_brief[:800]}" if asset_brief else ""
    targets: list[dict] = []

    # 1) UNCOVERED axes — the blind regions (key regions first). Highest priority.
    uncovered_axes = cov["uncovered_axes"]
    for axis in uncovered_axes:
        cs = concepts_under(axis, uncovered_only=True)
        has_key = any(c in key_concepts for c in cs)
        named = ", ".join(cs[:5]) or axis
        query = (f"Survey applied techniques on the '{axis}' axis for this task that have NOT been tried "
                 f"(specifically: {named}). Rank concrete, implementable methods by expected payoff.{ground}")
        targets.append({"axis": axis, "kind": "uncovered", "concepts": cs,
                        "query": query, "priority": 0 if has_key else 1,
                        "rationale": f"0 coverage on the '{axis}' axis across all {n} experiments"})

    # 2) FAILED directions — re-research a DIFFERENT implementation (don't re-propose the failed variant).
    fds = failed_directions(state, graph, tags)
    for fd in fds:
        axis = fd.concept.split("/", 1)[0]
        query = (f"The '{fd.concept}' direction was tried and FAILED ({fd.reason}). Research a "
                 f"DIFFERENT implementation of this direction (not the failed variant) and whether it is "
                 f"worth re-opening.{ground}")
        targets.append({"axis": axis, "kind": "failed-direction", "concepts": [fd.concept],
                        "query": query, "priority": 2,
                        "rationale": f"'{fd.concept}' failed on its only implementation — re-research it"})

    # 3) UNDER-COVERED axes — touched, but lightly (a small share of effort) and not a busy lever.
    # Skip ALL axes tied for the max touch (not just one), so a co-dominant busy lever is never mislabeled
    # under-covered.
    max_touch = max(axis_touch.values()) if axis_touch else 0
    busy_axes = {a for a, t in axis_touch.items() if t == max_touch and t > 0}
    for axis in sorted(all_axes):
        touch = axis_touch.get(axis, 0)
        if touch == 0 or axis in busy_axes:
            continue                                   # 0-coverage handled above; skip the busy lever(s)
        frac = touch / n if n else 0.0
        if frac <= 0.25:                               # lightly explored -> a secondary target
            cs = concepts_under(axis, uncovered_only=True)
            named = ", ".join(cs[:5]) or axis
            query = (f"Deepen research on the lightly-explored '{axis}' axis (untried here: {named}). "
                     f"Surface methods not yet applied.{ground}")
            targets.append({"axis": axis, "kind": "under-covered", "concepts": cs,
                            "query": query, "priority": 3,
                            "rationale": f"'{axis}' touched by only {touch}/{n} experiments"})

    targets.sort(key=lambda t: (t["priority"], t["axis"]))
    return {
        "targets": targets[:max_targets],
        "uncovered": uncovered_axes,
        "failed": [fd.concept for fd in fds],
        "covered_axes": sorted(axis_touch),
    }


def targeting_report(state: RunState, graph: ConceptGraph, **kwargs) -> str:
    """A compact text diagnostic over the research targets — for the CLI. Pure."""
    r = research_targets(state, graph, **kwargs)
    if not r["targets"]:
        return ("Axis-structured research targets: none — every axis in the concept skeleton already "
                "has coverage (or the skeleton is empty; pass a --task-type pack or --llm tags).")
    lines = ["Axis-structured deep-research targets (highest priority first):"]
    for i, t in enumerate(r["targets"], 1):
        lines.append(f"  {i}. [{t['kind']}] axis '{t['axis']}' — {t['rationale']}")
        lines.append(f"     query: {t['query'].splitlines()[0]}")
    return "\n".join(lines)
