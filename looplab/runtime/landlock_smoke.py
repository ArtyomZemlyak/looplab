"""THE GPU SMOKE under a Landlock allow-list — the owner's check before `Settings.landlock` defaults
to `enforce` (BACKLOG #7 residue, doc 80 §9 item 9; prepared 2026-10-14, NOT flipped).

`Settings.landlock` ships `off` because nobody has run an allow-list through a real GPU eval: torch,
CUDA, NCCL, `/dev/nvidia*`, `/dev/shm`, `/sys/class` and the FUSE read surfaces were never exercised,
and a ruleset that misses one of them refuses mid-training. `looplab landlock-check RUN --gpu-smoke`
runs, under exactly the allow-list that run's evaluations would get (plus a scratch workdir, as
`runtime/sandbox.py::run_argv` adds the launch's own), a short program that touches each of those
surfaces in the EVAL's interpreter (`--python`, e.g. the task's conda env) and reports each one:

  import torch -> CUDA init + a matmul on one device -> a DataLoader with 2 worker processes (the
  `/dev/shm` shared-memory path) -> `torch.distributed` init over loopback (NCCL when CUDA is up,
  else gloo) -> `nvidia-smi`-free device reads under `/sys/class` and `/proc/driver/nvidia` ->
  a write in the workdir -> reads of every declared mount.

A step whose library is absent is `skipped` (with why), never `ok`. Any `failed` step is a surface
the ruleset would break: fix the derivation (`runtime/read_allowlist.py`) before the flip. The flip
itself, once a real train+score has also completed under `LOOPLAB_LANDLOCK=enforce`: change the
`landlock` default to `"enforce"` and add `"landlock": "off"` to
`core/config.py::LEGACY_CONFIG_SNAPSHOT_DEFAULTS` so every pre-flip run resumes unchanged.

Imports nothing above `core`.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from typing import Iterable, Optional

from looplab.runtime import landlock

STEPS = ("import_torch", "cuda_matmul", "dataloader_shm", "distributed_init", "sysfs_reads",
         "workdir_write", "mount_reads")

_PROGRAM = r'''
import json, os, sys, time
out = {}
mounts = json.loads(sys.argv[1])

def step(name, fn):
    try:
        val = fn()
        out[name] = {"status": "skipped", "why": val[1]} if isinstance(val, tuple) and val[0] == "skip" \
            else {"status": "ok", **({"detail": str(val)[:200]} if val is not None else {})}
    except BaseException as exc:  # report, never raise: the parent reads the verdict
        out[name] = {"status": "failed", "error": ("%s: %s" % (type(exc).__name__, exc))[:400]}

torch = None
def _import():
    global torch
    try:
        import torch as _t
    except ImportError as exc:
        return ("skip", "torch is not installed in this interpreter (%s)" % exc)
    torch = _t
    return _t.__version__
step("import_torch", _import)

def _cuda():
    if torch is None:
        return ("skip", "no torch")
    if not torch.cuda.is_available():
        return ("skip", "no CUDA device visible (CUDA_VISIBLE_DEVICES=%r)" % os.environ.get("CUDA_VISIBLE_DEVICES"))
    a = torch.randn(256, 256, device="cuda")
    return float((a @ a).sum().item())
step("cuda_matmul", _cuda)

def _loader():
    if torch is None:
        return ("skip", "no torch")
    from torch.utils.data import DataLoader, TensorDataset
    ds = TensorDataset(torch.arange(64, dtype=torch.float32).reshape(16, 4))
    return sum(float(b[0].sum()) for b in DataLoader(ds, batch_size=4, num_workers=2))
step("dataloader_shm", _loader)

def _dist():
    if torch is None:
        return ("skip", "no torch")
    import socket
    import torch.distributed as dist
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    os.environ.update(MASTER_ADDR="127.0.0.1", MASTER_PORT=str(port))
    backend = "nccl" if torch.cuda.is_available() else "gloo"
    dist.init_process_group(backend, rank=0, world_size=1)
    t = torch.ones(4, device="cuda" if backend == "nccl" else "cpu")
    dist.all_reduce(t)
    dist.destroy_process_group()
    return backend
step("distributed_init", _dist)

def _sysfs():
    seen = []
    for p in ("/sys/class", "/proc/driver/nvidia/gpus", "/proc/self/status"):
        if os.path.exists(p):
            if os.path.isdir(p):
                os.listdir(p)
            else:
                open(p).read(100)
            seen.append(p)
    return ",".join(seen)
step("sysfs_reads", _sysfs)

def _write():
    with open("landlock-smoke.txt", "w") as fh:
        fh.write("ok")
    return os.getcwd()
step("workdir_write", _write)

def _mounts():
    if not mounts:
        return ("skip", "the task declares no data:/references: mount")
    for p in mounts:
        if os.path.isdir(p):
            os.listdir(p)
        elif os.path.exists(p):
            open(p, "rb").read(16)
    return len(mounts)
step("mount_reads", _mounts)

print("@@LANDLOCK_SMOKE@@" + json.dumps(out))
'''


def run_smoke(allow: list, *, python: str, mounts: Iterable[str] = (),
              env: Optional[dict] = None, timeout: float = 300.0) -> dict:
    """Run the smoke program under `allow` (+ a scratch workdir) with `python`; returns
    `{steps: {name: {status, ...}}, exit_code, stderr_tail, ok}` where `ok` = no step FAILED."""
    with tempfile.TemporaryDirectory(prefix="looplab-landlock-smoke-") as wd:
        rules = list(allow) + [(wd, "readwrite")]
        argv = landlock.launch_argv(python, landlock.format_env(rules),
                                    [python, "-c", _PROGRAM, json.dumps(list(mounts))])
        child_env = dict(os.environ if env is None else env)
        try:
            proc = subprocess.run(argv, cwd=wd, env=child_env, capture_output=True, text=True,
                                  timeout=timeout)
        except subprocess.TimeoutExpired:
            return {"steps": {}, "exit_code": None, "ok": False,
                    "stderr_tail": f"timed out after {timeout:.0f}s"}
    steps: dict = {}
    for line in proc.stdout.splitlines():
        if line.startswith("@@LANDLOCK_SMOKE@@"):
            try:
                steps = json.loads(line[len("@@LANDLOCK_SMOKE@@"):])
            except ValueError:
                steps = {}
    ok = bool(steps) and proc.returncode == 0 and all(
        row.get("status") != "failed" for row in steps.values())
    return {"steps": steps, "exit_code": proc.returncode, "ok": ok,
            "stderr_tail": proc.stderr[-1500:]}


def mount_paths(mounts: Iterable) -> list[str]:
    return [os.path.expanduser(str(path)) for path, _mode in mounts
            if os.path.exists(os.path.expanduser(str(path)))]


__all__ = ["STEPS", "mount_paths", "run_smoke"]
