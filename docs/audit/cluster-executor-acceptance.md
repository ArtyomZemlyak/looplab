# Cross-machine CPU executor — source contract and unverified target acceptance

The canonical doc 70 item retains the historical `no-cluster-executor` identity and the merged former
`eval-parallelism-is-in-process-only` duplicate. Local parallelism already existed. On 12 November 2026
an optional CPU transport was implemented in an isolated candidate. No other-host result or deployment
is claimed, and no accepted cross-host result record exists.

## Supported source contract

`cluster_executor` defaults to null, including on restoration of pre-field snapshots. Null keeps the
existing subprocess/Docker paths. Opting in requires `trust_mode=trusted_local`, explicit
`eval_parallel=1`, a fixed argument vector and a short `worker_id`. A transport can invoke an installed
worker through an operator-owned SSH command; LoopLab starts no listener, discovers no cluster and copies
no credentials. Configure transport authentication outside the argument vector; inline credentials are
refused. RPC timeout and polling are bounded. A worker installation and a durable, private worker root
must be provisioned separately by its operator.

Example configuration shape, with deployment paths and host supplied by the operator:

```json
{
  "trust_mode": "trusted_local",
  "eval_parallel": 1,
  "cluster_executor": {
    "argv": ["ssh", "--", "cpu-worker", "python", "-m", "looplab.runtime.cluster_worker",
             "--root", "/operator-owned/durable/jobs", "--worker-id", "cpu-a"],
    "worker_id": "cpu-a",
    "rpc_timeout_seconds": 5,
    "poll_seconds": 0.2
  }
}
```

This is an illustrative operator configuration, not a tested SSH deployment command. Fixed argv is
executed without a local shell. A POSIX client exchanges bounded stdio with a Linux worker. The worker processes one bounded JSON request on stdin and emits
one bounded JSON reply on stdout. It uses no model and runs no background service. An accepted command
has an owned execution process, independent of a client's connection lifetime.

Only a normally admitted native `solution.py` CPU candidate can launch. Its explicit zero-GPU footprint,
run UID, node lifecycle generation, inline attempt and deterministic invocation ID are checked. The
native claim records worker/transport identity and source/input SHA-256. Repository commands, side-file
patches, artifact producers/parent paths, host grading, canaries, ablations, noise-floor and confirmation passes are refused;
there is no fallback that silently runs them remotely or downgrades their evaluation. Task-level refusal
runs before paid role construction. Unbound sandbox calls cannot launch.

Only declared normalized text assets are transferred: at most 64 files, 64KiB each and 256KiB total;
code is bounded at 128KiB and the encoded RPC at 1MiB. The staged inputs must still match their pinned
contents, with links refused. No recursive workdir/checkpoint/private-file copy occurs. Worker candidate
processes receive the declared evaluation environment and CPU visibility/thread settings, without the
local session's inherited environment. This is a **trusted-code transport**, with the same self-reported
stdout metric authority as the local subprocess tier; it is not hostile-code isolation, independent
attestation or a distributed GPU allocator. A zero-GPU visibility setting is not a hardware security
boundary.

## Durable invocation and failure behavior

The worker stores immutable request bytes and a launch claim before spawning the command. File and
parent-directory fsync protect that local storage protocol; they do not prove a remote filesystem's
flush guarantees. A per-root lock and one active CPU slot coordinate all clients/runs of that worker.
An exact resubmission returns the original state/result. Changed content under the same key refuses.
A different invocation cannot occupy the slot until the previous job has a terminal receipt.

Status and cancellation use the same key. Operator cancellation and the worker timeout stop the owned
candidate process tree through the existing subprocess runtime; the client consumes a terminal only
when the worker records it. Already-cancelled fresh attempts submit nothing. A lost submit reply retries
only the identical immutable key, so reconnection cannot buy a second execution. A launch gap, worker
process loss, corrupted receipt or persistent transport failure becomes explicit UNKNOWN. It never
becomes an invented failed metric, a proof of stop or authority to launch a replacement.

UNKNOWN pauses the native run and leaves its invocation open. Finalization drains native children
and requires remote terminal receipts too; an unavailable worker leaves finalization deferred, without
a fabricated completed run. Reconnection can finish the original invocation without launching again. The native withheld record preserves the
client's wait, job identity and uncertainty. Resume on the same worker reattaches to the same invocation.
Changing worker/root transport while a claim is open refuses before contacting the new placement. A
reset does not erase remote ownership: before a new lifecycle launches, the engine queries/cancels the
old claim and requires its terminal receipt. A local superseded node terminal alone is insufficient.
The obsolete receipt is linked to its invocation settle; its metric cannot rank the new lifecycle.
If an executor died without an outcome, worker ownership and resources remain UNKNOWN. Restore/reconcile
that worker and retain forensic evidence; deleting its claims or blind resubmission is not recovery.
No automatic process-stop, billing or completion proof is fabricated for this unrecoverable case.

The normal native invocation settle and evaluated terminal retain placement, source/input/output digests
and worker execution seconds. Output digests cover the captured UTF-8 text tails, not an unrecorded raw
stream. The client independently reconstructs ordinary stdout parsing and verifies result/output identity;
malformed/coercible UID, integer, timing, metric or version fields cannot become quality. The full native
`eval_seconds` remains client lifecycle wall time, including transport/wait. Worker execution time is a
separate observation; physical occupancy and billing remain unknown, and their overlap is not summed.
This is neither a cloud invoice nor proof of distributed resource enforcement.

## CPU source acceptance

`tests/test_cluster_executor.py` executes actual separate Python worker processes through a normal
ToyTask Engine, not just a mocked submit call. It covers immutable source/input/placement and terminal
receipts, exact retry and lost accepted reply, UNKNOWN-to-resume, native reset and old-key stop before
new generation, transport-change refusal, worker loss, pre-cancel, actual timeout/cancel and finalize during healthy work, unknown finalize deferral/reconnection, bounded RPC
output/time, malformed success metadata, input/link drift, encoded payload preflight, launch-claim crash
gap and worker slot conflicts. Existing native invocation/recovery, withheld-cost, repair, canary,
legacy sandbox and event-payload contracts are checked separately. All execution is synthetic CPU on
one host. Passing these tests is source acceptance only.

## Remaining target acceptance

A declared executor must run one bounded synthetic command on **another host** through the normal node
lifecycle. Its immutable source, inputs, run UID/generation, attempt, resource request, actual placement,
result and cost receipts must agree. Reset, stop, timeout, lost reply and executor restart must retain
one honest terminal or explicit unknown/recovery; exact retry must not launch twice.

Verify installed interpreter/source identity, transport authentication, worker-root persistence/flush,
process-tree stop on the target kernel, protected-input boundaries, and observed resource/cost evidence.
Queue/client wait and unknown resources remain separate from measured execution. GPU coordination,
scheduler integration and production utility are unsupported/unverified, not inferred from a local
GPU lease or the CPU fixture. No current benchmark or native ML campaign depends on enabling this
transport. This protocol does not authorize cluster deployment, a neural experiment or extra workload.
