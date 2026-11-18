# Optional native report review

12 November 2026. Implemented and tested in the isolated audit branch; not published or deployed.

The historical §67.6 gap was confirmed: the final report writer had no separate native review
binding its comments to recorded outcomes and presenting research nominations for operator approval.
The Report tab now offers **Read report review**, then an explicitly optional paid review action.
Opening the tab or reading existing evidence makes no model call. The ordinary report writer,
engine cadence, native launch gates and original report remain unchanged.

## Evidence and authority

A review receives the exact published report and all current native node facts within explicit
bounds (100 nodes, 128 KiB evidence). Oversized populations are refused, not silently sampled.
It uses a separate two-message context and structured parser without mutation tools. The configured
provider can be the same as the author's; provider independence is not attested. Current prose
language applies to new comments, while quoted text, identifiers and measurements remain verbatim.

Each comment names an exact quote in a report field and distinct native node IDs. The server
attaches the original outcome, attempt, protocol, feasibility and code/idea digests; the model
cannot supply measurements. A nomination needs a cited evaluated feasible non-salvaged finite
outcome and a rationale. Null and legacy null protocol versions remain unknown. Native scalars
are not a substitute for genuine per-query arrays or external evaluator receipts: the reviewer
cannot infer missing quality evaluation or justify a repeated fit/score from a missing scalar.
No causal mechanism is established merely by a score gain.

The receipt binds run UID/generation, report sequence/content digest and the full evidence digest.
It is reconstructed against the exact captured native prefix before disclosure or nomination.
Forged or unbounded completions cannot support a recommendation or trigger another paid call.
New outcomes, report changes, resets, code/idea/protocol changes, trust evidence, operator directions
and budget changes make it historical. The original review stays readable but cannot nominate.

**Discuss nomination with Assistant** drafts an ordinary Assistant message; it sends nothing by
itself. `read_report_review` reads the saved receipt. `nominate_report_review` requests an explicit
HIGH-risk permission card even in Auto mode, using the existing `assistant_inject_tool` exposure.
Approval records a bounded, retractable research direction through the canonical command service.
It does not inject a node, confirm, ablate, schedule an evaluation, grant a repeat, or start an engine.
Scientific scope, protocol, budget and execution permissions still apply. This optional workflow
adds no ML/Root approval gate. Native evidence is checked again after approval and immediately
before the shared event-tail compare-and-append. A recorded direction itself changes the context,
so another old nomination requires a fresh review or an ordinary explicit research direction.

## Paid identity and recovery

Owner API: `GET /api/runs/{run_id}/report-review` reads input digests, the latest receipt and its
freshness. `POST` takes exactly `expected_generation`, integer `report_seq`, `report_digest`,
`input_digest` from that GET and an `Idempotency-Key` header. Boolean sequences and unsupported
metadata are refused before claim/provider work. The paid ledger durably claims before provider
construction and meters observed usage to the exact run generation. Report-review diagnostic
receipts are not control events and are not Assistant-appendable.

The UI verifies session-storage persistence before submitting, keeps the same request after a
lost/invalid response, and resumes it after remount or a model becoming unavailable. It never
silently replaces uncertain work with a fresh paid identity. Exact completed requests replay
without reading model configuration or calling a provider. A changed body under the same key
conflicts; an orphaned durable claim after process death stays uncertain and blocks a new key.
This is deliberate at-most-once paid dispatch, not a claim of exactly-once provider delivery.
An accepted nomination similarly recovers its journaled approved command without a new card or
new native hint, including after its own accepted direction makes the review historical.

## Acceptance and limits

`tests/test_native_report_review.py` drives real HTTP/job receipts, metering, app restart, lost
worker claims, unavailable storage, private provider failure, fabricated outcomes/quotes,
malformed metadata, finite targets, stale evidence during model work and operator approval,
shared-tail races, exact native-prefix validation, permission denial and approved-command recovery.
It asserts no node injection, forced confirmation/ablation, engine resume or duplicate paid call.
`ui/test/reportReview.test.js` mounts the component and checks deliberate clicks, durable browser
identity, same-key recovery, malformed receipts, historical/read-only controls and draft-only
Assistant handoff. Existing report, event, paid-ledger, permission, layering, language, policy
replay and catalogue checks cover the surrounding contracts. The actual localized Vite production build also
passes; its prebuild check exposed and corrected missing catalogue entries in this and earlier
source-only telemetry/monitor/map UI changes. No served assets were replaced.

These CPU/UI fixtures validate source behavior. They do not measure review quality, provider
independence, scientific benefit, real invoice reconciliation or production ML outcomes. A
complete stable local event descriptor is captured within 128 MiB; mounted readback does not
prove S3 flush. The feature remains optional and is not deployed by this acceptance.
