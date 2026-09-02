#!/usr/bin/env python3
"""Cheap per-instance CORRECTNESS check for the developer's writing sessions.

WHY. The score is zero unless every instance validates, and the writing sessions were checking the
wrong thing. Measured over three independent kcenters probes on 2026-08-28, of the agent's own
`run_probe` self-checks:

    probe        probes   compared to reference solve()   called is_solution()   timed something
    dsKcCtl        55                 3                           1                    23
    dsFBKc         42                 3                           1                    13
    fxKcenters     55                 3                           4                    28

It times things because timing is cheap and validating was not: the only correctness-checking
command pinned on the card is `eval_train`, a ~40 s full pass. dsKcCtl's node 1 is what that costs
-- it knowingly traded exactness for speed ("the decision oracle is monotone in r, so we can replace
most probes"), self-checked 55 times, and the engine's evaluation was the FIRST thing to say
`Solution is not optimal. Found value: 33.955, Optimal value: 33.408`. The node scored 0.0 and its
work was discarded, which at 2-4 nodes per run is a quarter of the budget.

The reference agent does not have this hole: `reference [1,2,3]` and `eval_input [1,2,3]` are
first-class commands, so its model never writes a validation harness by hand.

WHAT THIS IS NOT. It is not the ruler. It generates its OWN instances from the reference task's
generator and reports validity only -- no speedup, no baseline, nothing that could be confused with
`engine/evaluate.py`'s number. Timing that appears here is wall-clock convenience, not a score.
"""
from __future__ import annotations

import argparse
import importlib.util
import io
import json
import logging
import os
import re
import select
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

_REQUIRED = ("generate_problem", "solve", "is_solution")


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"looplab_check: cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def find_task(mod) -> Any:
    """The reference task, found by DUCK TYPING rather than `isinstance(..., Task)`.

    The arena's own base class is not importable everywhere this runs (and importing it would drag
    the whole harness in), so the rule is the three methods that matter. Deterministic: sorted by
    name, so a module holding two candidates always yields the same one.
    """
    best = None
    for attr in sorted(dir(mod)):
        obj = getattr(mod, attr)
        if not isinstance(obj, type):
            continue
        if all(callable(getattr(obj, m, None)) for m in _REQUIRED):
            if best is None or attr < best[0]:
                best = (attr, obj)
    if best is None:
        raise SystemExit("looplab_check: no class with generate_problem/solve/is_solution in the reference")
    return best[1]


def _one_instance(reference: Path, solver: Path, size: int, seed: int) -> dict:
    """Validate ONE instance. Runs in a forked child -- see `check` for why."""
    task_cls = find_task(_load_module(reference, "_looplab_reference"))
    try:
        task = task_cls()
    except TypeError:
        task = task_cls(n=size)
    # THE SUBMISSION'S OWN DIRECTORY GOES ON `sys.path`, BECAUSE THE GRADER PUTS IT THERE.
    #
    # `AlgoTune/scripts/evaluate_results.py:396` does `sys.path.insert(0, str(code_dir))` before it
    # imports the candidate, so a solver that says `from edge_cut import ...` -- a helper module or
    # a compiled extension beside it -- is scored fine. This checker did not, and answered
    # `ModuleNotFoundError: No module named 'edge_cut'` for every instance: 13 of the 480 `check`
    # calls in the corpus (2.7 %, seven probes), reported as INVALID INSTANCES.
    #
    # That is a FALSE RED, and its direction is what makes it expensive. remEEref9's champion is
    # exactly this shape and scores 218.85 on the graded split, while its own pre-flight command
    # was telling the model the solver was invalid. The model is being steered to rewrite working
    # code -- and `edit_surface` grants `*.pyx`/`*.pxd` precisely so it CAN write more than one
    # file. The mirror of the false GREEN in `build_gate` above, in the same command.
    sys.path.insert(0, str(solver.resolve().parent))
    solver_mod = _load_module(solver, "_looplab_candidate")
    solver_cls = getattr(solver_mod, "Solver", None)
    if solver_cls is None:
        return {"valid": False, "raised": f"{solver.name} defines no `Solver` class"}
    problem = task.generate_problem(size, random_seed=seed)
    # `is_solution` states its rejection through `logging.error`; capture it so the agent is told
    # WHY and not merely that something was wrong.
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setLevel(logging.ERROR)
    root = logging.getLogger()
    root.addHandler(handler)
    prev = root.level
    root.setLevel(logging.ERROR)
    t0 = time.perf_counter()
    try:
        answer = solver_cls().solve(problem)
        elapsed = time.perf_counter() - t0
        row = {"valid": bool(task.is_solution(problem, answer)), "seconds": round(elapsed, 4)}
    except Exception as exc:  # noqa: BLE001 — a candidate that raises is a FAILED instance
        row = {"valid": False, "seconds": round(time.perf_counter() - t0, 4),
               "raised": f"{type(exc).__name__}: {exc}"[:300]}
    finally:
        root.removeHandler(handler)
        root.setLevel(prev)
    why = " ".join(buf.getvalue().split())[:300]
    if why and not row["valid"]:
        row["reason"] = why
    return row


