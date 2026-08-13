"""The source-tree READ FENCE: make it impossible for a node's process to read the operator's
editable SOURCE tree.

WHY THIS EXISTS (the defect it closes, measured on `runs/rubertlite-dr-unified-v6`)
-----------------------------------------------------------------------------------
`expect` checks what a stage WRITES and never what it READS. Node 4 of that run — a merge node —
trained for 76 minutes and produced a genuinely good model (`train.log`: `RECALL@100: 0.726350`).
Its `vectorsearch/configs/config.yaml:215` carried

    checkpoint_path: /home/jovyan/data/vectorizer-unified/vectorsearch/experiments/…/final

an ABSOLUTE path into the operator's editable SOURCE tree, where a HUMAN's `model.safetensors` from
2026-07-18 sits. The protected scorer loaded that foreign model and scored it:
`score.log`: `RECALL@100: 0.224975` — and THAT is the number the run recorded as the node's metric.
Every existing gate passed: the artifact contract PASSED (the node did write what it declared), no
`expect_failed`, no salvage, no violation, `feasible=True`. The SAME authoring error killed node 0
loudly (2.6 GPU-hours) only because its absolute path named a directory the human had never created.
Whether this costs an hour or corrupts a result is pure luck.

Every cheaper rung is already spent. `adapters/repo_task.py::_entrypoint_protect` states the residual
hole ("a scorer that reads its checkpoint path from an editable config can still be pointed somewhere
else") and names the stage `expect` contract + the Developer prompt as what closes it — neither does.
The advisory note shipped into `edit_file`'s result FIRES AND IS IGNORED: `spans.jsonl` for node 4's
build carries it verbatim ("NOTE: this content hard-codes an absolute path inside the editable repo's
SOURCE tree … such a path can NEVER name an artifact this node's pipeline produced") and the node
committed the path anyway and trained on it. Prompt-level and advisory-level interventions are
exhausted; this is the mechanism.

WHAT IT IS
----------
A generated, self-contained `sitecustomize.py` written once per run into `<run_dir>/.looplab-fence/`,
whose directory is prepended to the eval process's `PYTHONPATH` (`runtime/sandbox.py::run_argv`,
the universal launch choke point). CPython imports `sitecustomize` at interpreter startup, so the
module installs a `sys.addaudithook` that inspects the `open` audit event — raised by `builtins.open`,
`io.open` AND `os.open` — and refuses any path resolving under an editable source root. Because
`PYTHONPATH` is inherited, every python the eval spawns (dataloader workers, a torchrun rank, a
shell script's `python`) is fenced too, at no extra cost.

THE MESSAGE IS THE POINT. A refusal must be actionable by the repair loop, so it is a plain
exception carrying `REFUSAL_MESSAGE` — which names the fix — and it surfaces in the node's own
stderr, i.e. in `eval.log`, in the captured `RunResult.stderr`, and therefore in the repair feedback.

WHY THE EXCEPTION IS NOT AN `OSError`
-------------------------------------
`LoopLabSourceReadRefused` derives from `Exception`, deliberately NOT from `OSError`/`PermissionError`.
The single most common shape around a file read in real training code is
`try: open(p) except (OSError, IOError): <fall back>` — a `PermissionError` would be swallowed by
exactly that pattern and the fence would become the silent skip the hard requirements forbid. A
broad `except Exception` can still swallow it; nothing can prevent that, which is why the refusal is
ALSO appended to the fence's own diagnostic log beside the run.

WHAT IT DOES NOT FENCE (by construction, and each is deliberate)
----------------------------------------------------------------
* the node's own workdir, the run directory, `/tmp`, site-packages, the HF/model cache — none of
  them are under an editable source root, so the prefix test never matches;
* `data:` / `references:` mount SOURCES — allow-listed explicitly, because a data source is legally
  allowed to live INSIDE the editable tree and mounts are exactly the sanctioned read channel;
* the engine's own machinery — seeding (`engine/workspace.py`), the git plumbing, the fault
  localizer, the agent's repo tools all run in the ENGINE process, which never carries the marker;
* the Docker tiers — the source tree is not bind-mounted into the container at all, so a container
  is fenced by construction (`engine/eval_dispatch.py::_data_binds` mounts only the workdir and the
  declared data/reference sources). `run_argv` therefore skips the `PYTHONPATH` prepend for a
  `docker run` argv rather than pointing the container at a host path that does not exist there;
* a non-Python process (a C binary, a `curl`) — the fence is an interpreter-level hook. A shell
  script IS covered as soon as it invokes python, which is how every eval in practice reads a model.

Residual, stated rather than papered over: the check is a PATH fence, not an inode fence. A symlink
inside the workdir pointing into the source tree resolves past it, because closing that hole means
`os.path.realpath` on every open — measured at +9,866 ns/open (+88 %) versus +311 ns/open (+2.8 %)
for the prefix compare, i.e. unaffordable on a training process that reads thousands of shards. The
only symlinks the engine itself creates into a source are the allow-listed mounts.

MEASURED COST (2026-08-13, this box, 5 reps, best-of)
-----------------------------------------------------
Design candidates, measured before choosing (open+read 4 KiB in a loop):
    no hook                             11,131 ns/open
    + an audit hook that does nothing   11,242 ns/open   (+1.0 %)   <- the floor for ANY hook
    + a prefix check on resolved roots  11,420 ns/open   (+2.8 %,  +311 ns/open)
    + realpath() per open               21,116 ns/open   (+88 %,  +9,866 ns/open)   <- rejected
The SHIPPED fence, same workload, launched through `run_argv` with and without the marker:
    11,739 -> 12,059 ns/open (+319, +2.7 %) and 11,800 -> 12,077 (+278, +2.4 %) on two runs.
Worst case for the RATIO — bare `os.open`/`close` on /dev/shm, no read at all:
    3,297 -> 3,635 ns/open (+337, +10.2 %).
Per-process startup (the `sitecustomize` import plus the chain probe): 17.9 -> 18.2 ms, +0.28 ms.
Import-heavy startup (json/logging/sqlite3/asyncio/…): 38 -> 38 ms, unmeasurable.
So the roots are resolved ONCE at generation time into a tuple of `str`s each ending in `os.sep`, and
the hot path is `event != "open"` (one interned-string compare) followed by `str.startswith(tuple)`.
No syscall and no `realpath` per open: a RELATIVE path with no `..` cannot leave the cwd's subtree,
so it bails before touching the filesystem. That bail is only sound while the CWD ITSELF is outside
every root, which `os.chdir` refusal alone does NOT establish — a launcher sets the cwd at process
creation (`subprocess.run(cwd=…)`, `bash -c 'cd <repo> && python …'`) and `fork_exec` does that
chdir in C, raising no audit event. So the cwd is resolved ONCE at interpreter startup into
`_CWD_REACHES_ROOT` (one `getcwd`, unmeasurable against the 17.9 ms process baseline) and
re-derived on each `os.chdir`; when it is true the relative branch pays an `abspath` instead of
bailing. Absolute paths additionally pay three C-level substring scans, because the prefix compare
is byte-exact and `'/src//repo/x'` / `'/src/./repo/x'` name the same file as `'/src/repo/x'`.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, Optional

from looplab.core.atomicio import atomic_write_text

# The env var the engine uses to hand a launch its fence, and the directory name under the run dir.
# `run_argv` consumes the var and prepends the directory to PYTHONPATH; it is deliberately an
# explicit marker rather than a filesystem search, because discovering the fence by walking up from
# the cwd would cost a `stat` of an ABSENT file per level per launch — 105-950 ms each on the
# geesefs/S3 mount a run root usually lives on (see the trace-fence measurements in CLAUDE.md).
FENCE_DIR_ENV = "LOOPLAB_READ_FENCE_DIR"
FENCE_DIRNAME = ".looplab-fence"
VIOLATION_LOG = "violations.log"

# The policy rungs, in increasing strictness. See `Settings.read_fence` for why `deny` is the
# default rather than `warn`.
POLICIES = ("off", "warn", "deny")

# The refusal text, verbatim in the node's own output. It names the FIX, because the reader is the
# repair loop (and after it, the Developer), not a human tailing a log.
REFUSAL_MESSAGE = (
    "refused: {path} is under the operator's SOURCE tree, not this node's workspace. "
    "This node runs in its own copy; the source tree cannot contain anything your pipeline "
    "produced. Use a workdir-relative path, or ask the operator for a `data:`/`references:` "
    "mount or `seed_mode: \"all\"`."
)


def settle_policy(policy) -> str:
    """Settle an arbitrary policy value onto one of `POLICIES`, FAIL-CLOSED.

    `Settings.read_fence` validates the enum, but `EngineOptions.read_fence` (a bare `Engine(...)`,
    a hand-edited snapshot, a Strategist value) does not — and the generated hook tests
    `_POLICY == "deny"` exactly, so an unrecognised spelling used to fall through to the WARN
    branch: every source-tree read allowed, one stderr line, and the operator's config still saying
    `deny`. Every sibling knob in the tree (`metric_salvage.settle_mode`, `widths.settle_width`)
    settles an unknown value to the CONSERVATIVE rung; this one settled to the permissive one, which
    is the whole failure class the fence exists to close. Unknown -> `deny`."""
    text = str(policy or "").strip().lower()
    return text if text in POLICIES else "deny"


def _norm_root(path) -> Optional[str]:
    """Resolve one prefix into the exact form the hot path compares against: absolute, symlinks
    resolved, with a trailing separator so `/srcfoo` never matches the root `/src`."""
    try:
        p = os.path.realpath(str(Path(path).expanduser()))
    except OSError:
        return None
    if not p:
        return None
    return p if p.endswith(os.sep) else p + os.sep


def _too_broad(root: str) -> bool:
    """A root so wide that fencing it would fence the interpreter itself.

    `/`, `/home`, `/home/<user>`, `/usr` — an editable path like `$HOME` is pathological, but the
    failure mode of accepting it is that every python on the box refuses to start, which is a worse
    outcome than an unfenced run. Dropped roots are REPORTED (see `fence_inputs`) rather than
    silently ignored, so an operator whose whole fence evaporated can see why."""
    parts = [p for p in root.strip(os.sep).split(os.sep) if p]
    if len(parts) < 2:
        return True
    home = os.path.realpath(os.path.expanduser("~"))
    return os.path.realpath(root.rstrip(os.sep)) in (home, os.path.realpath(os.sep))


def fence_inputs(repo_spec: Optional[dict], *, allow: Iterable = ()) -> tuple:
    """`(roots, allow, dropped, swallowed)` for a repo spec — the whole policy input, in one place.

    `roots` are the EDITABLE source trees: the thing a node has its own copy of, and therefore the
    one place on the filesystem that provably cannot hold an artifact this node produced.

    `allow` wins over `roots` and carries the two categories that legitimately live inside one:
    the caller's own paths (the run directory — a run may be `--out`-ed inside the repo it edits)
    and every `data:` / `references:` mount SOURCE. A mount is the sanctioned read channel; the
    engine materializes it as a read-only symlink whose target may well be under the editable tree,
    and reading THROUGH that symlink must stay legal. Data mounts are not the editable source root.

    An allow entry that is an ANCESTOR of (or equal to) a root is REFUSED, not kept: `_fenced` tests
    the allow tuple after the root tuple, so such an entry disables the whole fence. `swallowed` is
    the fourth element for exactly that reason — a root dropped by `_too_broad` is warned about, and
    a root neutralized through the allow list has to be equally loud rather than leaving an operator
    who set `read_fence="deny"` with an unfenced run and no diagnostic."""
    spec = repo_spec or {}
    roots: list[str] = []
    dropped: list[str] = []
    for ed in spec.get("editables", []):
        r = _norm_root(ed.get("path"))
        if r is None:
            continue
        if _too_broad(r):
            dropped.append(r)
            continue
        if r not in roots:
            roots.append(r)
    allowed: list[str] = []
    for extra in allow:
        a = _norm_root(extra)
        if a and a not in allowed:
            allowed.append(a)
    for name, ds in (spec.get("data", {}) or {}).items():
        src = ds.get("path") if isinstance(ds, dict) else ds
        a = _norm_root(src)
        if a and a not in allowed:
            allowed.append(a)
    for ref in (spec.get("references", []) or []):
        a = _norm_root(ref.get("path"))
        if a and a not in allowed:
            allowed.append(a)
    # An allow prefix that is not under any root is dead weight on the hot path — drop it so the
    # `startswith` tuple stays as short as the policy actually needs.
    #
    # An allow prefix that CONTAINS a root is not dead weight, it is a disabled fence: `_fenced`
    # consults the allow tuple after the root tuple, so `/src/` allow-listed against the root
    # `/src/repo/` returns None for every path under the source tree. This is reachable by ordinary
    # operator spelling — `looplab run --out .` beside an editable `./repo` makes `resources.py`
    # pass the repo's own PARENT as `allow=[run_dir]`. Refuse those and hand them back so the caller
    # can say so; only a strict DESCENDANT of a root is a real carve-out.
    swallowed = [a for a in allowed if any(r.startswith(a) for r in roots)]
    allowed = [a for a in allowed
               if a not in swallowed and any(a.startswith(r) for r in roots)]
    return roots, allowed, dropped, swallowed


# The generated fence. Kept as ONE template rather than a shipped file plus a config sidecar so the
# PYTHONPATH entry contains exactly one importable name (`sitecustomize`) — every additional module
# in that directory would shadow a real one for every process in the run.
#
# `__name__` gates installation: exec'ing this source under `_PROBE_NAME` yields the pure predicate
# `_fenced()` WITHOUT installing an irreversible audit hook, which is what lets the truth table be
# tested in-process (an audit hook can never be removed once added).
_PROBE_NAME = "__looplab_fence_probe__"

_TEMPLATE = '''\
"""LoopLab source-tree READ FENCE — GENERATED, do not edit.

