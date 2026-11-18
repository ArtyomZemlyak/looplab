# Admit ready consumers before paid cadence

RESULT 2026-11-11 — isolated engine implementation and CPU scheduler acceptance, no deployment claim.

The outer Card-mode loop opened a consumer session before cadence only for an outstanding build
request, a building node or a completed raw proposal. A fully committed pending node with no build
head therefore waited for the paid cadence although it already had code and valid scheduling inputs.
This is the built-in Card scheduler issue; it does not explain every external-agent preparation gap.

The loop now gives existing pending consumers one fill before cadence. It calls the same admission
phase used by ordinary Card sessions, retaining terminal, budget, wall deadline, attempt, aborted-node,
outer-rebuild, artifact, freshness, resource-pin and time-reservation checks. It requires the existing
run-scoped evaluator task group. The temporary session closes production and disables optional paid
research; it cannot elect a producer, stage new work or terminalize a stale selection before its
existing post-cadence boundary. Running evaluations retain their original lifetime and ownership set.

A successful admission or a released stale reservation requests a fresh outer control pass. A latch
permits one such fill per cadence boundary, so even a persistent pin rescan cannot starve the owed
cadence. Existing evaluations and cadence offload still overlap. No scientific result, extra node,
new seed or unbounded producer budget is created by this scheduling change.

`tests/test_ready_before_cadence.py` runs the actual outer engine loop with a genuine committed Card
node and no request head. The durable eval admission precedes a synthetic paid cadence; a control
that disables the new fill reproduces the old reverse order. No producer or research call is allowed.
Negative cases cover paused/budget/wall/resource/inflight/stale/changed-pin/abort/rebuild refusals.
A persistent changed pin still reaches cadence exactly once. Existing shared admission, unequal
refill, multiple producer, cadence offload, resource-time and artifact-fence suites remain required.
225 combined CPU tests pass. These checks establish ordering and guards, not production GPU speedup.
