"""ONE researcher-wrapper stack for every pair a run builds (review 2026-09-22, SCJ-02).

The stack was derived three times with three rules, and every case below was DRIVEN through the only
production constructor, `cli/__init__.py::_engine`, before the fix:

* the POOLED pair — the one the Layer-5 producer proposes on — was the bare `make_roles` pair: with
  `unified_agent=False, surrogate_proposer=True` on a dataset task the primary was
  `SurrogateResearcher -> ToolUsingResearcher` and the pooled researcher a bare `ToolUsingResearcher`;
* the mid-run BOHB switch (`_ensure_surrogate`) left the primary UNWRAPPED on a dataset task (no
  declared bounds) and on every `--backend toy` run (the raw `unified_agent=True` default), where the
  launch rule, given `policy=bohb`, wraps it.

`tests/factories.py::make_engine` bypasses `_engine`, which is how the engine suite never saw any of
this — so these tests build through `_engine`, with the endpoint preflight stubbed and a credential
in the environment, and never reach a network.
"""
from __future__ import annotations

import pytest

from looplab.core.config import Settings
from looplab.core.models import Idea, Node, NodeStatus, RunState
from looplab.search.foresight import ForesightPanelResearcher
from looplab.search.panel import PanelResearcher
from looplab.search.researcher_stack import (PAID_LAYERS, pooled_researcher, researcher_chain,
                                             with_surrogate)
from looplab.search.surrogate import SurrogateResearcher

_ENDPOINT = "http://127.0.0.1:9/v1"


class _Recorder:
    """A structured-output client: records every call's messages and emits one valid Idea."""

    def __init__(self):
        self.calls: list[list[dict]] = []

    def complete_tool(self, messages, json_schema=None, **_kw):
        self.calls.append([dict(m) for m in messages])
        return {"operator": "draft", "params": {}, "rationale": "r", "concept_mode": "full"}

    def complete_text(self, messages, **_kw):
        self.calls.append([dict(m) for m in messages])
        return ""


def _offline_llm(monkeypatch) -> None:
    """Let `_engine` build an LLM-backed run with no endpoint: the reachability probe is stubbed,
    the credential pair is the environment's (the only source `Settings` honours), and every client
    the CLI or `make_roles` builds is a recorder."""
    import looplab.agents.factory as factory
    import looplab.agents.preflight as preflight
    import looplab.cli as cli

    monkeypatch.setattr(preflight, "preflight_role_endpoints", lambda *_a, **_k: None)
    monkeypatch.setenv("LOOPLAB_LLM_API_KEY", "k")
    monkeypatch.setenv("LOOPLAB_LLM_API_KEY_BASE_URL", _ENDPOINT)
    monkeypatch.setattr(factory, "make_llm_client", lambda _settings, **_kw: _Recorder())
    monkeypatch.setattr(cli, "make_llm_client", lambda _settings, **_kw: _Recorder())


def _task(kind: str, tmp_path):
    if kind == "toy":
        from looplab.adapters.toytask import ToyTask
        return ToyTask()
    from looplab.adapters.dataset_task import DatasetTask
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    (data / "train.csv").write_text("x,y\n1,2\n2,3\n3,5\n", encoding="utf-8")
    return DatasetTask(id="ds", goal="g", direction="min", data_path=str(data))


def _build(tmp_path, name: str, task, **settings_kw):
    import looplab.cli as cli
    backend = settings_kw.pop("backend", "llm")
    extra = {"llm_model": "m", "llm_base_url": _ENDPOINT} if backend == "llm" else {}
    return cli._engine(tmp_path / name, task, Settings(backend=backend, **extra, **settings_kw),
                       None)


def _types(researcher) -> list[type]:
    return [type(link) for link in researcher_chain(researcher)]


def _free_part(researcher) -> list[type]:
    """The primary's chain with the PAID layers taken out — what a pooled pair must carry."""
    return [t for t in _types(researcher) if not issubclass(t, PAID_LAYERS)]


def _has_surrogate(researcher) -> bool:
    return any(isinstance(link, SurrogateResearcher) for link in researcher_chain(researcher))


def _evaluated_state(n: int = 5) -> RunState:
    """Enough evaluated numeric history for the surrogate to pass its warm-up (4) on a task that
    declares NO bounds: it learns the ranges from these points."""
    state = RunState(goal="g", direction="min")
    for i in range(n):
        params = {"lr": 0.05 * (i + 1), "depth": float(2 + i)}
        state.nodes[i] = Node(id=i, operator="draft", status=NodeStatus.evaluated, feasible=True,
                              metric=float((i - 2) ** 2), idea=Idea(operator="draft", params=params))
    return state


