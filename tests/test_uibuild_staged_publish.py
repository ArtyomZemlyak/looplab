"""The UI build must not be able to destroy a working bundle (`looplab/serve/uibuild.py`).

`looplab ui` auto-rebuilds when `ui/dist` is stale. Vite empties its output directory before it
writes anything and `npm run build`'s `prebuild` hook is an unconditional `rm -rf ui/dist`, so a
rebuild that failed after either point left NO servable UI — from a command an operator ran
expecting a no-op. `ui/dist` is not tracked in git, so there was nothing to restore from.

Builds therefore emit into a sibling STAGING directory and publish it over `dist` only once
`is_built()` accepts the staged output. These tests pin the properties that makes true:

  * a failed build leaves the previous bundle byte-identical, and still servable via `--no-build`;
  * a successful build replaces it wholesale (no file of the old bundle survives);
  * an interrupted publish can never leave `dist` in a state where NEITHER bundle is usable;
  * recovery from one is reachable WITHOUT a working toolchain — the crash and the broken
    toolchain are the same incident, so a repair only a build can perform is no repair at all;
  * a pinned `LOOPLAB_UI_DIST` is never built into, and grows no staging directories;
  * the freshness-stamp fast path still does no work at all.

The npm calls are monkeypatched, so these run with no Node toolchain and never touch the real `ui/`
tree (paths are pinned to tmp via LOOPLAB_UI_SRC / LOOPLAB_UI_DIST)."""
from __future__ import annotations

import errno
import os
import shutil
import time
from contextlib import contextmanager

import pytest
from typer.testing import CliRunner

from looplab.core.atomicio import file_identity
from looplab.serve import uibuild


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("LOOPLAB_UI_SRC", raising=False)
    monkeypatch.delenv("LOOPLAB_UI_DIST", raising=False)


# ----------------------------------------------------------------- fixtures / helpers


def _src_with_sources(tmp_path):
    """A UI source tree whose dependencies are already current, so only the build runs."""
    src = tmp_path / "ui"
    src.mkdir()
    (src / "package.json").write_text('{"name": "looplab-ui"}', encoding="utf-8")
    (src / "package-lock.json").write_text("{}", encoding="utf-8")
    (src / "node_modules").mkdir()
    uibuild._dependency_stamp_path(src).write_text(
        uibuild._dependency_digest(src) + "\n", encoding="ascii")
    return src


def _write_bundle(dist, marker):
    """A bundle with real substance: an entry point plus a hashed asset, like Vite emits."""
    (dist / "assets").mkdir(parents=True, exist_ok=True)
    (dist / "index.html").write_text(
        f'<!doctype html><script src="./assets/{marker}.js"></script>', encoding="utf-8")
    (dist / "assets" / f"{marker}.js").write_text(f"export const build = '{marker}'\n",
                                                  encoding="utf-8")
    return dist


def _tree(root):
    """Every file under *root* with its bytes — the strongest 'unchanged' statement available."""
    return {path.relative_to(root).as_posix(): path.read_bytes()
            for path in sorted(root.rglob("*")) if path.is_file()}


def _identity(path):
    """`atomicio.file_identity`: same inode AND same bytes/timestamps.

    Stronger than comparing content, and deliberately so — a builder that destroyed `dist` and
    restored an identical copy would pass a bytes-only check while still having had a window with
    no servable UI. A changed inode means the file was replaced, which is exactly what must not
    happen to a bundle nobody asked to replace."""
    return file_identity(os.stat(path))


def _staged_outdir(args, src):
    """The directory the build was pointed at, read back off its own argv."""
    args = list(args)
    return src / args[args.index("--outDir") + 1]


def _emitting_build(src, marker, calls=None):
    """A fake `_run` that behaves like a working toolchain: emits into the staged `--outDir`."""

    def run(args, *, cwd, log):
        if calls is not None:
            calls.append(list(args))
        if list(args)[:3] == ["npm", "run", "build"]:
            _write_bundle(_staged_outdir(args, src), marker)
        return True

    return run


def _failing_build(src, calls=None, *, emit_first=False):
    """A fake `_run` whose build exits non-zero — optionally after writing partial staged output."""

    def run(args, *, cwd, log):
        if calls is not None:
            calls.append(list(args))
        if list(args)[:3] != ["npm", "run", "build"]:
            return True
        if emit_first:
            # A build that dies PART WAY: some output landed, no valid entry point.
            staged = _staged_outdir(args, src)
            (staged / "assets").mkdir(parents=True, exist_ok=True)
            (staged / "assets" / "partial.js").write_text("half a chunk", encoding="utf-8")
        log("[ui] (simulated) vite exited 1")
        return False

    return run


# ----------------------------------------------------------------- where the build writes


def test_the_build_is_pointed_at_a_sibling_staging_directory_with_the_clean_hook_off(tmp_path):
    src = _src_with_sources(tmp_path)
    dist = src / "dist"
    stage = uibuild._stage_dir(dist)

    argv = uibuild._build_command(src, stage)

    assert stage.parent == dist.parent, "publishing is a rename; it cannot cross a filesystem"
    assert stage != dist
    assert argv[:3] == ["npm", "run", "build"]
    # `prebuild` is scripts/clean-dist.mjs — an unconditional `rm -rf ui/dist` that runs BEFORE vite
    # and would destroy the live bundle no matter where the build is told to write.
    assert "--ignore-scripts" in argv
    outdir = argv[argv.index("--outDir") + 1]
    assert (src / outdir).resolve() == stage.resolve()
    assert (src / outdir).resolve() != dist.resolve()


