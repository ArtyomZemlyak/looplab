"""LANDLOCK — a kernel-enforced READ ALLOW-LIST for one eval process and everything it spawns.

WHY THIS EXISTS, AND WHY IT IS NOT `read_fence.py`
--------------------------------------------------
`runtime/read_fence.py` is the shipped source-tree fence: a generated `sitecustomize.py` whose
`sys.addaudithook` refuses a Python `open` under an editable source root. It is the MESSAGE rung and
it stays — its refusal is a plain non-`OSError` carrying an actionable `REFUSAL_MESSAGE`, which is
exactly what the repair loop needs and exactly what a kernel `EACCES` is not.

What it cannot do is see a read that never reaches Python. Measured 2026-08-13 on this box, against
the 92 MB foreign checkpoint from the `rubertlite-dr-unified-v6` node 4 incident:

    safetensors.torch.load_file("<the foreign model.safetensors>")
      -> 55 tensors loaded
      -> audit 'open' events naming the file: NONE
      -> total audit 'open' events during the load: 0

`safetensors` is Rust; the weights read raises zero audit events. Doc 31 reports that its
reproduction of the real `SentenceTransformer` load WAS refused, which is consistent — `transformers`
reads `config.json` through Python `open` before the Rust loader touches the weights. Both are true,
and together they say the fence's coverage of this incident is a property of a third-party library's
file ORDERING, not a property of the fence. A loader that reads one self-describing artifact natively
walks straight through.

Landlock closes that: the kernel checks the path walk, so `libc.fopen`, a Rust `File::open`, a child
`cat` and a `torchrun` rank are all covered, inherited across `fork`/`exec` with no `PYTHONPATH`
trick and no `LD_PRELOAD` packaging.

MEASURED ON THIS BOX (2026-08-13, kernel 6.1.0-22)
--------------------------------------------------
    landlock_create_ruleset(NULL, 0, LANDLOCK_CREATE_RULESET_VERSION) -> 2   (ABI 2)
    NoNewPrivs already 1

Enforcement, one process, ruleset allowing `$WD` and not `$SRC`:

    | reader                                   | baseline | under Landlock                |
    | Python `open()`                          | OK       | PermissionError [Errno 13]    |
    | subprocess.run(["cat", ...]) — a CHILD   | rc=0     | rc=1, "Permission denied"     |
    | ctypes libc `fopen()` — pure native      | OK       | refused, errno 13             |

Cost (open + read 4 KiB, best-of-5 over 20,000 iterations, two independent runs):

    | ruleset                    | 4-component path      | 10-component path | setup     |
    | none                       | 4,784 / 4,749 ns      | 5,054 ns          | -         |
    | allow-list, 12 rules       | 4,886 / 4,844 (+2.1%) | 5,302 (+4.9%)     | 0.12-0.15 ms |
    | deny-by-complement, 55     | 4,874 / 4,843 (+1.9%) | 5,406 (+7.0%)     | 10-24 ms  |

That +2.1 % is the number that supersedes the CONCLUSION of `read_fence.py`'s +88 % `realpath`
measurement. The +88 % is real and stands — it is the cost of inode-grade resolution in PYTHON, per
open, in an audit hook. The kernel does the equivalent discrimination inside its own path walk for
+2.1 %, i.e. cheaper than the +2.8 % prefix compare the +88 % figure was rejected in favour of. The
measurement is not overturned; the inference "therefore we cannot have an inode fence" is.

Mount namespaces were the other candidate and are DEAD on this host, for a reason no engineering
fixes: `cat /proc/self/attr/current` -> `cri-containerd.apparmor.d (enforce)`, and that profile
denies `mount(2)` unconditionally, so a namespace can be created and is then useless (no bind, no
remount-ro, no overlay upper, no tmpfs). There is no `docker`, no `bwrap`, no `fuse-overlayfs`, no
`proot` either.

WHY THIS SHIPS OFF BY DEFAULT
------------------------------
The single largest unknown in the whole design is unretired: **nobody has run a Landlock allow-list
through a real GPU eval.** The numbers above are `open`/`read` microbenchmarks. torch, CUDA, NCCL,
`/dev/nvidia*`, `/dev/shm`, `/sys/class` and the geesefs read surfaces were never exercised. A
ruleset that is missing one of those does not degrade — it refuses, mid-training, as an `EACCES`
that a native library may well swallow. `Settings.landlock` therefore defaults to `"off"`, and
`looplab landlock-check` (cli/inspect_cmds) is the way to validate a candidate ruleset on one real
eval before anyone flips it. What would justify the flip is written down in that setting's own
comment and in `docs/guide/configuration.md`.

TWO RESIDUALS, STATED
---------------------
1. **Landlock refuses with `EACCES` -> `PermissionError` -> an `OSError`.** That is precisely the
   shape `read_fence.py` deliberately refuses to be, because `except OSError: <fall back>` is THE
   idiom around a file read and would turn a refusal into a silent skip. This is why the audit hook
   is NOT deleted: it stays the first rung and fires before the syscall for Python opens, with the
   actionable message. Landlock catches what the hook cannot see — native readers — where no Python
   `except OSError` is in play. A native library may still swallow its own EACCES; unmeasured.
2. **An allow-list built by ENUMERATION fails closed in an unpredictable place.** Measured: of 211
   candidate top-level rules for a deny-by-complement construction only 55 were accepted, because a
   path that fails to `open(O_PATH)` is silently OMITTED from the ruleset — i.e. silently DENIED. So
   `add_rules` REPORTS every rule it could not add rather than dropping it, and the allow-list is
   derived from the operator's declared mounts (`runtime/read_allowlist.py`), never enumerated.

Layering: `runtime` imports nothing above `core`; this module imports only stdlib + ctypes.
"""
from __future__ import annotations

