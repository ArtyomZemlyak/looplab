"""A snapshot's exit code is a claim that the bytes are safe. Three ways it lied.

All three were MEASURED on 2026-08-31, not reasoned about:

  A. `/home/jovyan/data` is a separate fuseblk (geesefs/S3) mounted over a tmpfs parent. Point the
     snapshot at a destination whose directory has vanished and `mkdir -p` recreates it, 111 MB get
     written, and it exits 0 -- onto storage that dies with the pod. This is the 2026-08-29 failure
     that cost 37 commits, wearing a success code.

  B. Two snapshots started in the same second share $STAMP and therefore share one output
     directory. Observed: rc=0 and rc=1, one 30-file tree interleaved from both, survivor reports
     success.

  C. `.env` was neither copied nor named, so a snapshot could not say which settings produced its
     numbers -- and line 77 of that file (LOOPLAB_LLM_STREAM=false) silently decides whether 28 %
     of calls die at nginx's 300 s ceiling.

Each test below reddens if its fix is removed from benchmarks/snapshot.sh.
"""
import os
import subprocess
import tempfile
import textwrap
from pathlib import Path

import pytest

from _bench_fixtures import bench_root

REPO = Path(__file__).resolve().parents[1]
SNAPSHOT = REPO / "benchmarks" / "snapshot.sh"


# The SOURCE every test here snapshots, built once per session and never the box's own.
# `os.environ.setdefault("BENCH_ROOT", "/var/tmp/looplab-bench")` stood here until 2026-09-07 and it
# is why eleven of these tests were red on any machine without an arena: with no such root the
# script reported six MISSING sources and exited 1, so eleven assertions about REFUSALS, locks and
# the environment record failed for a reason none of them is about. A test whose subject is what a
# script refuses has to own its inputs. The builder is shared with
# `test_snapshot_carries_the_repo_and_the_runs.py` rather than copied — see `tests/_bench_fixtures.py`.
_SOURCE: list = []


@pytest.fixture(scope="session", autouse=True)
def _synthetic_bench_source(tmp_path_factory):
    _SOURCE.append(bench_root(tmp_path_factory.mktemp("bench-source")))
    yield
    _SOURCE.clear()


def _run(dest, env=None, timeout=600, src=None):  # noqa: D401 - timeout is raised by the lock tests
    e = dict(os.environ)
    # SET, not `setdefault`: an operator running the suite on the bench stand has BENCH_ROOT
    # exported, and inheriting it would put these tests back on the box's live tree — the exact
    # dependency this removes, and invisibly, since it would still pass there.
    e["BENCH_ROOT"] = str(src or _SOURCE[0])
    if env:
        e.update(env)
    return subprocess.run(
        ["bash", str(SNAPSHOT), str(dest)],
        capture_output=True, text=True, timeout=timeout, env=e,
    )


def test_a_refuses_a_store_whose_sentinel_is_gone():
    """The mount is gone but the path is writable: refuse, do not write a doomed backup."""
    with tempfile.TemporaryDirectory() as td:
        store = Path(td) / "looplab-bench"      # stands in for the unmounted volume root
        store.mkdir()
        (store / "snapshots").mkdir()
        # Non-empty and sentinel-less == what an unmounted geesefs looks like from above.
        (store / "runs-archive").mkdir()

        r = _run(store / "snapshots")

    assert r.returncode != 0, (
        "snapshot exited 0 against a store with no .persistent-store-id -- "
        "this is the 2026-08-29 evaporating-backup bug\n" + r.stdout[-2000:] + r.stderr[-2000:]
    )
    assert "not mounted" in r.stderr.lower() or "persistent-store-id" in r.stderr, (
        "refused, but not for the stated reason:\n" + r.stderr[-2000:]
    )


def test_a_adopts_a_genuinely_empty_store_and_leaves_the_sentinel():
    """A brand-new store must still work -- the check must not be a wall against first use."""
    with tempfile.TemporaryDirectory() as td:
        store = Path(td) / "looplab-bench"
        store.mkdir()                            # empty: nothing to mistake for an unmount
        dest = store / "snapshots"

        r = _run(dest)

        assert r.returncode == 0, (
            "refused a legitimately empty new store:\n" + r.stdout[-2000:] + r.stderr[-2000:]
        )
        assert (store / ".persistent-store-id").is_file(), \
            "adopted the store but left no sentinel, so the next run will refuse it"


