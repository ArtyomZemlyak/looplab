"""A campaign that switches a task to one worker must have that task's ruler already.

§321 taught `run_one` to score a CP-SAT task at `ALGOTUNE_EVAL_WORKERS=1`. On a box holding only
wide entries that run cannot score: `looplab_eval` refuses `baseline_regime_mismatch` (the serial
key is absent while a wide one is there), and with `ALGOTUNE_ALLOW_NEW_REGIME=1` it spends the first
evaluation BUILDING the ruler and returns `baseline_measured_in_pass` -- a node of a paid probe,
consumed by the denominator. Both were driven on this box.

So the pre-flight mints them, on the free lane, before an arm starts. Driven end to end once
against a wide-only scratch cache: `max_clique_cpsat__test__lane22r3.json` and its `train` sibling
appeared. The tests below drive the shipped function with `python3` shimmed, so the loop's
decisions -- which tasks, which subsets, what happens when a mint fails -- are checked in seconds
rather than in the twenty minutes four real timings cost.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "benchmarks" / "algotune" / "campaign.sh"

DRIVE = """set -e
export PATH="$SHIM:$PATH"
export REPO="{repo}"
export ALGOTUNE_BASELINE_CACHE_DIR="$CACHE"
export ALGOTUNE_TASKS_ROOT="$TASKS_ROOT"
export TASKS="{tasks}"
export PREMINT_LANE="0-1"
export ALGOTUNE_PREMINT="${{WANT_MINT:-1}}"   # doubled: this line lives in a .format() template
sed -n '/^scoring_workers() {{/,/^}}/p' "{script}" > "$WORK/a.sh"
sed -n '/^premint_serial_rulers() {{/,/^}}/p' "{script}" > "$WORK/b.sh"
. "$WORK/a.sh"; . "$WORK/b.sh"
premint_serial_rulers
"""


# The tasks these fixtures treat as CP-SAT (i.e. scored serially). `min_dominating_set` and
# `max_clique_cpsat` are two of the nine §314 measured; `pagerank` is deliberately not one, which is
# what `test_only_the_serially_scored_tasks_are_minted` is about.
_CPSAT_TASKS = {"min_dominating_set", "max_common_subgraph", "kcenters"}


def _run(tasks, cache_files=(), shim_writes=True, want_mint="1"):
    with tempfile.TemporaryDirectory() as tmp:
        work, cache, shim = Path(tmp) / "w", Path(tmp) / "c", Path(tmp) / "s"
        for d in (work, cache, shim):
            d.mkdir()
        # THE REFERENCE TREE, as a fixture. `scoring_workers` runs `ruler_check` in a SUBPROCESS, so
        # a monkeypatched module attribute cannot reach it and the function answered `?` for every
        # task on any box without the bench's AlgoTune checkout at `/var/tmp/looplab-bench` — the
        # loop then minted nothing, and these tests failed on an empty `out` rather than on what
        # they are about. `ALGOTUNE_TASKS_ROOT` is the env override `ruler_check.CPSAT_ROOT` reads.
        tasks_root = Path(tmp) / "AlgoTuneTasks"
        for task in tasks.split():
            (tasks_root / task).mkdir(parents=True)
            (tasks_root / task / f"{task}.py").write_text(
                "from ortools.sat.python import cp_model\n" if task.endswith("_cpsat")
                or task in _CPSAT_TASKS else "import numpy\n", encoding="utf-8")
        for name in cache_files:
            (cache / name).write_text("{}", encoding="utf-8")
        # The shim stands in for `ruler_selfcheck.py`: it writes the file a real mint would write,
        # or -- when asked not to -- writes nothing, which is the failure the WARNING is for.
        real = subprocess.run(["bash", "-lc", "command -v python3"], capture_output=True,
                              text=True).stdout.strip() or "/usr/bin/python3"
        # `scoring_workers` ALSO calls python3, with `-` and a heredoc: the first version of this
        # shim swallowed that call too, so every task came back "?" and nothing was minted -- the
        # fixture disagreeing with the test rather than with the bug. Heredoc calls go to the real
        # interpreter; a `--task` call is the mint.
        (shim / "python3").write_text(
            "#!/bin/bash\n"
            f'if [ "$1" = "-" ]; then exec {real} "$@"; fi\n'
            + ("""t=""; s=""
while [ $# -gt 0 ]; do
  case "$1" in --task) t="$2"; shift 2;; --subset) s="$2"; shift 2;; *) shift;; esac
done
[ -n "$t" ] && echo '{}' > "$ALGOTUNE_BASELINE_CACHE_DIR/${t}__${s}__lane22r3.json"
""" if shim_writes else "exit 1\n"), encoding="utf-8")
        (shim / "python3").chmod(0o755)
        env = {**os.environ, "WORK": str(work), "CACHE": str(cache), "SHIM": str(shim),
               "TASKS_ROOT": str(tasks_root), "WANT_MINT": want_mint}
        got = subprocess.run(["bash", "-c", DRIVE.format(repo=REPO, script=SCRIPT, tasks=tasks)],
                             capture_output=True, text=True, env=env, timeout=300)
        return got.stdout + got.stderr, sorted(p.name for p in cache.iterdir())


def test_only_the_serially_scored_tasks_are_minted():
    """`pagerank` is scored wide and must not gain a serial ruler here -- minting one for every
    task would double the pre-flight and put a `lane22r3` entry beside every wide one."""
    out, files = _run("max_clique_cpsat pagerank")
    assert "max_clique_cpsat__test__lane22r3.json" in files, (out, files)
    assert "max_clique_cpsat__train__lane22r3.json" in files, (out, files)
    assert not any(f.startswith("pagerank") for f in files), (out, files)


def test_an_existing_ruler_is_not_re_minted():
    """A resumed campaign pays nothing: the check is for the file, not for a marker."""
    out, files = _run("max_clique_cpsat",
                      cache_files=("max_clique_cpsat__test__lane22r3.json",
                                   "max_clique_cpsat__train__lane22r3.json"))
    assert "minting" not in out, out
    assert len(files) == 2, files


def test_a_mint_that_fails_names_the_task_that_will_be_refused():
    """Not fatal and not silent. The task still reaches `looplab_eval`, which refuses rather than
    scoring it wrongly -- the operator needs to know which task will produce nulls BEFORE the arm
    runs, which is the whole point of doing this in the pre-flight."""
    out, files = _run("max_clique_cpsat", shim_writes=False)
    assert "WARNING" in out and "max_clique_cpsat" in out, out
    assert "will be REFUSED at scoring time" in out, out
    assert files == [], files


def test_minting_is_opt_in_because_a_preflight_ran_it_for_real():
    """Measured 2026-09-07: three ORPHANED `campaign.sh` processes (ppid 1) were minting into the
    box's LIVE `.baseline_times` on lane `0,48`, hours after the runs that started them were killed
    -- every campaign test that drives this script for its preflight was also driving a real
    three-minute timing per task, into the one directory the whole bench divides by. Eight
    `__lane2r3` entries got there that way.

    So the pre-flight NAMES what is missing and mints only when asked. The fixture below is the one
    that matters: without the flag, nothing is written and the operator is told which task will be
    refused."""
    out, files = _run("max_clique_cpsat", want_mint="0")
    assert files == [], files
    assert "MISSING serial ruler for max_clique_cpsat/test" in out, out
    assert "ALGOTUNE_PREMINT=1" in out, out
    assert "will be REFUSED at scoring time" in out, out

    asked, minted = _run("max_clique_cpsat", want_mint="1")
    assert "max_clique_cpsat__test__lane22r3.json" in minted, (asked, minted)
