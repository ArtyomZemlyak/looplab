---
name: looplab-research
description: Conduct or resume an external LoopLab research campaign using its live contract, evidence, and supported actions.
metadata:
  workflow_version: "1"
  contract_version: "1"
---

# LoopLab research

Use this skill when acting as the external researcher or executor for a LoopLab run. Begin with `capabilities` and `connection_check`, then read the current run state, task, config, `harness-contract` and `run_progress`. Verify run UID and generation; a saved handoff is context, not authorization to resume an old action. Read `phases` and `phase_info` for the current decision and inspect actual receipts before retrying.

Read the canonical workflow from `GET /api/runs/{run_id}/research-workflow` on the connected server (or `looplab/harness/external-research-workflow.md` in the source checkout) for scientific questions, evidence, comparisons, reporting and recovery. For live operations use the contract and tool schemas from the connected server; the guide does not grant budget, GPU, source or publication authority. Technical retry and score-only work should preserve their actual context without inventing research questions or results.

For literature and cross-source analysis invoke the sibling `looplab-deep-research` skill and actually perform its source→claim→comparison→experiment cycle. The internal Researcher does not run in external mode: the external agent authors research memos, question cards, experiment registrations, concepts, decisions, local lessons and reports. `backend=toy` means no internal model, not that no external research is required. Use `research_briefing.authoring_language` for every new human-readable label and section, including next actions; explicit run/project preferences take precedence over goal inference. Do not rewrite historical prose or translate measurements.

Keep train and its protected score in one staged scientific node whenever they evaluate one candidate. Separate score-only work only for a genuinely different scorer/policy or an existing producer, with explicit `model_producer` and comparison links. `job_role` describes prepare/profile/fit/score/recovery work independently of scientific `node_kind`; never make a scientific fit an artifact merely to hide a missing primary scalar. Artifact jobs are unranked and need no ranking scalar. Declare genuine secondary readers in `eval.metrics`; never manufacture a primary score.

Link future scientific work explicitly: direction `question_card_id`, candidate `parent_card_id`, scientific code lineage `parent_id`/`parent_ids`, and artifact dependencies `uses`. Do not expect LoopLab to invent missing scientific parentage from `uses`. Give experiments meaningful titles and stable concepts; an operator name is not a research question. If an optional link is genuinely inapplicable, explain why instead of fabricating a card.

After a meaningful terminal result or when a report is due, read `research_briefing.report_context`, the previous full overview and the report rubric in the canonical workflow. Author an evidence-linked cumulative synthesis as well as the current result without repeating the same prose in both. Publish only a material update, not a cadence-only near-duplicate. `key_metrics` contains references to real metrics and nodes, not authored numbers; when unavailable, explain the genuine cause in the saved report so its status is `explained_unavailable` rather than filling placeholders. After publication, read the saved version and fresh briefing, then review `report_fields_empty`, metric bindings, repetition warnings, references and meaning using the canonical rubric.

Record supported run-local lessons as research progresses. At configured knowledge reviews and finalization explicitly publish supported shared lessons/skill candidates or a reasoned no-action decision through the native phases. Run-local memos do not appear automatically in shared cross-run memory; an empty shared store is not evidence of absent research. Inspect actual receipts and the current store, never invent a lesson to make it nonempty.
