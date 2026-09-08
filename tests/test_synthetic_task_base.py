"""The five synthetic demo adapters share one skeleton, and the collapse changed no proposal.

Doc 25 RA-06 asked for `SyntheticTaskBase` + a parameterized `PerturbResearcher` to replace five
copy-paste task models and four near-identical seeded Researchers. Collapsing a seeded proposer is
exactly the refactor that can look right and silently propose different nodes: the parameters go on
moving, the metrics keep landing, and the only symptom is that a `--seed 4` run no longer reproduces
the one in the record. So the traces below are GOLDEN — they were captured from the hand-written
classes before the collapse and re-captured after, and the two are byte-identical.

The second half pins the omission that is a fact rather than an oversight: `seed` stays declared per
task. `core/setup_identity.py::setup_config_hash` hashes the task payload in the model's OWN field
order, that digest is `run_started.config_hash`, and `search/speculation_quality.py` re-derives it by
rebuilding a `ToyTask`. Hoisting `seed` into the base moves it ahead of `bounds`/`n`/`gap` and
changes the digest for every synthetic run already on disk.
"""
from __future__ import annotations

import random

import pytest

from looplab.adapters.classification import ClassificationTask, classification_researcher
from looplab.adapters.mlebench import MLEBenchTask, mlebench_researcher
from looplab.adapters.regression import (
    CodeRegressionTask, RegressionTask, regression_researcher)
from looplab.adapters.synthetic import (
    Carried, IntWalk, Jitter, PerturbResearcher, ScaledChoice, SyntheticTaskBase)
from looplab.adapters.timeseries import TimeSeriesTask, timeseries_researcher
from looplab.adapters.toytask import ToyTask
from looplab.core.models import Idea, Node, NodeStatus, RunState
from looplab.core.setup_identity import setup_config_hash

_SYNTHETIC = (ToyTask, RegressionTask, CodeRegressionTask, ClassificationTask, TimeSeriesTask,
              MLEBenchTask)


def _node(node_id: int, params: dict) -> Node:
    return Node(id=node_id, parent_ids=[], operator="draft",
                idea=Idea(operator="draft", params=dict(params)), metric=0.5,
                status=NodeStatus.evaluated)


# --------------------------------------------------------------------------- #
# The collapse proposes exactly what the five hand-written Researchers proposed.

GOLDEN = {
    "regression": (lambda: regression_researcher(max_degree=6, seed=4), [
        ("draft", {"degree": 1.0, "lam": 0.01}, "random hyperparameters"),
        ("improve", {"degree": 0.0, "lam": 0.02}, "perturb node 0 (degree=1)"),
        ("improve", {"degree": 0.0, "lam": 0.02}, "perturb node 1 (degree=0)"),
        ("improve", {"degree": 0.0, "lam": 0.01}, "perturb node 2 (degree=0)"),
    ]),
    "classification": (lambda: classification_researcher(max_degree=4, seed=2), [
        ("draft", {"degree": 1.0, "lr": 0.01, "l2": 0.0, "iters": 100.0},
         "random learner config"),
        ("improve", {"degree": 1.0, "lr": 0.02, "l2": 0.0, "iters": 100.0},
         "perturb node 0 (degree=1)"),
        ("improve", {"degree": 2.0, "lr": 0.02, "l2": 0.0, "iters": 100.0},
         "perturb node 1 (degree=1)"),
        ("improve", {"degree": 2.0, "lr": 0.04, "l2": 0.0, "iters": 100.0},
         "perturb node 2 (degree=2)"),
    ]),
    "timeseries": (lambda: timeseries_researcher(max_period=12, seed=1), [
        ("draft", {"alpha": 0.134, "period": 2.0}, "random forecaster config"),
        ("improve", {"alpha": 0.128, "period": 2.0}, "perturb node 0 (alpha=0.134)"),
        ("improve", {"alpha": 0.303, "period": 2.0}, "perturb node 1 (alpha=0.128)"),
        ("improve", {"alpha": 0.15, "period": 1.0}, "perturb node 2 (alpha=0.303)"),
    ]),
    "mlebench": (lambda: mlebench_researcher(max_k=15, seed=2), [
        ("draft", {"k": 14.0}, "random k"),
        ("improve", {"k": 12.0}, "perturb node 0 (k=14)"),
        ("improve", {"k": 10.0}, "perturb node 1 (k=12)"),
        ("improve", {"k": 8.0}, "perturb node 2 (k=10)"),
    ]),
}


