"""The run's LLM spend as a RESERVE-COMMIT budget every concurrent caller draws on.

`CostAccountant` (core/llm.py) is per CLIENT and commits POST HOC: it learns what a call cost when
the response lands, and its `limit` bounds one client's own spend, not the run's.

THAT LIMIT IS NO LONGER UNSET, and this paragraph said "when anyone set one, which
`make_llm_client` never did" until the 2026-09-07 merge made it false: `core/llm.py::
run_cost_accountant` now sets it from `Settings.llm_budget_usd` and shares one accountant across a
run's clients.

ONE CEILING, TWO HALVES — and until 2026-09-08 it was two ceilings racing. The tree briefly carried
a run-level USD cap on `llm_cost_limit` (reserving before the call, here) AND one on
`llm_budget_usd` (committing after it, on the accountant), both defaulting to 0.0, both raising
`BudgetExceeded`, each with a refusal sentence naming its own knob: setting only `llm_budget_usd`
bought the post-hoc commit with exactly the fan-out overshoot this module was written to remove,
setting only `llm_cost_limit` bought no `node_open_budget_floor_usd` stop, and setting both stopped
the run at whichever was lower under a message naming the other. `run_usd_ceiling` below is now the
ONE derivation both halves read: the tightest cap the operator actually declared, plus the NAME of
the knob that declared it, so the reserve half and the commit half can no longer hold different
numbers and every refusal says which knob to raise. `llm_budget_usd` stays THE documented spend
ceiling (it is the one with the node-open floor, the resume instruction and the `stop_account`
sentence); `llm_cost_limit` remains as the reserve half's own spelling for an operator who wants to
bound the reserve-commit budget beside `llm_token_limit`, and neither field changed its default,
its type or its env var.

WHY THE RESERVE HALF EXISTS AT ALL. `llm_broker.py` is concurrency ADMISSION with no notion of
money. So concurrent roles could not reserve against one cap: with N callers in flight
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

Two caps, both OFF by default (`Settings.llm_cost_limit` / `llm_budget_usd` / `llm_token_limit`
= 0): a provider that prices calls gives the cost cap meaning, a local model that prices nothing
leaves `spent` a floor (the accountant's own caveat) and the token cap is the one that can still
hold. A budget with neither cap reserves nothing and refuses nothing, byte-identical to the broker
before it.
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


# The knob a refusal names when the ceiling came from neither declaration (there is no ceiling) —
# and the one an operator is told to raise when both declared the same number. `llm_budget_usd` is
# the documented spend ceiling: it is the knob the `BudgetExceeded` message, the node-open floor
# (`CostAccountant.require_headroom`) and `docs/guide/configuration.md` already name.
DEFAULT_COST_KNOB = "llm_budget_usd"


def run_usd_ceiling(cost_limit=None, budget_usd=None) -> tuple[Optional[float], str]:
    """The run's ONE USD ceiling and the NAME of the knob that set it. `(None, "")` = no ceiling.

    THE RULE IS "TIGHTEST DECLARED WINS", and it is the only rule under which the two halves cannot
    disagree. Each half used to read one field: the reserve half here read `llm_cost_limit`, the
    commit half on `CostAccountant` read `llm_budget_usd`, so an operator who set one got half a
    ceiling and an operator who set both got a run that stopped at the lower number under a message
    naming the other one. Reading BOTH in one place, in both halves, makes a declared ceiling whole:
    reserved before the call AND committed after it, with the node-open floor underneath, whichever
    knob was typed.

    It can only ever TIGHTEN what the operator asked for — `min` over the declared caps — so no
    configuration spends more than it did before this function existed. `_cap` is the shared
    "0 / None / non-finite / negative means no cap" rule, so a knob left at its 0.0 default declares
    nothing and cannot pull the other one down.

    Ties name `llm_budget_usd` because that is the knob every operator-facing sentence in the tree
    already says (`docs/guide/configuration.md`, the refusal message's "raise `llm_budget_usd` in
    this run's `config.snapshot.json`"), and a refusal has one job: name the number to change.
    """
    caps = []
    for value, knob in ((budget_usd, DEFAULT_COST_KNOB), (cost_limit, "llm_cost_limit")):
        capped = _cap(value, integral=False)
        if capped is not None:
            caps.append((capped, knob))
    if not caps:
        return None, ""
    # `min` on the pair would break ties by comparing the KNOB NAME; break it on the value alone and
    # let the declaration order above (budget_usd first) decide, so a tie names the documented knob.
    return min(caps, key=lambda pair: pair[0])


class RunBudget:
    def __init__(self, cost_limit: Optional[float] = None, token_limit: Optional[int] = None,
                 budget_usd: Optional[float] = None):
        self.cost_limit, self.cost_knob = run_usd_ceiling(cost_limit, budget_usd)
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
                        f"{self.cost_knob} {self.cost_limit:.4f}")
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

    def adopt_cost_ceiling(self, limit, knob: str = DEFAULT_COST_KNOB) -> bool:
        """Take a ceiling declared elsewhere in the run, when it is TIGHTER than the one held.

        WHY THE ENGINE NEEDS THIS AT ALL. `EngineOptions` carries `llm_cost_limit` and not
        `llm_budget_usd` — the ceiling reaches a run through the shared `CostAccountant` that
        `core/llm.py::run_cost_accountant` mints from the run's `Settings` — so the reserve half is
        constructed before the run's accountants are known. `engine/costs.py::bind_cost_accountants`
        is the ONE place that walks them, and it hands what it finds here, which is what makes the
        two halves hold one number on the ordinary `run` path as well as in a library caller that
        builds its own accountant. Reading `Settings` a second time would be a second derivation of
        the ceiling, which is the defect this whole change removes.

        NEVER LOOSENS. A cap already held stands unless the new one is lower; an absent/0/non-finite
        figure declares nothing and changes nothing. So this can add a stop and never remove one,
        and calling it twice with the same accountant is a no-op.
        """
        capped = _cap(limit, integral=False)
        if capped is None:
            return False
        with self._lock:
            if self.cost_limit is not None and self.cost_limit <= capped:
                return False
            self.cost_limit = capped
            self.cost_knob = knob or DEFAULT_COST_KNOB
        return True

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
                # WHICH KNOB set the cost cap, beside the number. The broker publishes this snapshot
                # and a ceiling with no name is a number an operator cannot act on.
                "cost_knob": self.cost_knob,
                "committed_cost": self.committed_cost, "committed_tokens": self.committed_tokens,
                "committed_calls": self.committed_calls, "priced_calls": self.priced_calls,
                "reserved_cost": self.reserved_cost, "reserved_tokens": self.reserved_tokens,
                "reservations": self.reservations, "refusals": self.refusals,
                "estimate_cost": est_cost, "estimate_tokens": est_tokens, "seeded": self.seeded,
            }