# --------------------------------------------------------------------------- the pooled pair
_CONFIGS = {
    # The shipped default: unified. The launch stack's ONLY layer is the PAID foresight panel (the
    # free layers are unified-skipped), so the pooled pair is the bare facade — the exclusion.
    "default-unified": {},
    "foresight": {"unified_agent": False},
    "knn-panel": {"unified_agent": False, "researcher_panel": 3},
    # The finding's case: the operator's surrogate held on the primary and not on the producer.
    "surrogate": {"unified_agent": False, "surrogate_proposer": True},
    "surrogate+knn-panel": {"unified_agent": False, "surrogate_proposer": True,
                            "researcher_panel": 3},
    "bohb": {"unified_agent": False, "policy": "bohb"},
}


@pytest.mark.parametrize("kind", ["dataset", "toy"])
@pytest.mark.parametrize("config", list(_CONFIGS))
def test_the_pooled_pair_is_the_primary_stack_minus_its_paid_layers(
        tmp_path, monkeypatch, config, kind):
    """The pair the Layer-5 producer leases (`_producer_role_pair`) carries exactly the primary's
    chain with the PAID layers (`PAID_LAYERS`: the foresight and k-NN panels) removed — the documented
    exclusion in `speculation.py::_producer_role_pair` — and is still its own objects.

    MUTATIONS, each red here: drop the `pooled_researcher(...)` call in
    `node_build.py::_build_role_pairs` (the surrogate configs: the pooled chain is bare again), or
    hand pooled pairs the whole launch stack (every paid config: a panel appears on the pool)."""
    _offline_llm(monkeypatch)
    engine = _build(tmp_path, f"{config}-{kind}", _task(kind, tmp_path), **_CONFIGS[config])
    pooled_researcher_, pooled_developer = engine._producer_role_pair()

    assert _types(pooled_researcher_) == _free_part(engine.researcher), (
        _types(pooled_researcher_), _types(engine.researcher))
    assert not any(isinstance(link, PAID_LAYERS) for link in researcher_chain(pooled_researcher_))
    # Isolation is the pool's reason to exist: no link of the pooled chain is a primary object.
    primary_ids = {id(link) for link in researcher_chain(engine.researcher)}
    assert not primary_ids & {id(link) for link in researcher_chain(pooled_researcher_)}
    # R1 on the pool too: a unified facade stays ONE object for both handles.
    unified = engine.researcher is engine.developer
    assert (pooled_researcher_ is pooled_developer) is unified


def test_the_finding_case_before_and_after_in_one_place(tmp_path, monkeypatch):
    """The measured shape of the defect, spelled out: non-unified, `surrogate_proposer`, a dataset
    task. Before the fix the pooled researcher was the bare `ToolUsingResearcher`."""
    from looplab.agents.agent import ToolUsingResearcher

    _offline_llm(monkeypatch)
    engine = _build(tmp_path, "finding", _task("dataset", tmp_path),
                    unified_agent=False, surrogate_proposer=True)
    assert _types(engine.researcher) == [SurrogateResearcher, ToolUsingResearcher]
    assert _types(engine._producer_role_pair()[0]) == [SurrogateResearcher, ToolUsingResearcher]


# --------------------------------------------------------------------------- the mid-run switch
_SWITCHES = {
    # A repo/dataset task declares no bounds: the old `if bounds:` gate left bohb as bare ASHA.
    "dataset-no-bounds": ("dataset", {"unified_agent": False}),
    # `--backend toy` carries the default `unified_agent=True` with two separate role objects: the
    # old raw-flag skip left it bare where the launch wraps it.
    "toy-backend": ("toy", {"backend": "toy"}),
    "toy-backend-dataset": ("dataset", {"backend": "toy"}),
    # A genuinely unified run: neither the launch nor the switch wraps (R1) — unchanged.
    "unified-llm": ("dataset", {}),
}


