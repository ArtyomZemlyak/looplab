"""Late-binding call shims: resolve `<module>.<name>` at CALL time, not at import time.

Five modules hand-wrote the same three-line closure, each with its own paragraph re-explaining the
same hazard (doc 25 CT-07). The hazard is real and worth one careful explanation:

    from looplab.cli import _engine        # ← freezes the object that exists RIGHT NOW

A plain `from X import name` binds the object into the importing module's globals at import time. The
CLI's command modules are imported when `looplab.cli` initializes, so a test that later patches
`looplab.cli._engine` (the documented seam — `test_cli.py::_capture_backend`, and ×5 more for
`cli.make_llm_client`) patches the package attribute the frozen copy no longer reads. The command
then silently drives the REAL engine, or the real LLM client, against a test that believed it was
offline. That failure mode is invisible: the test passes, having tested nothing.

This module is in `core` rather than in `looplab/cli` on purpose. `looplab/bench.py` needs the same
shim, and importing `looplab.cli` at its module scope would pull the whole Typer command surface into
a module that today has no CLI dependency at all. `core` imports nothing above itself, and the target
module here is named by STRING and imported inside the call — so `core` gains no dependency either.
"""
from __future__ import annotations

import importlib
from typing import Any, Callable


def late_bound(module: str, name: str) -> Callable[..., Any]:
    """Return a callable that resolves `module.name` at CALL time and forwards to it.

    The returned shim is the monkeypatch seam: `monkeypatch.setattr(looplab.cli, "_engine", ...)`
    reaches every module that holds one of these, because none of them hold the function object.

    The import is inside the call, not captured in a closure cell, so a shim may be defined at the
    top of a module that the target module itself imports — which is the situation in all five
    original call sites.
    """

    def call(*args, **kwargs):
        return getattr(importlib.import_module(module), name)(*args, **kwargs)

    call.__name__ = name
    call.__qualname__ = name
    call.__doc__ = (f"Late-bound `{module}.{name}`: resolved at CALL time so a monkeypatch on "
                    f"`{module}.{name}` is seen here. See `looplab.core.latebind`.")
    return call
