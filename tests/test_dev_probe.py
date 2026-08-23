"""The Developer's PROBE and its boundary (F2, `looplab/tools/dev_probe.py`).

Every test here DRIVES the property rather than pinning the source (CLAUDE.md's tier 1): a refusal is
asserted by checking that the file the probe tried to create is NOT THERE, not by matching the
message — because the message is one comment away from vacuous and the effect is not. The whole point
of the module is that a probe cannot change anything, so "did it change anything" is the assertion.

These run a real interpreter per case (~100 ms each). That is deliberate: the boundary is composed of
a CPython audit hook, an `RLIMIT_FSIZE`, an inherited env var and a generated `sitecustomize`, and
none of those four is observable in-process.
"""
import json
import os
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import textwrap
from pathlib import Path

import pytest

from looplab.runtime import landlock, read_allowlist, read_fence
from looplab.tools import dev_probe
from looplab.tools._base import RESULT_CAP, stream_tails
from looplab.tools.dev_probe import _MAX_TIMEOUT, DevProbeTools


def _probe(code, **kw):
    return DevProbeTools(timeout_s=kw.pop("timeout_s", 30), **kw).execute("run_probe", {"code": code})


@pytest.fixture()
def outside(tmp_path):
    """A directory the PROBE has no business touching — it stands in for site-packages, the run dir
    and the operator's tree all at once. Every mutation test aims here and then checks the disk."""
    d = tmp_path / "outside"
    d.mkdir()
    (d / "existing.txt").write_text("original", encoding="utf-8")
    return d


# --------------------------------------------------------------------------------- it works at all

def test_a_probe_answers_the_question_the_developer_would_otherwise_have_guessed():
    """The whole reason this exists: the Developer wrote a fake loguru rather than spend one line
    finding out the real one imports."""
    out = _probe("import json, sys; print('ok', json.dumps({'v': sys.version_info[0]}))")
    assert "exit=0" in out
    assert "ok {\"v\": 3}" in out


def test_a_probe_that_prints_nothing_says_so_rather_than_looking_like_an_empty_answer():
    out = _probe("1 + 1")
    assert "exit=0" in out and "only returns what it PRINTS" in out


def test_a_failing_probe_returns_the_programs_own_traceback_without_the_harness_frames():
    """The launcher's own frames sat in front of every failure — eight lines of a bounded result
    spent on our plumbing, reading as if the harness were what broke."""
    out = _probe("raise ValueError('boom')")
    assert "exit=1" in out and "ValueError: boom" in out
    assert "probe.py" in out                       # the program's own frame survives
    assert "runpy" not in out and "probe_launcher" not in out


# ------------------------------------------------------------------ rule 2: it cannot write. at all

def test_a_probe_cannot_create_a_file(outside):
    target = outside / "made.txt"
    out = _probe(f"open({str(target)!r}, 'w').write('x')")
    assert not target.exists(), "the probe created a file — rule 2 is not holding"
    assert "exit=0" not in out


def test_a_probe_cannot_create_a_file_through_os_open(outside):
    """`open()` is not the only spelling: the audit hook has to read `os.open`'s FLAGS, where the
    mode string it branches on is None."""
    target = outside / "made2.txt"
    out = _probe(f"import os; os.open({str(target)!r}, os.O_WRONLY | os.O_CREAT, 0o644)")
    assert not target.exists()
    assert "exit=0" not in out


def test_a_probe_cannot_truncate_or_append_to_a_file_that_already_exists(outside):
    existing = outside / "existing.txt"
    for mode in ("w", "a", "r+"):
        _probe(f"open({str(existing)!r}, {mode!r}).write('CLOBBERED')")
        assert existing.read_text(encoding="utf-8") == "original", f"mode {mode!r} got through"


def test_a_probe_cannot_delete_rename_or_mkdir(outside):
    existing = outside / "existing.txt"
    _probe(f"import os; os.remove({str(existing)!r})")
    assert existing.exists(), "the probe deleted a file"
    _probe(f"import os; os.rename({str(existing)!r}, {str(outside / 'moved.txt')!r})")
    assert existing.exists() and not (outside / "moved.txt").exists()
    _probe(f"import shutil; shutil.rmtree({str(outside)!r})")
    assert outside.exists(), "the probe removed a tree"
    _probe(f"import os; os.mkdir({str(outside / 'newdir')!r})")
    assert not (outside / "newdir").exists()


def test_a_probe_may_still_read_its_own_workdir(tmp_path):
    """Rule 2 is about WRITES only. A probe that could not read would be useless — reading is the
    whole job — so this pins that neither the write rule nor the confinement below quietly became a
    ban on reading.

    Scoped to the probe's OWN directory, which is what it was always really asserting: the file it
    used to reach lived outside, and reaching outside is now refused on purpose (see the companion
    test)."""
    # It must actually OPEN something. `pathlib.Path('mine.txt')` only constructs a path object and
    # raises no `open` audit event at all, so the previous form passed unchanged even if the confine
    # fence refused every read in the probe's own workdir -- exactly the regression it claims to
    # guard.
    #
    # The file is STAGED rather than written by the probe. Writing it here was this test's own
    # bug: rule 2 is "it cannot write. Anywhere." -- the probe's own cwd included, because a probe
    # that could write its workspace would make `node_created.files` stop being the whole record
    # of what the Developer built. So the write was refused, the probe exited 1, and the read this
    # test exists to pin was never reached. It went unnoticed because it is red only where the
    # rest of the suite is already red for the platform -- which is the argument for reading a
    # DIFF against master rather than a failure count.
    out = DevProbeTools(timeout_s=30, staged=_Staged({"mine.txt": "hello"})).execute(
        "run_probe", {"code": "print('cwd-read-ok:', open('mine.txt').read())"})
    assert "exit=0" in out and "cwd-read-ok: hello" in out


def test_a_probe_cannot_read_outside_its_own_workdir(outside):
    """CONFINEMENT (2026-08-19). The probe's read fence used to be the ENGINE's denylist — it fenced
    the editable source tree and, when a task declared none, installed no fence at all.

    That was measured wrong in the sharpest way: on an AlgoTune benchmark run a Developer spent 150
    of its 239 tool calls inside `run_probe`, reading the BENCHMARK HARNESS sitting beside the run —
    `validation_pipeline.py` (how solutions are checked) and `isolated_benchmark.py` (how they are
    timed). A solver written after reading the checker is not a result; and the reference agent it
    was being compared against has no filesystem access at all, so the asymmetry also destroyed the
    comparison the probe was serving.

    The fence is now an ALLOW-LIST (`read_fence.render(confine=True)`): the probe's own replica plus
    what the interpreter needs to import, and nothing else. Cross-run and cross-node knowledge is
    unaffected — that never travelled through the probe's filesystem."""
    out = _probe(f"print(open({str(outside / 'existing.txt')!r}).read())")
    assert "exit=0" not in out, "the probe read a file outside its own workdir"
    assert "original" not in out, "the probe returned content from outside its workdir"


def test_the_kernel_backstop_is_armed_independently_of_the_audit_hook():
    """RLIMIT_FSIZE 0 is what holds when the hook cannot see the write — a C extension going
    straight to the syscall, or an audit event CPython adds after this was written.

    It is NOT a superset of the hook and the module docstring says so: mutating the hook's write
    rules out while leaving this in place still lets a raw `open` create an EMPTY file and truncate
    an existing one to zero (measured — four tests above go red). The rlimit bounds CONTENT; the hook
    bounds EXISTENCE. Both, or neither claim holds."""
    out = _probe("import resource; print('FSIZE', resource.getrlimit(resource.RLIMIT_FSIZE))")
    assert "FSIZE (0, 0)" in out


