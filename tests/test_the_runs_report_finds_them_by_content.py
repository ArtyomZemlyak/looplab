"""`bench_trees.sh` exists because three different hand-rolled discoveries lied in one sweep.

On 2026-08-31, while two probes were demonstrably alive and writing:

  * `find / -xdev -name events.jsonl` printed nothing. /var/tmp is a separate overlayfs and -xdev
    will not cross a mount boundary.
  * `find <probe-dir> -maxdepth 3 -type f` listed only the workspace. The events live at depth 4,
    under runs/<task>/run/.
  * guessing the archive path happened to work, but only because a snapshot had already copied a
    tree there.

Each read as "there are no measurements on this box". None of them meant it, and one of them --
the first -- is the same shape as the 2026-08-29 loss: an instrument answering a narrower question
than the one asked, silently.

So the contract is: find run trees by CONTENT, at any depth, across mount boundaries, under roots
taken from the same variables the rest of the bench writes with. These tests hold it to that.
"""
import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TREES = REPO / "benchmarks" / "bench_runs_report.sh"


def _mk_run(root, rel, *, lines=3, nodes=1, cost=0.25):
    d = root / rel
    d.mkdir(parents=True, exist_ok=True)
    rows = ['{"type": "run_started"}']
    rows += ['{"type": "node_evaluated"}'] * nodes
    rows += ['{"type": "llm_usage", "data": {"cost": %f}}' % cost] * lines
    (d / "events.jsonl").write_text("\n".join(rows) + "\n")
    return d


def _run(root, extra=None):
    env = dict(os.environ)
    env["BENCH_ROOT"] = str(root)
    env["SNAPSHOT_RUNS_ARCHIVE"] = str(root / "nonexistent-archive")
    r = subprocess.run(["bash", str(TREES)] + ([str(extra)] if extra else []),
                       capture_output=True, text=True, timeout=300, env=env)
    return r


def test_it_finds_a_run_however_deep_it_is_buried(tmp_path):
    """`-maxdepth 3` missed the real layout. Nothing here may assume a depth."""
    root = tmp_path / "bench"
    _mk_run(root, "model-probes/x/runs/task/run")                       # the real depth, 4
    _mk_run(root, "a/b/c/d/e/f/g/run")                                  # deeper than anyone guesses
    r = _run(root)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "model-probes/x/runs/task/run" in r.stdout, r.stdout
    assert "a/b/c/d/e/f/g/run" in r.stdout, (
        "a deeply buried run was not found -- a depth limit has crept back in\n" + r.stdout
    )


def test_a_directory_is_a_run_because_of_what_is_in_it_not_what_it_is_called(tmp_path):
    """Name globs were the other failure mode: `runs/` is not the property, events.jsonl is."""
    root = tmp_path / "bench"
    _mk_run(root, "not-called-anything-like-a-run/zzz")                 # no 'run' in the path
    (root / "runs" / "looks-right-but-empty").mkdir(parents=True)       # named right, holds nothing
    r = _run(root)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "not-called-anything-like-a-run/zzz" in r.stdout, (
        "a real run was skipped because its path did not look like one\n" + r.stdout
    )
    assert "looks-right-but-empty" not in r.stdout, (
        "an empty directory was reported as a measurement because of its name\n" + r.stdout
    )


def test_one_row_per_run_not_one_per_file(tmp_path):
    """events.jsonl and spans.jsonl sit side by side; the spans row was noise (0 nodes, $0)."""
    root = tmp_path / "bench"
    d = _mk_run(root, "model-probes/y/runs/task/run")
    (d / "spans.jsonl").write_text('{"span": 1}\n{"span": 2}\n')
    r = _run(root)
    assert r.returncode == 0, r.stdout + r.stderr
    hits = [l for l in r.stdout.splitlines() if "model-probes/y/runs/task/run" in l]
    assert len(hits) == 1, f"expected one row for the run, got {len(hits)}:\n" + r.stdout
    assert " 2 " in hits[0] or hits[0].rstrip().split()[-2] == "2", \
        "the spans count is not reported on the run's row:\n" + hits[0]


def test_it_reports_nodes_and_cost_not_just_existence(tmp_path):
    """The point is triage: a tree that is alive-but-nodeless must be distinguishable at a glance."""
    root = tmp_path / "bench"
    _mk_run(root, "model-probes/rich/runs/t/run", lines=4, nodes=3, cost=0.10)
    _mk_run(root, "model-probes/barren/runs/t/run", lines=2, nodes=0, cost=0.50)
    r = _run(root)
    assert r.returncode == 0, r.stdout + r.stderr
    rich = next(l for l in r.stdout.splitlines() if "rich" in l)
    barren = next(l for l in r.stdout.splitlines() if "barren" in l)
    assert "0.4000" in rich, f"cost not summed from the `cost` field: {rich}"
    assert "1.0000" in barren, f"cost not summed from the `cost` field: {barren}"
    assert " 3 " in rich, f"node count missing: {rich}"
    assert " 0 " in barren, f"node count missing: {barren}"


