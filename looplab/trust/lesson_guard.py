"""Lesson over-generalization guard (PART IV D6, §21.7/§21.10 — Phase 1b, offline/audit-only).

**Why this exists.** node_63 was a *correct direction* (false-negative handling) killed by *one bad
implementation* (a loss-side hack). The run distilled the wrong lesson — "don't correct false
negatives" — and that mis-lesson poisoned a sound direction for the rest of the run. Measured (§21.10):
a criteria-decomposed verifier over that exact lesson flags `over_generalizes=true`, identifies the sound
underlying direction, and rescopes it to "*this specific setup* failed". §21.12 (E4) adds a companion:
a scan of the whole lesson store for mutually-contradictory lessons (the false-negative mis-lesson is one).

**What this is.** An audit harness over a run's distilled lessons (`RunState.lessons_distilled`). For each
lesson it assembles the CHECKABLE evidence — the (child, parent) node pairs the lesson was distilled from,
with their real outcomes — and runs the 0c advisory verifier with `lesson_overgeneralization_criteria`.
A lesson is FLAGGED when the verifier says it over-generalizes AND the underlying direction is sound (the
mis-lesson pattern), and the flag is tagged to the concept-graph node the lesson touches so it attaches to
a taxonomy branch rather than a whole axis (§21.7). A separate `contradiction_scan` finds lesson pairs the
verifier judges mutually inconsistent.

**Discipline.** Strictly ADVISORY / audit (§21.7): it reads the folded run, calls the verifier
(best-effort — no client => `available=false`, never blocks), and returns findings; it writes no events
and never touches selection or the live lesson store. Wiring a flag into the live distillation is Phase 2.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from looplab.core.models import NodeStatus, RunState
from looplab.trust.verifier import (VerdictReport, lesson_overgeneralization_criteria, verify)


@dataclass
class LessonFinding:
    """One guarded lesson. `flagged` = the verifier judged it over-generalizes a sound direction."""
    statement: str
    flagged: bool
    over_generalizes: Optional[float]      # verifier score in [0,1] (None if unavailable)
    direction_sound: Optional[float]
    agreement: float
    concepts: list[str]                    # concept-graph tags of the lesson (attaches it to a branch)
    evidence_nodes: list[int]
    rescope_hint: str = ""
    at_node: int = 0


def _lesson_records(state: RunState) -> list[dict]:
    """Flatten `lessons_distilled` events into per-lesson records: {statement, outcome, at_node,
    node_ids}. `node_ids` are the evidence: the lesson's own `evidence` ids if present, else the child
    ids of the (child, parent) `pairs` the distillation spent (the failed/won experiments it came from)."""
    out: list[dict] = []
    for ev in (state.lessons_distilled or []):
        at_node = int(ev.get("at_node", 0) or 0)
        pair_children = [p[0] for p in (ev.get("pairs") or []) if isinstance(p, (list, tuple)) and p]
        for lz in (ev.get("lessons") or []):
            stmt = str(lz.get("statement", "") or "").strip()
            if not stmt:
                continue
            ev_ids = lz.get("evidence")
            node_ids = ([i for i in ev_ids if isinstance(i, int)]
                        if isinstance(ev_ids, list) else list(pair_children))
            out.append({"statement": stmt, "outcome": str(lz.get("outcome", "") or ""),
                        "at_node": at_node, "node_ids": node_ids})
    return out


def _evidence_text(rec: dict, state: RunState) -> str:
    """The checkable outcome of each cited node — mirrors trust/verify.py::_evidence_text so the verifier
    grades against what actually happened, not the lesson's own wording."""
    parts: list[str] = []
    if rec.get("outcome"):
        parts.append(f"distilled outcome: {rec['outcome']}")
    for nid in (rec.get("node_ids") or [])[:8]:
        n = state.nodes.get(nid)
        if n is None:
            parts.append(f"#{nid}: (no such experiment)")
        elif n.status is NodeStatus.failed:
            parts.append(f"#{nid} {n.operator}: FAILED ({n.error_reason or 'error'}) — "
                         f"{' '.join((n.idea.rationale or '').split())[:90]}")
        else:
            parts.append(f"#{nid} {n.operator}: metric={n.metric} — "
                         f"{' '.join((n.idea.rationale or '').split())[:90]}")
    return "\n".join(parts) or "(no evidence recorded)"


