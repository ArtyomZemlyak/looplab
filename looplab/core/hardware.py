"""Best-effort hardware / runtime-capability detection for HONEST prompt-building.

A task brief must not claim "CPU only, no GPU" on a GPU box, nor "only numpy/pandas/scikit-learn"
when `auto_install_deps` (deps.py) will pip-install torch/xgboost/etc. on first import. A wrong
brief makes the agent downgrade a neural-net idea (tree_dim/num_layers) into a tree model. This
module supplies the capability sentence those briefs should use — gated so it's only ever emitted
for tasks that actually support it (see `task_runtime_caps`).
"""
from __future__ import annotations

import inspect
import os
import shutil
import subprocess

_GPU_CACHE: "tuple[bool, str | None] | None" = None
_GPUS_CACHE: "list[dict] | None" = None


def detect_gpus() -> list[dict]:
    """All visible GPUs as [{index, name, mem_total_mib, mem_free_mib}], best-effort via nvidia-smi
    (no torch dependency — torch may be auto-installed later). Empty list when none/undetectable.
    Cached for the process. This is the richer counterpart of `detect_gpu()` (which returns only the
    first GPU's name, kept for back-compat)."""
    global _GPUS_CACHE
    if _GPUS_CACHE is not None:
        return _GPUS_CACHE
    gpus: list[dict] = []
    try:
        exe = shutil.which("nvidia-smi")
        if exe:
            out = subprocess.run(
                [exe, "--query-gpu=index,name,memory.total,memory.free",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5)
            for line in (out.stdout or "").strip().splitlines():
                parts = [p.strip() for p in line.split(",")]
                if len(parts) >= 4:
                    def _int(x):
                        try:
                            return int(float(x))
                        except (ValueError, TypeError):
                            return None
                    gpus.append({"index": _int(parts[0]), "name": parts[1],
                                 "mem_total_mib": _int(parts[2]), "mem_free_mib": _int(parts[3])})
    except (OSError, ValueError, subprocess.SubprocessError):
        gpus = []
    _GPUS_CACHE = gpus
    return gpus


def usable_cpu_count() -> int:
    """Usable CPU cores respecting the cgroup cpuset (sched_getaffinity), falling back to cpu_count.
    This is the number an eval's thread pools are (and should be) sized against."""
    try:
        return len(os.sched_getaffinity(0))   # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return os.cpu_count() or 1


def _fmt_gib(mib) -> str:
    return f"{round(mib/1024)} GB" if isinstance(mib, int) else "? GB"


def gpu_summary() -> str:
    """One-line human summary of GPUs, e.g. '2 GPU(s): NVIDIA H200 (143 GB), NVIDIA H200 (143 GB)'."""
    gpus = detect_gpus()
    if not gpus:
        return "0 GPUs (CPU only)"
    parts = [f"{g.get('name','GPU')} ({_fmt_gib(g.get('mem_total_mib'))})" for g in gpus]
    return f"{len(gpus)} GPU(s): " + ", ".join(parts)


def environment_brief() -> str:
    """A concise, HONEST hardware line for agent prompts: usable CPU cores + GPU inventory. Callers
    combine this with repo/data-size notes as needed."""
    return f"Hardware: {usable_cpu_count()} usable CPU cores; {gpu_summary()}."


def operational_attention_points(*, include_env: bool = True) -> str:
    """The shared 'be environment-aware' block appended to every planning/coding agent's system
    prompt (Genesis, Boss, Researcher, Developer, Strategist). CUES, not rules — the agent adapts
    them to the task. Starts with the live hardware line so decisions (GPU count, batch size,
    parallelism) are grounded in what's actually available. Kept in ONE place so all agents share
    the same operational awareness."""
    head = (environment_brief() + "\n") if include_env else ""
    return head + (
        "Operational attention points (consider these and adapt to the task — they are cues, not "
        "rigid rules):\n"
        "- HARDWARE: check the CPU/GPU actually available (via a gpu_info tool when you have one, "
        "else nvidia-smi) and plan experiments, "
        "parallelism, batch sizes and precision from it. By DEFAULT use ALL available GPUs (e.g. "
        "`--gpus <N>` / DataParallel/DDP for N GPUs) unless the task says otherwise; don't leave GPUs "
        "idle or run a tiny single-GPU job on a multi-GPU box without reason.\n"
        "- REPO/DATA SIZE: before copying or seeding, check how big the repo and datasets are. Copy "
        "only what's needed (usually the source code); never deep-copy multi-GB artifacts (model "
        "checkpoints, datasets) into a workspace — reference large read-only inputs by absolute path "
        "instead. If a tree is unexpectedly huge or you're unsure what to copy, ASK rather than "
        "blindly copy.\n"
        "- DEPENDENCIES: analyze the environment first; do NOT (re)install packages needlessly. "
        "Some roles cannot install at all — then work with what is installed; when you CAN install, "
        "install only what's genuinely missing or what the user asked for, and prefer a ONE-TIME "
        "install into the shared interpreter when deps are stable across experiments (vs reinstalling "
        "before every run). Note a venv may be impossible on some mounts (e.g. s3fs) — install "
        "directly when that's the case.\n"
        "- REUSE over REIMPLEMENT: prefer orchestrating the repo's existing, working scripts (via "
        "subprocess/import) to rewriting data loading, models or training from scratch — custom data "
        "formats (pickled classes) usually only load with the repo's own code.\n"
        "- MATCH THE SCRIPT'S CONTRACT EXACTLY: when driving an existing script, match the exact names "
        "and labels it keys on — not just flag names+types but the STRINGS it looks up. E.g. a "
        "checkpoint/early-stop that monitors `val/<metric>` needs the validation split named so the "
        "script logs `val/<metric>` (naming it 'test' makes `test/<metric>` and the monitor fails); a "
        "metric you read back from a filename/results file must use that exact key. When an error says "
        "a key/monitor was 'not found in the returned metrics: [...]', rename YOUR argument to match "
        "one of the listed keys.\n"
        "- HYPERPARAMETERS/BUDGET: when the task doesn't pin them, estimate sane values from the "
        "model size, available GPU memory, and any documented recipe; keep each experiment within the "
        "eval timeout; use ABSOLUTE paths for inputs outside the repo.\n"
        "- EXPENSIVE-STEP REUSE: split the eval into stages (train / score) via the stage manifest "
        "so a cheap late-stage failure never repeats training — the ENGINE re-runs only what changed "
        "and reuses the completed train stage's artifact. Write each artifact to a stable path inside "
        "the eval workdir; do NOT hand-roll 'skip if output exists' checks (a partial or foreign "
        "artifact silently freezes the result); never load artifacts your pipeline didn't produce.\n"
        "- ENVIRONMENT-FIRST: inspect the interpreter, installed packages and paths before assuming; "
        "when a sensitive choice is ambiguous (what to copy, how much to install, how long to train), "
        "prefer the cheap/safe option and surface the question.")


def path_size_note(path: str, *, timeout: float = 8.0) -> str:
    """A bounded, best-effort size note for a path — so a prompt can warn against copying a huge
    tree without ever hanging on a multi-GB/geesefs dir. Returns a short human string; on a slow
    filesystem the measurement is abandoned and reported as 'very large (measurement timed out)'.
    For a git working tree, also notes tracked vs untracked so 'seed only code' is an obvious call."""
    from pathlib import Path as _P
    p = _P(os.path.expanduser(str(path)))
    if not p.exists():
        return f"{path}: (missing)"
    try:
        out = subprocess.run(["du", "-sb", str(p)], capture_output=True, text=True, timeout=timeout)
        if out.returncode == 0 and out.stdout.strip():
            nbytes = int(out.stdout.split()[0])
            human = _human_bytes(nbytes)
        else:
            human = "?"
    except subprocess.TimeoutExpired:
        return f"{path}: very large (measurement timed out — treat as huge)"
    except (OSError, ValueError, subprocess.SubprocessError):
        human = "?"
    note = f"{path}: {human}"
    # git-tracked footprint (fast) so 'code is tiny, artifacts are huge' is visible.
    try:
        g = subprocess.run(["git", "-C", str(p if p.is_dir() else p.parent), "ls-files", "-z"],
                           capture_output=True, text=True, timeout=timeout)
        if g.returncode == 0:
            ntracked = sum(1 for x in g.stdout.split("\0") if x)
            if ntracked:
                note += f" ({ntracked} git-tracked files; untracked content is NOT source)"
    except (OSError, subprocess.SubprocessError):
        pass
    return note


def _human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


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
