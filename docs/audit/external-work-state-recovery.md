# External Work State Recovery — protocol, no result

**Status:** protocol for the related open item in doc 83; no recovery acceptance has run. Internal
`agent_checkpointed` events do not, by themselves, capture an independently running external agent's work.

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

## Acceptance

Recovery is bounded and auditable, preserves unfinished CPU work, and resumes decision-making rather than
execution. A work-state receipt is never execution authority: live progress, source health, protocol, budget
and attempt receipts are reread before any action.
