"""The Developer's PROBE: run a short program against the REAL environment, inside a boundary that
cannot change anything.

WHY THIS EXISTS (F2, docs/29-operator-backlog-2026-08-11.md)
------------------------------------------------------------
The repo Developer writes files and declares eval stages. It has never had a way to RUN anything, so
when it hits a question the read-only inspectors cannot answer it improvises around the question
instead of answering it. The shape the operator saw, verbatim:

    "Since I have no shell/install ability, the cleanest repair is a small loguru shim module"

— i.e. it wrote a fake library rather than checking whether the real one was importable. That check
is one line of Python. `tools/env_inspect.py` covers *static* questions (a version, a signature, an
Enum's members, a grep of installed source); it cannot answer "does this actually import in THIS
interpreter", "is this CSV parseable", "what does this config resolve to".

WHAT THIS IS NOT: A SHELL
-------------------------
The ask was "simple bash commands". This is not bash, and the reason is the one thing that must not
be got wrong here. `runtime/read_fence.py` makes it impossible for a node's process to read the
operator's editable SOURCE tree — the fence that exists because `rubertlite-dr-unified-v6` node 4
trained a good model and then scored a HUMAN's checkpoint an absolute path named, and RECORDED that
number. **The fence is a CPython audit hook**: it covers `open` inside a Python interpreter and it
covers nothing else. A tool that could run `cat`, `cp`, `find` or `bash` would be an execution
surface the fence does not reach — one `cp <source>/final/model.safetensors ./ckpt` away from
laundering somebody else's result into a node's own workspace, which is the v6 defect performed on
purpose. So:

    The probe surface is the INTERPRETER, because the interpreter is what the fence can cover.

That is a BOUNDARY, not an allow-list of commands (docs/36-agent-driven-decisions-2026-08-13.md: a
list of examples rots and the operator has rejected that shape). There is no table of permitted
programs to maintain and no table to fall behind; there is one rule, and it is the same rule that
decides whether the read fence applies at all.

THE FOUR RULES — the whole boundary
-----------------------------------
Each is universal (no path list, no command list), and each closes a recorded incident:

1. **It cannot read the operator's source tree.** The probe carries a fence rendered from
   `read_fence.render` on `read_fence.fence_inputs(repo_spec)` — the SAME derivation the engine
   installs beside the run, not a second policy that could drift from it. Always `deny`, and
   deliberately NOT gated on `Settings.read_fence`: that setting trades the eval tier's fence off
   against cost/compatibility on a training process, and a probe has no training to do. `off` there
   must not silently open a second door here.
2. **It cannot write. Anywhere.** Not site-packages, not the run directory, not the event log, not
   even its own scratch. THREE mechanisms, and what each is for is stated exactly — measured by
   mutating each one out of a throwaway tree and watching which tests survive:
     * the audit hook refuses `open` for write and the filesystem-mutating `os.*`/`shutil.*` events
       — the `os.*` half SPLICED from `read_fence.MUTATION_EVENTS` rather than listed again, so the
       one table `tests/test_read_fence.py` re-derives from the interpreter covers both surfaces.
       This is the rung that produces an ACTIONABLE message, in the one exception type
       `except OSError:` does not swallow. It sees only what CPython AUDITS, which is much less than
       "the ways a file can come into existence" — see the residual below. It is ALSO the only rung
       covering METADATA and truncation: Landlock ABI 2 has no ownership or mode right and
       `FS_TRUNCATE` is ABI 3, so `os.truncate`/`chmod`/`chown`/`utime` go through the kernel rung
       and are refused here (driven, all three, with the victim file unchanged).
     * a Landlock ruleset that HANDLES every filesystem-mutating access right and GRANTS NOTHING
       (`runtime/landlock.py::NO_MUTATION_HANDLED`, applied by the generated launcher before the
       program runs). This is the rung that covers a file's EXISTENCE for EVERY caller — a native
       extension, a `ctypes` call into libc, a syscall CPython does not audit and one it has not
       been taught to audit yet — because the check is the kernel's own path walk. It is the inverse
       of the eval tier's read allow-list and therefore has none of its risk: no read bit is
       handled, so it cannot refuse a read, and it grants nothing, so there is no list to enumerate
       and no rule that can go missing. It is also the only one of the three that can be
       UNAVAILABLE (a kernel without Landlock), and it says so on stderr rather than silently
       reducing the guarantee.
     * `RLIMIT_FSIZE = 0` makes the KERNEL refuse file CONTENT. Its limit is on bytes, NOT on
       existence — with the hook removed, a raw `open` still creates an EMPTY file and still
       truncates an existing one to zero — which is the measurement that says it never was a
       superset of the hook and is why the hole above existed at all.
       **Be honest about what it adds NOW.** With the Landlock rung applied, no route was found by
       which this surface could put bytes in a file that the ruleset would have permitted: every
       byte-write needs a writable descriptor, every writable descriptor needs a path open, and
       `FS_WRITE_FILE` is handled and ungranted. So its independent contribution under an APPLIED
       ruleset is unmeasured rather than demonstrated, and the honest reason it stays is the one
       case that IS demonstrable — it is what holds the CONTENT half on a kernel where Landlock is
       unavailable, i.e. exactly where the third rung is missing. It costs one `setrlimit`.
   Together they close the 2026-08-11 cautionary case, where a mid-run `pip install` corrupted a
   RUNNING node's site-packages (`AttributeError: partially initialized module 'pandas'`) and cost a
   whole repair generation because it read as a code defect. Note WHAT closes it: not a check for
   the word "pip", but the fact that no process on this surface can put bytes in a file — and, one
   rung earlier, that it cannot start pip at all (rule 3).

   **WHY THE KERNEL RUNG IS HERE AT ALL, and why a list of function names was not the fix.**
   Until 2026-08-15 rule 2 was the first and third bullets only, and it was OVERCLAIMED: measured on
   this interpreter (CPython 3.12.11) by re-deriving the audited-event table from a recording audit
   hook — the method `read_fence.MUTATION_EVENTS` is built by, for its reason —
   **`os.mknod` and `os.mkfifo` raise no audit event at all**, and `RLIMIT_FSIZE 0` bounds bytes and
   not existence, so both went straight through a real probe and left a file on disk outside the
   replica. `os.mknod` yields a REGULAR ZERO-BYTE FILE, which satisfies a `needs`/`expect` PRESENCE
   check and, dropped into an editable root as an empty `.py`, shadows a real module for every later
   node in the run — and because rules 2-4 are exactly the reason a probe emits no domain event, a
   write through that hole is invisible to the record.
   Those two are not the class, they are the two spellings inside `os`. The same sweep found the
   hole is wider and its wide part cannot be named: **`pyarrow.parquet.write_table` raises ZERO
   audit events** and `h5py.File(path, "w")` raises no `open`, both creating the file natively; a
   `sqlite3.connect` raises an event nothing was checking; a `socket.bind` on an `AF_UNIX` path
   creates a socket file. Each leaves a 0-byte entry at an attacker-chosen path, i.e. the identical
   consequence with a different name on the front. So binding `os.mknod`/`os.mkfifo` at the
   `sitecustomize` seam and stopping is the shape this module's own docstring rejects one paragraph
   up: a denylist that rots, and one already known to be incomplete on the day it ships. The
   boundary has to be where the syscall is, which is the kernel — the recommendation
   `read_fence.py`'s residual list has carried since 2026-08-13, applied to the WRITE half.
   The seam binding is kept anyway, and only for the MESSAGE (see `_UNAUDITED_MUTATORS` in the
   launcher): the kernel's refusal is an `EACCES` -> `PermissionError` -> an `OSError`, the silent-
   skip shape this surface's refusal type deliberately is not, so where a Python-level name exists
   the probe still answers with `LoopLabProbeRefused`. That is the same division of labour the hook
   already had with `RLIMIT_FSIZE`, and it is not the guarantee — remove the kernel rung and the
   guarantee is gone whatever the seam says.
3. **It cannot start another program.** `subprocess`/`os.exec*`/`os.system`/`posix_spawn` are
   refused. A fork is NOT — a forked child inherits the audit hook, an exec REPLACES it. The rule is
   "no new program", and it is what makes rule 1 total: without it, the fence stops at the first
   `subprocess.run(["cat", ...])`.
4. **It cannot see a GPU.** `CUDA_VISIBLE_DEVICES=""`. The host GPU-pool lease is one file per OS
   user and a run's evals hold real devices for hours; a probe that allocated on one would corrupt a
   sibling node's training rather than its own. `gpu_info` (env_inspect) is how you ask what GPUs
   exist.

WHY IT IS A SPAN AND NOT A DOMAIN EVENT
---------------------------------------
Engine invariant #3 gates every SIDE EFFECT on a domain event so resume-by-replay is idempotent.
Rules 2-4 above are exactly the statement that a probe HAS no side effect: the process cannot write,
cannot spawn, cannot take a device, and its scratch directory is deleted when it returns. There is
nothing for invariant #3 to gate. So the probe is recorded the way every other Developer tool call
is recorded — as a `tool` span in `spans.jsonl` (`agents/tool_loop.py::_run_tool_call`), visible in
the node's trace and conversation views, and NOT folded.

The two decisions are one decision. If the probe could mutate its own workspace it would have to be
a folded event, because the node's files would then have a source the log does not contain and
`node_created.files` would stop being the whole record of what the Developer built. Keeping it
read-only is what buys the cheap answer. It is also `docs/36`'s second corollary applied literally —
*a wider action space must not widen the trusted set*: authoring already has a recorded channel
(`write_file`/`edit_file`, which carry the absolute-path advisory the fence's own docstring quotes),
and a second, unrecorded authoring channel would route around it.

WHAT IT CAN SEE
---------------
The real interpreter and its real site-packages (that is the entire point), the declared `data:` /
`references:` mount sources (the fence allow-lists them), and a DISPOSABLE REPLICA of the files this
node has staged so far — `write.files`, materialized read-only into the probe's cwd. The replica
flows one way only: authoring -> probe, never back. It is the node's own staged content and not the
seeded repo tree, because at build time the node's workdir does not exist yet (RepoWriteTools
COLLECTS writes; `engine/workspace.py::materialize` puts them on disk later, at eval time).

RESIDUALS, stated rather than papered over
------------------------------------------
* `ctypes.dlopen` reaches libc, and libc reaches `execve`. Nothing in an audit hook can stop that;
  the same honesty applies to the read fence's own symlink residual. A probe that does it is not
  improvising around a missing tool, it is defeating a stated boundary. Note the WRITE half of that
  residual is closed as of 2026-08-15 and rule 3's half is not: driven, `libc.open(path, O_CREAT)`
  now returns -1 and creates nothing, because the kernel rung does not care which language asked;
  `execve` is not a filesystem mutation and Landlock ABI 2 does not mediate it.
* **RULE 2 IS A KERNEL GUARANTEE, so it is exactly as available as the kernel.** On a box without
  Landlock (Linux < 5.13, or `CONFIG_SECURITY_LANDLOCK=n`) the third bullet of rule 2 does not
  apply and its EXISTENCE half falls back to what CPython audits — which is not the whole set, and
  `os.mknod`/`os.mkfifo` are then refused by the seam binding while a native writer
  (`pyarrow`, `h5py`) is not. The launcher prints ONE line naming that when it happens, rather than
  running under a quieter version of the same sentence. Verified present here: Landlock ABI 2,
  kernel 6.1.0-22.
* Landlock ABI 2 does not mediate `chmod`/`chown`/`utime`/`ftruncate`-on-an-open-fd — there are no
  access bits for ownership or mode, and `FS_TRUNCATE` arrived in ABI 3. All four raise their own
  audit event and are refused by the hook with the actionable message; the point is only that the
  two rungs are complementary in BOTH directions, and neither is a superset.
* A read is not policed by any of this. That is deliberate — the whole value of a probe is that it
  reads the real environment — and rule 1 is what bounds it.
* Network is not cut. An eval stage on the trusted tier has network today, so cutting it here would
  be a rule the surface it mirrors does not honour; the untrusted tiers get `--network none` from the
  Docker wrap as they always did.
* The run directory is readable (it is not under an editable root, and the engine's own fence
  allow-lists it so a run `--out`-ed inside the repo works). A Developer that reads `events.jsonl`
  learns the run's ledger. That is a context concern, not a record concern: rule 2 means nothing it
  reads can reach the record except through `edit_file`, which is recorded.
* A probe BLOCKS the thread its tool loop is on for up to its timeout — exactly as the loop's own
  synchronous LLM calls already do, and for a fraction as long, which is why it is bounded by a
  wall clock rather than made async for a surface whose every other step is blocking anyway.
"""
from __future__ import annotations

