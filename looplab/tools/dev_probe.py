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
   even its own scratch. Two mechanisms that cover DIFFERENT things, and neither is redundant —
   measured by mutating each one out and watching which tests survive:
     * the audit hook refuses `open` for write and the filesystem-mutating `os.*`/`shutil.*` events.
       This is the rung that covers a file's EXISTENCE — creation, truncation, unlink, rename — and
       it is the one that produces an ACTIONABLE message.
     * `RLIMIT_FSIZE = 0` makes the KERNEL refuse file CONTENT, to anything the hook cannot see: a C
       extension going straight to the syscall, or any audit event CPython adds after this was
       written. Its limit is on bytes, NOT on existence — with the hook removed, a raw `open` still
       creates an EMPTY file and still truncates an existing one to zero. So the rlimit is not a
       superset of the hook and must not be described as one; it is what stops anything of
       consequence being PUT anywhere, and the hook is what stops something being destroyed.
   Together they close the 2026-08-11 cautionary case, where a mid-run `pip install` corrupted a
   RUNNING node's site-packages (`AttributeError: partially initialized module 'pandas'`) and cost a
   whole repair generation because it read as a code defect. Note WHAT closes it: not a check for
   the word "pip", but the fact that no process on this surface can put bytes in a file — and, one
   rung earlier, that it cannot start pip at all (rule 3).
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
  improvising around a missing tool, it is defeating a stated boundary.
* Network is not cut. An eval stage on the trusted tier has network today, so cutting it here would
  be a rule the surface it mirrors does not honour; the untrusted tiers get `--network none` from the
  Docker wrap as they always did.
* The run directory is readable (it is not under an editable root, and the engine's own fence
  allow-lists it so a run `--out`-ed inside the repo works). A Developer that reads `events.jsonl`
  learns the run's ledger. That is a context concern, not a record concern: rule 2 means nothing it
  reads can reach the record except through `edit_file`, which is recorded.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Optional

from looplab.runtime import read_fence
from looplab.tools._base import RESULT_CAP, clip, fn_spec, stream_tails

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
_MUTATE = frozenset((
    "os.mkdir", "os.rmdir", "os.remove", "os.rename", "os.link", "os.symlink", "os.truncate",
    "os.chmod", "os.chown", "os.utime", "os.setxattr", "os.removexattr", "os.startfile",
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
# The probe's cwd is its disposable workspace replica, so a staged module must be importable by the
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


def render_launcher(program_path: str) -> str:
    """The generated launcher source for one probe. Split out so the boundary can be read, diffed
    and driven directly by `tests/test_dev_probe.py` rather than only through a subprocess."""
    return _LAUNCHER % {"refusal": PROBE_REFUSAL, "program": str(program_path)}


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

    def execute(self, name: str, args: dict) -> str:
        args = args or {}
        if name != "run_probe":
            return f"(unknown tool: {name})"
        try:
            return self._probe(str(args.get("code") or ""), args.get("timeout"))
        except Exception as e:  # noqa: BLE001 — a provider returns an error string, never raises
            return f"(probe error: {type(e).__name__}: {e})"

    # ------------------------------------------------------------------------------------ the run
    def _probe(self, code: str, timeout) -> str:
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
            if self._install_fence(root):
                # `run_argv` consumes this marker and prepends the directory to the child's
                # PYTHONPATH, which is what makes CPython import our `sitecustomize` at startup.
                # Set only when there is a fence to carry, so a non-repo task's probe env stays
                # byte-identical to what it would be without this module.
                env[read_fence.FENCE_DIR_ENV] = str(root)
            # `sys.executable`, deliberately: `env_inspect` answers by IMPORTING in the engine's own
            # interpreter, so a probe on any other one could contradict `pkg_info` about the same
            # package and the Developer would have no way to tell which answer was about its eval.
            rc, out, err, timed_out = run_argv(
                [sys.executable, str(launcher)], str(work), to, env=env,
                max_output_bytes=_MAX_OUTPUT)
        finally:
            shutil.rmtree(root, ignore_errors=True)
        return self._project(rc, out, err, timed_out, to, replica_note)

    def _install_fence(self, root: Path) -> bool:
        """Render THIS run's source-tree read fence into the probe's own directory.

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
        roots, allow, _dropped = read_fence.fence_inputs(self.repo_spec, allow=())
        if not roots:
            return False
        (root / "sitecustomize.py").write_text(
            read_fence.render(roots, allow, policy="deny", log="", run="developer-probe"),
            encoding="utf-8")
        return True

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

    def _project(self, rc, out, err, timed_out, to: float, note: str) -> str:
        """The bounded output projection: exit status, then each stream as a TAIL.

        Tails, not heads, for the reason `clip(keep="tail")` exists — the end of a command's output
        is where the exception and the final printed line are. The per-stream split is the SHARED
        `_base.stream_tails`, so this surface and the assistant's `run_command` cannot come to
        disagree about which half of a failure survives the agent loop's `RESULT_CAP`."""
        head = f"exit={rc}"
        if timed_out:
            head += f" (TIMEOUT after {int(to)}s — a probe is a question; a long job is an eval stage)"
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
        if not (out or "").strip() and not (err or "").strip():
            parts.append("(no output — a probe only returns what it PRINTS)")
        return clip("\n".join(parts), RESULT_CAP, keep="head", reserve=80,
                    note="\n(probe result clipped)")
