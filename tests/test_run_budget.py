"""The run's LLM spend as a RESERVE-COMMIT budget every concurrent caller draws on (doc 52 row 15).

`CostAccountant` is per client and commits post hoc; the broker was concurrency admission with no
notion of money; nothing set a run-level cap at all. `core/llm_budget.py::RunBudget` rides the
broker: `borrow()` reserves one call's ESTIMATE (the run's own mean per committed call) before the
request is queued and refuses with `BudgetExceeded` when committed + reserved + estimate would cross
a cap; the durable ledger's sink commits what the provider actually reported; a resume seeds the
committed half from the `llm_usage` rows already on disk.
"""
from __future__ import annotations

import threading

import pytest

from looplab.core.config import Settings
from looplab.core.errors import BudgetExceeded
from looplab.core.llm import CostAccountant, OpenAICompatibleClient
from looplab.core.llm_broker import LLMConcurrencyBroker, llm_broker_scope, llm_lane_scope
from looplab.core.llm_budget import RunBudget
from looplab.engine.costs import _payload, bind_cost_accountants, seed_run_budget
from looplab.engine.options import EngineOptions
from looplab.events.types import EV_LLM_USAGE

from factories import make_engine


def _delta(cost: float, tokens: int = 100, priced: int = 1) -> dict:
    return {"cost": cost, "calls": 1, "priced_calls": priced, "prompt_tokens": tokens // 2,
            "completion_tokens": tokens - tokens // 2, "total_tokens": tokens}


# ------------------------------------------------------------------ the object

def test_no_cap_reserves_nothing_and_refuses_nothing():
    budget = RunBudget()
    assert budget.enabled is False
    assert budget.reserve() is None
    budget.commit(_delta(5.0))
    assert budget.reserve() is None
    assert budget.snapshot()["refusals"] == 0


def test_zero_negative_and_non_finite_caps_mean_no_cap():
    for value in (0, 0.0, -1, float("inf"), float("nan"), None, "x"):
        assert RunBudget(cost_limit=value, token_limit=value).enabled is False, value


def test_nothing_is_reserved_before_the_first_committed_call():
    """An estimate of nothing is nothing: a run's first fan-out admits as it always did."""
    budget = RunBudget(cost_limit=1.0)
    res = budget.reserve()
    assert res is not None and res.cost == 0.0 and res.tokens == 0
    assert budget.snapshot()["reserved_cost"] == 0.0
    budget.release(res)


def test_the_estimate_is_the_runs_own_mean_and_a_reservation_holds_it():
    budget = RunBudget(cost_limit=10.0, token_limit=10_000)
    budget.commit(_delta(0.2, tokens=100))
    budget.commit(_delta(0.4, tokens=300))
    assert budget.estimate() == (pytest.approx(0.3), 200)
    res = budget.reserve()
    snap = budget.snapshot()
    assert snap["reserved_cost"] == pytest.approx(0.3) and snap["reserved_tokens"] == 200
    assert snap["reservations"] == 1
    budget.release(res)
    snap = budget.snapshot()
    assert snap["reserved_cost"] == 0.0 and snap["reserved_tokens"] == 0 and snap["reservations"] == 0


def test_an_unpriced_call_does_not_dilute_the_cost_estimate():
    """`spent` is a floor on a gateway that prices nothing; the mean is per PRICED call."""
    budget = RunBudget(cost_limit=10.0)
    budget.commit(_delta(0.5, priced=1))
    budget.commit(_delta(0.0, priced=0))
    assert budget.estimate()[0] == pytest.approx(0.5)


def test_reserve_refuses_when_committed_plus_reserved_plus_estimate_crosses_the_cap():
    """THE FAN-OUT BOUND. With 0.4 committed and a 1.0 cap the second CONCURRENT caller is refused:
    0.4 committed + 0.4 held by the first + its own 0.4 estimate = 1.2."""
    budget = RunBudget(cost_limit=1.0)
    budget.commit(_delta(0.4))
    first = budget.reserve()
    with pytest.raises(BudgetExceeded, match="llm_cost_limit"):
        budget.reserve()
    assert budget.snapshot()["refusals"] == 1
    budget.release(first)
    second = budget.reserve()            # the first returned its hold; the same call fits again
    assert second is not None
    budget.release(second)


def test_an_exhausted_budget_refuses_even_a_zero_estimate():
    budget = RunBudget(token_limit=100)
    budget.commit({"cost": 0.0, "calls": 1, "priced_calls": 0, "total_tokens": 100})
    with pytest.raises(BudgetExceeded, match="llm_token_limit"):
        budget.reserve()


def test_the_token_cap_holds_where_the_cost_cap_cannot():
    budget = RunBudget(token_limit=250)
    budget.commit(_delta(0.0, tokens=100, priced=0))
    budget.commit(_delta(0.0, tokens=100, priced=0))     # committed 200, estimate 100
    with pytest.raises(BudgetExceeded, match="token"):
        budget.reserve()


def test_seed_adopts_the_ledger_once():
    budget = RunBudget(cost_limit=5.0)
    assert budget.seed([_delta(1.0), _delta(2.0)]) is True
    assert budget.snapshot()["committed_cost"] == pytest.approx(3.0)
    assert budget.seed([_delta(100.0)]) is False, "a second seed is a no-op"
    assert budget.snapshot()["committed_cost"] == pytest.approx(3.0)


# ------------------------------------------------------------------ at the broker's permit

def test_the_broker_reserves_at_borrow_and_releases_at_exit():
    budget = RunBudget(cost_limit=1.0)
    budget.commit(_delta(0.4))
    broker = LLMConcurrencyBroker(total=None, budget=budget)
    with broker.borrow("build"):
        assert broker.snapshot()["budget"]["reservations"] == 1
    assert broker.snapshot()["budget"]["reservations"] == 0


def test_a_refused_reservation_never_touches_the_queue():
    budget = RunBudget(cost_limit=1.0)
    budget.commit(_delta(0.4))
    broker = LLMConcurrencyBroker(total=1, budget=budget)
    entered = threading.Event()
    release = threading.Event()

    def holder():
        with broker.borrow("build"):
            entered.set()
            release.wait(3)

    thread = threading.Thread(target=holder, daemon=True)
    thread.start()
    entered.wait(3)
    with pytest.raises(BudgetExceeded):
        with broker.borrow("build"):
            pass
    snap = broker.snapshot()
    assert snap["waiting_by_lane"]["build"] == 0, "a refused caller must not leave a ghost ticket"
    assert snap["borrowed"] == 1
    release.set()
    thread.join(3)
    assert broker.snapshot()["borrowed"] == 0


def test_a_broker_without_a_budget_is_byte_identical():
    broker = LLMConcurrencyBroker(total=1)
    assert broker.budget is None and broker.snapshot()["budget"] is None
    with broker.borrow("build"):
        pass


def _transport_client(transport):
    """The real `_post` admission path over a fake transport (`tests/test_llm_broker.py`'s shape)."""
    client = OpenAICompatibleClient.__new__(OpenAICompatibleClient)
    client.model = "fake"
    client.temperature = 0.7
    client._cache = None
    client._max_retries = 0
    client.stream = False
    client._stream_stalls = 0
    client.reasoning = {}
    client._reasoning_ok = True
    client.base_url = "http://fake"
    client.accountant = CostAccountant()
    client._sdk_chat = transport
    return client


def test_a_refusal_propagates_out_of_the_client_as_the_hard_budget_stop():
    """The client's retry ladder catches provider errors; `BudgetExceeded` is not one and must
    reach the caller — the same door the accountant's own limit uses."""
    calls = []

    def transport(payload, use_stream):
        calls.append(payload)
        return {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}], "usage": {}}

    budget = RunBudget(cost_limit=1.0)
    budget.commit(_delta(0.6))
    budget.commit(_delta(0.5))                       # committed 1.1 >= cap
    client = _transport_client(transport)
    broker = LLMConcurrencyBroker(total=None, budget=budget)
    with llm_broker_scope(broker), llm_lane_scope("build"):
        with pytest.raises(BudgetExceeded):
            client._post({"model": "fake", "messages": [], "temperature": 0.7})
    assert calls == [], "the request never left: refused BEFORE the transport"


