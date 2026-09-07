"""The probe must score what the LOOP called best, not what the filesystem wrote last.

THE DEFECT, measured. `run_model_probe.sh` selected its champion with

    CH=$(ls -t "$OUT/runs/$TASK/run"/nodes/*/solver.py 2>/dev/null | head -1)

— the most RECENT `solver.py`, which is not the best one. On the `convex_hull` probe of 2026-08-27
node 0 held a train score of 3.7777 and node 1 held 2.7342, and `ls -t` returned node 1 because it
was written later (06:00:53 against 04:30:53). The test pass measured that one: 2.7829. The real
champion was never scored at all, and the number was reported as the probe's result for a day.

It also copied ONE file, so a champion that committed a `.pyx` and a `setup.py` arrived at the
scoring pass without them — the defect
`tests/test_extract_champion_ships_the_whole_submission.py` exists for, worth 261.1071 against a
`solver_unloadable` 0.0 on the live corpus.

`run_probe.sh` was repaired for both and `run_model_probe.sh` was not, and the two were copies of
one script with identical defaults. Worse, the repaired file's own usage line named the broken one,
so an operator reading the fix ran the defect. The repair is therefore not a third copy of the rule
but a redirection: `run_model_probe.sh` now `exec`s `run_probe.sh`.

WHAT IS DRIVEN HERE. The probe's real champion-selection block, extracted from the real script and
RUN over a run log whose newest node is not its best; and the real shim, executed, with a stub
sibling that records what it was handed. Nothing in this file matches source text for its verdict.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "benchmarks" / "algotune" / "run_probe.sh"
SHIM = ROOT / "benchmarks" / "algotune" / "run_model_probe.sh"

_BLOCK_START = "# ЧЕМПИОНА ВЫБИРАЕТ СВЁРТКА СОБЫТИЙ"
_BLOCK_END = '\nsay "чемпион:'


def _champion_block() -> str:
    """The probe's own champion selection, verbatim, between two of its own landmarks."""
    src = PROBE.read_text(encoding="utf-8")
    start = src.find(_BLOCK_START)
    assert start != -1, f"run_probe.sh no longer carries {_BLOCK_START!r}"
    end = src.find(_BLOCK_END, start)
    assert end != -1, "run_probe.sh no longer says which champion it picked"
    block = src[start:end]
    assert "extract_champion.py" in block, (
        "the probe picks its champion without asking the event fold — this is the `ls -t` defect")
    return block


def _two_nodes(run: Path) -> None:
    """The convex_hull shape: the BEST node written FIRST, a worse one written after it.

    Both nodes commit a multi-file submission, so a single-file copy loses the extension too.
    """
    def files(tag: str) -> dict:
        return {"solver.py": f"# {tag}\nfrom _ext import go\n\n\nclass Solver:\n"
                             f"    def solve(self, p, **k):\n        return go(p)\n",
                "_ext.pyx": f"# cython: language_level=3  # {tag}\ncpdef double go(list a):\n"
                            f"    return 1.0\n",
                "setup.py": "from setuptools import setup\nsetup()\n"}

    events = [
        {"v": 1, "seq": 0, "ts": 1.0, "type": "run_started",
         "data": {"run_id": "r", "task_id": "t", "goal": "g", "direction": "max"}},
        {"v": 1, "seq": 1, "ts": 2.0, "type": "node_created",
         "data": {"node_id": 0, "parent_ids": [], "operator": "draft", "files": files("best"),
                  "idea": {"operator": "draft", "params": {}, "rationale": "r"}}},
        {"v": 1, "seq": 2, "ts": 3.0, "type": "node_evaluated",
         "data": {"node_id": 0, "metric": 3.7777}},
        {"v": 1, "seq": 3, "ts": 4.0, "type": "node_created",
         "data": {"node_id": 1, "parent_ids": [0], "operator": "refine", "files": files("newer"),
                  "idea": {"operator": "refine", "params": {}, "rationale": "r"}}},
        {"v": 1, "seq": 4, "ts": 5.0, "type": "node_evaluated",
         "data": {"node_id": 1, "metric": 2.7342}},
    ]
    run.mkdir(parents=True, exist_ok=True)
    (run / "events.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n",
                                      encoding="utf-8")
    # The on-disk node trees the old selector globbed, with the WORSE one newer — the exact
    # ordering that made `ls -t` wrong (06:00:53 against 04:30:53).
    for node, tag, mtime in ((0, "best", 1_700_000_000), (1, "newer", 1_700_005_000)):
        d = run / "nodes" / str(node)
        d.mkdir(parents=True, exist_ok=True)
        for name, body in files(tag).items():
            (d / name).write_text(body, encoding="utf-8")
            os.utime(d / name, (mtime, mtime))


def _stand(tmp: Path) -> Path:
    """`$ROOT` as the probe means it: a directory holding a `looplab` checkout."""
    (tmp / "looplab").symlink_to(ROOT)
    return tmp


def _bash(script: str, tmp: Path) -> subprocess.CompletedProcess:
    # The block calls bare `python`; pin it to this interpreter so the test scores the script and
    # not the PATH it happened to run under.
    shim = tmp / "bin"
    shim.mkdir(exist_ok=True)
    (shim / "python").write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', encoding="utf-8")
    (shim / "python").chmod(0o755)
    env = dict(os.environ, PATH=f"{shim}{os.pathsep}{os.environ.get('PATH', '')}")
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=180,
                          cwd=str(tmp), env=env)