def test_a_box_with_nothing_says_so_instead_of_printing_an_empty_table(tmp_path):
    """Silence is what every lying instrument produced. An empty result must announce itself."""
    root = tmp_path / "bench"
    root.mkdir()
    r = _run(root)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "no measurements" in r.stdout.lower(), (
        "an empty box printed a bare header -- indistinguishable from the instrument failing\n"
        + r.stdout
    )


# ---------------------------------------------------------------- a zero is at least six facts
#
# `node_evaluated` carries `metric` and `eval_seconds` and nothing else, so a 0.0 in the event
# stream is indistinguishable from any other 0.0. The diagnosis is in `nodes/<id>/score.log` -- the
# harness's `no_speedup` block, with the evaluator's verdict and the actual `is_solution` errors --
# and this report was not reading it. On 2026-08-31 answering "ruler or solver?" for one zero took
# four hand-rolled commands, and that question is on the sweep list every single time.


def _mk_score(root, rel, *, speedup, eval_seconds, reason=None, verdict=None, errors=None):
    import json
    d = root / rel
    d.mkdir(parents=True, exist_ok=True)
    body = {"speedup": speedup, "eval_seconds": eval_seconds, "subset": "train"}
    if reason:
        ns = {"reason": reason}
        if verdict:
            ns["evaluator_verdict"] = verdict
        if errors:
            ns["is_solution_errors"] = [{"message": m, "count": 1} for m in errors]
        body["no_speedup"] = ns
    (d / "score.log").write_text(json.dumps(body))


def test_a_zero_is_reported_with_the_reason_not_just_the_number(tmp_path):
    root = tmp_path / "bench"
    _mk_run(root, "model-probes/z/runs/t/run")
    _mk_score(root, "model-probes/z/runs/t/run/nodes/node_0",
              speedup=0.0, eval_seconds=60.7, reason="no_valid_speedups",
              verdict="No valid speedup calculations from agent evaluation",
              errors=["Solution verification failed: max rel err=1.37e+05"])
    r = _run(root)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "no_valid_speedups" in r.stdout, (
        "a zero was listed without the harness's own reason, which is the whole point:\n" + r.stdout
    )
    assert "max rel err" in r.stdout, "the is_solution error that explains the zero is not shown"


def test_a_ruler_failure_is_called_one_and_a_solver_failure_is_not(tmp_path):
    """The sweep list's rule: ~0.1 s means the evaluation never reached the solver."""
    root = tmp_path / "bench"
    _mk_run(root, "model-probes/a/runs/t/run")
    _mk_run(root, "model-probes/b/runs/t/run")
    _mk_score(root, "model-probes/a/runs/t/run/nodes/node_0",
              speedup=0.0, eval_seconds=0.1, reason="solver_unloadable")
    _mk_score(root, "model-probes/b/runs/t/run/nodes/node_0",
              speedup=0.0, eval_seconds=60.7, reason="no_valid_speedups")
    r = _run(root)
    lines = [l for l in r.stdout.splitlines() if "node_0" in l]
    quick = [l for l in lines if "0.1s" in l]
    slow = [l for l in lines if "60.7s" in l]
    assert quick and "RULER" in quick[0], (
        f"a 0.1 s evaluation was not called a ruler failure: {quick}"
    )
    assert slow and "RULER" not in slow[0], (
        f"a 60.7 s evaluation was blamed on the ruler: {slow}"
    )


def test_a_real_score_is_not_dragged_into_the_zero_list(tmp_path):
    root = tmp_path / "bench"
    _mk_run(root, "model-probes/ok/runs/t/run")
    _mk_score(root, "model-probes/ok/runs/t/run/nodes/node_0", speedup=123.13, eval_seconds=54.0)
    r = _run(root)
    assert "no zero-scoring nodes" in r.stdout, (
        "a healthy 123.13 was reported as a zero:\n" + r.stdout
    )


def test_a_null_speedup_counts_as_a_zero_and_says_so(tmp_path):
    """`speedup: null` is the regime-mismatch/refusal shape and must not be silently skipped."""
    root = tmp_path / "bench"
    _mk_run(root, "model-probes/n/runs/t/run")
    _mk_score(root, "model-probes/n/runs/t/run/nodes/node_0",
              speedup=None, eval_seconds=0.0, reason="baseline_regime_mismatch")
    r = _run(root)
    assert "baseline_regime_mismatch" in r.stdout, (
        "a refused evaluation vanished from the report entirely:\n" + r.stdout
    )


