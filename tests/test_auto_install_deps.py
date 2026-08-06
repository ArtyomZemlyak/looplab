"""Environment self-prep: auto-install a missing KNOWN library and re-run, instead of letting the
crash-triage agent reject the idea. Plus timeout recovery (a timeout is repaired by reducing
compute, not abandoned). Covers the pure deps helpers and the engine integration end-to-end with
an INJECTED installer (no network / no real pip).
"""
from __future__ import annotations

from pathlib import Path

import anyio

from looplab.runtime import deps
from looplab.runtime.deps import InstallResult
from looplab.events.eventstore import EventStore
from looplab.core.models import Idea
from looplab.engine.orchestrator import Engine
from looplab.search.policy import GreedyTree
from looplab.events.replay import fold
from looplab.runtime.sandbox import SubprocessSandbox
from looplab.adapters.toytask import ToyTask

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


def test_the_retrieval_stack_is_installable_not_read_as_a_code_bug():
    """A dense-retrieval repo task imports these, and an off-list name is treated as a CODE BUG, not
    an install — so the node fails and the agent has no way forward when (as in the real testbed) the
    import sits in a `protect`ed file it cannot edit. Measured 2026-08-04: a rubert task died on
    `No module named 'sentence_transformers'` for exactly this reason.

    `faiss` maps to the CPU wheel deliberately: it is the build that installs everywhere, and a GPU
    build is an environment decision rather than something to infer from a traceback."""
    for module in ("sentence_transformers", "faiss", "evaluate"):
        assert deps.is_installable(module), f"{module} would be read as a code bug"
    assert deps.pip_package("sentence_transformers") == "sentence-transformers"
    assert deps.pip_package("faiss") == "faiss-cpu"


def test_the_traceback_does_not_always_name_the_missing_library():
    """The blind spot this scan HAS, and why the allowlist alone was never enough — an entry only
    helps when `missing_modules` can see the name. Both shapes are verbatim from
    `runs/rubert-dr-0805` node 0, and both cost that node a whole repair attempt:

      * a library that GUARDS an optional dependency degrades it into something else entirely
        (`transformers` → `is_accelerate_available()` → a bare NameError), so the distribution
        appears nowhere in the exception;
      * a library that re-raises its own missing-dependency error in prose rather than the
        canonical wording.

    `deps.triage_install_candidates` (see `tests/test_repair_budget_apportionment.py`) is the path
    that reaches these, using the crash-triage agent's diagnosis as the source of the NAME while
    keeping the traceback as the source of the EVIDENCE."""
    guarded = ("  File \"/opt/conda/lib/python3.11/site-packages/transformers/modeling_utils.py\", "
               "line 3736, in get_init_context\n    init_contexts = [no_init_weights(), "
               "init_empty_weights()]\nNameError: name 'init_empty_weights' is not defined\n")
    in_prose = ("ModuleNotFoundError: Neither `tensorboard` nor `tensorboardX` is available. "
                "Try `pip install`ing either.\n")
    assert deps.missing_modules(guarded) == []          # accelerate is nowhere in it
    assert deps.missing_modules(in_prose) == []         # not the "No module named 'X'" wording
    assert deps.is_installable("accelerate") and deps.is_installable("tensorboard")


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


# ------------------------------------------- a non-verdict may not drive an install (ordering)
class _NeverRepairs:
    """A developer whose `repair` must never be reached: the judge here never says "repair"."""

    def __init__(self, src):
        self.src = src
        self.repair_calls = 0

    def implement(self, idea):
        return self.src

    def repair(self, idea, code, error):
        self.repair_calls += 1
        raise AssertionError("the judge never said 'repair' — this must not be reached")


