"""Shared pytest fixtures.

The engine reads a `.env` file from the CWD (see looplab.core.config.Settings), and the suite runs from
the repo root — which is exactly where a developer's real `.env` lives. Without insulation, those
values would leak into every `Settings()` built in a test and break default-asserting tests
(e.g. `Settings().max_parallel == 1`). Disable dotenv loading for the whole suite so tests see only
field defaults plus whatever a test sets explicitly via monkeypatch.
"""
from __future__ import annotations

import pathlib

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