import ctypes
import errno
import os
from typing import Iterable, Optional

# syscall numbers, x86_64/aarch64 (the only architectures this engine runs on). Landlock's three
# syscalls were added in 5.13 and have the same numbers on both.
_SYS_LANDLOCK_CREATE_RULESET = 444
_SYS_LANDLOCK_ADD_RULE = 445
_SYS_LANDLOCK_RESTRICT_SELF = 446

# `landlock_create_ruleset(NULL, 0, LANDLOCK_CREATE_RULESET_VERSION)` returns the ABI version.
_LANDLOCK_CREATE_RULESET_VERSION = 1 << 0
_LANDLOCK_RULE_PATH_BENEATH = 1

# The filesystem access bits, ABI 1 unless noted. We only ever GRANT; anything not granted on a
# handled bit is denied for the whole process tree.
FS_EXECUTE = 1 << 0
FS_WRITE_FILE = 1 << 1
FS_READ_FILE = 1 << 2
FS_READ_DIR = 1 << 3
FS_REMOVE_DIR = 1 << 4
FS_REMOVE_FILE = 1 << 5
FS_MAKE_CHAR = 1 << 6
FS_MAKE_DIR = 1 << 7
FS_MAKE_REG = 1 << 8
FS_MAKE_SOCK = 1 << 9
FS_MAKE_FIFO = 1 << 10
FS_MAKE_BLOCK = 1 << 11
FS_MAKE_SYM = 1 << 12
FS_REFER = 1 << 13          # ABI 2 — and it is ONLY grantable on a ruleset that handles it

# What each declared mode grants. A READ mount gets read+exec (an allow-listed site-packages or a
# base-model cache must stay importable and, for a venv's `bin/`, executable); a READWRITE mount gets
# the whole ABI-1 set. `FS_REFER` is deliberately NOT handled: it is the ABI-2 rename/link-across-
# directories right, and a ruleset that HANDLES it forbids every cross-directory rename that is not
# explicitly re-granted — which would break `os.replace` in a checkpoint writer for no security we
# are asking for here. Not handling a bit means the kernel does not police it at all.
_READ = FS_READ_FILE | FS_READ_DIR | FS_EXECUTE
_WRITE = (_READ | FS_WRITE_FILE | FS_REMOVE_DIR | FS_REMOVE_FILE | FS_MAKE_CHAR | FS_MAKE_DIR
          | FS_MAKE_REG | FS_MAKE_SOCK | FS_MAKE_FIFO | FS_MAKE_BLOCK | FS_MAKE_SYM)
# The set the ruleset HANDLES — i.e. the set that is denied where it is not granted. Exactly the
# union above, so `handled \ granted` is what a rule refuses and nothing else is policed.
_HANDLED = _WRITE