# ------------- rule 2, the EXISTENCE half: what neither the audit hook nor RLIMIT_FSIZE can see
#
# Found 2026-08-15. `os.mknod` and `os.mkfifo` raise NO CPython audit event, and `RLIMIT_FSIZE 0`
# bounds bytes and not existence — so both went straight through and left a file on disk. `os.mknod`
# yields a REGULAR ZERO-BYTE file, which satisfies a `needs`/`expect` PRESENCE check and, as an empty
# `.py` in an editable root, shadows a real module for every later node in the run.
#
# The two names are not the class. The sweep below found the class is "anything that creates a
# filesystem entry without going through CPython's `open`", whose wide part has no Python-level name
# at all: `pyarrow.parquet.write_table` raises ZERO audit events and `h5py.File(p, "w")` raises no
# `open`. That is why the fix is a KERNEL rung and not a list — and every test in this block aims
# OUTSIDE the replica and then checks the disk, never the message.

# The path-based filesystem-mutating surface of `os`, one call each. Every entry mutates for real
# when it runs, which is what makes the derivation below a measurement rather than a table.
_OS_MUTATORS = {
    "mkdir": "os.mkdir(P('d'))",
    "rmdir": "os.mkdir(P('rd')); RECORD(); os.rmdir(P('rd'))",
    "remove": "mk('rm'); RECORD(); os.remove(P('rm'))",
    "unlink": "mk('ul'); RECORD(); os.unlink(P('ul'))",
    "rename": "mk('mv'); RECORD(); os.rename(P('mv'), P('mv2'))",
    "replace": "mk('rp'); mk('rp2'); RECORD(); os.replace(P('rp'), P('rp2'))",
    "link": "mk('ln'); RECORD(); os.link(P('ln'), P('ln2'))",
    "symlink": "mk('sl'); RECORD(); os.symlink(P('sl'), P('sl2'))",
    "truncate": "mk('tr'); RECORD(); os.truncate(P('tr'), 0)",
    "chmod": "mk('cm'); RECORD(); os.chmod(P('cm'), 0o600)",
    "chown": "mk('co'); RECORD(); os.chown(P('co'), -1, -1)",
    "utime": "mk('ut'); RECORD(); os.utime(P('ut'), (0, 0))",
    "setxattr": "mk('sx'); RECORD(); os.setxattr(P('sx'), 'user.k', b'1')",
    "removexattr": ("mk('rx'); os.setxattr(P('rx'), 'user.k', b'1'); RECORD(); "
                    "os.removexattr(P('rx'), 'user.k')"),
    "open": "os.close(os.open(P('oc'), os.O_WRONLY | os.O_CREAT, 0o644))",
    "mknod": "os.mknod(P('nod'))",
    "mkfifo": "os.mkfifo(P('fifo'))",
}


def test_the_unaudited_mutator_set_is_re_derived_from_the_interpreter(tmp_path):
    """WHICH filesystem-mutating calls raise no audit event is a fact about CPython, so measure it.

    This is `test_read_fence.py::test_mutation_arg_shapes_match_the_interpreter`'s method for the
    same reason: `_UNAUDITED_MUTATORS` names what the hook CANNOT see, and a release that stops
    auditing a third call would leave the launcher's message rung silently one name short. Run in a
    SUBPROCESS because `sys.addaudithook` is irreversible.

    It does not prove the boundary — the kernel rung does that, and the tests below drive it. It
    proves the two names in the launcher are the two the interpreter actually leaves unaudited."""
    probe = tmp_path / "unaudited.py"
    probe.write_text(textwrap.dedent("""
        import json, os, sys
        D = sys.argv[1]
        CASE = sys.argv[2]
        SEEN = []

        def RECORD():
            # Everything before this point is HARNESS noise — the `exec` of the case itself, and any
            # setup the case does to have something to mutate. Clearing rather than name-filtering
            # keeps the measurement total: an event under ANY name still counts as "audited".
            del SEEN[:]

        def hook(event, args):
            SEEN.append(event)

        sys.addaudithook(hook)

        def P(rel):
            return os.path.join(D, rel)

        def mk(rel):
            with open(P(rel), "wb") as fh:
                fh.write(b"x")

        try:
            exec("RECORD(); " + CASE)
        except BaseException as exc:
            print("@@" + json.dumps({"err": "%s: %s" % (type(exc).__name__, exc)}))
        else:
            print("@@" + json.dumps({"events": SEEN}))
        """), encoding="utf-8")
    unaudited = []
    for name, case in sorted(_OS_MUTATORS.items()):
        work = tmp_path / ("w_" + name)
        work.mkdir()
        res = subprocess.run([sys.executable, str(probe), str(work), case],
                             capture_output=True, text=True)
        assert res.returncode == 0, f"{name}: {res.stderr}"
        row = json.loads([ln for ln in res.stdout.splitlines() if ln.startswith("@@")][-1][2:])
        assert "err" not in row, f"{name} did not run: {row['err']}"
        if not row["events"]:
            unaudited.append(name)
    assert sorted(unaudited) == sorted(dev_probe._UNAUDITED_MUTATORS), (
        "this interpreter's unaudited filesystem mutators are "
        f"{sorted(unaudited)}, the launcher pre-empts {sorted(dev_probe._UNAUDITED_MUTATORS)}")


@pytest.mark.parametrize("call,kind", [
    ("os.mknod(T)", "a regular zero-byte file"),
    ("os.mknod(T, 0o600 | __import__('stat').S_IFIFO)", "a fifo"),
    ("os.mknod(T, 0o600 | __import__('stat').S_IFSOCK)", "a socket"),
    ("os.mkfifo(T)", "a fifo"),
    ("os.mknod(os.path.basename(T), dir_fd=os.open(os.path.dirname(T), os.O_RDONLY))", "via dir_fd"),
    ("os.mkfifo(os.path.basename(T), dir_fd=os.open(os.path.dirname(T), os.O_RDONLY))", "via dir_fd"),
])
def test_a_probe_cannot_create_a_filesystem_entry_cpython_does_not_audit(outside, call, kind):
    target = outside / "shadow.py"
    out = _probe(f"import os\nT = {str(target)!r}\n{call}\nprint('THROUGH')")
    assert not target.exists() and not target.is_symlink(), (
        f"the probe created {kind} — rule 2's existence half is not holding")
    assert "THROUGH" not in out and "exit=0" not in out


def test_the_refusal_for_an_unaudited_mutator_is_not_an_oserror(outside):
    """The type is the whole reason the seam binding is kept beside the kernel rung.

    Landlock answers `EACCES` -> `PermissionError` -> an `OSError`, and `except OSError: <fall back>`
    is THE shape around a file operation — a probe wrapped in one would report success having
    silently done nothing. Where a Python-level name exists, `LoopLabProbeRefused` (not an
    `OSError`) is what the program sees."""
    target = outside / "typed.txt"
    out = _probe("import os\n"
                 f"try:\n    os.mknod({str(target)!r})\n"
                 "except OSError:\n    print('SWALLOWED BY except OSError')\n")
    assert not target.exists()
    assert "SWALLOWED" not in out, "the refusal was caught by `except OSError` — a silent skip"


