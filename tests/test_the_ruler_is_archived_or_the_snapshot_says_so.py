"""The AlgoTune half of the snapshot could not make the exit code move at all.

`snapshot.sh`'s header calls the exit code the CLAIM: "anything this snapshot could not copy --
absent, or attempted and failed -- makes it a partial one". Measured on 35124d05, section 1b (our
own repo) contains two `SHORT=$((SHORT + 1))` and section 1 (the AlgoTune checkout) contains none.
Driven on a synthetic BENCH_ROOT with `AlgoTune/.git` removed and every other source in place:

    rc=0
    no MISSING line anywhere in the output
    PROVENANCE.txt: "AlgoTune:" and nothing after the colon

So a snapshot holding no copy whatever of the checkout every speedup on this box was measured
against reported complete success, `snapshot_timer.sh` recorded its fingerprint as archived, and
`campaign.sh`'s `|| echo "(snapshot failed...)"` arm could not fire. This is the defect that was
fixed for our own repo on 2026-08-30, in the half that was left behind.

The second half of the same blindness is the FALLBACK GATE. Section 1 degrades to a tar of tracked
files when the bundle cannot be made, and that fallback ran only `if [ ! -s "$OUT/AlgoTune.bundle" ]`
-- i.e. only when the failed attempt left an EMPTY file. Any failure that leaves a prefix behind,
which is what an ENOSPC part-way through a write to the geesefs mount produces, suppressed the
fallback and was counted as nothing: the archive kept a file named `AlgoTune.bundle` that no
`git clone` can read, and said the snapshot was complete.

WHY THE PARTIAL BUNDLE IS PRODUCED BY A `git` SHIM: the shape has to be "the command failed AND left
bytes", and modern git cleans up after itself when its own write fails -- driven 2026-08-31 under
`ulimit -f 1`, git exits 1 and removes the file, which is the one case the old `[ ! -s ]` gate
handled. The gate's blind spot is therefore reachable only when something else truncates the file:
a FUSE mount that took the rename and then failed to flush, a filesystem full at close(), an older
git. The shim writes a prefix and exits non-zero, which is exactly that, and nothing about the
assertions depends on how the prefix got there.

And `git bundle verify` is not the missing check: measured 2026-08-31, it returns 0 both for a
bundle truncated to its first 200 bytes and for one made from a `--depth 1` clone -- the case this
script's own header warns about ("the backup looks fine and is not one"). It reads the header and
the prerequisites, not the pack.
"""
import subprocess
from pathlib import Path

from tests.test_snapshot_carries_the_repo_and_the_runs import SNAPSHOT, _bench_root, _rmtree

# The failure `[ ! -s ]` cannot see: non-zero exit, bytes on disk.
_GIT_SHIM = """#!/bin/bash
if [ "$1" = "bundle" ] && [ "$2" = "create" ] && [[ "$PWD" == *AlgoTune ]]; then
  printf 'PACK\\x00a-prefix-and-no-more' > "$3"
  exit 1
fi
exec /usr/bin/git "$@"
"""

_TAR_SHIM = """#!/bin/bash
exit 1
"""


def _shim_bin(tmp_path, **shims):
    binroot = tmp_path / "shims"
    binroot.mkdir(exist_ok=True)
    for name, body in shims.items():
        (binroot / name).write_text(body)
        (binroot / name).chmod(0o755)
    return binroot


def _snapshot(src, dest, archive, path_prefix=None):
    path = "/usr/bin:/bin" if path_prefix is None else f"{path_prefix}:/usr/bin:/bin"
    return subprocess.run(
        ["bash", str(SNAPSHOT), str(dest)],
        env={"PATH": path, "HOME": str(src.parent), "BENCH_ROOT": str(src),
             "SNAPSHOT_RUNS_ARCHIVE": str(archive)},
        capture_output=True, text=True, timeout=300)