# THE SECOND RULESET SHAPE, and it is the INVERSE of the allow-list above: handle only the bits that
# CHANGE the filesystem, and grant NOTHING. `handled \ granted` is then the whole mutation set over
# every path on the box, and the read bits are not in `handled` at all — so the kernel does not
# police reads, and this ruleset cannot refuse one.
#
# That inversion is what makes an EMPTY ruleset correct here and catastrophic there. `_LAUNCHER`
# `_die`s on an empty allow-list because an allow-list with no rules denies reads too, i.e. denies
# the eval's own interpreter; a ruleset with no rules and no read bit handled denies exactly
# "create/remove/write anything, anywhere" and nothing else. There is no list to enumerate, nothing
# to keep in sync with a mount table, and therefore none of `build_ruleset`'s "a path that will not
# `open(O_PATH)` is a silent denial" hazard — the count of rules is zero by construction.
#
# Its ONE consumer is `tools/dev_probe.py`, whose rule 2 is "no write ANYWHERE" — the same sentence
# this ruleset is. It is deliberately not reachable from `Settings.landlock`: that knob chooses a
# read allow-list for an EVAL, whose unretired unknown is whether a ruleset survives a real GPU
# training run, and this shape has no such unknown because it grants nothing and forbids nothing a
# probe is allowed to do.
#
# `FS_TRUNCATE` (ABI 3) is absent because this box is ABI 2 and an unsupported bit in
# `handled_access_fs` is EINVAL for the whole ruleset. It costs nothing here: truncating a file
# through a path needs `FS_WRITE_FILE`, which IS handled, and `os.truncate`/`os.ftruncate` raise
# their own audit event, which `dev_probe`'s hook refuses with the actionable message. `FS_REFER`
# is absent for the same reason it is absent above, and needs no second argument here: a rename
# needs REMOVE on the source directory and MAKE on the destination, and neither is granted.
NO_MUTATION_HANDLED = (FS_WRITE_FILE | FS_REMOVE_DIR | FS_REMOVE_FILE | FS_MAKE_CHAR | FS_MAKE_DIR
                       | FS_MAKE_REG | FS_MAKE_SOCK | FS_MAKE_FIFO | FS_MAKE_BLOCK | FS_MAKE_SYM)

MODES = ("read", "readwrite")

# The policy rungs. `off` is the default (see the module docstring); `enforce` applies the ruleset in
# the child before `exec`. There is no `warn` rung and there cannot be one: Landlock has no audit
# mode in ABI 2 — the kernel either refuses or it does not. The warn-shaped rung is the audit hook in
# `read_fence.py`, which is a different mechanism and stays.
POLICIES = ("off", "enforce")

# The env var `engine/resources.py` stamps a launch's allow-list into and `runtime/sandbox.py`
# consumes at the Popen choke point.
#
# IT MUST NOT BE `LOOPLAB_LANDLOCK`, and that is the whole reason for the suffix. `Settings` is flat
# with `env_prefix="LOOPLAB_"` (CLAUDE.md: "`LOOPLAB_<FIELD>` env vars map 1:1"), so `LOOPLAB_LANDLOCK`
# is ALREADY taken — it is how an operator spells `Settings.landlock`, and it is the exact spelling
# this repo's own validation instruction hands them (`cli/inspect_cmds.py`'s "run ONE real eval with
# `LOOPLAB_LANDLOCK=enforce`", and the `landlock` row in `docs/guide/configuration.md`). `run_argv`
# builds its child env from `os.environ` FIRST, so that policy word arrived here as an ALLOW-LIST and
# `parse_env` refused it: `LandlockUnavailable: malformed landlock allow-list record: 'enforce'`,
# raised out of the universal launch choke point — no return code, no stderr, no node terminal, and
# it fires on `run_setup` before a single eval starts. Every documented attempt to turn this rung on
# killed the run instead. The two names answer different questions (a POLICY the operator chooses vs
# a DERIVED wire allow-list the engine computes), and only the derived one is free to move.
# `LOOPLAB_LANDLOCK_ALLOWLIST` maps to no `Settings` field and must not become one.
LANDLOCK_ENV = "LOOPLAB_LANDLOCK_ALLOWLIST"

# The allow-list's wire format: `<mode>\x1f<path>` records joined by `\x1e`.
#
# NOT `os.pathsep`-joined `mode:path`, which is what this shipped as for exactly one hour. On POSIX
# `os.pathsep` IS `":"`, so `format_env` produced `read:/usr:read:/lib` and every record then failed
# to partition — the parser dropped all of them, the ruleset came out EMPTY, and an empty allow-list
# denies everything: the launch died with `execvp -> EACCES` before running a single byte of the
# eval. That is the failure mode this whole module has to be careful about and it bit its own
# serializer first. ASCII 0x1E/0x1F are the record/unit separators, cannot appear in a POSIX path,
# and are legal in an environment value (only NUL is not).
_UNIT = "\x1f"
_RECORD = "\x1e"


