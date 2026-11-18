# Scoped operator direction lifecycle

RESULT 2026-11-11 — isolated source and synthetic public-command/replay acceptance, no deployment claim.

Legacy `hint` events and `replace:true` keep their existing meaning. A new scoped hint uses
`directive_id`, `topic`, `status` (active/completed/cancelled), `text` and `supersedes` through the
existing generation-fenced, idempotent command endpoint. It is authored context, not an execution
permit, completion measurement, liveness proof or a second notification mechanism.

An ID's active content is immutable. To correct a topic, use a new ID and explicitly name every
currently active ID of that topic in `supersedes`; an unrelated topic is preserved. Completion or
cancellation names the existing ID and topic. The fold removes its instruction from current context
and retains the retired identity. A late duplicate, an earlier accepted command receipt, a newer
notification timestamp or a legacy global replace cannot reactivate the old ID. A genuinely new
operator direction may intentionally use a new ID after retirement. Unknown retirement, conflicting
content and a changed topic fail explicitly. New scoped data from model Deep Research never enters
operator authority. Historical legacy hints have no invented scope and remain subject to their old
explicit global replace semantics.

Hint publication now takes the existing strict-lock, bounded tail-CAS semantic append path. The same transition
validator runs at intake and against the exact fold immediately before publication, so a competing
correction cannot be lost between validation and append. Unrelated event traffic retries after a
fresh fold. Hints retain their previous cold-start quarantine and active-command admission guards. They do not
acquire a GPU lease or wake an engine; authorization on internal/external
runs still applies. Progress preserves legacy text items and adds bounded active ID/topic records,
retired identities/statuses and explicit counts. Internal roles use the same current hint projection.

`tests/test_operator_direction_lifecycle.py` drives real HTTP commands over a synthetic completed
node, two independent topics, immutable-ID conflicts, correction, completion, cancellation, delayed
redelivery, exact accepted-command replay, service restart, reverse timestamps, legacy replace,
model advisories and real append races. The corrected subject wins; unrelated tail traffic is
retained. No node, engine, fit or score is created by recovery. Existing command, auth, payload,
control-registry, hint and progress suites remain required.

The legacy text-preview follow-up is documented in [bounded reads](external-read-bounds.md#operator-direction-text-previews).
A success receipt is not proof of a complete progress text read; scoped 2000-character rejection
and lifecycle authority remain unchanged.
