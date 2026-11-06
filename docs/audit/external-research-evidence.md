# External Research Evidence Acceptance — protocol, no result

**Status:** protocol for the related open item in doc 83; no acceptance has run. Existing APIs may satisfy a
check; this is not a request to invent another research store or require literature for every technical
operation.

## Goal

Verify that a fresh external agent and a human can traverse a scientific campaign in both directions:

`question → card → experiment/arm → attempt/metric → claim → report`, and back from a report claim to the
actual code, test and measurement. Unknown and not-applicable are explicit and are never converted to zero or
success.

## Fixtures

1. Synthetic CPU campaign with two related scientific questions, one fit, one score-only arm, one negative
   result and one technical recovery. Use genuine existing command APIs and replay; no GPU or model call.
2. A production-sized read-only fixture containing long directions, many memos/reports, unclassified legacy
   nodes, extra metrics without a scalar and notices with and without dispositions. No historical record is
   rewritten to manufacture completeness.

## Checks

- Each applicable experiment names a question/card or a reason it is not applicable; retry inherits context
  rather than minting fake science.
- Every report metric binding resolves to the exact node/attempt, test/cohort/policy and available evidence;
  missing scalar does not hide present extra metrics and matching numbers alone do not prove comparability.
- Each material claim has source/code/result links and a declared evidence tier. A link or filled claim is
  not itself verification.
- Question cards, work kinds, reports and notice dispositions have concrete supported remediation and owner.
  Transient/historical notices remain unknown when no durable record exists.
- `notice_resolution` reflects the existing disposition journal and never states that no journal exists
  merely because no disposition was recorded.
- Bounded reads preserve omissions and continuation links; a new agent can resume without the full history.
- Engineering-only work may skip literature and a separate scientific question without a warning storm.

## Acceptance

The above graph is navigable in UI/API/export, old logs replay without fabricated fields, and the report
contains no contradictory source-health statement. Record actual coverage and each unresolved warning with a
reason. A quality score, auto-acknowledgement or new mandatory field alone does not pass this protocol.
