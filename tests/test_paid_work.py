"""UI-side paid-work bookkeeping: the retained-cost retry registry (`flush_pending_run_costs`).

The registry is how a UI model call's usage survives a generation mismatch or a failed ledger
reconcile — the entry stays pending and is retried later instead of the cost being lost.
"""
from __future__ import annotations

import pytest


def test_a_raising_lease_release_does_not_drop_the_rest_of_the_retry_registry(tmp_path, monkeypatch):
    """`entries` is POPPED from `pending` before the loop, so an exception escaping the release used
    to take the whole batch with it: every already-`retained` entry AND every not-yet-examined one
    vanished from the retry registry, their leases and ledgers gone with no record."""
    import threading
    from types import SimpleNamespace

    from looplab.serve import paid_work

    class _Ctx:
        def __init__(self, boom=False):
            self.boom = boom
            self.released = False

        def __exit__(self, *_a):
            if self.boom:
                raise RuntimeError("lease release failed")
            self.released = True

    lock = threading.Lock()
    pending: dict = {}
    monkeypatch.setattr(paid_work, "pending_run_cost_state",
                        lambda _srv: (lock, pending, {}))
    monkeypatch.setattr(paid_work, "_flush_durable_run_costs_unlocked", lambda _rd: True)
    # Entry 0 is not durable (retained), entry 1 raises on release, entry 2 is never examined.
    monkeypatch.setattr(paid_work, "reconcile_cost_accountants",
                        lambda ledger: ledger != "stale")

    key = paid_work.pending_run_cost_key(tmp_path)
    entries = [
        {"generation": "g", "ledger": "stale", "activity_ctx": _Ctx()},
        {"generation": "g", "ledger": "ok", "activity_ctx": _Ctx(boom=True)},
        {"generation": "g", "ledger": "ok", "activity_ctx": _Ctx()},
    ]
    pending[key] = list(entries)
    srv = SimpleNamespace(commands=SimpleNamespace(run_generation=lambda _rd: "g"))

    with pytest.raises(RuntimeError, match="lease release failed"):
        paid_work.flush_pending_run_costs(srv, tmp_path)

    survivors = pending.get(key, [])
    assert entries[0] in survivors, "the already-retained entry was dropped"
    assert entries[2] in survivors, "the unexamined tail was dropped"
    assert entries[1] not in survivors                 # it was released-or-failed, not retryable