# ----------------------------------------------------------------- a failed build preserves


def test_failed_build_leaves_the_previous_bundle_byte_identical(tmp_path, monkeypatch):
    src = _src_with_sources(tmp_path)
    dist = _write_bundle(src / "dist", "good")
    uibuild._write_build_stamp(dist, "stamp-from-an-older-source")
    before, before_identity = _tree(dist), _identity(dist / "index.html")
    monkeypatch.setenv("LOOPLAB_UI_SRC", str(src))
    monkeypatch.setattr(uibuild, "_has_npm", lambda: True)
    monkeypatch.setattr(uibuild, "_run", _failing_build(src))
    logs = []

    assert uibuild.ensure_ui_built(log=logs.append) is False

    assert _tree(dist) == before
    assert _identity(dist / "index.html") == before_identity, "the file was replaced, not preserved"
    assert uibuild.is_built(dist), "--no-build must still have something to serve"
    assert any("not touched" in message for message in logs)


def test_a_partially_written_failed_build_still_preserves_the_previous_bundle(tmp_path, monkeypatch):
    """The original defect was positional: survival depended on WHERE in the build it died."""
    src = _src_with_sources(tmp_path)
    dist = _write_bundle(src / "dist", "good")
    before, before_identity = _tree(dist), _identity(dist / "index.html")
    monkeypatch.setenv("LOOPLAB_UI_SRC", str(src))
    monkeypatch.setattr(uibuild, "_has_npm", lambda: True)
    monkeypatch.setattr(uibuild, "_run", _failing_build(src, emit_first=True))

    assert uibuild.ensure_ui_built(force=True, log=lambda *_: None) is False

    assert _tree(dist) == before
    assert _identity(dist / "index.html") == before_identity


def test_a_build_that_emits_nothing_usable_does_not_publish_and_does_not_damage_dist(
        tmp_path, monkeypatch):
    """Exit 0 with no entry point is still a failed build — and must not touch `dist` either."""
    src = _src_with_sources(tmp_path)
    dist = _write_bundle(src / "dist", "good")
    before = _tree(dist)
    monkeypatch.setenv("LOOPLAB_UI_SRC", str(src))
    monkeypatch.setattr(uibuild, "_has_npm", lambda: True)
    monkeypatch.setattr(uibuild, "_run", lambda args, *, cwd, log: True)  # succeeds, emits nothing
    logs = []

    assert uibuild.ensure_ui_built(force=True, log=logs.append) is False
    assert _tree(dist) == before
    assert any("left" in message and "in place" in message for message in logs)


def test_failed_build_leaves_no_staging_directory_behind(tmp_path, monkeypatch):
    src = _src_with_sources(tmp_path)
    dist = _write_bundle(src / "dist", "good")
    monkeypatch.setenv("LOOPLAB_UI_SRC", str(src))
    monkeypatch.setattr(uibuild, "_has_npm", lambda: True)
    monkeypatch.setattr(uibuild, "_run", _failing_build(src, emit_first=True))

    assert uibuild.ensure_ui_built(force=True, log=lambda *_: None) is False

    assert not uibuild._stage_dir(dist).exists()
    assert not uibuild._retired_dir(dist).exists()


def test_stale_staging_output_is_never_published_as_a_fresh_build(tmp_path, monkeypatch):
    """A leftover stage must not let a build that died before emitting publish the LAST attempt."""
    src = _src_with_sources(tmp_path)
    dist = _write_bundle(src / "dist", "good")
    _write_bundle(uibuild._stage_dir(dist), "abandoned-attempt")
    before = _tree(dist)
    monkeypatch.setenv("LOOPLAB_UI_SRC", str(src))
    monkeypatch.setattr(uibuild, "_has_npm", lambda: True)
    # Exits non-zero without writing anything: whatever is in the stage is not this build's output.
    monkeypatch.setattr(uibuild, "_run", _failing_build(src))

    assert uibuild.ensure_ui_built(force=True, log=lambda *_: None) is False
    assert _tree(dist) == before
    assert "abandoned-attempt" not in "".join(_tree(dist))


# ----------------------------------------------------------------- a successful build replaces


def test_successful_build_replaces_the_previous_bundle_wholesale(tmp_path, monkeypatch):
    src = _src_with_sources(tmp_path)
    dist = _write_bundle(src / "dist", "old")
    monkeypatch.setenv("LOOPLAB_UI_SRC", str(src))
    monkeypatch.setattr(uibuild, "_has_npm", lambda: True)
    monkeypatch.setattr(uibuild, "_run", _emitting_build(src, "new"))

    assert uibuild.ensure_ui_built(force=True, log=lambda *_: None) is True

    assert uibuild.is_built(dist)
    assert (dist / "assets" / "new.js").is_file()
    # A rename-based publish replaces the whole directory, so nothing of the old build can survive —
    # the guarantee `prebuild`'s `rm -rf` used to provide, without its destructiveness.
    assert not (dist / "assets" / "old.js").exists()
    assert not uibuild._stage_dir(dist).exists()
    assert not uibuild._retired_dir(dist).exists()