@pytest.mark.parametrize("mod,code", [
    ("sqlite3", "import sqlite3; c = sqlite3.connect(T); c.execute('create table t(a)')"),
    ("h5py", "import h5py; h5py.File(T, 'w').close()"),
    ("pyarrow", "import pyarrow as pa, pyarrow.parquet as pq; "
                "pq.write_table(pa.table({'a': [1]}), T)"),
])
def test_a_native_writer_cannot_create_a_file_either(outside, mod, code):
    """THE test that says why the fix is a kernel boundary and not a list of names.

    Measured on this box: `pyarrow.parquet.write_table` raises ZERO audit events and `h5py.File(p,
    "w")` raises no `open` — no audit hook can be taught to see them, and before the kernel rung
    both left a 0-byte file at a path the model chose (`sqlite3` and `pyarrow` did; the process then
    died on SIGXFSZ, which is exactly the "bytes, not existence" gap). A `torch.py` created this way
    shadows a real module just as well as one from `os.mknod`."""
    pytest.importorskip(mod)
    target = outside / "native.py"
    out = _probe(f"T = {str(target)!r}\n{code}\nprint('THROUGH')")
    assert not target.exists(), f"{mod} created a file — a name list could never have stopped it"
    assert "THROUGH" not in out


def test_a_unix_socket_cannot_be_bound_into_the_filesystem(outside):
    """`socket.bind` on an AF_UNIX path creates a filesystem entry and raises `socket.bind`, an event
    the probe's `_MUTATE` list never held — audited, and unchecked, which is the same hole from the
    other side."""
    target = outside / "sock"
    out = _probe("import socket\n"
                 f"socket.socket(socket.AF_UNIX).bind({str(target)!r})\nprint('THROUGH')")
    assert not target.exists()
    assert "THROUGH" not in out


def test_ctypes_straight_into_libc_creates_nothing(outside):
    """The WRITE half of this module's `ctypes.dlopen` residual, closed by the kernel rung.

    `libc.open(path, O_CREAT)` bypasses every audit event there is. It now returns -1 with the file
    absent — the C call FAILS rather than raising, which is C semantics and not a refusal, so the
    assertion is about the disk. Rule 3's half of that residual (`execve` through libc) stays open
    and is stated in the module docstring: Landlock ABI 2 does not mediate exec."""
    target = outside / "by_libc.txt"
    _probe("import ctypes, os\n"
           "l = ctypes.CDLL('libc.so.6', use_errno=True)\n"
           f"fd = l.open({str(target)!r}.encode(), os.O_WRONLY | os.O_CREAT, 0o644)\n"
           "print('libc.open ->', fd)\n")
    assert not target.exists(), "libc created a file the kernel rung was supposed to refuse"


def test_the_kernel_no_write_rung_is_applied_and_says_so_when_it_is_not():
    """The rung is best-effort by design (one of three, and the only one that can be missing), so
    what must never happen is a SILENTLY reduced guarantee.

    On this box Landlock is available (ABI 2, kernel 6.1.0-22), so a probe's stderr must be clean.
    Where it is not available the launcher prints one line naming what is no longer covered — that
    branch is what keeps the stated rule and the enforced rule the same sentence."""
    out = _probe("print('ok')")
    assert "exit=0" in out and "ok" in out
    if landlock.unavailable_reason() is None:
        assert "kernel no-write rung could not be applied" not in out
    else:                                     # pragma: no cover — depends on the host kernel
        assert "kernel no-write rung could not be applied" in out


def test_the_audit_hook_is_still_the_only_rung_covering_metadata_and_truncation(outside):
    """The complementarity runs in BOTH directions, which is why the kernel rung is added beside the
    hook and not instead of it.

    Landlock ABI 2 has no ownership or mode access right and `FS_TRUNCATE` arrived in ABI 3, so
    `os.truncate`/`chmod`/`utime` pass the ruleset untouched — measured directly against a bare
    ruleset. All three raise their own audit event, so the hook refuses them with the actionable
    message, and this is the assertion that the file really is unchanged."""
    victim = outside / "existing.txt"
    for code in (f"import os; os.truncate({str(victim)!r}, 0)",
                 f"import os; os.chmod({str(victim)!r}, 0o600)",
                 f"import os; os.utime({str(victim)!r}, (0, 0))"):
        before = (victim.read_text(encoding="utf-8"), victim.stat().st_mode,
                  int(victim.stat().st_mtime))
        out = _probe(code)
        assert "exit=0" not in out, f"{code} was not refused"
        assert (victim.read_text(encoding="utf-8"), victim.stat().st_mode,
                int(victim.stat().st_mtime)) == before


def test_the_kernel_rung_polices_no_read_bit_so_it_cannot_refuse_a_read():
    """The ruleset is the INVERSE of the eval tier's allow-list and that is what makes an EMPTY one
    correct: it handles only mutation rights, so the kernel does not police reads at all. An
    allow-list with zero rules would have denied the probe its own interpreter."""
    assert landlock.NO_MUTATION_HANDLED & (landlock.FS_READ_FILE | landlock.FS_READ_DIR
                                           | landlock.FS_EXECUTE) == 0
    out = _probe("import json, os, sys; print('reads', bool(os.listdir(sys.prefix)), "
                 "json.dumps({'v': 1}))")
    assert "exit=0" in out and "reads True" in out


# ------------------------------------------------------- rule 3: it cannot start another program...

def test_a_probe_cannot_start_another_program(outside):
    target = outside / "by_shell.txt"
    out = _probe("import subprocess; subprocess.run(['/bin/sh', '-c', "
                 f"'echo hi > {target}'])")
    assert not target.exists(), "a subprocess ran and wrote a file"
    assert "exit=0" not in out


def test_a_probe_cannot_start_another_program_through_os_system(outside):
    target = outside / "by_system.txt"
    _probe(f"import os; os.system('touch {target}')")
    assert not target.exists()


def test_a_probe_cannot_start_another_program_through_posix_spawn(outside):
    target = outside / "by_spawn.txt"
    _probe("import os; os.posix_spawn('/bin/sh', ['/bin/sh', '-c', "
           f"'touch {target}'], os.environ)")
    assert not target.exists()


# ------------------------------ ...which is what makes the SOURCE-TREE READ FENCE total on this surface

@pytest.fixture()
def fenced(tmp_path):
    """A repo spec shaped like a real one: an editable source tree holding a human's artifact, and a
    `data:` mount SOURCE inside it, which is the sanctioned read channel and must stay readable."""
    src = tmp_path / "src" / "repo"
    (src / "experiments").mkdir(parents=True)
    (src / "experiments" / "final.txt").write_text("A HUMAN'S CHECKPOINT", encoding="utf-8")
    mount = src / "datasets"
    mount.mkdir()
    (mount / "train.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    spec = {"editables": [{"name": ".", "path": str(src)}],
            "data": {"train": {"path": str(mount), "mount": True}}}
    return DevProbeTools(spec, timeout_s=30), src, mount


def test_a_probe_cannot_read_the_operators_editable_source_tree(fenced):
    tools, src, _mount = fenced
    secret = src / "experiments" / "final.txt"
    out = tools.execute("run_probe", {"code": f"print(open({str(secret)!r}).read())"})
    assert "A HUMAN'S CHECKPOINT" not in out
    assert "exit=0" not in out


def test_a_probe_cannot_reach_the_source_tree_by_running_cat(fenced):
    """THE test for this surface. The read fence is a CPython audit hook and covers nothing outside
    an interpreter, so a shell tool would defeat it entirely — `cat <source>/final.txt` returns the
    bytes and no fence ever sees the read. Rule 3 (no new program) is what closes that, and this is
    the assertion that says so: the secret must not appear in the tool result."""
    tools, src, _mount = fenced
    secret = src / "experiments" / "final.txt"
    for prog in (f"import subprocess; print(subprocess.run(['cat', {str(secret)!r}], "
                 "capture_output=True, text=True).stdout)",
                 f"import os; os.system('cat {secret}')",
                 f"import subprocess; subprocess.call(['cp', {str(secret)!r}, './stolen'])"):
        out = tools.execute("run_probe", {"code": prog})
        assert "A HUMAN'S CHECKPOINT" not in out, f"the source tree leaked through: {prog}"


