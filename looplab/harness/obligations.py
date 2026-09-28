"""Effective agent choices and evaluator obligations for a launched run.

The catalog describes what an agent *can* do. This projection describes what
LoopLab actually checks for this task. Keep admission and evaluation rules in
their owning services; this is their agent-readable explanation, not a second
validator that can drift into accepting a weaker candidate.
"""
from __future__ import annotations

from looplab.harness.phases import PHASES


def evidence_revision(state) -> str:
    """Materialized node outcomes, stable across unrelated notes and commands."""
    import hashlib
    import orjson
    from looplab.engine.lessons_reconcile import LessonReconcileMixin

    aborted = set(state.aborted_nodes or [])
    rows = [(nid, LessonReconcileMixin._node_sig(node, aborted=nid in aborted))
            for nid, node in sorted(state.nodes.items())]
    return hashlib.sha256(orjson.dumps(rows, option=orjson.OPT_SORT_KEYS)).hexdigest()


def concept_tags_required(settings) -> bool:
    """Any enabled concept producer/consumer requires authored tags in external mode."""
    return bool(settings.external_harness and (
        settings.concept_pivot or settings.concept_run_base or settings.cross_run_concepts))


def candidate_concepts(idea, state, parent_ids: list[int]) -> set[str]:
    """Resolve the proposed full/delta membership against the same run DAG base."""
    from looplab.core.concepts import normalize_concept_id

    if idea.concept_mode != "delta":
        return {normalize_concept_id(item) for item in idea.concepts}
    if not parent_ids:
        if state.run_base_concept_receipt is not None:
            return set()
        inherited = set(state.run_base_concepts)
    else:
        receipts = state.node_concept_materialization_receipts
        if any(receipts.get(pid) is not None for pid in parent_ids):
            return set()
        inherited = set().union(*(state.node_concepts.get(pid, []) for pid in parent_ids))
    return ((inherited - {normalize_concept_id(item) for item in idea.concepts_removed})
            | {normalize_concept_id(item) for item in idea.concepts_added})


_OUTCOME_EVENTS = frozenset({"node_evaluated", "node_failed", "node_reset", "node_abort"})


def _last_seq(events, types: frozenset[str]) -> int:
    return max((event.seq for event in events if event.type in types), default=-1)


def research_due(settings, state, events=None) -> bool:
    """The enabled opening/periodic research must precede the next candidate."""
    from looplab.engine.cadence import deep_research_window

    if not settings.external_harness or settings.deep_research_every < 0:
        return False
    n = len(state.nodes)
    window = deep_research_window(settings.deep_research_every)
    if n and n % window:
        return False
    if not any(row.get("at_node") == n for row in state.research
               if isinstance(row, dict)):
        return True
    if events is not None:
        latest_memo = max((event.seq for event in events
                           if event.type == "research_completed"
                           and event.data.get("at_node") == n), default=-1)
        return latest_memo < _last_seq(events, _OUTCOME_EVENTS)
    return False


def final_report_due(settings, state, events=None) -> bool:
    """An enabled narrative must cover the candidate count at finish."""
    if not (settings.external_harness and settings.report_every > 0 and state.nodes):
        return False
    if not state.report or state.report.get("at_node") != len(state.nodes):
        return True
    if events is not None:
        latest_report = max((event.seq for event in events
                             if event.type == "report_generated"
                             and event.data.get("at_node") == len(state.nodes)), default=-1)
        return latest_report < _last_seq(events, _OUTCOME_EVENTS)
    return False