class LandlockUnavailable(RuntimeError):
    """This kernel/process cannot apply a Landlock ruleset, with the reason as the message.

    NOT an `OperatorRefusal`: on the default `off` rung nobody ever sees it, and under `enforce` the
    caller decides whether an unavailable kernel is a refusal (the CLI check) or a logged degradation
    (the launch path). Deriving it from `RuntimeError` keeps it out of every `except OSError` around
    a file read — the same reasoning `read_fence.ReadFenceRefusal` records.
    """


class _PathBeneathAttr(ctypes.Structure):
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


class _RulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]


def _libc():
    return ctypes.CDLL(None, use_errno=True)


def abi_version() -> Optional[int]:
    """The kernel's Landlock ABI version, or None when Landlock is not available at all.

    `landlock_create_ruleset(NULL, 0, LANDLOCK_CREATE_RULESET_VERSION)` is the documented probe and
    it allocates nothing, so this is safe to call anywhere (the CLI check, a test skip guard).
    """
    try:
        lib = _libc()
        rc = lib.syscall(ctypes.c_long(_SYS_LANDLOCK_CREATE_RULESET), None,
                         ctypes.c_size_t(0), ctypes.c_uint32(_LANDLOCK_CREATE_RULESET_VERSION))
    except (OSError, AttributeError, ValueError):
        return None
    return int(rc) if rc > 0 else None


def unavailable_reason() -> Optional[str]:
    """Why a ruleset could not be applied here — or None when it could.

    Three separate facts, reported separately because the fixes differ: no Landlock in the kernel, an
    ABI too old for the bits we grant, and `no_new_privs` unset (which `restrict_self` requires and
    which we set ourselves in the child, so it is informational rather than fatal).
    """
    abi = abi_version()
    if abi is None:
        return ("this kernel has no Landlock support (landlock_create_ruleset returned no version); "
                "Landlock needs Linux 5.13+ with CONFIG_SECURITY_LANDLOCK=y")
    if abi < 1:
        return f"Landlock ABI {abi} is older than the ABI 1 this ruleset needs"
    return None


def _grant(mode: str) -> int:
    if mode not in MODES:
        raise ValueError(f"landlock mode {mode!r} is not one of {MODES!r}")
    return _READ if mode == "read" else _WRITE


def build_ruleset(allow) -> tuple:
    """`(ruleset_fd, added, skipped)` for `allow` = an iterable of `(path, mode)`.

    `skipped` is `[(path, reason)]` and is the half that must never be silent. A path that cannot be
    `open(O_PATH)`ed — because it does not exist on this box, or is a dangling symlink — simply
    contributes no rule, and under an ALLOW-list that means the kernel denies it. Measured: 211
    candidate rules produced 55 accepted rules that way, i.e. 156 silent denials. The caller decides
    what to do about a skip; this function only refuses to hide it.

    The caller owns the returned fd and must `os.close` it (`apply` does).
    """
    reason = unavailable_reason()
    if reason is not None:
        raise LandlockUnavailable(reason)
    lib = _libc()
    attr = _RulesetAttr(handled_access_fs=_HANDLED)
    ctypes.set_errno(0)
    fd = lib.syscall(ctypes.c_long(_SYS_LANDLOCK_CREATE_RULESET), ctypes.byref(attr),
                     ctypes.c_size_t(ctypes.sizeof(attr)), ctypes.c_uint32(0))
    if fd < 0:
        raise LandlockUnavailable(
            f"landlock_create_ruleset failed: {os.strerror(ctypes.get_errno())}")
    added, skipped = [], []
    try:
        for path, mode in allow:
            access = _grant(mode)
            try:
                pfd = os.open(str(path), os.O_PATH | os.O_CLOEXEC)   # type: ignore[attr-defined]
            except OSError as exc:
                skipped.append((str(path), exc.strerror or str(exc)))
                continue
            try:
                rule = _PathBeneathAttr(allowed_access=access, parent_fd=pfd)
                ctypes.set_errno(0)
                rc = lib.syscall(ctypes.c_long(_SYS_LANDLOCK_ADD_RULE), ctypes.c_int(fd),
                                 ctypes.c_uint32(_LANDLOCK_RULE_PATH_BENEATH),
                                 ctypes.byref(rule), ctypes.c_uint32(0))
                if rc != 0:
                    skipped.append((str(path), os.strerror(ctypes.get_errno())))
                else:
                    added.append((str(path), mode))
            finally:
                os.close(pfd)
    except BaseException:
        os.close(fd)
        raise
    return fd, added, skipped


