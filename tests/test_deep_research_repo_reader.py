"""The deep-research stage must be able to OPEN the task's source, not just query empty stores.

Measured over the 19-task `runs-armb` campaign (2026-08-20), before `repo_reader_provider` existed:
the deep-research phase — the phase that mints a cold-start run's first hypotheses — made 336 tool
calls across 20 repo tasks and NOT ONE opened a workspace file, because `make_deep_researcher` built
its toolset from `_shared_providers` alone and every store there is empty by construction on a cold
start. Its `answered_by_context` block published 33 tools ALL AT ZERO; 57 of the 82 memos it wrote
(70%) state in the model's own words that no tool in the environment can read the task's source, 17
of 20 tasks in their FIRST memo; and `propose` then paid 968 `repo_read` calls to read those same
files. The memo is spliced into every later propose prompt, so an ungrounded one is re-paid on every
turn of the most expensive phase.

These are TIER-1 guards (CLAUDE.md's guard ladder): they build the real provider over a real
directory and READ A REAL FILE THROUGH THE COMPOSED TOOLSET, so "the call happened" and "the bytes
came back" cannot diverge, and no comment can satisfy them. The parity test is the one that stops
the defect coming back a third time — it derives the expectation from the Researcher's OWN toolset
rather than from a hard-coded name list, so a repo tool added later is covered by construction.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from looplab.agents.deep_research import make_deep_researcher
from looplab.agents.repo_reader import repo_reader_provider


class _Client:
    """Enough of an LLM client for `make_deep_researcher` to accept the stage as reachable."""

    def chat(self, messages, tools, tool_choice="auto"):    # pragma: no cover - never driven here
        raise AssertionError("these tests never call the model")


def _settings(tmp_path):
    """A settings stub with every OPTIONAL provider off, so the only providers that can appear are
    the run-introspection core and the repo reader under test. `researcher_tools` on is what makes
    the core providers present at all; knowledge/memory/skills/literature/web stay unwired."""
    return SimpleNamespace(
        researcher_tools=True, cross_run_tools=False, all_runs_tools=False,
        cross_run_read_tools=False, memory_dir=None, knowledge_dir=None, skills_dir=None,
        literature_search=False, web_search=False, prompt_dir=None, llm_parser="tool_call",
        hide_empty_tools=False, context_budget_chars=None, agent_max_turns=0,
        agent_time_budget_s=0.0, compressor_model=None,
    )


def _repo_task(tmp_path, *, params=None):
    """A minimal repo task: one editable mount holding the file an AlgoTune-shaped goal names."""
    root = tmp_path / "ws"
    root.mkdir()
    (root / "reference_convex_hull.py").write_text(
        "def is_solution(problem, solution):\n    return True  # THE ACCEPTANCE CONTRACT\n")
    (root / "description.txt").write_text("Return the convex hull.\n")
    return SimpleNamespace(
        params=params,
        repo_spec=lambda: {"editables": [{"name": ".", "path": str(root)}]},
    )


def _offered(tools):
    return {(spec.get("function") or {}).get("name") for spec in (tools.specs() or ())}


def test_deep_research_can_read_the_task_source_it_is_asked_to_reason_about(tmp_path):
    """The property the 70% of memos were reporting: the stage can open the reference file."""
    task = _repo_task(tmp_path)
    researcher = make_deep_researcher(
        _settings(tmp_path), client=_Client(), task=task, run_dir=tmp_path / "run")
    assert researcher is not None
    tools = researcher.tools
    assert tools is not None, "the deep-research stage was built with no tool surface at all"

    offered = _offered(tools)
    assert {"repo_read", "repo_list", "repo_grep"} <= offered, (
        f"the deep-research stage cannot open the workspace; it was offered {sorted(offered)}")

    # Drive it: the OFFER is not the property — the bytes are. A tool that is advertised but does
    # not route (a nested composite's hide filter, a provider registered under another name) would
    # pass an offer-only assertion and still leave the stage proposing blind.
    listing = str(tools.execute("repo_list", {}))
    assert "reference_convex_hull.py" in listing
    body = str(tools.execute("repo_read", {"path": "reference_convex_hull.py"}))
    assert "THE ACCEPTANCE CONTRACT" in body


def test_deep_research_offers_the_same_repo_reader_the_researcher_does(tmp_path):
    """Parity, derived from the Researcher's own toolset — never from a hard-coded name list.

    This is the assertion that makes the fix survive: the two sites drifted once precisely because
    each carried its own copy of the rule, so a repo tool added to `RepoTools` later must not need
    anyone to remember this file.
    """
    task = _repo_task(tmp_path)
    reader = repo_reader_provider(task)
    assert reader is not None
    researcher_repo_tools = {(spec.get("function") or {}).get("name")
                             for spec in reader.specs()}
    assert researcher_repo_tools, "RepoTools published no specs — the parity check would be vacuous"

    deep = make_deep_researcher(
        _settings(tmp_path), client=_Client(), task=task, run_dir=tmp_path / "run")
    assert researcher_repo_tools <= _offered(deep.tools)


@pytest.mark.parametrize("task_factory, why", [
    (lambda tmp_path: SimpleNamespace(), "a task with no repo_spec at all"),
    (lambda tmp_path: SimpleNamespace(repo_spec=lambda: {"editables": []}),
     "a repo_spec that mounts nothing"),
])
def test_no_reader_without_an_editable_repo(tmp_path, task_factory, why):
    assert repo_reader_provider(task_factory(tmp_path)) is None, why


def test_param_search_mode_still_gets_no_reader(tmp_path):
    """`make_roles`' rule verbatim: in the cli_overrides param-search mode an idea is an argv
    override, there is no code to read, and offering a reader there is the surface change this
    helper must NOT make while unifying the two call sites."""
    task = _repo_task(tmp_path, params={"lr": [0.1, 0.2]})
    assert repo_reader_provider(task) is None

    deep = make_deep_researcher(
        _settings(tmp_path), client=_Client(), task=task, run_dir=tmp_path / "run")
    assert "repo_read" not in _offered(deep.tools)


def test_a_broken_repo_spec_still_raises_rather_than_degrading_in_silence(tmp_path):
    """Pinned as a DECISION, not as an accident.

    `make_roles` has always called `repo_spec()` bare, so a task whose spec raises has always taken
    the role build down loudly. Swallowing it here would be the strictly worse failure mode for this
    change: the run would come up on a repo task with no reader, which is EXACTLY the silent state
    this fix exists to end, and it would look identical to a non-repo task. Loud stays loud.
    """
    def _boom():
        raise RuntimeError("repo spec unavailable")

    task = SimpleNamespace(params=None, repo_spec=_boom)
    with pytest.raises(RuntimeError):
        repo_reader_provider(task)
