"""Effective agent choices and evaluator obligations for a launched run.

The catalog describes what an agent *can* do. This projection describes what
LoopLab actually checks for this task. Keep admission and evaluation rules in
their owning services; this is their agent-readable explanation, not a second
validator that can drift into accepting a weaker candidate.
"""
from __future__ import annotations

from looplab.harness.phases import PHASES

# Operator policy for a launched external run. The MCP bridge uses the owner's
# HTTP token, so changing these fields through per-run config would otherwise
# let the agent remove its own admission/finish requirements. Other tuning
# fields still apply on resume. The agent can switch the search policy through
# set_strategy; that choice is tactical, while enabled reviews remain mandatory.
EXTERNAL_POLICY_FIELDS = frozenset({
    "concept_pivot", "concept_run_base", "cross_run_concepts", "track_hypotheses",
    "deep_research_every", "novelty_mode", "novelty_gate", "novelty_epsilon",
    "novelty_semantic", "novelty_semantic_threshold", "foresight", "foresight_panel",
    "foresight_alternatives",
    "foresight_min_confidence", "foresight_verify", "foresight_verify_samples",
    "foresight_agentic", "best_of_n", "best_of_n_listwise", "strategist_every",
    "report_every", "reflection_priors", "memory_dir", "lessons_every",
    "comparative_lessons", "cross_run_curation", "task_facets_finalize",
    "concept_tidy", "select_verifier", "select_verifier_samples",
    "mcts_value_weight", "train_monitor", "train_monitor_kill", "asha_live",
    "asha_live_kill", "coverage_context", "concept_retag_every",
    "stage_check_tools", "train_monitor_interval_s", "asha_live_min_siblings",
    "eval_deadline_grace_s",
})


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


def run_base_due(settings, state) -> bool:
    """Seed the enabled shared concept base once evidence from a scored node exists."""
    from looplab.core.concepts import (CONCEPT_DELTA_MISSING_RUN_BASE_REASON,
                                       normalized_concept_materialization_receipt)
    from looplab.core.models import NODE_CONCEPT_PROVENANCE_AUTHORED

    if not (settings.external_harness and settings.concept_run_base):
        return False
    receipt = normalized_concept_materialization_receipt(state.run_base_concept_receipt)
    if state.run_base_concepts or (state.run_base_concept_receipt is not None
                                   and (receipt is None or
                                        CONCEPT_DELTA_MISSING_RUN_BASE_REASON not in receipt["reasons"])):
        return False
    provenance = getattr(state, "node_concept_provenance", None) or {}
    return any(state.node_concepts.get(node.id)
               and provenance.get(node.id) == NODE_CONCEPT_PROVENANCE_AUTHORED
               for node in state.evaluated_nodes())


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


def report_cadence_due(settings, state, events=None) -> bool:
    """Require a narrative at the same node interval as the built-in writer."""
    if not (settings.external_harness and settings.report_every > 0
            and state.evaluated_nodes()):
        return False
    n = len(state.nodes)
    last = (state.report or {}).get("at_node") or 0
    if n - last < settings.report_every and n != last:
        return False
    return final_report_due(settings, state, events)


