"""The run-level stop word is a registry with one comparison (doc 52 §5.1 row 6, 2026-09-06).

`run_finished.reason` / `RunState.stop_reason` was decided at fourteen sites by
`str(x or "").lower() == "error"` (or `!=`) against a word no registry held — one writer (the CLI's
guarded abort) and thirteen readers answering "did this run finish cleanly, or does it still owe a
finalize?" A drifted spelling at one site silently changed that answer there and nowhere else. The
two node-terminal registries (`FAILURE_REASONS`, `ENGINE_TERMINAL_REASONS`) never covered it.

The guard is two-way and by AST, never by substring: no comparison against the literal survives
anywhere under `looplab/` but inside `is_error_stop` itself, and the set of modules that call the
helper is exactly the set this file names — a new reader must be listed here, and a reader that
stops asking is stale.
"""
from __future__ import annotations

import ast

from _source_scan import iter_trees
from pathlib import Path

import pytest

from looplab.core.models import RUN_STOP_ERROR, is_error_stop
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold

PKG = Path(__file__).resolve().parents[1] / "looplab"

# Every module that decides on the stop word, by the one helper. Two-way with the AST scan below.
READERS = {
    "cli/run_cmds.py", "engine/orchestrator.py", "engine/finalize.py", "events/finalize_scope.py",
    "serve/run_commands.py", "serve/appstate.py", "serve/command_observation.py",
    "serve/control_validation.py",
}


def test_the_helper_is_the_one_comparison():
    assert RUN_STOP_ERROR == "error"
    assert is_error_stop("error") and is_error_stop("Error") and is_error_stop(" ERROR ".strip())
    assert not is_error_stop(None) and not is_error_stop("") and not is_error_stop("aborted")
    assert not is_error_stop("errors")


def test_the_written_word_folds_to_the_word_the_readers_ask_about(tmp_path):
    """The writer (`cli/run_cmds.py`'s guarded abort) and the fold agree through the registry."""
    store = EventStore(tmp_path / "events.jsonl")
    store.append("run_started", {"run_id": "r", "task_id": "t", "direction": "min"})
    store.append("run_finished", {"reason": RUN_STOP_ERROR, "error": "boom"})
    state = fold(store.read_all())
    assert state.finished and is_error_stop(state.stop_reason)


def _compares_against_the_literal(node: ast.Compare) -> bool:
    return any(isinstance(c, ast.Constant) and c.value == "error" for c in node.comparators)


def test_no_site_compares_the_stop_word_as_a_literal():
    """MUTATION: put one `str(x or "").lower() == "error"` back anywhere under looplab/ -> named."""
    offenders = []
    for path, tree in iter_trees(PKG):
        for node in ast.walk(tree):
            if isinstance(node, ast.Compare) and _compares_against_the_literal(node):
                # Only a comparison whose left side reads a stop/finish REASON: `.lower()` on it,
                # `stop_reason`, or `.get("reason")`. A `level == "error"` on a log line is not it.
                text = ast.unparse(node.left)
                if "lower()" in text or "stop_reason" in text or 'get("reason")' in text:
                    rel = str(path.relative_to(PKG))
                    if rel == "core/models.py":
                        continue          # the helper's own body
                    offenders.append(f"{rel}:{node.lineno}: {ast.unparse(node)}")
    assert not offenders, "\n".join(offenders)


def test_the_readers_are_exactly_the_listed_modules():
    calling = set()
    for path, tree in iter_trees(PKG):
        rel = str(path.relative_to(PKG))
        if rel == "core/models.py":
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "is_error_stop":
                calling.add(rel)
    assert calling == READERS, (calling ^ READERS)


def test_the_writer_spells_the_word_through_the_registry():
    """The one producer of the run-level stop word names the constant, not the string."""
    src = (PKG / "cli" / "run_cmds.py").read_text(encoding="utf-8")
    assert '{"reason": "error"' not in src
    tree = ast.parse(src)
    named = any(isinstance(n, ast.Name) and n.id == "RUN_STOP_ERROR" for n in ast.walk(tree))
    assert named, "the guarded abort no longer writes the registered word"


# --------------------------------------------------------------------------- the refusal type


def test_a_failed_run_setup_is_an_environment_refusal(tmp_path, monkeypatch):
    """`engine/eval_dispatch.py` raised a bare `RuntimeError("run_setup failed …")` where every other
    deliberate refusal about the operator's box wears `core/errors.py::EnvironmentRefusal`, so the
    CLI boundary printed it as a 42-frame engine crash. Same base class, so the swap is invisible to
    every `except RuntimeError` on the way up. MUTATION: raise RuntimeError again -> the isinstance
    below fails while the RuntimeError one still passes."""
    from looplab.core.errors import EnvironmentRefusal, OperatorRefusal
    from looplab.runtime import sandbox
    from tests.factories import make_engine

    engine = make_engine(tmp_path / "run")
    engine._eval_spec = {"setup": ["false"], "run_setup_timeout": 5.0}
    # `_do_run_setup` imports `_run_argv` from the sandbox module at call time, so that is the seam.
    monkeypatch.setattr(sandbox, "_run_argv",
                        lambda *a, **k: (1, "", "ERROR: No matching distribution found for nope\n", False))
    with pytest.raises(EnvironmentRefusal) as excinfo:
        engine._do_run_setup(["false"])
    exc = excinfo.value
    assert isinstance(exc, RuntimeError) and isinstance(exc, OperatorRefusal)
    assert "run_setup failed" in str(exc) and "eval.setup" in str(exc), str(exc)