def build_gate(solver_dir: Path, timeout: float = 120.0) -> dict:
    """Compile the submission the way the EVALUATOR will, before validating anything.

    THE HOLE THIS CLOSES, measured over the whole probe corpus on 2026-09-02. Six runs ended on a
    node that scored 0; two of them (`remEE6` node 3, `remEEref6` node 2) died on a Cython
    `CompileError`, and BOTH had run this checker -- ten and six times -- with `"ok": true` every
    time. The reason is not that the model skipped the cheap command. It is that the cheap command
    was answering a different question:

        try:
            from edge_expansion_cy import edge_expansion_count   # never built here
        except ImportError:
            ...                                                  # <- what `check` validated

    `_run_isolated` imports `solver.py` in a child, the extension is absent, the guarded import
    falls through, and the checker certifies the PURE-PYTHON path. `looplab_eval.py` then runs
    `setup.py build_ext --inplace` (line 1067), the compile fails, and the node is graded 0 -- on a
    path the checker never touched. A green light on code the grader will not run is worse than no
    light: it is the last thing the model saw before spending its final draw.

    THE RULE IS IMPORTED, NOT RE-SPELLED. `build_decision` is the evaluator's own -- a `.pyx` with
    no `setup.py`/`pyproject.toml` is NOT compiled and the fallback IS what gets graded, which this
    reports rather than treats as an error -- and `_build_error_digest` is the evaluator's own too,
    so the model reads the compiler's line here in the same words it will read there. Two spellings
    of one rule is how the two commands come to disagree.

    IT IS CHEAP, WHICH IS WHY IT BELONGS ON THE CHEAP COMMAND. Measured on this box, 2026-09-02:
    the broken `edge_expansion_cy.pyx` fails in 0.67 s (Cython errors before the C compiler is
    reached), a healthy `edge_cut.pyx` compiles in 1.3 s, and an unchanged rebuild is 0.4 s --
    against this checker's own 3.6-9.1 s and the card's 120 s ceiling.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from looplab_eval import _build_error_digest, build_decision

    try:
        submitted = {f.name for f in solver_dir.iterdir() if f.is_file()}
    except OSError as exc:
        return {"ok": True, "note": f"not run: {type(exc).__name__}: {exc}"}
    run_build, skip_note = build_decision(submitted)
    if skip_note:
        # The evaluator grades the fallback in this case, so neither does this: it is a fact the
        # model needs, not a failure. Reported, never fatal.
        return {"ok": True, "note": skip_note}
    if not run_build:
        return {}
    cmd = [sys.executable, "setup.py", "build_ext", "--inplace"]
    try:
        built = subprocess.run(cmd, cwd=str(solver_dir), capture_output=True, text=True,
                               timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "note": f"timeout after {timeout:.0f}s"}
    except OSError as exc:
        return {"ok": True, "note": f"not run: {type(exc).__name__}: {exc}"}
    if built.returncode == 0:
        return {"ok": True, "note": "ok"}
    return {"ok": False,
            "note": f"failed rc={built.returncode}: {_build_error_digest(built.stderr)}"}


def check(reference: Path, solver: Path, n: int, size: int, seed: int, timeout: float = 30.0) -> dict:
    """Validate `n` instances, EACH IN ITS OWN CHILD PROCESS.

    WHY A CHILD AND NOT A `try`. Measured 2026-08-28 on dsKcCtl node 1 at the graded size: the
    candidate dies with `Fatal glibc error: malloc.c:4376 (_int_malloc): assertion failed` -- SIGABRT
    from native code under numpy/numba. `except Exception` cannot see that, so the first version of
    this checker was killed with it and printed NOTHING: the agent asked whether its solver was
    valid and got an empty answer at exactly the size that matters. A child turns a process-killing
    candidate into one failed instance with a named signal, and a hanging one into a timeout, and
    the other instances still get answered.
    """
    # A MISSING `Solver` IS A FILE-LEVEL FACT, reported ONCE. Asked per-instance it would come back
    # as n identical rows, which reads like n failures. Checked in the parent, before any fork.
    # OPEN[solver-check-requires-a-literal-class-statement] a valid solver that BINDS `Solver`
    # (import or assignment) is refused by this regex while the arena's loader accepts it.
    # proof:present:search(r"^\s*class@benchmarks/algotune/looplab_check.py
    # REVIEW 2026-08-30 (correctness): the arena resolves the module ATTRIBUTE — as
    # `_run_isolated`'s own `getattr(solver_mod, "Solver", None)` does one function up — so
    # `from impl import Solver` or `Solver = make_solver()` scores fine and this checker tells the
    # Developer its solver "defines no `Solver` class", steering a rewrite the grader never
    # required. Resolve the attribute (import the module the way `_run_isolated` does, or fall
    # back to the regex only as a fast pre-check that can acquit, never convict).
    if not re.search(r"^\s*class\s+Solver\b", solver.read_text(encoding="utf-8", errors="replace"), re.M):
        return {"ok": False, "error": f"{solver.name} defines no `Solver` class"}
    # BEFORE ANY INSTANCE, because a submission that cannot compile is graded 0 whatever the
    # instances say -- and because building it here is what makes the rows below describe the code
    # the evaluator will actually run. See `build_gate`.
    build = build_gate(solver.parent if str(solver.parent) else Path("."))
    if build and not build.get("ok"):
        # ONE copy of the digest, not two. A dev-command result is clipped to
        # `core/context_budget.py::RESULT_CAP` (4000 chars) and this digest runs to ~700, so
        # printing it under both `error` and `build_ext` spends a third of the model's window on
        # the same sentence twice.
        return {"ok": False, "error": f"build_ext {build['note']}",
                "note": "THE EXTENSION DID NOT COMPILE. `looplab_eval` runs the same "
                        "`setup.py build_ext --inplace` and grades the node 0 when it fails, so no "
                        "number this checker could print about the instances would be the one you "
                        "get. Fix the compile first."}
    rows = []
    for i in range(n):
        rows.append(_run_isolated(reference, solver, size, seed + i, timeout, i))
    n_valid = sum(1 for r in rows if r.get("valid"))
    out = {"ok": n_valid == len(rows), "instances": len(rows), "valid": n_valid,
            "invalid": len(rows) - n_valid, "size": size, "rows": rows,
            "note": ("ALL VALID on these instances. This is a correctness check on freshly generated "
                     "instances at the GRADED size, NOT the score -- the run's number still comes "
                     "from the engine's evaluation." if n_valid == len(rows) else
                     "INVALID INSTANCES PRESENT. The score is 0 unless every instance validates, so "
                     "fix this before optimising further.")}
    if build.get("note"):
        out["build_ext"] = build["note"]
    return out


def _run_isolated(reference: Path, solver: Path, size: int, seed: int, timeout: float, index: int) -> dict:
    """`_one_instance` in a forked child, so a SIGABRT or a hang is a ROW rather than the end."""
    base = {"instance": index, "seed": seed, "size": size}
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:                                        # child
        # NO CORE DUMPS. A candidate that dies by SIGSEGV/SIGABRT writes a core into the CWD, and the
        # CWD here is the agent's workspace. Measured 2026-08-28: one deliberate segfault produced a
        # 1.4 GB core in the repo root, and this checker's own test left 88 MB behind on EVERY run.
        # The signal is what this function reports; the dump is pure disk pressure inside a candidate
        # tree, so the child refuses to write one. Best-effort: a platform without RLIMIT_CORE just
        # keeps the old behaviour rather than failing the check.
        try:
            import resource
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        except Exception:                               # noqa: BLE001
            pass
        payload = {"valid": False, "raised": "child produced no verdict"}
        try:
            payload = _one_instance(reference, solver, size, seed)
        except BaseException as exc:                    # noqa: BLE001 — the child reports, never propagates
            payload = {"valid": False, "raised": f"{type(exc).__name__}: {exc}"[:300]}
        finally:
            try:
                os.write(write_fd, json.dumps(payload).encode("utf-8"))
                os.close(write_fd)
            except Exception:                           # noqa: BLE001
                pass
            os._exit(0)
    os.close(write_fd)
    deadline = time.time() + timeout
    chunks = []
    try:
        while True:
            ready, _, _ = select.select([read_fd], [], [], max(0.0, deadline - time.time()))
            if not ready:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
                return {**base, "valid": False, "seconds": round(timeout, 4),
                        "raised": f"TIMEOUT after {timeout:.0f}s -- the solver did not finish one "
                                  f"instance at the graded size"}
            block = os.read(read_fd, 65536)
            if not block:
                break
            chunks.append(block)
    finally:
        os.close(read_fd)
    _, status = os.waitpid(pid, 0)
    if chunks:
        try:
            return {**base, **json.loads(b"".join(chunks).decode("utf-8"))}
        except ValueError:
            pass
    if os.WIFSIGNALED(status):
        sig = os.WTERMSIG(status)
        name = signal.Signals(sig).name if sig in {s.value for s in signal.Signals} else str(sig)
        return {**base, "valid": False,
                "raised": f"the solver KILLED its process with {name} -- a native-code fault "
                          f"(malloc/segfault) that no Python `try` can catch"}
    return {**base, "valid": False, "raised": "the child exited without a verdict"}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Per-instance correctness check against the reference task.")
    ap.add_argument("--reference", required=True, type=Path)
    ap.add_argument("--solver", default=Path("solver.py"), type=Path)
    ap.add_argument("--n", type=int, default=3)
    ap.add_argument("--size", type=int, default=20,
                    help="instance size; make_task pins the GRADED n from the arena dataset name")
    # 30 s PER INSTANCE, and the arithmetic is the reason. The card pins this command at a 120 s
    # ceiling, so three instances at 60 s could be killed by their own runtime before printing
    # anything -- the exact failure this checker was just repaired for. 3 x 30 + ~10 s of setup
    # fits under 120 with room. Measured cost of a healthy call is far below it: 3.6 s for
    # edge_expansion at its graded n=4408, 9.1 s for kcenters at n=49.
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args(argv)
    if not args.reference.exists():
        print(json.dumps({"ok": False, "error": f"reference not found: {args.reference}"}))
        return 1
    if not args.solver.exists():
        print(json.dumps({"ok": False, "error": f"solver not found: {args.solver}"}))
        return 1
    print(json.dumps(check(args.reference, args.solver, args.n, args.size, args.seed,
                       args.timeout), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
