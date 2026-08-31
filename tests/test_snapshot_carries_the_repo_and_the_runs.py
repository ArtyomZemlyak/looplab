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



# ------------------------------------------------------------------ the archive ACROSS snapshots
#
# Everything above runs the script ONCE. Three claims in the runs-archive block are about what the
# SECOND run does -- "copied ONCE, not once per snapshot", `cp -ru` keeping the sync incremental
# "because a LIVE run's directory grows while this script is reading it", and an archive that
# accumulates in a sibling the prune never touches -- and no test had ever invoked `snapshot.sh`
# twice into the same archive, so none of them was checked.


def _run_log(src, tree, probe, run):
    return src / tree / probe / "runs" / run / "run"


def test_a_finished_run_is_copied_once_not_once_per_snapshot(tmp_path):
    """`cp -ru`, not `cp -r`.

    A finished run is immutable, so re-copying it every thirty minutes is seven copies of the same
    bytes onto a shared S3-backed FUSE mount -- the cost this whole sibling-archive design exists
    to avoid, and the reason the runs do not simply ride inside each rotating snapshot.

    Whether `cp` wrote the file again is observed by making the archived copy DISTINGUISHABLE from
    its source and newer than it, which is exactly the condition `-u` tests. A second pass that
    re-copies overwrites the sentinel; one that honours `-u` leaves it alone.
    """
    src = _bench_root(tmp_path)
    dest, archive = tmp_path / "snapshots", tmp_path / "runs-archive"
    assert _snapshot(src, dest, archive).returncode == 0

    archived = archive / "model-probes" / "dsPde3" / "runs" / "r1" / "run" / "events.jsonl"
    source = _run_log(src, "model-probes", "dsPde3", "r1") / "events.jsonl"
    archived.write_text(archived.read_text() + '{"sentinel": "not rewritten"}\n')
    import os
    os.utime(archived, ns=(source.stat().st_mtime_ns + 60 * 10**9,) * 2)

    result = _snapshot(src, dest, archive)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "sentinel" in archived.read_text(), (
        "the second snapshot re-copied a finished run that had not changed; at a thirty-minute "
        "cadence that is the same bytes written to geesefs eight times a day\n" + result.stdout)


def test_the_archive_is_the_union_of_what_the_box_has_ever_held(tmp_path):
    """Incremental must not mean partial, and accumulating must not mean re-copying.

    The `-u` above is only safe if the other half holds: a run that GREW between snapshots is
    picked up (the live-run case the comment names), a run that appeared is picked up, and a run
    that has since been deleted from BENCH_ROOT stays archived -- the archive is a sibling the
    prune never reaches, and that is what makes it the durable half.
    """
    import os
    src = _bench_root(tmp_path)
    dest, archive = tmp_path / "snapshots", tmp_path / "runs-archive"
    assert _snapshot(src, dest, archive).returncode == 0
    first_manifest = (next(iter(sorted(dest.glob("2*")))) / "runs-manifest.txt").read_text()
    assert "model-probes 1" in first_manifest, first_manifest

    # A live run appends while the box keeps working, and a second probe starts.
    live = _run_log(src, "model-probes", "dsPde3", "r1") / "events.jsonl"
    live.write_text(live.read_text() + '{"type": "llm_usage", "data": {"cost": 0.0031}}\n')
    os.utime(live, ns=(live.stat().st_mtime_ns + 60 * 10**9,) * 2)
    second = _run_log(src, "model-probes", "dsHull", "r1")
    second.mkdir(parents=True)
    (second / "events.jsonl").write_text('{"type": "node_evaluated"}\n')

    result = _snapshot(src, dest, archive)
    assert result.returncode == 0, result.stdout + result.stderr

    archived = archive / "model-probes" / "dsPde3" / "runs" / "r1" / "run" / "events.jsonl"
    assert "0.0031" in archived.read_text(), (
        "a run that grew between snapshots was not re-read, so the archive holds a truncated "
        "prefix of the evidence\n" + result.stdout)
    assert (archive / "model-probes" / "dsHull" / "runs" / "r1" / "run" / "events.jsonl").exists()

    # Each snapshot records what the archive held AT ITS MOMENT, so a restorer can tell a run that
    # was never archived from one archived and later lost.
    latest = sorted(dest.glob("2*"))[-1]
    assert "model-probes 2" in (latest / "runs-manifest.txt").read_text()

    # And what BENCH_ROOT has since dropped is still there: /var/tmp is the container's writable
    # layer, so "gone from the box" is the ordinary case, not the exotic one.
    _rmtree(src / "model-probes" / "dsPde3")
    assert _snapshot(src, dest, archive).returncode == 0
    assert "0.0031" in archived.read_text(), "the archive lost a run the box no longer has"