@pytest.mark.parametrize("label", sorted(GOLDEN))
def test_the_collapsed_researcher_reproduces_its_predecessors_stream(label):
    """Captured from the pre-collapse classes; a changed draw order or coercion is a red test.

    The params dict is compared with its KEY ORDER, because that order is also the order the knobs
    draw from `rng` — comparing only values would pass a researcher that had swapped two knobs and
    would therefore propose a different node on every subsequent seed.
    """
    make, expected = GOLDEN[label]
    researcher = make()
    state, parent = RunState(), None
    for index, (operator, params, rationale) in enumerate(expected):
        idea = researcher.propose(state, parent)
        assert idea.operator == operator
        assert idea.params == params and list(idea.params) == list(params)
        assert idea.rationale == rationale
        parent = _node(index, idea.params)


@pytest.mark.parametrize("label", sorted(GOLDEN))
def test_a_parent_that_declares_none_of_the_knobs_falls_back_to_each_knobs_default(label):
    """`improve` off a seeded/injected node whose params are empty must still emit every knob.

    Each knob owns its own default (degree 1, alpha 0.5, k 3, iters 100.0) exactly as the
    hand-written `params.get(name, default)` calls did; a knob that dropped its default would emit
    a KeyError or a missing parameter the templated Developer then silently substitutes.
    """
    researcher = GOLDEN[label][0]()
    idea = researcher.propose(RunState(), _node(9, {}))
    expected_names = [knob.name for knob in researcher.knobs]
    assert list(idea.params) == expected_names
    assert all(isinstance(v, (int, float)) for v in idea.params.values())


def test_the_rationale_cites_the_first_knob_and_the_parent_value_it_walked_from():
    """The first knob is the structural lever, and it cites the PARENT reading, not the new one.

    `IntWalk` cites the coerced `int` it walked from while `Jitter` cites the parent's raw float —
    the divergence the hand-written classes had (`degree={pd}` vs `alpha={pa}`) and the reason a
    knob returns its own citation instead of the researcher formatting the value it produced.
    """
    walker = PerturbResearcher((IntWalk("degree", 0, 6, default=1.0),), seed=0)
    assert walker.propose(RunState(), _node(3, {"degree": 4.0})).rationale.endswith("(degree=4)")

    jitter = PerturbResearcher((Jitter("alpha", sigma=0.15),), seed=0)
    assert jitter.propose(RunState(), _node(3, {"alpha": 0.125})).rationale.endswith("(alpha=0.125)")


# --------------------------------------------------------------------------- #
# The knob vocabulary, one truth table each.

class _ScriptedRandom(random.Random):
    """A `random.Random` whose answers are scripted, so a knob's arithmetic can be stated exactly."""

    def __init__(self, *, randints=(), choices=(), randoms=(), gausses=()):
        super().__init__(0)
        self.randints, self.choices_, self.randoms, self.gausses = (
            list(randints), list(choices), list(randoms), list(gausses))
        self.draws: list[str] = []

    def randint(self, a, b):
        self.draws.append("randint")
        return self.randints.pop(0)

    def choice(self, seq):
        self.draws.append("choice")
        return self.choices_.pop(0)

    def random(self):
        self.draws.append("random")
        return self.randoms.pop(0)

    def gauss(self, mu, sigma):
        self.draws.append("gauss")
        return self.gausses.pop(0)


def test_int_walk_draws_uniformly_then_walks_whole_units_clamped():
    knob = IntWalk("degree", 1, 4, default=1.0)
    assert knob.draft(_ScriptedRandom(randints=[3])) == 3.0
    assert knob.improve(_ScriptedRandom(choices=[1]), {"degree": 2.0}) == (3.0, 2)
    assert knob.improve(_ScriptedRandom(choices=[1]), {"degree": 4.0}) == (4.0, 4)   # clamped high
    assert knob.improve(_ScriptedRandom(choices=[-1]), {"degree": 1.0}) == (1.0, 1)  # clamped low
    # The parent's value is COERCED before the walk: a float degree is a whole complexity axis.
    assert knob.improve(_ScriptedRandom(choices=[0]), {"degree": 2.6}) == (3.0, 3)