def test_the_published_bundle_and_its_freshness_stamp_appear_together(tmp_path, monkeypatch):
    """The stamp is written into the STAGE, so `dist` is never fresh-but-unstamped or the reverse."""
    src = _src_with_sources(tmp_path)
    dist = src / "dist"
    monkeypatch.setenv("LOOPLAB_UI_SRC", str(src))
    monkeypatch.setattr(uibuild, "_has_npm", lambda: True)

    published = {}
    monkeypatch.setattr(uibuild, "_run", _emitting_build(src, "new"))
    real_publish = uibuild._publish_staged_dist

    def record_then_publish(stage, target, *, log):
        published["stamped_before_publish"] = uibuild._installed_build_digest(stage)
        return real_publish(stage, target, log=log)

    monkeypatch.setattr(uibuild, "_publish_staged_dist", record_then_publish)

    assert uibuild.ensure_ui_built(log=lambda *_: None) is True
    assert published["stamped_before_publish"] == uibuild._build_digest(src)
    assert uibuild._installed_build_digest(dist) == uibuild._build_digest(src)


def test_a_first_build_with_no_previous_bundle_publishes_normally(tmp_path, monkeypatch):
    src = _src_with_sources(tmp_path)
    monkeypatch.setenv("LOOPLAB_UI_SRC", str(src))
    monkeypatch.setattr(uibuild, "_has_npm", lambda: True)
    monkeypatch.setattr(uibuild, "_run", _emitting_build(src, "first"))

    assert uibuild.ensure_ui_built(log=lambda *_: None) is True
    assert uibuild.is_built(src / "dist")
    assert not uibuild._stage_dir(src / "dist").exists()


# ----------------------------------------------------------------- interrupted publish


class _Crash(BaseException):
    """Stands in for the process dying mid-publish.

    Deliberately NOT an OSError and not an Exception: a real crash runs no `except` handler and no
    `finally`, so a test that raised something the production code catches would be measuring the
    error path instead of the crash path."""


@contextmanager
def _crash_after(root, mutations):
    """Let *mutations* filesystem changes under *root* happen, then die instead of the next one.

    Restores the real `os.rename`/`shutil.rmtree` on the way out rather than going through
    `monkeypatch`, because the recovery this test runs NEXT must see a working filesystem — a
    half-patched teardown would silently truncate the repair too and the test would prove nothing.
    """
    real_rename, real_rmtree = os.rename, shutil.rmtree
    performed = []

    def counted(kind, path, action):
        if str(path).startswith(str(root)):
            performed.append(kind)
            if len(performed) > mutations:
                raise _Crash(f"process died at mutation {len(performed)} ({kind} {path})")
        return action()

    os.rename = lambda a, b, **kw: counted("rename", a, lambda: real_rename(a, b, **kw))
    shutil.rmtree = lambda p, **kw: counted("rmtree", p, lambda: real_rmtree(p, **kw))
    try:
        yield performed
    finally:
        os.rename, shutil.rmtree = real_rename, real_rmtree


@pytest.mark.parametrize("crash_after", [0, 1, 2, 3, 4])
@pytest.mark.parametrize("atomic_exchange", [True, False])
def test_an_interrupted_publish_never_leaves_both_bundles_unusable(
        tmp_path, monkeypatch, crash_after, atomic_exchange):
    """Kill the process at every filesystem step of the publish; a bundle must always survive.

    The fallback publish (`atomic_exchange=False` — what `atomicio.exchange_paths_if_supported`
    reports on geesefs and every filesystem without renameat2 flags) is two renames, and only ONE
    order is safe: retire the
    old bundle to a sibling name first, publish the staged one second. The intuitive order — remove
    `dist`, then move the new bundle in — has an instant where the only copy of a working bundle has
    already been deleted. That is the defect this whole mechanism exists to remove, so it is checked
    at every interruption point rather than argued about."""
    src = _src_with_sources(tmp_path)
    dist = _write_bundle(src / "dist", "old")
    stage = _write_bundle(uibuild._stage_dir(dist), "new")
    retired = uibuild._retired_dir(dist)
    if not atomic_exchange:
        monkeypatch.setattr(uibuild, "exchange_paths_if_supported", lambda *_a, **_k: False)

    with _crash_after(tmp_path, crash_after):
        try:
            uibuild._publish_staged_dist(stage, dist, log=lambda *_: None)
            crashed = False
        except _Crash:
            crashed = True

    if not atomic_exchange:
        # The forced fallback is exactly four mutations — discard the retired name, retire `dist`,
        # publish the stage, drop the retired copy — so every one of these interruption points is a
        # real kill and not a vacuous "the publish had already finished".
        assert crashed is (crash_after < 4)

    # 1. The instant-by-instant invariant: a COMPLETE bundle is always on disk under a name the
    #    recovery knows about. Never "neither the old nor the new".
    assert uibuild.is_built(dist) or uibuild.is_built(retired) or uibuild.is_built(stage)
    assert uibuild.is_built(dist) or uibuild.is_built(retired)

    # 2. What the next `looplab ui` does: heal, then serve.
    uibuild._recover_interrupted_publish(dist, log=lambda *_: None)
    assert uibuild.is_built(dist)
    served = _tree(dist)
    assert served["index.html"] in (b'<!doctype html><script src="./assets/old.js"></script>',
                                    b'<!doctype html><script src="./assets/new.js"></script>')
    marker = "old" if b"old.js" in served["index.html"] else "new"
    assert f"assets/{marker}.js" in served, "a half-published bundle was left servable"