@pytest.mark.parametrize("case", list(_SWITCHES))
def test_a_mid_run_bohb_switch_wraps_exactly_where_the_launch_rule_does(
        tmp_path, monkeypatch, case):
    """A Strategist switch to `bohb` gives the primary the surrogate iff a LAUNCH with `policy=bohb`
    would have, and the pool follows it — including the pair the producer had already leased.

    MUTATIONS, each red here: restore the `if bounds:` gate (dataset-no-bounds), decide R1 on
    `self.unified_agent` again (both toy-backend cases), or stop re-applying the layer to the pairs
    already minted in `_ensure_surrogate` (the pooled assertion)."""
    kind, settings_kw = _SWITCHES[case]
    _offline_llm(monkeypatch)
    task = _task(kind, tmp_path)
    engine = _build(tmp_path, f"{case}-greedy", task, **dict(settings_kw))
    engine._producer_role_pair()          # the pool exists BEFORE the switch lands
    engine._apply_strategy({"policy": "bohb"})
    launched = _build(tmp_path, f"{case}-bohb", task, **dict(settings_kw, policy="bohb"))

    assert engine._policy_name == "bohb"
    assert _has_surrogate(engine.researcher) is _has_surrogate(launched.researcher)
    if case != "unified-llm":
        assert _has_surrogate(engine.researcher), "the launch rule wraps here and the switch did not"
    assert _types(engine._producer_role_pair()[0]) == _free_part(engine.researcher)
    # Idempotent: a params-only re-application on an already-bohb run adds nothing.
    before = engine.researcher
    engine._apply_strategy({"policy": "bohb", "policy_params": {"eta": 2}})
    assert engine.researcher is before


def test_a_pooled_surrogate_draws_from_its_own_stream(tmp_path, monkeypatch):
    """The surrogate is deterministic given (seed, history). A pooled instance on the primary's seed
    re-proposes the primary lane's point over the same history — two lanes, one proposer. Past
    warm-up, on one state, the two must disagree.

    MUTATION: pass `seed=0` in `_build_role_pairs` -> the two proposals are identical."""
    _offline_llm(monkeypatch)
    engine = _build(tmp_path, "streams", _task("dataset", tmp_path),
                    unified_agent=False, surrogate_proposer=True)
    pooled = engine._producer_role_pair()[0]
    state = _evaluated_state()
    primary_idea = engine.researcher.propose(state, None)
    pooled_idea = pooled.propose(state, None)
    assert "surrogate-guided" in primary_idea.rationale and "surrogate-guided" in pooled_idea.rationale
    assert primary_idea.params != pooled_idea.params


# --------------------------------------------------------------------------- the free layer is free
def test_the_surrogate_layer_adds_no_paid_call_and_changes_no_prompt(tmp_path, monkeypatch):
    """The PROOF the free layer may go on a pooled pair without a flag (brief step 3).

    Below warm-up the pooled `Surrogate(LLMResearcher)` makes the SAME single call a bare pooled
    researcher makes, with byte-identical messages — the engine stamps its hints on the outermost
    handle and `forward_hints` mirrors them inward. Past warm-up it makes NO call: a call removed,
    never one added. Both researchers come from the builder the pool itself uses (`make_roles`), and
    the hints from the engine's own per-proposal stamp (`_set_complexity_hint`)."""
    from looplab.adapters.tasks import make_roles
    from looplab.agents.roles import LLMResearcher

    _offline_llm(monkeypatch)
    task = _task("dataset", tmp_path)
    settings_kw = {"unified_agent": False, "surrogate_proposer": True, "researcher_tools": False}
    engine = _build(tmp_path, "proof", task, **settings_kw)
    pooled = engine._producer_role_pair()[0]
    bare, _developer = make_roles(task, Settings(backend="llm", llm_model="m",
                                                 llm_base_url=_ENDPOINT, **settings_kw),
                                  tmp_path / "proof")
    assert _types(pooled) == [SurrogateResearcher, LLMResearcher]
    assert type(bare) is LLMResearcher

    cold = RunState(goal="g", direction="min")
    for handle in (pooled, bare):
        engine._set_complexity_hint(cold, None, researcher=handle)
    pooled.propose(cold, None)
    bare.propose(cold, None)
    assert len(pooled.fallback.client.calls) == len(bare.client.calls) == 1
    assert pooled.fallback.client.calls == bare.client.calls

    warm = _evaluated_state()
    for handle in (pooled, bare):
        engine._set_complexity_hint(warm, None, researcher=handle)
    idea = pooled.propose(warm, None)
    bare.propose(warm, None)
    assert "surrogate-guided" in idea.rationale
    assert len(pooled.fallback.client.calls) == 1, "the surrogate layer made a paid call"
    assert len(bare.client.calls) == 2


# --------------------------------------------------------------------------- the AUTO width probe
_HIDDEN_CLIENT_CONFIGS = {
    # The finding's case: an LLM Researcher behind the surrogate, a templated toy Developer.
    "surrogate": {"surrogate_proposer": True},
    # The k-NN panel forwards `client` from its base — which is the surrogate, which holds none.
    "surrogate+knn-panel": {"surrogate_proposer": True, "researcher_panel": 3},
}


