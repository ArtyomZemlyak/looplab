"""Embedding calls reach the durable cost ledger.

`LLMEmbedder` posts to a paid `/embeddings` endpoint with a bearer key, and
`engine/costs.py::_CHILD_ATTRS` has walked `embed` since the abstractor/embedder pair was added —
its own comment says both are "LIVE chat/embedding clients under the shipped defaults, each with
its own `CostAccountant`". The abstractor had one; the embedder did not. So the walk reached the
object, found no `accountant`, and every embed call was spent and unbilled: invisible to the
durable `llm_usage` ledger `looplab tokens` reconciles against. The knowledge index re-embeds
whenever the case store is appended to, so the residual is not small.

DRIVEN, not pinned. A source check that the attribute exists would pass on an accountant nothing
increments, and the whole defect was an object the walk could see and get nothing from — so these
tests run a fake endpoint through the real `_call` and then run the real
`engine/costs.py::find_cost_accountants` over an engine-shaped object holding the embedder.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from looplab.tools.vectorstore import LLMEmbedder, hash_embed, make_embedder


class _Body:
    """The `with opener.open(req) as resp` shape, holding one canned response body."""

    def __init__(self, payload):
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Opener:
    def __init__(self, *payloads):
        self._payloads = list(payloads)
        self.requests = 0

    def open(self, req, timeout=None):
        self.requests += 1
        payload = self._payloads.pop(0)
        if isinstance(payload, Exception):
            raise payload
        return _Body(payload)


def _embedder(*payloads, dim_fallback=4):
    embedder = LLMEmbedder("embed-model", dim_fallback=dim_fallback)
    embedder._opener = _Opener(*payloads)
    return embedder


def _ok(vectors, usage=None):
    body = {"data": [{"embedding": v} for v in vectors]}
    if usage is not None:
        body["usage"] = usage
    return body


def test_a_successful_embed_is_billed(tmp_path):
    """MUTATION: drop the `_bill` call -> the vectors arrive, the money does not, and nothing in
    the run says a provider call happened."""
    embedder = _embedder(_ok([[1.0, 0.0, 0.0, 0.0]], usage={"prompt_tokens": 8, "total_tokens": 8}))
    assert embedder.embed("hello") == [1.0, 0.0, 0.0, 0.0]

    assert embedder.accountant.calls == 1
    assert embedder.accountant.prompt_tokens == 8
    assert embedder.accountant.total_tokens == 8


def test_an_endpoint_that_states_no_cost_is_UNPRICED_not_free():
    """`cost_is_reported`'s whole point, applied here: most embedding endpoints report no amount,
    and a run priced by nobody must not roll up as a run that cost nothing."""
    embedder = _embedder(_ok([[1.0, 0.0, 0.0, 0.0]], usage={"prompt_tokens": 8, "total_tokens": 8}))
    embedder.embed("hello")
    assert embedder.accountant.calls == 1
    assert embedder.accountant.priced_calls == 0
    assert embedder.accountant.spent == 0.0


def test_a_gateway_that_states_a_cost_is_priced():
    embedder = _embedder(_ok([[1.0, 0.0, 0.0, 0.0]],
                             usage={"prompt_tokens": 8, "total_tokens": 8, "cost": 0.25}))
    embedder.embed("hello")
    assert embedder.accountant.priced_calls == 1
    assert embedder.accountant.spent == pytest.approx(0.25)


def test_a_response_the_embedder_DISCARDS_is_still_billed():
    """The provider produced it and charged for it. Billing at the bottom of `_call` would make a
    malformed batch read as a call that never happened — the same rule `CostAccountant.add` states
    for the chat path ("a successful response with missing/malformed usage still increments it")."""
    # Rows that disagree on dimension: the embedder refuses them and falls back to `hash_embed`.
    # (It commits `_dim` to the first row's length before noticing, so the fallback is at that
    # width — the point here is that the endpoint's rows are NOT what came back.)
    embedder = _embedder(_ok([[1.0, 0.0], [1.0, 0.0, 0.0]], usage={"prompt_tokens": 3}))
    out = embedder.embed_many(["a", "b"])
    assert out != [[1.0, 0.0], [1.0, 0.0, 0.0]], "the malformed batch was discarded"
    assert len({len(v) for v in out}) == 1, "and the fallback is dimensionally consistent"
    assert embedder.accountant.calls == 1, "and it was still billed"
    assert embedder.accountant.prompt_tokens == 3


@pytest.mark.parametrize("body", [
    {"data": "not a list", "usage": {"prompt_tokens": 5}},           # malformed envelope
    {"data": [], "usage": {"prompt_tokens": 5}},                     # wrong row count
    {"data": [{"embedding": []}], "usage": {"prompt_tokens": 5}},    # an empty vector
    {"data": [{"embedding": ["not", "numeric"]}], "usage": {"prompt_tokens": 5}},
    {"usage": {"prompt_tokens": 5}},                                 # no data at all
])
def test_a_body_that_CALL_ITSELF_rejects_is_still_billed(body):
    """The `_call`-level half of the rule above, and the one that pins WHERE the billing sits.

    MUTATION: move `_bill` to the bottom of `_call` (after the body is validated) -> every row here
    goes unbilled while the provider charged for all of them. The dimension-mismatch case one test
    up does NOT catch that, because `_call` accepts those rows and `embed_many` is what rejects
    them — so the bill still happens on the way out.
    """
    embedder = _embedder(body)
    embedder.embed("hello")
    assert embedder.accountant.calls == 1
    assert embedder.accountant.prompt_tokens == 5


def test_a_transport_failure_is_NOT_billed():
    """Nothing came back, so nothing was charged. Billing a raise would inflate the ledger with
    calls that never reached the provider — the inverse error, and the one that is harder to spot."""
    import urllib.error
    embedder = _embedder(urllib.error.URLError("connection refused"))
    embedder.embed("hello")
    assert embedder.accountant.calls == 0


def test_the_hash_fallback_is_not_billed():
    """It spends nothing. A local bag-of-words counted as a provider call would make an OFFLINE run
    report spend."""
    import urllib.error
    embedder = _embedder(urllib.error.URLError("down"), urllib.error.URLError("down"))
    embedder.embed("a")
    embedder.embed("b")     # breaker: the second embed does not even reach the endpoint
    assert embedder.accountant.calls == 0
    assert embedder._opener.requests == 1, "the breaker stopped calling"


def test_the_engine_accounting_walk_REACHES_it():
    """The half a source pin cannot prove. `_CHILD_ATTRS` already contained `embed`; what was
    missing was anything for the walk to find on the other end.

    MUTATION: remove the `accountant` attribute -> the walk returns an empty list here, which is
    precisely the state this closed, and it is invisible to every other test in the suite.
    """
    from looplab.engine.costs import find_cost_accountants

    embedder = _embedder(_ok([[1.0, 0.0, 0.0, 0.0]]))

    class _KnowledgeTools:
        def __init__(self, embed):
            self.embed = embed

    class _Researcher:
        def __init__(self, tools):
            self.tools = tools

    class _Engine:
        pass

    engine = _Engine()
    engine.researcher = _Researcher([_KnowledgeTools(embedder)])

    found = find_cost_accountants(engine)
    assert embedder.accountant in found, (
        "engine/costs.py walks `embed` and must find the embedder's accountant there")


def test_the_hash_embedder_has_no_accountant_and_that_is_correct():
    """`make_embedder` returns the bare function when no model is configured. It spends nothing, so
    an accountant on it would be a decoy the walk counts as a billed client."""
    class _Settings:
        embed_model = ""

    embedder = make_embedder(_Settings())
    assert embedder is hash_embed
    assert getattr(embedder, "accountant", None) is None


def test_a_shared_accountant_can_be_injected():
    """An embedder handed one by its constructor must not mint a second: two accountants for one
    client is how a walk double-counts, and `find_cost_accountants` dedupes by IDENTITY."""
    from looplab.core.llm import CostAccountant

    shared = CostAccountant()
    embedder = LLMEmbedder("m", accountant=shared)
    assert embedder.accountant is shared


def test_billing_never_breaks_an_embed():
    """The call already succeeded. A telemetry failure must not turn a good vector into a fallback."""
    embedder = _embedder(_ok([[1.0, 0.0, 0.0, 0.0]], usage={"prompt_tokens": 8}))

    class _Exploding:
        def add(self, *a, **kw):
            raise RuntimeError("ledger is down")

    embedder.accountant = _Exploding()
    assert embedder.embed("hello") == [1.0, 0.0, 0.0, 0.0]


def test_the_embedder_stops_at_the_run_ceiling_instead_of_billing_past_it():
    """The spend stop is not telemetry, and `_bill` used to treat it as telemetry.

    `accountant.add` is WHERE `BudgetExceeded` is raised, and `_bill` wrapped it in a blind
    `except Exception: pass` whose comment said telemetry must never break an embed. True of a
    malformed usage payload; false of the ceiling. Driven on the shipped code before the fix:
    twenty $0.25 embeds against a $1.00 limit committed $5.00 and raised nothing — a 400 %
    overshoot on a call the knowledge index repeats for the life of the run.

    Both halves are asserted here, because the containment this replaces was real: the ceiling must
    propagate, AND a junk payload must still not break an embed.
    """
    from looplab.core.llm import BudgetExceeded, CostAccountant
    from looplab.tools.vectorstore import LLMEmbedder

    accountant = CostAccountant(limit=1.0)
    embedder = LLMEmbedder.__new__(LLMEmbedder)
    embedder.accountant = accountant

    def bill_one():
        LLMEmbedder._bill(embedder, {"usage": {"prompt_tokens": 1000, "total_tokens": 1000,
                                               "cost": 0.25}})

    # The ceiling fires when spend REACHES the limit, not when it passes it — the refusal says
    # "$1.0000 of the $1.0000" — so three quarter-dollar calls fit and the fourth is the one that
    # must speak. Written from the observed refusal rather than from what I first assumed.
    for _ in range(3):
        bill_one()
    with pytest.raises(BudgetExceeded):
        bill_one()

    # …and the containment that was there for a reason is still there. A FRESH accountant, because
    # the one above is now AT its ceiling and would refuse these too — which is correct behaviour
    # and would make this half assert nothing about containment.
    embedder.accountant = CostAccountant(limit=1.0)
    LLMEmbedder._bill(embedder, {"usage": "not a dict"})
    LLMEmbedder._bill(embedder, "not a body")

    class _Broken:
        def add(self, *_a, **_k):
            raise RuntimeError("the accountant itself is broken")

    embedder.accountant = _Broken()
    LLMEmbedder._bill(embedder, {"usage": {"total_tokens": 1}})   # must not raise


# --------------------------------------------------------------------------------------------
# Review 2026-09-22, CORE-01 part 2: BILLED IS NOT GOVERNED. Every embed reached a ledger, but the
# ledger was the embedder's OWN: `make_embedder` handed `LLMEmbedder` no accountant, so it minted a
# private one with no limit — `llm_budget_usd` never saw an embed, and `_bill`'s re-raise of the
# ceiling (above) could not fire on the shipped path. And `_call` posted with no
# `llm_request_permit`, so the run's reserve half (`RunBudget`, metered at the broker's `borrow()`)
# and the call meter never saw the request at all, and a cancelled caller still sent it.
# --------------------------------------------------------------------------------------------

def _run_settings(**kw):
    from looplab.core.config import Settings

    return Settings(embed_model="embed-model", embed_base_url="http://127.0.0.1:9/v1",
                    llm_base_url="http://127.0.0.1:9/v1", **kw)


def test_the_run_embedder_meters_on_the_RUN_accountant_and_stops_at_its_ceiling():
    """MUTATION: drop `accountant=run_cost_accountant(settings)` from `make_embedder` -> the
    embedder carries a private, unlimited accountant, the identity asserts fail, and the two $0.25
    embeds below spend $0.50 against a $0.50 ceiling without a word."""
    from looplab.core.llm import BudgetExceeded, make_llm_client, run_cost_accountant

    settings = _run_settings(llm_budget_usd=0.5)
    embedder = make_embedder(settings)
    assert isinstance(embedder, LLMEmbedder)
    assert embedder.accountant is run_cost_accountant(settings), (
        "the embedder meters on an accountant the run's ceiling does not own")
    assert embedder.accountant is make_llm_client(settings).accountant, (
        "the run's chat clients and its embedder must share ONE ledger")

    priced = {"prompt_tokens": 4, "total_tokens": 4, "cost": 0.25}
    embedder._opener = _Opener(_ok([[1.0, 0.0]], usage=priced), _ok([[0.0, 1.0]], usage=priced))
    embedder.embed("first")
    with pytest.raises(BudgetExceeded):
        embedder.embed("second")
    assert run_cost_accountant(settings).spent == pytest.approx(0.5)


class _PermitProbe(_Opener):
    """Records how many broker permits were out WHILE the request was on the wire."""

    def __init__(self, broker, *payloads):
        super().__init__(*payloads)
        self.broker = broker
        self.borrowed: list[int] = []

    def open(self, req, timeout=None):
        self.borrowed.append(self.broker.snapshot()["borrowed"])
        return super().open(req, timeout=timeout)


def test_an_embed_request_is_admitted_by_the_run_broker_and_counted_by_its_meter():
    """The permit is where the run's reserve half and its per-window call meter live
    (`core/llm_broker.py::llm_request_permit`). MUTATION: remove the permit from `_call` -> the
    request goes out holding nothing (`borrowed == [0]`) and the meter reads zero calls."""
    from looplab.core.llm_broker import (LLMConcurrencyBroker, ProviderCallMeter,
                                         llm_broker_scope, provider_call_meter)

    broker = LLMConcurrencyBroker(total=1)
    embedder = _embedder()
    embedder._opener = _PermitProbe(broker, _ok([[1.0, 0.0, 0.0, 0.0]]))
    meter = ProviderCallMeter()
    with llm_broker_scope(broker), provider_call_meter(meter):
        assert embedder.embed("hello") == [1.0, 0.0, 0.0, 0.0]
    assert embedder._opener.borrowed == [1], "the embed request was sent outside the broker"
    assert meter.calls == 1, "the run's call meter never saw the embed request"
    assert broker.snapshot()["borrowed"] == 0, "the permit leaked"


def test_a_run_budget_that_cannot_afford_the_embed_refuses_it_BEFORE_it_is_sent():
    """The reserve half refuses at admission, so the refused request never leaves. It is the
    ceiling, not an endpoint failure: it propagates, and the breaker does not count it."""
    from looplab.core.llm import BudgetExceeded
    from looplab.core.llm_broker import LLMConcurrencyBroker, llm_broker_scope
    from looplab.core.llm_budget import RunBudget

    budget = RunBudget(cost_limit=0.5)
    budget.commit({"cost": 0.5, "calls": 1, "priced_calls": 1, "total_tokens": 10})
    embedder = _embedder(_ok([[1.0, 0.0, 0.0, 0.0]]))
    with llm_broker_scope(LLMConcurrencyBroker(budget=budget)):
        with pytest.raises(BudgetExceeded):
            embedder.embed("hello")
    assert embedder._opener.requests == 0, "a refused reservation still sent the request"
    assert embedder._live is None and embedder._misses == 0, "the refusal tripped the breaker"


def test_a_cancelled_caller_sends_no_embed_request_and_the_breaker_does_not_count_it():
    """Same cancellation point as the chat client's `_post`: checked before the permit, per
    request. A cancel is the CALLER's decision, not the endpoint failing — counting it as a miss
    would, on a first embed, degrade the embedder to `hash_embed` for the rest of the run."""
    from looplab.core.llm import LLMCancelled, cancel_check_scope

    embedder = _embedder(_ok([[1.0, 0.0, 0.0, 0.0]]))
    with cancel_check_scope(lambda: True):
        with pytest.raises(LLMCancelled):
            embedder.embed("hello")
    assert embedder._opener.requests == 0
    assert embedder._live is None and embedder._misses == 0
    assert embedder.embed("hello") == [1.0, 0.0, 0.0, 0.0], "the endpoint was written off"


def test_the_ENGINE_level_embedder_is_reconciled_even_when_no_role_holds_a_client():
    """`Engine(embedder=...)` stores it as `_embedder`, which no role reaches. On a run whose
    roles hold no LLM client (a toy backend with an embedding model configured) the walk found
    nothing, so its embeds reached no durable `llm_usage` row. MUTATION: drop `_embedder` from
    `engine/costs.py::_ROOT_ATTRS` -> the list below is empty."""
    from looplab.engine.costs import find_cost_accountants

    embedder = _embedder(_ok([[1.0, 0.0, 0.0, 0.0]]))

    class _Engine:
        pass

    engine = _Engine()
    engine._embedder = embedder
    assert find_cost_accountants(engine) == [embedder.accountant]


# ---- the embed is a generation span (review 2026-09-22, doc 66 §6 item 6 — the W2-2 tail) ----
#
# Billed and admitted, an embed still opened no `generation` span: `record_paid_call` found none on
# the stack (the chat turn's span has closed by the time a tool embeds), so its tokens reached the
# durable `llm_usage` ledger and no span, and `looplab tokens` could only show them as residual.

def _spans_of(path):
    import orjson
    return [orjson.loads(line) for line in path.read_bytes().splitlines()]


def test_an_embed_is_a_generation_span_under_the_phase_that_made_it(tmp_path):
    """MUTATION: drop the `tracing.generation` wrapper in `_call` -> no generation span, and the
    reconciliation below reports the 8 tokens as residual instead of the `knowledge_index` row."""
    from looplab.core.tracing import JsonlSpanExporter, Tracer
    from looplab.events.token_spend import token_spend_by_phase

    embedder = _embedder(_ok([[1.0, 0.0, 0.0, 0.0]],
                             usage={"prompt_tokens": 8, "total_tokens": 8, "cost": 0.001}))
    t = Tracer(JsonlSpanExporter(tmp_path / "s.jsonl"), run_id="r")
    with t.span("knowledge_index", new_trace=True):
        assert embedder.embed("the embedded text") == [1.0, 0.0, 0.0, 0.0]

    spans = _spans_of(tmp_path / "s.jsonl")
    (gen,) = [s for s in spans if s.get("kind") == "generation"]
    attrs = gen["attributes"]
    assert attrs["op"] == "embed" and attrs["model"] == "embed-model"
    assert attrs["phase"] == "knowledge_index"
    assert attrs["usage"]["total"] == 8 and attrs["cost"] == pytest.approx(0.001)
    # The texts are not a conversation and must not ride into the span.
    assert "the embedded text" not in json.dumps(gen)

    spend = token_spend_by_phase(spans, ledger_total=embedder.accountant.total_tokens)
    assert [(r["phase"], r["tokens"]) for r in spend["rows"]] == [("knowledge_index", 8)]
    assert spend["residual"] == 0


def test_a_failed_embed_is_a_generation_that_says_so(tmp_path):
    """A transport failure spent nothing, and the span says the call happened and failed rather
    than vanishing — the fallback vector is still returned."""
    from looplab.core.tracing import JsonlSpanExporter, Tracer

    embedder = _embedder(OSError("connection refused"))
    t = Tracer(JsonlSpanExporter(tmp_path / "s.jsonl"), run_id="r")
    with t.span("knowledge_index", new_trace=True):
        assert embedder.embed("x") == hash_embed("x", dim=4)
    (gen,) = [s for s in _spans_of(tmp_path / "s.jsonl") if s.get("kind") == "generation"]
    assert gen["attributes"]["embed_failed"] is True
    assert "usage" not in gen["attributes"]
