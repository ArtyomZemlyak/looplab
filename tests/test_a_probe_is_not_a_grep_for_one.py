"""A search for the probe command line contains the probe command line.

The standing sweep's point 1 asks which `looplab.cli run` processes are on their lanes, and the
naive answer — walk `/proc` for command lines containing `looplab.cli` and `run` — counts a `grep`
FOR that command line as a probe. Measured 2026-09-04: `grep -rn "python -m looplab.cli run --out"`
samples as `argv[0]=grep` with all 96 cpus, the naive matcher says probe, the rule says not. Two
phantom "run" lines had already gone through that morning's sweep.

`run_probe.sh` learned this on 2026-09-01 (pytest's own `looplab.cli ui --help` and
`looplab.cli resume /tmp/…/run` were making concurrent dry runs refuse) and keeps the rule in a
heredoc. `benchmarks/lanes.py` is the callable copy; the last test here pins the two to agree, so
the bench does not end up with a fixed rule and a naive one at the same time.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmarks"))

import lanes  # noqa: E402

ROOT = "/var/tmp/looplab-bench"
PROBE = ["/opt/conda/bin/python", "-m", "looplab.cli", "run",
         f"{ROOT}/model-probes/capA3/ws/algotune_edge_expansion.json",
         "--out", f"{ROOT}/model-probes/capA3/runs/edge_expansion/run", "--backend", "llm"]


def test_a_real_probe_is_a_probe():
    assert lanes.is_bench_probe(PROBE, ROOT)


def test_a_grep_for_the_probe_line_is_not_a_probe():
    assert not lanes.is_bench_probe(
        ["grep", "-rn", "python -m looplab.cli run --out", f"{ROOT}/model-probes"], ROOT), (
        "argv[0] is the discriminator: a search for a string contains that string")
    for tool in ("ugrep", "rg", "vim", "less"):
        assert not lanes.is_bench_probe([tool, " ".join(PROBE)], ROOT), tool


def test_a_grep_whose_own_flags_spell_the_probe_is_not_a_probe():
    """The case above does not actually test `argv[0]`, and mutation proved it: a one-argument
    pattern has no bare `-m` element, so dropping the interpreter check left the file green.

    `grep -rn -m 1 -e looplab.cli -e run <root>` is an ordinary invocation whose argv carries `-m`,
    `looplab.cli`, `run` AND the bench root as separate elements. Every clause but the interpreter
    one says probe."""
    argv = ["grep", "-rn", "-m", "1", "-e", "looplab.cli", "-e", "run", f"{ROOT}/model-probes"]
    assert "-m" in argv and "looplab.cli" in argv and "run" in argv and ROOT in " ".join(argv)
    assert not lanes.is_bench_probe(argv, ROOT), (
        "only argv[0] separates this from a probe, and grep is not a python interpreter")


def test_pytests_own_children_are_not_probes():
    """Both were caught occupying nothing on 2026-09-01 and refusing every concurrent dry run."""
    assert not lanes.is_bench_probe(
        ["/opt/conda/bin/python", "-m", "looplab.cli", "ui", "--help"], ROOT)
    assert not lanes.is_bench_probe(
        ["/opt/conda/bin/python", "-m", "looplab.cli", "resume",
         "/tmp/pytest-of-jovyan/pytest-1/x/run"], ROOT), (
        "a resume OUTSIDE the bench root is somebody else's run; the root is part of the rule")
    assert lanes.is_bench_probe(
        ["/opt/conda/bin/python", "-m", "looplab.cli", "resume",
         f"{ROOT}/model-probes/capA3/runs/edge_expansion/run"], ROOT), (
        "a resume INSIDE the bench root does occupy the lane")


def test_a_subcommand_that_only_LOOKS_at_a_run_is_not_a_probe():
    """`ui --help` has no bench root in it, so it never tested the subcommand clause -- mutation
    showed that accepting ANY subcommand left the file green. An operator inspecting a finished run
    passes the run directory, and that argv satisfies every clause except the subcommand."""
    argv = ["/opt/conda/bin/python", "-m", "looplab.cli", "inspect",
            f"{ROOT}/model-probes/capA3/runs/edge_expansion/run"]
    assert not lanes.is_bench_probe(argv, ROOT), (
        "reading a run directory does not occupy its lane; only run/resume do")


def test_python_dash_c_is_never_a_probe():
    assert not lanes.is_bench_probe(
        ["/opt/conda/bin/python", "-c", f"import looplab.cli; run('{ROOT}')"], ROOT)


def test_a_dash_c_script_given_the_probe_words_as_ARGUMENTS_is_not_a_probe():
    """That first argv has no bare `-m`, so it never tested the `-c` clause; mutation showed the
    file stayed green with the clause deleted. A `-c` script takes `sys.argv` after the source, and
    the sweep's own scanners are written exactly that way -- source first, then the lane and the
    root. Give one the probe's own words and every other clause says probe."""
    argv = ["/opt/conda/bin/python", "-c", "import sys; print(sys.argv)",
            "-m", "looplab.cli", "run", f"{ROOT}/model-probes"]
    assert "-m" in argv and "looplab.cli" in argv and "run" in argv
    assert not lanes.is_bench_probe(argv, ROOT), (
        "a -c script is the sweep's own instrument, never a probe")