def test_scaled_choice_multiplies_and_clamps_inside_its_own_bounds():
    knob = ScaledChoice("lr", (0.01, 0.3), lo=0.001, hi=1.0, ndigits=4, draft_ndigits=3,
                        default=0.1)
    assert knob.draft(_ScriptedRandom(choices=[0.30001])) == 0.3      # draft rounding
    assert knob.improve(_ScriptedRandom(choices=[2.0]), {"lr": 0.05}) == (0.1, 0.05)
    assert knob.improve(_ScriptedRandom(choices=[2.0]), {"lr": 0.9}) == (1.0, 0.9)   # clamped high
    assert knob.improve(_ScriptedRandom(choices=[0.5]), {"lr": 0.001}) == (0.001, 0.001)
    # An unbounded-above scale (the ridge lambda) keeps only the floor.
    lam = ScaledChoice("lam", (0.0, 1.0), lo=0.0, ndigits=6, default=0.0)
    assert lam.improve(_ScriptedRandom(choices=[2.0]), {"lam": 40.0}) == (80.0, 40.0)


def test_carried_draws_once_and_then_never_moves():
    knob = Carried("iters", (50.0, 100.0), default=100.0)
    assert knob.draft(_ScriptedRandom(choices=[50.0])) == 50.0
    scripted = _ScriptedRandom()
    assert knob.improve(scripted, {"iters": 200.0}) == (200.0, 200.0)
    assert scripted.draws == [], "a carried knob must not consume randomness on improve"
    assert knob.improve(_ScriptedRandom(), {}) == (100.0, 100.0)


def test_jitter_drafts_across_its_range_and_steps_by_a_gaussian():
    knob = Jitter("alpha", sigma=0.15, lo=0.0, hi=1.0, ndigits=3, default=0.5)
    assert knob.draft(_ScriptedRandom(randoms=[0.1234])) == 0.123
    assert knob.improve(_ScriptedRandom(gausses=[0.1]), {"alpha": 0.2}) == (0.3, 0.2)
    assert knob.improve(_ScriptedRandom(gausses=[5.0]), {"alpha": 0.2}) == (1.0, 0.2)
    assert knob.improve(_ScriptedRandom(gausses=[-5.0]), {"alpha": 0.2}) == (0.0, 0.2)


def test_the_knobs_draw_in_declaration_order_on_both_branches():
    """The rule the golden traces depend on, stated on its own so it can be read.

    `PerturbResearcher` promises that `knobs` order IS the draw order; that is what let four seeded
    Researchers collapse into one without any of them changing what it proposes.
    """
    knobs = (Jitter("a", sigma=0.1), IntWalk("b", 1, 4), Carried("c", (1.0,)))
    researcher = PerturbResearcher(knobs, seed=0)

    drafting = _ScriptedRandom(randoms=[0.5], randints=[2], choices=[1.0])
    researcher.rng = drafting
    assert list(researcher.propose(RunState(), None).params) == ["a", "b", "c"]
    assert drafting.draws == ["random", "randint", "choice"]

    improving = _ScriptedRandom(gausses=[0.0], choices=[0])
    researcher.rng = improving
    assert list(researcher.propose(RunState(), _node(0, {"a": 0.5, "b": 2.0, "c": 1.0})).params) \
        == ["a", "b", "c"]
    assert improving.draws == ["gauss", "choice"]


# --------------------------------------------------------------------------- #
# The base model: what it hoists, and the one field it deliberately does not.

@pytest.mark.parametrize("model", _SYNTHETIC, ids=lambda m: m.__name__)
def test_every_synthetic_task_model_is_built_on_the_shared_base(model):
    assert issubclass(model, SyntheticTaskBase)
    with pytest.raises(Exception):
        model(direction="mxa")            # the validator is INHERITED, not re-copied per model


def test_the_base_does_not_declare_seed():
    """A task model's field ORDER is a wire contract, and hoisting `seed` would move it.

    `setup_config_hash` dumps the task payload WITHOUT sorted keys, so `run_started.config_hash` is
    a digest of the model's own order. `seed` sits after `bounds`/`n`/`gap` in some models and
    before them in `MLEBenchTask`; a base declaration would pull it to position six in all of them
    and silently re-digest every synthetic run on disk. The five fields the base DOES hoist are
    already the first five, in this order, in every one of them.
    """
    assert "seed" not in SyntheticTaskBase.model_fields
    assert list(SyntheticTaskBase.model_fields) == [
        "kind", "id", "goal", "direction", "comparison_contract"]
    for model in _SYNTHETIC:
        assert list(model.model_fields)[:5] == list(SyntheticTaskBase.model_fields)
        assert "seed" in model.model_fields