Regenerated per run by `looplab/runtime/read_fence.py`; see that module for why this exists.
This file is imported by CPython at interpreter startup because its directory is first on
PYTHONPATH. It refuses reads of the operator's editable SOURCE tree from inside a node's process:
the node runs in its own copy, so the source tree provably cannot hold anything the node produced.
"""
import os
import sys
import threading

_ROOTS = %(roots)r
_ALLOW = %(allow)r
_POLICY = %(policy)r
_LOG = %(log)r
_MESSAGE = %(message)r
_RUN = %(run)r          # provenance: the run this fence was generated for

_SEP = os.sep
_DOTDOT = ".."
_DUP_SEP = _SEP + _SEP          # '/src//repo/x' — what f"{root}/{sub}" spells when root ends in '/'
_DOT_SEG = _SEP + "." + _SEP    # '/src/./repo/x'
_DOT_END = _SEP + "."           # '/src/repo/.'  (a trailing no-op segment)
_NT = %(nt)r
_abspath = os.path.abspath
_normpath = os.path.normpath
_seen = set()
_busy = threading.local()

# Is a RELATIVE open able to reach a fenced root from where this process stands? Normally no, and
# that is what buys the syscall-free fast bail for the branch nearly every read takes. But a
# process's cwd is far more often set AT CREATION than by `os.chdir`: `subprocess.run(cwd=...)`,
# `bash -c 'cd <repo> && python score.py'`, a `WorkingDirectory=` wrapper. `fork_exec` performs
# that chdir in C in the child and raises NO `os.chdir` audit event, so the "the cwd cannot be
# inside a root" premise the bail rested on was simply false for every launcher-style command —
# measured: with cwd inside the root, `open('model.safetensors')` returned the human's checkpoint
# under policy "deny" with nothing logged. Resolved ONCE at interpreter startup (one `getcwd`,
# unmeasurable against the 17.9 ms baseline) and re-derived on each `os.chdir`.
_CWD_REACHES_ROOT = False


class LoopLabSourceReadRefused(Exception):
    """A node process tried to read the operator's editable SOURCE tree.

    NOT an OSError subclass on purpose: `except (OSError, IOError): <fall back>` is the standard
    shape around a file read, and being caught by it would turn this refusal into a silent skip."""


def _resolve(p):
    """Normalize an `open` argument to an absolute path string, or None if it cannot name a file
    under a fenced root. Deliberately syscall-free on the hot path.

    The three normalizations below all exist because the check downstream is a PREFIX COMPARE
    against roots that were `realpath`-ed at generation time. Any spelling of the same file that
    does not share those bytes is a bypass, and each of these was reproduced:
      * duplicate or no-op separators — '/src//repo/x' and '/src/./repo/x' were ALLOWED while
        '/src/repo/x' refused, and `f"{root}/{sub}"` produces exactly that whenever root ends in a
        slash. Three C-level substring scans, paid only on the absolute branch;
      * Windows drive-letter and UNC paths never start with `os.sep`, so they took the relative
        fast bail and were never compared against a root at all — the fence was a complete no-op on
        that platform while every surface reported the run as fenced. `_NT` is baked at generation
        time, so POSIX pays one already-false boolean for this and nothing else;
      * a cwd inside a fenced root (see `_CWD_REACHES_ROOT`), which makes a bare relative name reach
        the source tree without ever containing `..`.
    """
    cls = p.__class__
    if cls is not str:
        if cls is int:
            return None            # an already-open fd (os.fdopen): the path was checked at open
        try:
            p = os.fspath(p)
        except TypeError:
            return None
        if p.__class__ is not str:
            try:
                p = os.fsdecode(p)
            except Exception:
                return None
    if _NT:
        # Windows: either slash separates, absolute spellings are drive-letter/UNC rather than
        # sep-leading, and `_ROOTS` are backslash-normalized `realpath` output. There is no correct
        # syscall-free bail here, so normalize unconditionally. Windows is not a perf target for
        # this fence; being INERT there was the defect.
        return _normpath(_abspath(p))
    if p[:1] != _SEP:
        # The branch nearly every open in a training process takes. A relative path with no `..`
        # can only reach the cwd's subtree, so it needs no filesystem work UNLESS the cwd itself
        # stands inside a fenced root.
        if not _CWD_REACHES_ROOT and _DOTDOT not in p:
            return None
        p = _abspath(p)
    elif (_DOTDOT in p or _DUP_SEP in p or _DOT_SEG in p
            or p.endswith(_DOT_END)):
        p = _normpath(p)
    return p


def _cwd_reaches_root():
    """Whether a bare relative open from the CURRENT directory could land under a fenced root.

    False for the overwhelmingly common case (the cwd is the node's own workdir), which is what
    keeps `_resolve`'s relative branch syscall-free. An allow-listed cwd answers False too: reading
    through a sanctioned mount is legal, and a `..` escape out of it is still normalized above."""
    try:
        d = os.getcwd()
    except OSError:
        return True                # cannot prove it is safe -> resolve, and let `_fenced` decide
    d = d if d.endswith(_SEP) else d + _SEP
    if not d.startswith(_ROOTS):
        return False
    return not (_ALLOW and d.startswith(_ALLOW))


def _fenced(p):
    """The path this fence refuses, or None. The whole policy, in three string operations."""
    p = _resolve(p)
    if p is None or not p.startswith(_ROOTS):
        return None
    if _ALLOW and p.startswith(_ALLOW):
        return None
    return p


def _fenced_dir(p):
    """`_fenced` for a path naming a DIRECTORY. A directory's own name carries no trailing
    separator, so `os.chdir('/src/repo')` misses the prefix test `open('/src/repo/x')` hits — and
    symmetrically `os.chdir` into an allow-listed mount must still be permitted. Both are fixed by
    comparing the trailing-separator form; the `open` path deliberately does NOT pay for this,
    because opening a directory raises IsADirectoryError before it can read anything."""
    r = _resolve(p)
    if r is None:
        return None
    d = r if r.endswith(_SEP) else r + _SEP
    if not d.startswith(_ROOTS):
        return None
    if _ALLOW and d.startswith(_ALLOW):
        return None
    return r


def _record(path, rung):
    """Append one line to the run's fence diagnostic. Re-entrancy-guarded: this opens a file, which
    raises `open` again — the guard makes that provably terminate rather than relying on the fence
    log being outside every root.

    The guard is THREAD-LOCAL, not a module-level list. A process-global one made a concurrent
    thread's violation vanish: `_report` marks the path seen BEFORE calling here, so a thread that
    found the flag set while another thread was blocked in this `open` (105-950 ms on the
    geesefs/S3 mount a run root usually lives on) lost its line permanently — leaving `warn`, whose
    entire purpose is to produce this audit file, silently under-reporting what the eval read."""
    if not _LOG or getattr(_busy, "on", False):
        return
    _busy.on = True
    try:
        with open(_LOG, "a", encoding="utf-8") as fh:
            fh.write("%%s\\t%%s\\t%%s\\t%%s\\n" %% (rung, os.getpid(), sys.argv[0], path))
    except Exception:
        pass
    finally:
        _busy.on = False


def _report(path):
    # Bounded: a retry loop must not write a gigabyte. The bound covers BOTH sinks — gating only
    # `_record` on it INVERTED the guard, because a path never added to a saturated `_seen` is
    # "first" on every subsequent read of it, so the stderr write below fired per `open()` rather
    # than per distinct path (measured: 256 distinct paths then 1000 reads of one more produced
    # 1256 lines). That is a write+flush syscall pair on the hot read path, and it floods the
    # captured stderr that `eval.log` and the repair feedback are built from.
    first = path not in _seen and len(_seen) < 256
    if first:
        _seen.add(path)
        _record(path, _POLICY)
    if _POLICY == "deny":
        raise LoopLabSourceReadRefused(_MESSAGE.replace("{path}", path))
    if first:
        try:
            sys.stderr.write(
                "LOOPLAB READ FENCE (warn): " + _MESSAGE.replace("{path}", path) + "\\n")
            sys.stderr.flush()
        except Exception:
            pass


def _hook(event, args):
    # Hot path: one interned-string compare for every audited event that is not an open.
    if event == "open":
        try:
            bad = _fenced(args[0])
        except Exception:
            return                   # a bug in the fence must never break an unrelated open
        if bad is not None:
            _report(bad)
        return
    if event != "os.chdir":
        return
    # Refused so a process cannot walk into the source tree and read it with bare relative names.
    # The audit event fires BEFORE the chdir, so this also re-derives `_CWD_REACHES_ROOT` from the
    # target — the flag that keeps `_resolve`'s relative fast bail correct rather than merely
    # asserted. Under `warn` the chdir proceeds, so the flag must be set even though we do not stop
    # it; under `deny` `_report` raises, the chdir never happens, and the assignment is skipped.
    global _CWD_REACHES_ROOT
    try:
        bad = _fenced_dir(args[0])
    except Exception:
        return
    if bad is not None:
        _report(bad)
        _CWD_REACHES_ROOT = True
    else:
        # `os.fchdir` hands an already-open fd, whose target this hook cannot resolve. Nothing
        # proves it is outside a root, so pay the `abspath` on relative opens rather than guess.
        _CWD_REACHES_ROOT = args[0].__class__ is int


def _chain():
    """Run whatever `sitecustomize` the environment already had.

    This directory is PREPENDED to PYTHONPATH, so it shadows any other `sitecustomize` on the path
    (coverage's subprocess support, a distro's, a conda env's). Shadowing one silently would be a
    real regression in someone else's tooling, so hand off explicitly.

    The handoff loads the other module by SPEC, under a different name, and never touches
    `sys.modules['sitecustomize']`. The obvious spelling — pop the name and re-`import` — leaves the
    entry missing when there is no other sitecustomize (the common case), and `importlib._bootstrap`
    ends its own load with a `sys.modules.pop(name)` that then raises `KeyError`. `site` catches it
    and prints `Error in sitecustomize; set PYTHONVERBOSE for traceback: KeyError: 'sitecustomize'`
    on the stderr of EVERY fenced process — measured, and diagnosed only because the fence's own
    benchmark reported the module as not loaded while the hook was demonstrably installed."""
    try:
        from importlib.machinery import PathFinder
        from importlib.util import module_from_spec

        me = os.path.abspath(__file__)
        here = os.path.dirname(me)
        # `PathFinder.find_spec`, not `importlib.util.find_spec`: the latter consults `sys.modules`
        # FIRST, where `sitecustomize` is bound to THIS module mid-import — so it hands back our own
        # spec and the chain re-executes the fence against itself. Measured: one refused read became
        # 247 refusals and the fence never reached the real sitecustomize at all.
        spec = PathFinder.find_spec(
            "sitecustomize", [p for p in sys.path if os.path.abspath(p or os.curdir) != here])
        if spec is None or spec.loader is None or os.path.abspath(spec.origin or "") == me:
            return
        other = module_from_spec(spec)
        sys.modules.setdefault("_looplab_chained_sitecustomize", other)
        spec.loader.exec_module(other)
    except Exception:
        pass


if __name__ != "%(probe)s":
    if _POLICY != "off" and _ROOTS:
        # Resolve the launcher-set cwd ONCE, before the hook is armed, so the very first relative
        # open is already judged correctly (and so this `getcwd` is not itself audited).
        _CWD_REACHES_ROOT = _cwd_reaches_root()
        sys.addaudithook(_hook)
    _chain()
'''


def render(roots, allow, *, policy: str, log: str = "", run: str = "") -> str:
    """The generated `sitecustomize.py` source for one run's fence."""
    return _TEMPLATE % {
        "roots": tuple(roots), "allow": tuple(allow), "policy": settle_policy(policy),
        "log": str(log), "message": REFUSAL_MESSAGE, "run": str(run), "probe": _PROBE_NAME,
        # Baked rather than probed in the child: the fence runs on the same box that generated it
        # (the Docker tiers are fenced by construction and skip the marker entirely), so this keeps
        # the POSIX hot path at one already-false boolean instead of an `os.path.isabs` call.
        "nt": os.name == "nt",
    }


def install(run_dir, *, roots, allow, policy: str) -> Optional[str]:
    """Materialize the fence beside a run and return the directory to prepend to `PYTHONPATH`.

    `None` when the fence would be a no-op (policy `off`, or no editable root survived
    `_too_broad`) — the caller must then not set the marker at all, so a non-repo run's child env is
    byte-identical to what it was before this module existed."""
    if settle_policy(policy) == "off" or not roots:
        return None
    d = Path(run_dir) / FENCE_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    src = render(roots, allow, policy=policy, log=str(d / VIOLATION_LOG), run=str(run_dir))
    target = d / "sitecustomize.py"
    # Rewrite only on change: the eval workers call this concurrently, and an unconditional
    # write would let one worker truncate the file another interpreter is mid-import of.
    try:
        if target.read_text(encoding="utf-8") == src:
            return str(d)
    except OSError:
        pass
    # `atomic_write_text`, not a hand-rolled temp+replace: it already owns the per-CALL unique
    # scratch name this needs (the eval workers are THREADS, so a shared `<name>.tmp` would let two
    # of them interleave into one file and `os.replace` the interleaving into place) AND the two
    # things the private copy was missing — an fsync before the replace, so a crashy FUSE/S3 run
    # root cannot publish a truncated `sitecustomize.py` that every python of the run then fails to
    # import, and an `except BaseException: unlink(tmp)` so a failed or cancelled write does not
    # leave a permanent multi-KB `.tmp` in the run dir with nothing to reclaim it.
    atomic_write_text(target, src)
    return str(d)


def prepend_pythonpath(env: dict, fence_dir: str) -> None:
    """Put `fence_dir` first on the child's `PYTHONPATH`, in place.

    PREPEND rather than replace: a task whose eval legitimately sets PYTHONPATH keeps it, and
    prepending is also what makes our `sitecustomize` win the import (it then chains to theirs)."""
    if not fence_dir:
        return
    existing = env.get("PYTHONPATH") or ""
    if existing:
        parts = [p for p in str(existing).split(os.pathsep) if p]
        if parts and parts[0] == fence_dir:
            return
        parts = [p for p in parts if p != fence_dir]
        env["PYTHONPATH"] = os.pathsep.join([fence_dir, *parts])
    else:
        env["PYTHONPATH"] = fence_dir


def violations(run_dir) -> list[str]:
    """The fence's diagnostic lines for a run (`[]` when it never fired). Read by an operator and by
    the tests; the engine does not need it, because a `deny` refusal is already in the node's own
    stderr and a `warn` one is on its stderr too."""
    p = Path(run_dir) / FENCE_DIRNAME / VIOLATION_LOG
    try:
        return [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except OSError:
        return []