def apply(allow, *, strict: bool = True) -> list:
    """Restrict THIS process (and everything it spawns) to `allow`. Returns the rules added.

    Irreversible by design — that is what makes it safe to call in a `preexec_fn` between `fork` and
    `exec`. `strict` refuses rather than enforcing a ruleset that lost rules: under an allow-list a
    lost rule is a DENIAL, and enforcing a partial allow-list is how legitimate work dies in a place
    nobody can predict (see `build_ruleset`).

    `PR_SET_NO_NEW_PRIVS` is set here rather than assumed: `landlock_restrict_self` requires it, and
    setting it is harmless for an eval (nothing in a candidate's pipeline legitimately gains
    privileges through setuid).
    """
    fd, added, skipped = build_ruleset(allow)
    try:
        if skipped and strict:
            raise LandlockUnavailable(
                "landlock allow-list is incomplete, refusing to enforce a ruleset that would deny "
                "paths it was asked to allow: "
                + "; ".join(f"{p} ({why})" for p, why in skipped[:8]))
        lib = _libc()
        ctypes.set_errno(0)
        if lib.prctl(ctypes.c_int(38), ctypes.c_ulong(1), ctypes.c_ulong(0),
                     ctypes.c_ulong(0), ctypes.c_ulong(0)) != 0:   # PR_SET_NO_NEW_PRIVS = 38
            raise LandlockUnavailable(
                f"prctl(PR_SET_NO_NEW_PRIVS) failed: {os.strerror(ctypes.get_errno())}")
        ctypes.set_errno(0)
        if lib.syscall(ctypes.c_long(_SYS_LANDLOCK_RESTRICT_SELF),
                       ctypes.c_int(fd), ctypes.c_uint32(0)) != 0:
            err = ctypes.get_errno()
            raise LandlockUnavailable(
                f"landlock_restrict_self failed: {os.strerror(err)}"
                + (" (E2BIG usually means too many stacked rulesets)" if err == errno.E2BIG else ""))
    finally:
        os.close(fd)
    return added


def parse_env(value: Optional[str]) -> list:
    """`[(path, mode)]` from the `\\x1e`-joined `<mode>\\x1f<path>` spelling `LANDLOCK_ENV` carries.

    The wire format is stated once above (`_UNIT`/`_RECORD`); this docstring named the `os.pathsep`
    `mode:path` spelling the body abandoned in its first hour, which is a comment contradicting the
    code it sits on in the one function whose separator bug the module docstring says bit it first.

    RAISES on a malformed record rather than dropping it. Dropping was the original behaviour and it
    is what turned the separator collision above into a total, silent denial: every record was junk,
    every record was dropped, and the caller got a well-formed empty allow-list. Under an allow-list
    an unparseable entry is a DENIAL, so it has to be as loud as any other.
    """
    out: list = []
    for item in (value or "").split(_RECORD):
        if not item:
            continue
        mode, sep, path = item.partition(_UNIT)
        if not sep or mode not in MODES or not path:
            raise LandlockUnavailable(f"malformed landlock allow-list record: {item!r}")
        out.append((path, mode))
    return out


def format_env(allow: Iterable) -> str:
    """The `LANDLOCK_ENV` spelling of an allow-list. Inverse of `parse_env`."""
    return _RECORD.join(f"{mode}{_UNIT}{path}" for path, mode in allow)


