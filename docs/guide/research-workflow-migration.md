# Research workflow: migration and recovery

This guide describes an additive research-facing projection over existing LoopLab journals. It does
not create another scheduler, GPU dispatcher, scientific protocol or report store. The canonical
external-agent process, current examples and supported routes are in the server's
`GET /api/runs/{run_id}/research-workflow` and `looplab/harness/external-research-workflow.md`.
The live contract, phases and progress take precedence over a copied instruction.

## Starting an external agent

Copy `skills/looplab-research/` into the agent's configured skills directory and reload its skill
catalogue if required. The skill is intentionally a short router. Connect to the intended server,
read capabilities/connection, run UID and generation, contract, progress and relevant phase. Read
`research_briefing` for the current direction and action context, then page `research_map` as needed.
A prior handoff, a copied skill or a ready observation does not resume an old experiment or grant
new budget. A single authorized executor can continue independently; named multi-agent handoffs are
optional and do not introduce a Root approval barrier.

For a local run, `looplab research-map RUN_DIR --generation TOKEN` reads a page and adding
`--briefing` reads the compact context. This local form cannot inspect the running server's mirror
supervisor and labels that observation unavailable. API/MCP/UI use the connected server for it.
Independent source reads are not atomic; a mismatched event prefix requires refresh before action.

## Old data and reports

Old events, source snapshots, uploads and report versions remain authoritative and are never
rewritten. A previous report is a point-in-time publication, not an automatically cumulative story.
New `report_generated.content.campaign_overview` is an authored cumulative snapshot; later focused
reports do not replace the most recent authored overview in the research map. Historic versions
remain viewable in full. No missing past question, source, metric or relationship is invented.
Optional `question_card_id` on new registrations links an existing direction card; it does not
retroactively preregister an old result. Model producer, scientific control and code reference are
separate authored relationships; they are not proof of checkpoint identity.

A readiness binding records that a bounded producer manifest matched its declared digest at one
instant. A retry recovers the original receipt; a later attempt or changed scope makes the current
binding stale. Recheck the manifest and existing protocol/source/budget/lease gates before using it.
An optional explicit artifact path and SHA-256 additionally verifies that artifact at read time, outside the command sequencer; later freshness is not rechecked. Neither the binding nor an acknowledgement authorizes execution.

Metric profiles are owner-managed project presentation metadata, edited with a compare-and-swap
digest. They name a primary metric, other metrics, denominators and desired slices; they do not
change old scientific registrations or create measurements. A report metric reference can bind a
node attempt and declare a test/cohort/policy; labels remain author declarations. Existing
`extra_metrics` and per-query comparison receipts supply values. Query/cluster test identity does
not by itself prove identical ground truth or policy. Missing metrics are shown unavailable, not zero.

## Notifications and recovery

The existing outbox, wait cursor and configured hook remain the delivery path. If a named hook has
an explicit event-kind filter, add `agent_handoff` and `operator_direction` where desired. Addressed
handoffs use per-event ACK; a successful hook exit or unrelated run read is not that ACK. A persisted
pending reaction is recovered only when its outbox rows and original hook can be reconstructed. A
damaged pending ledger is reported unavailable and preserved. External delivery is at least once
across the crash window: deduplicate by event identity and inspect current progress. No shell
watchdog or new daemon is needed.

A mirror's running supervisor is not proof of a healthy copy. Inspect stuck/errors, restart count and
last successful pass; recent local metadata and a readback do not prove S3 flush. Recovery keeps
source/runtime, native journal, checkpoints, raw data and published reports as distinct artifacts.
Do not replay completed work merely because a process restarted.

## Export and deployment

`research-export` returns bounded Markdown, its SHA-256, selected historical report references and
content-addressed figure links. Figure bytes, models and raw rankings are not copied. Project profile
and journals are independent observations; the profile is current at export, not necessarily the one
that existed at a historic report's publication. Configure any external publication separately under
its existing permissions.

Before deploying, run the CPU backend, replay and UI checks and `tests/browser/run_research_smoke.sh`
against a fresh isolated fixture. Preserve the existing source export and native event history. Deploy
only at an appropriate stopped service boundary, without interrupting healthy experiments. Verify the
server contract, report history, notification health and mirror state after rollout; revert the service
binary/source to the prior version if verification fails. This change does not itself authorize a
restart, deployment or publication.
