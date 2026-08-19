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


# --- C5/C2: best-of-N ranks the RETURN VALUE, and the repo Developer's return value is a sentinel ---
# Measured on `runs/` (docs/BACKLOG.md §0.18): the corpus's 52 real repo builds cost 7.37M prompt
# tokens each, so `best_of_n=3` was billing +14.7M tokens per node for a selection that could not
# run — every candidate scored -1.0, both LLM tie-breaks were skipped on `len({""}) == 1`, and
# candidate 0 always won. These pin the property, not the wiring: the first test DRIVES the broken
# selection through the real `BestOfNDeveloper` so the refusal below is provably not cosmetic.

class _RepoShapedDev:
    """The `LLMRepoDeveloper` contract exactly: `implement` returns the SENTINEL "" and the
    artifact travels on `last_files` (adapters/repo_developer.py — '"" means the files are the
    answer'). Deliberately NOT the real class: this is the SHAPE best-of-N cannot rank, and any
    third-party Developer with the same shape inherits the same refusal."""

    def __init__(self, working_sets):
        self.working_sets = working_sets
        self.calls = 0
        self.last_files: dict = {}
        self.last_deleted: list = []
        self.last_footprint = None

    def implement(self, idea):
        self.last_files = self.working_sets[self.calls % len(self.working_sets)]
        self.calls += 1
        return ""


def test_best_of_n_cannot_rank_a_developer_that_answers_on_last_files():
    """THE reason for the refusal below, driven rather than asserted about."""
    good, broken = {"train.py": _GOOD}, {"train.py": _BROKEN}
    inner = _RepoShapedDev([broken, good])          # candidate 0 is the one with the syntax error
    dev = BestOfNDeveloper(inner, n=2, listwise=False, foresight=False)
    dev.implement(Idea(operator="draft"))
    assert inner.calls == 2                          # N full builds were generated and paid for
    assert dev.last_n_scores == [-1.0, -1.0]         # and the scorer separated nothing
    assert dev.last_files == broken                  # so candidate 0 won — the broken one


def test_answers_with_code_is_a_positive_marker_the_repo_developer_does_not_carry():
    from looplab.agents.roles import LLMDeveloper
    from looplab.adapters.repo_developer import LLMRepoDeveloper
    assert LLMDeveloper.answers_with_code is True
    # absent means NO (the `honors_idea_space` rule): a Developer that never declares it is
    # fail-closed, so a third-party/templated Developer is refused rather than silently billed.
    assert not getattr(LLMRepoDeveloper, "answers_with_code", False)


def test_wraps_developer_forwards_answers_with_code():
    """A wrapper must not make an unrankable Developer look rankable."""
    assert BestOfNDeveloper(_RepoShapedDev([{}]), n=1).answers_with_code is False
    assert BestOfNDeveloper(_VaryingDev([_GOOD]), n=1).answers_with_code is False   # plain stub
    from looplab.agents.roles import LLMDeveloper, ValidatingDeveloper
    assert ValidatingDeveloper(LLMDeveloper(client=None)).answers_with_code is True


def test_make_roles_refuses_best_of_n_it_cannot_honour_on_a_repo_task():
    """A silent drop to N=1 is the failure this repo already measured on `developer_backend`
    aliases; refuse at launch instead, as a typed `OperatorRefusal` so the CLI boundary prints
    one line at exit 2 (and a LIVE Strategist swap records a `refused` receipt)."""
    from pathlib import Path
    import pytest
    from looplab.core.config import Settings
    from looplab.core.errors import ConfigRefusal, OperatorRefusal
    from looplab.adapters.tasks import load_task, make_roles
    task = load_task(Path(__file__).resolve().parents[1] / "examples" / "repo_task.json")
    with pytest.raises(ConfigRefusal) as excinfo:
        make_roles(task, Settings(backend="llm", best_of_n=3, unified_agent=False))
    assert isinstance(excinfo.value, OperatorRefusal)
    assert "best_of_n=3" in str(excinfo.value) and "last_files" in str(excinfo.value)
    # N=1 is untouched: the knob is off, nothing to honour, the run starts.
    _r, dev = make_roles(task, Settings(backend="llm", best_of_n=1, unified_agent=False))
    from looplab.adapters.repo_developer import LLMRepoDeveloper
    assert isinstance(dev, LLMRepoDeveloper)


def test_make_roles_still_wraps_best_of_n_where_the_answer_is_the_code():
    """The refusal is narrow: a script-solution task is byte-for-byte what it was."""
    from pathlib import Path
    from looplab.core.config import Settings
    from looplab.adapters.tasks import load_task, make_roles
    task = load_task(Path(__file__).resolve().parents[1] / "examples" / "code_regression_task.json")
    _r, dev = make_roles(task, Settings(backend="llm", best_of_n=3, unified_agent=False))
    assert isinstance(dev, BestOfNDeveloper) and dev.n == 3 and dev.answers_with_code is True
