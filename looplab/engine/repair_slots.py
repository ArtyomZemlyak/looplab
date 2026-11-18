"""A dispatched evaluation's physical slot, independent of its lifecycle ownership.

The dispatcher acquired the token before launching the child. An LLM repair may return it;
only a later sandbox attempt reacquires it. The lifecycle remains owned by the dispatcher.
"""
from __future__ import annotations


class RepairableEvalSlot:
    def __init__(self, semaphore=None):
        self.semaphore = semaphore
        self.held = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        # The dispatcher's finally releases time/devices FIRST, then this token.
        return False

    def release_for_repair(self):
        if self.held:
            self.held = False
            if self.semaphore is not None:
                self.semaphore.release()

    async def reacquire(self):
        if not self.held:
            if self.semaphore is not None:
                await self.semaphore.acquire()
            self.held = True

    def close(self):
        self.release_for_repair()


def occupied_lifecycles(engine, inflight):
    """Parked repair is still owned and must never be dispatched twice."""
    return len(set(inflight) - getattr(engine, '_eval_repair_yielded', set()))
