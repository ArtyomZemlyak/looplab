"""One public vocabulary for LoopLab decisions in either orchestration mode.

The built-in mode invokes the named owners below. In an externally driven run the
coding agent reads the same durable entities and submits the listed domain actions.
This is a workflow contract, not a second event writer: commands still pass through
the server's validation, authorization, generation fence and replay projections.

Prompt keys are assigned here rather than reproduced in MCP prose. The completeness
assertion makes new PromptStore phases visible to external clients on the same commit.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from looplab.core.prompts import PROMPT_KEYS, UNGOVERNED_PROMPT_FAMILIES


@dataclass(frozen=True)
class Phase:
    id: str
    entity: str
    internal: str
    purpose: str
    reads: tuple[str, ...]
    writes: tuple[str, ...]
    prompts: tuple[str, ...] = ()
    legacy_prompt_family: str = ""

    def public(self) -> dict:
        row = asdict(self)
        for key in ("reads", "writes", "prompts"):
            row[key] = list(row[key])
        # This index covers both orchestration modes. An agent using the scoped
        # harness credential can inspect global settings, the task launch surface
        # and the authoring stores, but cannot mutate operator defaults — settings,
        # prompts, skills, knowledge — or start another run (`serve/server.py`'s
        # harness deny list; critic crit_v62 F2: the authoring write read as the
        # agent's after the middleware had closed it).
        row["write_access"] = {
            ref: ("operator" if ref in OPERATOR_WRITES else "external_agent")
            for ref in self.writes
        }
        row["obligation"] = "see GET /api/runs/{run_id}/harness-contract and /harness-progress"
        return row


# The writes the scoped harness credential may not make (`tests/test_agent_token_scope.py` drives
# each against the middleware, and every other HTTP write of the index through it).
OPERATOR_WRITES = frozenset({"PUT /api/settings", "POST /api/start",
                             "POST /api/runs/{run_id}/upstream/recover",
                             "POST /api/runs/{run_id}/resolve-activity-claims",
                             "PUT /api/{kind}/{name}/operations/{operation_id}"})


# A write of "command:NAME" means POST /api/runs/{run_id}/commands with
# {type: NAME, data: ..., expected_generation: ...} and a unique Idempotency-Key.
# The other entries are HTTP method + OpenAPI path. They are actual existing routes,
# not proposed tools. A phase can be skipped if its analysis is redundant; the
# evaluator and its recorded metrics are never replaced by an agent's judgement.
PHASES: tuple[Phase, ...] = (
    Phase("upstream", "Capability/BaseRevision", "agents/maintainer.py; engine/upstream.py",
          "Generalize measured reusable hunks into a run-owned worktree; keep recipes separate and the old flag default. Pause and wait for engine exit; buy real full-source equivalence plus declared artifact regression/repair-trigger checks; inspect the verdict; advance with explicit current-evidence CAS, then resume separately. A crashed claim needs operator abandonment and a fresh action. Never writes or pushes the owner repository.",
          ("GET /api/runs/{run_id}/upstream", "GET /api/runs/{run_id}/upstream/requests/{proposal_id}",
           "GET /api/runs/{run_id}/state"),
          ("POST /api/runs/{run_id}/upstream/proposals", "POST /api/runs/{run_id}/upstream/check",
           "POST /api/runs/{run_id}/upstream/advance", "POST /api/runs/{run_id}/upstream/recover")),
    Phase("recovery", "Run/CommandReceipt", "serve/command_receipt.py; serve/run_commands.py",
          "Reconnect to the same run; observe a saved receipt before explicitly choosing recovery. The stdio MCP client can recover original command bodies/keys through saved_commands and saved_command; local intent is not a server verdict. An idle run still needs engine ownership before another candidate. An inconclusive lock probe does not prove death; reads never resume. Agent liveness is not measured.",
          ("GET /api/runs/{run_id}/state", "GET /api/runs/{run_id}/command-receipt",
           "GET /api/runs/{run_id}/harness-progress", "GET /api/runs/{run_id}/harness-checkpoints"),
          ("command:pause", "command:resume", "command:run_abort",
           "POST /api/runs/{run_id}/commands/{command_id}/retry",
           "POST /api/runs/{run_id}/resolve-activity-claims")),
    Phase("configuration", "Settings/PromptBundle", "core/config.py; core/prompts.py; serve/routers/misc.py",
          "Inspect and tune run settings and hot-reloaded role prompts.",
          ("GET /api/settings", "GET /api/runs/{run_id}/config",
           "GET /api/runs/{run_id}/artifacts", "GET /api/runs/{run_id}/artifact",
           "GET /api/{kind}"),
          ("PUT /api/runs/{run_id}/config", "PUT /api/settings",
           "PUT /api/{kind}/{name}/operations/{operation_id}")),
    Phase("agent_knowledge", "Skill/KnowledgeFile", "tools/skills.py; serve/authoring_store.py",
          "Manage editable skills and knowledge documents with revision-fenced writes (the "
          "operator's), and propose a skill from a measured lesson (the agent's).",
          ("GET /api/{kind}",),
          ("PUT /api/{kind}/{name}/operations/{operation_id}",
           "POST /api/runs/{run_id}/skill-candidates")),
    Phase("genesis", "TaskSpec", "engine/genesis.py::author_task",
          "Design a task and its evaluation contract before launching a run.",
          ("GET /api/runs",), ("POST /api/start",), legacy_prompt_family="genesis"),
    Phase("onboarding", "EvalSpec", "agents/established.py / agents/factory.py",
          "Review the task's scoring setup and any required human approval.",
          ("GET /api/runs/{run_id}/config", "GET /api/runs/{run_id}/artifact"),
          ("command:spec_approved",),
          ("repo_onboarder_system",)),
    Phase("research", "ResearchMemo", "agents/deep_research.py; engine/research_cadence.py",
          "Synthesize measured results, literature, claims and directions.",
          ("GET /api/runs/{run_id}/state", "GET /api/runs/{run_id}/nodes/{nid}",
           "GET /api/cross-run/claims"), ("command:research_completed",),
          ("deep_research_system",)),
    Phase("hypothesis_board", "Hypothesis/Card", "engine/research_cadence.py",
          "Keep open questions and prioritized cards attached to the measured work.",
          ("GET /api/runs/{run_id}/state",),
          ("command:hypothesis_added", "command:hypothesis_updated", "command:card_edited",
           "command:card_reprioritized", "command:card_dropped", "command:card_reopened")),
    Phase("proposal", "Idea", "agents/agent.py::propose; agents/unified_agent.py::propose",
          "Choose a change and parent candidates, including question and concept links.",
          ("GET /api/runs/{run_id}/state", "GET /api/runs/{run_id}/concepts",
           "GET /api/memory"), ("command:inject_node",),
          ("researcher_system", "tool_researcher_system", "tool_researcher_alternative")),
    Phase("novelty", "Idea/NoveltyGrade", "engine/novelty.py; search/novelty_recall.py",
          "Compare an idea with prior attempts; decide to revise, skip or submit.",
          ("POST /api/runs/{run_id}/novelty-preview", "GET /api/runs/{run_id}/state"),
          ("POST /api/runs/{run_id}/harness-decisions", "command:inject_node")),
    Phase("foresight", "IdeaRanking", "search/foresight.py",
          "Rank candidate ideas by expected payoff.",
          ("GET /api/runs/{run_id}/state",),
          ("POST /api/runs/{run_id}/harness-decisions", "command:inject_node"),
          ("foresight_system",)),
    Phase("candidate_ranking", "CandidateBatch", "search/best_of_n.py",
          "Compare multiple complete candidate implementations before admission.",
          ("GET /api/runs/{run_id}/state",),
          ("POST /api/runs/{run_id}/harness-decisions", "command:inject_node"),
          ("bestofn_judge_system",)),
    Phase("strategy", "SearchPolicy", "agents/strategist.py; engine/plan.py",
          "Choose policy, budget, width, or fidelity using the current evidence.",
          ("GET /api/runs/{run_id}/state", "GET /api/runs/{run_id}/config",
           "GET /api/runs/{run_id}/harness-progress"),
          ("POST /api/runs/{run_id}/harness-decisions", "command:set_strategy", "command:budget_extend",
           "PUT /api/runs/{run_id}/config"),
          ("strategist_system", "tool_strategist_system")),
    Phase("stages", "StageSpec", "adapters/repo_developer.py",
          "Design prep/train/eval stages when the task benefits from them.",
          ("GET /api/runs/{run_id}/config",), ("command:inject_node",),
          ("repo_developer_system_intro", "repo_developer_system_body")),
    Phase("implementation", "Node/SourceSnapshot", "agents/roles.py::LLMDeveloper; adapters/repo_developer.py",
          "Implement and validate code within the task's edit surface.",
          ("GET /api/runs/{run_id}/config", "GET /api/runs/{run_id}/artifacts"),
          ("command:inject_node",), ("developer_system",)),
    Phase("repair", "Node/Failure", "engine/crash_repair.py; adapters/repo_developer.py",
          "Diagnose a terminal failure: read node detail with expected_generation, then bounded logs with expected_generation and the current attempt. An accepted inject receipt is not training success. Exact retry does not repair code; submit a corrected child with a new key and parent generation, or explicitly finish. Never claim a delta against a failed parent without a measured score.",
          ("GET /api/runs/{run_id}/nodes/{nid}/logs", "GET /api/runs/{run_id}/nodes/{nid}"),
          ("command:inject_node",), ("developer_repair_prefix", "triage_system",
                                      "triage_look_invitation", "triage_findings_invitation",
                                      "repair_critic_system")),
    Phase("monitor", "StageLog/TrainingVerdict", "engine/train_monitor.py; engine/asha_monitor.py",
          "Inspect current-attempt evidence; watch before an authorized abort. Improving loss and completed artifacts can remove training-stop authority. ASHA requires three same-rung underperforming checks in the current stage; objective retarget removes stop authority, including an open question. Read current kill_enabled.",
          ("GET /api/runs/{run_id}/nodes/{nid}/logs", "GET /api/runs/{run_id}/state",
           "GET /api/runs/{run_id}/harness-checkpoints"),
          ("POST /api/runs/{run_id}/harness-checkpoints", "command:node_abort"),
          legacy_prompt_family="monitor"),
    Phase("deadline_grace", "StageDeadline", "engine/eval_stages.py; runtime/command_eval.py",
          "At a command deadline, answer extend or stop. While unanswered, the command may keep running with no automatic timeout; the one-time cap starts after extend is consumed.",
          ("GET /api/runs/{run_id}/harness-checkpoints", "GET /api/runs/{run_id}/nodes/{nid}/logs"),
          ("POST /api/runs/{run_id}/harness-checkpoints",)),
    Phase("evaluation", "Node/Metric", "engine/evaluate.py; engine/eval_stages.py",
          "Answer mandatory inter-stage checks; inspect metric provenance and trust signals.",
          ("GET /api/runs/{run_id}/nodes/{nid}/metrics",
           "GET /api/runs/{run_id}/nodes/{nid}/logs",
           "GET /api/runs/{run_id}/harness-checkpoints"),
          ("POST /api/runs/{run_id}/harness-checkpoints", "command:node_reset",
           "command:node_abort")),
    Phase("confirmation", "Node/RepeatedEvidence", "engine/confirm_phase.py; engine/upstream_workspace.py",
          "Repeat a terminal experiment with full-profile seeds. Retain its primary measured base and scientific implementation after upstream advancement; a new candidate lifecycle uses the current base. Forced confirmation records evidence without promoting a robust winner. Read current node generation, checkpoints and results; missing historical archives refuse before cleanup.",
          ("GET /api/runs/{run_id}/state", "GET /api/runs/{run_id}/nodes/{nid}",
           "GET /api/runs/{run_id}/harness-checkpoints"),
          ("command:force_confirm",)),
    Phase("concept_tags", "ConceptFrame", "engine/concept_cadence.py; search/concept_tagging.py",
          "Tag candidate and observed nodes, and maintain the run's base concepts.",
          ("GET /api/runs/{run_id}/concepts",),
          ("command:concept_tag_edited", "command:run_concepts", "command:inject_node")),
    Phase("hypothesis_merge", "HypothesisBoard", "search/hybrid_merge.py",
          "Consolidate near-duplicate hypotheses without losing their evidence.",
          ("GET /api/runs/{run_id}/state", "GET /api/runs/{run_id}/harness-hypotheses"),
          ("POST /api/runs/{run_id}/harness-hypotheses",), ("merge_system",)),
    Phase("selection_verifier", "SelectorTie", "engine/verifier_tiebreak.py",
          "When enabled, judge the complete selector tie against the realized results.",
          ("GET /api/runs/{run_id}/harness-selection", "GET /api/runs/{run_id}/state"),
          ("POST /api/runs/{run_id}/harness-selection/verify",)),
    Phase("value_estimate", "MCTSBranch", "engine/value_estimate.py",
          "When MCTS branch weighting is enabled, estimate remaining headroom from outcomes.",
          ("GET /api/runs/{run_id}/harness-selection", "GET /api/runs/{run_id}/state"),
          ("POST /api/runs/{run_id}/harness-selection/values",)),
    Phase("concept_merge", "ConceptGraph", "search/concept_map.py; engine/concept_steward.py",
          "Consolidate concept vocabulary and govern aliases/splits.",
          ("GET /api/cross-run/atlas", "GET /api/cross-run/concept-policy"),
          ("POST /api/cross-run/concept-merge", "POST /api/cross-run/concept-split",
           "POST /api/cross-run/concept-alias-clear", "POST /api/cross-run/concept-purge",
           "POST /api/runs/{run_id}/harness-reviews"),
          ("concept_consolidate_system",), legacy_prompt_family="steward"),
    Phase("concept_ratification", "ConceptProposal", "engine/concept_tidy.py",
          "Review proposed merges and apply only governed, supported changes.",
          ("GET /api/cross-run/atlas", "GET /api/cross-run/concept-policy"),
          ("POST /api/cross-run/concept-merge", "POST /api/runs/{run_id}/harness-reviews")),
    Phase("claim_curation", "ClaimLedger", "engine/claim_steward.py",
          "Inspect claims and record governed meaning decisions.",
          ("GET /api/cross-run/claims", "GET /api/cross-run/claim-curation-log"),
          ("POST /api/cross-run/claim-decide", "POST /api/runs/{run_id}/harness-reviews"),
          legacy_prompt_family="steward"),
    Phase("task_facets", "TaskFacetLedger", "engine/task_facets.py; engine/curation_protocol.py",
          "Classify the task by domain, language, modality, interaction and objective.",
          ("GET /api/cross-run/task-facets",),
          ("POST /api/cross-run/task-facets", "POST /api/runs/{run_id}/harness-reviews")),
    Phase("lessons", "Lesson/Skill", "engine/lessons_distill.py; engine/lessons_reconcile.py",
          "Publish evidence-bound cross-run lessons; stale evidence is reconciled on replay.",
          ("GET /api/memory", "GET /api/runs/{run_id}/state"),
          ("POST /api/runs/{run_id}/lessons", "POST /api/runs/{run_id}/harness-reviews")),
    Phase("skill_candidates", "AutoSkill", "engine/memory.py::write_auto_skill",
          "Draft a reusable technique from a supported, evidence-linked lesson; promotion requires distinct task fingerprints.",
          ("GET /api/memory", "GET /api/runs/{run_id}/state"),
          ("POST /api/runs/{run_id}/skill-candidates", "POST /api/runs/{run_id}/harness-reviews")),
    Phase("result_summary", "CompletionReceipt", "serve/result_notices.py",
          "After each terminal node and finalized run, read MCP result_notices and publish a brief interpretation in Assistant chat using measured evidence; on reconnect pass next_cursor for older receipts.",
          ("GET /api/runs/{run_id}/result-notices", "GET /api/runs/{run_id}/state",
           "GET /api/runs/{run_id}/harness-checkpoints"),
          ("POST /api/runs/{run_id}/result-notices",)),
    Phase("report", "RunReport", "serve/report.py; engine/research_cadence.py",
          "Write a narrative report over the measured run.",
          ("GET /api/runs/{run_id}/state",), ("command:report_generated",),
          legacy_prompt_family="report"),
    Phase("pilot", "NextAction", "agents/unified_agent.py; agents/strategist.py",
          "Decide which admissible action to take next, or finish.",
          ("GET /api/runs/{run_id}/state",),
          ("command:inject_node", "command:pause", "command:run_abort"),
          ("pilot_system",)),
    Phase("assistant", "OperatorIntent", "serve/routers/assistant.py; serve/routers/boss.py",
          "Translate a natural-language request into the same guarded domain actions.",
          ("GET /api/runs/{run_id}/state",),
          ("POST /api/runs/{run_id}/commands",), legacy_prompt_family="assistant"),
)

assert len({phase.id for phase in PHASES}) == len(PHASES)
assert {key for phase in PHASES for key in phase.prompts} == set(PROMPT_KEYS), (
    "every overridable prompt must be assigned to an external-visible decision phase")
assert {phase.legacy_prompt_family for phase in PHASES if phase.legacy_prompt_family} == {
    family for family, _location in UNGOVERNED_PROMPT_FAMILIES}


def phase_catalog(query: str = "") -> list[dict]:
    needle = query.casefold().strip()
    return [phase.public() for phase in PHASES
            if not needle or needle in (phase.id + " " + phase.entity + " " + phase.purpose).casefold()]


# Checkpoint kinds describe runtime verdicts; MCP phases describe decisions.
# Keep this bridge beside the catalog so discovery never advertises a phantom phase.
CHECKPOINT_DECISION_PHASES = {"stage_check": "evaluation", "train_monitor": "monitor",
                             "asha_live": "monitor", "deadline_grace": "deadline_grace"}
assert set(CHECKPOINT_DECISION_PHASES.values()) <= {phase.id for phase in PHASES}


def phase_detail(phase_id: str) -> dict | None:
    return next((phase.public() for phase in PHASES if phase.id == phase_id), None)