def test_a_score_log_with_no_reason_still_appears(tmp_path):
    """An unexplained zero is the most important one to see, not the easiest one to drop."""
    root = tmp_path / "bench"
    _mk_run(root, "model-probes/u/runs/t/run")
    _mk_score(root, "model-probes/u/runs/t/run/nodes/node_0", speedup=0.0, eval_seconds=44.0)
    r = _run(root)
    assert "node_0" in r.stdout and "unstated" in r.stdout, (
        "a zero carrying no no_speedup block was dropped instead of flagged:\n" + r.stdout
    )


def test_one_zero_in_two_places_is_reported_once(tmp_path):
    """A finished run exists twice -- live tree and archive -- and that is not two zeros.

    Within an hour of the zeros section shipping it printed `remPde4 node_0` twice, because it
    walked each root separately and printed a line per score.log. The reader of a sweep needs the
    number of ZEROS, not the number of copies of the evidence.
    """
    root = tmp_path / "bench"
    archive = tmp_path / "runs-archive"
    _mk_run(root, "model-probes/dup/runs/t/run")
    for base in (root, archive):
        _mk_score(base, "model-probes/dup/runs/t/run/nodes/node_0",
                  speedup=0.0, eval_seconds=60.7, reason="no_valid_speedups")

    import os
    env = dict(os.environ)
    env["BENCH_ROOT"] = str(root)
    env["SNAPSHOT_RUNS_ARCHIVE"] = str(archive)
    r = subprocess.run(["bash", str(TREES)], capture_output=True, text=True, timeout=300, env=env)
    assert r.returncode == 0, r.stdout + r.stderr

    hits = [l for l in r.stdout.splitlines() if "node_0" in l and "no_valid_speedups" in l]
    assert len(hits) == 1, f"one zero was reported {len(hits)} times:\n" + r.stdout
    assert "2 copies" in hits[0], (
        "deduped, but silently -- a reader cannot tell a deduped zero from a single-copy one:\n"
        + hits[0]
    )


def test_the_zero_section_actually_reads_its_input(tmp_path):
    """`python3 -` takes its PROGRAM from stdin; piping the path list there empties sys.stdin.

    That defect printed "no zero-scoring nodes on this box" over a box that had one -- the worst
    possible failure for this section, because silence is exactly what a clean box looks like.
    """
    root = tmp_path / "bench"
    _mk_run(root, "model-probes/q/runs/t/run")
    _mk_score(root, "model-probes/q/runs/t/run/nodes/node_0",
              speedup=0.0, eval_seconds=60.7, reason="no_valid_speedups")
    r = _run(root)
    assert "no zero-scoring nodes" not in r.stdout, (
        "the section reported a clean box while a zero sat in front of it:\n" + r.stdout
    )
    assert "no_valid_speedups" in r.stdout


def test_a_score_log_with_a_build_prefix_is_still_read(tmp_path):
    """`looplab_eval` prints its build result BEFORE the JSON, and parsing the whole file raises.

    Measured 2026-09-01: 31 of 76 score.log files on this box carry such a prefix, and one of the
    corpus's three zero-scoring nodes was invisible because of it -- remEE6's node_3, whose Cython
    kernel failed to compile. A section built to stop a zero going unreported was losing one.
    """
    root = tmp_path / "bench"
    _mk_run(root, "model-probes/pfx/runs/t/run")
    d = root / "model-probes/pfx/runs/t/run/nodes/node_3"
    d.mkdir(parents=True)
    import json as _j
    (d / "score.log").write_text(
        "looplab_eval: build_ext failed rc=1: kernel.pyx:27:0: 'x.pxd' not found\n"
        + _j.dumps({"speedup": 0.0, "eval_seconds": 7.6,
                    "no_speedup": {"reason": "compilation_failed",
                                   "evaluator_verdict": "Compilation failed"}}))
    r = _run(root)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "node_3" in r.stdout, (
        "a zero whose score.log starts with a build line was dropped:\n" + r.stdout
    )
    assert "compilation_failed" in r.stdout