# ------------------------------------------------------------------ the engine and the ledger

def test_settings_reach_the_engines_broker_budget(tmp_path):
    options = EngineOptions.from_settings(Settings(llm_cost_limit=2.5, llm_token_limit=4096))
    engine = make_engine(tmp_path / "run", options=options)
    assert engine._llm_cost_limit == 2.5 and engine._llm_token_limit == 4096
    budget = engine._llm_broker.budget
    assert budget is engine._llm_budget
    assert budget.cost_limit == 2.5 and budget.token_limit == 4096


def test_the_defaults_carry_no_cap_and_the_options_agree_with_settings(tmp_path):
    assert Settings().llm_cost_limit == EngineOptions().llm_cost_limit == 0.0
    assert Settings().llm_token_limit == EngineOptions().llm_token_limit == 0
    engine = make_engine(tmp_path / "run")
    assert engine._llm_broker.budget is not None and engine._llm_broker.budget.enabled is False


def test_the_ledger_sink_commits_into_the_budget(tmp_path):
    """The COMMIT half: what the provider reported, as the ledger sanitized it."""
    from types import SimpleNamespace

    options = EngineOptions.from_settings(Settings(llm_cost_limit=10.0))
    engine = make_engine(tmp_path / "run", options=options)
    accountant = CostAccountant()
    engine.researcher.client = SimpleNamespace(accountant=accountant)
    bind_cost_accountants(engine)
    accountant.add(0.25, usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})
    snap = engine._llm_budget.snapshot()
    assert snap["committed_cost"] == pytest.approx(0.25)
    assert snap["committed_tokens"] == 15 and snap["committed_calls"] == 1


def test_a_resumed_run_seeds_the_budget_from_the_durable_ledger(tmp_path):
    """The cap holds across restarts because the ledger, not a fresh accountant, is the source."""
    run_dir = tmp_path / "run"
    first = make_engine(run_dir, options=EngineOptions.from_settings(Settings(llm_cost_limit=1.0)))
    first.store.append(EV_LLM_USAGE, _payload("a" * 32, _delta(0.7)))
    first.store.append(EV_LLM_USAGE, _payload("b" * 32, _delta(0.2)))
    second = make_engine(run_dir, options=EngineOptions.from_settings(Settings(llm_cost_limit=1.0)))
    # Seeded at CONSTRUCTION: `bind_cost_accountants` runs inside `Engine.__init__` and seeds first,
    # so the explicit call is the idempotence check, not the seed.
    snap = second._llm_budget.snapshot()
    assert snap["seeded"] is True
    assert snap["committed_cost"] == pytest.approx(0.9) and snap["committed_calls"] == 2
    assert seed_run_budget(second) is False
    with pytest.raises(BudgetExceeded):        # 0.9 committed + a 0.45 estimate > 1.0
        second._llm_broker.borrow("build").__enter__()
