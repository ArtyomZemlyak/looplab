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
export TASKS="{tasks}"
export PREMINT_LANE="0-1"
sed -n '/^scoring_workers() {{/,/^}}/p' "{script}" > "$WORK/a.sh"
sed -n '/^premint_serial_rulers() {{/,/^}}/p' "{script}" > "$WORK/b.sh"
. "$WORK/a.sh"; . "$WORK/b.sh"
premint_serial_rulers
"""


def _run(tasks, cache_files=(), shim_writes=True):
    with tempfile.TemporaryDirectory() as tmp:
        work, cache, shim = Path(tmp) / "w", Path(tmp) / "c", Path(tmp) / "s"
        for d in (work, cache, shim):
            d.mkdir()
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
        env = {**os.environ, "WORK": str(work), "CACHE": str(cache), "SHIM": str(shim)}
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
