"""The two CLI-wide indirections that were written out once per command module (doc 25 CT-07, CT-08).

CT-07 — five hand-written late-binding shims. `def X(*a, **k): from looplab import cli; return
cli.X(*a, **k)` appeared five times, each with its own paragraph re-explaining the same
freeze-at-import hazard. `core/latebind.py::late_bound` is that explanation written once.

CT-08 — `config.snapshot.json` loaded three ways with three failure semantics: a strict loader, two
verbatim inline prologues calling it, and an independent re-implementation in `inspect_cmds` that
re-parsed the JSON and fell back silently. "Which settings does this command actually run with" had
three answers depending on which command you typed. `cli.load_run_settings(run_dir, strict=…)` names
the two that are legitimate.

Both are seams a test can break silently, which is why they are pinned rather than trusted: a frozen
shim means a monkeypatched test drives the REAL engine or the REAL client and passes anyway, and a
strict-loader-turned-lenient means `resume` quietly finishes a not-yet-approved run under ambient
settings.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import typer

from looplab import bench, cli
from looplab.cli import export_cmds, load_run_settings, run_cmds
from looplab.core.latebind import late_bound


# ------------------------------------------------------------------ CT-07: the late-binding seam

@pytest.mark.parametrize("holder,attr,target", [
    (run_cmds, "_engine", "_engine"),
    (run_cmds, "make_llm_client", "make_llm_client"),
    (export_cmds, "make_llm_client", "make_llm_client"),
    (bench, "_engine", "_engine"),
])
def test_every_shim_resolves_the_package_attribute_at_call_time(holder, attr, target,
                                                                monkeypatch):
    """The whole point of the seam. A frozen `from looplab.cli import _engine` would have bound the
    pre-patch object at package-init time, and the command would drive the real engine against a
    test that believed it was offline — passing, having tested nothing."""
    monkeypatch.setattr(cli, target, lambda *a, **k: ("patched", a, k))
    assert getattr(holder, attr)(1, x=2) == ("patched", (1,), {"x": 2})


def test_the_shim_forwards_arguments_verbatim_including_none_and_keywords():
    calls = []
    shim = late_bound("looplab.cli", "_engine")
    original = cli._engine
    try:
        cli._engine = lambda *a, **k: calls.append((a, k)) or "answer"
        assert shim(None, 0, "", key=None, flag=False) == "answer"
    finally:
        cli._engine = original
    assert calls == [((None, 0, ""), {"key": None, "flag": False})]


def test_a_shim_can_be_defined_before_its_target_module_is_importable():
    """The import is inside the CALL, not in a closure cell, which is what lets a command module
    define its shim at module scope even though `looplab.cli` is mid-initialization when that module
    is imported — the situation at all five original sites."""
    shim = late_bound("looplab.this.module.does.not.exist", "anything")
    with pytest.raises(ModuleNotFoundError):
        shim()


def test_the_shim_keeps_the_targets_name_so_a_traceback_still_reads_right():
    assert late_bound("looplab.cli", "make_llm_client").__name__ == "make_llm_client"


def test_the_diagnostics_shim_composes_the_seam_rather_than_forwarding_to_it(monkeypatch):
    """`cli._make_llm_client` is the one that differs: it hands the builder to
    `make_llm_client_for` as a FACTORY. Late binding still has to reach it, or the diagnostics call a
    live endpoint from an offline test."""
    from looplab.core import llm as llm_module

    seen = {}

    def _build(settings, factory):
        try:
            seen["built"] = factory(settings)
        except Exception as exc:  # a FROZEN factory is the real builder, which explodes on a stub
            seen["built"] = f"{type(exc).__name__}: {exc}"

    monkeypatch.setattr(cli, "make_llm_client", lambda *a, **k: "the patched client")
    monkeypatch.setattr(llm_module, "make_llm_client_for", _build)
    cli._make_llm_client(object())
    assert seen["built"] == "the patched client"


def test_no_command_module_hand_writes_the_shim_again():
    """A grep guard: the body that was quintuplicated is `from looplab import cli` inside a function
    that immediately calls back into it."""
    root = Path(__file__).resolve().parents[1] / "looplab"
    offenders = []
    for path in sorted(list((root / "cli").glob("*.py")) + [root / "bench.py"]):
        text = path.read_text(encoding="utf-8")
        for index, line in enumerate(text.split("\n"), start=1):
            if line.strip() == "from looplab import cli":
                offenders.append(f"{path.name}:{index}")
    assert offenders == [], (
        f"hand-written late-binding shim outside `core/latebind.py`: {offenders}")


# ------------------------------------------------------------------ CT-08: one snapshot loader

def _snapshot(tmp_path: Path, payload) -> Path:
    run_dir = tmp_path / "run"
    run_dir.mkdir(exist_ok=True)
    (run_dir / "config.snapshot.json").write_text(
        payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")
    return run_dir


def test_a_valid_snapshot_is_loaded_the_same_way_under_both_modes(tmp_path):
    run_dir = _snapshot(tmp_path, {"llm_model": "recorded-model", "max_nodes": 7})
    for strict in (True, False):
        settings = load_run_settings(run_dir, strict=strict)
        assert settings.llm_model == "recorded-model" and settings.max_nodes == 7


@pytest.mark.parametrize("payload", ["{not json", "[]", '"a string"', "", '{"max_nodes": -4}'])
def test_strict_maps_every_corruption_to_one_operator_facing_line(tmp_path, payload):
    """A raw JSONDecodeError/ValidationError traceback does not tell the operator WHICH file to fix.
    The mode exists so `run`/`resume`/`finalize` name the file."""
    run_dir = _snapshot(tmp_path, payload)
    with pytest.raises(typer.BadParameter) as exc:
        load_run_settings(run_dir, strict=True)
    assert "cannot load original config snapshot" in str(exc.value)
    assert "config.snapshot.json" in str(exc.value)


@pytest.mark.parametrize("payload", ["{not json", "[]", '"a string"', "", '{"max_nodes": -4}'])
def test_lenient_degrades_to_ambient_so_a_diagnostic_can_still_read_an_old_run(tmp_path, payload):
    from looplab.core.config import Settings

    run_dir = _snapshot(tmp_path, payload)
    assert load_run_settings(run_dir, strict=False).llm_model == Settings().llm_model


def test_an_absent_snapshot_is_ambient_under_BOTH_modes(tmp_path):
    """`strict` is about corruption, not about requiring the file. The one caller that needs the file
    to exist checks for it itself, and says so in a message naming BOTH snapshots."""
    for strict in (True, False):
        assert load_run_settings(tmp_path / "no-such-run", strict=strict) is not None
    assert load_run_settings(None, strict=True) is not None


def test_finalization_recovery_still_refuses_a_missing_snapshot_by_name(tmp_path):
    """Routing the load through the shared helper must not have turned "the snapshot is gone" into
    "run with ambient settings" — recovery would then finalize a boundary with the wrong inputs."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    with pytest.raises(typer.BadParameter) as exc:
        run_cmds._pending_finalization_inputs(run_dir, task_id="toy")
    assert "task.snapshot.json" in str(exc.value) and "config.snapshot.json" in str(exc.value)


