"""The run's LLM spend as a RESERVE-COMMIT budget every concurrent caller draws on.

`CostAccountant` (core/llm.py) is per CLIENT and commits POST HOC: it learns what a call cost when
the response lands, and its `limit` — when anyone set one, which `make_llm_client` never did —
bounded one client's own spend, not the run's. `llm_broker.py` is concurrency ADMISSION with no
notion of money. So concurrent roles could not reserve against one cap: with N callers in flight
under a cap the ledger reads as under budget until all N land, and the run overshoots by up to
N calls — the asyncio fan-out overshoot the Token Budgets measurement names (doc 52 row 15; the
doc 27 marker `no-shared-reserve-commit-run-budget`).

`RunBudget` is ONE object per run, attached to the run's broker, and it does the reserve half:

* `reserve()` runs at `LLMConcurrencyBroker.borrow()`, BEFORE the request is queued, and holds an
  ESTIMATE of the call — the mean cost per priced call and the mean tokens per call over what the
  run has already committed. Nothing is reserved before the first committed call (an estimate of
  nothing is nothing), so a run's first fan-out admits as it always did and every later one is
  bounded by what the run itself measured. A reservation that would carry committed + reserved
  past either cap is REFUSED with `BudgetExceeded`, the same exception the accountant raises when
  a landed call crosses its own limit, so every `except BudgetExceeded: raise` funnel in the tree
  ends the run for the same reason through the same door.
* `commit()` is fed by the durable ledger's sink (`engine/costs.py`), i.e. by the usage the
  provider actually reported, and `seed()` is what a resumed run calls with the `llm_usage` rows
  already on disk — the cap holds across restarts because the ledger is the source of truth for
  both halves.

Two caps, both OFF by default (`Settings.llm_cost_limit` / `llm_token_limit` = 0): a provider
that prices calls gives the cost cap meaning, a local model that prices nothing leaves `spent` a
floor (the accountant's own caveat) and the token cap is the one that can still hold. A budget
with neither cap reserves nothing and refuses nothing, byte-identical to the broker before it.
"""
from __future__ import annotations

import math
import threading
from dataclasses import dataclass
from typing import Iterable, Mapping, Optional

from looplab.core.errors import BudgetExceeded


@dataclass(frozen=True)
class Reservation:
    cost: float
    tokens: int


def _cap(value, *, integral: bool) -> Optional[float | int]:
    """0 / None / non-finite / negative -> no cap; otherwise the positive cap."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return int(number) if integral else number


class RunBudget:
    def __init__(self, cost_limit: Optional[float] = None, token_limit: Optional[int] = None):
        self.cost_limit: Optional[float] = _cap(cost_limit, integral=False)
        self.token_limit: Optional[int] = _cap(token_limit, integral=True)
        self._lock = threading.Lock()
        self.committed_cost = 0.0
        self.committed_tokens = 0
        self.committed_calls = 0
        self.priced_calls = 0
        self.reserved_cost = 0.0
        self.reserved_tokens = 0
        self.reservations = 0
        self.refusals = 0
        self.seeded = False

    @property
    def enabled(self) -> bool:
        return self.cost_limit is not None or self.token_limit is not None

    # ------------------------------------------------------------------ the estimate
    def estimate(self) -> tuple[float, int]:
        """Mean cost per PRICED call and mean tokens per call, over what this run committed."""
        with self._lock:
            return self._estimate_locked()

    def _estimate_locked(self) -> tuple[float, int]:
        cost = (self.committed_cost / self.priced_calls) if self.priced_calls > 0 else 0.0
        tokens = (self.committed_tokens // self.committed_calls) if self.committed_calls > 0 else 0
        return cost, tokens

    # ------------------------------------------------------------------ reserve / release
    def reserve(self) -> Optional[Reservation]:
        """Hold one call's estimate, or refuse it. None when no cap is set (nothing to hold)."""
        if not self.enabled:
            return None
        with self._lock:
            est_cost, est_tokens = self._estimate_locked()
            if self.cost_limit is not None:
                would = self.committed_cost + self.reserved_cost + est_cost
                if self.committed_cost >= self.cost_limit or would > self.cost_limit:
                    self.refusals += 1
                    raise BudgetExceeded(
                        f"run LLM cost budget: committed {self.committed_cost:.4f} + reserved "
                        f"{self.reserved_cost:.4f} + this call's estimate {est_cost:.4f} exceeds "
                        f"llm_cost_limit {self.cost_limit:.4f}")
            if self.token_limit is not None:
                would_tokens = self.committed_tokens + self.reserved_tokens + est_tokens
                if self.committed_tokens >= self.token_limit or would_tokens > self.token_limit:
                    self.refusals += 1
                    raise BudgetExceeded(
                        f"run LLM token budget: committed {self.committed_tokens} + reserved "
                        f"{self.reserved_tokens} + this call's estimate {est_tokens} exceeds "
                        f"llm_token_limit {self.token_limit}")
            self.reserved_cost += est_cost
            self.reserved_tokens += est_tokens
            self.reservations += 1
            return Reservation(cost=est_cost, tokens=est_tokens)

    def release(self, reservation: Optional[Reservation]) -> None:
        if reservation is None:
            return
        with self._lock:
            self.reserved_cost = max(0.0, self.reserved_cost - reservation.cost)
            self.reserved_tokens = max(0, self.reserved_tokens - reservation.tokens)
            self.reservations = max(0, self.reservations - 1)

    # ------------------------------------------------------------------ commit / seed
    def commit(self, delta: Mapping) -> None:
        """One provider-call delta as the ledger sanitized it (`engine/costs.py::sanitize_usage_delta`)."""
        cost = float(delta.get("cost", 0.0) or 0.0)
        tokens = int(delta.get("total_tokens", 0) or 0)
        calls = int(delta.get("calls", 0) or 0)
        priced = int(delta.get("priced_calls", 0) or 0)
        if not math.isfinite(cost) or cost < 0:
            cost = 0.0
        with self._lock:
            self.committed_cost += cost
            self.committed_tokens += max(0, tokens)
            self.committed_calls += max(0, calls)
            self.priced_calls += max(0, priced)

    def seed(self, deltas: Iterable[Mapping]) -> bool:
        """Adopt a resumed run's durable `llm_usage` rows ONCE; later calls are no-ops (False)."""
        with self._lock:
            if self.seeded:
                return False
            self.seeded = True
        for delta in deltas:
            self.commit(delta)
        return True

    def snapshot(self) -> dict:
        with self._lock:
            est_cost, est_tokens = self._estimate_locked()
            return {
                "enabled": self.enabled,
                "cost_limit": self.cost_limit, "token_limit": self.token_limit,
                "committed_cost": self.committed_cost, "committed_tokens": self.committed_tokens,
                "committed_calls": self.committed_calls, "priced_calls": self.priced_calls,
                "reserved_cost": self.reserved_cost, "reserved_tokens": self.reserved_tokens,
                "reservations": self.reservations, "refusals": self.refusals,
                "estimate_cost": est_cost, "estimate_tokens": est_tokens, "seeded": self.seeded,
            }