def test_b_two_snapshots_at_once_do_not_share_one_directory():
    """Same-second concurrency must not interleave two trees into one output directory."""
    with tempfile.TemporaryDirectory() as td:
        store = Path(td) / "looplab-bench"
        store.mkdir()
        dest = store / "snapshots"
        dest.mkdir()
        (store / ".persistent-store-id").write_text("test")

        env = dict(os.environ)
        env.setdefault("BENCH_ROOT", "/var/tmp/looplab-bench")
        procs = [
            subprocess.Popen(["bash", str(SNAPSHOT), str(dest)],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, env=env)
            for _ in range(2)
        ]
        outs = [p.communicate(timeout=900) for p in procs]
        rcs = [p.returncode for p in procs]

        trees = sorted(d for d in dest.iterdir() if d.is_dir() and d.name[0].isdigit())

        # Exactly one may run at a time. The other either waits and gets its own stamp, or
        # declines. What must never happen is two runs writing into one directory.
        succeeded = [i for i, rc in enumerate(rcs) if rc == 0]
        assert len(trees) >= len(succeeded) or len(succeeded) <= 1, (
            f"{len(succeeded)} runs reported success but only {len(trees)} trees exist -- "
            f"they shared a directory. rcs={rcs}\n" + str(outs)[-2000:]
        )
        for t in trees:
            assert not (t / ".partial").exists(), f"{t} left a partial marker"


def test_c_records_the_settings_but_never_the_key():
    """A measurement's configuration must be recoverable from the snapshot, minus the secret."""
    with tempfile.TemporaryDirectory() as td:
        store = Path(td) / "looplab-bench"
        store.mkdir()
        dest = store / "snapshots"
        dest.mkdir()
        (store / ".persistent-store-id").write_text("test")

        r = _run(dest, env={
            "LOOPLAB_LLM_STREAM": "1",
            "LOOPLAB_LLM_API_KEY": "sk-do-not-leak-me-0123456789",
        })
        assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]

        trees = [d for d in dest.iterdir() if d.is_dir() and d.name[0].isdigit()]
        assert trees, "no snapshot tree"
        envfile = trees[0] / "ENVIRONMENT.txt"

        assert envfile.is_file(), (
            "no ENVIRONMENT.txt -- the snapshot again cannot say which settings produced its numbers"
        )
        body = envfile.read_text()

        assert "LOOPLAB_LLM_STREAM" in body, (
            "ENVIRONMENT.txt does not record LOOPLAB_LLM_STREAM, the one setting that decides "
            "whether 28 % of calls die at the 300 s ceiling:\n" + body[:2000]
        )
        assert "sk-do-not-leak-me-0123456789" not in body, \
            "the API key was written into a snapshot bound for S3"
        assert "chars>" in body, "nothing was redacted; the redaction path never ran"


def test_c_the_header_names_what_it_omits():
    """A silent omission is the thing being fixed; the omission must be written down."""
    head = SNAPSHOT.read_text()[:4000]
    assert ".env" in head and "NOT copied" in head, (
        "snapshot.sh's header lists what it deliberately skips but still does not name .env"
    )