import os
import hashlib
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Optional

from looplab.runtime import landlock, read_fence
from looplab.tools._base import (RESULT_CAP, CancelSignal, ToolCapability, ToolResult,
                                 clip, fn_spec, stream_tails)

# A probe is a QUESTION, not a job. The hard ceiling is well under a training stage's: anything that
# needs longer is a declared eval stage, which is the surface with a metric contract and a repair
# loop attached. `_DEFAULT_TIMEOUT` is what `Settings.developer_probe_timeout_s` carries.
_DEFAULT_TIMEOUT = 60.0
_MAX_TIMEOUT = 300.0

# A program longer than this is authoring, not probing — and authoring belongs in `write_file`,
# where it is recorded. Refused with that reason rather than silently truncated to something that
# would run and mean something else.
_MAX_CODE_CHARS = 40_000

# Bounds on the disposable replica of the node's staged files. A repo Developer stages tens of small
# source files; these exist so a pathological build cannot make every probe copy megabytes.
_MAX_REPLICA_FILES = 400
_MAX_REPLICA_BYTES = 4_000_000

# The bytes we ask `run_argv` to keep per stream before our own tail projection runs. Same value the
# sandbox tiers use; the model-facing cut is `stream_tails`, below.
_MAX_OUTPUT = 64_000

