"""The staleness check compares a proxy process to the file IT RUNS, not to one guessed from a root.

WHAT THIS IS ABOUT, measured 2026-09-18 on the restored bench stand. `check_money.sh` printed

    УСТАРЕВШИЙ ПРОКСИ: pid=250010 порт=8801 стартовал на 0.5 ч РАНЬШЕ последней правки proxy.py
    — его числа считает код, которого в дереве нет

about a meter that was an hour NEWER than its code. There are two checkouts on this box -- the
repository and the stand under `$BENCH_ROOT` -- the meter runs from the repository, where
`proxy.py` last changed at 06:03, and the check took the mtime of the STAND's copy, which a
`git reset --hard` had touched at 07:40. Two trees, and it compared against the wrong one: the same
confusion between two copies of one file that cost four probe launches the same morning
(`test_the_probe_refuses_a_stand_that_cannot_run.py`).

A false alarm here is not harmless. The check exists because a proxy four days older than its code
counted money nobody could reproduce (docs/53 §9), and an operator who has learned that this line
lies is an operator who will scroll past the true one.

The path is in the process's own argv, which is the one place it is not guessed.
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from _posix_gates import BASH_HARNESS

# Its subject is the bash bench harness: see tests/_posix_gates.py::BASH_HARNESS.
pytestmark = BASH_HARNESS

ROOT = Path(__file__).resolve().parents[1]
CHECK_MONEY = ROOT / "benchmarks" / "algotune" / "check_money.sh"

STUB = "import sys, time\ntime.sleep(300)\n"


def _stand(tmp: Path) -> Path:
    """A bench root with a meter ledger and its OWN `proxy.py` copy -- the one that must not be read."""
    root = tmp / "stand"
    (root / "meter").mkdir(parents=True)
    (root / "meter" / "meter.jsonl").write_text("", encoding="utf-8")
    other = root / "looplab" / "benchmarks" / "meter"
    other.mkdir(parents=True)
    (other / "proxy.py").write_text("# the stand's copy\n" + STUB, encoding="utf-8")
    return root


def _run(root: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ, ROOT=str(root))
    env.pop("PROXY_SRC_OVERRIDE", None)
    return subprocess.run(["bash", str(CHECK_MONEY), "3"], capture_output=True, text=True,
                          timeout=180, env=env)


def _proxy_from(where: Path):
    """Start a real process whose argv names `where/proxy.py`, the way the meter's does."""
    where.mkdir(parents=True, exist_ok=True)
    src = where / "proxy.py"
    src.write_text(STUB, encoding="utf-8")
    proc = subprocess.Popen(["python3", str(src), "--port", "18899"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.5)                       # its /proc/<pid>/stat start time must be readable
    return proc, src


def test_a_proxy_newer_than_its_own_file_is_not_called_stale(tmp_path):
    """The 2026-09-18 false alarm, with both trees present and disagreeing."""
    root = _stand(tmp_path)
    repo = tmp_path / "repo" / "benchmarks" / "meter"
    proc, src = _proxy_from(repo)
    try:
        os.utime(src, (time.time() - 7200, time.time() - 7200))      # the repo's copy: two hours old
        os.utime(root / "looplab" / "benchmarks" / "meter" / "proxy.py", None)  # the stand's: now
        got = _run(root)
    finally:
        proc.kill(); proc.wait(timeout=30)
    assert f"pid={proc.pid}" not in got.stdout, (
        "a proxy newer than the file it runs was reported stale from another tree's mtime:\n"
        + got.stdout)
    assert "новее своего кода" in got.stdout, got.stdout + got.stderr


def test_a_proxy_older_than_its_own_file_is_still_caught(tmp_path):
    """And the guard itself survives: the docs/53 §9 case is what it is for."""
    root = _stand(tmp_path)
    repo = tmp_path / "repo" / "benchmarks" / "meter"
    proc, src = _proxy_from(repo)
    try:
        os.utime(src, None)                                          # edited AFTER it started
        got = _run(root)
    finally:
        proc.kill(); proc.wait(timeout=30)
    assert f"pid={proc.pid}" in got.stdout, got.stdout + got.stderr
    assert "УСТАРЕВШИЙ ПРОКСИ" in got.stdout


def test_the_refusal_names_the_file_it_judged(tmp_path):
    """Two checkouts is the normal state of this box, so "последней правки proxy.py" is ambiguous
    exactly where it matters. The line names the path, so the next operator can check it."""
    root = _stand(tmp_path)
    repo = tmp_path / "repo" / "benchmarks" / "meter"
    proc, src = _proxy_from(repo)
    try:
        os.utime(src, None)
        got = _run(root)
    finally:
        proc.kill(); proc.wait(timeout=30)
    assert str(src) in got.stdout, got.stdout
