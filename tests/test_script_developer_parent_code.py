"""The script Developer is shown the code it is improving on — and an ensemble, the code it merges.

THE DEFECT (review 2026-09-23, Q-2; rendered through the real `LLMDeveloper`). The engine routes every
parent-based build — an `improve`, an ensemble `merge`, an ablation's `refine_block` — through
`implement_from(idea, parent)` "so an IMPROVE/REFINE starts from the parent's actual solution … and
patches it, instead of regenerating everything from the pristine baseline"
(`engine/node_build.py::NodeBuildMixin._implement`). The repo Developer has that path; the plain
script Developer every LLM script task hands its build to (`dataset`, `mlebench`, `mlebench_real`,
`code_regression`, `timeseries`) never had it, so the engine's probe fell through to
`implement(idea)` — doc 13's recorded "residual gap". Metered on `examples/dataset_task.json`:

  build            request (chars)   parent code in it   co-parent code in it
  draft                 5,699                -                    -
  improve               5,725               no                    -
  ensemble merge        5,895               no                   no

The improve prompt differs from the draft by the rationale alone; the ensemble prompt names its two
parents in 120 characters of rationale each and shows neither script — and `merge_mode=auto`
resolves to `ensemble` for exactly this Developer (it declares `is_code_generating`), so on these
tasks every merge was a from-scratch rewrite of two programs the model had never seen.

THE FIX is `Settings.developer_parent_code` — a PROMPT change, so ONE flag, OFF at the constructor
and as the class default, a `LEGACY_CONFIG_SNAPSHOT_DEFAULTS` row False, one reader
(`agents/roles.py::parent_code_enabled`), threaded by `agents/factory.py::make_roles`. OFF,
`implement_from` IS `implement` — byte for byte (pinned below by sha256 on the unmodified tree).
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from looplab.agents.roles import LLMDeveloper
from looplab.core.models import Idea

ROOT = Path(__file__).resolve().parents[1]
_BRIEF = "You are an ML/DS agent. Goal: predict target. Print one JSON metric line."
_ANSWER = "```python\nprint('{\"metric\": 1.0}')\n```"


class _Recorder:
    """A client that records every request and answers one fenced script."""

    def __init__(self):
        self.calls: list = []

    def complete_text(self, messages):
        self.calls.append([dict(m) for m in messages])
        return _ANSWER


@pytest.fixture(autouse=True)
def _no_hardware(monkeypatch):
    # The live hardware line is the only machine-dependent text in the Developer's system prompt.
    import looplab.core.hardware as hardware
    monkeypatch.setattr(hardware, "operational_attention_points", lambda **_: "<ATTENTION>")


def _dev(**kw):
    client = _Recorder()
    return LLMDeveloper(client, brief=_BRIEF, **kw), client


def _idea(**kw):
    base = dict(operator="improve", params={"depth": 6.0},
                rationale="Add target encoding of the categorical columns.")
    base.update(kw)
    return Idea(**base)


_PARENT_CODE = ("import pandas as pd\nfrom sklearn.ensemble import GradientBoostingRegressor\n"
                "df = pd.read_csv('/data/train.csv')\n# PARENT-ONLY LINE\nprint('{\"metric\": 0.81}')\n")
_CO_CODE = "import numpy as np\n# CO-PARENT-ONLY LINE\nprint('{\"metric\": 0.79}')\n"


def _parent(code=_PARENT_CODE, id=2, metric=0.81):
    return SimpleNamespace(id=id, code=code, metric=metric, files={}, deleted=[],
                           idea=Idea(operator="draft", params={}, rationale="gbm"))


def _sha(messages) -> str:
    return hashlib.sha256(json.dumps(messages, ensure_ascii=False).encode("utf-8")).hexdigest()


# The historical request `implement(_idea())` sends, sha256 over [system, user] with the hardware
# line normalized — computed on the tree BEFORE this change (master d1ae0e65).
_HISTORICAL_IMPLEMENT = "0684c03fa01db473e839ed1487d09f59886e972bf88355612cf7d8531e66fbfa"
_HISTORICAL_SWEEP = "05433e06d38d2a328e0654b812a1414b4640c688a3dae5e47074106319cc85db"


def test_implement_renders_the_historical_request():
    dev, client = _dev()
    dev.implement(_idea())
    assert _sha(client.calls[0]) == _HISTORICAL_IMPLEMENT, _sha(client.calls[0])
    dev.implement(_idea(space={"depth": [4.0, 8.0]}))
    assert _sha(client.calls[1]) == _HISTORICAL_SWEEP, _sha(client.calls[1])


@pytest.mark.parametrize("space", [None, {"depth": [4.0, 8.0]}])
def test_off_implement_from_is_implement_byte_for_byte(space):
    """OFF (the constructor default, the class default, a pre-field snapshot): the parent-aware
    entry point sends exactly the request `implement` sends — what the engine's probe fell back to
    before the method existed. MUTATION: render the parent block unconditionally -> red."""
    kw = {"space": space} if space else {}
    dev, client = _dev()
    dev.implement(_idea(**kw))
    dev.implement_from(_idea(**kw), _parent(), co_parents=(_parent(_CO_CODE, id=4, metric=0.79),))
    assert client.calls[0] == client.calls[1]
    assert _sha(client.calls[1]) == (_HISTORICAL_SWEEP if space else _HISTORICAL_IMPLEMENT)


def test_the_defect_the_historical_request_carries_no_parent_code():
    dev, client = _dev()
    dev.implement_from(_idea(), _parent())
    text = json.dumps(client.calls[0])
    assert "PARENT-ONLY LINE" not in text and "PARENT SOLUTION" not in text


def test_on_an_improve_is_shown_the_parent_script_and_its_score():
    """The parent's whole script, fenced, with its id and metric, after the idea — and the system
    prompt untouched. MUTATION: drop the code, the metric or the id -> red."""
    dev, client = _dev(parent_code=True)
    off_dev, off_client = _dev()
    dev.implement_from(_idea(), _parent())
    off_dev.implement(_idea())
    (on_sys, on_user), (off_sys, off_user) = client.calls[0], off_client.calls[0]
    assert on_sys == off_sys                               # the persona and brief do not move
    assert on_user["content"].startswith(off_user["content"])   # the idea turn, then the parent
    added = on_user["content"][len(off_user["content"]):]
    assert "=== PARENT SOLUTION (your starting point; parent experiment #2, metric=0.81) ===" in added
    assert "```python\n" + _PARENT_CODE in added
    assert "COMPLETE updated script" in added
    assert "CO-PARENT" not in added


def test_on_an_ensemble_merge_is_shown_every_lineage():
    """MUTATION: ignore `co_parents` -> red."""
    dev, client = _dev(parent_code=True)
    dev.implement_from(_idea(operator="merge"), _parent(),
                       co_parents=(_parent(_CO_CODE, id=4, metric=0.79),))
    user = client.calls[0][1]["content"]
    assert "PARENT-ONLY LINE" in user and "CO-PARENT-ONLY LINE" in user
    assert user.index("=== PARENT SOLUTION") < user.index("=== CO-PARENT SOLUTIONS")
    assert "--- co-parent experiment #4 (metric=0.79) ---" in user


def test_on_a_long_parent_is_bounded_and_the_cut_says_so():
    """`core/context_budget.py::bounded_page` is the house rule: a cut names the range it covered and
    that nothing continues it. MUTATION: a silent head cut -> red."""
    from looplab.agents.roles import SCRIPT_PARENT_CHARS
    long_code = "".join(f"x_{i} = {i}\n" for i in range(SCRIPT_PARENT_CHARS // 6))
    assert len(long_code) > SCRIPT_PARENT_CHARS
    dev, client = _dev(parent_code=True)
    dev.implement_from(_idea(), _parent(long_code))
    user = client.calls[0][1]["content"]
    assert f"of {len(long_code)}; the rest is not addressable from this call]" in user
    assert long_code[:1000] in user and long_code[-200:] not in user


def test_on_a_parent_with_no_script_is_a_plain_implement():
    """A seeded row or a repo-shaped node carries no script: nothing to start from, so the request
    is `implement`'s, exactly as the repo Developer falls back when its parent has no files."""
    dev, client = _dev(parent_code=True)
    dev.implement(_idea())
    dev.implement_from(_idea(), _parent(code=""))
    assert client.calls[0] == client.calls[1]


