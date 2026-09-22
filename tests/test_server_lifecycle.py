"""`serve/lifecycle.py::ServerLifecycle` — the UI server's ONE lifespan (review 2026-09-22, SRV1-06).

Why it exists, measured: FastAPI deprecated `@app.on_event`, and an app built with
`FastAPI(lifespan=...)` SILENTLY stops running every `on_event` hook still registered on it
(FastAPI 0.141.1 / Starlette 1.6.0 — `review/SRV1/lifespan_trap.py`). The server's four hooks came
from three installers, so they moved together, and the order they ran in — which `make_app` used to
imply by the order it called the installers — is now data the lifecycle owns. These tests drive a
real lifespan through `TestClient`; `tests/test_server.py::test_resume_shutdown_hook_precedes_jupyter_reaper`
drives the production composition.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from looplab.serve.lifecycle import ServerLifecycle  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def _app(lifecycle: ServerLifecycle) -> FastAPI:
    return FastAPI(lifespan=lifecycle.lifespan)


def test_steps_run_in_registration_order_around_the_served_requests():
    calls = []
    lifecycle = ServerLifecycle()
    lifecycle.on_startup("scan", lambda: calls.append("start:scan"))
    lifecycle.on_startup("recover", lambda: calls.append("start:recover"))
    lifecycle.on_shutdown("cancel", lambda: calls.append("stop:cancel"))
    lifecycle.on_shutdown("reap", lambda: calls.append("stop:reap"))
    app = _app(lifecycle)

    @app.get("/ping")
    def ping():
        calls.append("request")
        return {}

    with TestClient(app) as client:
        assert client.get("/ping").status_code == 200
    assert calls == ["start:scan", "start:recover", "request", "stop:cancel", "stop:reap"]
    assert [name for name, _step in lifecycle.shutdown] == ["cancel", "reap"]


def test_a_failing_shutdown_step_cannot_cost_the_reaper_and_is_still_reported():
    """The one deliberate difference from `on_event`, which stopped at the first raising shutdown
    hook: resume-timer cancellation runs FIRST so the reaper sees every child it let through, and a
    cancellation that raised must not also cost a JupyterHub pod its reaper."""
    calls = []
    lifecycle = ServerLifecycle()

    def cancel_resume_timers():
        calls.append("cancel")
        raise RuntimeError("cancellation failed")

    lifecycle.on_shutdown("cancel_resume_timers", cancel_resume_timers)
    lifecycle.on_shutdown("reap_on_shutdown", lambda: calls.append("reap"))
    with pytest.raises(RuntimeError, match="cancellation failed"):
        with TestClient(_app(lifecycle)):
            pass
    assert calls == ["cancel", "reap"]


def test_a_failing_startup_refuses_to_serve_and_still_unwinds_what_it_armed():
    """A server that could not recover its runs' durable intents must not serve them — and the
    timers an earlier startup step armed are cancelled rather than left to the process exit."""
    calls = []
    lifecycle = ServerLifecycle()
    lifecycle.on_startup("scan", lambda: calls.append("scan"))

    def recover():
        raise RuntimeError("recovery failed")

    lifecycle.on_startup("recover", recover)
    lifecycle.on_startup("never", lambda: calls.append("never"))
    lifecycle.on_shutdown("cancel", lambda: calls.append("cancel"))
    with pytest.raises(RuntimeError, match="recovery failed"):
        with TestClient(_app(lifecycle)):
            calls.append("served")
    assert calls == ["scan", "cancel"]


def test_no_serve_module_registers_a_deprecated_lifecycle_hook():
    """NEGATIVE pin over `looplab/serve/`. An `on_event`/`add_event_handler` hook registered on an
    app that owns a `lifespan=` never runs, and nothing reports it, so one coming back is a hook
    silently switched off. AST, not text, so a comment explaining the migration cannot trip it."""
    from _source_scan import iter_trees     # the shared walk: one decoding, checkpoints excluded

    offenders = []
    for path, tree in iter_trees(ROOT / "looplab" / "serve"):
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in {"on_event", "add_event_handler"}):
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert offenders == [], offenders