def _rmtree(path):
    for child in sorted(path.rglob("*"), reverse=True):
        child.rmdir() if child.is_dir() else child.unlink()
    path.rmdir()


# --------------------------------------------------------------------------- the two alarms in 1b
#
# Section 1b exists because of the loss above, and it has exactly two ways of saying that the loss
# is happening again: the bundle was attempted and failed, or there was no checkout to bundle. Both
# were unreachable from this suite. Driven 2026-08-31: with the `|| { echo "BUNDLE FAILED ..."; }`
# arm replaced by `|| true` AND the whole `else ... MISSING looplab.bundle` branch deleted, the four
# snapshot/timer files stayed at 48 passed -- a snapshot that carries no commit of ours exits 0 and
# tells `snapshot_timer.sh` to record the fingerprint as archived, which is the 2026-08-29 shape
# reproduced by the very script written to prevent it.
#
# The alarm is the ONLY thing between an operator and that outcome, because there is no fallback
# here: section 1 above degrades to a tar of tracked files when its bundle fails, 1b does not.


def test_a_bundle_that_could_not_be_made_is_not_a_silent_success(tmp_path):
    """`.git` is there, HEAD names objects nothing contains -- the wreck, exactly.

    That is not a hypothetical corruption: it is what `af0e4772` became at 19:15 on 2026-08-29,
    a name with no object behind it. The `if [ -d .git ]` test above passes on it, `git bundle
    create` cannot, and what lands in the destination is `PROVENANCE.txt` naming a HEAD and no
    bundle -- a receipt for a backup that was not taken, printed by the script that exists to take
    it.
    """
    src = _bench_root(tmp_path)
    for obj in sorted((src / "looplab" / ".git" / "objects").rglob("*"), reverse=True):
        obj.unlink() if obj.is_file() else None

    result = _snapshot(src, tmp_path / "snapshots", tmp_path / "runs-archive")

    assert result.returncode == 1, (
        "the bundle could not be made and the snapshot still claims to be complete, so "
        "snapshot_timer.sh records this fingerprint as archived and never retries\n" + result.stdout)
    assert "BUNDLE FAILED" in result.stdout, result.stdout
    assert "OUR COMMITS ARE NOT IN THIS SNAPSHOT" in result.stdout, result.stdout
    assert "INCOMPLETE SNAPSHOT: 1 source(s)" in result.stdout, result.stdout

    # And the shape the alarm is describing is real: a receipt, and nothing behind it.
    out = next((tmp_path / "snapshots").glob("2*"))
    assert (out / "PROVENANCE.txt").exists()
    assert not (out / "looplab.bundle").exists() or not (out / "looplab.bundle").stat().st_size


