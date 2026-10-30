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

After a meaningful terminal result or when a report is due, read `research_briefing.report_context`, the previous full overview and the report rubric in the canonical workflow. Author an evidence-linked cumulative synthesis as well as the current result; never invent missing measurements or turn reporting advice into an execution gate.