def test_algotuner_keeps_its_bare_name_match():
    assert lanes.is_bench_probe(["/bin/bash", f"{ROOT}/AlgoTuner/algotune.sh", "solve"], ROOT)


def _fake_proc(tmp_path, pids: dict):
    for pid, argv in pids.items():
        d = tmp_path / str(pid)
        d.mkdir()
        (d / "cmdline").write_bytes(b"".join(a.encode() + b"\0" for a in argv))
    return str(tmp_path)


def test_the_scan_reports_the_lane_and_ignores_the_grep(tmp_path):
    proc = _fake_proc(tmp_path, {
        100: PROBE,
        101: ["grep", "-rn", "python -m looplab.cli run --out", f"{ROOT}/model-probes"],
    })
    aff = {100: set(range(0, 11)) | set(range(48, 59)), 101: set(range(96))}
    got = lanes.probes(ROOT, proc, aff.__getitem__)
    assert [(p["pid"], p["probe"]) for p in got] == [(100, "capA3")], got
    assert lanes._fmt(got[0]["cpus"]) == "0-10,48-58"
    assert [p["pid"] for p in lanes.lane_busy("0-10,48-58", ROOT, proc, aff.__getitem__)] == [100]
    assert lanes.lane_busy("11-21,59-69", ROOT, proc, aff.__getitem__) == [], (
        "the grep's 96-cpu affinity intersects every lane; that is exactly how a phantom makes "
        "every lane look busy")


def test_a_process_that_exits_mid_scan_is_not_an_error(tmp_path):
    proc = _fake_proc(tmp_path, {100: PROBE, 102: PROBE})

    def aff(pid):
        if pid == 102:
            raise ProcessLookupError("vanished under the reader")
        return {0}
    assert [p["pid"] for p in lanes.probes(ROOT, proc, aff)] == [100]


def test_the_rule_here_and_the_rule_in_run_probe_sh_are_the_same_rule():
    """Two copies of a guard is how the bench got a fixed script and a naive sweep at once."""
    body = (Path(__file__).resolve().parents[1]
            / "benchmarks" / "algotune" / "run_probe.sh").read_text(encoding="utf-8")
    # `<<'PYEOF'` puts the delimiter on the OPENING line too, so the body is between the FIRST
    # and SECOND occurrence -- splitting on the first one yields ` - "$LANE" "$ROOT" <<'`.
    guard = body.split("BUSY=$(python3", 1)[1].split("PYEOF")[1]
    for clause in ('"-c" in argv',
                   'exe.startswith("python")',
                   '"-m" in argv and "looplab.cli" in argv',
                   'any(v in argv for v in ("run", "resume"))',
                   "root in line"):
        assert re.search(re.escape(clause), guard), (
            f"run_probe.sh no longer contains `{clause}`; lanes.py and the lane guard have "
            "drifted, and the bench is back to a fixed copy and a naive one")