def test_a_looplab_checkout_that_is_not_there_at_all_is_not_a_silent_success(tmp_path):
    """The other arm: nothing to bundle, so NO commit of ours is archived.

    This is the ordinary morning-after state of `BENCH_ROOT` -- `/var/tmp` is the container's own
    writable layer, so after a restart the checkout is simply absent until someone re-clones it.
    A snapshot taken in that window carries the AlgoTune bundle, the meter and the logs, looks
    entirely healthy, and holds not one line of our work.
    """
    src = _bench_root(tmp_path)
    _rmtree(src / "looplab" / ".git")

    result = _snapshot(src, tmp_path / "snapshots", tmp_path / "runs-archive")

    assert result.returncode == 1, (
        "there is no repository of ours on this box and the snapshot exits 0\n" + result.stdout)
    assert "MISSING" in result.stdout and "looplab.bundle" in result.stdout, result.stdout
    assert "NO commit of ours is archived" in result.stdout, result.stdout
    assert "INCOMPLETE SNAPSHOT: 1 source(s)" in result.stdout, result.stdout

    out = next((tmp_path / "snapshots").glob("2*"))
    assert not (out / "looplab.bundle").exists(), result.stdout
    # The rest of the archive is unaffected -- it is the CLAIM that is being corrected, not the copy.
    assert (out / "AlgoTune.bundle").exists(), result.stdout


def test_an_idle_box_is_not_reported_as_a_shortfall(tmp_path):
    """A claim that fires unconditionally is not a claim, and this one had a running cost.

    Measured 2026-08-31 on a freshly rebuilt box: with no campaign and no runs yet, every cycle
    exited 1, `snapshot_timer.sh` refused to record the fingerprint -- correctly, by its own rule
    that an incomplete archive is not done -- and so re-wrote a 110 MB snapshot every thirty
    minutes and never pruned, because the prune sits downstream of the completeness check. Nine
    snapshots and 3.0 GB before anyone looked. An alarm that is always on is one an operator learns
    to scroll past, which is the same failure as no alarm at all.
    """
    src = _bench_root(tmp_path)
    _rmtree(src / "campaign-final")
    _rmtree(src / "model-probes")

    result = _snapshot(src, tmp_path / "snapshots", tmp_path / "runs-archive")
    assert result.returncode == 0, result.stdout
    assert "idle box" in result.stdout
    assert "INCOMPLETE" not in result.stdout

    # It is only the ALARM that is withheld. The checkouts still travel, which is the whole point.
    out = next((tmp_path / "snapshots").glob("2*"))
    assert (out / "looplab.bundle").exists()



def _fake_snapshot(dest, stamp, *, measured):
    """A previous snapshot, of the two kinds this box actually produces."""
    d = dest / stamp
    d.mkdir(parents=True)
    (d / "AlgoTune.bundle").write_text("x" * 64)
    (d / "looplab.bundle").write_text("x" * 64)
    if measured:
        (d / "campaign-final").mkdir()
        (d / "campaign-final" / "B-task.final.json").write_text('{"speedup": 2.45}')
        (d / "runs-manifest.txt").write_text("model-probes 17 /archive/model-probes\n")
    else:
        (d / "runs-manifest.txt").write_text("model-probes 0 /archive/model-probes\n")
    return d


