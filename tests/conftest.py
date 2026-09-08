"""Shared pytest fixtures.

The engine reads a `.env` file from the CWD (see looplab.core.config.Settings), and the suite runs from
the repo root — which is exactly where a developer's real `.env` lives. Without insulation, those
values would leak into every `Settings()` built in a test and break default-asserting tests
(e.g. `Settings().max_parallel == 1`). Disable dotenv loading for the whole suite so tests see only
field defaults plus whatever a test sets explicitly via monkeypatch.

The other class of fixture here bounds what a test may WAIT on. A test that waits without a bound
cannot fail — it can only hang, and a hang costs the whole run while naming no assertion:

    test calls subprocess.run(...)
              │
              ├── with an explicit timeout=  ──▶ used unchanged (setdefault, never assignment)
              │                                   a test driving timeout behaviour keeps its own
              └── with NO timeout            ──▶ _SUBPROCESS_WAIT_BOUND_S (900s, env-overridable)
                                                        │
                                    child exits in time ├──▶ normal result
                                    child wedges        └──▶ TimeoutExpired: ONE test fails, by
                                                             name, and run() kills the child so it
                                                             stops holding what the next test needs

    without the bound:  select() forever ──▶ no result, no name, the whole suite lost

MEASURED 2026-09-01: `test_otel_bridge.py::test_spans_become_real_recording_otel_spans` sat in
`communicate()` for 65 minutes with its child
already exited (the pytest process held both ends of its own capture pipe, so the read never saw
EOF). It passes in isolation in seconds. 78 subprocess waits then existed in tests/, 20 with a
timeout and 58 without. Bounding it here rather than at 58 call sites also means a NEW call site
cannot reintroduce the hang.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
import pathlib
import subprocess

import pytest

from looplab.core.config import Settings


@pytest.fixture
def _isolation_patch():
    """A `MonkeyPatch` the TEST cannot undo — the instance every autouse isolation below uses.

    `monkeypatch` is function-scoped, so the fixtures and the test body share ONE instance, and a
    test that calls `monkeypatch.undo()` to drop its own patch silently reverts every autouse
    isolation with it. Six tests in this suite call `undo()`, and the damage is invisible until
    something reads the environment AFTER the call: `test_concept_lens_durability.py:550` does, and
    the `make_app` two lines later saw this box's real `JUPYTERHUB_API_TOKEN`, decided it was a
    shared origin, failed the control plane closed and answered 401 — surfacing four frames away as
    `KeyError: 'generation'`.

    What that would have cost if it had landed on a different fixture is the reason this is a
    harness change and not six call-site fixes: `_isolate_looplab_home` keeps a minted UI token out
    of the developer's real `~/.looplab`, and `_isolate_host_gpu_pool_lease` keeps the suite off the
    REAL host GPU lease — which on this box is held by a live training run, so a test that took it
    would block on that run and read as a hang. A per-fixture instance is torn down by the fixture
    itself and is unreachable from the test, so `undo()` means what its caller thinks it means.
    """
    patch = pytest.MonkeyPatch()
    yield patch
    patch.undo()


# A test that waits on a child with no bound cannot FAIL — it can only hang, and a hang costs the
# whole run while naming no assertion. MEASURED 2026-09-01:
# `test_otel_bridge.py::test_spans_become_real_recording_otel_spans` sat in
# `communicate()` for 65 minutes with its child already exited and both ends of the capture pipe
# held by the pytest process itself; the same file passes in isolation in seconds. An AST sweep of
# tests/ at that moment: 78 subprocess waits, 20 with `timeout=`, 58 without. Bounding it here
# rather than at 58 call sites means a NEW call site cannot reintroduce the hang either.
#
# Deliberately generous: this is a disaster bound, not a performance budget. The longest honest
# waits in this suite run a real engine over geesefs while a training run holds the box, and a
# bound that fires on those would be a harness that fails as its own defect — the exact thing it
# exists to prevent.
_SUBPROCESS_WAIT_BOUND_S = float(
    os.environ.get("LOOPLAB_TEST_SUBPROCESS_TIMEOUT_S", "900"))


@pytest.fixture(autouse=True)
def _bound_every_subprocess_wait(_isolation_patch):
    """Give every unbounded subprocess wait in the suite a timeout, preserving explicit ones.

    `subprocess.run` kills the child on `TimeoutExpired`, so the bound also releases whatever the
    wedged child was holding instead of leaving it for the next test to trip over.
    """
    real_run = subprocess.run
    real_communicate = subprocess.Popen.communicate

    def run(*args, **kwargs):
        kwargs.setdefault("timeout", _SUBPROCESS_WAIT_BOUND_S)
        return real_run(*args, **kwargs)

    def communicate(self, input=None, timeout=None):
        return real_communicate(
            self, input=input,
            timeout=_SUBPROCESS_WAIT_BOUND_S if timeout is None else timeout)

    _isolation_patch.setattr(subprocess, "run", run)
    _isolation_patch.setattr(subprocess.Popen, "communicate", communicate)


@pytest.fixture(autouse=True)
def _no_dotenv_in_tests(_isolation_patch):
    _isolation_patch.setitem(Settings.model_config, "env_file", None)


@pytest.fixture(autouse=True)
def _no_repo_dotenv_in_tests(_isolation_patch):
    """`Settings.model_config["env_file"]` stopped being the only dotenv reader.

    Connection profiles (`core/llm.py::_ambient_credential`, and `_ambient_shared_pair` over it) and
    the UI secret store (`serve/settings_store.py::_dotenv_values`) call `dotenv_values(".env")`
    DIRECTLY, so the fixture above — which only reaches the pydantic loader — no longer insulates
    them. The suite runs from the repo root, so a developer's real `.env` becomes a live credential
    source: ~50 tests fail with the mis-bound-credential refusal (the endpoint the test points at is
    not the one the developer's key is bound to), and `test_secret_settings` prints the real API key
    into its assertion diff.

    Neutralize the REPO-ROOT `.env` only. A test that chdirs to a tmp dir and writes its own
    `.env` (`test_dotenv_key_wins_over_stored_secret`) must still see it, so the guard compares
    resolved paths rather than blanking every call."""
    import dotenv

    repo_env = pathlib.Path(__file__).resolve().parent.parent / ".env"
    real_dotenv_values = dotenv.dotenv_values

    def _guarded(dotenv_path=".env", *args, **kwargs):
        try:
            if pathlib.Path(dotenv_path).resolve() == repo_env:
                return {}
        except OSError:  # unresolvable path is not the repo's .env
            pass
        return real_dotenv_values(dotenv_path, *args, **kwargs)

    _isolation_patch.setattr(dotenv, "dotenv_values", _guarded)


@pytest.fixture(scope="session")
def _session_isolation_patch():
    """The session-scoped twin of `_isolation_patch`, for the floor below."""
    patch = pytest.MonkeyPatch()
    yield patch
    patch.undo()


@pytest.fixture(autouse=True, scope="session")
def _isolate_looplab_home_for_the_whole_session(_session_isolation_patch, tmp_path_factory):
    """The FLOOR under `_isolate_looplab_home`, because a function-scoped fixture cannot be one.

    pytest instantiates higher-scoped fixtures FIRST, so every module- and session-scoped fixture in
    the suite runs BEFORE the per-test isolation below and therefore with the developer's real
    environment. Measured 2026-09-08: 25 such fixtures across 18 files, and
    `tests/test_phase_progress.py::offline_run` (module scope) runs a real `looplab run`, which took
    69 interprocess locks on the real `~/.looplab/memory` and WROTE to it — 80 synthetic
    `toy_quadratic` rows were sitting in this box's real `lessons.jsonl`, the same store a real run
    reads its cross-run priors and case library from. The GPU-lease and dotenv guards have the same
    hole for the same reason: a higher-scoped fixture that builds an LLM client reads the real
    `.env`, and a GPU-capable adapter in one takes the REAL host lease.

    This does not replace the per-test fixture — that one still gives each TEST its own directory,
    which is what keeps two tests from sharing a store. This one only has to make the DEFAULT
    unreachable for the whole process, so a fixture that runs outside the per-test window lands in a
    session tmp dir instead of the operator's home."""
    home = tmp_path_factory.mktemp("_ll_home_session")
    _session_isolation_patch.setenv("LOOPLAB_MEMORY_DIR", str(home / "memory"))
    _session_isolation_patch.setenv("LOOPLAB_KNOWLEDGE_DIR", str(home / "knowledge"))
    _session_isolation_patch.setenv("LOOPLAB_UI_TOKEN_FILE", str(home / "ui-token"))
    # Same floor for the host GPU-pool lease, and by the same mechanism its per-test sibling uses:
    # the path is a MODULE ATTRIBUTE, not an env var, and both bindings must be patched because
    # `orchestrator` imported the name directly. The lease is ONE file per OS user and exclusive
    # across processes, so a higher-scoped fixture taking the real one blocks on whatever else this
    # box is running and reads as a hang -- which is how it was misdiagnosed once already.
    from looplab.engine import orchestrator as _orch, resources as _res

    _session_lease = home / "gpu-pool.lock"
    for _module in (_res, _orch):
        _session_isolation_patch.setattr(_module, "default_gpu_host_lease_path",
                                         lambda _p=_session_lease: _p, raising=False)


@pytest.fixture(autouse=True)
def _isolate_looplab_home(_isolation_patch, tmp_path):
    """Cross-run memory and the knowledge base are ON BY DEFAULT — they point at the developer's real
    `~/.looplab`. Left alone, every engine test would read and write there (polluting real memory, and
    on a slow/locking FUSE mount even hanging on the append). Point both at a per-test tmp dir. Set via
    the environment, not just field defaults, so it also reaches subprocess-based tests (which spawn a
    fresh `looplab` process that reads the real `.env`) through their inherited environment."""
    home = tmp_path / "_ll_home"
    _isolation_patch.setenv("LOOPLAB_MEMORY_DIR", str(home / "memory"))
    _isolation_patch.setenv("LOOPLAB_KNOWLEDGE_DIR", str(home / "knowledge"))
    # `serve/owner_token.py` MINTS an owner credential into `~/.looplab/ui-token` when a shared-hub
    # server starts without one. Same insulation, same reason: a test must never write the
    # developer's real credential file, and two tests must not share one minted token.
    _isolation_patch.setenv("LOOPLAB_UI_TOKEN_FILE", str(home / "ui-token"))


@pytest.fixture(autouse=True)
def _isolate_shared_origin_detection(_isolation_patch):
    """`serve/engine_proc.py::_on_shared_hub` reads JupyterHub's own env, and the suite is routinely
    run INSIDE a JupyterHub single-user server — where those variables are set for the developer's
    shell, not for the deployment under test. That used to be invisible (the detection only chose a
    log line); since it also chooses whether an unset `LOOPLAB_UI_TOKEN` fails closed, an inherited
    `JUPYTERHUB_API_TOKEN` would silently token-gate every anonymous-mode server test on that box and
    leave them passing everywhere else. Tests that mean "shared hub" set the variable themselves."""
    for name in ("JUPYTERHUB_SERVICE_PREFIX", "JUPYTERHUB_API_TOKEN"):
        _isolation_patch.delenv(name, raising=False)
    # Same reason in both directions: a developer who exports LOOPLAB_UI_TOKEN would token-gate every
    # anonymous-mode server test, and a test that lets the server MINT one would otherwise leak that
    # credential into every later test in the process (`resolve_owner_token` exports what it mints).
    # Deleting it through monkeypatch restores whatever was there when the test ends.
    _isolation_patch.delenv("LOOPLAB_UI_TOKEN", raising=False)


@pytest.fixture(autouse=True)
def _isolate_host_gpu_pool_lease(_isolation_patch, tmp_path):
    """The host GPU-pool lease is ONE file per OS user (`/tmp/looplab-gpu-pool-<uid>.lock`) and is
    exclusive ACROSS PROCESSES by design. On a GPU box that makes it shared state between the suite
    and every other thing the developer is running: an Engine test that reserves a device blocks on a
    real training run's lease and waits — the suite stops at a fixed test count and reads as a hang,
    which is exactly how it was misdiagnosed. Point it at a per-test tmp file, the same insulation
    `_isolate_looplab_home` gives cross-run memory.

    Both bindings are patched: `resources` is the canonical home, and `orchestrator` imported the name
    directly, so patching only one leaves the Engine calling the real path. Tests that pass an
    explicit `lease_path` are untouched — they already own their file.

    Does NOT reach subprocess-based tests (a spawned `looplab` reads the real default). Those are safe
    for a different reason: the shipped offline/synthetic adapters now declare `gpu_capable() -> False`
    (`adapters/tasks.py`), so a toy/regression/mlebench CLI run never asks for the pool at all."""
    from looplab.engine import orchestrator, resources

    lease = tmp_path / "_ll_gpu_pool.lock"
    for module in (resources, orchestrator):
        _isolation_patch.setattr(module, "default_gpu_host_lease_path", lambda _p=lease: _p,
                                 raising=False)


@pytest.fixture(autouse=True)
def _stop_watch_schedulers_at_teardown(_isolation_patch):
    """A watch scheduler must not outlive the test that armed it.

    `serve/assistant_watch.py::WatchService` starts a daemon thread the moment an app is built over
    a store holding an armed watch, and NOTHING ever stops it: the thread keeps ticking every
    `interval_s` (2s) for the rest of the session, and each tick can run a whole wake-up turn —
    including `srv.make_llm_client`, which resolves `looplab.serve.server.make_llm_client` AT CALL
    TIME, on purpose, so one patch reaches every router (see `serve/appstate.py`). That late binding
    is what makes a leaked scheduler another test's problem: instrumenting `AppState.make_llm_client`
    across a third of the suite caught FOUR client constructions from `looplab-assistant-watch`
    threads landing inside three unrelated later tests, and two such threads were still alive at
    session end.

    A stray lands in whatever the test being run at that moment is counting. That is how
    `test_concept_lens_durability.py::test_recovery_fences_live_worker_then_wins_late_cross_process_terminal`
    read `[True, True] == [True]` in a full-suite run while passing alone, after its own file, and
    after every one of its alphabetical predecessors: it patches that global factory and then spends
    seconds restarting an app and resolving an orphan, which is a wide window for someone else's
    scheduler to construct a client in. NOT a recovery-fence defect — the fence is fine, and the
    at-most-once evidence beside that assertion (one claim, one terminal, one `llm_usage` in the
    run's own log) is untouched. Same class as the `monkeypatch.undo()` defect fixed in `edb416ba`:
    the harness losing its own insulation, not the feature under test misbehaving.

    Stopped at TEARDOWN, so a test that drives the scheduler still gets a live thread; `stop()` sets
    the event its loop waits on, so the loop exits before its next tick rather than running one more.
    """
    try:
        from looplab.serve import assistant_watch
    except Exception:      # the `[ui]` extra is optional; no server, no scheduler
        yield
        return

    started: list = []
    original = assistant_watch.WatchService.ensure_started

    def _tracked(self):
        started.append(self)
        return original(self)

    _isolation_patch.setattr(assistant_watch.WatchService, "ensure_started", _tracked)
    yield
    for service in started:
        try:
            service.stop()
        except Exception:  # noqa: BLE001 - teardown must never mask the test's own outcome
            pass


# --------------------------------------------------------------------------------------------
# WHY THERE IS NO FIXTURE HERE REDIRECTING `ALGOTUNE_BASELINE_CACHE_DIR`.
#
# 2026-09-07: eight `<task>__<subset>__lane2r3.json` entries appeared in the box's live
# `.baseline_times` -- 100 real timings each, a regime this box scores in neither way. The first
# answer was a session fixture pointing every test at a scratch copy. It cost two tests twice:
# `test_the_card_says_when_it_lost_its_timings` reads that directory ON PURPOSE, and its pair
# distinguishes "card with timings" from "card without" by building from two repo roots -- an
# environment variable that overrides the path makes those two cards identical either way.
#
# And it was not the fix. `/proc` named the writer: three ORPHANED `campaign.sh` processes running
# `premint_serial_rulers`, hours after the runs that started them were killed. That is closed at the
# source -- the pre-flight now mints only under `ALGOTUNE_PREMINT=1` -- and
# `test_the_live_cache_is_clean_and_in_one_regime` is what catches a recurrence. A second
# instrument that changes what the first measures is not a guard; it is the next defect.
