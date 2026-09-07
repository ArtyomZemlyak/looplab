"""The ONE write policy for `trust_gate_changed`, shared by its two surfaces.

`EV_TRUST_GATE_CHANGED` is folded LAST-WRITE-WINS and has two writers: the server's config PUT
(`serve/routers/runs.py::_put_run_config_locked`) and the assistant's settings tool
(`tools/machine_runs_tools.py::MachineRunsTools._tool_set_trust_gate`). They were two
implementations of one write, and they had already drifted on every property that matters:

  * IDEMPOTENCE — the router refolds first and returns without appending when the gate is already
    the requested value; the tool appended unconditionally, so an assistant confirming a gate that
    already held grew the durable log by a row claiming a change nobody made.
  * THE TAIL CAS — the router appends under `expected_last_seq` and retries up to four times
    (refolding each round, so a concurrent writer that already applied the same gate collapses the
    retry into a no-op); the tool appended bare.
  * THE WRITER LOCK — the router passes `require_lock=True` and therefore fails visibly when the
    cross-process lock cannot be taken; the tool's append was best effort, i.e. it could interleave
    with the engine's own writes on the same `events.jsonl`.

That is §0.8's shape one surface over: a rule with more than one implementation drifts between the
copies, and here the weaker copy is the one an LLM drives.

WHAT THIS FUNCTION DELIBERATELY DOES NOT DO is phrase the refusal. Contention returns
`GATE_WRITE_CONTENDED` and each caller raises its own live vocabulary over it — a 409 with the
config editor's wording on the HTTP side, a parenthesised sentence the assistant can read on the
tool side. That is `serve/durable_op.py::refuse_unless_quiescent`'s rule: share the probe and its
order, never the words, because the words are contracts of the surface that speaks them.

It also does not mirror `config.snapshot.json`. Both callers do that, but under DIFFERENT locks
held for different spans, and folding the mirror in here would mean this module deciding a lock
ordering on behalf of two surfaces that already have one.
"""
from __future__ import annotations

from pathlib import Path

from looplab.events.eventstore import EventStore, EventStoreConcurrencyError
from looplab.events.replay import fold
from looplab.events.types import EV_TRUST_GATE_CHANGED

#: The gate values the fold understands. Anything else is a caller bug, not an operator refusal —
#: both surfaces validate the operator's input against their own vocabulary before they get here.
TRUST_GATE_VALUES = ("audit", "gate", "block")

GATE_WRITE_APPENDED = "appended"
GATE_WRITE_ALREADY_SET = "already_set"
GATE_WRITE_CONTENDED = "contended"

#: Every outcome, so a caller's match cannot silently miss one.
GATE_WRITE_OUTCOMES = (GATE_WRITE_APPENDED, GATE_WRITE_ALREADY_SET, GATE_WRITE_CONTENDED)

_ATTEMPTS = 4


def apply_trust_gate(rd: Path, requested: str, *, source: str, attempts: int = _ATTEMPTS) -> str:
    """Move the run's trust gate to *requested*, returning one of :data:`GATE_WRITE_OUTCOMES`.

    `source` rides on the row and is the only thing that differs between the two callers: it is how
    a later reader tells a config edit from an assistant action, which is exactly the distinction
    the durable log exists to keep.
    """
    if requested not in TRUST_GATE_VALUES:
        raise ValueError(f"trust_gate must be one of {TRUST_GATE_VALUES}, got {requested!r}")
    store = EventStore(Path(rd) / "events.jsonl")
    for _attempt in range(max(1, int(attempts))):
        events = store.read_all()
        if fold(events).trust_gate == requested:
            return GATE_WRITE_ALREADY_SET
        expected = events[-1].seq if events else -1
        try:
            store.append(
                EV_TRUST_GATE_CHANGED,
                {"trust_gate": requested, "source": source},
                expected_last_seq=expected,
                require_lock=True,
            )
            return GATE_WRITE_APPENDED
        except EventStoreConcurrencyError:
            # Another writer advanced the log. Refold under a fresh CAS: it may already have applied
            # this exact gate, in which case the retry becomes a no-op above.
            continue
    return GATE_WRITE_CONTENDED
