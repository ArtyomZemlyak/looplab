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
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

from looplab.runtime import landlock, read_fence
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
    out = _probe("import pathlib; p = pathlib.Path('mine.txt'); print('cwd-read-ok')")
    assert "exit=0" in out and "cwd-read-ok" in out


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