def test_the_prune_spends_empty_snapshots_before_it_touches_a_measured_one(tmp_path):
    """Age is not worth, and this prune used to act as if it were.

    `ls | head -n -KEEP` deletes the oldest directories, full stop. After the 2026-08-29 restart the
    oldest on this box were the eight snapshots holding `campaign-final/` and the meter ledgers --
    the finished paired campaign, twenty task-arms across both arms -- while every snapshot taken
    since holds two git bundles and nothing measured, because nothing has been measured since. Nine
    of those arrive within five hours at a thirty-minute cadence, and the prune would have spent the
    irreplaceable to make room for the reproducible, silently, as ordinary successful operation.
    """
    src = _bench_root(tmp_path)
    dest, archive = tmp_path / "snapshots", tmp_path / "runs-archive"
    keep_a = _fake_snapshot(dest, "20260829-154101", measured=True)
    keep_b = _fake_snapshot(dest, "20260829-191124", measured=True)
    doomed_a = _fake_snapshot(dest, "20260831-010635", measured=False)
    doomed_b = _fake_snapshot(dest, "20260831-010917", measured=False)

    result = subprocess.run(
        ["bash", str(SNAPSHOT), str(dest)],
        env={"PATH": "/usr/bin:/bin", "HOME": str(src.parent), "BENCH_ROOT": str(src),
             "SNAPSHOT_RUNS_ARCHIVE": str(archive), "SNAPSHOT_KEEP": "3"},
        capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stdout + result.stderr

    assert keep_a.exists() and keep_b.exists(), (
        "the campaign was deleted to make room for a snapshot of an idle box\n" + result.stdout)
    assert not doomed_a.exists() and not doomed_b.exists(), result.stdout
    assert "carries no campaign and no runs" in result.stdout


def test_when_only_measured_snapshots_are_left_the_prune_says_what_it_is_deleting(tmp_path):
    """Deleting a measured snapshot is a real loss, not housekeeping, so it may not look like
    housekeeping in the log."""
    src = _bench_root(tmp_path)
    dest, archive = tmp_path / "snapshots", tmp_path / "runs-archive"
    oldest = _fake_snapshot(dest, "20260829-154101", measured=True)
    _fake_snapshot(dest, "20260829-191124", measured=True)

    result = subprocess.run(
        ["bash", str(SNAPSHOT), str(dest)],
        env={"PATH": "/usr/bin:/bin", "HOME": str(src.parent), "BENCH_ROOT": str(src),
             "SNAPSHOT_RUNS_ARCHIVE": str(archive), "SNAPSHOT_KEEP": "2"},
        capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not oldest.exists(), result.stdout
    assert "WITH MEASUREMENTS" in result.stdout, result.stdout


def test_the_prune_never_spends_the_snapshot_this_run_just_wrote(tmp_path):
    """"The newest snapshot is never a candidate whatever it holds" -- deletable with no red test.

    Driven 2026-08-31: with `[ "$D" = "$NEWEST" ] && continue` removed from the prune loop, the
    whole file stayed at 10 passed. The rule reads like belt-and-braces beside the measured/
    unmeasured sort, and it is not: the two interact exactly backwards. On an idle box the snapshot
    THIS run just wrote is by definition unmeasured -- no campaign, no runs manifest -- so it sorts
    to the front of the spend order, ahead of every older measured snapshot the sort exists to
    protect. The run then archives the box, deletes its own archive, and exits 0.

    The rule is what makes "unmeasured is cheap" safe to say: a snapshot is only cheap because a
    newer one describes the box, and for the newest one nothing does. It is the current state of
    the box.
    """
    src = _bench_root(tmp_path)
    _rmtree(src / "campaign-final")
    _rmtree(src / "model-probes")                    # idle: what this run writes is unmeasured
    dest, archive = tmp_path / "snapshots", tmp_path / "runs-archive"
    old_a = _fake_snapshot(dest, "20260829-154101", measured=True)
    old_b = _fake_snapshot(dest, "20260829-191124", measured=True)

    result = subprocess.run(
        ["bash", str(SNAPSHOT), str(dest)],
        env={"PATH": "/usr/bin:/bin", "HOME": str(src.parent), "BENCH_ROOT": str(src),
             "SNAPSHOT_RUNS_ARCHIVE": str(archive), "SNAPSHOT_KEEP": "2"},
        capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stdout + result.stderr

    written = [d for d in sorted(dest.glob("2*")) if d not in (old_a, old_b)]
    assert written, ("the run pruned the snapshot it had just written -- it archived the box and "
                     "then deleted the archive, and exited 0\n" + result.stdout)
    assert (written[0] / "looplab.bundle").exists(), result.stdout

    # Having refused the cheap one, it had to reach a measured snapshot -- and to say so.
    assert "WITH MEASUREMENTS" in result.stdout, result.stdout
    assert not old_a.exists() and old_b.exists(), result.stdout
    assert written[0].name not in result.stdout.split("pruning")[-1], result.stdout


def test_a_box_running_probes_without_a_campaign_is_complete(tmp_path):
    """The live case that broke the previous version of this rule within the hour.

    Campaigns and probe runs are two independent ways of measuring here. Requiring both made every
    cycle on a probes-only box exit 1, which made `snapshot_timer.sh` refuse the fingerprint and
    re-write a 110 MB snapshot every thirty minutes without pruning -- the same unbounded loop the
    "neither exists" condition had just been introduced to stop, resurfacing under a new name as
    soon as two probes started and `model-probes/` appeared.

    What the archive owes is everything that EXISTS. An absent mode is reported so a restorer knows
    what this box was doing; it is not a shortfall.
    """
    src = _bench_root(tmp_path)
    _rmtree(src / "campaign-final")

    result = _snapshot(src, tmp_path / "snapshots", tmp_path / "runs-archive")
    assert result.returncode == 0, result.stdout
    assert "INCOMPLETE" not in result.stdout
    assert "running probes, not a campaign" in result.stdout

    # And the runs it DOES have still travel -- "not a shortfall" may never come to mean "skipped".
    assert (tmp_path / "runs-archive" / "model-probes" / "dsPde3" / "runs" / "r1" / "run"
            / "events.jsonl").exists()


def test_a_named_source_that_is_not_there_is_counted(tmp_path):
    """One half of the exit-code contract: a source the script NAMES and cannot find."""
    src = _bench_root(tmp_path)
    _rmtree(src / "AlgoTune" / "reports")

    result = _snapshot(src, tmp_path / "snapshots", tmp_path / "runs-archive")
    assert result.returncode == 1, result.stdout
    assert "INCOMPLETE SNAPSHOT: 1 source(s)" in result.stdout


def test_a_source_that_exists_and_cannot_be_read_is_still_a_shortfall(tmp_path):
    """The alarm that must survive all this: `copy` counts a source that is THERE and unreadable.
    That is the case the exit code was built for, and it is untouched by mode-awareness.

    IT HAS TO BE UNREADABLE, not absent. Until 2026-08-31 this test DELETED the directory, so it
    exercised the `MISSING` arm one branch above and said nothing at all about the `COPY FAILED`
    arm its own name and docstring describe. Driven: with the whole `cp`-failure branch cut out of
    `snapshot.sh` -- the counter, the message and all -- the old body still passed. That branch is
    the one the header calls the worst outcome ("a snapshot that copied nothing at all still
    printed PROVENANCE.txt and exited 0"), and on a geesefs S3 mount a part-way `cp` failure is the
    ORDINARY error rather than an exotic one.
    """
    src = _bench_root(tmp_path)
    summary = src / "AlgoTune" / "reports" / "agent_summary.json"
    summary.write_text('{"score": 1.0}\n')
    # The unreadable thing is the FILE, not the directory holding it. `cp -r` over an unreadable
    # DIRECTORY creates the destination with the source's own 0o000 mode before it fails to read
    # it, and pytest's tmp-dir cleanup then cannot remove what it made -- the test would pass and
    # leave `d---------` behind for every later session. Same arm of `copy()`, no litter.
    summary.chmod(0o000)                    # there, named, and unreadable -- the geesefs case
    try:
        result = _snapshot(src, tmp_path / "snapshots", tmp_path / "runs-archive")
    finally:
        summary.chmod(0o644)

    assert result.returncode == 1, result.stdout
    assert "COPY FAILED" in result.stdout, (
        "a source that is present and unreadable was archived silently\n" + result.stdout)
    assert "MISSING" not in result.stdout, "it is not absent; it is unreadable\n" + result.stdout
    assert "INCOMPLETE SNAPSHOT: 1 source(s)" in result.stdout