def external_finish_due(rd, settings, state, events) -> dict:
    """One live preflight for every external finish writer, after evaluation drains.

    This must also guard the CLI and engine budget paths, which do not pass
    through HTTP's ``run_abort`` normalizer. A pending candidate can still
    change measured evidence, invalidating an earlier report or review.
    """
    if not settings.external_harness:
        return {"report": False, "reviews": [], "pending_nodes": []}
    from looplab.harness.reviews import missing_reviews
    from looplab.events.run_generation import run_generation_token

    return {
        "report": final_report_due(settings, state, events),
        "reviews": missing_reviews(rd, settings, state, run_generation_token(events))
        if state.run_uid else [],
        "pending_nodes": sorted(node.id for node in state.nodes.values()
                                if node.status == "pending" and not node.tombstoned
                                and node.id not in (state.aborted_nodes or [])),
    }


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
        "delegated_semantics": {
            "novelty": "An Idea-bound decision is mandatory; deterministic novelty-preview is advice. The external agent may submit a near duplicate; the built-in LLM/algo veto does not run.",
            "foresight": "Panel size is mandatory. The agent's choice is recorded; min_confidence, verifier samples and agentic tool use are not independently measured or enforced.",
            "candidate_ranking": "Distinct complete implementations and exact selected artifacts are mandatory. Built-in static filtering, confidence abstention and listwise tie break do not run.",
            "knowledge_review": "A receipt is mandatory where configured. A completed action reference exists in a domain ledger; no_applicable_action is an agent attestation, not independent semantic verification.",
        } if external else {},
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
                "proof": "nonempty idea.hypothesis (injected candidate mints its own Card)",
                "enforced": True,
            },
            "hypothesis_merge": {
                "required": bool(external and settings.track_hypotheses),
                "settings": {"track_hypotheses": settings.track_hypotheses},
                "checkpoint": "before_candidate_when_four_or_more_pure_beliefs_are_open",
                "proof": "atomic merge receipt or explicit no_merge review of the current board",
                "enforced": True, "conditional_on": "open pure-belief board size >= 4",
            },
            "selection_verifier": {
                "required": bool(external and settings.select_verifier),
                "settings": {"select_verifier": settings.select_verifier,
                             "select_verifier_samples": settings.select_verifier_samples},
                "checkpoint": "before_next_candidate_when_selector_tie_exists",
                "proof": "one complete evidence-bound sample set for each reachable tie",
                "enforced": True,
            },
            "value_estimate": {
                "required": bool(external and settings.mcts_value_weight > 0),
                "settings": {"policy": settings.policy,
                             "mcts_value_weight": settings.mcts_value_weight},
                "checkpoint": "before_next_candidate_when_mcts_value_weight_is_active",
                "proof": "one headroom judgment per current branch in the bounded batch",
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
            "concept_run_base": {
                "required": bool(external and settings.concept_run_base),
                "settings": {"concept_run_base": settings.concept_run_base},
                "checkpoint": "before_next_candidate_after_first_scored_authored_concepts",
                "proof": "run_concepts command seeds the shared base from measured authored tags",
                "enforced": True,
            },
            "coverage_snapshots": {
                "required": bool(external and (settings.coverage_context or settings.concept_pivot)),
                "settings": {"coverage_context": settings.coverage_context,
                             "concept_pivot": settings.concept_pivot,
                             "concept_retag_every": settings.concept_retag_every},
                "checkpoint": "configured_creation_cadences",
                "proof": "LoopLab derives breadth and concept coverage from recorded node outcomes and agent-authored tags",
                "enforced": True, "owner": "deterministic_harness",
            },
            "memory_cadence": {
                "required": bool(external and settings.reflection_priors and settings.memory_dir
                                 and settings.lessons_every > 0),
                "settings": {"reflection_priors": settings.reflection_priors,
                             "comparative_lessons": settings.comparative_lessons,
                             "lessons_every": settings.lessons_every},
                "checkpoint": "before_candidate_at_each_configured_node_window",
                "proof": "lesson and skill review receipts citing actual writes or explaining no action",
                "enforced": True,
            },
            "report": {
                "required": bool(external and settings.report_every > 0),
                "settings": {"report_every": settings.report_every},
                "checkpoint": "configured_node_interval_and_run_finish",
                "proof": "report_generated covering current node count and measured outcomes",
                "enforced": True,
            },
            "stage_check": {
                "required": bool(external),
                "settings": {"stage_check_tools": settings.stage_check_tools},
                "checkpoint": "after_each_checked_or_asserted_command_stage",
                "proof": "answer the pending harness-checkpoints question before the next stage",
                "enforced": True, "conditional_on": "resolved stage check or expect.assert",
            },
            "deadline_grace": {
                "required": bool(external and settings.eval_deadline_grace_s != 0),
                "settings": {"eval_deadline_grace_s": settings.eval_deadline_grace_s},
                "checkpoint": "command_stage_reaches_its_time_limit",
                "proof": "answer extend or stop; the command may keep running during the unanswered wait, which has no automatic timeout; one capped extension starts after extend is consumed",
                "enforced": True, "conditional_on": "a command evaluation reaches its deadline",
            },
            "train_monitor": {
                "required": bool(external and settings.train_monitor),
                "settings": {"train_monitor": settings.train_monitor,
                             "train_monitor_kill": settings.train_monitor_kill,
                             "train_monitor_interval_s": settings.train_monitor_interval_s},
                "checkpoint": "changed_attributed_live_log_at_configured_cadence_or_first_final_log",
                "proof": "answer each opened observation before terminal; watch requests another look, not abort permission; current-attempt loss and completed training artifacts can remove authority, so read each question's kill_enabled",
                "enforced": True, "conditional_on": "command evaluation produces an attributed live log",
            },
            "asha_live": {
                "required": bool(external and settings.asha_live),
                "settings": {"asha_live": settings.asha_live,
                             "asha_live_kill": settings.asha_live_kill,
                             "asha_live_min_siblings": settings.asha_live_min_siblings},
                "checkpoint": "intermediate_objective_with_enough_finished_siblings",
                "proof": "answer each rank observation before terminal; abort requires watch and three consecutive same-rung underperforming checks in the current stage, and is disabled after objective retarget, including an already open question",
                "enforced": True, "conditional_on": "metric reader yields an intermediate sample",
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
