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

# The two `agents` modules `agents/factory.py` may name at MODULE level — see the layering test at
# the bottom of this file, which re-derives the condition each one is admitted on.
MODULE_LEVEL_AGENT_IMPORTS = ("looplab.agents.providers", "looplab.agents.developer_backends")


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

    `agents/factory.py`'s cap went 520 -> 530 on 2026-08-27, and the reason it moved AT ALL is the
    distinction the cap exists to make. It crossed 520 by five lines: one provider added to
    `_shared_providers` plus the paragraph saying why (`QuestionBoardTools`). Wiring a provider into
    the composition root is that module doing its job, not regaining a second domain — which is what
    happened to `adapters/tasks.py` and what the tests above actually check. Punishing the root for
    composing would incentivise deleting the rationale comment to fit, and comments are load-bearing
    here.

    THE SIZE OF THE RAISE IS THE WHOLE DISCIPLINE, and the first cut of this got it wrong: it went
    to 560, buying 35 lines of headroom for a 5-line overrun while the sibling cap next to it runs
    at ONE (399 of 400). A cap raised seven times further than the change needed is not a cap that
    moved, it is a cap that stopped being consulted — which is exactly what the last paragraph of
    this docstring warns about, one paragraph above where it happened. The rule: a raise pays for
    the lines actually spent and nothing more, so the NEXT overrun is a decision somebody has to
    make rather than slack somebody already banked.

    If it is spent again, the answer is an EXTRACTION and the candidate is already visible:
    `make_roles` is 222 lines, nearly half the file, and `_shared_providers` (75) is a coherent
    unit — "the providers every agentic role shares" — that would move cleanly behind its existing
    re-export. Raise this number a third time and the guard means nothing.

    Both halves of that rule have now been exercised, which is why the ledger below runs in two
    directions. `agents/developer_backends.py` was RAISED for eight lines a merge spent wiring the
    backend it is named for. `adapters/tasks.py` was not raised at all: its cap was spent, so the
    extraction happened (`normalize_task` -> `adapters/task_schema.py`) and the cap FOLLOWED the
    file down. A cap is only a decision if it can move either way for a stated reason.
    """
    for rel, cap in (("adapters/tasks.py", 233), ("agents/factory.py", 385),
                     ("agents/developer_backends.py", 186),
                     ("adapters/task_schema.py", 231)):
    #
    # 2026-08-29, MERGE with master: master's 530 is KEPT and not raised. The merged file is 529
    # lines -- master's additions plus this branch's two composition lines, `stage_guidance=` and
    # `step_feedback_command=` -- so the cap has ONE line of headroom, which is the discipline the
    # paragraph above demands rather than a coincidence to be widened away. This branch's own cap
    # (522) was computed before master's rows existed and is superseded, not overruled.
    #
    # 530 -> 532, 2026-08-31 MERGE with master, and this is the case the paragraph above is FOR.
    # Neither side moved this literal, so nothing conflicted and nothing was chosen: the file simply
    # became 531 lines because master's `9168bff`-era `role=role` wiring on `MemoryTools` carries a
    # two-line why-comment that this branch's copy did not have. The raise pays for exactly the two
    # lines spent, leaving the same ONE line of headroom the entry above left, so the next overrun is
    # still a decision somebody has to make. Measured on the merged file, not inferred from 529 + 2.
    #
    # 532 -> 541, 2026-09-06, docs/57 `run-accountant-splits-on-settings-copy`: the composition
    # root's two `model_copy` fork sites now attach the run's ONE `CostAccountant` to the PARENT
    # before forking (`run_cost_accountant`, one call plus a three-line why each) and the import
    # line wrapped to two -- nine lines, a money rule wired where the fork is, not a second domain.
    # The raise pays for exactly those nine (531 -> 540 measured), keeping the ONE line of headroom.
    # The extraction candidate named above (`make_roles`, `_shared_providers`) is still the answer
    # if this is spent again.
    #
    # 541 -> 547, 2026-09-06, docs/60 A5: the run's ONE `EstablishedContext` is built here and
    # handed to the two roles that drive a tool loop (`agents/established.py`) — a function-local
    # import plus three lines of why plus the call, and one keyword on each of the two existing
    # constructions. Measured 546, so the raise keeps the one line of headroom the entry above
    # left. This is composition, which is what this file is for; the extraction candidate
    # (`make_roles`, `_shared_providers`) is still the answer the next time it is spent.
    #
    # 547 -> 385, 2026-09-08, doc 25 RA-01's remaining half: the extraction the paragraph above
    # names was SPENT, on the other candidate. `make_roles`'s three developer-backend wirings moved
    # to `agents/developer_backends.py` behind named gates, and the cap FOLLOWS the file down to
    # measured + 1 (384 measured). A cap left at 547 over a 384-line file is 163 lines of slack
    # nobody decided to bank — the same failure as raising it seven times further than the change
    # needed, one direction over. The new module gets the same discipline: 177 measured, cap 178.
    #
    # 178 -> 186, 2026-09-08 MERGE, and this is again the case the docstring is FOR: nothing
    # conflicted, so nothing was chosen. `external_cli_developer` gained the run's own
    # `CostAccountant` on a parallel branch (doc 27 `external-cli-usage-is-unpriced`) — one keyword,
    # a six-line why and the widened `core.llm` import, eight lines measured (177 -> 185) — which is
    # this module wiring the backend it is named for, not a second domain. The raise pays for those
    # eight and leaves the same ONE line of headroom.
    #
    # 400 -> 233, 2026-09-08: `adapters/tasks.py` SPENT its 399-of-400 and the answer this guard
    # names — an EXTRACTION, not a raise — was taken. Two parallel changes spent it (the
    # `shift_inputs` hook row, and `submit_warnings` single-sourcing the CLI's hand-copied submit
    # warnings), and both are the task module doing its job. What came out is the other half of the
    # file's own docstring: `normalize_task`, the composable/legacy SCHEMA front-end, 203 lines that
    # take a dict and return a dict, now `adapters/task_schema.py` and re-exported. tasks.py is 232
    # measured, so the cap follows the file DOWN to measured + 1 rather than banking 167 lines of
    # slack nobody decided on — the same rule the entry above applies upward. The new module gets it
    # too: 230 measured, cap 231.
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
    # `agents/providers.py` (the shared providers, extracted 2026-09-06) and
    # `agents/developer_backends.py` (the three developer-backend wirings, 2026-09-08) are the ONLY
    # agents modules the factory may import at module level, because the re-export identity
    # `adapters.tasks` relies on needs a module-level binding — and each is admissible only while
    # it keeps the same property itself, which the second assertion holds SEPARATELY for each: its
    # own module-level imports reach nothing in `search`, `tools` or `agents`, so the cycle stays
    # exactly as open as before. The condition is RE-DERIVED per module rather than trusted, so
    # adding a name to this tuple without the property is a red test, not a widened hole.
    offenders = sorted(m for m in module_level
                       if m.startswith(("looplab.search", "looplab.tools", "looplab.agents."))
                       and m not in MODULE_LEVEL_AGENT_IMPORTS)
    assert offenders == [], f"module-level imports that must stay function-local: {offenders}"
    for dotted in MODULE_LEVEL_AGENT_IMPORTS:
        rel = dotted.replace("looplab.", "").replace(".", "/") + ".py"
        allowed_tree = ast.parse((_PKG / rel).read_text(encoding="utf-8"))
        allowed_level = {node.module for node in allowed_tree.body
                         if isinstance(node, ast.ImportFrom) and node.module}
        assert not [m for m in allowed_level
                    if m.startswith(("looplab.search", "looplab.tools", "looplab.agents."))], (
            f"{rel} must keep every search/tools/agents import function-local, or the "
            "factory's module-level import of it closes the cycle this guard exists to keep open")


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