def test_a_judge_that_produced_no_verdict_cannot_drive_an_install(tmp_path, monkeypatch):
    """ORDER, and it is load-bearing. The triage-driven install used to run ABOVE the non-verdict
    branch in `_evaluate`'s attempt loop, and it `continue`s on success — so each successful install
    bought an already-diagnosed-dead judge another full eval and another triage call. Measured on the
    shipped loop: a judge answering `unanswerable` every round with a package-naming rationale bought
    7 evals and pushed SIX packages (faiss-cpu, tensorboardX, fastai, gensim, textblob, umap-learn)
    into the shared eval interpreter before the circuit breaker fired.

    That install exists because the agent's RATIONALE proves it read the traceback and named a
    library the traceback could not (`runtime/deps.py::triage_install_candidates`, hardened
    precisely against a shopping list). A verdict nobody could read denies that premise outright: it
    is not evidence about anything, least of all about what to pip install.

    Driven end to end rather than pinned on source order, because the property is "no pip ran", not
    "these lines are in this sequence"."""
    monkeypatch.setitem(deps._PIP_NAME, "faketestlib", "faketestlib")
    installs: list[str] = []

    def recording_install(package, *, python=None, timeout=None):
        installs.append(package)
        return InstallResult(package=package, ok=True, returncode=0, output="ok")

    # An unresolved-name-shaped traceback whose missing symbol is provided by an allowlisted lib —
    # the exact shape the triage-driven install is FOR, so nothing but the ordering can stop it.
    src = ("import sys\n"
           "sys.stderr.write('Traceback (most recent call last):\\n"
           "  File \"/w/solution.py\", line 3, in <module>\\n"
           "    x = faketestlib.thing()\\n"
           "NameError: name \\'faketestlib\\' is not defined\\n')\n"
           "sys.exit(1)\n")

    class _NonVerdictJudge(_Stub):
        def __init__(self, action):
            self.action = action
            self.calls = 0

        def triage_crash(self, node, error, attempt, **kw):
            self.calls += 1
            return {"action": self.action,
                    "rationale": "the faketestlib distribution is not installed, so the "
                                 "faketestlib symbol is undefined",
                    "missing_dependency": "faketestlib"}

    for action in ("unanswerable", "keep_going"):    # the transport word, and any other non-verdict
        dev = _NeverRepairs(src)
        judge = _NonVerdictJudge(action)
        run_dir = tmp_path / f"run_{action}"
        eng = Engine(run_dir, task=ToyTask.load(TASK), researcher=judge, developer=dev,
                     sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=1),
                     auto_install_deps=True, dep_installer=recording_install,
                     inline_repair=True, inline_repair_attempts=12)
        eng.store.append("run_started",
                         {"run_id": "r", "task_id": "t", "goal": "g", "direction": "min"})
        eng.store.append("node_created", {
            "node_id": 0, "parent_ids": [], "operator": "draft",
            "idea": {"operator": "draft", "params": {"x": 1.0, "y": 1.0}, "rationale": "seed"},
            "code": src})

        async def _bounded() -> bool:
            with anyio.move_on_after(120) as scope:
                await eng._evaluate(0, anyio.CapacityLimiter(1), None)
            return scope.cancelled_caught

        assert not anyio.run(_bounded), f"the loop did not terminate for {action}"
        evs = _events(run_dir)
        assert installs == [], f"{action} drove a pip install: {installs}"
        assert not [e for e in evs if e.type == "deps_installed"]
        assert judge.calls == 2, action      # one ask + one re-ask, then stop — not one per install
        assert len([e for e in evs if e.type in ("node_evaluated", "node_failed")]) == 1