def guard_lessons(state: RunState, *, client=None, samples: int = 3, parser: str = "tool_call",
                  graph=None, flag_threshold: float = 0.6) -> dict:
    """Run the over-generalization guard over every distilled lesson. Returns:

      available   - False when no client is wired (nothing was scored)
      n_lessons   - lessons examined
      n_flagged   - lessons flagged as over-generalizing a sound direction
      findings    - [LessonFinding-as-dict] for every lesson (flagged first)

    A lesson is FLAGGED when `over_generalizes >= flag_threshold` AND `direction_sound >= flag_threshold`
    (it broadens a single failed implementation into a whole sound direction — the node_63 pattern).
    Advisory: best-effort, never raises."""
    recs = _lesson_records(state)
    crit = lesson_overgeneralization_criteria()
    findings: list[LessonFinding] = []
    available = client is not None
    for rec in recs:
        concepts = _tag_lesson(rec["statement"], graph)
        if client is None:
            findings.append(LessonFinding(rec["statement"], False, None, None, 0.0, concepts,
                                          rec["node_ids"], at_node=rec["at_node"]))
            continue
        rep: VerdictReport = verify(rec["statement"], _evidence_text(rec, state), crit,
                                    client=client, samples=samples, parser=parser)
        og = rep.per_criterion.get("over_generalizes", {}).get("mean")
        ds = rep.per_criterion.get("direction_sound", {}).get("mean")
        flagged = (og is not None and ds is not None
                   and og >= flag_threshold and ds >= flag_threshold)
        hint = ""
        if flagged:
            hint = ("rescope to 'this specific implementation/setup failed'; keep the underlying "
                    "direction open for a different implementation")
        findings.append(LessonFinding(rec["statement"], flagged, og, ds, rep.agreement, concepts,
                                       rec["node_ids"], rescope_hint=hint, at_node=rec["at_node"]))
    findings.sort(key=lambda f: (not f.flagged, -(f.over_generalizes or 0.0)))
    return {
        "available": available,
        "n_lessons": len(recs),
        "n_flagged": sum(1 for f in findings if f.flagged),
        "findings": [_finding_dict(f) for f in findings],
    }


def contradiction_scan(state: RunState, *, client=None, parser: str = "tool_call",
                       max_pairs: int = 40) -> dict:
    """E4 (§21.12): scan the lesson store for MUTUALLY-CONTRADICTORY lesson pairs (the false-negative
    mis-lesson vs a data-side false-neg win is one). For each candidate pair the verifier judges whether
    lesson B contradicts lesson A. Advisory / best-effort; `available=False` without a client. Bounded by
    `max_pairs` so a big store can't explode the call count (reports if it truncated)."""
    from looplab.trust.verifier import Criterion
    recs = _lesson_records(state)
    stmts = [r["statement"] for r in recs]
    if client is None:
        return {"available": False, "n_lessons": len(stmts), "contradictions": [], "truncated": False}
    crit = [Criterion("contradicts",
                      "Do these two distilled lessons give CONTRADICTORY guidance — such that following "
                      "one means violating the other (not merely different topics)?")]
    contradictions: list[dict] = []
    checked = 0
    truncated = False
    for i in range(len(stmts)):
        for j in range(i + 1, len(stmts)):
            if checked >= max_pairs:
                truncated = True
                break
            checked += 1
            rep = verify("Lesson pair", f"LESSON A: {stmts[i]}\nLESSON B: {stmts[j]}", crit,
                         client=client, parser=parser, samples=1)
            sc = rep.per_criterion.get("contradicts", {}).get("mean")
            if sc is not None and sc >= 0.75:
                contradictions.append({"a": stmts[i], "b": stmts[j], "score": sc})
        if truncated:
            break
    return {"available": True, "n_lessons": len(stmts), "contradictions": contradictions,
            "truncated": truncated}


def _tag_lesson(statement: str, graph) -> list[str]:
    """Concept-graph tags for a lesson statement (attaches the finding to a taxonomy branch, §21.7).
    Deterministic; empty when no graph or no alias matches. Lessons are natural sentences, so plural
    forms are allowed ("false negatives" -> the "false negative" alias)."""
    if graph is None:
        return []
    try:
        from looplab.search.concept_graph import tag_text
        return sorted(tag_text(statement, graph, allow_plural=True))
    except Exception:  # noqa: BLE001 — tagging is best-effort context, never blocks the guard
        return []


def _finding_dict(f: LessonFinding) -> dict:
    return {"statement": f.statement, "flagged": f.flagged, "over_generalizes": f.over_generalizes,
            "direction_sound": f.direction_sound, "agreement": f.agreement, "concepts": f.concepts,
            "evidence_nodes": f.evidence_nodes, "rescope_hint": f.rescope_hint, "at_node": f.at_node}
