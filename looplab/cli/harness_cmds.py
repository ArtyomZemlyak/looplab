"""Machine-readable discovery for external agents controlling LoopLab."""
from __future__ import annotations

import json
from pathlib import Path

import typer

from looplab.cli import app
from looplab.core.config import DEVELOPER_BACKENDS, Settings
from looplab.harness.phases import phase_catalog


def harness_manifest(*, include_settings: bool = False) -> dict:
    """The versioned, stable entry point; schemas come from their actual owners."""
    result = {
        "protocol_version": 4,
        "mode": "external_harness_or_delegated_developer",
        "external_run_mode": {
            "launch": "looplab run CONFIG --out RUN_DIR --backend toy -s external_harness=true",
            "reasoning_owner": "Codex, Claude Code or another MCP client: proposal, novelty, stages, plan, implementation, repair, lesson writing and concept curation are agent decisions.",
            "engine_owner": "LoopLab: candidate admission, source snapshot, evaluation, metrics, budgets, durable control commands and replay.",
            "submission": "Submit ready-made code/files using inject_node through /api/runs/{run_id}/commands; set parent_id for a branch. A code-less proposal is refused in this mode.",
            "repair": "Inspect a failed node and submit a corrected ready-made candidate; no in-process triage or inline repair runs.",
            "stopping": "Pause or finalize through the durable command API. No automatic proposal or empty-search finalization runs in this mode.",
        },
        "developer_backends": list(DEVELOPER_BACKENDS),
        "decision_phases": phase_catalog(),
        "node_build": {
            "optional_agent_actions": [
                {"id": "stages", "purpose": "Declare repeatable prep/train/eval boundaries when needed; omit if the operator already declared stages or the scoring command is monolithic."},
                {"id": "plan", "purpose": "Break a complex edit into steps when that reduces risk; skip for a small change."},
                {"id": "implement", "purpose": "Edit only the task's allowed files and validate the result."},
                {"id": "repair", "purpose": "Inspect the failure and change the same candidate; do not report a fabricated metric."},
            ],
            "authority": "The external Developer may choose its own reasoning steps. LoopLab still validates the patch, owns evaluation and records node transitions.",
        },
        "knowledge": {
            "novelty": {"preview": "POST /api/runs/{run_id}/novelty-preview",
                        "decision": "When enabled, POST /api/runs/{run_id}/harness-decisions for the exact idea, submit/reject, reason and distinct alternatives. Admission requires an idea-bound submit receipt."},
            "lessons": {"read": "GET /api/memory and GET /api/cross-run/claims",
                        "write": "POST /api/runs/{run_id}/lessons with expected_generation, action_id, statement, outcome, role and terminal evidence node ids. The server stamps fingerprint, run UID and node outcome signatures; exact retries are idempotent.",
                        "finalize": "External runs never invoke LoopLab's internal lesson reflection; publish lessons before or after finalization."},
            "concepts": {"per_candidate": "Author idea.concepts or concept_mode plus concepts_added/concepts_removed on inject_node.",
                         "requirement": "When concept_pivot, concept_run_base or cross_run_concepts is enabled, every external candidate must have nonempty effective concepts; the intake enforces this. Otherwise tagging is optional.",
                         "per_node": "concept_tag_edited command, with node_generation",
                         "run_base": "run_concepts command",
                         "inspect": "GET /api/runs/{run_id}/concepts, GET /api/cross-run/atlas, GET /api/cross-run/concept-policy",
                         "deduplicate": "POST /api/cross-run/concept-merge (or alias-clear, split, split-clear, purge) with portfolio identity and observed governance revisions. External runs skip automatic steward and ratifier."},
            "claims": "Inspect GET /api/cross-run/claims; use claim-decide for governed meaning changes. Research claims and concept capsules are still deterministic run-end projections from authored evidence.",
            "skills": "POST /api/runs/{run_id}/skill-candidates with a current supported lesson_action_id and measured node evidence. The server derives candidate/promoted status from distinct task fingerprints. Enabled cross-run knowledge requires a final harness-reviews receipt with a justified no-action decision where appropriate.",
        },
        "obligations": "GET /api/runs/{run_id}/harness-contract returns effective choices and enforced evaluation constraints. Decision phases are available, not a mandatory sequence.",
        "live_checkpoints": "Poll GET /api/runs/{run_id}/harness-checkpoints?expected_generation=TOKEN while evaluations run. POST a verdict, reason and action_id to the same route. Checked stages wait before advancing; opened live observations must be answered before terminal. Abort is available only when the checkpoint grants it.",
        "interfaces": {
            "start": "looplab run CONFIG --out RUN_DIR --developer-backend codex",
            "resume": "looplab resume RUN_DIR",
            "inspect": "looplab inspect RUN_DIR",
            "replay": "looplab replay RUN_DIR",
            "stop": "looplab stop RUN_DIR --wait",
            "configuration": "looplab init; looplab run CONFIG -s KEY=VALUE; looplab run --help",
            "control_plane": "looplab ui; see docs/guide/api-reference.md for authenticated live control actions",
            "mcp": "looplab harness-mcp (stdio; pip install 'looplab[harness,ui]')",
        },
        "limits": [
            "External mode requires backend=toy and a live external agent to drive the run; the normal mode retains its in-process Researcher and other roles.",
            "Legacy fork, forced ablation, deep-research and propose/implement node resets are refused in external mode; use a ready-made child candidate instead.",
            "Codex and Claude use their own CLI authentication and model configuration; LoopLab does not pass its LLM key or developer_model into them.",
            "An external agent's file changes are accepted only through the existing edit-surface and validation gate.",
        ],
    }
    if include_settings:
        result["settings_schema"] = Settings.model_json_schema()
        curated = Path(__file__).resolve().parents[1] / "serve" / "settings_ui_schema.json"
        result["settings_help"] = json.loads(curated.read_text(encoding="utf-8"))
    return result


@app.command(name="harness")
def harness(
    settings: bool = typer.Option(False, "--settings", help="Include full Settings JSON Schema and curated field help."),
) -> None:
    """Print the external-agent capability contract as JSON (read-only)."""
    typer.echo(json.dumps(harness_manifest(include_settings=settings), ensure_ascii=False))


@app.command(name="harness-mcp")
def harness_mcp() -> None:
    """Serve all live UI API operations to a coding agent over stdio MCP.

    Requires `pip install 'looplab[harness,ui]'` and a running `looplab ui`.
    Set LOOPLAB_HARNESS_URL for another local/proxied UI and LOOPLAB_UI_TOKEN if secured.
    """
    from looplab.harness.mcp_server import run_stdio
    run_stdio()
