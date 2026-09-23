"""The Developer is told the versions its code will meet, before it writes a line.

Measured 2026-09-23: a MiniOneRec node wrote `DynamicCache.key_cache` -- transformers 4 API -- against
the transformers 5.7.0 its eval ran on. `pkg_info` would have said 5.7.0 had anything prompted the
question. The block states, measured on the TASK's interpreter, the versions of the packages the repo
itself imports.
"""
from __future__ import annotations

import sys
from pathlib import Path

from looplab.core.models import Idea
from looplab.tools import env_inspect
from looplab.tools.env_inspect import environment_fingerprint


def test_the_fingerprint_names_installed_third_party_packages_and_nothing_else(monkeypatch):
    monkeypatch.setattr(env_inspect, "_FINGERPRINT_CACHE", {})
    fp = environment_fingerprint(sys.executable, ["pytest", "json", "os", "no_such_package_xyz"])
    assert fp is not None and fp["python"] == sys.executable
    names = [p.split(" ", 1)[0].lower() for p in fp["packages"]]
    assert "pytest" in names
    assert "json" not in names and "os" not in names, "the stdlib is not a dependency"
    assert not any("no_such_package" in n for n in names)


def test_an_interpreter_that_cannot_answer_gives_no_block(monkeypatch, tmp_path):
    monkeypatch.setattr(env_inspect, "_FINGERPRINT_CACHE", {})
    assert environment_fingerprint(str(tmp_path / "gone" / "python"), ["pytest"]) is None


def _dev():
    from looplab.adapters.repo_task import EvalSpec, LLMRepoDeveloper, RepoTask
    fixture = Path(__file__).resolve().parent / "fixtures" / "repo_fixture"
    task = RepoTask(id="r", goal="g", direction="max", editable_path=str(fixture),
                    edit_surface=["*.py"], protect=[],
                    eval=EvalSpec(command=[sys.executable, "ttrain.py"],
                                  metric={"kind": "stdout_json", "key": "metric"}))
    return LLMRepoDeveloper(object(), task), fixture


def test_the_repo_s_own_modules_are_not_mistaken_for_dependencies():
    dev, fixture = _dev()
    names = dev._repo_import_names()
    local = {p.stem for p in fixture.glob("*.py")}
    assert not (set(names) & local), (names, local)


def test_the_block_reaches_the_system_prompt_of_a_real_build(monkeypatch):
    import looplab.agents.agent as agent_mod
    dev, _fixture = _dev()
    dev._environment_block_text = ("THE ENVIRONMENT YOUR CODE RUNS IN (measured on its own "
                                   "interpreter, not assumed): /env/python -- Python 3.10.18. "
                                   "Installed versions ...: transformers 5.7.0.\n\n")
    systems: list = []

    def fake_loop(client, tools, messages, emit_spec, *, finalize, fallback, **opts):
        systems.append(messages[0]["content"])
        name = emit_spec["function"]["name"]
        if name == "declare_stages":
            return finalize({"stages": []})
        if name == "propose_plan":
            return finalize({"steps": []})
        return finalize({"summary": "s"})

    monkeypatch.setattr(agent_mod, "drive_tool_loop", fake_loop)
    dev.implement(Idea(operator="draft", params={}, rationale="x"))
    assert systems and all("transformers 5.7.0" in s for s in systems)


def test_a_failed_fingerprint_leaves_the_prompt_without_the_block(monkeypatch):
    dev, _fixture = _dev()
    monkeypatch.setattr(env_inspect, "environment_fingerprint", lambda *a, **k: None)
    assert dev._environment_block() == ""