def test_recovery_prefers_the_bundle_that_was_actually_being_served(tmp_path, monkeypatch):
    """Mid-swap state: `dist` gone, the previous bundle retired, the new one staged.

    The retired copy goes back, because that restores exactly the state the interrupted command
    started from. Its stamp no longer matches the source, so the caller simply rebuilds."""
    src = _src_with_sources(tmp_path)
    dist = src / "dist"
    _write_bundle(uibuild._retired_dir(dist), "old")
    _write_bundle(uibuild._stage_dir(dist), "new")
    logs = []

    uibuild._recover_interrupted_publish(dist, log=logs.append)

    assert (dist / "assets" / "old.js").is_file()
    assert any("restored the previous UI bundle" in message for message in logs)
    # Recovery can leave the caller with nothing to do — a restored bundle whose stamp still matches
    # takes the freshness fast path and never reaches `_prepare_stage`. So recovery, not the next
    # build, has to be what collects both scratch names.
    assert not uibuild._retired_dir(dist).exists()
    assert not uibuild._stage_dir(dist).exists()


def test_recovery_runs_before_the_next_build_judges_freshness(tmp_path, monkeypatch):
    """A crashed publish must not read as "no bundle" and it must not block the next build."""
    src = _src_with_sources(tmp_path)
    dist = src / "dist"
    _write_bundle(uibuild._retired_dir(dist), "old")
    monkeypatch.setenv("LOOPLAB_UI_SRC", str(src))
    monkeypatch.setattr(uibuild, "_has_npm", lambda: True)
    monkeypatch.setattr(uibuild, "_run", _failing_build(src))

    assert uibuild.ensure_ui_built(log=lambda *_: None) is False
    # The rebuild failed, but the bundle the operator was serving before the crash is back.
    assert uibuild.is_built(dist)
    assert (dist / "assets" / "old.js").is_file()


def test_a_finished_publish_leaves_no_retired_copy_to_shadow_the_new_bundle(tmp_path):
    """Crash after the publish rename: `dist` is the NEW bundle, so the retired copy is garbage."""
    src = _src_with_sources(tmp_path)
    dist = _write_bundle(src / "dist", "new")
    _write_bundle(uibuild._retired_dir(dist), "old")
    # What an atomic exchange leaves behind if the process dies before it discards the stage.
    _write_bundle(uibuild._stage_dir(dist), "old")

    uibuild._recover_interrupted_publish(dist, log=lambda *_: None)

    assert (dist / "assets" / "new.js").is_file()
    assert not uibuild._retired_dir(dist).exists()
    assert not uibuild._stage_dir(dist).exists()


# --------------------------------------------- recovery must not need a working toolchain
#
# The crash and the broken toolchain are usually the SAME incident. The failure this whole mechanism
# was written for was measured on a JupyterHub geesefs home where `npm run build` cannot work at all,
# and that is exactly the box where an operator kills a `looplab ui` that appears to hang. Recovery
# that only ran inside the build was therefore unreachable in its own motivating scenario: a complete
# bundle under `.dist.looplab-previous`, nothing servable at `dist`, and no message naming either.


def _killed_mid_swap(src, marker="old"):
    """The state a SIGINT between the two publish renames leaves: `dist` gone, previous retired.

    Built by running the real publish and killing it, not by staging directories by hand, so the
    test cannot drift away from what the code actually produces.
    """
    dist = _write_bundle(src / "dist", marker)
    uibuild._write_build_stamp(dist, "the-stamp-of-the-source-that-built-it")
    stage = _write_bundle(uibuild._stage_dir(dist), "new")
    identity = _identity(dist / "index.html")
    real_exchange = uibuild.exchange_paths_if_supported
    uibuild.exchange_paths_if_supported = lambda *_a, **_k: False  # geesefs: no renameat2
    try:
        with _crash_after(src, 2):  # rmtree(retired) + rename(dist -> retired), then die
            with pytest.raises(_Crash):
                uibuild._publish_staged_dist(stage, dist, log=lambda *_: None)
    finally:
        uibuild.exchange_paths_if_supported = real_exchange
    # `_ensure_ui_built_locked`'s `finally: _discard_dir(stage)` runs even on the way out of a
    # KeyboardInterrupt, so the NEW bundle is gone too — the measured state, not a kinder one.
    shutil.rmtree(stage, ignore_errors=True)
    assert not uibuild.is_built(dist) and uibuild.is_built(uibuild._retired_dir(dist))
    return dist, identity


def test_recovery_runs_when_the_toolchain_is_missing_entirely(tmp_path, monkeypatch):
    """No npm: `ensure_ui_built` still refuses (nothing was built), but the UI comes back.

    Before this, every path that could not BUILD returned before the lock, and recovery lived only
    inside it — so the one command an operator runs after a killed publish reported "cannot build"
    and left a working bundle sitting under a name nothing had told them about.
    """
    src = _src_with_sources(tmp_path)
    dist, before_identity = _killed_mid_swap(src)
    monkeypatch.setenv("LOOPLAB_UI_SRC", str(src))
    monkeypatch.setattr(uibuild, "_has_npm", lambda: False)
    monkeypatch.setattr(uibuild, "_run", lambda *a, **k: pytest.fail("there is no npm to run"))
    logs = []

    assert uibuild.ensure_ui_built(log=logs.append) is False  # a build genuinely did not happen

    assert uibuild.is_built(dist), "the last good bundle is servable again"
    assert _identity(dist / "index.html") == before_identity, "a copy, not the bundle itself"
    assert (dist / "assets" / "old.js").is_file()
    assert not uibuild._retired_dir(dist).exists()
    assert any("restored the previous UI bundle" in message for message in logs)