def test_a_probe_may_still_read_a_declared_data_mount_inside_that_tree(fenced):
    """A mount SOURCE is legally allowed to live inside the editable tree and is the sanctioned read
    channel — 'validate the data' is half of what the operator asked for. The allow-list comes from
    the SAME `read_fence.fence_inputs` the engine's own fence uses."""
    tools, _src, mount = fenced
    out = tools.execute("run_probe", {"code": f"print(open({str(mount / 'train.csv')!r}).read())"})
    assert "exit=0" in out and "1,2" in out


def test_a_task_with_no_editable_tree_gets_no_fence_and_still_probes():
    out = _probe("print('fine')", repo_spec={})
    assert "exit=0" in out and "fine" in out


# ---------------------------- rule 1, the case a `tmp_path` fixture cannot reach: a MACHINE TIER
#                               that CONTAINS the editable tree
#
# Everything above puts the source tree under `tmp_path`, and the shared temp roots are dropped from
# the grant list, so no fixture here ever had a machine tier sitting ABOVE a root. That is the
# blind spot that let a version ship where the tiers were concatenated onto the grant list AFTER
# `fence_inputs` returned, skipping both guarantees that function exists to give: the `_norm_root`
# normalization and the refusal of an allow prefix that CONTAINS a root. `/opt` granted with the
# repo at `/opt/myrepo` is not a wide grant, it is rule 1 switched off — in BOTH halves, because
# the same tuple reaches the kernel rung.
#
# These fixtures therefore use REAL machine tiers: `~/.cache` (a tier `machine_read_tiers` yields
# unconditionally, on the model-cache row) and `sys.prefix` (this interpreter's own, which is what
# makes "just drop the tier" unusable — drop it and python cannot start).

@pytest.fixture()
def under_cache_tier():
    """An editable source tree INSIDE a machine tier, holding the operator's artifact.

    `~/.cache` and not `tmp_path`: the tier has to be one the probe's grant derivation really
    produces, and the temp tiers are the one family it deliberately drops."""
    root = Path.home() / ".cache" / f"looplab-fence-test-{os.getpid()}-{id(object())}"
    try:
        (root / "experiments").mkdir(parents=True)
    except OSError as exc:                              # noqa: PERF203 - a read-only HOME is a skip
        pytest.skip(f"cannot create a fixture under ~/.cache: {exc}")
    (root / "experiments" / "final.txt").write_text("A HUMAN'S CHECKPOINT", encoding="utf-8")
    try:
        yield DevProbeTools({"editables": [{"name": ".", "path": str(root)}]}, timeout_s=60), root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_a_source_tree_under_a_machine_tier_is_still_fenced(under_cache_tier):
    """The reproduction, driven end to end: the tier is granted, the repo is inside it, and the
    probe must still be refused the operator's file. Pre-fix this returned `exit=0` and the
    checkpoint's own bytes in the tool result — the v6 node 4 defect, performed by the tool that
    exists to make it impossible."""
    tools, root = under_cache_tier
    secret = root / "experiments" / "final.txt"
    out = tools.execute("run_probe", {"code": f"print(open({str(secret)!r}).read())"})
    assert "A HUMAN'S CHECKPOINT" not in out, "the tier above the root granted the root"
    assert "exit=0" not in out


def test_the_kernel_half_refuses_it_too_where_the_audit_hook_cannot_look(under_cache_tier):
    """BOTH halves, not one. `ctypes` into libc raises no audit event at all (the module's own
    stated residual), so what answers here is only the Landlock rung — and it was handed the SAME
    un-normalized tuple, which is why one fix has to close both. `EACCES` is the whole answer a
    kernel refusal can give, hence the read is driven and not the message."""
    tools, root = under_cache_tier
    secret = root / "experiments" / "final.txt"
    out = tools.execute("run_probe", {"code": (
        "import ctypes, os\n"
        "libc = ctypes.CDLL(None, use_errno=True)\n"
        f"fd = libc.open({str(secret)!r}.encode(), os.O_RDONLY)\n"
        "print('DENIED' if fd < 0 else os.read(fd, 64))\n")})
    assert "DENIED" in out, f"libc read through the kernel rung was permitted: {out}"
    assert "A HUMAN'S CHECKPOINT" not in out


@pytest.fixture()
def under_interpreter_tier():
    """The same shape one tier up: the editable tree inside the INTERPRETER's own prefix.

    This is the tier the probe's child derives for ITSELF (`sys.prefix`, in the real interpreter,
    where the parent cannot see it), so it is the only fixture that drives the containment rule at
    that second site — and it is the case that makes "just drop the swallowing tier" unusable,
    because a confined process without its own prefix does not start."""
    root = Path(sys.prefix) / f"looplab-fence-test-{os.getpid()}-{id(object())}"
    if not os.access(sys.prefix, os.W_OK):
        pytest.skip("this interpreter's prefix is read-only")
    (root / "experiments").mkdir(parents=True)
    (root / "experiments" / "final.txt").write_text("A HUMAN'S CHECKPOINT", encoding="utf-8")
    try:
        yield DevProbeTools({"editables": [{"name": ".", "path": str(root)}]}, timeout_s=60), root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_a_source_tree_under_the_interpreters_own_prefix_is_still_fenced(under_interpreter_tier):
    """A venv with the checkout inside it, or a conda prefix with one. The grant that swallows the
    root here is not on any list this process wrote — the probe's own interpreter derives it — and
    it is refused there for the same reason and by the same rule."""
    tools, root = under_interpreter_tier
    secret = root / "experiments" / "final.txt"
    out = tools.execute("run_probe", {"code": f"print(open({str(secret)!r}).read())"})
    assert "A HUMAN'S CHECKPOINT" not in out and "exit=0" not in out


def test_the_kernel_half_refuses_the_interpreters_prefix_case_too(under_interpreter_tier):
    """And through the rung the audit hook cannot stand in for. The hook answers the `open` above
    whatever the kernel was granted, so only a read it cannot see says which half is holding —
    `ctypes` into libc raises no audit event, and this is the tier the CHILD granted itself."""
    tools, root = under_interpreter_tier
    secret = root / "experiments" / "final.txt"
    out = tools.execute("run_probe", {"code": (
        "import ctypes, os\n"
        "libc = ctypes.CDLL(None, use_errno=True)\n"
        f"fd = libc.open({str(secret)!r}.encode(), os.O_RDONLY)\n"
        "print('DENIED' if fd < 0 else os.read(fd, 64))\n")})
    assert "DENIED" in out, f"the child granted the tier its own root sits under: {out}"
    assert "A HUMAN'S CHECKPOINT" not in out


def test_the_interpreters_own_tier_survives_a_root_inside_it(under_interpreter_tier):
    """The other half of the same fixture, and the reason the swallowing tier is EXPANDED rather
    than dropped: dropping `sys.prefix` fences the probe by making python unable to import — the
    failure `read_fence._too_broad` exists to prevent one layer down, arriving through the grant
    list instead of the root list. Refusing a read and refusing to run are different answers.

    The witness is DERIVED, not named: an import only proves the tier survived if the module it
    imports really lives in that tier, and a hard-coded `numpy` asserts a fact about what the box
    has installed rather than about the fence (measured 2026-08-22 — the venv this suite runs under
    on one box has no numpy, so the test was red for a reason that was not the boundary). `pytest`
    is in `purelib` by construction here: it is what is running this."""
    tools, _root = under_interpreter_tier
    purelib = _interpreter_witness()
    out = tools.execute("run_probe", {"code": "import pytest; print('imports ok', pytest.__file__)"})
    assert purelib in out, "the witness came from somewhere other than the interpreter tier"
    assert "imports ok" in out and "exit=0" in out, out


