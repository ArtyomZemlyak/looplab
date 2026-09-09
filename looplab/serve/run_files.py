"""Shared serialization for mutable per-run snapshot files.

The two locks themselves moved DOWN to `looplab/engine/run_lifecycle.py` on 2026-09-08 (doc 25
XP-03): `run_config_write_lock` is one of the five `RunLifecycleFns` primitives a run-MUTATING agent
tool needs, and while it lived here `tools/` could only reach its own default by importing `serve/`
upward. This module stays as the historical import path — `serve/routers/*` and the CLI both name
it — and re-exports both names unchanged.
"""
from __future__ import annotations

from looplab.engine.run_lifecycle import (  # noqa: F401 - re-exported for the historical import path
    run_config_thread_lock, run_config_write_lock)

__all__ = ["run_config_thread_lock", "run_config_write_lock"]
