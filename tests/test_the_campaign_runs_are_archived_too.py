"""The campaign's own per-run evidence was archived by nobody, and watched by nobody.

Measured 2026-08-31 on `35124d05`:

    grep -c camp-runs benchmarks/snapshot.sh        -> 0
    grep -c camp-runs benchmarks/snapshot_timer.sh  -> 0

`box-jhub-l40s.sh`'s `CAMPAIGN_RUNS` sets `CAMPAIGN_RUNS="$BENCH_ROOT/camp-runs"`. `campaign.sh`'s `RUNS_ROOT` reads it into
`RUNS_ROOT` and `campaign.sh::run_one` writes every task-arm's run to
`$RUNS_ROOT/<task>/run/events.jsonl`. The archiver's discovery loop was

    for D in "$SRC"/runs-* "$SRC"/model-probes "$SRC"/probes

and the timer's fingerprint was the same four patterns. `camp-runs` matches none of them, so the
run logs of a CAMPAIGN -- what the loop proposed, what each call cost, which node became champion --
were copied by no line of either script.

This is the same shape of loss that took sixty-nine runs and ~$100 of metered spend on 2026-08-29,
closed for the probe path on 2026-08-30 and left open for the campaign path. It is worse here in one
respect: `campaign.sh::run_one` runs `rm -rf "$TASK_ROOT"` at the head of every attempt, so a RETRY
destroys the previous attempt's evidence. No container restart is required; re-running a task is
enough.

The fixture is the shape `campaign.sh` actually produces -- `<task>/{memory,knowledge,run,champion}`
with the run log under `run/` -- and not a shape invented to make a test pass; the same directory
layout is visible under `model-probes/*/runs/<task>/` on this box, one level deeper.
"""
import subprocess
from pathlib import Path

from tests.test_snapshot_carries_the_repo_and_the_runs import SNAPSHOT, _bench_root, _rmtree

TIMER = Path(__file__).resolve().parents[1] / "benchmarks" / "snapshot_timer.sh"


def _camp_runs(root: Path, task: str = "svm") -> Path:
    """What `campaign.sh` leaves behind for one arm-B task-arm.

    `rm -rf "$TASK_ROOT"; mkdir -p "$TASK_ROOT/memory" "$TASK_ROOT/knowledge"`, then
    `looplab.cli run ... --out "$TASK_ROOT/run"`, then `extract_champion.py --out
    "$TASK_ROOT/champion/solver.py"`.
    """
    troot = root / task
    (troot / "memory").mkdir(parents=True)
    (troot / "knowledge").mkdir(parents=True)
    (troot / "run").mkdir(parents=True)
    (troot / "run" / "events.jsonl").write_text(
        '{"type": "llm_usage", "data": {"cost": 0.0717}}\n'
        '{"type": "node_evaluated", "data": {"node_id": 3, "metric": 27.466}}\n')
    (troot / "run" / "spans.jsonl").write_text('{"name": "generation"}\n')
    (troot / "champion").mkdir()
    (troot / "champion" / "solver.py").write_text("class Solver:\n    pass\n")
    return troot


def _snapshot(src, dest, archive, **env):
    return subprocess.run(
        ["bash", str(SNAPSHOT), str(dest)],
        env={"PATH": "/usr/bin:/bin", "HOME": str(src.parent),
             "BENCH_ROOT": str(src), "SNAPSHOT_RUNS_ARCHIVE": str(archive), **env},
        capture_output=True, text=True, timeout=300)


def test_the_campaigns_own_run_logs_reach_the_archive(tmp_path):
    """No `CAMPAIGN_RUNS` in the environment -- the operator ran `campaign.sh` without sourcing the
    box profile, which is the documented invocation for every box that is not this one. The tree is
    then found by what it CONTAINS, so a name nobody told the archiver about is still archived."""
    src = _bench_root(tmp_path)
    _camp_runs(src / "camp-runs")
    dest, archive = tmp_path / "snapshots", tmp_path / "runs-archive"

    result = _snapshot(src, dest, archive)
    assert result.returncode == 0, result.stdout + result.stderr

    events = archive / "camp-runs" / "svm" / "run" / "events.jsonl"
    assert events.exists(), (
        "a campaign wrote its whole arm into camp-runs/ and the snapshot copied none of it; the "
        "markers survive and the evidence behind every number in them does not\n" + result.stdout)
    assert "0.0717" in events.read_text() and "27.466" in events.read_text()
    assert (archive / "camp-runs" / "svm" / "run" / "spans.jsonl").exists()
    assert (archive / "camp-runs" / "svm" / "champion" / "solver.py").exists(), (
        "the champion that was actually scored is not in the archive")

    # And the snapshot has to SAY it, or a restorer cannot tell a run that was never archived from
    # one archived and later lost.
    out = next(dest.glob("2*"))
    assert "camp-runs 1" in (out / "runs-manifest.txt").read_text(), (
        (out / "runs-manifest.txt").read_text())


