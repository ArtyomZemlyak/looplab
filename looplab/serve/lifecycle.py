"""The UI server's startup and shutdown as ONE ordered lifespan (review 2026-09-22, SRV1-06).

FastAPI deprecated `@app.on_event`, and its replacement is not a rename: an app built with
`FastAPI(lifespan=...)` SILENTLY STOPS RUNNING every `on_event` hook still registered on it.
Starlette reads `router.on_startup`/`on_shutdown` only from its DEFAULT lifespan, which a supplied
one replaces (verified on FastAPI 0.141.1 / Starlette 1.6.0: no error, and no warning beyond the
deprecation one both spellings carry). The server had four such hooks from three installers — the
resume-intent startup scan and the resume-timer cancellation
(`engine_proc.py::install_resume_reconcile_hooks`), the JupyterHub reaper
(`engine_proc.py::install_reap_hooks`) and the command-worker restart recovery (`server.py::make_app`)
— so they could only move together, in one change, and the ORDER they ran in, which `make_app` used
to imply by the order it happened to call three installers, has to be owned by something. It is
owned here: two ordered `(name, step)` lists and the one `lifespan` that runs them.

Startup steps run in registration order and a failure PROPAGATES: a server that could not recover
its runs' durable intents must not start serving them, which is what a raising `on_event` startup
hook did too. Shutdown steps run in registration order and a failing one does NOT stop the rest —
the one deliberate difference from `on_event`, which stopped at the first raising shutdown hook.
The resume cancellation is registered BEFORE the reaper precisely so the reaper's snapshot sees
every child cancellation let through, and a cancellation that raised must not also cost a
JupyterHub pod its reaper. Nothing is swallowed: each failure is logged with its step's name and
the shutdown still RAISES after the last step (an `ExitStack` unwinds that way — the last failure
propagates with every earlier one chained as its `__context__`). The shutdown steps also run when
STARTUP fails part-way, so timers an earlier startup step armed are cancelled rather than left to
the process exit.

Steps are synchronous and are called on the event-loop thread, exactly as Starlette called the
synchronous `on_event` handlers they replace.
"""
from __future__ import annotations

import logging
from contextlib import ExitStack, asynccontextmanager
from typing import Callable

_log = logging.getLogger("looplab.server")

Step = Callable[[], object]


def _named_shutdown_step(name: str, step: Step) -> None:
    try:
        step()
    except BaseException:
        _log.exception("server shutdown step %r failed; the remaining steps still run", name)
        raise


class ServerLifecycle:
    """The ordered startup/shutdown steps of one app, and the `lifespan` FastAPI runs them through."""

    def __init__(self) -> None:
        self.startup: list[tuple[str, Step]] = []
        self.shutdown: list[tuple[str, Step]] = []

    def on_startup(self, name: str, step: Step) -> Step:
        """Append a startup step (runs after every step registered before it)."""
        self.startup.append((str(name), step))
        return step

    def on_shutdown(self, name: str, step: Step) -> Step:
        """Append a shutdown step (runs after every step registered before it)."""
        self.shutdown.append((str(name), step))
        return step

    def run_startup(self) -> None:
        for _name, step in list(self.startup):
            step()

    def run_shutdown(self) -> None:
        # An ExitStack runs its callbacks LIFO and KEEPS running them when one raises, re-raising at
        # the end — exactly "every step runs, nothing is swallowed". So push in REVERSE to run in
        # registration order.
        with ExitStack() as unwind:
            for name, step in reversed(self.shutdown):
                unwind.callback(_named_shutdown_step, name, step)

    @asynccontextmanager
    async def lifespan(self, _app):
        try:
            self.run_startup()
            yield
        finally:
            self.run_shutdown()
