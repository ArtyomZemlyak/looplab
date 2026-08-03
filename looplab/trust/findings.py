"""One finding shape, and the signal namespaces that go with it (doc 25 CT-10).

The trust detectors each grew their own row vocabulary: leakage returns
``{"detector", "leak", …}``, reward-hack returns ``{"signal", "detail", "method", "confidence"}``,
`ExploitSuite.scan` returns ``{"signal", "detail"}``, and the critic returns ``{"issue", "detail"}``.
`engine/evaluate.py` then reconciled them by hand, and — the part that actually mattered — MINTED
the signal namespace while doing it::

    sigs.append({"signal": "data_leakage:" + f["signal"], …})
    sigs.append({"signal": "critic:" + c["issue"], …})

Those namespaced strings are not cosmetic. `events/replay.py::is_hard_signal` keys gating decisions
off them: `critic:hardcoded_metric` EXCLUDES a node from selection, everything else under `critic:`
stays advisory. So the namespace a detector's output lands in decided whether a run could be gated —
and it was assembled at the consumer, three files away from the detector that knows what it found.
A second consumer would have had to re-derive the same mapping from scratch, and any drift between
the two would be a silent change to what gates.

This module owns the namespaces and the emitting helpers. Each detector now emits its own
already-namespaced rows, and `evaluate.py` concatenates. The dict shape is unchanged, so the
`reward_hack_suspected` events stay wire-compatible with every log already on disk.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional

# The two namespaces the consumer used to mint. Constants, because `is_hard_signal` matches on the
# exact spelling — a typo'd literal would silently move a signal between "gates" and "advisory".
LEAKAGE_NS = "data_leakage:"
CRITIC_NS = "critic:"

# The keys a trust finding may carry. `signal` and `detail` are required; `method`/`confidence` are
# what the reward-hack detector adds; anything else a detector wants to attach rides along and is
# preserved verbatim into the event.
FINDING_KEYS = ("signal", "detail", "method", "confidence")


def finding(signal: str, detail: str, *, method: Optional[str] = None,
            confidence: Optional[float] = None, **extra: Any) -> dict:
    """One trust finding, in the shape `reward_hack_suspected` already stores.

    `signal` must arrive ALREADY NAMESPACED — that is the whole point of the move. A detector that
    returns a bare issue name is asking a consumer to guess which gate its output belongs to.
    """
    row: dict[str, Any] = {"signal": str(signal), "detail": str(detail)}
    if method is not None:
        row["method"] = method
    if confidence is not None:
        row["confidence"] = confidence
    row.update(extra)
    return row


def namespaced(prefix: str, rows: Iterable[Mapping[str, Any]], *, signal_key: str,
               detail: str | Any) -> list[dict]:
    """Project a detector's own rows into namespaced findings.

    `detail` is a callable taking the row, or a plain string. Kept tiny on purpose: the value here
    is that ONE place turns a detector's private vocabulary into a gate-visible signal name.
    """
    out: list[dict] = []
    for row in rows:
        name = row.get(signal_key)
        if not name:
            continue
        text = detail(row) if callable(detail) else detail
        out.append(finding(prefix + str(name), text))
    return out