# The refusal text the generated launcher raises. Like the read fence's, it NAMES THE FIX, because
# its reader is a model deciding what to do next — not a human tailing a log.
PROBE_REFUSAL = (
    "refused: a probe cannot {what}. A probe OBSERVES — it reads, imports and computes, and it "
    "changes nothing. If this experiment needs to produce a file, install a package or run a "
    "program, that belongs in the node's own files and its declared eval stages, not here."
)

# The exit status a process gets when the kernel's RLIMIT_FSIZE backstop fires (SIGXFSZ = 25).
# `run_argv` returns a negative return code for a signal, so this is what a write the audit hook
# could not see looks like from the host.
_SIGXFSZ_RC = -25

# The `os` functions that MUTATE the filesystem and raise NO CPython audit event — measured on this
# interpreter (3.12.11) with a recording hook, not remembered, and RE-DERIVED the same way by
# `tests/test_dev_probe.py::test_the_unaudited_mutator_set_is_re_derived_from_the_interpreter`, so a
# release that stops auditing a third call goes red here instead of leaving the launcher one name
# short. Module level rather than buried in the template because a rule nobody can state is a rule
# nobody reviews.
#
# It is the MESSAGE rung and NOT the boundary — the module docstring's rule 2 says which is which,
# and the launcher comment beside its use says it again where the temptation is. `os.mknod` is first
# because it is the sharp one: it yields a regular ZERO-BYTE file.
_UNAUDITED_MUTATORS = ("mknod", "mkfifo")


class ProbeRefusal(Exception):
    """Host-side twin of the generated launcher's own exception class.

    The probe child cannot import looplab (it is a fresh interpreter whose whole point is to be the
    REAL one, which may be a different environment), so the launcher defines its own
    `LoopLabProbeRefused`. This class exists only so host-side code and tests have a NAME for the
    same refusal; the two are matched by message, not by identity — exactly as
    `read_fence.ReadFenceRefusal` is."""


