"""A requirement whose environment MARKER excludes it on this box is never "lost" (review 2026-10-08).

`pywin32==306; sys_platform == "win32"` on Linux, or `tomli; python_version < "3.11"` on 3.12, was
never installed — pip skipped it — yet the run_setup re-verification asked the interpreter for it,
found it absent, and re-ran the whole install on EVERY resume under a false `reverified_missing`.
The marker is now evaluated IN the eval interpreter (`deps.absent_distributions(markers=)`), never
with the engine's own `sys`, since the two may differ.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from looplab.events.eventstore import EventStore
from looplab.runtime import deps

from test_declared_deps import _engine, _repo

_MARKED = ('pywin32==306; sys_platform == "win32"\n'
           'tomli; python_version < "3.11"\n')


@pytest.mark.parametrize("line, marker", [
    ('pywin32==306; sys_platform == "win32"', 'sys_platform == "win32"'),
    ('tomli; python_version < "3.11"', 'python_version < "3.11"'),
    ('tomli ; python_version < "3.11"  # a comment', 'python_version < "3.11"'),
    ("polars", None),
    ("transformers==4.51.0", None),
    ("bm25s[full]>=0.2", None),
    ("pkg; ", None),
    # PEP 508: a URL requirement's marker needs whitespace before the `;`, so a `;` inside the URL
    # stays the URL's.
    ("pkg @ https://h.example/p.whl;x=1", None),
    ('pkg @ https://h.example/p.whl ; sys_platform == "linux"', 'sys_platform == "linux"'),
])
def test_the_marker_is_read_off_the_operator_s_line(line, marker):
    assert deps.requirement_marker(line) == marker


def _host_excludes(name: str) -> bool:
    """Whether THIS interpreter's own marker answer excludes the two driven lines — the test's
    interpreter IS the eval interpreter here (`python=None` -> `sys.executable`)."""
    if name == "pywin32":
        return sys.platform != "win32"
    if name == "tomli":
        return sys.version_info >= (3, 11)
    raise AssertionError(name)


def test_a_marker_false_on_the_eval_interpreter_is_not_reported_absent():
    pins, _directives = deps.parse_requirements(_MARKED)
    names = sorted(pins) + ["no-such-dist-looplab-xyz", "no-such-dist-looplab-marked"]
    markers = {n: deps.requirement_marker(pins[n]) for n in pins}
    # A marker that HOLDS everywhere is probed as before; one that cannot be evaluated counts as
    # applying, so a marker never hides a distribution the probe could not rule out.
    markers["no-such-dist-looplab-marked"] = 'python_version >= "3"'
    markers["no-such-dist-looplab-xyz"] = "this is not a marker"
    got = deps.absent_distributions(names, markers=markers)
    assert got is not None
    for name in ("pywin32", "tomli"):
        if _host_excludes(name):
            assert name not in got, f"{name}'s marker excludes it on this box: {got}"
    assert "no-such-dist-looplab-xyz" in got and "no-such-dist-looplab-marked" in got, got


def test_no_markers_is_the_old_probe():
    assert deps.absent_distributions(["pytest", "no-such-dist-looplab-xyz"]) == [
        "no-such-dist-looplab-xyz"]


def test_a_resume_does_not_reinstall_lines_this_box_never_needed(tmp_path, monkeypatch):
    """Driven through `_ensure_run_setup` over two Engines (a resume in a new process), with the
    REAL interpreter probe: the declaration holds only lines this box's markers exclude."""
    if not (_host_excludes("pywin32") and _host_excludes("tomli")):
        pytest.skip("this interpreter satisfies one of the markers; the driven case needs neither")
    calls: list = []

    def fake_run_argv(argv, cwd, timeout, log_path=None):
        calls.append(list(argv))
        return (0, "ok", "", False)

    monkeypatch.setattr("looplab.runtime.sandbox._run_argv", fake_run_argv)
    repo = _repo(tmp_path, _MARKED)

    def _make():
        eng = _engine(tmp_path / "run", auto_install_deps=True)
        eng._repo_spec = {"editables": [{"name": ".", "path": str(repo)}]}
        eng._eval_spec = {"run_setup": [], "run_setup_timeout": 60.0}
        return eng

    _make()._ensure_run_setup()
    assert len(calls) == 1, "the first process installs once"
    _make()._ensure_run_setup()
    started = [e.data for e in EventStore(Path(tmp_path) / "run" / "events.jsonl").read_all()
               if e.type == "run_setup_started"]
    assert len(calls) == 1 and len(started) == 1, (
        f"the install re-ran on resume: {[s.get('reverified_missing') for s in started]}")
