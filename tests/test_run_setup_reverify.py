"""A durable "run_setup succeeded" no longer outlives the environment it installed.

Incident 2026-10-06: a container restart wiped the conda env under a run whose log still carried
`run_setup_finished{exit_code: 0}`. The fold's exactly-once record made every resume SKIP the
install, so each node crashed on an import and bought a triage + repair for it. The record is
evidence the command succeeded once; whether its effects survive is asked of the interpreter.
"""
from __future__ import annotations

from pathlib import Path

from looplab.events.eventstore import EventStore
from looplab.runtime import deps

from test_declared_deps import _LIVE_SHAPES, _engine, _repo


def _resume_engine(tmp_path, monkeypatch, *, absent, text=_LIVE_SHAPES):
    """Run the derived setup once in a first Engine, then build a SECOND Engine over the same run
    directory (a resume in a new process) with the interpreter's answer scripted to `absent`."""
    calls: list = []

    def fake_run_argv(argv, cwd, timeout, log_path=None):
        calls.append(list(argv))
        return (0, "ok", "", False)

    monkeypatch.setattr("looplab.runtime.sandbox._run_argv", fake_run_argv)
    repo = _repo(tmp_path, text)

    def _make():
        eng = _engine(tmp_path / "run", auto_install_deps=True)
        eng._repo_spec = {"editables": [{"name": ".", "path": str(repo)}]}
        eng._eval_spec = {"run_setup": [], "run_setup_timeout": 60.0}
        return eng

    _make()._ensure_run_setup()
    assert len(calls) == 1, "the first process installs once"
    asked: list = []

    def fake_absent(dists, *, python=None, timeout=60.0):
        asked.append(sorted(dists))
        return absent

    monkeypatch.setattr(deps, "absent_distributions", fake_absent)
    _make()._ensure_run_setup()
    started = [e.data for e in EventStore(Path(tmp_path) / "run" / "events.jsonl").read_all()
               if e.type == "run_setup_started"]
    return calls, asked, started


def test_a_resume_whose_environment_survived_skips_the_install(tmp_path, monkeypatch):
    calls, asked, started = _resume_engine(tmp_path, monkeypatch, absent=[])
    assert len(calls) == 1 and len(started) == 1
    assert asked, "the resume must ASK the interpreter, not trust the record alone"


def test_a_resume_whose_environment_was_wiped_reinstalls_and_says_why(tmp_path, monkeypatch):
    calls, _asked, started = _resume_engine(tmp_path, monkeypatch, absent=["polars", "torch"])
    assert len(calls) == 2, "the wiped environment must be installed again"
    assert started[-1]["reverified_missing"] == ["polars", "torch"]
    assert "reverified_missing" not in started[0], "the first install is an ordinary one"


def test_a_probe_that_cannot_run_does_not_reinstall(tmp_path, monkeypatch):
    """None = the question could not be asked (a vanished interpreter is the infra probe's to
    report); re-running pip into it could only fail."""
    calls, _asked, started = _resume_engine(tmp_path, monkeypatch, absent=None)
    assert len(calls) == 1 and len(started) == 1


def test_lines_the_first_install_dropped_are_not_counted_as_lost(tmp_path, monkeypatch):
    from test_declared_deps import _NO_DIST
    results = iter([(1, "", _NO_DIST, False), (0, "ok", "", False)])
    calls: list = []

    def fake_run_argv(argv, cwd, timeout, log_path=None):
        calls.append(list(argv))
        return next(results, (0, "ok", "", False))

    monkeypatch.setattr("looplab.runtime.sandbox._run_argv", fake_run_argv)
    repo = _repo(tmp_path, _LIVE_SHAPES + "ecom-mlflow\n")

    def _make():
        eng = _engine(tmp_path / "run", auto_install_deps=True)
        eng._repo_spec = {"editables": [{"name": ".", "path": str(repo)}]}
        eng._eval_spec = {"run_setup": [], "run_setup_timeout": 60.0}
        return eng

    _make()._ensure_run_setup()
    asked: list = []
    monkeypatch.setattr(deps, "absent_distributions",
                        lambda dists, **k: asked.append(sorted(dists)) or [])
    _make()._ensure_run_setup()
    assert asked and "ecom-mlflow" not in asked[0], (
        "a line the install dropped as unresolvable was never installed; counting it as lost would "
        "re-run the install on every resume")


def test_absent_distributions_reads_the_real_interpreter():
    assert deps.absent_distributions(["pytest", "no-such-dist-looplab-xyz"]) == [
        "no-such-dist-looplab-xyz"]
    assert deps.absent_distributions([]) == []
    assert deps.absent_distributions(["pytest"], python="/nonexistent/python") is None


def test_a_marker_guarded_requirement_is_asked_only_where_its_marker_holds():
    """critic 2026-10-08: pip SKIPS `tomli; python_version<"3.11"` on a newer interpreter, so it is
    never installed and never dropped — and an unevaluated marker re-ran the install every resume.
    The marker is evaluated in the eval interpreter itself."""
    from looplab.runtime.deps import absent_distributions
    got = absent_distributions({
        "pywin32": 'pywin32>=300; sys_platform=="win32" and sys_platform=="linux"',
        "dataclasses-xyz": 'dataclasses-xyz; python_version<"3.0"',
        "looplab-missing-xyz": "looplab-missing-xyz==1",
        "looplab-missing-guarded": 'looplab-missing-guarded; python_version>="3.0"',
        "pip": "pip"})
    assert got == ["looplab-missing-guarded", "looplab-missing-xyz"]
    assert absent_distributions(["looplab-missing-xyz", "pip"]) == ["looplab-missing-xyz"]