# --------------------------------------------------------------------------- the generated launcher
#
# Kept as ONE self-contained template, for the same reason `read_fence._TEMPLATE` is: the child may
# be a different interpreter in a different virtualenv and can import nothing of ours. It is passed
# as a FILE rather than `-c` so a traceback has real line numbers, and it is executed as argv[1]
# rather than via PYTHONSTARTUP/sitecustomize because that slot is the READ FENCE's — a second
# module named `sitecustomize` on the path would shadow the fence instead of composing with it.
#
# `%` is escaped as `%%` throughout (the template is `%`-formatted, like the fence's).
_LAUNCHER = '''\
"""LoopLab developer PROBE launcher — GENERATED, do not edit.

Installs the probe boundary (no write, no new program), then runs the probe program. See
`looplab/tools/dev_probe.py` for why each rule is here and what it costs to remove one.
"""
import os
import runpy
import sys

_REFUSAL = %(refusal)r
_PROGRAM = %(program)r


class LoopLabProbeRefused(Exception):
    """The probe tried to change something. NOT an OSError subclass, for the read fence's reason:
    `try: ... except OSError: <fall back>` is THE shape around a file operation, and being caught by
    it would turn a refusal into a silent skip."""


# Filesystem mutation that does not go through `open` (a rename, an unlink, a tree copy). Refusing
# these is what makes the message ACTIONABLE; the RLIMIT_FSIZE backstop below is what makes the
# boundary hold for anything not on this list.
#
# The `os.*` half is SPLICED from `read_fence.MUTATION_EVENTS`, the registry that already answers
# "which audited events change a file", not copied out of it. This launcher templates
# `read_fence.fence_inputs`/`render` and `landlock.no_mutation_source()` in the same way, so the
# splice is the module's existing shape; what it buys is that
# `tests/test_read_fence.py::test_mutation_arg_shapes_match_the_interpreter` RE-DERIVES that table
# from a recording audit hook, so an interpreter that renames or drops an event goes red ONCE for
# both surfaces. A hand-kept second copy inherits none of that — it just stops covering the event,
# silently, which is the exact failure mode `_UNAUDITED_MUTATORS` below is re-derived to prevent.
_MUTATE = frozenset(%(mutations)r) | frozenset((
    # This surface's own additions, which the read fence deliberately does NOT carry — and the
    # reason it does not is a fact about IT, not about them. That fence refuses paths under a ROOT,
    # where every one of these lowers to an `os.*` event or an `open` on the SAME path, so a row
    # would be a second name for a refusal that already happened. Here the rule is "no write
    # ANYWHERE": there is no path test to defer to, and refusing at the name the program actually
    # called leaves the raising frame at that `shutil.move` instead of several frames inside the
    # stdlib, which is what the model reads. `os.startfile` is Windows-only and mutates nothing —
    # it is here because it OPENS something with the shell, i.e. rule 3's act under a filesystem
    # spelling.
    "os.startfile",
    "shutil.copyfile", "shutil.copymode", "shutil.copystat", "shutil.copytree", "shutil.move",
    "shutil.rmtree", "shutil.unpack_archive",
))
# Starting another PROGRAM. A fork is deliberately absent: a forked child inherits this hook, an
# exec replaces it. Without this rule the read fence stops at the first subprocess.run(["cat", ...]),
# which is the whole hole this surface exists not to open.
_EXEC = frozenset(("subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.spawn",
                   "pty.spawn", "winreg.CreateKey"))

_WRITE_FLAGS = (getattr(os, "O_WRONLY", 1) | getattr(os, "O_RDWR", 2) | getattr(os, "O_CREAT", 64)
                | getattr(os, "O_TRUNC", 512) | getattr(os, "O_APPEND", 1024))


def _refuse(what):
    raise LoopLabProbeRefused(_REFUSAL.replace("{what}", what))


def _hook(event, args):
    # Hot path first: `open` is the frequent audited event, and a READ open must cost one string
    # scan and return. (Unlike the read fence this hook is not on a training loop's critical path —
    # a probe is a question, bounded by its own timeout — so clarity wins over the last nanosecond.)
    if event == "open":
        mode = args[1] if len(args) > 1 else None
        if mode is None:
            flags = args[2] if len(args) > 2 else 0
            if isinstance(flags, int) and (flags & _WRITE_FLAGS):
                _refuse("write files")
            return
        if isinstance(mode, str) and ("w" in mode or "a" in mode or "x" in mode or "+" in mode):
            _refuse("write files")
        return
    if event in _MUTATE:
        _refuse("create, move or delete files")
    if event in _EXEC:
        _refuse("start another program")
    # Forward compatibility, not a second list: a spelling CPython adds later for the same act
    # ("os.execve2", some future "os.posix_spawnp") must inherit the rule rather than escape it.
    if event[:3] == "os." and ("exec" in event or "spawn" in event):
        _refuse("start another program")


sys.addaudithook(_hook)
# The two filesystem-mutating calls in `os` that CPython 3.12 does NOT audit — re-derived from a
# recording hook on this interpreter, not remembered; `tests/test_dev_probe.py` re-derives the whole
# set the same way, so a third one CPython leaves unaudited goes red rather than quietly through.
# `os.mknod` creates a REGULAR ZERO-BYTE FILE, which passes a `needs`/`expect` presence check and
# shadows a real module as an empty `.py`.
#
# THIS IS THE MESSAGE, NOT THE BOUNDARY, and the distinction is load-bearing. The boundary is the
# Landlock rung below, which refuses these — and `pyarrow`/`h5py`/`sqlite3`/`libc`, which have no
# Python-level name to rebind and are the reason a list of names could never be the boundary. What
# rebinding buys is the refusal TYPE: the kernel answers `PermissionError`, an `OSError`, i.e. the
# shape `except OSError: <fall back>` swallows into a silent skip, and where a Python name exists we
# can still answer with `LoopLabProbeRefused`. Delete the Landlock rung and this list guarantees
# nothing; delete this list and the boundary holds with a worse message.
_UNAUDITED_MUTATORS = %(unaudited)r


def _refuse_unaudited(_name):
    def _blocked(*_a, **_k):
        _refuse("create, move or delete files")
    _blocked.__name__ = _name
    return _blocked


for _fname in _UNAUDITED_MUTATORS:
    if hasattr(os, _fname):
        setattr(os, _fname, _refuse_unaudited(_fname))
%(landlock)s
# The kernel EXISTENCE rung. Handles every filesystem-mutating access right and grants nothing, so
# no path on the box can gain, lose or change a file — checked in the kernel's own path walk, which
# is why it covers a native writer (`pyarrow.parquet.write_table` raises ZERO audit events and
# `h5py` raises no `open`), a `ctypes` call into libc, and any syscall CPython has not been taught to
# audit. Applied AFTER the hook so the hook owns every case it can explain with a non-`OSError`
# message, and it handles no READ bit, so it cannot refuse a read the probe exists to make.
#
# Best-effort with a STATED limit rather than a refusal to run: this is one of three rungs and the
# only one that can be missing, and refusing would break the probe surface on a kernel without
# Landlock instead of narrowing what it claims. One line, on the probe's own stderr, which is where
# both the model and the operator read it.
_LL_REASON = %(landlock_fn)s()
if _LL_REASON:
    sys.stderr.write(
        "LOOPLAB probe: the kernel no-write rung could not be applied (%%s). The audit hook and "
        "RLIMIT_FSIZE 0 still hold, so nothing can put BYTES in a file, but a native writer "
        "(pyarrow/h5py/ctypes) can still CREATE an empty one.\\n" %% (_LL_REASON,))
%(read_rung)s# The probe's cwd is its disposable workspace replica, so a staged module must be importable by the
# name the Developer knows it by. `python <launcher>` puts the LAUNCHER's directory on sys.path, not
# the cwd, and without this `import train` fails for a file the model can see with os.listdir().
sys.path.insert(0, os.getcwd())
# The kernel backstop. RLIMIT_FSIZE 0 means no process on this surface can put BYTES in a file — not
# through a C extension, not through a raw syscall, not through an audit event that did not exist
# when the hook above was written. It bounds content, not existence: on its own it would still let a
# raw open create an empty file or truncate one to zero, which is exactly why the hook above covers
# creation/truncation/unlink and this covers everything the hook cannot see. Set AFTER the hook so
# the hook owns every case it can explain, and inherited by anything the process manages to start.
# stdout/stderr are pipes, which RLIMIT_FSIZE does not govern, so the probe can still answer.
try:
    import resource

    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
except Exception:      # noqa: BLE001 — no `resource` module (Windows): the audit hook stands alone
    pass
# No .pyc anywhere: importing a replica module would otherwise try to write __pycache__ and be
# refused by our own hook, turning a legitimate import into a confusing refusal.
sys.dont_write_bytecode = True

try:
    runpy.run_path(_PROGRAM, run_name="__main__")
except SystemExit:
    raise
except BaseException:
    # Print the traceback WITHOUT this launcher's own frames and without `<frozen runpy>`. Those
    # eight lines sit in front of every failure, and they are eight lines of a bounded tool result
    # that the model spends re-reading OUR plumbing instead of its error — and worse, they read as
    # if the probe harness were what broke. The refusal frames go too: the refusal MESSAGE is the
    # actionable part, its call stack inside the audit hook is not.
    import traceback

    _t, _v, _tb = sys.exc_info()
    _frames = [f for f in traceback.extract_tb(_tb)
               if f.filename != __file__ and not f.filename.startswith("<frozen ")]
    sys.stderr.write("Traceback (most recent call last):\\n")
    sys.stderr.write("".join(traceback.format_list(_frames)))
    sys.stderr.write("".join(traceback.format_exception_only(_t, _v)))
    sys.exit(1)
'''