def test_b2_a_taken_stamp_does_not_become_a_shared_directory():
    """The stamp is not an identity, and this is the half the concurrency test cannot see.

    Found by mutation on 2026-08-31: deleting the uniquifying loop left all four tests above green.
    The concurrency test only proves two SIMULTANEOUS runs do not collide -- the flock already
    guarantees that -- but two runs a second apart (or two under the lock, back to back) still
    resolve to the same $STAMP and, without the loop, silently write into one tree. So take the
    name first and check the snapshot goes somewhere else.
    """
    import time

    with tempfile.TemporaryDirectory() as td:
        store = Path(td) / "looplab-bench"
        store.mkdir()
        dest = store / "snapshots"
        dest.mkdir()
        (store / ".persistent-store-id").write_text("test")

        # Claim the name this run is about to want, and make it recognisable.
        stamp = time.strftime("%Y%m%d-%H%M%S")
        squatter = dest / stamp
        squatter.mkdir()
        (squatter / "PRIOR.txt").write_text("written by an earlier snapshot\n")

        r = _run(dest)
        assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]

        assert sorted(p.name for p in squatter.iterdir()) == ["PRIOR.txt"], (
            f"the run wrote into an existing snapshot directory: "
            f"{sorted(p.name for p in squatter.iterdir())}"
        )
        others = [d for d in dest.iterdir() if d.is_dir() and d != squatter and d.name[0].isdigit()]
        assert others, "the run reported success but produced no snapshot directory of its own"
        assert (others[0] / "PROVENANCE.txt").is_file(), \
            f"{others[0].name} is not a real snapshot"


# ---------------------------------------------------- the lock may not claim success over nothing
#
# Measured 2026-09-01, and both paths were introduced by the flock added to fix a DIFFERENT defect:
#   * destination not creatable -> the `exec 9>` redirect fails, flock gets a bad fd, and the script
#     printed "another snapshot is running (waited 60s)" INSTANTLY and exited 0 having written
#     nothing. A lie about the reason on top of a lie about the outcome.
#   * lock genuinely held -> waited its 60 s and exited 0 having written nothing.
# `snapshot_timer.sh` records its fingerprint on rc=0 and does not retry, so either path is the
# 2026-08-29 failure -- an empty backup under a success code -- reintroduced by its own repair.


# THE REFUSAL HAS A FALSIFIER EVERY SUITE CAN RUN, since 2026-09-08. It used to have exactly one —
# `chmod 0555` — and root ignores the write bit, so on a root container (which is where this suite
# runs) the only test of this rung SKIPPED and the branch was undriven. The rung's own condition is
# not "the directory is unwritable"; it is `! : > "$DEST/.snapshot.lock"` — the destination cannot
# hold a lock file — and permission is only one way to fail that. A lock PATH that is a directory
# fails it as EISDIR for root and non-root alike, so the same branch is driven either way and the
# two spellings share one body below.
#
# Rejected: making $DEST itself a file (ENOTDIR aborts one line earlier, at `mkdir -p`, so it never
# reaches this branch and would pin a different refusal under this name), an immutable attribute
# (`chattr +i` needs CAP_LINUX_IMMUTABLE and a filesystem that has it — unavailable in a plain
# container), and a read-only bind mount (needs CAP_SYS_ADMIN).
def _assert_it_refused_without_claiming_busy(r, dest):
    assert r.returncode != 0, (
        "a destination that cannot even hold a lock file reported SUCCESS:\n" + r.stdout + r.stderr
    )
    assert "NOTHING WAS WRITTEN" in r.stderr, (
        "it failed, but not in words that stop somebody reading it as a skip:\n" + r.stderr
    )
    assert "another snapshot is running" not in r.stderr, (
        "it still blames a concurrent snapshot for a permission problem"
    )
    # …and the sentence must be TRUE, not merely printed. Asserting the words alone would pass a
    # script that wrote a half tree and then said it had not: `snapshot_timer.sh` reads the exit
    # code, but an operator reads this line, and the 2026-08-29 failure was believing one.
    if dest.is_dir():
        wrote = sorted(e.name for e in dest.iterdir() if e.name != ".snapshot.lock")
        assert wrote == [], f"it said NOTHING WAS WRITTEN and wrote {wrote}"


def test_a_destination_that_cannot_hold_a_lock_file_is_a_failure_not_a_skip(tmp_path):
    """THE ROOT-RUNNABLE SPELLING of the rung, and the reason this branch was undriven for a week.

    `$DEST/.snapshot.lock` is a DIRECTORY, so `: > "$DEST/.snapshot.lock"` fails with EISDIR for
    every user including root — which is exactly the condition the branch guards on, and exactly
    the "destination not creatable" case its comment measured on 2026-09-01 (the redirect fails,
    flock gets a bad fd, and the script printed "another snapshot is running" instantly and exited
    0: a lie about the reason on top of a lie about the outcome).

    MUTATION: delete the `if ! : > "$DEST/.snapshot.lock"` guard from `benchmarks/snapshot.sh` and
    `exec 9>` fails, flock reports on a bad fd, and this run claims busy or claims success.
    """
    store = tmp_path / "looplab-bench"
    dest = store / "snapshots"
    dest.mkdir(parents=True)
    (store / ".persistent-store-id").write_text("test")
    # The name the script must be able to create as a FILE, occupied by a directory.
    (dest / ".snapshot.lock").mkdir()

    r = _run(dest)
    _assert_it_refused_without_claiming_busy(r, dest)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the write bit, so there is no refusal")
