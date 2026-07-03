"""Shared pytest fixtures.

The engine reads a `.env` file from the CWD (see looplab.config.Settings), and the suite runs from
the repo root — which is exactly where a developer's real `.env` lives. Without insulation, those
values would leak into every `Settings()` built in a test and break default-asserting tests
(e.g. `Settings().max_parallel == 1`). Disable dotenv loading for the whole suite so tests see only
field defaults plus whatever a test sets explicitly via monkeypatch.
"""
from __future__ import annotations

import pytest

from looplab.config import Settings


@pytest.fixture(autouse=True)
def _no_dotenv_in_tests(monkeypatch):
    monkeypatch.setitem(Settings.model_config, "env_file", None)


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