def test_a_grant_the_child_derives_for_itself_says_so_when_it_refuses_it(under_interpreter_tier):
    """A derivation the parent cannot see is one the parent cannot fence, so the roots travel with
    the grants and the rule is applied again where the grant is made. LOUD, because a dropped grant
    IS a denial and a silent one reads as a broken interpreter — the same sentence the skipped-rule
    line exists to say."""
    tools, _root = under_interpreter_tier
    out = tools.execute("run_probe", {"code": "print('the-probe-executed')"})
    assert "the fence wins over the grant" in out, out
    assert sys.prefix in out and "exit=0" in out


def test_a_confinement_that_cannot_be_built_refuses_to_run_and_says_so():
    """The one case the expansion cannot answer: the grant IS the root (the repo and the
    interpreter prefix are the same directory). There is no subtree left to grant, and the two
    alternatives are to run unfenced or to say so. It says so — a probe may fail loudly, it may
    never run silently unfenced."""
    tools = DevProbeTools({"editables": [{"name": ".", "path": sys.prefix}]}, timeout_s=30)
    result = tools.execute_result("run_probe", {"code": "print('the-probe-executed')"})
    assert result.is_error and "the-probe-executed" not in result.content
    assert "cannot be built" in result.content and sys.prefix in result.content


def test_a_grant_list_that_swallows_a_root_is_never_installed_as_a_fence(monkeypatch):
    """The last check before the file is written, driven with the DEFECT as its input.

    `read_fence.fence_inputs` already refuses an allow prefix that contains a root — "not dead
    weight, a disabled fence" — and returns it in `swallowed`. The version this replaces discarded
    that fourth element and then appended the machine tiers past it, which is how the derivation and
    the enforcement came apart in the first place. So `swallowed` is asserted rather than assumed:
    if the two ever disagree again the probe refuses to run, which is the one outcome that is never
    silently unfenced."""
    def pre_fix_derivation(self, work_root):
        _roots, allow, _dropped, _swallowed = read_fence.fence_inputs(self.repo_spec, allow=())
        tiers = tuple(path for path, _mode in read_allowlist.machine_read_tiers())
        return tuple(allow) + tiers + (str(work_root),)

    monkeypatch.setattr(DevProbeTools, "_confined_allow", pre_fix_derivation)
    root = str(Path.home() / ".cache" / "looplab-fence-test-absent")
    tools = DevProbeTools({"editables": [{"name": ".", "path": root}]}, timeout_s=30)
    result = tools.execute_result("run_probe", {"code": "print('the-probe-executed')"})
    assert result.is_error and "the-probe-executed" not in result.content
    assert "source root" in result.content


def test_the_kernel_rungs_grant_list_is_itself_refused_a_swallower_not_only_the_hook(monkeypatch):
    """The swallowed net must live at the DERIVATION, not only at `_install_fence`.

    `_confined_allow` is the ONE list both halves of rule 1 project from. The HOOK re-derives
    `fence_inputs` in `_install_fence` and re-checks `swallowed`; the KERNEL rung reads the same
    list through `_read_allow`, which does NOT pass through `_install_fence`. So a `confine_grants`
    regression that returned a grant CONTAINING a root without flagging it in `refused` -- the exact
    "the derivation and the enforcement came apart" drift this slice's history is about -- reached
    the kernel allow-list unchecked, and an allow-list holding the root's own ANCESTOR is rule 1
    switched off in the kernel half. Before the fix, `_confined_allow` returned it silently and only
    `_install_fence`'s separate, order-dependent re-check stood between it and a run.

    Driven by injecting that regression into `confine_grants` directly (empty `refused`), so the
    property is proven at the derivation and does not depend on any real filesystem layout."""
    root = "/opt/looplab-swallow-guard/repo"      # a normal 2+ component root, not `_too_broad`

    def buggy_confine_grants(candidates, roots):
        # Keep the root's ANCESTOR verbatim and flag nothing -- a future derivation bug.
        rootv = tuple(r for r in (read_fence._norm_root(x) for x in roots) if r)
        kept = {read_fence._norm_root(c) for c in candidates}
        kept.discard(None)
        kept.add("/opt/")                          # the swallower: an ancestor of the root
        return tuple(sorted(kept)), ()

    monkeypatch.setattr(read_fence, "confine_grants", buggy_confine_grants)
    tools = DevProbeTools({"editables": [{"name": ".", "path": root}]}, timeout_s=5)

    # (1) the KERNEL rung's own grant list must refuse to be built -- not merely `_install_fence`.
    with pytest.raises(dev_probe.ProbeRefusal) as caught:
        tools._read_allow(Path(tempfile.gettempdir()) / "swallow-guard" / "work")
    assert "/opt/" in str(caught.value) and "source root" in str(caught.value)

    # (2) end to end, the probe refuses the RUN rather than handing `/opt/` to the kernel allow-list.
    result = tools.execute_result("run_probe", {"code": "print('the-probe-executed')"})
    assert result.is_error and "the-probe-executed" not in result.content
    assert "source root" in result.content


def test_the_hook_and_the_kernel_are_told_the_same_thing_about_every_path():
    """THE property. Two enforcement points for one rule, and the day their lists differ the weaker
    one is the boundary. Both halves are projections of `_confined_allow`, so this drives them
    against each other over a corpus of paths that matters: what is inside the tree, what is a
    declared mount inside it, what the interpreter needs, and the sibling that a prefix compare
    without a trailing separator would confuse for the tier.

    The second assertion is the incident that made the single derivation a rule: the kernel rung
    `open(O_PATH)`s each of its grants IN the interpreter the hook is already live in, so a grant
    the hook refuses kills the probe while it is ADDING the rule."""
    root = Path(sys.prefix) / "looplab-fence-test-absent"
    mount = root / "datasets"
    spec = {"editables": [{"name": ".", "path": str(root)}],
            "data": {"train": {"path": str(mount), "mount": True}}}
    tools = DevProbeTools(spec, timeout_s=30)
    grants = tools._confined_allow(Path(tempfile.gettempdir()) / "looplab-probe-x" / "work")
    fenced = _fence_predicate(tools, grants)

    def granted(path):
        p = path if path.endswith(os.sep) else path + os.sep
        return any(p.startswith(g) for g in grants)

    # (1) nothing under the root is reachable through either half — except the declared mount,
    #     which is the sanctioned channel and must stay open in both.
    # Files, not the root directory itself: opening a directory raises `IsADirectoryError` before
    # it can read anything, which is why the `open` hot path does not pay for the trailing-separator
    # compare (`_prefixed` is where a directory is judged, on `os.chdir`).
    for inside in (str(root / "experiments" / "final.txt"), str(root / "train.py")):
        assert not granted(inside), f"the kernel would grant {inside}"
        assert fenced(inside) is not None, f"the hook would allow {inside}"
    assert granted(str(mount / "train.csv")) and fenced(str(mount / "train.csv")) is None
    # (2) every grant the kernel is asked to open passes the hook that is already live.
    for g in grants:
        assert fenced(g) is None, f"the hook refuses a path the kernel rung must open: {g}"
    # (3) the interpreter still has its stdlib, or the probe is fenced by not starting.
    assert granted(os.path.join(sysconfig.get_paths()["stdlib"], "json", "__init__.py"))