# The child-side launcher: a fresh single-threaded interpreter that applies the ruleset and then
# `execvp`s the real argv, exactly the shape `runtime/sandbox.py::_RLIMIT_LAUNCHER` already uses and
# for the same recorded reason — `preexec_fn` is not an option under threaded evaluators, and this
# engine runs evals from `anyio.to_thread` workers. `execvp` keeps the pid, cwd, env, session and
# inherited pipes, so `_kill_tree`'s pgid, `proc.pid` and the drain threads all see the process they
# would have seen anyway, and Landlock is inherited across the exec by the kernel.
#
# It cannot `import looplab`: the eval may be a different interpreter in a different virtualenv. So
# the syscall numbers and access bits are TEMPLATED IN from this module's own constants rather than
# hand-copied — the failure mode of a hand-copy here is not a crash but a WRONG ruleset, which under
# an allow-list means silently refusing paths nobody asked to refuse.
#
# FAIL-CLOSED, and this is the one policy decision inside the child. If the ruleset cannot be applied
# the launcher REFUSES to exec, with the reason on stderr — i.e. in `eval.log`, in the captured
# `RunResult.stderr`, and therefore in the repair feedback. Running the eval unrestricted instead
# would mean an operator who set `landlock="enforce"` silently gets no boundary, which is worse than
# a failed launch they can see: the whole content of this rung is a guarantee about what was NOT
# readable while the number was produced.
_LAUNCHER = """import ctypes, os, sys
_UNIT, _RECORD = %(unit)r, %(record)r
_CREATE, _ADD, _RESTRICT = %(create)d, %(add)d, %(restrict)d
_READ, _WRITE, _HANDLED = %(read)d, %(write)d, %(handled)d
_a = sys.argv[1:]
_spec, _argv = _a[0], _a[2:]


class _Attr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]


class _Rule(ctypes.Structure):
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


def _die(msg):
    sys.stderr.write("LOOPLAB landlock: %%s\\n" %% (msg,))
    sys.exit(126)


_lib = ctypes.CDLL(None, use_errno=True)
_attr = _Attr(handled_access_fs=_HANDLED)
_fd = _lib.syscall(ctypes.c_long(_CREATE), ctypes.byref(_attr),
                   ctypes.c_size_t(ctypes.sizeof(_attr)), ctypes.c_uint32(0))
if _fd < 0:
    _die("landlock_create_ruleset failed: %%s" %% os.strerror(ctypes.get_errno()))
_rules = 0
for _item in _spec.split(_RECORD):
    if not _item:
        continue
    _mode, _sep, _path = _item.partition(_UNIT)
    if not _sep or _mode not in ("read", "readwrite") or not _path:
        _die("malformed allow-list record %%r (an unparseable record is a DENIAL, not a no-op)"
             %% (_item,))
    try:
        _pfd = os.open(_path, os.O_PATH | os.O_CLOEXEC)
    except OSError as exc:
        _die("cannot allow-list %%s: %%s (refusing to enforce a ruleset that would deny a path it "
             "was asked to allow)" %% (_path, exc))
    _r = _Rule(allowed_access=(_WRITE if _mode == "readwrite" else _READ), parent_fd=_pfd)
    ctypes.set_errno(0)
    if _lib.syscall(ctypes.c_long(_ADD), ctypes.c_int(_fd), ctypes.c_uint32(1),
                    ctypes.byref(_r), ctypes.c_uint32(0)) != 0:
        _die("landlock_add_rule(%%s) failed: %%s" %% (_path, os.strerror(ctypes.get_errno())))
    os.close(_pfd)
    _rules += 1
if not _rules:
    _die("empty allow-list: an empty ruleset denies EVERYTHING, so this would refuse the eval's own "
         "workdir and its interpreter. Nothing was restricted and nothing was run.")
ctypes.set_errno(0)
if _lib.prctl(ctypes.c_int(38), ctypes.c_ulong(1), ctypes.c_ulong(0),
              ctypes.c_ulong(0), ctypes.c_ulong(0)) != 0:
    _die("prctl(PR_SET_NO_NEW_PRIVS) failed: %%s" %% os.strerror(ctypes.get_errno()))
ctypes.set_errno(0)
if _lib.syscall(ctypes.c_long(_RESTRICT), ctypes.c_int(_fd), ctypes.c_uint32(0)) != 0:
    _die("landlock_restrict_self failed: %%s" %% os.strerror(ctypes.get_errno()))
os.close(_fd)
try:
    os.execvp(_argv[0], _argv)
except OSError as exc:
    sys.stderr.write("failed to launch: %%s\\n" %% (exc,))
    sys.exit(127)
"""


