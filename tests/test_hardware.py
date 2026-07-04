"""Honest runtime-capability brief + task-aware gating (no torch claim for offline tasks)."""
from __future__ import annotations

from looplab.core.hardware import runtime_capabilities_brief, task_runtime_caps


def test_caps_off_is_conservative():
    out = runtime_capabilities_brief(auto_install=False, gpu="RTX 5090")
    assert "scikit-learn" in out and "CPU only, no GPU/network" in out
    assert "torch" not in out            # locked stack: never advertise deep-learning frameworks


def test_caps_on_advertises_frameworks_and_gpu():
    out = runtime_capabilities_brief(auto_install=True, gpu="RTX 5090")
    assert "torch" in out and "xgboost" in out
    assert "RTX 5090" in out
    assert "auto-installed" in out
    assert "downgrading it to sklearn" in out   # the exact anti-pattern the bug exhibited


def test_caps_on_no_gpu_says_cpu():
    out = runtime_capabilities_brief(auto_install=True, gpu=None)
    assert "torch" in out and "no GPU detected" in out


class _CapableTask:
    def llm_roles(self, client, parser="tool_call", runtime_caps=None):
        return None, None


class _LockedTask:                      # offline/synthetic: llm_roles has no runtime_caps kwarg
    def llm_roles(self, client, parser="tool_call"):
        return None, None


def test_task_caps_gated_on_opt_in():
    # A task that accepts runtime_caps gets the sentence; one that doesn't is left locked (None),
    # so a synthetic numpy+stdlib task is never told torch is available even with the flag on.
    assert task_runtime_caps(_CapableTask(), auto_install=True, gpu="X") is not None
    assert task_runtime_caps(_LockedTask(), auto_install=True, gpu="X") is None


def test_task_caps_reflects_auto_install():
    capable = _CapableTask()
    assert "torch" in task_runtime_caps(capable, auto_install=True, gpu=None)
    assert "torch" not in task_runtime_caps(capable, auto_install=False, gpu=None)
