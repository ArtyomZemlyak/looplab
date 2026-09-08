"""Two "orphans" on a healthy box, and both were the launcher of the live work.

§332. The sweep's wide orphan scan -- ppid 1 and the script named anywhere in the command line --
counted two while a probe and a ruler mint were running normally. Both were the shells that started
them: `bash -c source …/shell-snapshots/… && nohup run_probe.sh …`, reparented to init the moment
the tool call returned, each holding the live work as a child. Matching a name anywhere in an argv
is the `pkill -f` mistake the list warns about, one layer up.

What the scan is for is real: §313's 23 forkservers, parent gone, 648 MiB, nothing running under
them. The difference is not the reparenting -- `nohup` reparents on purpose -- it is whether
anything is still running underneath.
"""
from __future__ import annotations

import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1] / "benchmarks"
sys.path.insert(0, str(BENCH))

import pulse  # noqa: E402

LAUNCHER = ("/bin/bash -c source /home/jovyan/.claude/shell-snapshots/snapshot-bash.sh && "
            "nohup bash benchmarks/algotune/run_probe.sh deepseek remDL13 0-10,48-58 discrete_log")
PROBE = "/bin/bash benchmarks/algotune/run_probe.sh deepseek remDL13 0-10,48-58 discrete_log"


def test_the_launcher_of_live_work_is_not_a_stray():
    table = [{"pid": "100", "ppid": "1", "cmdline": LAUNCHER},
             {"pid": "101", "ppid": "100", "cmdline": PROBE}]
    assert pulse.stray_bench_processes(table) == []


def test_a_leftover_with_nobody_under_it_is():
    """§313's shape: the run that started it is long dead and nothing is running below."""
    table = [{"pid": "200", "ppid": "1", "cmdline":
              "/bin/bash /var/tmp/looplab-bench/looplab/benchmarks/algotune/campaign.sh"}]
    assert pulse.stray_bench_processes(table) == ["200"]


def test_a_detached_daemon_with_a_child_is_not_a_stray():
    """`nohup … &` reparents to init BY DESIGN -- that is what it is for. The snapshot timer has
    lived that way for three days."""
    table = [{"pid": "300", "ppid": "1", "cmdline":
              "/bin/bash /var/tmp/looplab-bench/looplab/benchmarks/algotune/campaign.sh"},
             {"pid": "301", "ppid": "300", "cmdline": "python3 benchmarks/ruler_selfcheck.py --task x"}]
    assert pulse.stray_bench_processes(table) == []


def test_a_mention_deep_in_an_argv_is_not_the_process():
    """The fixture that separates the two rules: the name is in the command line, the process is a
    grep. Under the old rule this counted; it is the self-match `pkill -f` is forbidden for."""
    table = [{"pid": "400", "ppid": "1", "cmdline": "grep -rn run_probe.sh /var/tmp/looplab-bench"}]
    assert pulse.stray_bench_processes(table) == []


def test_a_child_of_a_living_shell_is_not_a_stray_either():
    table = [{"pid": "500", "ppid": "42", "cmdline": PROBE}]
    assert pulse.stray_bench_processes(table) == []
