"""The agent/role composition root is its own module (doc 25 RA-01).

`adapters/tasks.py` said "TaskAdapter seam (ADR-2) + a loader for tasks" while more than half its
lines were LLM/agent wiring with no task-adapter content. Two modules in one file, and the docstring
described only one of them.

What is pinned here is the SPLIT plus the two things that make it safe: the re-exports resolve to the
SAME objects (so every `from looplab.adapters.tasks import make_roles` and every patch of that name
still works), and the layering direction is preserved — `agents/factory.py` reaches `search` and
`tools` only through function-local imports, because `search` imports `agents` at module scope.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from looplab.adapters import tasks
from looplab.agents import factory

_PKG = Path(__file__).resolve().parents[1] / "looplab"

# Everything the composition root owns. A name added to `factory` that callers reach through
# `adapters.tasks` has to be re-exported, or the back-compat surface silently loses it.
MOVED = ("make_roles", "build_unified_agent", "build_strategist_tools", "make_developer_factory",
         "_shared_providers", "_make_abstractor", "_memora_cache_path", "_set_role_client",
         "_agent_model")

# What the task half still owns.
KEPT = ("TaskAdapter", "kinds", "normalize_task", "validate_task", "load_task")


@pytest.mark.parametrize("name", MOVED)
def test_every_moved_name_is_the_same_object_through_both_paths(name):
    """Not merely importable — IDENTICAL. Two module objects would make every existing
    `monkeypatch.setattr("looplab.adapters.tasks.make_roles", ...)` a silent no-op."""
    assert getattr(tasks, name) is getattr(factory, name)


@pytest.mark.parametrize("name", KEPT)
def test_the_task_half_kept_its_own_surface(name):
    assert hasattr(tasks, name)
    assert not hasattr(factory, name) or name == "TaskAdapter", (
        f"{name} is task machinery and must not have moved into the agent factory")


def test_the_llm_client_re_export_stayed_with_the_task_module():
    """`from looplab.adapters.tasks import make_llm_client` is spelled at dozens of call sites,
    including `cli/__init__.py`. It rode along with the agent half on the first cut of this split
    and had to be put back — pinned so it cannot drift out again."""
    from looplab.core import llm

    for name in ("LlmTarget", "client_kwargs_for", "make_llm_client", "make_llm_client_for",
                 "resolve_llm_target"):
        assert getattr(tasks, name) is getattr(llm, name), f"tasks.{name} is no longer re-exported"


def test_the_task_module_no_longer_contains_the_agent_wiring():
    source = inspect.getsource(tasks)
    for marker in ("def make_roles", "def build_unified_agent", "def _shared_providers"):
        assert marker not in source, f"{marker} is still defined in adapters/tasks.py"
    assert "from looplab.agents.factory import" in source


def test_the_docstrings_now_describe_what_each_module_holds():
    """The finding's headline was the header, not the line count: a module docstring that names half
    its contents sends every reader to the wrong file."""
    assert "agents/factory.py" in (tasks.__doc__ or "")
    assert "composition root" in (factory.__doc__ or "")


def test_neither_module_is_a_god_module_again():
    """A BACKSTOP for the real property above, which is the domain split — this file's own header
    says the finding's headline "was the header, not the line count".

    `agents/factory.py`'s cap went 520 -> 560 on 2026-08-27, and the reason is the distinction the
    cap exists to make. It crossed 520 by SEVEN lines: one provider added to `_shared_providers`
    plus the paragraph saying why (`QuestionBoardTools`). Wiring a provider into the composition
    root is that module doing its job, not regaining a second domain — which is what happened to
    `adapters/tasks.py` and what the tests above actually check. Punishing the root for composing
    would incentivise deleting the rationale comment to fit, and comments are load-bearing here.

    The headroom is deliberately small. If it is spent again, the answer is an EXTRACTION and the
    candidate is already visible: `make_roles` is 222 lines, nearly half the file, and
    `_shared_providers` (75) is a coherent unit — "the providers every agentic role shares" — that
    would move cleanly behind its existing re-export. Raise this number a third time and the guard
    means nothing.
    """
    for rel, cap in (("adapters/tasks.py", 400), ("agents/factory.py", 560)):
        lines = len((_PKG / rel).read_text(encoding="utf-8").splitlines())
        assert lines < cap, f"{rel} is back to {lines} lines"


# ------------------------------------------------------------------ the layering that keeps working

def test_the_factory_reaches_search_and_tools_only_through_function_local_imports():
    """`search` imports `agents` at module scope (five modules do). A module-level `looplab.search`
    import here closes the cycle into an ImportError at startup — the asymmetry CLAUDE.md documents
    and `tests/test_agents_search_direction.py` guards. The split had to preserve it."""
    tree = ast.parse((_PKG / "agents/factory.py").read_text(encoding="utf-8"))
    module_level = {
        node.module for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module
    }
    # `if TYPE_CHECKING:` blocks are module-level statements but never execute, so include them too
    for node in tree.body:
        if isinstance(node, ast.If):
            module_level |= {inner.module for inner in ast.walk(node)
                             if isinstance(inner, ast.ImportFrom) and inner.module
                             and "TYPE_CHECKING" not in ast.unparse(node.test)}
    offenders = sorted(m for m in module_level
                       if m.startswith(("looplab.search", "looplab.tools", "looplab.agents.")))
    assert offenders == [], f"module-level imports that must stay function-local: {offenders}"


def test_the_task_adapter_annotation_does_not_create_an_import_cycle():
    """`adapters.tasks` re-exports FROM `agents.factory`, so a runtime import of `TaskAdapter` the
    other way would be a cycle. It is TYPE_CHECKING-only, which works because the module has
    `from __future__ import annotations`."""
    source = (_PKG / "agents/factory.py").read_text(encoding="utf-8")
    assert "from __future__ import annotations" in source
    assert "if TYPE_CHECKING:" in source

    # Checked on the AST, not the text: the module DOCSTRING legitimately quotes
    # `from looplab.adapters.tasks import make_roles` when explaining the re-export, and a substring
    # check flags that prose as if it were a runtime import.
    tree = ast.parse(source)
    runtime = [node for node in tree.body
               if isinstance(node, ast.ImportFrom) and node.module == "looplab.adapters.tasks"]
    assert runtime == [], "the TaskAdapter import must stay TYPE_CHECKING-only"
    guarded = [node for node in tree.body
               if isinstance(node, ast.If) and "TYPE_CHECKING" in ast.unparse(node.test)
               and any(isinstance(inner, ast.ImportFrom)
                       and inner.module == "looplab.adapters.tasks" for inner in node.body)]
    assert guarded, "TaskAdapter is no longer imported for typing at all"


def test_importing_the_factory_first_still_works():
    """The cycle would show up as an ImportError only in one order, so exercise that order: a fresh
    interpreter importing `agents.factory` before `adapters.tasks`."""
    import subprocess
    import sys

    probe = ("import looplab.agents.factory as f; import looplab.adapters.tasks as t;"
             "assert t.make_roles is f.make_roles; print('ok')")
    done = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True,
                          cwd=str(_PKG.parent))
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "ok"


def test_the_dead_ambient_target_store_is_gone():
    """A `shared = resolve_llm_target(settings)` that nothing read — every comparison below it is
    stage-vs-ROLE. Pre-existing; the split is what surfaced it."""
    body = inspect.getsource(factory.build_unified_agent)
    # The STATEMENT, not the mention: the note below explains the removal by quoting the line, so a
    # substring check would flag the explanation as the defect it documents.
    statements = [line.strip() for line in body.splitlines()
                  if not line.strip().startswith("#")]
    assert "shared = resolve_llm_target(settings)" not in statements
    assert "dead store" in body, "keep the note, so it is not re-added as if it were needed"
