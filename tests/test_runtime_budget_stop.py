"""The runtime's two paid callbacks let the operator's spend ceiling through — after the child is dead.

Review 2026-09-22, RTA-04 (ENG2-12, TAT-01). `runtime/` may not call a model, so the engine hands it
two PAID callbacks: the inter-stage checker (`check_fn`, called between stages by
`command_eval._run_stages`) and the one-shot deadline judge (`on_deadline`, called at a stage's wall
by `sandbox._granted_grace`). Both judges re-raise `BudgetExceeded`; both runtime call sites wrapped
them in a blind `except Exception` that turned the ceiling into "no concern" / "no grace" and let
the pipeline go on to its next paid call. The census could not see either site: `runtime/` was
outside its scope and neither callback name was a paid name.

What holds now, driven against real subprocesses where a process is involved:

* the stage check re-raises the ceiling — no later stage runs. Safe there by construction: the check
  runs BETWEEN stages, with no child alive. The eval driver's own terminal-first handling
  (`engine/evaluate.py::_land_terminal_before_ceiling`) already names "a stage check" among the paid
  steps whose stop it expects to propagate.
* the deadline judge's ceiling is NOT a naive re-raise, because it is asked with the child still
  running at its wall: `_tee_drain` kills the tree exactly as a judge that declined would, finishes
  its own drain, and only then lets the stop through — and `run_argv` removes a `docker run`
  container before it does. A raise from inside the wait loop would have leaked the process tree
  (and, under Docker, a container holding its GPU).
"""
from __future__ import annotations

import io
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from looplab.core.errors import BudgetExceeded
from looplab.runtime import sandbox as sb
from looplab.runtime.command_eval import _run_stages
from looplab.runtime.sandbox import _granted_grace, run_argv

from test_command_eval_run_split import METRIC, PRINTS_METRIC, _exec


def _ceiling(*_a, **_k):
    raise BudgetExceeded("LLM spend ceiling reached")


# ------------------------------------------------------------------ the inter-stage checker

def test_a_budget_stopped_stage_check_ends_the_pipeline_instead_of_passing_it(tmp_path):
    """MUTATION: delete the `except BudgetExceeded: raise` above `_run_stages`' checker handler ->
    the ceiling reads as "no concern" and the score stage runs (and pays for its own check)."""
    marker = tmp_path / "score_ran"
    score = [sys.executable, "-c", f"open({str(marker)!r}, 'w').write('x'); print('{{}}')"]
    with pytest.raises(BudgetExceeded):
        _run_stages([{"name": "train", "command": PRINTS_METRIC, "check": True},
                     {"name": "score", "command": score}],
                    _exec(tmp_path), timeout=30.0, start_stage=None, metric=METRIC,
                    eval_started=0.0, check_fn=_ceiling)
    assert not marker.exists(), "a stage ran after the spend ceiling stopped its checker"


def test_an_ordinary_checker_failure_still_never_fails_the_candidate(tmp_path):
    """The other half, unchanged: a checker that breaks is the checker's problem, not the node's."""
    def broken(_name, _tail):
        raise RuntimeError("checker is broken")

    run = _run_stages([{"name": "train", "command": PRINTS_METRIC, "check": True}],
                      _exec(tmp_path), timeout=30.0, start_stage=None, metric=METRIC,
                      eval_started=0.0, check_fn=broken)
    assert run.early is None


# ------------------------------------------------------------------ the deadline judge

def test_the_grace_clamp_lets_the_ceiling_through_and_nothing_else():
    """The truth-table row `test_deadline_grace.py` does not have: every NON-answer is still 0.0 —
    including a judge that raises — except the operator's ceiling."""
    with pytest.raises(BudgetExceeded):
        _granted_grace(_ceiling, "tail", 2.0)
    assert _granted_grace(lambda t: 1 / 0, "tail", 2.0) == 0.0


def _pid_recording_child(tmp_path) -> tuple[list[str], Path]:
    pid_file = tmp_path / "child.pid"
    script = tmp_path / "chatty.py"
    script.write_text(
        "import os, time\n"
        f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
        "for i in range(400):\n"
        "    print(f'step {i}/400', flush=True)\n"
        "    time.sleep(0.05)\n"
        "print('DONE')\n", encoding="utf-8")
    return [sys.executable, str(script)], pid_file


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:          # exists, owned by someone else — cannot be our child
        return True
    # A reaped child is gone; an unreaped zombie is dead for our purpose. Tell them apart.
    try:
        state = Path(f"/proc/{pid}/stat").read_text().split(")")[-1].split()[0]
    except OSError:
        return False
    return state != "Z"


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group semantics")
def test_a_budget_stopped_deadline_judge_kills_the_child_first_then_stops(tmp_path):
    """THE PROPERTY. The judge is asked with the child still running at its wall. The stop must come
    out of `run_argv` — and the child must already be dead when it does. MUTATION: re-raise from
    inside `_tee_drain`'s wait loop (the naive fix) -> the child is still alive here."""
    argv, pid_file = _pid_recording_child(tmp_path)
    asked = []

    def judge(tail):
        asked.append(tail)
        raise BudgetExceeded("LLM spend ceiling reached")

    t0 = time.monotonic()
    with pytest.raises(BudgetExceeded):
        run_argv(argv, str(tmp_path), 0.6, on_deadline=judge, deadline_grace_max_s=5.0)
    assert time.monotonic() - t0 < 12
    assert len(asked) == 1, "asked ONCE, exactly like a judge that declines"
    pid = int(pid_file.read_text())
    assert not _alive(pid), "the spend stop left the stage's process running past its wall"


def test_a_budget_stopped_deadline_judge_still_removes_the_docker_container(tmp_path, monkeypatch):
    """Killing the local `docker run` client does not stop the daemon-owned container — the timeout
    path's `docker rm -f <cid>` is the only thing that does, so the budget-stop path must run it too.
    Same fake-daemon shape as `test_sandbox_gate.py`'s cancel test."""
    cid = "c" * 64
    seen: dict = {"argv": None, "cleanup": None}

    class Proc:
        def __init__(self, argv, **_kwargs):
            self.stdout = io.BytesIO(b"")
            self.stderr = io.BytesIO(b"")
            self.returncode = None
            seen["argv"] = list(argv)
            Path(argv[argv.index("--cidfile") + 1]).write_text(cid, encoding="ascii")

        def wait(self, timeout=None):
            if self.returncode is None:
                time.sleep(min(timeout or 0.0, 0.05))
                raise subprocess.TimeoutExpired("docker", timeout)
            return self.returncode

    monkeypatch.setattr(sb.subprocess, "Popen", Proc)
    monkeypatch.setattr(sb, "_kill_tree", lambda proc: setattr(proc, "returncode", -9))

    def fake_run(argv, **_kwargs):
        seen["cleanup"] = list(argv)
        return type("Done", (), {"returncode": 0})()

    monkeypatch.setattr(sb.subprocess, "run", fake_run)
    with pytest.raises(BudgetExceeded):
        sb.run_argv(["docker", "run", "--rm", "image", "python", "solution.py"],
                    str(tmp_path), timeout=0.3, on_deadline=_ceiling, deadline_grace_max_s=5.0)
    assert "--cidfile" in seen["argv"]
    assert seen["cleanup"] == ["docker", "rm", "-f", cid]
    cidfile = Path(seen["argv"][seen["argv"].index("--cidfile") + 1])
    assert not cidfile.exists(), "the cidfile — the only cleanup handle — must not outlive the stop"
