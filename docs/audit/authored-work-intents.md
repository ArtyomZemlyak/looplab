# Authored work intents and optional direction receipts

The earlier 67.11 finding was that same-task runs exposed prior results but no structured declaration
of work currently being prepared. Native checkpoints now carry optional bounded `work_intents`.
They are recovered after process loss and exposed by progress and a caller-selected peer API/MCP
read. No shared mutable memory file, ownership lease, background watcher or launch gate is added.
The canonical [workflow](../guide/external-research-workflow.md) describes fields and exact read/write paths.

Each declaration is authored evidence of intent, not measured execution. Subject SHA and topic are
not verified against data/source/model artifacts. Exact active operation/subject matches are possible
overlaps. A current hint can be explicitly cited as followed, deferred or conflicting; unknown or
model-generated hint text cannot be cited as operator text. New unaddressed visible hints remain
visible without imposing an ACK or Root approval barrier. Latest explicit empty declarations clear
current work notes; unrelated checkpoints do not silently withdraw them.

The real Nov12 coordination observation was narrower: Root selected a direction around04:44,
transport accepted a queued message, and an independently authorized executor began a different
healthy method around04:59. Root later adopted that actual work. The receipts do **not** establish
that the executor read and ignored the direction. The original native1996/1997/2000 and2032 and
`CURRENT_WEIGHTED44_ACTUAL_SCIENTIFIC_DECISION.json` remain the incident sources. No full private
message or transcript is copied here. This implementation provides explicit missing interpretation
and optional retained ACK visibility; it does not retroactively establish comprehension or blame.

The read correlates current native operator hint sequence IDs with retained outbox IDs and official
self-attested ACKs. A queue/delivery receipt is not an ACK; an ACK is not comprehension, agreement,
scientific completion or recipient authentication. Missing/unsupported journals are unavailable,
not an assertion that a person failed to act. No notification configuration or private command is
read. One confirmed adjacent schema defect was repaired: HTTP and MCP no longer coerce booleans,
floats or numeric strings into another event's integer ACK identity.

The selected peer read is limited to eight same-native-task-ID runs and4–64KiB. Foreign tasks are
outside scope. Damaged complete journals and invalid latest declarations have unknown claims,
never fabricated empty work. Normal unfinished final event writes retain canonical EventStore
prefix behavior. Each observed generation/prefix is independent, not a distributed snapshot.
Oversized answers omit whole peers and name them for explicit recovery; no intent text is shortened.
The typed client rejects malformed identity, claim, authority, count and receipt metadata without
passing a usable body or automatically retrying. Actual source/protocol/budget and credential guards
remain unchanged; no claim, ACK or scientific interpretation blocks ML.

Acceptance drives public commands, actual native hint/outbox/ACK writers, HTTP and registered MCP,
service restart, exact unchanged native bytes on reads, current-context changes, malformed receipts,
strict event IDs, foreign/corrupt peer refusal and whole-peer recovery. Tests do not claim production
adoption, duplicate prevention, provider comprehension, cross-machine liveness or scientific utility.
The implementation is isolated source only; live remains separately owned and unchanged.
