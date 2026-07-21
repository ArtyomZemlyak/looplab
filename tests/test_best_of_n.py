"""C2 best-of-N candidate selection (execution-free reward)."""
from __future__ import annotations

from looplab.search.best_of_n import BestOfNDeveloper, _score
from looplab.core.models import Idea

_GOOD = "import json\nprint(json.dumps({'metric': 0.1}))\n"
_BROKEN = "def f(:\n  pass\n"        # syntax error
_EMPTY = "   "


def test_score_ranks_valid_over_broken():
    assert _score(_GOOD) > _score(_BROKEN) >= 0.0
    assert _score(_EMPTY) < 0.0


class _VaryingDev:
    """Returns a rotating list of candidate outputs; records call count."""
    def __init__(self, outs):
        self.outs = outs
        self.calls = 0
        self.last_files = {}

    def implement(self, idea):
        o = self.outs[self.calls % len(self.outs)]
        self.calls += 1
        return o

    def repair(self, idea, code, error):
        return _GOOD


def test_best_of_n_picks_best_candidate():
    dev = BestOfNDeveloper(_VaryingDev([_BROKEN, _GOOD]), n=2)
    out = dev.implement(Idea(operator="draft"))
    assert out == _GOOD and dev.inner.calls == 2
    assert max(dev.last_n_scores) == _score(_GOOD)


def test_best_of_n_restores_the_winning_candidates_footprint():
    class _FootprintDev(_VaryingDev):
        def __init__(self):
            super().__init__([_GOOD, _BROKEN])
            self.footprints = [{"gpus": 1, "gpu_mem_mib": 8_000}, {"gpus": 9}]
            self.last_footprint = None

        def implement(self, idea):
            index = self.calls % len(self.outs)
            out = super().implement(idea)
            self.last_footprint = self.footprints[index]
            return out

    inner = _FootprintDev()
    dev = BestOfNDeveloper(inner, n=2)
    assert dev.implement(Idea(operator="draft")) == _GOOD
    assert inner.last_footprint == {"gpus": 9}  # the final generated candidate lost
    assert dev.last_footprint == {"gpus": 1, "gpu_mem_mib": 8_000}


def test_best_of_one_is_passthrough():
    inner = _VaryingDev([_GOOD])
    dev = BestOfNDeveloper(inner, n=1)
    assert dev.implement(Idea(operator="draft")) == _GOOD
    assert inner.calls == 1   # exactly one generation when N=1


def test_best_of_n_forwards_repair_and_audit():
    dev = BestOfNDeveloper(_VaryingDev([_BROKEN]), n=3)
    assert dev.repair(Idea(operator="debug"), "x", "err") == _GOOD
    assert dev.audit_extra()["best_of_n"] == 3


class _ParentAwareDev:
    """A developer with the parent-aware protocol (like RepoDeveloper)."""
    def __init__(self, outs):
        self.outs = outs
        self.calls = 0
        self.from_calls = 0
        self.last_files = {}
        self.last_deleted = []

    def implement(self, idea):
        o = self.outs[self.calls % len(self.outs)]; self.calls += 1
        return o

    def implement_from(self, idea, parent):
        self.from_calls += 1
        return self.implement(idea)

    def repair(self, idea, code, error):
        return _GOOD

    def repair_from(self, idea, node, error):
        self.from_calls += 1
        return _GOOD


def test_best_of_n_forwards_parent_aware_hooks():
    """arch-review §4 P1-9: BestOfN must expose implement_from/repair_from so the engine's capability
    check routes the parent-aware path THROUGH the wrapper (not fall back to baseline regeneration)."""
    inner = _ParentAwareDev([_BROKEN, _GOOD])
    dev = BestOfNDeveloper(inner, n=2)
    assert hasattr(dev, "implement_from") and hasattr(dev, "repair_from")
    out = dev.implement_from(Idea(operator="improve"), parent=object())
    assert out == _GOOD and inner.from_calls == 2       # best-of-N ran through implement_from twice

    class _N:
        code = "x"
        idea = Idea(operator="debug")
    assert dev.repair_from(Idea(operator="debug"), _N(), "err") == _GOOD
    assert inner.from_calls == 3                          # repair_from is single-shot


def test_best_of_n_implement_from_falls_back_when_inner_lacks_it():
    # a plain inner (no implement_from) -> BestOfN.implement_from degrades to plain best-of-N implement
    dev = BestOfNDeveloper(_VaryingDev([_BROKEN, _GOOD]), n=2)
    assert dev.implement_from(Idea(operator="improve"), parent=object()) == _GOOD


def test_make_roles_wraps_best_of_n():
    from pathlib import Path
    from looplab.core.config import Settings
    from looplab.adapters.tasks import load_task, make_roles
    root = Path(__file__).resolve().parents[1]
    task = load_task(root / "examples" / "code_regression_task.json")
    _r, dev = make_roles(task, Settings(backend="llm", best_of_n=3, unified_agent=False))
    assert isinstance(dev, BestOfNDeveloper) and dev.n == 3
    # the run objective is threaded so the FOREAGENT ranker optimizes for the RIGHT direction
    assert dev.direction == task.direction and dev.goal == task.goal


_GOOD2 = "import json\n# distinct variant\nprint(json.dumps({'metric': 0.2}))\n"


def test_best_of_n_threads_direction_into_foresight(monkeypatch):
    """The predict-before-execute ranker must be told the run's REAL direction — a max-direction task
    (accuracy/AUC) told the default 'min' would rank the worst-predicted candidate first."""
    seen = {}

    def _fake_rank(client, report, items, *, goal="", direction="min", parser="tool_call", prompts=None):
        seen["direction"] = direction
        seen["goal"] = goal
        return ([0] + list(range(1, len(items))), 0.9, "stub")   # pick candidate 0, valid order

    monkeypatch.setattr("looplab.search.foresight.rank", _fake_rank)
    # two DISTINCT top-scoring candidates so the >1-distinct foresight gate actually fires
    inner = _VaryingDev([_GOOD, _GOOD2])
    inner.client = object()                       # `dev.client` reads through the wrapper to here (non-None)
    dev = BestOfNDeveloper(inner, n=2, foresight=True, direction="max", goal="maximize AUC")
    assert dev.client is not None                 # foresight branch is reachable
    dev.implement(Idea(operator="draft"))
    assert seen == {"direction": "max", "goal": "maximize AUC"}
