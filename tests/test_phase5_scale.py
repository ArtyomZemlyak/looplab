"""Phase 5 (docs/12): list-wise best-of-N tie-break, deterministic LLM response cache,
endgame-reserve strategy rule."""
from __future__ import annotations

import pytest

from looplab.core.models import Idea, RunState
from looplab.agents.strategist import RuleStrategist, StrategyContext
from looplab.search.best_of_n import BestOfNDeveloper, _listwise_pick


# --------------------------------------------------------------------------- #
# D10 list-wise best-of-N
# --------------------------------------------------------------------------- #

class _VaryingDev:
    """Returns a rotating set of candidates; all statically valid so they tie on score."""
    is_code_generating = True

    def __init__(self, codes):
        self._codes = codes
        self._i = 0
        self.client = object()      # non-None so the list-wise path is reachable
        self.last_files = {}
        self.last_deleted = []

    def implement(self, idea):
        c = self._codes[self._i % len(self._codes)]
        self._i += 1
        return c


_VALID_A = "import json\nprint(json.dumps({'metric': 0.1}))\n"
_VALID_B = "import json\nprint(json.dumps({'metric': 0.2}))\n"


def test_listwise_breaks_tie_with_selector(monkeypatch):
    dev = BestOfNDeveloper(_VaryingDev([_VALID_A, _VALID_B]), n=2, listwise=True)
    # force the selector to pick index 1 (the second tied candidate)
    monkeypatch.setattr("looplab.search.best_of_n._listwise_pick", lambda c, i, cands: 1)
    out = dev.implement(Idea(operator="draft", params={}))
    assert out == _VALID_B


def test_listwise_off_takes_first_top_scorer(monkeypatch):
    dev = BestOfNDeveloper(_VaryingDev([_VALID_A, _VALID_B]), n=2, listwise=False)
    called = {"n": 0}
    monkeypatch.setattr("looplab.search.best_of_n._listwise_pick",
                        lambda *a: called.__setitem__("n", called["n"] + 1) or 1)
    out = dev.implement(Idea(operator="draft", params={}))
    assert out == _VALID_A and called["n"] == 0    # selector never consulted


def test_listwise_not_consulted_when_no_tie(monkeypatch):
    # one candidate is invalid (syntax error) -> the valid one wins on score, no tie
    dev = BestOfNDeveloper(_VaryingDev([_VALID_A, "def ("]), n=2, listwise=True)
    called = {"n": 0}
    monkeypatch.setattr("looplab.search.best_of_n._listwise_pick",
                        lambda *a: called.__setitem__("n", called["n"] + 1) or 0)
    out = dev.implement(Idea(operator="draft", params={}))
    assert out == _VALID_A and called["n"] == 0


def test_listwise_pick_falls_back_on_error():
    class _BadClient:
        pass
    # parse_structured will fail on a bare object -> index 0
    assert _listwise_pick(_BadClient(), Idea(operator="draft", params={}),
                          [_VALID_A, _VALID_B]) == 0


# --------------------------------------------------------------------------- #
# T7 deterministic LLM cache
# --------------------------------------------------------------------------- #

def test_llm_cache_only_deterministic(monkeypatch):
    from looplab.core.llm import OpenAICompatibleClient

    c = OpenAICompatibleClient("m", cache=True, temperature=0.0)
    # temp 0 -> cacheable
    assert c._cache_key({"messages": [{"role": "user", "content": "hi"}], "temperature": 0}) is not None
    # temp > 0 -> never cached (sampling must vary)
    assert c._cache_key({"messages": [{"role": "user", "content": "hi"}], "temperature": 0.7}) is None


def test_llm_cache_disabled_by_default():
    from looplab.core.llm import OpenAICompatibleClient
    c = OpenAICompatibleClient("m", temperature=0.0)
    assert c._cache is None
    assert c._cache_key({"messages": [], "temperature": 0}) is None


def test_llm_cache_serves_stored_body(monkeypatch):
    from looplab.core.llm import OpenAICompatibleClient

    c = OpenAICompatibleClient("m", cache=True, temperature=0.0)
    calls = {"n": 0}
    body = {"choices": [{"message": {"content": "cached"}}], "usage": {}}

    # first call: miss -> hits the (faked) network path once and stores
    def fake_urlopen(*a, **k):
        calls["n"] += 1
        raise AssertionError("network should not be called on a cache hit")

    payload = {"model": "m", "messages": [{"role": "user", "content": "q"}], "temperature": 0}
    ck = c._cache_key(payload)
    c._cache[ck] = body                       # pre-seed the cache
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    out = c._post(payload)
    assert out is body and calls["n"] == 0    # served from cache, no network


# --------------------------------------------------------------------------- #
# P2 endgame reserve
# --------------------------------------------------------------------------- #

def _ctx(**kw):
    base = dict(node_count=9, phase="exploit", failure_rate=0.0, improves_since_best=0,
                is_numeric_space=True, current_policy="greedy", node_budget_frac=0.9,
                available_policies=["greedy", "mcts"], available_developers=[], defaults={})
    base.update(kw)
    return StrategyContext(**base)


def test_endgame_reserve_switches_to_ensemble():
    rs = RuleStrategist(n_seeds=3)
    out = rs.decide(RunState(), _ctx(node_budget_frac=0.85))
    assert out and out["operators"]["merge_mode"] == "ensemble"
    assert out["operators"]["ablate_every"] == 0
    assert "endgame" in out["rationale"]


def test_no_endgame_reserve_early():
    rs = RuleStrategist(n_seeds=3)
    out = rs.decide(RunState(), _ctx(node_budget_frac=0.4, is_numeric_space=False))
    assert out is None or out.get("operators", {}).get("merge_mode") != "ensemble"


def test_settings_phase5_defaults():
    from looplab.core.config import Settings
    s = Settings()
    assert s.best_of_n_listwise is True
    assert s.llm_cache is False
