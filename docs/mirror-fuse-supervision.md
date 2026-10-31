# Mirror health and FUSE timeouts

`GET /api/run-mirror` reports source-local metadata. `supervisor.state=running` only means the child has not exited; it does not establish that a copy is progressing. Inspect `diagnostic.state`, `stuck`, the recent `errors`, and the last successful pass. A successful pass traversed all listed runs with no reported errors or timeout. Older state files lack this timestamp and remain `unknown` until a new clean pass. `last_pass_finished_at` also advances for failed passes and must not be presented as success. The diagnostic's freshness allowance is at least 180 seconds and, with a managed supervisor, at least twice the configured interval plus 120 seconds. Historical error entries are retained and can keep the status degraded even after a later successful pass; review them rather than assuming they are current.

The mirror stops the pass on an operation timeout. The underlying FUSE thread cannot be cancelled by Python and may remain in kernel sleep after the timeout or termination signals. The supervisor waits for its child to exit before starting another; after three stuck exits within 30 minutes, it waits until the oldest exits that window before another attempt. This is a per-supervisor in-memory limit, not a global bound across UI restarts or a guarantee about kernel threads. Recovery is automatic when the retry window expires. During shutdown a child that did not exit after TERM and KILL is reported as `stop_unconfirmed` rather than as stopped. Storage and its mount must be diagnosed separately; this feature does not repair them, guarantee a remote flush, or replace an independently verified backup.


`last_failure` in `run-mirror-status` and `/api/run-mirror` preserves the most recent recorded
stuck operation, per-file error or unexpected pass exception across a later clean pass and worker
restart. It is bounded source-local metadata, separate from the current `stuck` flag and current
health diagnosis; it does not by itself indicate a current outage. Older state has no such history;
a still-present legacy `stuck` record is retained on the next pass. A worker killed before its
state write, an unwritable local state file, loss of the source root, or a supervisor-only crash can
leave no detail. This does not identify or repair the underlying storage fault.
