# Physical evaluation occupancy during LLM repair

**RESULT — isolated source implementation and CPU acceptance, 12 November 2026.** No live source,
service, native run, model, GPU lease or immutable research snapshot was changed.

The historical 69.11 incident remains in its original review. Returning a repair's GPU did not return
its evaluation-width slot: Card admission still counted the owned lifecycle, and the legacy width-one
dispatcher awaited the whole repair chain. Its future eval-time reservation also remained committed.
Independent ready work therefore could not use otherwise idle capacity during a long LLM repair.

A dispatched lifecycle now has a separate `RepairableEvalSlot`. LLM repair returns physical width,
future eval allowance and the existing device reservation, while **retaining lifecycle ownership**.
Card admission counts owned lifecycles except explicitly parked repairs; it never makes a parked
node dispatchable a second time. Legacy width one uses the existing continuous dispatcher with a
single physical token. Before the next sandbox attempt, the repair reacquires physical width, checks
fresh cumulative allowance including all unterminalized actual chain spend, reserves its future
attempt allowance and reclaims/re-pins devices. Existing artifact/source/footprint, lifecycle and
pause checks remain. A finalizer gives back only its own currently held token/devices and clears its
own reservation; finishing while parked cannot free a sibling's token or budget.

Actual failed-attempt costs are separate from future allowance. They stay in the in-process
admission budget until the original terminal carries them. The native durable invocation/repair/
withheld receipts and original terminal accounting are unchanged. If independent work exhausts the
allowance while the repair is parked, the next attempt is refused as `eval_budget_exhausted`, with
its already-paid durable cost preserved and a native pause. This is a budget refusal, not an extra
human/Root approval step or permission to launch work outside the current protocol.

The single-width resource wait still uses bounded ticks and checks the deferred stop both before
and after a grant. Its one producer-owned tail-gated fold survives quiet wait ticks and is moved
into ADMIT once; it is never a shared mutable global replay cache. Width changes do not resize a
legacy batch's already captured semaphore. Existing shared Developer serialization remains; the
fix releases evaluation resources, not the Developer's own session lock. One previously implicit
`_pre_cadence_admission_done` boolean is now declared at its existing false default, as required by
the unchanged attribute census.

## Acceptance and limits

**323 CPU checks passed; 5 existing optional checks skipped.** The new public engine-path fixtures
run actual CPU Python subprocesses with an explicitly empty GPU inventory and scripted unpaid
Developer responses. A mechanical crash parks its real repair; an independent native evaluation
finishes before the repair returns, then the corrected first node runs. Both legacy continuous
width one and real Card admission are exercised, with and without an eval ceiling. Original
invocation IDs, one lifecycle admission per node and exact terminal cost sums are checked.

Two nodes can both fail while another repair is parked and subsequently repair without exceeding
one physical sandbox execution. This fixture preserves the real shared Developer lock. Cancellation
while waiting for reclaim leaves the sibling's reservation intact. Another real CPU subprocess
exhausts a small allowance during the repair; no third invocation is launched, the genuine prior
cost survives, and the expected budget refusal is recorded.

Existing device-yield/re-pin, changed generation, abort/reset/pause, cumulative reservation, deferred
paid-budget stop, resource fit/footprint, admission, exception containment, repaired-chain and golden
receipt suites pass. The unchanged-prefix fold-count and moved-tail refusal tests also pass; old
repair goldens were not regenerated. Two old private test hosts were made scope-aware by supplying
their own empty temporary run directory, and their optional reclaim signature follows the native
caller. No launch guard was disabled to make a test pass.

This proves source scheduling and cost behavior on controlled native CPU evaluations. It does not
measure production GPU utilization, speedup, repair quality, distributed fairness or deployment.
Ephemeral occupancy state is not a new durable or cross-process lease. Existing GPU host ownership
and durable recovery remain authoritative; a healthy live run must not be restarted for this patch.