def run_obligations(task, settings, *, generation: str) -> dict:
    external = bool(settings.external_harness)
    repo_spec = task.repo_spec() if callable(getattr(task, "repo_spec", None)) else None
    declared = getattr(getattr(task, "eval", None), "stages", None) or []
    from looplab.harness.reviews import required_reviews
    review_rules = {
        phase: {"required": True, "settings": {key: getattr(settings, key) for key in keys},
                "checkpoint": "run_finish_when_candidates_exist",
                "proof": "review receipt with recorded action reference or reason no action applies",
                "enforced": True, "action_validation": "domain write uses its own guarded API"}
        for phase, keys in required_reviews(settings).items()
    }
    # A live run may be past its opening turn. This static policy is paired with
    # the current node count at admission; the client can always re-read state.
    idea_rules = {
        "novelty": {"required": bool(external and (settings.novelty_mode != "off" or settings.novelty_gate)),
                    "settings": {"novelty_mode": settings.novelty_mode,
                                 "novelty_gate": settings.novelty_gate},
                    "checkpoint": "candidate_admission", "proof": "idea-bound submit review receipt",
                    "enforced": True},
        "foresight": {"required": bool(external and settings.foresight and settings.foresight_panel > 1),
                      "settings": {"foresight": settings.foresight,
                                   "foresight_panel": settings.foresight_panel},
                      "checkpoint": "candidate_admission", "proof": "review at least configured panel size",
                      "enforced": True},
        "candidate_ranking": {"required": bool(external and settings.best_of_n > 1),
                              "settings": {"best_of_n": settings.best_of_n},
                              "checkpoint": "candidate_admission", "proof": "review distinct complete implementations; selected code/files must match admission",
                              "enforced": True},
        "strategy": {"required": bool(external),
                     "settings": {"strategist_every": settings.strategist_every},
                     "checkpoint": "candidate_admission_at_configured_cadence",
                     "proof": "idea-bound strategy review receipt", "enforced": True},
    }
    return {
        "version": 1,
        "generation": generation,
        "reasoning_owner": "external_agent" if external else "looplab",
        "agent_choices": [phase.id for phase in PHASES],
        "choice_policy": {
            "default": "configured",
            "meaning": "Enabled settings impose phase obligations; optional phases may be skipped only when their policy permits it.",
            "concept_tags": ("required for each submitted candidate" if concept_tags_required(settings)
                             else "optional"),
            "novelty": "advisory preview; the agent decides to submit, revise or discard",
            "lessons_and_skills": "when reflection_priors is enabled, review both; publish only evidence-backed conclusions and portable techniques",
        },
        "phase_obligations": {
            **idea_rules,
            **review_rules,
            "research": {
                "required": bool(external and settings.deep_research_every >= 0),
                "settings": {"deep_research_every": settings.deep_research_every},
                "checkpoint": "before_first_candidate_and_each_due_research_window",
                "proof": "research_completed memo stamped at current node count",
                "enforced": True,
            },
            "hypothesis_board": {
                "required": bool(external and settings.track_hypotheses),
                "settings": {"track_hypotheses": settings.track_hypotheses},
                "checkpoint": "candidate_admission",
                "proof": "idea.hypothesis or link to an existing Card",
                "enforced": True,
            },
            "concept_tags": {
                "required": concept_tags_required(settings),
                "settings": {key: getattr(settings, key) for key in
                             ("concept_pivot", "concept_run_base", "cross_run_concepts")},
                "checkpoint": "candidate_admission",
                "proof": "nonempty effective full/delta membership on every injected candidate",
                "enforced": True,
            },
            "report": {
                "required": bool(external and settings.report_every > 0),
                "settings": {"report_every": settings.report_every},
                "checkpoint": "run_finish_when_candidates_exist",
                "proof": "report_generated covering current node count",
                "enforced": True,
            },
        },
        "enforced_on_candidate": {
            "ready_made": external,
            "source_policy": "The task's edit surface and protected files are checked at admission and build.",
            "evaluation": "LoopLab executes the trusted scorer and records the measured metric.",
            "operator_stages": [str(getattr(stage, "name", "") or
                                    (stage.get("name", "") if isinstance(stage, dict) else ""))
                                for stage in declared],
            "stage_policy": ("operator_declared" if declared else
                             "agent_may_propose_preceding_stages_within_edit_surface" if repo_spec
                             else "task_evaluation_only"),
            "edit_surface": list(repo_spec.get("edit_surface") or []) if repo_spec else None,
            "protected_names": list(repo_spec.get("protected_names") or []) if repo_spec else None,
        },
        "enforced_on_knowledge_write": {
            "lesson": "terminal node evidence, current generation and stable action_id",
            "skill": "fresh supported lesson evidence and a portable technique; cross-task promotion is server-derived",
            "concept_governance": "portfolio identity, revision and governed action constraints",
        },
    }
