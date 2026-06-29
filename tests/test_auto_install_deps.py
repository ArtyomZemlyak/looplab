"""Environment self-prep: auto-install a missing KNOWN library and re-run, instead of letting the
crash-triage agent reject the idea. Plus timeout recovery (a timeout is repaired by reducing
compute, not abandoned). Covers the pure deps helpers and the engine integration end-to-end with
an INJECTED installer (no network / no real pip).
"""
from __future__ import annotations

from pathlib import Path

import anyio

from looplab import deps
from looplab.deps import InstallResult
from looplab.eventstore import EventStore
from looplab.models import Idea
from looplab.orchestrator import Engine
from looplab.policy import GreedyTree
from looplab.replay import fold
from looplab.sandbox import SubprocessSandbox
from looplab.toytask import ToyTask

ROOT = Path(__file__).resolve().parents[1]
TASK = ROOT / "examples" / "toy_task.json"

_GOOD = "import json; print(json.dumps({'metric': 0.1}))\n"


class _Stub:
    def propose(self, state, parent):
        return Idea(operator="x", params={"x": 1.0, "y": 1.0})


def _engine(run_dir, dev, **kw):
    return Engine(run_dir, task=ToyTask.load(TASK), researcher=_Stub(), developer=dev,
                  sandbox=SubprocessSandbox(),
                  policy=GreedyTree(n_seeds=1, max_nodes=4, debug_depth=1), **kw)


def _events(run_dir):
    return list(EventStore(Path(run_dir) / "events.jsonl").read_all())


# --------------------------------------------------------------------------- pure deps helpers
def test_missing_modules_extracts_top_level_packages():
    err = (
        "Traceback (most recent call last):\n"
        '  File "solution.py", line 5, in <module>\n'
        "    from catboost import CatBoostRegressor\n"
        "ModuleNotFoundError: No module named 'catboost'\n"
    )
    assert deps.missing_modules(err) == ["catboost"]
    # dotted import -> top-level package (the unit pip installs)
    assert deps.missing_modules("No module named 'torch.nn'") == ["torch"]
    # de-duplicated, first-seen order
    twice = "No module named 'xgboost'\nNo module named 'xgboost'\nNo module named 'torch'"
    assert deps.missing_modules(twice) == ["xgboost", "torch"]
    assert deps.missing_modules("clean run, no import error") == []


def test_is_installable_allowlist_and_pip_name_mapping():
    # the run's real failures are all on the allowlist
    for m in ("torch", "catboost", "xgboost"):
        assert deps.is_installable(m)
    # import name != pip name is the whole point of the map
    assert deps.pip_package("sklearn") == "scikit-learn"
    assert deps.pip_package("cv2") == "opencv-python"
    assert deps.pip_package("torch") == "torch"
    # a typo'd / local helper module is NOT auto-installed (it's a code bug, not a missing lib)
    assert not deps.is_installable("my_local_helper_zzz")
    assert not deps.is_installable("definitely_not_a_real_module")


# ----------------------------------------------------------------- engine: install-then-rerun
class _MissingLibThenInstalled:
    """First eval raises ModuleNotFoundError for a 'known' lib; once the injected installer drops a
    flag file, the SAME code finds it and emits a metric — modelling install-then-rerun without a
    real pip/network round-trip."""

    def __init__(self, flag: Path):
        self.flag = flag
        self.repair_calls = 0

    def implement(self, idea):
        return (
            "import os, json\n"
            f"if not os.path.exists(r'{self.flag}'):\n"
            "    raise ModuleNotFoundError(\"No module named 'faketestlib'\")\n"
            "print(json.dumps({'metric': 0.1}))\n"
        )

    def repair(self, idea, code, error):       # must not be needed on the happy path
        self.repair_calls += 1
        return _GOOD


def test_missing_lib_is_installed_and_rerun_not_rejected(tmp_path, monkeypatch):
    # make the synthetic lib auto-installable (it stands in for torch/catboost/xgboost)
    monkeypatch.setitem(deps._PIP_NAME, "faketestlib", "faketestlib")
    flag = tmp_path / "installed.flag"
    calls: list[str] = []

    def fake_install(package, *, python=None, timeout=None):
        calls.append(package)
        flag.write_text("x", encoding="utf-8")     # the (fake) install makes the re-run succeed
        return InstallResult(package=package, ok=True, returncode=0)

    dev = _MissingLibThenInstalled(flag)
    eng = _engine(tmp_path / "run", dev, auto_install_deps=True, dep_installer=fake_install)
    anyio.run(eng.run)

    evs = _events(tmp_path / "run")
    installed = [e for e in evs if e.type == "deps_installed"]
    assert installed, "expected a deps_installed audit event"
    assert installed[0].data["packages"] == ["faketestlib"]
    assert calls == ["faketestlib"]               # installer invoked once

    st = fold(evs)
    n0 = st.nodes[0]
    assert n0.metric == 0.1                        # node ran after install
    assert n0.status.name == "evaluated"
    assert n0.error_reason != "idea_rejected"      # the whole point: NOT rejected for a missing lib
    assert dev.repair_calls == 0                   # install + rerun, no code repair needed