def test_the_engine_routes_an_improve_and_a_merge_through_it():
    """The capability is what the engine probes: an improve reaches `implement_from` with the
    parent, a merge with its co-parents (`accepts_co_parents`), and the unified facade forwards
    both. MUTATION: drop the `co_parents` keyword from the signature -> the merge case is red."""
    from looplab.agents.unified_agent import UnifiedAgent
    from looplab.engine.node_build import NodeBuildMixin, accepts_co_parents
    from looplab.engine.orchestrator import Engine

    dev, client = _dev(parent_code=True)
    assert accepts_co_parents(dev.implement_from)
    eng = Engine.__new__(Engine)
    eng.developer = UnifiedAgent(researcher=None, developer=dev)
    NodeBuildMixin._implement_result(eng, _idea(), _parent())
    assert "PARENT-ONLY LINE" in client.calls[-1][1]["content"]
    NodeBuildMixin._implement_result(eng, _idea(operator="merge"), _parent(),
                                     co_parents=(_parent(_CO_CODE, id=4),))
    assert "CO-PARENT-ONLY LINE" in client.calls[-1][1]["content"]


# ------------------------------------------------------------------ the switch

def test_the_flag_is_on_for_new_runs_and_off_for_a_pre_field_snapshot():
    """MUTATION: drop the LEGACY row -> red."""
    from looplab.core.config import (LEGACY_CONFIG_SNAPSHOT_DEFAULTS, Settings,
                                     settings_from_snapshot)
    assert Settings().developer_parent_code is True
    assert LEGACY_CONFIG_SNAPSHOT_DEFAULTS["developer_parent_code"] is False
    legacy = Settings().masked_snapshot()
    for field in LEGACY_CONFIG_SNAPSHOT_DEFAULTS:
        legacy.pop(field, None)
    legacy.pop("config_snapshot_schema", None)
    assert settings_from_snapshot(legacy).developer_parent_code is False
    assert settings_from_snapshot(Settings().masked_snapshot()).developer_parent_code is True