# The NO-MUTATION rung, as source to be spliced INTO a caller's own generated launcher rather than
# wrapped around its argv. Two differences from `_LAUNCHER` above, and both follow from the ruleset
# shape rather than from taste:
#
#   * it applies IN PROCESS instead of exec'ing. `_LAUNCHER` has to be a separate `python -c` because
#     its caller is `run_argv`, a universal choke point running under `anyio.to_thread` workers where
#     a `preexec_fn` is the recorded deadlock shape. `dev_probe` already generates and runs its OWN
#     launcher — a fresh, single-threaded interpreter that runs before one byte of the probe program
#     — so there is a seam here that costs no extra process. `landlock_restrict_self` is per-thread
#     and irreversible, which is exactly what a boundary wants;
#   * it does NOT fail closed by refusing to run. `_LAUNCHER` refuses because an operator who asked
#     for `landlock="enforce"` and silently got nothing has lost the whole content of that rung. Here
#     the rung is one of THREE (the audit hook and `RLIMIT_FSIZE 0` are the other two) and it is the
#     only one that can be unavailable, so refusing would take a probe surface that works on this
#     kernel and break it on the next box. It reports instead: one line naming what is no longer
#     covered, on the probe's own stderr, where the model and the operator both read it. An
#     overclaimed guarantee is worse than a stated limit — and a SILENT reduced guarantee is worse
#     than both.
_NO_MUTATION_SOURCE = '''\
def _looplab_no_mutation_ruleset():
    """Kernel rung: deny every filesystem MUTATION, for this process and anything it starts.

    Returns None on success, or a one-line reason it could not be applied. Grants nothing and
    handles no read bit, so it cannot refuse a read — see `runtime/landlock.py::NO_MUTATION_HANDLED`.
    """
    import ctypes
    import os

    class _Attr(ctypes.Structure):
        _fields_ = [("handled_access_fs", ctypes.c_uint64)]

    try:
        lib = ctypes.CDLL(None, use_errno=True)
    except Exception as exc:                       # noqa: BLE001 — no libc reachable (rare, stated)
        return "libc unavailable: %%s" %% (exc,)
    attr = _Attr(handled_access_fs=%(handled)d)
    ctypes.set_errno(0)
    fd = lib.syscall(ctypes.c_long(%(create)d), ctypes.byref(attr),
                     ctypes.c_size_t(ctypes.sizeof(attr)), ctypes.c_uint32(0))
    if fd < 0:
        return ("landlock_create_ruleset failed: %%s (Landlock needs Linux 5.13+ with "
                "CONFIG_SECURITY_LANDLOCK=y)" %% os.strerror(ctypes.get_errno()))
    try:
        ctypes.set_errno(0)
        if lib.prctl(ctypes.c_int(38), ctypes.c_ulong(1), ctypes.c_ulong(0),
                     ctypes.c_ulong(0), ctypes.c_ulong(0)) != 0:   # PR_SET_NO_NEW_PRIVS
            return "prctl(PR_SET_NO_NEW_PRIVS) failed: %%s" %% os.strerror(ctypes.get_errno())
        ctypes.set_errno(0)
        if lib.syscall(ctypes.c_long(%(restrict)d), ctypes.c_int(fd), ctypes.c_uint32(0)) != 0:
            return "landlock_restrict_self failed: %%s" %% os.strerror(ctypes.get_errno())
    finally:
        os.close(fd)
    return None
'''

# The NAME the spliced source defines, so a caller calls it without hard-coding a spelling this
# module owns.
NO_MUTATION_FUNCTION = "_looplab_no_mutation_ruleset"


# --------------------------------------------------------------------------- the READ half
# The mirror image of the block above, and it exists for a threat the write rung does not touch:
# a BENCHMARK CONTESTANT reading the grader. Measured 2026-08-21 on a live AlgoTune arm -- of 119
# `run_probe` calls in one task, 116 reached outside the workdir by absolute path and read the
# evaluation bridge, the scorer, the cached reference timings and the dataset directory. The read
# FENCE could not have stopped it and was not failing: `read_fence` is a deny-prefix on the
# operator's EDITABLE tree, and the grader is not under one. "May this node read the operator's
# source" and "may this contestant read the answer key" are different questions, and only the second
# one needs an allow-list.
#
# Why the kernel and not the audit hook: the hook covers `open` inside ONE interpreter. This covers
# `ctypes` into libc, a native reader, a syscall CPython does not audit, and -- the one that decides
# it -- a child across `execve`. Verified against all of those, including a freshly exec'd
# `python -I -S -E` with every fence-delivery mechanism stripped.
#
# TWO THINGS IT DOES NOT DO, and both must be said wherever this is turned on:
#   * ABI 2 has no metadata right, so `os.stat` still reports existence, size and mtime. BYTES are
#     confined; NAMES are not. For benchmark integrity that is the right trade -- an answer key is
#     bytes -- but it is not invisibility.
#   * It cannot make a path unreachable that the allow-list contains. If the grader is installed
#     INTO site-packages, granting site-packages grants the grader. Path confinement cannot separate
#     two things at the same path; only not putting them there can (the operator's E.1).
READ_CONFINE_HANDLED = FS_READ_FILE | FS_READ_DIR