def test_an_unwritable_destination_is_a_failure_not_a_skip(tmp_path):
    """THE PERMISSION SPELLING of the same rung — kept, but no longer the only one.

    FALSELY GREEN UNTIL 2026-09-07, and only on a box with no arena. Root bypasses directory write
    permission, so `chmod 0555` refuses this process nothing and the snapshot writes happily. It
    passed anyway because `_run` was pointed at the box's own `/var/tmp/looplab-bench`: with no such
    tree the script exited 1 for six MISSING sources, and the `returncode != 0` below read that as
    the permission refusal it is about. Giving the tests their own source removed the second cause
    and left the first visible. Same rule as `test_read_fence.py`'s write-bit falsifier: a
    permission test cannot be run by the user that has none — which is why the sibling above
    reaches the identical branch by a route root cannot bypass.
    """
    store = tmp_path / "looplab-bench"
    store.mkdir()
    (store / ".persistent-store-id").write_text("test")
    store.chmod(0o555)
    try:
        r = _run(store / "snapshots")
    finally:
        store.chmod(0o755)
    _assert_it_refused_without_claiming_busy(r, store / "snapshots")


def test_a_busy_lock_exits_non_zero_so_the_timer_retries(tmp_path):
    """Skipping is legitimate; claiming a snapshot was taken is not."""
    import subprocess as sp
    import time

    store = tmp_path / "looplab-bench"
    dest = store / "snapshots"
    dest.mkdir(parents=True)
    (store / ".persistent-store-id").write_text("test")
    lock = dest / ".snapshot.lock"
    lock.touch()

    holder = sp.Popen(["bash", "-c", f'flock 8; sleep 90 8>"{lock}"'], stdout=sp.DEVNULL,
                      stderr=sp.DEVNULL)
    holder2 = sp.Popen(["bash", "-c", f'( flock 8; sleep 90 ) 8>"{lock}"'], stdout=sp.DEVNULL,
                       stderr=sp.DEVNULL)
    time.sleep(2)
    try:
        r = _run(dest, timeout=200)
    finally:
        for h in (holder, holder2):
            h.kill()
            h.wait()

    assert r.returncode != 0, (
        "a run that waited out the lock and wrote nothing reported success, so the timer records "
        "its fingerprint and never retries:\n" + r.stdout + r.stderr
    )
    assert not [d for d in dest.iterdir() if d.is_dir() and d.name[0].isdigit()], \
        "premise: nothing should have been written"


def test_the_timer_does_not_record_a_fingerprint_for_a_non_zero_snapshot():
    """The exit code only helps if the caller reads it."""
    timer = (REPO / "benchmarks" / "snapshot_timer.sh").read_text()
    assert 'if [ "$snap_rc" = "0" ]' in timer, (
        "snapshot_timer no longer branches on the snapshot's exit code"
    )
    assert "NOT recording this fingerprint" in timer, (
        "the timer records a fingerprint regardless of outcome, which is what makes a false "
        "success permanent rather than merely wrong once"
    )


# --------------------------------------------------- the environment record may not leak a secret
#
# Measured 2026-09-01 on the real script: `ALGOTUNE_AUTH`, `LOOPLAB_GATEWAY_CREDENTIALS` and a
# `LOOPLAB_LLM_BASE_URL` carrying `user:hunter2@` were all written in FULL, because the rule was a
# denylist on the NAME (KEY|TOKEN|SECRET|PASSWORD) and none of those names contains one of those
# words. The same file redacted BASE_URL in its `.env` section, so it contradicted itself. Probe
# trees and snapshots go to S3.


