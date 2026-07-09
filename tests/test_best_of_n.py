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


def test_best_of_one_is_passthrough():
    inner = _VaryingDev([_GOOD])
    dev = BestOfNDeveloper(inner, n=1)
    assert dev.implement(Idea(operator="draft")) == _GOOD
    assert inner.calls == 1   # exactly one generation when N=1


def test_best_of_n_forwards_repair_and_audit():
    dev = BestOfNDeveloper(_VaryingDev([_BROKEN]), n=3)
    assert dev.repair(Idea(operator="debug"), "x", "err") == _GOOD
    assert dev.audit_extra()["best_of_n"] == 3


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