_READ_CONFINE_SOURCE = '''\
def _looplab_read_confine(allow):
    """Kernel rung: read only under `allow`, for this process and everything it starts.

    `allow` is a tuple of paths. Returns `(reason_or_None, skipped)` where `skipped` is
    `[(path, why)]` -- under an ALLOW-list a path that cannot be opened contributes no rule, which
    means the kernel denies it. That list is never swallowed: measured upstream, 211 candidate rules
    produced 55 accepted ones, i.e. 156 silent denials, and a silent denial here reads to the caller
    as a broken interpreter rather than as a missing rule.
    """
    import ctypes
    import os

    class _Attr(ctypes.Structure):
        _fields_ = [("handled_access_fs", ctypes.c_uint64)]

    class _Beneath(ctypes.Structure):
        _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int)]

    try:
        lib = ctypes.CDLL(None, use_errno=True)
    except Exception as exc:                       # noqa: BLE001 — no libc reachable (rare, stated)
        return ("libc unavailable: %%s" %% (exc,), [])
    attr = _Attr(handled_access_fs=%(handled)d)
    ctypes.set_errno(0)
    fd = lib.syscall(ctypes.c_long(%(create)d), ctypes.byref(attr),
                     ctypes.c_size_t(ctypes.sizeof(attr)), ctypes.c_uint32(0))
    if fd < 0:
        return (("landlock_create_ruleset failed: %%s (Landlock needs Linux 5.13+ with "
                 "CONFIG_SECURITY_LANDLOCK=y)" %% os.strerror(ctypes.get_errno())), [])
    skipped = []
    try:
        for _path in allow:
            try:
                pfd = os.open(str(_path), os.O_PATH | os.O_CLOEXEC)
            except OSError as exc:
                skipped.append((str(_path), exc.strerror or str(exc)))
                continue
            try:
                rule = _Beneath(allowed_access=%(handled)d, parent_fd=pfd)
                ctypes.set_errno(0)
                if lib.syscall(ctypes.c_long(%(add)d), ctypes.c_int(fd), ctypes.c_uint32(1),
                               ctypes.byref(rule), ctypes.c_uint32(0)) != 0:
                    skipped.append((str(_path), os.strerror(ctypes.get_errno())))
            finally:
                os.close(pfd)
        ctypes.set_errno(0)
        if lib.prctl(ctypes.c_int(38), ctypes.c_ulong(1), ctypes.c_ulong(0),
                     ctypes.c_ulong(0), ctypes.c_ulong(0)) != 0:   # PR_SET_NO_NEW_PRIVS
            return ("prctl(PR_SET_NO_NEW_PRIVS) failed: %%s" %% os.strerror(ctypes.get_errno()),
                    skipped)
        ctypes.set_errno(0)
        if lib.syscall(ctypes.c_long(%(restrict)d), ctypes.c_int(fd), ctypes.c_uint32(0)) != 0:
            return ("landlock_restrict_self failed: %%s" %% os.strerror(ctypes.get_errno()), skipped)
    finally:
        os.close(fd)
    return (None, skipped)
'''

READ_CONFINE_FUNCTION = "_looplab_read_confine"


def read_confine_source() -> str:
    """Source defining `READ_CONFINE_FUNCTION` — the read allow-list rung, self-contained.

    Spliced rather than imported for `no_mutation_source`'s reason: the launcher runs in the probe's
    own interpreter, which may not have this package importable, and a hand-copied constant does not
    crash — it silently produces a different ruleset."""
    return _READ_CONFINE_SOURCE % {"handled": READ_CONFINE_HANDLED,
                                   "create": _SYS_LANDLOCK_CREATE_RULESET,
                                   "add": _SYS_LANDLOCK_ADD_RULE,
                                   "restrict": _SYS_LANDLOCK_RESTRICT_SELF}


def no_mutation_source() -> str:
    """Source defining `NO_MUTATION_FUNCTION` — the deny-every-mutation kernel rung, self-contained.

    For a caller that generates its OWN child launcher and needs the rung inside it (`dev_probe`).
    The syscall numbers and the handled-bit mask are TEMPLATED IN from this module's constants for
    the reason `_LAUNCHER` states: a hand-copied bit here does not crash, it silently produces a
    ruleset that polices something other than what was asked for."""
    return _NO_MUTATION_SOURCE % {"handled": NO_MUTATION_HANDLED,
                                  "create": _SYS_LANDLOCK_CREATE_RULESET,
                                  "restrict": _SYS_LANDLOCK_RESTRICT_SELF}


def launcher_source() -> str:
    """The `-c` source for `launch_argv`, with this module's constants substituted in."""
    return _LAUNCHER % {"unit": _UNIT, "record": _RECORD, "create": _SYS_LANDLOCK_CREATE_RULESET,
                        "add": _SYS_LANDLOCK_ADD_RULE, "restrict": _SYS_LANDLOCK_RESTRICT_SELF,
                        "read": _READ, "write": _WRITE, "handled": _HANDLED}


def launch_argv(python: str, spec: str, argv: list) -> list:
    """`argv` wrapped so it runs under the allow-list `spec` (a `LANDLOCK_ENV` string)."""
    return [python, "-c", launcher_source(), spec, "--", *argv]