def test_a_grant_of_a_tier_does_not_admit_its_sibling():
    """`/opt` must not admit `/optfoo` — the `/src` vs `/srcfoo` bug `_norm_root`'s own docstring
    says it exists to prevent, arriving through the grant list, where under `_CONFINE` the hot path
    is a bare `startswith` against exactly these strings."""
    tools = DevProbeTools({"editables": [{"name": ".", "path": "/opt/looplab-fence-test-absent"}]},
                          timeout_s=30, confine_reads=False)   # the HOOK is the confinement here
    grants = tools._confined_allow(Path(tempfile.gettempdir()) / "looplab-probe-x" / "work")
    fenced = _fence_predicate(tools, grants)
    for g in grants:
        assert g.endswith(os.sep), f"a grant without a trailing separator: {g}"
    for sibling in ("/optfoo/secret", "/usrfoo/secret", "/etcfoo/secret"):
        assert fenced(sibling) is not None, f"a sibling of a granted tier was admitted: {sibling}"


def test_a_task_with_no_editable_tree_installs_no_hook_at_all(tmp_path):
    """`_install_fence` returning False is not a tidiness point: the fence directory goes FIRST on
    the child's PYTHONPATH, so an inert `sitecustomize` of ours displaces the box's own for every
    probe of every non-repo task. There is nothing for a denylist with no roots to refuse — the
    probe is confined by the kernel rung, which is a different rung and still on."""
    fence_dir = tmp_path / "fence"
    fence_dir.mkdir()
    assert DevProbeTools({}, timeout_s=5)._install_fence(fence_dir) is False
    assert list(fence_dir.iterdir()) == []
    # ...and when the HOOK is the confinement, an empty root list is not inert at all: its allow
    # list is the whole boundary, so the fence must still be written.
    assert DevProbeTools({}, timeout_s=5, confine_reads=False)._install_fence(fence_dir) is True
    assert (fence_dir / "sitecustomize.py").exists()


def test_the_probe_of_a_task_with_no_editable_tree_is_still_confined(tmp_path):
    """The companion to the guard above, and why restoring it costs nothing: the AlgoTune shape —
    no editable tree at all, the benchmark harness sitting beside the run — is refused by the
    kernel rung, which is what `confine_reads` turned on."""
    victim = tmp_path / "harness" / "validation_pipeline.py"
    victim.parent.mkdir()
    victim.write_text("HOW SOLUTIONS ARE CHECKED", encoding="utf-8")
    out = _probe(f"print(open({str(victim)!r}).read())", repo_spec={})
    assert "HOW SOLUTIONS ARE CHECKED" not in out and "exit=0" not in out


def _interpreter_witness() -> str:
    """`purelib`, having checked that importing `pytest` really exercises it.

    A guard on the guard: if this interpreter's pytest were vendored or on `PYTHONPATH` rather than
    installed in `purelib`, an import of it would prove nothing about the tier and the test above
    would be green whatever the grant list said."""
    purelib = sysconfig.get_paths()["purelib"]
    if not str(Path(pytest.__file__).resolve()).startswith(purelib):
        pytest.skip("this interpreter's pytest is not in purelib, so it witnesses nothing")
    return purelib


def test_the_grants_always_cover_the_interpreter_that_will_run_the_probe():
    """The invariant behind every "and it still works" assertion in this file, stated once and
    derived from the running interpreter rather than from what this box looks like.

    It is here because it was FALSE and nothing said so: `/tmp` and `/var/tmp` are dropped from the
    grant list (they are granted whole by `read_allowlist`, and granting them hands a probe the
    checkout beside the run), and the drop tested CONTAINMENT — so on a box whose venv is
    `/var/tmp/<checkout>/.venv`, `sys.prefix` and its `site-packages` went with them and a confined
    probe could not import an installed package. A boundary that removes the interpreter has not
    fenced anything; it has stopped the process, which is the outcome `_too_broad` exists to
    prevent one layer down."""
    grants = DevProbeTools({}, timeout_s=5)._confined_allow(
        Path(tempfile.gettempdir()) / "looplab-probe-x" / "work")
    covered = lambda p: any((str(p) + os.sep).startswith(g) for g in grants)     # noqa: E731
    for needed in (sys.prefix, sys.base_prefix, sysconfig.get_paths()["purelib"],
                   sysconfig.get_paths()["stdlib"]):
        assert covered(os.path.realpath(needed)), f"the probe's own interpreter lost {needed}"


def _venv_under_a_temp_root(tmp_path):
    """A plausible interpreter laid out under a shared temp root — `uv`'s default on this box is
    `/var/tmp/<checkout>/.venv`, and `tmp_path` is under `/tmp` for the same reason."""
    prefix = tmp_path / "checkout" / ".venv"
    (prefix / "lib" / "python3.11" / "site-packages").mkdir(parents=True)
    (prefix / "bin").mkdir()
    return prefix


def test_an_interpreter_under_a_shared_temp_root_is_still_granted(tmp_path, monkeypatch):
    """The tier and a path INSIDE it are different statements. What was measured is that granting
    `/tmp` and `/var/tmp` WHOLE hands a probe the AlgoTune checkout beside the run; a venv that
    happens to live under one is not that, and granting it grants no other part of the checkout.

    Driven with a fabricated interpreter so the property does not depend on where this box put its
    python — which is exactly how it shipped broken on the box that puts it under `/var/tmp`."""
    prefix = _venv_under_a_temp_root(tmp_path)
    monkeypatch.setattr(DevProbeTools, "_interpreter_allow",
                        staticmethod(lambda: (str(prefix) + os.sep, "/tmp/", "/var/tmp/", "/usr/")))
    grants = DevProbeTools({}, timeout_s=5)._confined_allow(tmp_path / "probe" / "work")
    covered = lambda p: any((str(p) + os.sep).startswith(g) for g in grants)     # noqa: E731
    assert covered(prefix / "lib" / "python3.11" / "site-packages")
    assert not covered(tmp_path / "checkout" / "harness")
    for tier in ("/tmp", "/var/tmp", os.path.realpath(tempfile.gettempdir())):
        assert not covered(tier), f"the shared temp root {tier} was granted whole"


def test_a_root_that_is_the_interpreter_is_refused_wherever_the_interpreter_lives(tmp_path,
                                                                                  monkeypatch):
    """The second half of the same seam, and the one that decides a guarantee: a candidate the
    derivation drops before `confine_grants` sees it can neither be punched NOR refused, so the
    probe ran without its interpreter and without anyone saying so. It was still fenced — the root
    was granted by nothing — but "refuses to run" was not true on that path, and a guarantee that
    holds only where the box puts python outside `/var/tmp` is not a guarantee."""
    prefix = _venv_under_a_temp_root(tmp_path)
    monkeypatch.setattr(DevProbeTools, "_interpreter_allow",
                        staticmethod(lambda: (str(prefix) + os.sep, "/usr/")))
    tools = DevProbeTools({"editables": [{"name": ".", "path": str(prefix)}]}, timeout_s=5)
    with pytest.raises(dev_probe.ProbeRefusal) as caught:
        tools._confined_allow(tmp_path / "probe" / "work")
    assert str(prefix) in str(caught.value) and "cannot be built" in str(caught.value)


def _fence_predicate(tools, grants):
    """The generated hook's own `_fenced`, for the fence THIS provider would install.

    Exec'd under `read_fence._PROBE_NAME`, the seam that yields the pure predicate without
    installing an irreversible audit hook — and rendered through the same projection
    `_install_fence` uses, so what is driven is the file the probe would really carry."""
    roots, hook_allow, _dropped, swallowed = read_fence.fence_inputs(tools.repo_spec, allow=grants)
    assert not swallowed, f"a grant contains a root: {swallowed}"
    confine = not tools.confine_reads
    src = read_fence.render(roots, grants if confine else hook_allow, policy="deny",
                            run="developer-probe", confine=confine)
    ns = {"__name__": read_fence._PROBE_NAME}
    exec(compile(src, "<fence>", "exec"), ns)       # probe name: no audit hook installed
    return ns["_fenced"]