@pytest.mark.parametrize("config", list(_HIDDEN_CLIENT_CONFIGS))
def test_the_surrogate_does_not_hide_the_llm_researcher_from_the_auto_widths(
        tmp_path, monkeypatch, config):
    """`orchestrator.py::_build_calls_an_llm` decides whether AUTO widths fan out, and it read the
    roles' `client` — which `SurrogateResearcher` hides on purpose (review 2026-09-22, W5-5
    follow-up). MEASURED through this constructor before the fix, toy task, non-unified,
    `max_parallel=4`: without the surrogate `_build_calls_an_llm()` True, `llm_parallel` 4,
    `speculation_depth` 4; with `surrogate_proposer` (and with `researcher_panel=3` on top) it
    answered False and AUTO settled `llm_parallel` 1, `speculation_depth` 0 — a proposal lane with a
    provider call per node run as if it had no latency to overlap.

    MUTATION: drop the `base`/`fallback`/`inner` descent from `_build_calls_an_llm` -> both configs
    are back at (1, 0)."""
    _offline_llm(monkeypatch)
    task = _task("toy", tmp_path)
    bare = _build(tmp_path, f"{config}-bare", task, unified_agent=False, max_parallel=4)
    wrapped = _build(tmp_path, f"{config}-wrapped", task, unified_agent=False, max_parallel=4,
                     **_HIDDEN_CLIENT_CONFIGS[config])
    assert _has_surrogate(wrapped.researcher) and not _has_surrogate(bare.researcher)
    # The precondition, stated: neither handle surfaces a client — the surrogate hides it and the
    # toy Developer is a template — so only the wrapper descent can find the LLM.
    assert getattr(wrapped.researcher, "client", None) is None
    assert getattr(wrapped.developer, "client", None) is None
    for engine in (bare, wrapped):
        assert engine._build_calls_an_llm() is True
        assert (engine._llm_parallel, engine.speculation_depth) == (4, 4)


def test_a_surrogate_over_a_templated_researcher_still_settles_serial(tmp_path, monkeypatch):
    """The descent must not widen an OFFLINE run: `--backend toy` with the surrogate wraps a
    `ToyResearcher` — no client anywhere in the chain — so AUTO keeps the serial, byte-reproducible
    offline spine (`tests/test_settled_width_pins.py`)."""
    engine = _build(tmp_path, "toy-backend", _task("toy", tmp_path), backend="toy",
                    surrogate_proposer=True, max_parallel=4)
    assert _has_surrogate(engine.researcher)
    assert engine._build_calls_an_llm() is False
    assert (engine._llm_parallel, engine.speculation_depth) == (1, 0)


# --------------------------------------------------------------------------- the layer's own rule
class _Plain:
    def __init__(self, bounds=None):
        self.bounds = bounds

    def propose(self, _state, _parent):
        return Idea(operator="draft", params={})


def test_with_surrogate_truth_table():
    """The surrogate layer's ONE rule, statable: R1 on the objects, idempotent over the WHOLE chain,
    constructed whether or not any link declares bounds, and declared bounds found at any depth."""
    unified = _Plain()
    assert with_surrogate(unified, unified, explore=0.1) is unified          # R1: one agent
    base, developer = _Plain(), object()
    wrapped = with_surrogate(base, developer, explore=0.1)
    assert isinstance(wrapped, SurrogateResearcher) and wrapped.fallback is base
    assert wrapped.bounds == {}                                             # learns them instead
    panel = PanelResearcher(wrapped, k=3)
    assert with_surrogate(panel, developer, explore=0.1) is panel           # already in the chain
    deep = ForesightPanelResearcher(_Plain(bounds={"x": (0.0, 1.0)}), k=2, client=None)
    assert with_surrogate(deep, developer, explore=0.1).bounds == {"x": (0.0, 1.0)}


def test_pooled_researcher_follows_the_primarys_free_layers_only():
    developer = object()
    bare_primary, surrogate_primary = _Plain(), with_surrogate(_Plain(), developer, explore=0.1)
    fresh = _Plain()
    assert pooled_researcher(bare_primary, fresh, developer, explore=0.1, seed=1) is fresh
    followed = pooled_researcher(PanelResearcher(surrogate_primary, k=3), fresh, developer,
                                 explore=0.1, seed=1)
    assert _types(followed) == [SurrogateResearcher, _Plain]
    # A primary whose only layer is PAID gives the pool nothing.
    paid_only = ForesightPanelResearcher(_Plain(), k=2, client=object())
    assert pooled_researcher(paid_only, fresh, developer, explore=0.1, seed=1) is fresh
