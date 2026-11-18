# External Work State Recovery

RESULT 2026-11-11 — isolated implementation and synthetic native/MCP/cold-process recovery
acceptance. This is not a live agent compaction, deployment or a measurement of research quality.

The existing diagnostic `agent_checkpointed` event now supports an optional bounded
`external_work_state`, authored through the existing generation-fenced/idempotent command protocol.
Internal phase checkpoints keep their old shape and are never inferred to contain external work.
No new event spine, daemon, launch gate, scheduler or model call is introduced.

## Fixture

Create an external session with a Harness Revision, one completed fit, pending CPU preparation with artifact
references, a saved but unacknowledged command, a current blocker/owner and several historical handoffs. Use
synthetic commands and artifacts only; no GPU, fit or real model call is needed.

The authored work-state projection contains the current goal, last completed step, next supported action,
blocker/owner, active work claims and unfinished artifact references. It contains no raw chain-of-thought,
credentials or full transcript and creates no second event spine. Existing saved commands/actions and source
receipts remain authoritative for those operations.

## Recovery cases

1. Compact/restart the external agent. It reconstructs the current goal and one next supported action without
   replaying historical notifications or duplicating fit/score.
2. A newer operator direction supersedes only the same topic. Independent pending work remains visible; old
   notifications do not renew authority.
3. The saved command has already been applied. Recovery reads its receipt instead of submitting another.
4. The external process and engine have different observations. The projection reports both and does not infer
   liveness or actual work from a read request.
5. The Harness Revision or artifact scope changed. The next action is withheld pending fresh gates rather than
   executed from the old work-state receipt.
6. A client restart changes the actual callable tool catalogue or loses a runtime alias target while some
   prepared artifacts survive. Recovery checks the live catalogue, exact API schema and resolved artifact
   identity before rebuilding or retrying. A separate MCP probe does not prove tools were installed into the
   host client. Unsupported guessed routes, missing local dependencies and task-helper schema errors remain
   attributed to their actual layer, not automatically to a failed LoopLab service.
7. A late reminder requests work already completed or superseded by a direct user correction. Its delivery
   is retained as history and cannot renew execution authority. An ACK proves receipt, not applied work.
   Client queue saturation and provider/tool approval states remain explicit external blockers; no second
   notification scheduler or automatic approval bypass is introduced.
8. The exact frozen input/ID artifact survives under a different resolved location. Recovery uses its pinned
   identity and reports missing evidence; it neither reruns a completed model nor silently substitutes another
   cohort. Producer-durable outputs and a native journal mirror are separate recovery references. Readback is
   not remote object-store flush verification.

## Acceptance

Recovery is bounded and auditable, preserves unfinished CPU work, and resumes decision-making rather than
execution. A work-state receipt is never execution authority: live progress, source health, protocol, budget
and attempt receipts are reread before any action.

## Implemented contract and limits

Submit `type:agent_checkpointed` with `data.external_work_state` containing a goal, last completed
step, next step, optional blocker/owner, declared harness revision, at most eight unfinished artifact
references and eight authored work claims. Optional saved command ID and saved client-action reference
preserve recovery links. The whole authored compact UTF-8 JSON document is limited to 8192 bytes,
with additional field limits. Unknown fields, including transcript/reasoning/credentials bags, are
refused; text is redacted before native persistence. References are metadata: the endpoint does not
open or hash model, data, credential or artifact paths. Unknown identity is never reported verified.

The server stamps UID, generation and a digest of observed direction, node lifecycle, objective,
budget and pinned strategy context. Progress and briefing return the latest explicit external
checkpoint with its source sequence and bounded notes. A changed observed context withholds the old
authored next-step suggestion, preserving the unfinished references. Harness revision, tool catalogue,
artifact identity and exclusive ownership remain declared or unverified, rather than falsely attested.
The only concrete next action is a supported GET of the original **pure command receipt** or fresh
progress. It never calls reconciliation, retries a command, probes an artifact or launches work.
Current tool catalogue, original saved actions and actual source/protocol/budget/attempt gates must
be read before execution. A failed latest checkpoint is unavailable, without silent fallback to an
older healthy one. Historical unscoped internal checkpoints are left uninvented.

`tests/test_external_work_state.py` publishes through the registered MCP API tool and actual HTTP
command service, restores the client's private saved intent and restarts the service. A separate cold
Python process reconstructs the same work from the native journal alone. The recovery reads an already
applied original receipt without resubmission; exact checkpoint retry adds no event. The synthetic
completed node remains the only node. Changed directions/attempts, malformed latest records, nested
canonical addresses, whole UTF-8 limits, missing artifact paths and invalid schema cases are exercised.
This demonstrates decision-state recovery, not automatic execution or external client installation.
