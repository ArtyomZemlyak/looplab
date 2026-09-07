"""STATE DELTAS for the run's SSE stream (doc 52 row 29): the difference between two folded-state
payloads as a bounded list of operations, and its exact inverse.

WHY. `serve/routers/runs.py::stream_events` serialized and retransmitted the COMPLETE folded state
on every event (its own note called it quadratic: one long-lived tab receives O(events × state)
bytes and the server repeats whole-state encoding per tick). The frame after the first can be a
DELTA against the frame the client already holds, keyed on the seq it last saw — a full snapshot
stays the first frame of every connection and the fallback whenever a delta would not be smaller.

WHAT A DELTA IS. `diff(old, new)` walks the two JSON trees and emits, in a stable order:
    ["set", <path>, <value>]   the subtree at <path> is now <value> (a new key, or a changed leaf
                               or list — lists are atomic: a changed list is re-sent whole, because
                               a positional list diff is the one place a client could apply an op
                               to the wrong element after a reorder);
    ["del", <path>]            the key at <path> is gone.
`apply(old, ops)` returns the new tree without mutating `old`, and `diff` ∘ `apply` is an identity
on any pair of JSON documents whose dict keys are strings — `tests/test_state_delta.py` drives the
round trip on real folded payloads. No library: the shape is three lines and the client applies it
in `ui/src/stateDelta.js` with the same rules, so the two halves are one contract.

WHAT KEEPS IT HONEST. The stream ties every delta to `base_seq`, the seq of the payload it was
computed against, and the client refuses a delta whose base is not the snapshot it holds — it
reconnects and receives a full frame. A delta is never sent to a client that has not received a
full frame on the SAME connection, and never across a generation change.
"""
from __future__ import annotations

from typing import Any

DELTA_VERSION = 1
SET, DEL = "set", "del"


def _is_doc(value) -> bool:
    return isinstance(value, dict)


def diff(old: Any, new: Any, _path: tuple = ()) -> list:
    """The operations that turn `old` into `new`."""
    if not (_is_doc(old) and _is_doc(new)):
        return [] if old == new else [[SET, list(_path), new]]
    ops: list = []
    for key in sorted(set(old) | set(new), key=str):
        if key not in new:
            ops.append([DEL, list(_path) + [key]])
        elif key not in old:
            ops.append([SET, list(_path) + [key], new[key]])
        else:
            ops.extend(diff(old[key], new[key], _path + (key,)))
    return ops


def apply(old: Any, ops: list) -> Any:
    """`old` with `ops` applied — a new tree; `old` is not mutated. `ValueError` on a malformed op."""
    if not ops:
        return old
    root = _clone_dicts(old)
    for op in ops:
        if not isinstance(op, list) or len(op) < 2 or op[0] not in (SET, DEL):
            raise ValueError(f"malformed delta op: {op!r}")
        kind, path = op[0], op[1]
        if not isinstance(path, list) or (kind == SET and len(op) != 3) or (kind == DEL and len(op) != 2):
            raise ValueError(f"malformed delta op: {op!r}")
        if not path:
            if kind == DEL:
                raise ValueError("cannot delete the root")
            root = _clone_dicts(op[2])
            continue
        node = root
        for key in path[:-1]:
            child = node.get(key) if isinstance(node, dict) else None
            if not isinstance(child, dict):
                child = {}
                node[key] = child
            node = child
        if kind == SET:
            node[path[-1]] = _clone_dicts(op[2])
        else:
            node.pop(path[-1], None)
    return root


def _clone_dicts(value: Any) -> Any:
    """Copy the dict SPINE so an apply never writes into the caller's tree; lists and leaves are
    shared, because an op replaces a list whole and never reaches inside one."""
    if isinstance(value, dict):
        return {k: _clone_dicts(v) for k, v in value.items()}
    return value
