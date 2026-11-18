# Pending inject cancellation — acceptance and local result

The baseline audit recorded a missing control-plane capability. An operator workaround is evidence of
the need, not a supported API or permission to edit a production journal. No engine, model or GPU run is
authorized by this document.

## Current decision sites

`looplab/serve/control_validation.py::CONTROL_SPECS` gives inject `ENSURE_RUNNING` semantics.
`looplab/engine/forced_requests.py::_reserve_and_claim_inject` consumes the queue head before the paid
materializer. `looplab/events/replay_requests.py::_on_inject_done` advances the inject cursor. At that baseline there was no
supported exact cancellation operation in that vocabulary. A queue index is not a node ID.

## Required cases

Use an isolated CPU run with two queued, unmaterialized injects and a stopped engine. Invoke the supported
operation with run UID/generation, original command ID, request identity/digest and a new stable action ID.

1. Cancel exactly the selected pending head. Record an auditable cancellation in the existing command and
   event history. No engine spawn, node reservation/materialization, subprocess or GPU acquisition occurs.
   Preserve the event prefix, every prior node, scientific outcome, cost and budget. The second inject stays.
2. Retry the exact cancellation after its receipt, including after another head becomes current. Return the
   original receipt; do not consume the next request. A changed body under the same action ID is a conflict.
3. Wrong UID, generation, command ID, request digest or queue identity refuses before mutation. A supplied
   node ID alone must not be mistaken for a pending request identity.
4. A live materializer, reserved request or already materialized node refuses or returns its actual state.
   This operation is not `stop_node`, `retry_node`, permission to abort a healthy job or `resume`.
5. Crash at each publication boundary. The cancellation is atomic or recovery gates the unsettled request;
   it cannot publish a consumed cursor with no cancellation record, nor duplicate the paid materializer.
6. If non-head cancellation is offered, prove the separate skip/tombstone semantics preserve every earlier
   request. A cursor jump over other queued work is never an implementation of exact cancellation.

## Acceptance

Drive the public command/API and MCP surface, then restart and replay the isolated run. Compare event
prefix, node table, queue, budgets and invocation counters. Retain the original refusal and cancellation
receipts. A hand-written event append, unit-tested helper alone, or a broad `resume` does not pass.

## RESULT 2026-11-11 — isolated CPU acceptance

Implemented read-only `GET /api/runs/{run_id}/pending-inject` with original `command_id` and
`expected_generation`, plus `POST /api/runs/{run_id}/pending-inject-cancellation`. MCP exposes both through
its existing `api_request`, live operation schema and recovery phase. The semantic action is retained by
`saved_actions` before HTTP. It requires explicit run UID/generation, original command ID, queue index and
scoped digest, a new stable action ID and reason. No node ID substitutes for a request identity. Legacy
runs without a recorded UID are explicitly unavailable rather than assigned an invented incarnation.

The command sequencer, run lifecycle lock, actual engine writer lock and event-tail CAS exclude concurrent
serving. Live/unknown engine ownership, spawn/activity claims, another active/unreadable command, any build
reservation, served request or non-head request refuse. No ownership claim is silently retired.
The existing `inject_failed` plus `inject_done` cancellation pair is published in one strict atomic batch.
The full prior event prefix, nodes, metrics and budgets remain unchanged; the next request stays pending.

That event pair is the authority; original-command metadata is its recoverable projection. A previously
successful admission remains historical admission success with a separate cancellation receipt. Pending
workers, GET reconciliation, explicit retry, monitor settlement and both replacement-spawn paths read the
same cancellation authority. A crash after the batch but before metadata save cannot restart that inject.
An exact action replay returns its original cancellation even after another head becomes current; changed
content under that ID conflicts. An incomplete batch exposes no consumed cursor and gates recovery on the
unavailable source, with no automatic journal repair.

`tests/test_pending_inject_cancellation.py` exercises actual public command admission for two queued
requests, the supported HTTP action and registered MCP tool, live operation schema, scoped auth, exact
replay after the head changes, identity refusals, actual `engine.lock` contention, reservation/owner claims,
served/legacy state, before/after-commit failure and a torn batch. A live original worker is deliberately
held after admission, cancellation loses its metadata acknowledgement, and that worker resumes without
spawning an engine. All side effects use disposable CPU fixtures, not a production cancellation script.
The broader command/worker/receipt/cost/client-request/MCP suite passed **330 tests, 2 skipped**.
Replay/event/API/index/documentation/claim/budget guards passed **289 tests**; counts overlap. Exact logs
and strict documentation build are retained in the durable implementation capsule. Receipts identify
the author as agent or operator without retaining a credential value. Phase-size ceilings were kept, with shared terminal-before-respawn
and missing-intent helpers extracted instead of raising limits.

Scope: isolated local code and CPU acceptance. No live UI, native campaign, runtime mirror, GPU, saved
scientific artifact or remote branch was changed. Deployment and platform-specific storage acceptance
remain separately unverified.
