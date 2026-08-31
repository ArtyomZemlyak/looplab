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