def test_the_lifecycle_commands_read_the_snapshot_STRICTLY():
    """`resume`/`finalize` falling back to ambient would silently drop run-only flags
    (require_approval, trust_mode, confirm_*, eval_trust_mode, backend, …) — e.g. finishing a paused,
    not-yet-approved run without any approval. A source check, because the alternative is driving a
    whole resume against a corrupt snapshot just to observe the exception."""
    import inspect

    source = inspect.getsource(run_cmds)
    assert "load_run_settings(run_dir, strict=False)" not in source
    assert source.count("load_run_settings(run_dir, strict=True)") == 3


def test_no_command_re_derives_the_snapshot_PARSE():
    """A grep guard on the third variant: `inspect_cmds` used to call `settings_from_snapshot`
    itself, which is what gave the same file a second decision table. The guard is on the PARSE, not
    on the filename — commands legitimately still name `config.snapshot.json` to write it (`run`),
    to check it exists (recovery), and to print it verbatim (`inspect`)."""
    root = Path(__file__).resolve().parents[1] / "looplab" / "cli"
    offenders = [f"{path.name}:{index}"
                 for path in sorted(root.glob("*.py")) if path.name != "__init__.py"
                 for index, line in enumerate(path.read_text(encoding="utf-8").split("\n"), 1)
                 if "settings_from_snapshot" in line]
    assert offenders == [], f"snapshot parsed outside `load_run_settings`: {offenders}"