# Captured from the hand-written models BEFORE the base landed. A changed digest here means a
# resumed run refuses its own `task.snapshot.json` and every calibration receipt over a Toy run
# stops validating (`search/speculation_quality.py::_validate_calibration_setup`).
#
# RE-PINNED 2026-09-08, `TimeSeriesTask` only: `9d30cbf5e7c3` -> `90fca841d551`. Not drift and not a
# field-order accident — the payload's fields and their order are unchanged, and feeding this model
# dump the OLD goal string still reproduces `9d30cbf5e7c3` exactly. What moved is the task's `goal`,
# because the TASK changed (docs/BACKLOG.md §14): the candidate now WRITES the forecaster against a
# shipped metric instead of tuning two floats of a template the adapter embedded, so the goal went
# from "choose a forecaster's smoothing weight + seasonal period to minimize backtest MASE" to
# "forecast a seasonal+trend series: write a forecaster that minimizes the rolling-origin backtest
# MASE". A run started under `9d30cbf5e7c3` was solving a DIFFERENT problem — its search space was
# two numbers, not a program — so its metrics are NOT comparable with a run started under
# `90fca841d551`, and a pre-change run resumed against today's tree will (correctly) refuse its own
# `task.snapshot.json` rather than silently continue on the new task.
_CONFIG_HASHES = {
    "ToyTask": "83797d036689",
    "RegressionTask": "c636f0fe9aee",
    "CodeRegressionTask": "cd1fc83de08d",
    "ClassificationTask": "f1c63e88302f",
    "TimeSeriesTask": "90fca841d551",
    "MLEBenchTask": "bb1dc1ae1661",
}


@pytest.mark.parametrize("model", _SYNTHETIC, ids=lambda m: m.__name__)
def test_the_shipped_default_task_still_hashes_to_the_identity_runs_were_started_under(model):
    assert setup_config_hash(model().model_dump(mode="json")) == _CONFIG_HASHES[model.__name__]


def test_the_two_regression_tasks_share_one_dataset_definition():
    """RA-06's second half: they were two copies of six fields, `_data()` and `columns()`.

    Same base, same data, and the difference that remains is the one that was ever real — who
    writes the solution, which shows up as `external_fallback_uses_llm`.
    """
    assert RegressionTask()._data() == CodeRegressionTask()._data()
    assert RegressionTask().columns() == CodeRegressionTask().columns()
    assert RegressionTask.__mro__[1] is CodeRegressionTask.__mro__[1]
    assert RegressionTask().external_fallback_uses_llm() is False
    assert CodeRegressionTask().external_fallback_uses_llm() is True


def test_the_base_default_for_the_external_fallback_is_the_templated_one():
    """Three of the five said `return False  # the fallback fills a deterministic local template`.

    That default lives on the base now; the tasks whose fallback is a script-writing `LLMDeveloper`
    (and `ToyTask`, whose reason is its own closed-form Developer) still say so themselves, because
    the ANSWER is shared and the reasons are not.

    `TimeSeriesTask` moved to the `True` side on 2026-09-08 and the BASE default deliberately did
    not move with it. The answer is a property of what a task's `llm_roles` hands back, not a house
    style: that task's `llm_roles` now returns an `LLMDeveloper` writing a forecaster against the
    shipped metric (docs/BACKLOG.md §14 — the same shape `CodeRegressionTask` has always had), so
    validation really can reach a LoopLab-managed LLM Developer for it and
    `agents/reachability.py::external_developer_fallback_uses_llm` has to be told, or startup
    reports a false-green fallback target. Every task whose fallback still fills a deterministic
    local template keeps the base's `False`, which is why the base is the one thing unchanged here.
    """
    assert SyntheticTaskBase.external_fallback_uses_llm(object()) is False
    assert [t().external_fallback_uses_llm() for t in _SYNTHETIC] == [
        False, False, True, False, True, True]
