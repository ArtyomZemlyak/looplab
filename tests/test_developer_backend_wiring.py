"""The three developer-backend wirings are STATABLE gates, not clauses inside `make_roles`.

doc 25 RA-01's remaining half. `make_roles` picks the role backends from config, and three of its
branches are not shared setup at all — they are three different Developers, each with its own gate:
the in-house repo code-writer, the external coding agent (ADR-7 presets) and the C2 best-of-N wrap.
They interleaved with the provider/prompt setup, so "when is the in-house editor wired?" could only
be answered by reading a multi-clause `if` in the middle of a 230-line function, and the answer was
coupled to a second fact — `_handoff_dev` — through a variable set inside that branch.

What is driven here is the RULE, not its text (CLAUDE.md's tier 2: hoist a buried rule into a named
function and test its truth table). Each gate is called directly with real `Settings` and the real
example tasks, in every configuration that turns it off and the one that turns it on; then
`make_roles` is driven through counting stubs to prove it consults all three, in the order that
makes an external preset take precedence over the in-house editor, and that the Researcher's
handoff brief still follows the in-house editor and nothing else.

The end-to-end behaviour of each backend is already covered where it was
(`tests/test_cli_agent.py::test_make_roles_selects_cli_agent`,
`tests/test_best_of_n.py::test_make_roles_wraps_best_of_n`,
`tests/test_best_of_n.py::test_make_roles_refuses_best_of_n_it_cannot_honour_on_a_repo_task`), and
those stay the proof that the extraction changed no wiring.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from looplab.adapters.tasks import load_task, make_roles
from looplab.agents import factory
from looplab.agents.developer_backends import (best_of_n_developer, external_cli_developer,
                                               in_house_repo_developer)
from looplab.core.config import Settings

ROOT = Path(__file__).resolve().parents[1]


def _repo_task():
    return load_task(ROOT / "examples" / "repo_task.json")


def _script_task():
    return load_task(ROOT / "examples" / "code_regression_task.json")


def _llm() -> Settings:
    return Settings(backend="llm", unified_agent=False)


# ---------------------------------------------------------------- the in-house repo code-writer

def test_the_in_house_editor_is_wired_for_a_repo_task_with_editables():
    from looplab.adapters.repo_developer import LLMRepoDeveloper

    developer = in_house_repo_developer(_repo_task(), _llm(), client=None,
                                        param_search=False, established=None)
    assert isinstance(developer, LLMRepoDeveloper)


@pytest.mark.parametrize("settings,task,param_search,why", [
    (Settings(backend="llm", unified_agent=False, developer_backend="opencode"), _repo_task,
     False, "an external preset owns the edit surface instead"),
    (_llm(), _repo_task, True, "a cli_overrides param search must not inject authored code"),
    (_llm(), _script_task, False, "a script-solution task has no repo to edit"),
])
def test_the_in_house_editor_is_off_for_each_of_its_three_gates(settings, task, param_search, why):
    """Each `None` is a DIFFERENT run shape, and before the extraction all three were one `if`."""
    assert in_house_repo_developer(task(), settings, client=None, param_search=param_search,
                                   established=None) is None, why


# ------------------------------------------------------------------ the external coding agent

def test_the_external_agent_wraps_the_task_developer_as_its_own_validation_fallback():
    from looplab.agents.cli_agent import CliAgentDeveloper
    from looplab.agents.roles import ValidatingDeveloper

    settings = Settings(backend="llm", unified_agent=False, developer_backend="opencode")
    baseline = object()
    developer = external_cli_developer(_script_task(), settings, baseline, param_search=False)
    assert isinstance(developer, ValidatingDeveloper)
    assert isinstance(developer.inner, CliAgentDeveloper)
    # The Developer this branch REPLACES is the same object it degrades to — one object, two roles.
    assert developer.fallback is baseline


def test_the_external_agent_is_raw_when_validation_is_off():
    from looplab.agents.cli_agent import CliAgentDeveloper

    settings = Settings(backend="llm", unified_agent=False, developer_backend="opencode",
                        validate_agent=False)
    developer = external_cli_developer(_script_task(), settings, object(), param_search=False)
    assert isinstance(developer, CliAgentDeveloper)


@pytest.mark.parametrize("settings,param_search,why", [
    (_llm(), False, "no preset was requested"),
    (Settings(backend="llm", unified_agent=False, developer_backend="opencode"), True,
     "a param-search run keeps the task's baseline developer even with a preset"),
])
def test_the_external_agent_is_off_for_each_of_its_two_gates(settings, param_search, why):
    assert external_cli_developer(_script_task(), settings, object(),
                                  param_search=param_search) is None, why


def test_the_external_agent_resolves_the_developer_stage_target_at_its_own_constructor():
    """An external agent has no role `.client` for `_set_role_client` to rebind, so the per-role
    model has to be resolved HERE or the operator's `developer_model` is accepted and ignored."""
    settings = Settings(backend="llm", unified_agent=False, developer_backend="opencode",
                        validate_agent=False, llm_model="shared",
                        role_profiles={}, developer_model="coder-model")
    developer = external_cli_developer(_script_task(), settings, object(), param_search=False,
                                       developer_role="developer")
    assert developer.model == "ollama/coder-model"