# The READ rung, spliced ONLY when a caller asks for confinement. Kept out of `_LAUNCHER`
# rather than left inert inside it so that a probe nobody confined renders the byte-identical
# launcher it always did -- the same rule `_system_body` follows for `developer_probe=False`.
_READ_RUNG = '''\
%(landlock_read)s
# THE READ RUNG. Only when the caller asked for it (`confine_reads`), and then it FAILS CLOSED —
# unlike the write rung above, which degrades with a stated limit. The asymmetry is the point: the
# write rung is one of three, so losing it narrows a guarantee that still partly holds; this is the
# ONLY thing standing between a benchmark contestant and the grader, and a probe that runs without
# it produces a number nobody can defend. A caller that has not asked for confinement is unaffected.
#
# The allow-list is derived HERE, inside the probe's own interpreter, not templated from the parent:
# the parent may be a different interpreter with a different prefix, and an allow-list computed for
# the wrong python denies python itself. Measured while building this: allowing the PARENT's
# `sys.prefix` left numpy/scipy/sklearn at PermissionError, with nothing to say why.
_LL_READ_EXTRA = %(read_allow_extra)r
if _LL_READ_EXTRA is not None:
    import site as _ll_site
    import sysconfig as _ll_sysconfig

    # The interpreter, plus the system directories every CPython needs to start and to load a
    # shared library. They are here rather than left to the caller because a forgotten path under an
    # allow-list is a SILENT DENIAL that reads as a broken interpreter: measured while building this,
    # omitting `/opt` made python itself unopenable, and omitting these made `import numpy` an
    # ImportError with nothing to say why. None of them is a secret; the grader is not under them.
    _ll_allow = {os.getcwd(), sys.prefix, sys.base_prefix, os.path.dirname(sys.executable),
                 _ll_sysconfig.get_paths().get("stdlib") or "",
                 _ll_sysconfig.get_paths().get("purelib") or "",
                 "/usr", "/lib", "/lib64", "/bin", "/sbin", "/etc", "/opt", "/dev", "/proc/self"}
    for _fn in (_ll_site.getsitepackages, _ll_site.getusersitepackages):
        try:
            _got = _fn()
        except Exception:                       # noqa: BLE001 — absent in some venvs; prefixes cover it
            continue
        _ll_allow.update([_got] if isinstance(_got, str) else _got)
    _ll_allow.update(_LL_READ_EXTRA)
    _ll_reason, _ll_skipped = %(landlock_read_fn)s(sorted(p for p in _ll_allow if p))
    if _ll_reason:
        sys.stderr.write(
            "LOOPLAB probe: read confinement was REQUIRED and could not be applied (%%s). Refusing "
            "to run: without it this surface can read the evaluation harness, and a result produced "
            "beside a readable grader is not a result.\\n" %% (_ll_reason,))
        sys.exit(3)
    if _ll_skipped:
        # A skip under an allow-list IS a denial. Naming it is the difference between "the
        # interpreter is broken" and "add this path": upstream measured 211 candidates -> 55 rules,
        # i.e. 156 silent denials, and that is what this line exists to make loud.
        sys.stderr.write("LOOPLAB probe: read allow-list skipped %%d path(s), which the kernel now "
                         "DENIES: %%s\\n" %% (len(_ll_skipped), _ll_skipped))

'''