def test_recover_ui_bundle_repairs_with_no_toolchain_at_all(tmp_path, monkeypatch):
    """The entry point `looplab ui --no-build` uses: repair only, no build attempted or needed."""
    src = _src_with_sources(tmp_path)
    dist, before_identity = _killed_mid_swap(src)
    monkeypatch.setenv("LOOPLAB_UI_SRC", str(src))
    monkeypatch.setattr(uibuild, "_has_npm", lambda: pytest.fail("recovery must not ask about npm"))
    monkeypatch.setattr(uibuild, "_run", lambda *a, **k: pytest.fail("recovery must not build"))

    assert uibuild.recover_ui_bundle(log=lambda *_: None) is True

    assert uibuild.is_built(dist)
    assert _identity(dist / "index.html") == before_identity
    assert not uibuild._retired_dir(dist).exists()


def test_ui_no_build_serves_the_recovered_bundle(tmp_path, monkeypatch):
    """`looplab ui --no-build` is the documented escape hatch, so it must actually reach `serve`."""
    from looplab.cli import app

    src = _src_with_sources(tmp_path)
    dist, before_identity = _killed_mid_swap(src)
    monkeypatch.setenv("LOOPLAB_UI_SRC", str(src))
    monkeypatch.setattr(uibuild, "_has_npm", lambda: False)
    monkeypatch.setattr(uibuild, "ensure_ui_built",
                        lambda **_kwargs: pytest.fail("--no-build must not build"))
    served = []
    monkeypatch.setattr("looplab.serve.server.serve",
                        lambda run_root, **kwargs: served.append(run_root))

    result = CliRunner().invoke(app, ["ui", "--no-build", "--run-root", str(tmp_path / "runs")])

    assert result.exit_code == 0, (result.output, result.exception)
    assert served, "the server was never started"
    assert uibuild.is_built(dist)
    assert _identity(dist / "index.html") == before_identity


def test_a_requested_build_that_cannot_run_still_points_at_the_recovered_bundle(tmp_path, monkeypatch):
    """Plain `looplab ui` on a box with no npm: refuse to serve silently, but say where the UI is.

    The refusal is unchanged — a requested refresh that failed must not quietly serve something
    older. What must be true now is that the thing it names EXISTS: `--no-build` has a bundle to
    serve because recovery ran before the toolchain check, not after it.
    """
    from looplab.cli import app

    src = _src_with_sources(tmp_path)
    dist, _identity_before = _killed_mid_swap(src)
    monkeypatch.setenv("LOOPLAB_UI_SRC", str(src))
    monkeypatch.setattr(uibuild, "_has_npm", lambda: False)

    result = CliRunner().invoke(app, ["ui", "--run-root", str(tmp_path / "runs")])

    assert result.exit_code == 1
    assert "previous bundle was left untouched" in result.output
    assert "--no-build" in result.output
    # Not the "output no successful build produced" branch: this bundle is a recovered one, and
    # telling an operator their restored UI is unexplained junk would be worse than saying nothing.
    assert "no successful build produced" not in result.output
    assert uibuild.is_built(dist)


def test_recovery_is_serialized_against_a_concurrent_build(tmp_path, monkeypatch):
    """Recovery renames directories a build also renames, so it holds the same source-root lock."""
    src = _src_with_sources(tmp_path)
    dist, _before = _killed_mid_swap(src)
    monkeypatch.setenv("LOOPLAB_UI_SRC", str(src))
    held = []
    real_lock = uibuild._ui_build_lock

    @contextmanager
    def watched_lock(source, **kwargs):
        with real_lock(source, **kwargs):
            held.append(uibuild.is_built(dist))
            yield

    monkeypatch.setattr(uibuild, "_ui_build_lock", watched_lock)

    assert uibuild.recover_ui_bundle(log=lambda *_: None) is True
    assert held == [False], "the repair happened inside the lock, not before or after it"