def test_an_absent_algotune_checkout_is_named_and_counted(tmp_path):
    """The ordinary morning-after state: `/var/tmp` is the container's writable layer, so after a
    restart the checkout is simply gone until someone re-clones it. A snapshot taken in that window
    carried our bundle, the meter, the logs and the runs, looked entirely healthy, and held nothing
    of the ruler -- while PROVENANCE.txt printed an `AlgoTune:` line with nothing after it."""
    src = _bench_root(tmp_path)
    _rmtree(src / "AlgoTune" / ".git")
    dest = tmp_path / "snapshots"

    result = _snapshot(src, dest, tmp_path / "runs-archive")

    assert result.returncode == 1, (
        "the checkout every speedup on this box was measured against is not in this snapshot and "
        "the snapshot exits 0, so the timer records the fingerprint as archived\n" + result.stdout)
    assert "MISSING" in result.stdout and "AlgoTune.bundle" in result.stdout, result.stdout
    assert "is NOT archived" in result.stdout, result.stdout
    assert "INCOMPLETE SNAPSHOT: 1 source(s)" in result.stdout, result.stdout

    # The rest of the archive is unaffected: it is the CLAIM being corrected, not the copy.
    out = next(dest.glob("2*"))
    assert (out / "looplab.bundle").exists(), result.stdout
    # And the receipt is still a receipt for nothing, which is why the alarm has to exist: the
    # PROVENANCE line names the checkout and has nothing after the colon.
    prov = [ln for ln in (out / "PROVENANCE.txt").read_text().splitlines()
            if ln.startswith("AlgoTune:")]
    assert [ln.rstrip() for ln in prov] == ["AlgoTune:"], prov


def test_a_bundle_that_failed_leaving_a_prefix_does_not_suppress_the_tar(tmp_path):
    """`[ ! -s ]` read a prefix as a finished bundle. The status of the command is the question."""
    src = _bench_root(tmp_path)
    dest = tmp_path / "snapshots"
    binroot = _shim_bin(tmp_path, git=_GIT_SHIM)

    result = _snapshot(src, dest, tmp_path / "runs-archive", path_prefix=str(binroot))

    out = next(dest.glob("2*"))
    tar = out / "AlgoTune-tracked.tar.gz"
    assert tar.exists() and tar.stat().st_size > 0, (
        "the bundle failed and left bytes behind, so the fallback never ran; the snapshot holds a "
        "file called AlgoTune.bundle that no git clone can read and nothing else\n" + result.stdout)
    assert not (out / "AlgoTune.bundle").exists(), (
        "a partial bundle was left in the archive -- that is a failure wearing the name of a "
        "backup, and it is what the old gate mistook for one\n" + result.stdout)
    assert "FAILED" in result.stdout, result.stdout

    # A tar of tracked files IS restorable, so this degradation is not a shortfall -- making it one
    # would put a permanently-shallow checkout into the unbounded re-snapshot loop that cost 3.0 GB.
    assert result.returncode == 0, result.stdout
    assert "NO history" in result.stdout, (
        "the log does not say what the degradation costs; an operator reading it cannot tell that "
        "the upstream sha a published number cites is named and not carried\n" + result.stdout)
    assert subprocess.run(["tar", "-tzf", str(tar)], capture_output=True).returncode == 0


def test_when_neither_the_bundle_nor_the_tar_can_be_written_the_snapshot_says_so(tmp_path):
    """The end of the road: no bundle, no tar, and the exit code is the only thing left that can
    say the ruler is not in this archive."""
    src = _bench_root(tmp_path)
    dest = tmp_path / "snapshots"
    binroot = _shim_bin(tmp_path, git=_GIT_SHIM, tar=_TAR_SHIM)

    result = _snapshot(src, dest, tmp_path / "runs-archive", path_prefix=str(binroot))

    assert result.returncode == 1, (
        "neither form of the AlgoTune checkout reached the archive and the snapshot claims to be "
        "complete\n" + result.stdout)
    assert "BOTH FAILED" in result.stdout, result.stdout
    assert "INCOMPLETE SNAPSHOT: 1 source(s)" in result.stdout, result.stdout
    out = next(dest.glob("2*"))
    assert not (out / "AlgoTune.bundle").exists(), result.stdout