def render_launcher(program_path: str, read_allow: Optional[tuple] = None) -> str:
    """The generated launcher source for one probe. Split out so the boundary can be read, diffed
    and driven directly by `tests/test_dev_probe.py` rather than only through a subprocess.

    `landlock` is spliced as a VALUE of this one `%`-format, not concatenated afterwards, which is
    what keeps `landlock.no_mutation_source()`'s own `%` spellings out of this template's escaping
    rules — `%`-formatting does not re-scan what it substitutes."""
    return _LAUNCHER % {"refusal": PROBE_REFUSAL, "program": str(program_path),
                        "unaudited": tuple(_UNAUDITED_MUTATORS),
                        # SORTED so two renders of one registry are byte-identical: a dict's
                        # order is its insertion order, and a launcher that changes shape when
                        # someone
                        # reorders a table is a diff nobody can read.
                        "mutations": tuple(sorted(read_fence.MUTATION_EVENTS)),
                        "landlock": landlock.no_mutation_source(),
                        "landlock_fn": landlock.NO_MUTATION_FUNCTION,
                        # Spliced only when confinement is asked for, so a launcher that was not
                        # asked for it is byte-identical to what it has always been.
                        "read_rung": ("" if read_allow is None else _READ_RUNG % {
                            "landlock_read": landlock.read_confine_source(),
                            "landlock_read_fn": landlock.READ_CONFINE_FUNCTION,
                            "read_allow_extra": tuple(read_allow)})}


