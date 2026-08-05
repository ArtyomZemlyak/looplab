"""LLM_PRESENCE_ATTRS / FACADE_STAGE_ATTRS registry enforcement (CLAUDE.md: duck-typed seams are
REGISTRY-GUARDED).

`engine/orchestrator.py::_build_calls_an_llm` decides one half of the AUTO width settle — whether a
build has provider latency worth fanning out over, which sets `llm_parallel` and, through it,
`speculation_depth`. It answers that question with `getattr(obj, name, default)` probes against the
roles and, under `unified_agent=True`, against the facade's public per-stage handles.

The OTHER half of the very same decision — `gpu_capable`, which settles the eval axis — has been
registry-guarded in `adapters/tasks.py::TASK_OPTIONAL_HOOKS` since it shipped. This half had no
registry at all, and it is the half with the worse failure mode: `gpu_capable` absent means CAPABLE
(the historical behaviour), while a missing LLM marker means "no LLM", which is a CHANGED execution
treatment. Measured on the real `UnifiedAgent` shape with an LLM Researcher and a templated
Developer, hiding the public `researcher` handle flips `_build_calls_an_llm` True -> False,
`llm_parallel` 4 -> 1 and `speculation_depth` 4 -> 0 — the exact regression b89b0209 fixed — with
nothing red anywhere.

Two-way, like every other registry test here:

* every attribute the predicate probes must be REGISTERED (catches a typo'd probe, which reads
  `None`/`False` forever and only ever costs fan-out, so nothing ever fails);
* every registered attribute must still be a live PUBLIC handle on the shipped roles/facade
  (catches the producer-side rename the probe cannot see).
"""
from __future__ import annotations

import ast
import inspect
import textwrap
from pathlib import Path

import pytest

from looplab.agents.roles import FACADE_STAGE_ATTRS, LLM_PRESENCE_ATTRS
from looplab.agents.unified_agent import UnifiedAgent
from looplab.engine.orchestrator import Engine

_PKG = Path(__file__).resolve().parents[1] / "looplab"
_REGISTERED = frozenset(LLM_PRESENCE_ATTRS) | frozenset(FACADE_STAGE_ATTRS)


def _probed_attribute_names() -> set[str]:
    """Every string literal `_build_calls_an_llm` passes to `getattr`, read from its own AST.

    Parsing the predicate itself rather than grepping the module keeps the scan honest when the
    orchestrator grows another `getattr(role, ...)` elsewhere: only THIS decision is registered here.
    """
    # `dedent`, not `cleandoc`: cleandoc strips the leading whitespace of EVERY line independently
    # and un-indents the body out from under its own `def`.
    tree = ast.parse(textwrap.dedent(inspect.getsource(Engine._build_calls_an_llm)))
    names: set[str] = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "getattr" and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
                and isinstance(node.args[1].value, str)):
            names.add(node.args[1].value)
    return names


def test_every_build_llm_probe_names_a_registered_attribute():
    probed = _probed_attribute_names()
    # Guard the guard: an emptied scan would pass every assertion below vacuously.
    assert len(probed) >= len(_REGISTERED), (
        f"_build_calls_an_llm probes only {sorted(probed)} — the AST scan stopped seeing the "
        "predicate's getattr calls, so this whole file is now vacuous.")
    unknown = probed - _REGISTERED
    assert not unknown, (
        f"_build_calls_an_llm probes unregistered attribute(s) {sorted(unknown)} — either a typo'd "
        "probe (it reads None/False forever and only ever narrows the width, so nothing fails) or a "
        "new marker missing from agents/roles.py::LLM_PRESENCE_ATTRS / FACADE_STAGE_ATTRS.")


def test_every_registered_attribute_is_still_probed():
    orphaned = _REGISTERED - _probed_attribute_names()
    assert not orphaned, (
        f"registered attribute(s) {sorted(orphaned)} are no longer probed by _build_calls_an_llm — "
        "registry rot after a refactor; drop them or restore the probe.")


@pytest.mark.parametrize("attr", FACADE_STAGE_ATTRS)
def test_the_unified_agent_facade_still_exposes_every_stage_handle(attr):
    """The producer side of the descent — the direction the probe physically cannot check.

    `_build_calls_an_llm` reads these off the facade with a default, so making one private (or
    renaming it to `_researcher`) silently reverts b89b0209: the predicate stops finding the LLM
    Researcher behind a templated Developer and AUTO pins the build width to 1. Assert on a REAL
    `UnifiedAgent`, not a stand-in, because a stand-in would keep passing after the rename.
    """
    agent = UnifiedAgent(researcher=object(), developer=object())
    assert not attr.startswith("_"), f"{attr} must stay PUBLIC for the facade descent to reach it"
    assert hasattr(agent, attr), (
        f"UnifiedAgent no longer exposes `{attr}` — engine/orchestrator.py::_build_calls_an_llm "
        "descends into the facade through it, and the cost roll-up walks it too "
        "(engine/costs.py::_CHILD_ATTRS). Renaming it silently pins the build width to 1 on every "
        "run with a templated Developer and an LLM Researcher.")


@pytest.mark.parametrize("attr", LLM_PRESENCE_ATTRS)
def test_every_llm_presence_marker_has_a_shipped_producer(attr):
    """`client` and `is_code_generating` must still be spelled that way on the role side.

    Source-scanned rather than instantiated: the producers are spread across the plain roles, the
    external-CLI Developer and three forwarding wrappers, and what matters is that the NAME survives
    on all of them, not that any one object is constructible in a test.
    """
    producers = [_PKG / "agents" / "roles.py", _PKG / "agents" / "cli_agent.py",
                 _PKG / "agents" / "unified_agent.py", _PKG / "search" / "best_of_n.py",
                 _PKG / "search" / "foresight.py"]
    hits = [p.name for p in producers
            if p.exists() and attr in p.read_text(encoding="utf-8", errors="replace")]
    assert hits, (
        f"no shipped role/wrapper spells `{attr}` any more — _build_calls_an_llm's probe for it now "
        "reads its default forever, which narrows the build width with nothing red. Update "
        "agents/roles.py::LLM_PRESENCE_ATTRS in the same change as the rename.")


def test_the_facade_descent_is_what_answers_for_a_templated_developer():
    """Behavioural anchor for the registry: the shape b89b0209 fixed, through a real UnifiedAgent.

    Without this the two source scans could both stay green while the descent stopped WORKING (an
    early return, a reordered guard). With it, the registry and the behaviour fail together.
    """
    from looplab.adapters.toytask import ToyTask
    from looplab.agents.roles import ToyObjectiveDeveloper, ToyResearcher

    task = ToyTask()
    researcher = ToyResearcher(task.bounds)
    researcher.client = object()                     # the LLMResearcher marker
    facade = UnifiedAgent(researcher=researcher, developer=ToyObjectiveDeveloper())
    assert facade.client is None, "the facade's own forwarder still describes the DEVELOPER stage"

    engine = Engine.__new__(Engine)                  # the predicate reads only these two attributes
    engine.researcher = engine.developer = facade
    assert engine._build_calls_an_llm() is True