def test_one_reader_and_every_default_off_path_reads_off():
    from looplab.agents.roles import parent_code_enabled
    from looplab.core.config import Settings
    assert parent_code_enabled(Settings()) is True
    assert parent_code_enabled(Settings(developer_parent_code=False)) is False
    assert parent_code_enabled(object()) is False                   # a duck-typed stub
    assert _dev()[0].parent_code is False                             # the constructor default
    assert LLMDeveloper.__new__(LLMDeveloper).parent_code is False    # the class default


@pytest.mark.parametrize("case", ["on", "off", "pre_field_snapshot"])
def test_the_run_settings_reach_the_script_developer_through_the_factory(monkeypatch, case):
    """Settings -> `agents/factory.py::make_roles` -> the dataset task's `LLMDeveloper`, split roles
    and the unified facade both. MUTATION: drop the factory's assignment -> the `on` case is red."""
    import looplab.agents.factory as factory
    from looplab.adapters.tasks import load_task
    from looplab.core.config import (LEGACY_CONFIG_SNAPSHOT_DEFAULTS, Settings,
                                     settings_from_snapshot)

    monkeypatch.setattr(factory, "make_llm_client_for", lambda *a, **k: _Recorder())
    if case == "pre_field_snapshot":
        snapshot = Settings(backend="llm").masked_snapshot()
        for field in LEGACY_CONFIG_SNAPSHOT_DEFAULTS:
            snapshot.pop(field, None)
        snapshot.pop("config_snapshot_schema", None)
        base = settings_from_snapshot(snapshot)
    else:
        base = Settings(backend="llm", developer_parent_code=(case == "on"))
    task = load_task(ROOT / "examples" / "dataset_task.json")
    for unified in (False, True):
        settings = base.model_copy(update={"unified_agent": unified, "memora": False,
                                           "web_search": False})
        _researcher, developer = factory.make_roles(task, settings)
        script_dev = getattr(developer, "developer", developer)       # the facade's code stage
        assert isinstance(script_dev, LLMDeveloper), type(script_dev)
        assert script_dev.parent_code is (case == "on"), (unified, case)