# ------------------------------------------------------- rule 4: it cannot disturb a sibling's GPU

def test_a_probe_sees_no_gpu_so_it_cannot_allocate_on_one_a_running_node_holds():
    out = _probe("import os; print('CVD=%r' % os.environ.get('CUDA_VISIBLE_DEVICES'))")
    assert "CVD=''" in out


# --------------------------------------------------------------------------- the disposable replica

class _Staged:
    """Stand-in for the live `RepoWriteTools` — only `.files` is read."""

    def __init__(self, files):
        self.files = files


def test_the_probe_runs_in_a_copy_of_what_the_developer_has_staged():
    tools = DevProbeTools(timeout_s=30,
                          staged=_Staged({"conf.py": "N = 41\n", "sub/mod.py": "X = 1\n"}))
    out = tools.execute("run_probe", {"code": "import conf; print('N+1 =', conf.N + 1)"})
    assert "exit=0" in out and "N+1 = 42" in out
    assert "2 staged file(s) replicated" in out


def test_the_replica_cannot_flow_back_into_the_build():
    """One-way by construction: the staged dict the probe was handed must be exactly what it was."""
    files = {"conf.py": "N = 41\n"}
    tools = DevProbeTools(timeout_s=30, staged=_Staged(files))
    tools.execute("run_probe", {"code": "open('conf.py', 'w').write('N = 0')"})
    assert files == {"conf.py": "N = 41\n"}


def test_the_probes_whole_world_is_deleted_when_it_returns():
    """No side effect is not a claim about intent — it is what makes the span-not-event decision
    correct, so it has to be observed."""
    before = {p for p in Path(tempfile.gettempdir()).glob("looplab-probe-*")}
    _probe("print('x')")
    _probe("raise SystemExit(3)")
    _probe("import time; time.sleep(0.05)")
    assert {p for p in Path(tempfile.gettempdir()).glob("looplab-probe-*")} == before


# ------------------------------------------------------------- the bounded output projection

def test_a_verbose_probe_is_tailed_and_still_fits_the_agent_loops_result_cap():
    out = _probe("print('L' * 500_000)")
    assert len(out) <= RESULT_CAP
    assert "truncated" in out


def test_a_verbose_stdout_cannot_push_the_traceback_out_of_the_result():
    """The failure this shape exists to prevent: a chatty probe whose stderr — the reason it failed —
    was dropped by the loop's blunt head-cut. The split is the shared `_base.stream_tails`."""
    out = _probe("import sys; print('L' * 500_000); "
                 "sys.stderr.write('MARKER-THE-REAL-ERROR\\n'); raise ValueError('boom')")
    assert "MARKER-THE-REAL-ERROR" in out or "ValueError: boom" in out
    assert len(out) <= RESULT_CAP


def test_the_two_stream_split_is_the_one_shared_rule_and_not_a_second_copy():
    """`shell_tools.run_command` and `dev_probe.run_probe` report the same shape; two copies is how
    they would come to disagree about which half of a failure survives."""
    import looplab.tools.shell_tools as sh

    assert sh._stream_tails is stream_tails


def test_a_probe_that_will_not_stop_is_killed_and_says_which_bound_it_hit():
    out = _probe("import time; time.sleep(30)", timeout_s=2)
    assert "TIMEOUT" in out and "eval stage" in out


# ------------------------------------------------------------------------------ the provider contract

def test_the_timeout_is_clamped_at_both_ends():
    assert DevProbeTools(timeout_s=10_000).timeout_s == _MAX_TIMEOUT
    assert DevProbeTools(timeout_s=0).timeout_s > 0
    assert DevProbeTools(timeout_s=-5).timeout_s > 0


def test_a_junk_tool_call_never_raises():
    t = DevProbeTools()
    assert "unknown tool" in t.execute("nope", {})
    assert t.execute("run_probe", {}).startswith("(run_probe:")
    assert t.execute("run_probe", None).startswith("(run_probe:")
    assert t.execute("run_probe", {"code": "   "}).startswith("(run_probe:")


def test_a_program_big_enough_to_be_authoring_is_refused_with_that_reason():
    """A 'probe' the size of a module is the Developer routing its authoring around the recorded
    channel — which is the one thing the read-only decision exists to prevent."""
    out = DevProbeTools().execute("run_probe", {"code": "#" * 50_000})
    assert "write_file" in out and "QUESTION" in out


def test_the_generated_launcher_is_readable_valid_python_on_its_own():
    """`render_launcher` is split out so the boundary can be diffed and compiled without a
    subprocess — a generated file that only ever runs inside one is a file nobody reviews."""
    import ast

    from looplab.tools.dev_probe import PROBE_REFUSAL, render_launcher

    src = render_launcher("/some/where/probe.py")
    ast.parse(src)                                   # a syntax error here is a dead probe surface
    assert "/some/where/probe.py" in src
    assert PROBE_REFUSAL in src
    assert "sys.addaudithook" in src and "RLIMIT_FSIZE" in src


def test_the_provider_speaks_the_duck_typed_tool_contract():
    t = DevProbeTools()
    specs = t.specs()
    assert [s["function"]["name"] for s in specs] == ["run_probe"]
    assert t.bind_state(None, None) is None      # optional hook, second arg required at dispatch


# ---------------------------------------------------------- it is a SPAN, not a domain event

def test_the_probe_cannot_append_a_domain_event():
    """The load-bearing decision: rules 2-4 mean a probe has no side effect, so engine invariant #3
    has nothing to gate and the probe is recorded as a `tool` span like every other Developer tool
    call. A negative pin, deliberately (CLAUDE.md): what must not come back is the TEXT — an import
    of the event store here would be someone concluding the opposite without reading why."""
    src = Path(__import__("looplab.tools.dev_probe", fromlist=["_"]).__file__).read_text("utf-8")
    body = src.split('"""', 2)[2]                 # skip the module docstring, which discusses events
    for banned in ("looplab.events", "EventStore", "store.append", "EV_"):
        assert banned not in body, f"{banned!r} appeared in dev_probe's code"


# --------------------------------------------------------------------- the Developer-side wiring

def _developer(probe: bool):
    from looplab.adapters.repo_developer import LLMRepoDeveloper

    dev = LLMRepoDeveloper.__new__(LLMRepoDeveloper)
    dev._probe = probe
    dev._probe_repo_spec = {}
    dev._probe_timeout_s = 30.0
    dev._cross_run_read_tools = False
    dev._editables = []
    return dev


def test_the_probe_reaches_every_developer_phase_through_the_one_composition_point():
    """`_scout_tools` is what all four phases (stages / plan / step / single-session) compose, so a
    probe added there reaches the read-only phases too — a stages phase that cannot check whether a
    library imports declares a pipeline around a library that is not there."""
    names = [s["function"]["name"] for t in _developer(True)._scout_tools(None) for s in t.specs()]
    assert "run_probe" in names
    assert [s["function"]["name"] for t in _developer(False)._scout_tools(None) for s in t.specs()] == []