# ------------------------------------------------------------------------- the best-of-N wrap

def test_best_of_n_wraps_only_above_one_and_only_where_the_answer_is_the_code():
    from looplab.core.errors import ConfigRefusal
    from looplab.search.best_of_n import BestOfNDeveloper
    from looplab.agents.roles import LLMDeveloper

    task, base = _script_task(), LLMDeveloper(client=None)
    wrapped = best_of_n_developer(task, Settings(backend="llm", unified_agent=False, best_of_n=3),
                                  base, param_search=False)
    assert isinstance(wrapped, BestOfNDeveloper) and wrapped.n == 3
    # …and the objective rides along, so the foresight ranker optimises the run's own direction.
    assert wrapped.direction == task.direction and wrapped.goal == task.goal
    assert best_of_n_developer(task, Settings(backend="llm", unified_agent=False, best_of_n=1),
                               base, param_search=False) is None
    # The gate does not swallow the refusal: an unrankable Developer still refuses at launch.
    from looplab.adapters.repo_developer import LLMRepoDeveloper
    repo_dev = in_house_repo_developer(_repo_task(), _llm(), client=None, param_search=False,
                                       established=None)
    assert isinstance(repo_dev, LLMRepoDeveloper)
    with pytest.raises(ConfigRefusal):
        best_of_n_developer(_repo_task(), Settings(backend="llm", unified_agent=False, best_of_n=3),
                            repo_dev, param_search=False)


@pytest.mark.parametrize("settings,param_search,why", [
    (Settings(backend="llm", unified_agent=False, best_of_n=3, developer_backend="opencode"), False,
     "N full builds through an external agent is the cost rule this gate exists for"),
    (Settings(backend="llm", unified_agent=False, best_of_n=3), True,
     "the no-edit param mode has no candidate code to rank"),
])
def test_best_of_n_is_off_for_each_of_its_two_other_gates(settings, param_search, why):
    assert best_of_n_developer(_script_task(), settings, object(),
                               param_search=param_search) is None, why


# --------------------------------------------------------- and `make_roles` consults all three

def test_make_roles_applies_the_three_gates_in_order(monkeypatch):
    """DRIVEN, not source-scanned: a gate that stopped being consulted would leave its sentinel
    unused, and an order flip would let the in-house editor win over an external preset — which is
    the one precedence this function has always had."""
    seen: list[str] = []
    repo_dev, agent_dev, ranked = object(), object(), object()

    def _repo(task, settings, client, *, param_search, established):
        seen.append("repo")
        return repo_dev

    def _cli(task, settings, developer, *, param_search, developer_role="developer"):
        seen.append("cli")
        assert developer is repo_dev            # the external agent replaces what came before it
        return agent_dev

    def _bon(task, settings, developer, *, param_search):
        seen.append("best_of_n")
        assert developer is agent_dev           # …and the ranker wraps whatever survived
        return ranked

    monkeypatch.setattr(factory, "in_house_repo_developer", _repo)
    monkeypatch.setattr(factory, "external_cli_developer", _cli)
    monkeypatch.setattr(factory, "best_of_n_developer", _bon)
    _researcher, developer = make_roles(_script_task(), Settings(backend="llm", unified_agent=False,
                                                                researcher_tools=False))
    assert seen == ["repo", "cli", "best_of_n"]
    assert developer is ranked


def test_the_handoff_brief_follows_the_in_house_editor_and_nothing_else():
    """`_handoff_dev` is the second half of the in-house editor's gate: only that Developer runs
    stages->plan->implement inside the node's handoff scope and reads the Researcher's brief, so a
    gate that returned the developer instead of None could not keep the two facts together."""
    _r_repo, _d_repo = make_roles(_repo_task(), _llm())
    assert getattr(_r_repo, "handoff", None) is True
    preset = Settings(backend="llm", unified_agent=False, developer_backend="opencode")
    _r_agent, _d_agent = make_roles(_repo_task(), preset)
    assert getattr(_r_agent, "handoff", None) is False


def test_an_external_preset_takes_precedence_over_the_in_house_editor():
    """The real composition, not a stub: on a repo task WITH editables both gates are open, and the
    preset must win — the in-house editor's own output becomes the validation fallback."""
    from looplab.adapters.repo_developer import LLMRepoDeveloper
    from looplab.agents.cli_agent import CliAgentDeveloper
    from looplab.agents.roles import ValidatingDeveloper

    preset = Settings(backend="llm", unified_agent=False, developer_backend="opencode")
    _researcher, developer = make_roles(_repo_task(), preset)
    assert isinstance(developer, ValidatingDeveloper)
    assert isinstance(developer.inner, CliAgentDeveloper)
    assert not isinstance(developer.fallback, LLMRepoDeveloper)
