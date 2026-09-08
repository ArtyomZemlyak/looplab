"""A campaign that exports one evaluation width for twenty tasks now produces six null scores.

§314 measured the reference submitted as its own candidate on the six CP-SAT tasks: 1.1375-1.6028
with `auto` and 1.0113-1.0967 with `ALGOTUNE_EVAL_WORKERS=1`, on an idle box, against baselines
built in each regime. §315 made `looplab_eval` refuse the wide case outright
(`regime_not_scorable_for_task`), because a candidate that changes nothing scores about 1.5 there.
`campaign.sh` exports `auto` once for the whole run — so the refusal, which is correct, would have
cost six dollars in probes that could not be scored.

Driven against the real `campaign.sh`, not a copy of its logic: the function is extracted from the
shipped file and run.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DRIVE = """set -e
sed -n '/^scoring_workers() {{/,/^}}/p' "{script}" > "$1/sw.sh"
. "$1/sw.sh"
REPO="{repo}"
for t in {tasks}; do echo "$t=$(scoring_workers "$t")"; done
"""

# The tasks the fixture reference tree writes a CP-SAT solver for; everything else gets a plain one.
# §314's nine are the real set — these two are the pair this file drives.
_CPSAT = {"max_clique_cpsat", "min_dominating_set"}


def _drive(*tasks, root=True) -> dict:
    script = REPO / "benchmarks" / "algotune" / "campaign.sh"
    with tempfile.TemporaryDirectory() as tmp:
        # THE REFERENCE TREE, as a fixture, because `scoring_workers` asks `ruler_check` in a
        # SUBPROCESS: without the bench box's AlgoTune checkout at `/var/tmp/looplab-bench` every
        # task answered `?` and this test failed on the fixture rather than on the rule.
        # `ALGOTUNE_TASKS_ROOT` is the env override `ruler_check.CPSAT_ROOT` reads.
        env = dict(os.environ)
        if root:
            tasks_root = Path(tmp) / "AlgoTuneTasks"
            for task in tasks:
                (tasks_root / task).mkdir(parents=True)
                (tasks_root / task / f"{task}.py").write_text(
                    "from ortools.sat.python import cp_model\n" if task in _CPSAT
                    else "import numpy\n", encoding="utf-8")
            env["ALGOTUNE_TASKS_ROOT"] = str(tasks_root)
        else:
            env["ALGOTUNE_TASKS_ROOT"] = str(Path(tmp) / "no-such-checkout")
        body = DRIVE.format(script=script, repo=REPO, tasks=" ".join(tasks))
        got = subprocess.run(["bash", "-c", body, "_", tmp], capture_output=True, text=True,
                             timeout=300, env=env)
        out = {}
        for line in got.stdout.splitlines():
            if "=" in line:
                k, v = line.rsplit("=", 1)
                out[k] = v.strip()
        return out


def test_a_cpsat_task_is_scored_serially_and_a_plain_one_wide():
    got = _drive("max_clique_cpsat", "pagerank")
    assert got.get("max_clique_cpsat") == "1", got
    assert got.get("pagerank") == "auto", got


def test_an_unreadable_reference_is_not_reported_as_not_cpsat():
    """`uses_cpsat` answers False when the file is missing -- right for an inventory, wrong here:
    it would send an unknown task to the wide regime silently, which is the null-score campaign
    arriving through the safety net. Driven before the fix, this printed `auto`."""
    got = _drive("no_such_task_at_all", root=False)   # a reference tree with nothing in it
    assert got.get("no_such_task_at_all") == "?", got


def test_the_driver_acts_on_the_answer_rather_than_only_printing_it():
    src = (REPO / "benchmarks" / "algotune" / "campaign.sh").read_text(encoding="utf-8")
    body = src[src.index("run_one() {"):src.index("run_one() {") + 2000]
    assert 'WANT_WORKERS="$(scoring_workers "$T")"' in body, body[:400]
    assert 'export ALGOTUNE_EVAL_WORKERS="$WANT_WORKERS"' in body, body[:400]
    # And the unreadable case must not silently keep the campaign default.
    assert 'if [ "$WANT_WORKERS" = "?" ]; then' in body, body[:400]