def test_the_setting_off_restores_the_previous_system_prompt_byte_for_byte():
    """An operator turning the probe off must get the run they had before this shipped, prompt
    included — which is why the clause is spliced at its original position, not appended."""
    import looplab.adapters.repo_developer as rd
    from looplab.core.prompts import render

    off, on = _developer(False), _developer(True)
    off.prompts = on.prompts = None
    assert off._system_body(render) == rd._REPO_DEV_SYSTEM_BODY
    assert "There is NO shell / bash / run-command tool" in off._system_body(render)
    body_on = on._system_body(render)
    assert "There is NO shell / bash / run-command tool" not in body_on
    assert "PROBE BEFORE YOU WORK AROUND SOMETHING" in body_on
    # The head and tail are shared verbatim: only the execution clause differs.
    assert body_on.startswith(rd._REPO_DEV_SYSTEM_BODY_HEAD)
    assert body_on.endswith(rd._REPO_DEV_SYSTEM_BODY_TAIL)


def test_an_operator_prompt_override_still_wins_in_both_configurations():
    from looplab.core.prompts import render

    for flag in (True, False):
        dev = _developer(flag)
        dev.prompts = {"repo_developer_system_body": "MY OWN BODY"}
        assert dev._system_body(render) == "MY OWN BODY"


def test_a_real_repo_task_gets_a_probe_fenced_against_its_own_editable_tree(tmp_path):
    """The whole chain, end to end: `Settings.developer_probe` -> `make_roles` -> the developer's
    toolset -> a probe whose fence is derived from THAT task's editable root.

    The unit tests above build the provider directly, so none of them would notice a `make_roles`
    that forgot to pass the spec — and a probe with an empty spec is an UNFENCED probe that still
    passes every fence test written against a spec it was handed itself."""
    from looplab.adapters.repo_developer import LLMRepoDeveloper
    from looplab.adapters.repo_task import EvalSpec, RepoTask
    from looplab.agents.factory import make_roles
    from looplab.core.config import Settings
    from looplab.core.prompts import render

    src = tmp_path / "repo"
    (src / "experiments").mkdir(parents=True)
    (src / "train.py").write_text("print('hi')\n", encoding="utf-8")
    (src / "experiments" / "human.txt").write_text("A HUMAN'S ARTIFACT", encoding="utf-8")
    task = RepoTask(id="r", goal="g", direction="max", editable_path=str(src),
                    edit_surface=["*.py"],
                    eval=EvalSpec(command=[sys.executable, "train.py"],
                                  metric={"kind": "stdout_json"}))

    def _inner(role):
        # make_roles returns the UnifiedAgent facade by default; the probe lives on the repo
        # developer it delegates to.
        while not isinstance(role, LLMRepoDeveloper) and getattr(role, "developer", None) is not None:
            role = role.developer
        return role

    on = _inner(make_roles(task, Settings(backend="llm", developer_probe=True,
                                          llm_base_url="http://x/v1"))[1])
    tools = on._scout_tools(None)
    names = {s["function"]["name"] for t in tools for s in t.specs()}
    assert "run_probe" in names
    assert "PROBE BEFORE YOU WORK AROUND SOMETHING" in on._system_body(render)

    probe = next(t for t in tools if "run_probe" in {s["function"]["name"] for s in t.specs()})
    leak = probe.execute("run_probe",
                         {"code": f"print(open({str(src / 'experiments' / 'human.txt')!r}).read())"})
    assert "A HUMAN'S ARTIFACT" not in leak, "make_roles wired an UNFENCED probe"
    assert "OK" in probe.execute("run_probe", {"code": "print('OK')"})

    off = _inner(make_roles(task, Settings(backend="llm", developer_probe=False,
                                           llm_base_url="http://x/v1"))[1])
    assert "run_probe" not in {s["function"]["name"]
                               for t in off._scout_tools(None) for s in t.specs()}


@pytest.mark.skipif(os.name == "nt", reason="the boundary's kernel half is POSIX rlimits")
def test_the_engines_own_interpreter_is_what_answers():
    """`env_inspect` answers by IMPORTING in the engine's interpreter; a probe on a different one
    could contradict `pkg_info` about the same package and the Developer would have no way to tell
    which answer was about its eval."""
    out = _probe("import sys; print('EXE', sys.executable)")
    assert f"EXE {sys.executable}" in out


# ---------------------------------------------- the mutation events are ONE table, not two copies


def _launcher_binding(name):
    """The VALUE the generated launcher binds to *name*, without running the launcher.

    It cannot simply be exec'd: `sys.addaudithook` is irreversible and the launcher installs one at
    module level, which would fence the rest of the session. So the assignment is located in the
    parsed source and only its right-hand side is evaluated — `frozenset(...) | frozenset(...)` is
    an expression over builtins, and a comment carrying an event name is not an AST node."""
    import ast

    tree = ast.parse(dev_probe.render_launcher("/tmp/probe.py"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return eval(compile(ast.Expression(node.value), "<launcher>", "eval"), {})
    raise AssertionError(f"the launcher no longer binds {name}")


# What this surface refuses BEYOND the shared registry, and why each is not in it. `read_fence`
# fences paths under a ROOT, where every one of these lowers to an `os.*` event or an `open` on the
# same path; here the rule is "no write anywhere", so the high-level name is refused on its own.
_PROBE_ONLY_MUTATIONS = {
    "os.startfile",
    "shutil.copyfile", "shutil.copymode", "shutil.copystat", "shutil.copytree", "shutil.move",
    "shutil.rmtree", "shutil.unpack_archive",
}


def test_the_launcher_splices_the_mutation_registry_rather_than_keeping_a_second_copy():
    """One table, two surfaces. The `os.*` half of the probe's `_MUTATE` IS
    `read_fence.MUTATION_EVENTS` — which `tests/test_read_fence.py` re-derives from a recording audit
    hook on the running interpreter, so a CPython release that renames or drops an event goes red
    once instead of leaving THIS launcher checking an event nothing raises any more. A hand-kept
    copy inherits none of that guard, which is why the copy was the defect and not the list."""
    mutate = _launcher_binding("_MUTATE")
    assert set(read_fence.MUTATION_EVENTS) <= mutate, (
        "the launcher stopped covering an event the shared registry names")
    assert mutate - set(read_fence.MUTATION_EVENTS) == _PROBE_ONLY_MUTATIONS, (
        "this surface's additions beyond the registry changed; state the new one and why it is not "
        "in the registry")


def test_an_event_added_to_the_registry_reaches_the_launcher_with_no_edit_here(monkeypatch):
    """The splice, driven: that is what "one source of truth" MEANS. Extend the registry and the
    generated launcher refuses the new event — no second list to remember, which is exactly what the
    copy could not do."""
    monkeypatch.setitem(read_fence.MUTATION_EVENTS, "os.futuremutator", ((0, None),))
    assert "os.futuremutator" in _launcher_binding("_MUTATE")


@pytest.mark.parametrize("event", ["os.remove", "os.rename", "os.chmod", "os.utime"])
def test_a_spliced_event_is_live_in_a_real_probe_child(outside, event):
    """…and the spliced set is not just text: each of these is refused by a real probe, with the
    victim file still there afterwards. The registry is the writer; the disk is the assertion."""
    victim = outside / "existing.txt"
    call = {"os.remove": f"os.remove({str(victim)!r})",
            "os.rename": f"os.rename({str(victim)!r}, {str(outside / 'gone')!r})",
            "os.chmod": f"os.chmod({str(victim)!r}, 0o600)",
            "os.utime": f"os.utime({str(victim)!r}, (0, 0))"}[event]
    before = victim.stat()
    out = _probe(f"import os\n{call}\nprint('THROUGH')")
    assert "THROUGH" not in out and event in read_fence.MUTATION_EVENTS
    assert victim.exists() and victim.read_text(encoding="utf-8") == "original"
    assert victim.stat().st_mode == before.st_mode and victim.stat().st_mtime == before.st_mtime