@pytest.mark.skipif(os.name == "nt", reason="holds a POSIX flock from a second descriptor")
def test_recovery_gives_up_quickly_when_a_build_holds_the_lock(tmp_path, monkeypatch):
    """It waits SECONDS, not the build deadline — and it is not the one that has to do the repair.

    Whoever holds this lock is a build, and a build's first act is the same recovery. Waiting out
    `_BUILD_LOCK_TIMEOUT_S` would repair nothing that is not already being repaired, while turning
    `looplab ui --no-build` — whose whole promise is not to wait for a build — into a five-minute
    silent stall behind someone else's `npm run build`.
    """
    import fcntl

    src = _src_with_sources(tmp_path)
    dist, _before = _killed_mid_swap(src)
    monkeypatch.setenv("LOOPLAB_UI_SRC", str(src))
    monkeypatch.setattr(uibuild, "_RECOVERY_LOCK_TIMEOUT_S", 0.2)
    monkeypatch.setattr(uibuild, "_BUILD_LOCK_POLL_S", 0.01)
    logs = []

    with (src / uibuild._BUILD_LOCK).open("a+b") as sibling:  # stands in for a running build
        sibling.write(b"\0")
        sibling.flush()
        fcntl.flock(sibling.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        started = time.monotonic()
        assert uibuild.recover_ui_bundle(log=logs.append) is False
        waited = time.monotonic() - started

    assert waited < 30, f"waited {waited:.1f}s — that is the BUILD deadline, not recovery's"
    assert any("holds" in message for message in logs)
    assert uibuild.is_built(uibuild._retired_dir(dist)), "left intact for the lock holder to restore"

    # …and once the build lets go, the very next launch repairs it.
    assert uibuild.recover_ui_bundle(log=logs.append) is True
    assert uibuild.is_built(dist)


def test_recovery_with_nothing_to_repair_takes_no_lock(tmp_path, monkeypatch):
    """A healthy tree must not pay for this. `looplab ui` runs it on EVERY launch.

    The probe is deliberately narrow (`_publish_recovery_pending`): a leftover scratch directory
    beside a good bundle is garbage the next build collects, so reporting it would make every launch
    take the lock — and wait on a sibling build for it — to tidy something nobody can observe.
    """
    src = _src_with_sources(tmp_path)
    dist = _write_bundle(src / "dist", "current")
    _write_bundle(uibuild._stage_dir(dist), "someone-elses-build-in-progress")
    monkeypatch.setenv("LOOPLAB_UI_SRC", str(src))
    monkeypatch.setattr(uibuild, "_ui_build_lock",
                        lambda *a, **k: pytest.fail("must not take the build lock"))

    assert uibuild.recover_ui_bundle(log=lambda *_: None) is True
    assert uibuild._stage_dir(dist).exists(), "a live build's staging directory was not disturbed"


def test_recovery_never_touches_a_pinned_or_packaged_bundle(tmp_path, monkeypatch):
    """LOOPLAB_UI_DIST and a wheel's packaged dist are never staged over, so they cannot need this."""
    pinned = _write_bundle(tmp_path / "pinned", "prebuilt")
    before = _tree(pinned)
    monkeypatch.setenv("LOOPLAB_UI_SRC", str(tmp_path / "ui"))
    monkeypatch.setenv("LOOPLAB_UI_DIST", str(pinned))
    monkeypatch.setattr(uibuild, "_ui_build_lock",
                        lambda *a, **k: pytest.fail("must not lock a pinned bundle's source root"))

    assert uibuild.recover_ui_bundle(log=lambda *_: None) is True
    assert _tree(pinned) == before
    assert not uibuild._stage_dir(pinned).exists()
    assert not uibuild._retired_dir(pinned).exists()

    monkeypatch.delenv("LOOPLAB_UI_DIST")  # no sources either -> the wheel's packaged bundle
    packaged = uibuild.ui_dist_dir()
    assert uibuild.recover_ui_bundle(log=lambda *_: None) == uibuild.is_built(packaged)
    assert not uibuild._stage_dir(packaged).exists()
    assert not uibuild._retired_dir(packaged).exists()


# ------------------------------------------- a crash during the FIRST publish keeps its bundle


def test_a_stamped_stage_is_published_when_the_first_publish_died(tmp_path, monkeypatch):
    """No previous bundle means no retire rename, so a kill before the publish rename strands it.

    Nothing SERVABLE is lost either way — there was nothing to serve. But the staged bundle is a
    finished, verified, freshness-stamped build, and the alternative is that the next
    `_prepare_stage` deletes it. That was a wasted build when recovery only ran ahead of another
    build; it is a UI that never comes up now that recovery also runs where no build can.
    """
    src = _src_with_sources(tmp_path)
    dist = src / "dist"
    stage = _write_bundle(uibuild._stage_dir(dist), "first")
    uibuild._write_build_stamp(stage, uibuild._build_digest(src))
    staged_identity = _identity(stage / "index.html")
    monkeypatch.setenv("LOOPLAB_UI_SRC", str(src))
    monkeypatch.setattr(uibuild, "_has_npm", lambda: False)
    logs = []

    assert uibuild.recover_ui_bundle(log=logs.append) is True

    assert (dist / "assets" / "first.js").is_file()
    assert _identity(dist / "index.html") == staged_identity
    assert uibuild._installed_build_digest(dist) == uibuild._build_digest(src)
    assert not uibuild._stage_dir(dist).exists()
    assert any("left staged" in message for message in logs)


def test_an_unstamped_stage_is_never_published(tmp_path, monkeypatch):
    """The stamp is the certificate. Without it the staged tree is mid-build or abandoned output.

    Publishing an unverified stage is the exact false-green `_prepare_stage` exists to prevent: a
    build that died before emitting anything would otherwise inherit the previous attempt's output
    and report success.
    """
    src = _src_with_sources(tmp_path)
    dist = src / "dist"
    _write_bundle(uibuild._stage_dir(dist), "unverified")  # index.html, but no build stamp
    monkeypatch.setenv("LOOPLAB_UI_SRC", str(src))
    monkeypatch.setattr(uibuild, "_has_npm", lambda: False)

    assert uibuild.recover_ui_bundle(log=lambda *_: None) is False
    assert not dist.exists()
    assert uibuild.is_built(uibuild._stage_dir(dist)), "left for `_prepare_stage` to clear"


def test_a_retired_bundle_still_wins_over_a_stamped_stage(tmp_path):
    """Promotion is only for the first-publish case; otherwise the SERVED bundle is what comes back."""
    src = _src_with_sources(tmp_path)
    dist = src / "dist"
    _write_bundle(uibuild._retired_dir(dist), "was-being-served")
    stage = _write_bundle(uibuild._stage_dir(dist), "never-served")
    uibuild._write_build_stamp(stage, uibuild._build_digest(src))

    uibuild._recover_interrupted_publish(dist, log=lambda *_: None)

    assert (dist / "assets" / "was-being-served.js").is_file()
    assert not uibuild._stage_dir(dist).exists()


# ------------------------------------------- what recovery may and may not delete at `dist`


def test_recovery_refuses_to_delete_a_dist_directory_it_did_not_create(tmp_path, monkeypatch):
    """`dist` is the OPERATOR's path — the one directory here LoopLab may not have created.

    Neither publish path can leave `dist` half-populated (both replace the whole name with a rename,
    which is atomic), so a `dist` with files but no `index.html` came from somewhere else: a
    hand-copied bundle, an interrupted `cp -r`, a directory of notes. Recovery used to `rmtree` it
    on the grounds that nothing servable was there. Nothing servable is not nothing.
    """
    src = _src_with_sources(tmp_path)
    dist = src / "dist"
    dist.mkdir()
    (dist / "half-copied-bundle.js").write_text("the only copy", encoding="utf-8")
    retired = _write_bundle(uibuild._retired_dir(dist), "old")
    retired_before = _tree(retired)
    monkeypatch.setenv("LOOPLAB_UI_SRC", str(src))
    logs = []

    assert uibuild.recover_ui_bundle(log=logs.append) is False

    assert (dist / "half-copied-bundle.js").read_text(encoding="utf-8") == "the only copy"
    assert _tree(retired) == retired_before, "the previous bundle is still whole where it was"
    assert uibuild.is_built(retired)
    # An operator who cannot see the retired name cannot act on it, so the refusal has to name both.
    assert any(str(retired) in message and str(dist) in message for message in logs)


def test_recovery_clears_an_empty_dist_directory_and_restores(tmp_path, monkeypatch):
    """An empty directory holds no data, and `rmdir` makes that the kernel's ruling, not ours."""
    src = _src_with_sources(tmp_path)
    dist = src / "dist"
    dist.mkdir()
    _write_bundle(uibuild._retired_dir(dist), "old")
    monkeypatch.setenv("LOOPLAB_UI_SRC", str(src))

    assert uibuild.recover_ui_bundle(log=lambda *_: None) is True
    assert (dist / "assets" / "old.js").is_file()


def _deny_rename_from(monkeypatch, *sources):
    """Make the filesystem refuse to move specific directories, sparing every other rename."""
    real_rename = os.rename
    blocked = {str(path) for path in sources}

    def rename(a, b, **kw):
        if str(a) in blocked:
            raise OSError(errno.EACCES, "rename denied", str(b))
        return real_rename(a, b, **kw)

    monkeypatch.setattr(os, "rename", rename)


def test_a_publish_that_fails_in_process_restores_the_previous_bundle(tmp_path, monkeypatch):
    """Not a crash — an `os.rename` that returns an error. The old bundle still comes back."""
    src = _src_with_sources(tmp_path)
    dist = _write_bundle(src / "dist", "old")
    before = _tree(dist)
    stage = _write_bundle(uibuild._stage_dir(dist), "new")
    monkeypatch.setattr(uibuild, "exchange_paths_if_supported", lambda *_a, **_k: False)
    _deny_rename_from(monkeypatch, stage)

    with pytest.raises(OSError):
        uibuild._publish_staged_dist(stage, dist, log=lambda *_: None)

    assert uibuild.is_built(dist)
    assert _tree(dist) == before


def test_even_an_unrestorable_publish_keeps_the_previous_bundle_and_says_where(
        tmp_path, monkeypatch):
    """When the filesystem refuses BOTH moves, the old bundle is still whole — just under the
    retired name. Nothing is ever deleted before its replacement is in place, so the worst case is
    a bundle the operator has to move back by hand, never one that no longer exists."""
    src = _src_with_sources(tmp_path)
    dist = _write_bundle(src / "dist", "old")
    before = _tree(dist)
    stage = _write_bundle(uibuild._stage_dir(dist), "new")
    retired = uibuild._retired_dir(dist)
    monkeypatch.setattr(uibuild, "exchange_paths_if_supported", lambda *_a, **_k: False)
    _deny_rename_from(monkeypatch, stage, retired)
    logs = []

    with pytest.raises(OSError):
        uibuild._publish_staged_dist(stage, dist, log=logs.append)

    assert _tree(retired) == before
    assert any(str(retired) in message and "could not be restored" in message for message in logs)


def test_the_whole_build_survives_an_unpublishable_dist(tmp_path, monkeypatch):
    """The caller reports failure, keeps the old bundle, and says nothing was lost."""
    src = _src_with_sources(tmp_path)
    dist = _write_bundle(src / "dist", "old")
    before = _tree(dist)
    monkeypatch.setenv("LOOPLAB_UI_SRC", str(src))
    monkeypatch.setattr(uibuild, "_has_npm", lambda: True)
    monkeypatch.setattr(uibuild, "_run", _emitting_build(src, "new"))
    monkeypatch.setattr(uibuild, "exchange_paths_if_supported", lambda *_a, **_k: False)
    _deny_rename_from(monkeypatch, uibuild._stage_dir(dist))
    logs = []

    assert uibuild.ensure_ui_built(force=True, log=logs.append) is False
    assert _tree(dist) == before
    assert any("nothing was lost" in message for message in logs)
    assert not uibuild._stage_dir(dist).exists()


def test_the_atomic_exchange_publish_is_used_where_the_filesystem_supports_it(tmp_path):
    """Where `atomicio.exchange_paths_if_supported` says yes there is no window at all — `dist` never
    vanishes. (The primitive itself is covered in tests/test_atomicio_exchange.py; this asserts the
    publish actually goes through it.)

    Skipped rather than asserted on unsupported mounts: geesefs (this repo's own JupyterHub home)
    rejects every renameat2 flag with EINVAL, which is precisely why the ordered fallback above is
    the path that must carry the guarantee."""
    src = _src_with_sources(tmp_path)
    dist = _write_bundle(src / "dist", "old")
    stage = _write_bundle(uibuild._stage_dir(dist), "new")
    if not uibuild.exchange_paths_if_supported(stage, dist):
        pytest.skip("this filesystem has no renameat2/renamex_np directory exchange")

    assert (dist / "assets" / "new.js").is_file()
    assert (stage / "assets" / "old.js").is_file()


# ----------------------------------------------------------------- pinned dist / no-op


def test_a_pinned_dist_is_never_built_into(tmp_path, monkeypatch):
    """LOOPLAB_UI_DIST means "use this prebuilt bundle" — no build, and no staging next to it."""
    _src_with_sources(tmp_path)
    pinned = _write_bundle(tmp_path / "pinned", "prebuilt")
    uibuild._write_build_stamp(pinned, "a-digest-from-the-image-build")
    before, before_identity = _tree(pinned), _identity(pinned / "index.html")
    monkeypatch.setenv("LOOPLAB_UI_SRC", str(tmp_path / "ui"))
    monkeypatch.setenv("LOOPLAB_UI_DIST", str(pinned))
    monkeypatch.setattr(uibuild, "_has_npm", lambda: True)
    monkeypatch.setattr(uibuild, "_run",
                        lambda *a, **k: pytest.fail("must not build a pinned dist"))
    monkeypatch.setattr(uibuild, "_publish_staged_dist",
                        lambda *a, **k: pytest.fail("must not publish over a pinned dist"))

    # Forced, stale-stamped, and with a source tree present: none of it may reach the pinned bundle.
    assert uibuild.ensure_ui_built(force=True, log=lambda *_: None) is True

    assert _tree(pinned) == before
    assert _identity(pinned / "index.html") == before_identity
    assert not uibuild._stage_dir(pinned).exists()
    assert not uibuild._retired_dir(pinned).exists()
    assert not (tmp_path / "ui" / "dist").exists()


def test_an_empty_pinned_dist_reports_missing_without_building_or_staging(tmp_path, monkeypatch):
    _src_with_sources(tmp_path)
    pinned = tmp_path / "pinned"
    pinned.mkdir()
    monkeypatch.setenv("LOOPLAB_UI_SRC", str(tmp_path / "ui"))
    monkeypatch.setenv("LOOPLAB_UI_DIST", str(pinned))
    monkeypatch.setattr(uibuild, "_has_npm", lambda: True)
    monkeypatch.setattr(uibuild, "_run",
                        lambda *a, **k: pytest.fail("must not build a pinned dist"))
    logs = []

    assert uibuild.ensure_ui_built(force=True, log=logs.append) is False
    assert list(pinned.iterdir()) == []
    assert not uibuild._stage_dir(pinned).exists()
    assert any("LOOPLAB_UI_DIST" in message for message in logs)


def test_a_fresh_stamp_still_means_no_work_at_all(tmp_path, monkeypatch):
    """The fast path must stay free: no lock, no npm, no staging directory, no copying."""
    src = _src_with_sources(tmp_path)
    dist = _write_bundle(src / "dist", "current")
    uibuild._write_build_stamp(dist, uibuild._build_digest(src))
    before_identity = _identity(dist / "index.html")
    monkeypatch.setenv("LOOPLAB_UI_SRC", str(src))
    monkeypatch.setattr(uibuild, "_has_npm", lambda: True)
    monkeypatch.setattr(uibuild, "_run", lambda *a, **k: pytest.fail("must not shell out to npm"))
    monkeypatch.setattr(uibuild, "_ui_build_lock",
                        lambda *a, **k: pytest.fail("must not take the build lock"))
    monkeypatch.setattr(uibuild, "_prepare_stage",
                        lambda *a, **k: pytest.fail("must not create a staging directory"))
    monkeypatch.setattr(uibuild, "_publish_staged_dist",
                        lambda *a, **k: pytest.fail("must not republish an unchanged bundle"))

    assert uibuild.ensure_ui_built(log=lambda *_: None) is True

    assert _identity(dist / "index.html") == before_identity
    assert not uibuild._stage_dir(dist).exists()
    assert not uibuild._retired_dir(dist).exists()