def test_failed_install_falls_through_to_repair_and_caches(tmp_path, monkeypatch):
    """When the install FAILS (offline / not on PyPI), env-prep gives up on that module (cached so it
    can't loop) and the normal inline-repair path takes over."""
    monkeypatch.setitem(deps._PIP_NAME, "faketestlib", "faketestlib")
    flag = tmp_path / "nope.flag"                  # never created -> code keeps crashing
    calls: list[str] = []

    def failing_install(package, *, python=None, timeout=None):
        calls.append(package)
        return InstallResult(package=package, ok=False, returncode=1, output="No matching distribution")

    dev = _MissingLibThenInstalled(flag)           # its repair() returns working code
    eng = _engine(tmp_path / "run", dev, auto_install_deps=True, dep_installer=failing_install,
                  inline_repair=True, inline_repair_attempts=1)
    anyio.run(eng.run)

    evs = _events(tmp_path / "run")
    assert not any(e.type == "deps_installed" for e in evs)   # nothing installed
    assert len(calls) == 1                          # tried once, then cached in _dep_failed (no loop)
    # fell through to inline repair -> the node was fixed in place
    assert any(e.type == "node_repaired" for e in evs)
    st = fold(evs)
    assert st.nodes[0].status.name == "evaluated"


def test_raising_installer_degrades_not_crashes(tmp_path, monkeypatch):
    """A custom dep_installer that RAISES must not crash the eval — env-prep swallows it, the module is
    marked attempted (no loop), and the node flows to the normal inline-repair path."""
    monkeypatch.setitem(deps._PIP_NAME, "faketestlib", "faketestlib")
    flag = tmp_path / "nope.flag"                  # never created -> code keeps crashing
    calls: list[str] = []

    def boom_install(package, *, python=None, timeout=None):
        calls.append(package)
        raise RuntimeError("installer blew up")

    dev = _MissingLibThenInstalled(flag)           # its repair() returns working code
    eng = _engine(tmp_path / "run", dev, auto_install_deps=True, dep_installer=boom_install,
                  inline_repair=True, inline_repair_attempts=1)
    anyio.run(eng.run)                              # must not raise

    evs = _events(tmp_path / "run")
    assert not any(e.type == "deps_installed" for e in evs)
    assert len(calls) == 1                          # tried once, marked attempted, no loop
    st = fold(evs)
    assert st.nodes[0].status.name == "evaluated"   # recovered via inline repair


def test_auto_install_disabled_skips_env_prep(tmp_path, monkeypatch):
    monkeypatch.setitem(deps._PIP_NAME, "faketestlib", "faketestlib")
    flag = tmp_path / "installed.flag"
    calls: list[str] = []

    def fake_install(package, *, python=None, timeout=None):
        calls.append(package)
        return InstallResult(package=package, ok=True, returncode=0)

    dev = _MissingLibThenInstalled(flag)
    eng = _engine(tmp_path / "run", dev, auto_install_deps=False, dep_installer=fake_install)
    anyio.run(eng.run)

    assert calls == []                              # installer never called
    assert not any(e.type == "deps_installed" for e in _events(tmp_path / "run"))


def test_auto_install_gated_by_flag(tmp_path):
    # the engine gate is `auto_install_deps and trust_mode == 'trusted_local'`; the default tier
    # here is trusted_local, so the flag alone decides.
    on = _engine(tmp_path / "a", _MissingLibThenInstalled(tmp_path), auto_install_deps=True)
    assert on._auto_install_deps is True
    off = _engine(tmp_path / "b", _MissingLibThenInstalled(tmp_path), auto_install_deps=False)
    assert off._auto_install_deps is False


# ----------------------------------------------------------------- engine: timeout recovery
class _TimeoutThenFast:
    """First eval sleeps past the budget (timeout); repair() returns fast code. Asserts the repair
    context tells the Developer it was a timeout (so it reduces compute rather than guessing)."""

    def __init__(self):
        self.repair_calls = 0
        self.last_error = ""

    def implement(self, idea):
        return "import time\ntime.sleep(10)\n"

    def repair(self, idea, code, error):
        self.repair_calls += 1
        self.last_error = error
        return _GOOD


def test_timeout_is_repaired_by_reducing_compute(tmp_path):
    dev = _TimeoutThenFast()
    # timeout 2s: the 10s sleep is killed (reason=timeout); _GOOD finishes well under 2s on re-run.
    eng = _engine(tmp_path / "run", dev, timeout=2.0, auto_install_deps=False,
                  inline_repair=True, inline_repair_attempts=1)
    anyio.run(eng.run)

    evs = _events(tmp_path / "run")
    repaired = [e for e in evs if e.type == "node_repaired" and e.data.get("node_id") == 0]
    assert repaired, "a timeout should be inline-repaired (default inline_repair_reasons includes it)"
    assert dev.repair_calls >= 1
    assert "timeout" in dev.last_error.lower()       # the cost-reduction directive reached the Developer
    st = fold(evs)
    assert st.nodes[0].status.name == "evaluated"    # recovered, not left dead