def test_a_real_verdict_still_drives_the_install(tmp_path, monkeypatch):
    """The other direction of the same ordering: moving the breaker above the install must not cost
    the install itself, which is what lets a degraded optional dependency be fixed without spending
    a repair attempt (`runs/rubert-dr-0805` node 0)."""
    monkeypatch.setitem(deps._PIP_NAME, "faketestlib", "faketestlib")
    monkeypatch.setattr(deps, "is_present", lambda m, **kw: m != "faketestlib")
    installs: list[str] = []
    flag = tmp_path / "faketestlib.installed"

    def recording_install(package, *, python=None, timeout=None):
        installs.append(package)
        flag.write_text("ok")
        return InstallResult(package=package, ok=True, returncode=0, output="ok")

    src = ("import os, sys\n"
           f"if os.path.exists({str(flag)!r}):\n"
           "    import json; print(json.dumps({'metric': 0.1})); sys.exit(0)\n"
           "sys.stderr.write('Traceback (most recent call last):\\n"
           "  File \"/w/solution.py\", line 3, in <module>\\n"
           "    x = faketestlib.thing()\\n"
           "NameError: name \\'faketestlib\\' is not defined\\n')\n"
           "sys.exit(1)\n")

    class _NamesIt(_Stub):
        def triage_crash(self, node, error, attempt, **kw):
            return {"action": "repair",
                    "rationale": "the faketestlib distribution is not installed, so the "
                                 "faketestlib symbol is undefined",
                    "missing_dependency": "faketestlib"}

    dev = _NeverRepairs(src)                 # an install must fix it WITHOUT a code repair
    run_dir = tmp_path / "run"
    eng = Engine(run_dir, task=ToyTask.load(TASK), researcher=_NamesIt(), developer=dev,
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=1),
                 auto_install_deps=True, dep_installer=recording_install,
                 inline_repair=True, inline_repair_attempts=12)
    eng.store.append("run_started",
                     {"run_id": "r", "task_id": "t", "goal": "g", "direction": "min"})
    eng.store.append("node_created", {
        "node_id": 0, "parent_ids": [], "operator": "draft",
        "idea": {"operator": "draft", "params": {"x": 1.0, "y": 1.0}, "rationale": "seed"},
        "code": src})

    async def _bounded() -> bool:
        with anyio.move_on_after(120) as scope:
            await eng._evaluate(0, anyio.CapacityLimiter(1), None)
        return scope.cancelled_caught

    assert not anyio.run(_bounded)
    evs = _events(run_dir)
    assert installs == ["faketestlib"]
    dep_events = [e for e in evs if e.type == "deps_installed"]
    assert len(dep_events) == 1 and dep_events[0].data["source"] == "triage"
    assert dev.repair_calls == 0                     # an install is not a repair
    assert fold(evs).nodes[0].status.name == "evaluated"


def test_pip_child_does_not_inherit_the_operator_secrets(monkeypatch):
    """`pip install` of an sdist runs the package's setup.py/build backend — ARBITRARY CODE — so this
    child needs the same scrub every other spawn applies. It used to inherit the full os.environ, so a
    typosquatted sdist reaching a caller that skipped `is_installable` got the operator's keys.

    The scrub is NAME-based on purpose: pip's own index configuration is credential-bearing by design
    (`PIP_INDEX_URL` with an inline token for a private index), and stripping it would break exactly
    the installs an operator set up.
    """
    monkeypatch.setenv("LOOPLAB_LLM_API_KEY", "sk-must-not-leak")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-must-not-leak")
    monkeypatch.setenv("PIP_INDEX_URL", "https://user:tok@pypi.internal/simple")
    monkeypatch.setenv("PATH", "/usr/bin")

    seen: dict = {}

    class _Proc:
        returncode, stdout, stderr = 0, "ok", ""

    def _fake_run(argv, **kwargs):
        seen["env"] = kwargs.get("env")
        return _Proc()

    monkeypatch.setattr(deps.subprocess, "run", _fake_run)
    assert deps.install("numpy").ok

    env = seen["env"]
    assert env is not None, "the pip child inherited the parent environment wholesale"
    assert "LOOPLAB_LLM_API_KEY" not in env and "AWS_SECRET_ACCESS_KEY" not in env
    assert env.get("PATH") == "/usr/bin"                      # ...but pip can still find things
    assert env.get("PIP_INDEX_URL") == "https://user:tok@pypi.internal/simple"   # ...and its index
