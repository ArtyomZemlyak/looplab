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


@pytest.fixture(autouse=True)
def _no_dotenv_in_tests(monkeypatch):
    monkeypatch.setitem(Settings.model_config, "env_file", None)


@pytest.fixture(autouse=True)
def _no_repo_dotenv_in_tests(monkeypatch):
    """`Settings.model_config["env_file"]` stopped being the only dotenv reader.

    Connection profiles (`core/llm.py::_ambient_shared_pair`) and the UI secret store
    (`serve/settings_store.py::_dotenv_values`) call `dotenv_values(".env")` DIRECTLY, so the
    fixture above — which only reaches the pydantic loader — no longer insulates them. The suite
    runs from the repo root, so a developer's real `.env` becomes a live credential source: ~50
    tests fail with "LLM credential is bound to a different endpoint; refusing to construct
    transport", and `test_secret_settings` prints the real API key into its assertion diff.

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

    monkeypatch.setattr(dotenv, "dotenv_values", _guarded)


@pytest.fixture(autouse=True)
def _isolate_looplab_home(monkeypatch, tmp_path):
    """Cross-run memory and the knowledge base are ON BY DEFAULT — they point at the developer's real
    `~/.looplab`. Left alone, every engine test would read and write there (polluting real memory, and
    on a slow/locking FUSE mount even hanging on the append). Point both at a per-test tmp dir. Set via
    the environment, not just field defaults, so it also reaches subprocess-based tests (which spawn a
    fresh `looplab` process that reads the real `.env`) through their inherited environment."""
    home = tmp_path / "_ll_home"
    monkeypatch.setenv("LOOPLAB_MEMORY_DIR", str(home / "memory"))
    monkeypatch.setenv("LOOPLAB_KNOWLEDGE_DIR", str(home / "knowledge"))


@pytest.fixture(autouse=True)
def _isolate_host_gpu_pool_lease(monkeypatch, tmp_path):
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
        monkeypatch.setattr(module, "default_gpu_host_lease_path", lambda _p=lease: _p,
                            raising=False)
