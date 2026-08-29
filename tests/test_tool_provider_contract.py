"""Every tool provider satisfies the STRUCTURAL contract — checked by scan, not by inheritance.

THE DEFECT THIS EXISTS FOR, measured on a live run. `tools/question_board.py::QuestionBoardTools`
shipped with `def call(self, name, args)` where `tools/_base.py::ToolProvider` requires `execute`.
Because the protocol is structural — its own docstring says "no provider inherits this" — nothing
checked it at import or at construction, and the first run that loaded the provider lost its ENTIRE
deep-research stage on the first dispatch:

    summary: "(deep research unavailable: 'QuestionBoardTools' object has no attribute 'execute')"
    findings 0 · claims 0 · directions 0 · questions 0

Ten unit tests, the neighbourhood suite and a clean import all passed, because every one of those
tests called `.call(...)` — the name the object defined. A test that drives the method an object
happens to have can never discover that the contract wanted a different one.

IT WAS ALSO LATENT FOR A WHOLE SESSION behind a run that pinned older code: the engine watched all
day launched before the provider existed, so its deep research worked. "A running engine pins its own
code" is usually a reason a FIX is absent; here it hid a REGRESSION.

So this is a two-way scan, in the spirit of the repo's other registry guards: a class that offers
`specs` is a provider, and a provider owes `execute`. Adding one that offers neither is fine; adding
a half-provider is what goes red.
"""
from __future__ import annotations

import importlib
import inspect
import pkgutil

import looplab.tools as tools_pkg

# `_base` defines the Protocol itself; a Protocol legitimately declares `specs` without a body.
_SKIP_MODULES = {"looplab.tools._base"}


def _provider_classes():
    """Every class in `looplab/tools/` that offers `specs` — the duck-typed provider shape."""
    found = []
    for info in pkgutil.iter_modules(tools_pkg.__path__, tools_pkg.__name__ + "."):
        if info.name in _SKIP_MODULES:
            continue
        try:
            module = importlib.import_module(info.name)
        except Exception:            # an optional dependency is not this guard's business
            continue
        for name, obj in vars(module).items():
            if not inspect.isclass(obj) or obj.__module__ != info.name:
                continue
            if callable(getattr(obj, "specs", None)):
                found.append((info.name, name, obj))
    return found


def test_every_provider_that_offers_specs_also_offers_execute():
    """The exact break, as a class-wide property.

    MUTATION: rename `QuestionBoardTools.execute` back to `call` and this goes red — which is the
    whole point, because the ten tests written FOR that provider stayed green through it.
    """
    providers = _provider_classes()
    assert providers, "the scan found no providers at all — it has stopped checking anything"
    missing = [f"{mod}.{cls}" for mod, cls, obj in providers
               if not callable(getattr(obj, "execute", None))]
    assert not missing, (
        "provider(s) offering `specs` but not `execute`: " + ", ".join(missing)
        + " — `ToolProvider` is STRUCTURAL, so this is checked by nothing at import and surfaces as "
          "a dead agent phase at dispatch")


def test_the_scan_actually_reaches_the_provider_that_broke():
    """A guard whose population is empty passes forever. Pin the specific class into it."""
    names = {f"{mod}.{cls}" for mod, cls, _ in _provider_classes()}
    assert "looplab.tools.question_board.QuestionBoardTools" in names, (
        "MUTATION: narrow `_provider_classes` (e.g. require a base class) and the very provider this "
        "guard was written for drops out of the population")


def test_the_question_board_provider_DISPATCHES_not_merely_defines():
    """Tier 1: drive it. `hasattr` would have been satisfied by the wrong name being right.

    This calls through the contract's name with no state bound, which is the one dispatch that needs
    no fixture — a provider must answer rather than raise when it has nothing to report.
    """
    from looplab.tools.question_board import QuestionBoardTools

    out = QuestionBoardTools().execute("read_questions", {})
    assert isinstance(out, str) and out, "a dispatch must return a bounded string answer"
    unknown = QuestionBoardTools().execute("no_such_tool", {})
    assert "unknown tool" in unknown, "and an unknown name is answered, not raised"