def test_a_failed_build_is_named_as_such_not_blamed_on_the_solver(tmp_path):
    """Three categories, not two: the ruler failing, the solver failing, and never compiling.

    No eval_seconds threshold separates the third -- remEE6's node_3 came back in 8.3 s, far from
    the ~0.1 s that means the ruler never ran.
    """
    root = tmp_path / "bench"
    _mk_run(root, "model-probes/bld/runs/t/run")
    d = root / "model-probes/bld/runs/t/run/nodes/node_0"
    d.mkdir(parents=True)
    import json as _j
    (d / "score.log").write_text(_j.dumps(
        {"speedup": 0.0, "eval_seconds": 8.3,
         "no_speedup": {"reason": "compilation_failed", "evaluator_verdict": "Compilation failed"}}))
    r = _run(root)
    line = next(l for l in r.stdout.splitlines() if "node_0" in l and "compilation_failed" in l)
    assert "BUILD" in line, f"a build failure was reported as a solver failure: {line}"
    assert "RULER" not in line, f"a 8.3 s build failure was blamed on the ruler: {line}"


def test_a_solver_failure_is_still_a_solver_failure(tmp_path):
    root = tmp_path / "bench"
    _mk_run(root, "model-probes/slv/runs/t/run")
    d = root / "model-probes/slv/runs/t/run/nodes/node_0"
    d.mkdir(parents=True)
    import json as _j
    (d / "score.log").write_text("looplab_eval: build_ext ok\n" + _j.dumps(
        {"speedup": 0.0, "eval_seconds": 60.7,
         "no_speedup": {"reason": "no_valid_speedups", "evaluator_verdict": "No valid speedups"}}))
    r = _run(root)
    line = next(l for l in r.stdout.splitlines() if "node_0" in l and "no_valid_speedups" in l)
    assert "BUILD" not in line and "RULER" not in line, (
        f"a build that succeeded and computed wrong values was misfiled: {line}"
    )


def _score(root, probe, body):
    d = root / "model-probes" / probe / "runs" / "t" / "run" / "nodes" / "node_0"
    d.mkdir(parents=True, exist_ok=True)
    (d / "score.log").write_text(body)


def test_a_brace_inside_the_build_prefix_does_not_lose_the_node(tmp_path):
    """C and Cython print braces freely: `error: expected '}' before` is an ordinary message.

    `raw.find("{")` aimed the decoder at that brace, `json.loads` raised, and `except: continue`
    dropped the node -- the failure this section exists to prevent, inside its own repair.
    """
    root = tmp_path / "bench"
    _mk_run(root, "model-probes/br/runs/t/run")
    _score(root, "br", 'looplab_eval: build_ext failed rc=1: error: expected 4{5} before return\n'
                       '{"speedup": 0.0, "eval_seconds": 9.9, '
                       '"no_speedup": {"reason": "compilation_failed"}}\n')
    r = _run(root)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "node_0" in r.stdout and "compilation_failed" in r.stdout, (
        "a brace in the build prefix lost the node again:\n" + r.stdout
    )


def test_text_after_the_json_does_not_lose_the_node(tmp_path):
    root = tmp_path / "bench"
    _mk_run(root, "model-probes/tr/runs/t/run")
    _score(root, "tr", '{"speedup": 0.0, "eval_seconds": 9.9, '
                       '"no_speedup": {"reason": "no_valid_speedups"}}\n'
                       'looplab_eval: cleanup ok\n')
    r = _run(root)
    assert "node_0" in r.stdout and "no_valid_speedups" in r.stdout, (
        "a trailing line made the whole tail undecodable and the node vanished:\n" + r.stdout
    )


def test_a_missing_eval_seconds_is_unknown_not_a_ruler_failure(tmp_path):
    """`float(x or 0.0)` turned an absent field into 0.0 and then into a diagnosis."""
    root = tmp_path / "bench"
    _mk_run(root, "model-probes/ns/runs/t/run")
    _score(root, "ns", '{"speedup": 0.0, "no_speedup": {"reason": "no_valid_speedups"}}\n')
    r = _run(root)
    line = next(l for l in r.stdout.splitlines() if "node_0" in l)
    assert "RULER" not in line, (
        f"an absent eval_seconds was read as 'the evaluation never reached the solver': {line}"
    )
    assert "unknown" in line and "?s" in line, f"the absence is not reported as an absence: {line}"


def test_an_unreadable_score_log_is_announced_not_skipped(tmp_path):
    """A torn file is a node whose outcome is UNKNOWN, which is worse than a zero, not better."""
    root = tmp_path / "bench"
    _mk_run(root, "model-probes/torn/runs/t/run")
    _score(root, "torn", "looplab_eval: build_ext ok\n")
    r = _run(root)
    assert "COULD NOT BE READ" in r.stdout, (
        "a score.log with no JSON at all was skipped in silence:\n" + r.stdout
    )
    assert "torn" in r.stdout
