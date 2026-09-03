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
