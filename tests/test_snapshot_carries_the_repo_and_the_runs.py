"""A snapshot that records a sha but not the objects is a receipt, not a backup.

This test exists because of a loss, not a theory. `benchmarks/snapshot.sh` calls itself "everything
that cannot be regenerated" and, until 2026-08-30, it bundled the third-party AlgoTune checkout
while writing OUR repo into `PROVENANCE.txt` as one line of text:

    looplab:  af0e4772 docs(56): 11.5 measurements per dollar ... (0 dirty files)

That line was written at 19:11 on 2026-08-29 and was accurate. At about 19:15 the container
restarted. `BENCH_ROOT` lives under `/var/tmp`, which is the container's own writable layer and not
the pod's persistent mount, so it came back empty, and `af0e4772` became a sha naming an object that
no surviving repository anywhere contained -- not the two local clones, not any of the 111 branches
on the remote, whose bench branch had stopped at 07:11 that morning. Thirty-seven commits went with
it, five of them code fixes carrying their own falsifying tests, two of those still waiting to be
accepted by a probe that was running when the container died.

The per-run evidence went the same way and for the same reason: `runs-*` and `model-probes/` hold
each run's `events.jsonl` and `spans.jsonl` -- the per-call cost, the node scores, the reasoning the
loop's own analysis is written from -- and no line of the script had ever copied them. The campaign
markers survived in `campaign-final/`; the evidence behind every number in docs/56 did not.

So both halves are asserted here, and both are asserted the only way that means anything: by
RESTORING from the archive and finding the work, rather than by finding a file with a promising
name. Run against the script as it stood on 2026-08-29, both fail.
"""
import subprocess
from pathlib import Path

import pytest

SNAPSHOT = Path(__file__).resolve().parents[1] / "benchmarks" / "snapshot.sh"


def _git(cwd, *args):
    return subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
                          cwd=cwd, check=True, capture_output=True, text=True)


def _bench_root(tmp_path):
    """A BENCH_ROOT shaped like the real one on the morning of the loss."""
    src = tmp_path / "bench"

    # Both checkouts. The third-party one was always bundled; ours never was.
    for name, subject in (("AlgoTune", "the ruler generation lives in the key"),
                          ("looplab", "the commit the restart was about to eat")):
        repo = src / name
        repo.mkdir(parents=True)
        (repo / "kept.txt").write_text(f"{name} tracked content\n")
        _git(repo, "init", "-q")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", subject)

    # An uncommitted edit, so "(0 dirty files)" is a claim the archive can be checked against.
    (src / "looplab" / "kept.txt").write_text("looplab tracked content\nan edit nobody committed\n")

    # The sources the script already knew about, so a MISSING line for one of them cannot be what
    # makes this test red.
    (src / "looplab" / "benchmarks" / "algotune").mkdir(parents=True)
    (src / "looplab" / "benchmarks" / "algotune" / ".baseline_times").mkdir()
    (src / "AlgoTune" / "reports").mkdir()
    (src / "meter").mkdir()
    (src / "logs").mkdir()
    campaign = src / "campaign-final"
    campaign.mkdir()
    (campaign / "B-pde_heat1d.final.json").write_text('{"speedup": 99.0029}\n')

    # A probe mid-flight: the shape whose loss cost sixty-nine runs.
    run = src / "model-probes" / "dsPde3" / "runs" / "r1" / "run"
    run.mkdir(parents=True)
    (run / "events.jsonl").write_text('{"type": "llm_usage", "data": {"cost": 0.0717}}\n')
    (run / "spans.jsonl").write_text('{"name": "generation", "attributes": {"phase": "propose"}}\n')
    return src


def _snapshot(src, dest, archive):
    return subprocess.run(
        ["bash", str(SNAPSHOT), str(dest)],
        env={"PATH": "/usr/bin:/bin", "HOME": str(src.parent),
             "BENCH_ROOT": str(src), "SNAPSHOT_RUNS_ARCHIVE": str(archive)},
        capture_output=True, text=True, timeout=300)


def test_our_own_commits_are_restorable_from_the_archive_alone(tmp_path):
    src = _bench_root(tmp_path)
    dest, archive = tmp_path / "snapshots", tmp_path / "runs-archive"
    result = _snapshot(src, dest, archive)
    assert result.returncode == 0, result.stdout + result.stderr

    out = next(dest.glob("2*"))
    bundle = out / "looplab.bundle"
    assert bundle.exists(), (
        "PROVENANCE.txt names our HEAD; without the objects behind it the name is worthless.\n"
        + result.stdout)

    # The claim is restorability, so restore. `git clone <bundle>` is the operation a person
    # actually performs at 03:00 after a container has eaten the working tree.
    restored = tmp_path / "restored"
    subprocess.run(["git", "clone", "-q", str(bundle), str(restored)], check=True,
                   capture_output=True)
    log = subprocess.run(["git", "log", "--oneline", "-1"], cwd=restored,
                         capture_output=True, text=True, check=True).stdout
    assert "the commit the restart was about to eat" in log

    # "(0 dirty files)" must be checkable, and a dirty tree must survive too.
    assert (out / "looplab-dirty.txt").read_text().strip(), "the dirty file is not even listed"
    assert "an edit nobody committed" in (out / "looplab-uncommitted.patch").read_text()


def test_the_per_run_evidence_is_archived_not_just_the_campaign_markers(tmp_path):
    src = _bench_root(tmp_path)
    dest, archive = tmp_path / "snapshots", tmp_path / "runs-archive"
    result = _snapshot(src, dest, archive)
    assert result.returncode == 0, result.stdout + result.stderr

    events = archive / "model-probes" / "dsPde3" / "runs" / "r1" / "run" / "events.jsonl"
    assert events.exists(), (
        "the campaign markers were archived and the evidence behind them was not\n" + result.stdout)
    assert "0.0717" in events.read_text()
    assert (archive / "model-probes" / "dsPde3" / "runs" / "r1" / "run" / "spans.jsonl").exists()

    # The snapshot has to SAY what the archive held at its moment, or a restorer cannot tell a run
    # that was never archived from one that was archived and later pruned.
    out = next(dest.glob("2*"))
    assert "model-probes 1" in (out / "runs-manifest.txt").read_text()


def test_a_missing_run_tree_makes_the_snapshot_report_itself_incomplete(tmp_path):
    """The exit code is the claim: silence about an absent source is the failure mode this
    script's own header calls the worst outcome."""
    src = _bench_root(tmp_path)
    for p in sorted((src / "model-probes").rglob("*"), reverse=True):
        p.rmdir() if p.is_dir() else p.unlink()
    (src / "model-probes").rmdir()

    result = _snapshot(src, tmp_path / "snapshots", tmp_path / "runs-archive")
    assert result.returncode == 1, result.stdout
    assert "NO per-run evidence archived" in result.stdout
