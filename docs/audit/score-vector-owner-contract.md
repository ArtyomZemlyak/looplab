# Score-vector owner and checkpoint lineage

**RESULT — expected identity refusal revalidated; isolated error/contract improvement, 12 November 2026.**
No live registration, upload, raw output, model, job or runtime was changed.

Root reported that an accepted query39 upload was followed by a rejected saved-control upload:
the registered baseline arm named fit33, while its saved score vectors belonged to34. These identities
are parent-reported facts; this check does not claim an independent reconstruction of the original
registration. A bounded visible-tool-result tail scan yielded no additional independent receipt.
The old live source and the candidate both contain the same intended owner mismatch guard. The live
source SHA and observation limits are recorded in the isolated evidence package.

This409 is correct under those inputs. An experiment arm's node identifies the producer of measured
score vectors. A separate fit/checkpoint producer is a `model_producer` relationship, not an alias for
the score node. A combined fit+score node may own both; roles are not used to invent another owner.
Neither normalization nor composite registration silently replaces an arm node with a producer ID.
Why the reported declaration used that ID is not independently established here.

The recovery mechanism already exists: read the immutable original declaration; if its owner is wrong,
register a new experiment ID with `supersedes` and a reason, use the genuine score node as the arm owner,
and retain checkpoint lineage separately. If the request names the wrong node instead, correct it from
genuine vector provenance. Reuse existing saved vectors; no new model evaluation is implied. Preserve
accepted receipts. An unknown future owner can be omitted until the first genuine arm upload binds it.

Previously the generic refusal did not name the declared owner or distinguish a registered binding from
a prior accepted upload. Its additive response now includes `ownership_source`, `owner`, submitted node,
experiment/arm and a conditional explicit-correction explanation. The409 code, owner checks, attempt and
run identity, preflight-before-payload and sequencer recheck remain. A superseding declaration does not
silently alias or rewrite an older registration. No new duplicate OPEN item is introduced for this
expected protection or the immediately improved error context.

CPU acceptance uses actual authenticated HTTP registration/preflight/upload routes with controlled
saved vector fixtures. It proves that a mismatched fit-reference/score-owner request fails before any
payload read/decode and adds no files/events; the existing explicit superseding declaration then accepts
the correct score-node upload while retaining both original owner and producer lineage. Another case
leaves the owner unknown: the first genuine fixture upload binds it and a later other-node upload is
refused before payload work. Original event and manifest bytes remain unchanged on refusal. Existing
protocol-version, consumer-lineage, replay and recovery tests remain in the regression suite.

This is a source-contract/ergonomic improvement, not a repaired identity bug, production deployment,
new scientific measurement or independent proof of checkpoint lineage.