class DevProbeTools:
    """ToolProvider (`specs()`/`execute()`) giving the repo Developer ONE tool: run a short Python
    program against the real environment, inside the boundary this module's docstring states.

    `repo_spec` is the task's own spec (`adapters/tasks.py::repo_spec`) — the SAME input
    `engine/resources.py::_read_fence_dir` hands the engine's fence, so the probe's fence and the
    eval's fence are derived from one function and cannot drift apart. `staged` is the live
    `RepoWriteTools` whose `files` dict is replicated, read-only, into the probe's cwd."""

    def __init__(self, repo_spec: Optional[dict] = None, *, timeout_s: float = _DEFAULT_TIMEOUT,
                 staged=None):
        self.repo_spec = repo_spec or {}
        self.timeout_s = max(1.0, min(float(timeout_s or _DEFAULT_TIMEOUT), _MAX_TIMEOUT))
        self.staged = staged

    def bind_state(self, state=None, parent=None) -> None:
        return None

    def specs(self) -> list[dict]:
        return [
            fn_spec("run_probe",
                    "Run a short PYTHON program to CHECK something you would otherwise have to "
                    "GUESS — whether a package really imports here, whether a data file parses, what "
                    "a config resolves to, whether the code you just staged has the API right. "
                    "Returns exit code + stdout/stderr. USE THIS INSTEAD OF INVENTING A WORKAROUND: "
                    "if you are about to write a shim, a stub or a fallback because you are not sure "
                    "something exists, probe it first — it usually does.\n"
                    "You have NO SHELL, and this is the equivalent; the bash you would have typed "
                    "has a one-line Python spelling: `ls` -> os.listdir('.'), `find` -> "
                    "pathlib.Path('.').rglob('*.py'), `head -5 f` -> open('f').readlines()[:5], "
                    "`wc -l f` -> sum(1 for _ in open('f')), `python -c 'import x'` -> just import "
                    "x. For GPUs use gpu_info, not nvidia-smi.\n"
                    "The probe OBSERVES and changes nothing: it CANNOT write files, install "
                    "packages, run other programs or use a GPU, and it cannot read the operator's "
                    "source tree (you run in a copy of the files you have staged so far — the same "
                    "paths write_file uses). Anything that must produce a file belongs in your "
                    "node's files and its declared eval stages. PRINT what you want to see; only "
                    "stdout/stderr come back, each as a tail if long.",
                    {"code": {"type": "string", "description": "the Python program to run; print() "
                              "whatever you need to see"},
                     "timeout": {"type": "number", "description": "seconds before the probe is "
                                 f"killed (default {int(self.timeout_s)}, max {int(_MAX_TIMEOUT)})"}},
                    ["code"]),
        ]

    def capabilities(self) -> list[ToolCapability]:
        schema = self.specs()[0]["function"]["parameters"]
        return [ToolCapability(
            name="run_probe", effect="execute", risk="medium", idempotency="idempotent",
            concurrency_safe=True, cancellable=True, approval="never", input_schema=schema,
            output_schema={"type": "object", "properties": {
                "exit_code": {"type": ["integer", "null"]},
                "timed_out": {"type": "boolean"}, "cancelled": {"type": "boolean"},
                "output": {"type": "string"}}},
            source="looplab.tools.dev_probe")]

    def execute(self, name: str, args: dict) -> str:
        return self.execute_result(name, args).content

    def execute_result(self, name: str, args: dict, *, cancel_check=None) -> ToolResult:
        args = args or {}
        if name != "run_probe":
            return ToolResult(content=f"(unknown tool: {name})", is_error=True, retryable=False,
                              provenance={"source": "developer_probe"})
        code = str(args.get("code") or "")
        try:
            text = self._probe(code, args.get("timeout"), cancel_check=cancel_check)
        except Exception as e:  # noqa: BLE001 — a provider returns an error string, never raises
            return ToolResult(content=f"(probe error: {type(e).__name__}: {e})", is_error=True,
                              retryable=False, provenance={"source": "developer_probe"})
        first = text.splitlines()[0] if text else ""
        match = re.match(r"exit=(-?\d+)", first)
        exit_code = int(match.group(1)) if match else None
        timed_out = "TIMEOUT" in first
        cancelled = "CANCEL" in first
        return ToolResult(
            content=text,
            structured={"exit_code": exit_code, "timed_out": timed_out,
                        "cancelled": cancelled, "output": text},
            is_error=(exit_code != 0 or timed_out or cancelled),
            retryable=True if (timed_out or cancelled) else False,
            provenance={"source": "developer_probe"},
            receipt={"code_sha256": hashlib.sha256(
                code.encode("utf-8", errors="replace")).hexdigest()})

    # ------------------------------------------------------------------------------------ the run
    def _probe(self, code: str, timeout, *, cancel_check=None) -> str:
        if not code.strip():
            return "(run_probe: give a `code` string — the Python program to run)"
        if len(code) > _MAX_CODE_CHARS:
            return (f"(run_probe: that program is {len(code)} chars; a probe is a QUESTION, not a "
                    f"job. Keep it under {_MAX_CODE_CHARS} — anything bigger is code your node "
                    "should own, written with write_file.)")
        to = max(1.0, min(float(timeout or self.timeout_s), _MAX_TIMEOUT))
        # `mkdtemp`, and a `finally` that removes it: the probe's whole world is disposable, which is
        # the reason it needs no durable event (see the module docstring). The engine PROCESS creates
        # and destroys it — the probe itself cannot, by rule 2.
        root = Path(tempfile.mkdtemp(prefix="looplab-probe-"))
        try:
            work = root / "work"
            work.mkdir()
            fence_dir = root / "fence"
            fence_dir.mkdir()
            replica_note = self._replicate(work)
            program = root / "probe.py"
            program.write_text(code, encoding="utf-8")
            launcher = root / "probe_launcher.py"
            launcher.write_text(render_launcher(str(program)), encoding="utf-8")
            from looplab.runtime.sandbox import run_argv
            env = {
                # Belt to the launcher's braces: no bytecode written anywhere, so an import of a
                # replica module cannot be refused for writing a __pycache__ nobody asked for.
                "PYTHONDONTWRITEBYTECODE": "1",
                # Rule 4. A run's evals hold real devices for hours behind a host-wide pool lease;
                # a probe that allocated on one would break a SIBLING node's training.
                "CUDA_VISIBLE_DEVICES": "",
            }
            # The fence gets a directory of its OWN, holding exactly `sitecustomize.py`. That
            # directory goes on PYTHONPATH, and `read_fence`'s own docstring states the rule this
            # honours: "the PYTHONPATH entry contains exactly one importable name — every additional
            # module in that directory would shadow a real one for every process in the run". Putting
            # the fence beside `probe.py`/`probe_launcher.py` would have shadowed three.
            if self._install_fence(fence_dir):
                # `run_argv` consumes this marker and prepends the directory to the child's
                # PYTHONPATH, which is what makes CPython import our `sitecustomize` at startup.
                # Set only when there is a fence to carry, so a non-repo task's probe env stays
                # byte-identical to what it would be without this module.
                env[read_fence.FENCE_DIR_ENV] = str(fence_dir)
            # `sys.executable`, deliberately: `env_inspect` answers by IMPORTING in the engine's own
            # interpreter, so a probe on any other one could contradict `pkg_info` about the same
            # package and the Developer would have no way to tell which answer was about its eval.
            #
            # `-P` (PYTHONSAFEPATH) for the same shadowing reason: without it CPython prepends the
            # SCRIPT's directory, which is the probe's scratch, making `probe`/`probe_launcher`
            # importable names ahead of the real ones. It does not touch PYTHONPATH, so the fence is
            # unaffected, and the launcher puts the replica cwd on the path explicitly instead.
            signal = CancelSignal(cancel_check)
            rc, out, err, timed_out = run_argv(
                [sys.executable, "-P", str(launcher)], str(work), to, env=env,
                max_output_bytes=_MAX_OUTPUT, cancel=signal)
            cancelled = signal.is_set()
            timed_out = bool(timed_out and not cancelled)
        finally:
            shutil.rmtree(root, ignore_errors=True)
        return self._project(rc, out, err, timed_out, to, replica_note, cancelled=cancelled)

    def _install_fence(self, fence_dir: Path) -> bool:
        """Render THIS run's source-tree read fence into the probe's own fence directory.

        Derived from `read_fence.fence_inputs` / `render` — the same two functions
        `engine/resources.py::_read_fence_dir` uses — so there is one policy with two installation
        sites, not two policies. The site differs because the lifetime does: the engine's fence lives
        beside the run and is shared by every eval, and the probe's directory exists for one call.

        `allow` is only what `fence_inputs` derives from the spec (the `data:`/`references:` mount
        sources). The engine additionally allow-lists its run directory, because a run may be
        `--out`-ed INSIDE the repo it edits; a probe has no run directory to protect, so passing one
        would widen this fence for nothing.

        Policy is always `deny` and deliberately NOT `Settings.read_fence` — see rule 1 in the module
        docstring. Returns whether a fence was written: `False` means the task declares no editable
        source tree at all, i.e. there is nothing a probe could read that the node does not own."""
        roots, allow, _dropped, _swallowed = read_fence.fence_inputs(self.repo_spec, allow=())
        # CONFINE, not the engine's denylist. The old shape fenced only the editable source tree and
        # returned False when a task declared none -- installing NO fence at all, on the reasoning
        # that "there is nothing a probe could read that the node does not own". That reasoning holds
        # only if the editable tree is the sole interesting thing on the disk, and it is not:
        # measured 2026-08-19, a Developer spent 150 of its 239 tool calls reading the BENCHMARK
        # HARNESS beside the run -- its `validation_pipeline.py` (how solutions are checked) and
        # `isolated_benchmark.py` (how they are timed). A solver written after reading the checker is
        # not a result, and the reference agent it was being compared against has no filesystem at
        # all, so the asymmetry also broke the comparison it existed to serve.
        #
        # The allow-list is the probe's own disposable replica plus what the interpreter needs to
        # start and import -- nothing else, by construction rather than by enumeration of what to
        # refuse. Cross-run and cross-node knowledge is unaffected: it never travels through the
        # probe's filesystem, it arrives through the run-reading tools, which are unchanged.
        allow = tuple(allow) + self._interpreter_allow(fence_dir.parent)
        (fence_dir / "sitecustomize.py").write_text(
            read_fence.render(roots, allow, policy="deny", log="", run="developer-probe",
                              confine=True),
            encoding="utf-8")
        return True

    @staticmethod
    def _interpreter_allow(work_root) -> tuple:
        """The paths a confined probe must still reach: its own tree, and the interpreter.

        Denying the interpreter does not fence a probe, it stops python from starting -- which is the
        failure `read_fence._too_broad` exists to prevent one layer down. Everything here is derived
        from the running interpreter rather than listed, so a venv, a conda env and a system python
        all work without a per-machine path table."""
        import site
        import sysconfig
        paths = {str(Path(work_root).resolve()), sys.prefix, sys.base_prefix,
                 sysconfig.get_paths().get("stdlib") or "", sysconfig.get_paths().get("purelib") or ""}
        try:
            paths.update(site.getsitepackages())
        except Exception:  # noqa: BLE001 - absent inside some venvs; the prefixes above still cover it
            pass
        try:
            paths.add(site.getusersitepackages())
        except Exception:  # noqa: BLE001
            pass
        # The MODEL CACHE and the temp dir, from the same derivation the eval launcher uses rather
        # than a second hand-written list. `read_allowlist` exists because "a base-model download is
        # a read every repo task makes"; without those tiers a confined probe doing
        # `AutoModel.from_pretrained(...)` is refused `~/.cache/huggingface/...` and dies on a
        # non-OSError that library `except OSError` fallbacks cannot catch -- i.e. the probe is
        # broken for the most ordinary thing a Developer would check.
        try:
            from looplab.runtime import read_allowlist
            paths.update(path for path, _mode in read_allowlist.machine_read_tiers())
        except Exception:  # noqa: BLE001 - the interpreter tiers above still let python start
            pass
        # NORMALIZED through the fence's own rule. These entries are appended AFTER `fence_inputs`
        # has run, so they skip the `_norm_root` every other root and allow entry goes through --
        # and under `_CONFINE` the hot path is a bare `p.startswith(_ALLOW)`. Without the trailing
        # separator an allow entry `/opt/venv` also admits `/opt/venv-secrets`, which is exactly the
        # `/srcfoo` vs `/src` bug `_norm_root`'s docstring says it exists to prevent; without the
        # realpath a symlinked venv fails its own compare and the probe is refused its stdlib.
        normalized = []
        for raw in paths:
            if not raw:
                continue
            norm = read_fence._norm_root(raw)
            if norm:
                normalized.append(norm)
        return tuple(sorted(set(normalized)))

    def _replicate(self, work: Path) -> str:
        """Materialize the node's staged files into the probe's cwd, bounded.

        One-way by construction: the probe cannot write (rule 2), so nothing here can flow back into
        the build. Bounded because this runs per probe — an over-cap build gets a stated note, never
        a silently partial workspace (the failure that would produce is an ImportError the model
        cannot explain)."""
        files = dict(getattr(self.staged, "files", None) or {})
        if not files:
            return ""
        written, used, skipped = 0, 0, 0
        for rel in sorted(files):
            body = str(files[rel] or "")
            if written >= _MAX_REPLICA_FILES or used + len(body) > _MAX_REPLICA_BYTES:
                skipped += 1
                continue
            dest = (work / rel).resolve()
            # Containment: the staged keys come from RepoWriteTools, which path-validates them, but
            # this is the site that turns a key into a real filesystem write in the ENGINE process.
            # A key that escaped would write outside the disposable directory, where nothing else
            # here can undo it.
            if not str(dest).startswith(str(work.resolve()) + os.sep):
                skipped += 1
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(body, encoding="utf-8")
            written += 1
            used += len(body)
        note = f"\n(workspace: {written} staged file(s) replicated here"
        if skipped:
            note += f"; {skipped} omitted to stay under the replica cap — read them with read_file"
        return note + ")"

    def _project(self, rc, out, err, timed_out, to: float, note: str, *, cancelled=False) -> str:
        """The bounded output projection: exit status, then each stream as a TAIL.

        Tails, not heads, for the reason `clip(keep="tail")` exists — the end of a command's output
        is where the exception and the final printed line are. The per-stream split is the SHARED
        `_base.stream_tails`, so this surface and the assistant's `run_command` cannot come to
        disagree about which half of a failure survives the agent loop's `RESULT_CAP`."""
        head = f"exit={rc}"
        if timed_out:
            head += f" (TIMEOUT after {int(to)}s — a probe is a question; a long job is an eval stage)"
        elif cancelled:
            head += " (CANCELLED by the user)"
        elif rc == _SIGXFSZ_RC:
            # The kernel backstop fired, i.e. something wrote through a path the audit hook cannot
            # see. Say what happened, in the hook's own words — an unexplained "exit=-25" reads as a
            # crash in the thing being probed, which is exactly the misdiagnosis this note prevents.
            head += " — " + PROBE_REFUSAL.replace("{what}", "write files")
        parts = [head + note]
        out_take, err_take = stream_tails(out or "", err or "")
        if out and out.strip():
            parts.append("stdout:\n" + clip(out, out_take, keep="tail", note="…(truncated)…\n"))
        if err and err.strip():
            parts.append("stderr:\n" + clip(err, err_take, keep="tail", note="…(truncated)…\n"))
        # On kernels without Landlock the launcher contributes one honest reduced-guarantee line
        # to stderr. That is harness telemetry, not an answer from a program that printed nothing;
        # retain the actionable empty-answer hint instead of letting the warning mask it.
        err_text = (err or "").strip()
        reduced_warning_only = (
            err_text.startswith(
                "LOOPLAB probe: the kernel no-write rung could not be applied")
            and "\n" not in err_text
        )
        if not (out or "").strip() and (not err_text or reduced_warning_only):
            parts.append("(no output — a probe only returns what it PRINTS)")
        return clip("\n".join(parts), RESULT_CAP, keep="head", reserve=80,
                    note="\n(probe result clipped)")
