"""Best-effort hardware / runtime-capability detection for HONEST prompt-building.

A task brief must not claim "CPU only, no GPU" on a GPU box, nor "only numpy/pandas/scikit-learn"
when `auto_install_deps` (deps.py) will pip-install torch/xgboost/etc. on first import. A wrong
brief makes the agent downgrade a neural-net idea (tree_dim/num_layers) into a tree model. This
module supplies the capability sentence those briefs should use — gated so it's only ever emitted
for tasks that actually support it (see `task_runtime_caps`).
"""
from __future__ import annotations

import inspect
import shutil
import subprocess

_GPU_CACHE: "tuple[bool, str | None] | None" = None


def detect_gpu() -> str | None:
    """The first GPU's name via `nvidia-smi`, or None if none/undetectable. Cached for the process.
    Deliberately NO torch dependency — torch may not be installed yet (it's auto-installed on demand),
    so importing it here would either fail or trigger a heavy import just to probe the device."""
    global _GPU_CACHE
    if _GPU_CACHE is not None:
        return _GPU_CACHE[1]
    name: str | None = None
    try:
        exe = shutil.which("nvidia-smi")
        if exe:
            out = subprocess.run([exe, "--query-gpu=name", "--format=csv,noheader"],
                                 capture_output=True, text=True, timeout=5)
            first = (out.stdout or "").strip().splitlines()
            if first and first[0].strip():
                name = first[0].strip()
    except (OSError, ValueError, subprocess.SubprocessError):
        name = None
    _GPU_CACHE = (True, name)
    return name


def runtime_capabilities_brief(*, auto_install: bool, gpu: str | None = None) -> str:
    """The 'what you may use' sentence for a task brief, honest about libraries + hardware.

    `auto_install` True  -> the engine pip-installs missing packages, so deep-learning / boosting
                            frameworks are fair game and the agent should build the model the idea
                            actually calls for instead of forcing sklearn.
    `auto_install` False -> the conservative legacy contract (only the pre-installed stack)."""
    if not auto_install:
        return ("You may use numpy, pandas and scikit-learn (all installed) plus the Python "
                "standard library; CPU only, no GPU/network.")
    hw = (f"a GPU is available ({gpu}); use it when your framework supports it (e.g. torch.cuda)"
          if gpu else "no GPU detected, so assume CPU")
    return ("You may use numpy, pandas and scikit-learn AND deep-learning / gradient-boosting "
            "frameworks (torch, xgboost, lightgbm, catboost): any package you import that isn't "
            "installed is auto-installed and the run retried, so build the model the idea actually "
            "calls for (e.g. a real neural network with the proposed architecture) rather than "
            f"downgrading it to sklearn just to avoid an import. Hardware: {hw}. No internet for "
            "downloading data, but missing Python packages are installed for you.")


def task_runtime_caps(task, *, auto_install: bool, gpu: str | None) -> str | None:
    """The capability sentence for THIS task, or None when the task is locked to the offline stack.

    Task-aware on purpose: synthetic/tutorial tasks (CodeRegressionTask, the offline MLEBenchTask)
    genuinely run with only numpy+stdlib, so they must NEVER be told torch is available — even when
    the engine flag is on. The opt-in signal is whether the task's `llm_roles` accepts a
    `runtime_caps` kwarg; a task that doesn't is treated as locked and gets None (conservative)."""
    roles = getattr(task, "llm_roles", None)
    if not callable(roles):
        return None
    try:
        if "runtime_caps" not in inspect.signature(roles).parameters:
            return None
    except (TypeError, ValueError):
        return None
    return runtime_capabilities_brief(auto_install=auto_install, gpu=gpu)