def test_a_runs_root_the_operator_moved_is_archived_because_it_is_named_not_guessed(tmp_path):
    """`CAMPAIGN_RUNS` is the operator's variable and may point anywhere, including off BENCH_ROOT.

    This is the half a pattern over directory names can never cover, and the reason the fix is not
    "add camp-runs to the glob": the next campaign is pointed somewhere else and the glob goes
    quietly out of date, exactly as the `campaign*` one did on 2026-08-23.
    """
    src = _bench_root(tmp_path)
    elsewhere = tmp_path / "arm-b-runs-2026-09"
    _camp_runs(elsewhere, task="kcenters")
    dest, archive = tmp_path / "snapshots", tmp_path / "runs-archive"

    result = _snapshot(src, dest, archive, CAMPAIGN_RUNS=str(elsewhere))
    assert result.returncode == 0, result.stdout + result.stderr

    assert (archive / "arm-b-runs-2026-09" / "kcenters" / "run" / "events.jsonl").exists(), (
        "the archiver ignored the variable that names where the campaign is writing\n"
        + result.stdout)


def test_a_directory_that_holds_no_run_logs_is_not_dragged_in(tmp_path):
    """The other half of discovery-by-content: it must not degrade into "copy BENCH_ROOT".

    `stale-baselines-from-20260829/` is a real directory on this box -- 40-odd baseline JSONs, all
    regenerable by re-measuring, none of them a run. The archive is a sibling of the snapshot
    rotation that is NEVER pruned, so anything that lands in it stays on the S3-backed mount for
    good; a discovery rule that swept up inputs would grow it without bound.
    """
    src = _bench_root(tmp_path)
    _camp_runs(src / "camp-runs")
    (src / "stale-baselines-from-20260829").mkdir()
    (src / "stale-baselines-from-20260829" / "svm__train__w22x1r3.json").write_text('{"ms": 12.5}')
    (src / "looplab_ws").mkdir()
    (src / "looplab_ws" / "algotune_svm.json").write_text('{"task": "svm"}')
    dest, archive = tmp_path / "snapshots", tmp_path / "runs-archive"

    result = _snapshot(src, dest, archive)
    assert result.returncode == 0, result.stdout + result.stderr

    assert (archive / "camp-runs").is_dir()
    assert not (archive / "stale-baselines-from-20260829").exists(), result.stdout
    assert not (archive / "looplab_ws").exists(), result.stdout


def test_an_idle_box_is_still_idle_when_the_campaign_runs_tree_is_empty(tmp_path):
    """`campaign.sh` empties `$CAMPAIGN_RUNS/<task>` at the head of every attempt, so the tree is
    legitimately empty for the first minutes of an arm -- and an empty tree is not a measurement.

    The alarm this protects is the one that cost 3.0 GB: a claim that fires on every routine cycle
    is one an operator learns to scroll past.
    """
    src = _bench_root(tmp_path)
    _rmtree(src / "campaign-final")
    _rmtree(src / "model-probes")
    (src / "camp-runs" / "svm" / "run").mkdir(parents=True)      # rm -rf'd and re-made, nothing yet

    result = _snapshot(src, tmp_path / "snapshots", tmp_path / "runs-archive")
    assert result.returncode == 0, result.stdout
    assert "INCOMPLETE" not in result.stdout, result.stdout


# ----------------------------------------------------------------- and the timer that gates it all
#
# Archiving a tree is no use if the function that decides whether to archive AT ALL cannot see it
# grow. `snapshot_timer.sh` skips a cycle when its fingerprint has not moved, and the fingerprint
# was the same stale four patterns -- so a campaign could fill `camp-runs/` for hours while the
# timer logged "nothing new since the last snapshot; skipping" every thirty minutes.


def _fingerprint(root: Path) -> str:
    from tests.test_snapshot_timer_sees_the_runs import _fingerprint as fp
    return fp(root)


def test_a_campaign_writing_its_runs_moves_the_fingerprint(tmp_path):
    root = tmp_path / "bench"
    (root / "meter").mkdir(parents=True)
    (root / "meter" / "meter.jsonl").write_text("{}\n")
    _camp_runs(root / "camp-runs")
    before = _fingerprint(root)

    # Exactly what an arm-B lane does and nothing else: it appends to its own run log. The task is
    # evaluating locally, so no LLM call and `meter/` -- the directory that was covering run trees
    # by accident -- does not move.
    (root / "camp-runs" / "svm" / "run" / "events.jsonl").write_text(
        (root / "camp-runs" / "svm" / "run" / "events.jsonl").read_text()
        + '{"type": "node_evaluated", "data": {"node_id": 4, "metric": 221.5387}}\n')

    assert _fingerprint(root) != before, (
        "a campaign filled camp-runs/ and the timer reports 'nothing new'; the insurance against a "
        "container restart in the middle of a multi-hour arm never fires")