def _env_record(tmp_path, extra_env):
    store = tmp_path / "looplab-bench"
    dest = store / "snapshots"
    dest.mkdir(parents=True)
    (store / ".persistent-store-id").write_text("test")
    r = _run(dest, env=extra_env)
    assert r.returncode == 0, r.stdout + r.stderr
    tree = next(d for d in dest.iterdir() if d.is_dir() and d.name[0].isdigit())
    return (tree / "ENVIRONMENT.txt").read_text()


def test_a_credential_whose_NAME_looks_innocent_is_still_redacted(tmp_path):
    body = _env_record(tmp_path, {
        "ALGOTUNE_AUTH": "sk-LEAK-auth",
        "LOOPLAB_GATEWAY_CREDENTIALS": "sk-LEAK-creds",
    })
    assert "sk-LEAK-auth" not in body and "sk-LEAK-creds" not in body, (
        "a name-based denylist let a credential through:\n" + body
    )
    assert "ALGOTUNE_AUTH" in body, "the variable vanished entirely; its NAME is not the secret"


def test_a_url_carrying_userinfo_is_redacted(tmp_path):
    body = _env_record(tmp_path, {"LOOPLAB_LLM_BASE_URL": "https://user:hunter2@gw.example/v1"})
    assert "hunter2" not in body, "a password embedded in a URL was written into the record:\n" + body


def test_the_measurement_settings_are_still_shown(tmp_path):
    """A redaction that hides everything records nothing; the point is the settings."""
    body = _env_record(tmp_path, {"LOOPLAB_LLM_STREAM": "1", "ALGOTUNE_EVAL_WORKERS": "auto"})
    live = body.split("live process environment", 1)[1]
    assert "LOOPLAB_LLM_STREAM                       = 1" in live, (
        "the one setting that decides the 300 s ceiling is no longer legible:\n" + live
    )
    assert "ALGOTUNE_EVAL_WORKERS                    = auto" in live


def test_the_record_says_which_of_two_values_was_in_force(tmp_path):
    """A real snapshot carried STREAM=false and STREAM=1 with nothing saying which one ran."""
    body = _env_record(tmp_path, {"LOOPLAB_LLM_STREAM": "1"})
    assert "THIS IS THE ONE IN FORCE" in body, (
        "the live section is not marked as authoritative, so a reader facing two values for one "
        "setting cannot tell which produced the numbers:\n" + body
    )
    assert "ON DISK ONLY" in body, "the .env section is not marked as possibly superseded"


def test_it_no_longer_promises_a_sha_it_never_computes(tmp_path):
    body = _env_record(tmp_path, {})
    assert "truncated sha256." not in body.split("none was ever computed")[0], (
        "the header still claims a sha256 that is not computed anywhere"
    )


# The two defences -- an allowlist on the NAME and a sniff of the VALUE -- overlap on every fixture
# above, so mutation could delete either one and the other caught it. These two separate them.


def test_the_allowlist_alone_covers_a_value_that_does_not_look_like_a_secret(tmp_path):
    """A denylist on the name would print this; only the allowlist stops it."""
    body = _env_record(tmp_path, {"LOOPLAB_INTERNAL_ENDPOINT": "prod-db-17.internal:5432"})
    live = body.split("live process environment", 1)[1]
    assert "prod-db-17.internal" not in live, (
        "a variable that is not a measurement setting had its value printed; nothing about the "
        "VALUE looks secret, so only the allowlist can stop this:\n" + live
    )
    assert "LOOPLAB_INTERNAL_ENDPOINT" in live, "the name should still be recorded"


def test_the_value_sniff_alone_covers_an_allowlisted_name_holding_a_credential(tmp_path):
    """If a measurement setting ever carries a token, the allowlist would wave it through."""
    body = _env_record(tmp_path, {"LOOPLAB_LLM_MODEL": "sk-oops-a-token-in-the-model-field"})
    live = body.split("live process environment", 1)[1]
    assert "sk-oops-a-token" not in live, (
        "an ALLOWLISTED name printed a value that looks like a credential; only the value sniff "
        "can stop this:\n" + live
    )