def _pick(tmp: Path) -> subprocess.CompletedProcess:
    stand = _stand(tmp)
    out = tmp / "out"
    (out / "runs" / "demo").mkdir(parents=True)
    _two_nodes(out / "runs" / "demo" / "run")
    script = (f'set -u\nROOT="{stand}"\nOUT="{out}"\nTASK=demo\nLOG="{tmp}/probe.log"\n'
              f'say() {{ echo "$*"; }}\n'
              + _champion_block()
              + '\necho "CH=${CH}"\n')
    return _bash(script, tmp)


def test_the_probe_scores_the_best_node_and_not_the_newest(tmp_path):
    """The falsifier for `ls -t`: node 1 is newer, node 0 is the champion."""
    got = _pick(tmp_path)
    assert got.returncode == 0, got.stdout + got.stderr
    champion = tmp_path / "out" / "champion_solver.py"
    assert f"CH={champion}" in got.stdout, got.stdout + got.stderr
    assert champion.exists(), (got.stdout, got.stderr,
                               (tmp_path / "probe.log").read_text(errors="replace"))
    body = champion.read_text(encoding="utf-8")
    assert "# best" in body, f"the probe extracted the LATER node, not the best one: {body!r}"


def test_the_whole_submission_travels_with_it(tmp_path):
    """`--all-files`: a compiled champion delivered without its extension scores 0.0 as
    `solver_unloadable`, which reads like a broken solver rather than a harness that shipped half
    of one."""
    got = _pick(tmp_path)
    assert got.returncode == 0, got.stdout + got.stderr
    beside = {p.name for p in (tmp_path / "out").iterdir() if p.is_file()}
    assert {"champion_solver.py", "_ext.pyx", "setup.py"} <= beside, beside


def _shim_body() -> str:
    """The file, checked to BE a shim before anybody runs it.

    THIS CHECK IS A SAFETY INTERLOCK AND NOT A STYLE ASSERTION. The pre-repair `run_model_probe.sh`
    hardcodes `ROOT=/var/tmp/looplab-bench` -- the live stand -- with no environment override, and
    it makes directories under `$ROOT/model-probes/`, touches the fence and opens a run directory
    before any of its own refusals can fire. So a test that COPIES this file and executes it is
    safe only while the file is one `exec` line, and the moment somebody restores the old body to
    watch this test go red, the test itself runs the old body against the live stand.

    That is not hypothetical: it happened on 2026-08-31 at 09:56 during exactly that mutation. It
    cost no money (the run refused in a second for want of a credential pair) but it wrote a
    directory into the live `model-probes/`, left an `engine.lock`, and the 10:09 snapshot carried
    that empty directory into the archive that is supposed to hold measurements only.

    So the shape is asserted FIRST and the execution below only ever reaches a shim. A mutation now
    reddens this file without touching anything outside `tmp_path`.
    """
    body = SHIM.read_text(encoding="utf-8")
    assert body.startswith("#!"), body[:80]
    live = [ln for ln in body.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    assert len(live) == 1, f"NOT A SHIM, and it is not safe to execute a copy of it: {live}"
    assert live[0].startswith("exec "), live
    assert "run_probe.sh" in live[0], live
    assert "/var/tmp/looplab-bench" not in body, (
        "the shim names the live stand; a copy of it must never be executed by a test")
    return body


def test_the_old_name_runs_the_repaired_script(tmp_path):
    """`run_model_probe.sh` must be a redirection and not a second copy of the rule.

    Driven with a stub sibling: the shim is copied beside a `run_probe.sh` that only records its
    argv, so this asserts what the old name actually EXECUTES and what it hands over — including
    the optional task, meter and budget arguments the old copy never had.
    """
    body = _shim_body()          # the interlock: nothing is executed unless it IS the shim
    here = tmp_path / "algotune"
    here.mkdir()
    (here / "run_model_probe.sh").write_text(body, encoding="utf-8")
    (here / "run_model_probe.sh").chmod(0o755)
    recorded = tmp_path / "argv.txt"
    (here / "run_probe.sh").write_text(
        f'#!/bin/bash\nprintf "%s\\n" "$@" > "{recorded}"\necho DELEGATED\n', encoding="utf-8")
    (here / "run_probe.sh").chmod(0o755)
    args = ["z-ai/glm-5.3-flash", "glm53f", "0-21", "convex_hull", "http://127.0.0.1:8802", "1.00"]
    got = subprocess.run(["bash", str(here / "run_model_probe.sh"), *args],
                         capture_output=True, text=True, timeout=60)
    assert got.returncode == 0, got.stdout + got.stderr
    assert "DELEGATED" in got.stdout, (
        "the old name still runs a body of its own", got.stdout, got.stderr)
    assert recorded.read_text(encoding="utf-8").splitlines() == args, recorded.read_text()


def test_the_old_name_carries_no_second_implementation():
    """The other half of "one rule, one place": whatever the shim does, it must not select a
    champion, copy a solver or spend a budget on its own again. Same predicate the interlock uses,
    stated once."""
    _shim_body()
