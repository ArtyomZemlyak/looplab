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
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SNAPSHOT = REPO / "benchmarks" / "snapshot.sh"


sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_snapshot_carries_the_repo_and_the_runs import _bench_root  # noqa: E402


_TOY_ROOT: list = []


def _toy_root(dest) -> str:
    """A COMPLETE but tiny BENCH_ROOT, built once per destination.

    These tests defaulted `BENCH_ROOT` to the LIVE `/var/tmp/looplab-bench`, because only a complete
    root makes `snapshot.sh` exit 0 and every one of them asserts `returncode == 0`. The cost, by
    `--durations` on 2026-09-04: **27-33 s each** across a dozen cases and **62 s** for the busy-lock
    one, all of it walking 5,151 files of the live corpus with a `cmp` apiece and copying 1.2 G --
    while probes are writing into that tree. What they actually assert on is `ENVIRONMENT.txt`,
    which is built from the ENVIRONMENT, and an exit code.

    `_bench_root` is imported rather than copied: a sibling file already builds this shape, with the
    reasoning for each part written into it, and two copies of a fixture drift exactly like two
    copies of a rule (§204).
    """
    # Built ONCE, in a directory of its own, and never under the destination: two of these tests
    # are about a store root that is empty and about a destination that cannot be written, and a
    # fixture that plants a tree there answers both questions for them.
    if not _TOY_ROOT:
        holder = Path(tempfile.mkdtemp(prefix="snapshot-toy-bench-"))
        _TOY_ROOT.append(str(_bench_root(holder)))
    return _TOY_ROOT[0]


def _run(dest, env=None, timeout=600):  # noqa: D401 - timeout is raised by the lock tests
    e = dict(os.environ)
    e.setdefault("BENCH_ROOT", _toy_root(dest))
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

        # A BENCH_ROOT WITH ONE RUN TREE. The subject here is the LOCK and the stamp, not the
        # corpus, and this defaulted to the LIVE /var/tmp/looplab-bench -- so the case started TWO
        # real snapshots of the 1.2 G bench root, `find` over 5,151 files with a `cmp` each and
        # 1.2 G of `cp -ru`, twice, which is why it carried a 900 s timeout. Caught 2026-09-04 when
        # `find /var/tmp/looplab-bench/model-probes` turned up in /proc during a suite run. §206
        # reproduced this same lock behaviour on a toy tree in under a second.
        toy = store / "bench"
        (toy / "model-probes" / "p1" / "runs" / "t" / "run").mkdir(parents=True, exist_ok=True)
        (toy / "model-probes" / "p1" / "runs" / "t" / "run" / "events.jsonl").write_text(
            '{"type":"run_started"}\n', encoding="utf-8")
        env = dict(os.environ)
        env.setdefault("BENCH_ROOT", str(toy))
        procs = [
            subprocess.Popen(["bash", str(SNAPSHOT), str(dest)],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, env=env)
            for _ in range(2)
        ]
        outs = [p.communicate(timeout=120) for p in procs]
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


def test_an_unwritable_destination_is_a_failure_not_a_skip(tmp_path):
    store = tmp_path / "looplab-bench"
    store.mkdir()
    (store / ".persistent-store-id").write_text("test")
    store.chmod(0o555)
    try:
        r = _run(store / "snapshots")
    finally:
        store.chmod(0o755)
    assert r.returncode != 0, (
        "a destination that cannot even hold a lock file reported SUCCESS:\n" + r.stdout + r.stderr
    )
    assert "NOTHING WAS WRITTEN" in r.stderr, (
        "it failed, but not in words that stop somebody reading it as a skip:\n" + r.stderr
    )
    assert "another snapshot is running" not in r.stderr, (
        "it still blames a concurrent snapshot for a permission problem"
    )


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


def test_these_tests_do_not_snapshot_the_live_bench_root():
    """The guard for the fixture above, because the cost of losing it is invisible.

    Every case here asserts `returncode == 0`, and only a COMPLETE `BENCH_ROOT` gives that, so the
    file defaulted to the live `/var/tmp/looplab-bench` — walking 5,151 files with a `cmp` apiece and
    copying 1.2 G, per test, while probes were writing into that tree. Measured by `--durations` on
    2026-09-04: 27–33 s each across a dozen cases, now 0.14–0.16 s. (The busy-lock case stays at
    62 s: it waits out `flock -w 60` on purpose, and that second is the behaviour under test.)
    """
    # PARSED, not grepped: the path is named all over the comments and docstrings above and that
    # is the record. What must not exist is a string CONSTANT carrying it -- i.e. code that points
    # a test at the live tree. Docstrings are excluded because they are exactly the prose.
    import ast
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body and isinstance(body[0], ast.Expr) \
                and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
            docstrings.add(id(body[0].value))
    live = [n for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and n.value == "/var/tmp/looplab-bench" and id(n) not in docstrings]
    # The two below are this guard's own comparisons.
    assert len(live) == 2, (
        f"{len(live)} code references to the live bench root (expected only this guard's two) -- "
        "a snapshot test must build its own root, not walk the corpus probes are writing to")
    got = _toy_root(Path(tempfile.gettempdir()) / "unused")
    assert got != "/var/tmp/looplab-bench" and Path(got).is_dir(), got
    assert (Path(got) / "looplab" / ".git").is_dir(), (
        f"{got} is not a complete bench root, so snapshot.sh would exit 1 and every "
        "`returncode == 0` here would be asserting the wrong thing")
