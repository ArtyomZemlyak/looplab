"""A run whose `config.snapshot.json` is gone may not be resumed on ambient defaults.

`load_run_settings(..., strict=True)` is what `resume`, `finalize` and the finalization recovery use
to answer "which settings does this run actually run with". With the snapshot ABSENT it returned a
fresh `Settings()`, whose `require_approval` is False — and until 2026-09-06 `require_approval` was
read LIVE off Settings, not pinned in `run_started` (it is pinned now, `tests/test_require_approval_
pin.py`; the other settings named below still are not) — so deleting one file finished a paused
approval-pending run with no approval. `trust_mode`, `eval_trust_mode`, `confirm_*` and `backend` degraded the same way,
silently, which is the case the loader's own docstring already warned about for the CORRUPT snapshot
and then did anyway for the absent one.

The discriminator is the run's own EVENT LOG. A bare `--out` path with no log is a fresh run whose
snapshot has not been written yet — this function is called before the engine writes one — so
refusing on "the file is absent" alone would break `looplab run`.
"""
from __future__ import annotations

import json

import pytest
import typer

from looplab.cli import load_run_settings
from looplab.core.config import Settings


def _log(rd):
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "events.jsonl").write_text(
        json.dumps({"seq": 0, "type": "run_started", "data": {"run_id": "r"}}) + "\n",
        encoding="utf-8")
    return rd


def _snapshot(rd, **fields):
    (rd / "config.snapshot.json").write_text(json.dumps(fields), encoding="utf-8")


def test_an_existing_run_with_no_snapshot_is_REFUSED_when_the_caller_requires_it(tmp_path):
    """THE DEFECT. MUTATION: return `Settings()` here again -> `resume` continues an
    approval-pending run with `require_approval=False` and finishes it unapproved."""
    rd = _log(tmp_path / "run")
    with pytest.raises(typer.BadParameter) as excinfo:
        load_run_settings(rd, strict=True, require_snapshot=True)
    message = str(excinfo.value)
    assert "config.snapshot.json" in message
    assert "require_approval" in message, "the refusal must name what would have been lost"


def test_the_refusal_would_have_hidden_a_REAL_approval_gate(tmp_path):
    """Driven as the incident rather than as a signature: the snapshot says `require_approval: true`,
    and the value the caller gets must never silently become False."""
    rd = _log(tmp_path / "run")
    _snapshot(rd, require_approval=True)
    assert load_run_settings(rd, strict=True).require_approval is True

    (rd / "config.snapshot.json").unlink()
    with pytest.raises(typer.BadParameter):
        load_run_settings(rd, strict=True, require_snapshot=True)
    # ...and the ambient default this used to fall back to is the unsafe one.
    assert Settings().require_approval is False


def test_a_FRESH_run_directory_is_untouched(tmp_path):
    """`looplab run --out <new dir>` calls this before the engine has written a snapshot. MUTATION:
    key the refusal on the file's absence alone -> starting any new run refuses."""
    fresh = tmp_path / "brand-new"
    fresh.mkdir()
    assert load_run_settings(fresh, strict=True, require_snapshot=True).require_approval is False


def test_a_LEGACY_run_is_still_FINALIZABLE(tmp_path):
    """The narrowing, and the regression the first cut caused. A run predating
    `config.snapshot.json` is a real, supported thing, and `finalize` / the finalization recovery
    wrap up a run that has already STOPPED — they never run the search spine, so they cannot reach
    the approval gate this refusal exists for. Refusing them makes an old run permanently
    unfinishable over a gate it can no longer touch.

    MUTATION: key the refusal on `strict` again -> `looplab finalize` on any pre-snapshot run dies,
    which `tests/test_finalization_recovery.py` catches by name.
    """
    rd = _log(tmp_path / "run")
    assert load_run_settings(rd, strict=True).require_approval is False


def test_only_RESUME_asks_for_it():
    """The asymmetry, by AST over the real command module rather than by counting strings: a second
    caller quietly adopting `require_snapshot=True` re-breaks legacy finalize."""
    import ast
    from pathlib import Path as _Path

    from looplab.cli import run_cmds

    tree = ast.parse(_Path(run_cmds.__file__).read_text(encoding="utf-8"))
    asked = [node for node in ast.walk(tree)
             if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "load_run_settings"
             and any(kw.arg == "require_snapshot" and getattr(kw.value, "value", False) is True
                     for kw in node.keywords)]
    assert len(asked) == 1, "exactly one caller may require the snapshot"
    enclosing = [fn.name for fn in ast.walk(tree)
                 if isinstance(fn, ast.FunctionDef)
                 and any(call is asked[0] for call in ast.walk(fn))]
    assert "resume" in enclosing, enclosing


def test_a_nonexistent_directory_and_None_still_answer(tmp_path):
    """Both were already contract (`tests/test_cli_shared_indirection.py`) and stay so."""
    for mode in (True, False):
        assert load_run_settings(tmp_path / "no-such-run", strict=mode) is not None
    assert load_run_settings(None, strict=True) is not None


def test_a_READ_ONLY_diagnostic_still_degrades_to_ambient(tmp_path):
    """`strict=False` is the diagnostics path: an old or partially-written run must stay READABLE.
    MUTATION: apply the refusal to both modes -> every read command on a snapshot-less run dies."""
    rd = _log(tmp_path / "run")
    assert load_run_settings(rd, strict=False) is not None


def test_a_present_snapshot_is_unchanged_in_both_modes(tmp_path):
    """The whole change is about ABSENCE; a run that has its snapshot must behave byte-identically."""
    rd = _log(tmp_path / "run")
    _snapshot(rd, max_nodes=7, require_approval=True)
    for mode in (True, False):
        settings = load_run_settings(rd, strict=mode)
        assert settings.max_nodes == 7 and settings.require_approval is True


def test_the_corrupt_case_is_still_its_own_refusal(tmp_path):
    """Unchanged behaviour, pinned here because the two failures now live one branch apart and a
    later edit could collapse them into one message that names the wrong remedy."""
    rd = _log(tmp_path / "run")
    (rd / "config.snapshot.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(typer.BadParameter) as excinfo:
        load_run_settings(rd, strict=True)
    assert "events.jsonl" not in str(excinfo.value), "a corrupt snapshot is not the missing-file case"


def test_resume_refuses_end_to_end(tmp_path):
    """Through the real command, which is where the approval gate is actually reached."""
    from typer.testing import CliRunner

    from looplab.cli import app

    rd = _log(tmp_path / "run")
    (rd / "task.snapshot.json").write_text(json.dumps({
        "kind": "quadratic", "task_id": "t", "goal": "g", "direction": "min",
        "expr": "(x-3)**2"}), encoding="utf-8")
    result = CliRunner().invoke(app, ["resume", str(rd)])
    assert result.exit_code != 0
    # Click renders a BadParameter inside a box that wraps and pads every line, so compare on the
    # text with all whitespace and box drawing removed rather than on the raw output.
    flat = "".join(ch for ch in result.output if not ch.isspace() and ch not in "|\u2502")
    assert "config.snapshot.jsonismissing" in flat, result.output
