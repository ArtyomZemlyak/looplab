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
