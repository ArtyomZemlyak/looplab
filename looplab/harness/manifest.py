"""The external-agent capability manifest: the versioned, machine-readable contract an MCP client
or a coding agent reads first (`looplab harness`, the `harness-mcp` server's discovery tool).

It lives in the HARNESS package, not in `cli/harness_cmds.py` where it was written: the MCP server
(`harness/mcp_server.py`) serves it too, and importing it from the CLI pulled Typer and every
command group into a library module — the one edge `tests/test_package_layering.py` refuses at any
level (`cli` is the process entry). `cli/harness_cmds.py` re-exports it as the SAME object.
"""
from __future__ import annotations

import json
from pathlib import Path

from looplab.core.config import DEVELOPER_BACKENDS, Settings
from looplab.harness.phases import phase_catalog


def harness_manifest(*, include_settings: bool = False) -> dict:
    """The versioned, stable entry point; schemas come from their actual owners."""
    result = {
        "protocol_version": 4,
        "mode": "external_harness_or_delegated_developer",
        "external_run_mode": {
            "launch": "looplab run CONFIG --out RUN_DIR --backend toy -s external_harness=true",
            "credential": "Use distinct LOOPLAB_HARNESS_TOKEN; scoped agent requests cannot edit /api/settings or the owner's prompts, skills and knowledge, launch, reset, purge or delete runs, write a run's chat log, clear a node trace, resolve a stuck command's activity claim, abandon the owner's lens or scope actions, revoke a share link, delete a project or super-task, or invoke LoopLab's owner model workflows; ask the operator for those. On a run not launched with external_harness it may only pause, annotate or add a comment (a hint reads to every role as the operator's directive, so it is refused too); every other command, a retry of one and a config edit are refused with agent_token_refused, and the commands it may submit are marked submitted_by=agent_token. Legacy LOOPLAB_UI_TOKEN is full owner authority.",
            "reasoning_owner": "Codex, Claude Code or another MCP client: proposal, novelty, stages, plan, implementation, repair, lesson writing and concept curation are agent decisions.",
            "engine_owner": "LoopLab: candidate admission, source snapshot, evaluation, metrics, budgets, durable control commands and replay.",
            "submission": "Submit ready-made code/files using inject_node through /api/runs/{run_id}/commands; set parent_id for a branch. A code-less proposal is refused in this mode.",
            "task_snapshot_read": "Read the launched task via GET /api/runs/{run_id}/artifact?root=run&path=task.snapshot.json&expected_generation=TOKEN; obtain TOKEN from /state. GET /api/runs/{run_id}/config reads its settings.",
            "next_step_read": "MCP run_progress(run_id, expected_generation) reads the compact next step, expansion/finish gates and source health. execution separates recorded node activity from the last-read engine lock probe; agent connection is not measured. It forwards one GET harness-progress?brief=true; follow detail references for questions/history and refresh even without new events. Advice does not grant admission or restart work.",
            "live_discovery": "MCP operations and operation_schema each read authenticated live OpenAPI once, then select matching routes/referenced schemas. No retry or cached fallback. Transport loss returns status=null, code=api_unreachable, outcome=unavailable, at=openapi; non-200 preserves status with api_read_failed; malformed JSON/catalog returns response_incomplete at HTTP 200. Error bodies/URLs are omitted. A failed read is not an empty capability catalog; repeat discovery explicitly after recovery. The full catalog is not subject to the individual API-reply cap.",
            "remote_client_install": "Install .[harness] on the external MCP client; .[ui] is needed on the server machine. phase_info uses the UI-free protocol's canonical request/server-derived field tables, also imported by server validation. Remote metadata discovery needs no FastAPI and stdio does not run/import Uvicorn; the MCP SDK may install Uvicorn transitively. Client/server LoopLab versions should match for local metadata; operations/operation_schema read the live server catalog.",
            "connection_context": "UI Progress > Agent cycle > Connect external agent reads generation-fenced harness-handoff. It shows server paths, task constraints, Codex/Claude/generic MCP configs and a run instruction. Supply the scoped secret separately; scope is server-wide. harness-mcp never falls back to the owner token. Project server approval and tool-call approval are separate client steps. Connected proves stdio only; exit 0 can include permission_denials. Inspect tool errors, HTTP status and receipts. connection_check(run_id, expected_generation) reads fenced context, source health and obligations; copying/checking starts no work.",
            "lost_response": "MCP transport loss returns status=null: writes have code=request_outcome_unknown/outcome=unknown, reads api_unreachable/outcome=unavailable. Writes with 5xx or invalid/oversized 2xx acknowledgements also have unknown outcome while preserving HTTP status; 200 alone does not prove durable acceptance. Typed progress/result/command reads verify generation and command receipt ID against the requested ID/original key. Different identity returns response_context_mismatch/unavailable even at HTTP 200; missing/malformed fields or oversized reads are unavailable too. Unbound bodies are omitted. No automatic retry is made. command_receipt observes saved status without worker restart. Preserve the exact body/key or action_id; missing receipts do not prove absence of action. GET /commands/{id} can restart a nonterminal worker; read state/events/checkpoints before recovery.",
            "completion_briefs": "After each terminal node and finalized run, call MCP result_notices(run_id, expected_generation, limit=50, cursor=null), or GET result-notices, and POST a short interpretation in the user's language using the exact receipt_id/evidence_token, summary (max 700 chars), and stable action_id. Typed MCP reads verify returned generation; inspect status/code/outcome before body. Follow next_cursor with the same generation for older current receipts; max 200 per page. One GET per call, no automatic paging/retry. Pages remain chronological, newest page first. Cursor binds run directory/generation and anchor evidence, not commentary; changed anchor returns 409 with refresh advice. Refresh the head after draining for newer completions. Scores are server-derived; extra metric/action/role fields are refused. Commentary appears beside automatic measured briefs in Assistant chat, executes no commands and replaces no report/checkpoint. Reads/reconnects create no duplicate briefs; stale commentary is withdrawn when evidence changes. Paging adds no engine gate or wait.",
            "repair": "An inject_node command receipt marked succeeded proves candidate admission, not training success. Inspect the terminal node and result_notices; a failed node has no completed score. Read node detail with expected_generation and bounded node logs with expected_generation/current attempt. Retry the original body/key only to recover its receipt; corrected files need a new key and ready-made child with parent_id and parent_generations. Inspect measured evidence after evaluation; a failed parent cannot support a score delta. Explicit finish remains available. No in-process triage or inline repair runs.",
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
        "obligations": "GET /api/runs/{run_id}/harness-contract returns fixed run obligations and delegated_semantics. Enabled settings impose admission/finish gates; optional phases have no fixed sequence. Change obligation policy in a new run.",
        "progress": "GET /api/runs/{run_id}/harness-progress?expected_generation=TOKEN returns current expansion/finish gates and separate paged histories for decisions, reviews and answered or pending checkpoints. Inspect source_health for the event log and all three sidecars; refresh after event_seq changes. Incomplete decision/review sources refuse acknowledgement, writes and dependent obligations; operator recovery is required, with no automatic repair or resume. Exact decision/review body/action retries return original at_node/evidence_revision receipts, not approval of a new window. Check validity; superseded receipts require fresh justified actions. Conflicting authored content/generation is refused. HTTP 200 command receipts can be rejected; inspect status/error. Commands and measured outcomes are in the event timeline; private unsubmitted agent reasoning is not persisted.",
        "agent_activity": "Progress also exposes process-local activity from successful progress reads authenticated with the harness credential. Owner/browser reads do not refresh it. After 120 seconds it is quiet, not proven dead. UI restart, cache eviction or generation change has no inherited observation. This signal starts no work, imposes no gate, and never proves a thinking/connected agent or triggers takeover.",
        "live_checkpoints": "Poll GET /api/runs/{run_id}/harness-checkpoints?expected_generation=TOKEN while evaluations run. POST a verdict, reason and action_id to the same route. Checked stages wait before advancing; opened live observations must be answered before terminal. A command reaching its deadline opens deadline_grace when enabled: answer extend or stop. While unanswered, the command may keep running with no automatic timeout; the one-time cap starts after extend is consumed. Abort is available only when the monitor checkpoint grants it.",
        "hypothesis_merge": "When four or more pure-belief Cards are open, GET /api/runs/{run_id}/harness-hypotheses and POST an atomic merge or justified no_merge review before the next candidate.",
        "selection": "GET /api/runs/{run_id}/harness-selection. If select_verifier is enabled and a tie exists, POST complete evidence-bound samples to /harness-selection/verify. If MCTS value_weight is active, POST all current batch estimates to /harness-selection/values. Both are admission gates when due.",
        "memory_cadence": "With reflection_priors and lessons_every enabled, review skill_candidates at each node window; also review lessons there when comparative_lessons is enabled. Both reviews are due at finish. New evidence invalidates current-window reviews.",
        "report_cadence": "When report_every is enabled, publish report_generated at each configured node interval before the next candidate, and cover the latest candidate before finishing.",
        "interfaces": {
            "start": "looplab run CONFIG --out RUN_DIR --developer-backend codex",
            "resume": "looplab resume RUN_DIR",
            "inspect": "looplab inspect RUN_DIR",
            "replay": "looplab replay RUN_DIR",
            "stop": "looplab stop RUN_DIR --wait",
            "configuration": "looplab init; looplab run CONFIG -s KEY=VALUE; looplab run --help",
            "control_plane": "looplab ui; see docs/guide/api-reference.md for authenticated live control actions",
            "mcp": "looplab harness-mcp (stdio; pip install 'looplab[harness,ui]'; use a distinct LOOPLAB_HARNESS_TOKEN)",
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
