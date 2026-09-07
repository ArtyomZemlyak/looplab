"""SECCOMP — a kernel SYSCALL policy for one eval process and everything it spawns (doc 52 row 28).

WHY THIS EXISTS BESIDE `landlock.py` AND `read_fence.py`
--------------------------------------------------------
The shipped boundary is path-based: the audit hook (`read_fence.py`) refuses a Python `open` under
an editable root with an actionable message, and Landlock (`landlock.py`) refuses the same read in
the kernel's own path walk so a native reader is covered too. Neither can say anything about a call
that names NO path: on the default tier an eval could `connect()` anywhere, and the two mutators
`dev_probe` found audit-invisible — `mknod` and `mkfifo` — had no kernel rung at all (Landlock's
`FS_MAKE_CHAR`/`FS_MAKE_FIFO` bits exist, but only where Landlock does, and this box has none: see
below). Sandlock (arXiv 2605.26298) enforces FS + TCP + IPC + syscall policy without root at ~5 ms
startup on exactly this constraint set; this module is its SYSCALL rung, on the shape the two
existing rungs already have — a classic-BPF filter installed with `prctl(2)`, no root, no
libseccomp, no package to install, inherited across `fork`/`exec` by the kernel.

THE TWO POLICIES, and why the vocabulary stops there:
  * `mutators` — `mknod`/`mknodat` answer `EPERM`. A device node or a FIFO under the run record is
    the one filesystem mutation the audit hook cannot see and the run's `RLIMIT_FSIZE 0` cannot
    bound (a FIFO holds no bytes). Nothing an ML eval legitimately does needs either.
  * `egress` — `mutators`, plus `socket(AF_INET|AF_INET6)` answers `EPERM`. A seccomp filter sees
    the syscall's REGISTER arguments and never the memory they point at, so it can refuse a socket
    FAMILY and cannot read the address a later `connect()` would dial. That is the exact limit of
    this rung and it is stated rather than hidden: `egress` refuses loopback TCP too, so a
    `torchrun`/NCCL rendezvous over `127.0.0.1` does not run under it. The address-aware rungs are
    Landlock TCP (`LANDLOCK_ACCESS_NET_CONNECT_TCP`, ABI 4 — the box measured ABI 2) and a network
    namespace with only `lo` up (`unshare -n`, root or unprivileged user namespaces); neither is
    measured here, and an `egress` that silently allowed loopback would be the wider claim.

MEASURED ON THIS BOX (2026-09-06, kernel 6.18.44, x86_64, inside a container whose
`landlock_create_ruleset` answers `ENOSYS` — i.e. a box on which the OTHER kernel rung does not
exist at all):
    install (PR_SET_NO_NEW_PRIVS + PR_SET_SECCOMP)         219 us
    getpid(2), 200,000 iterations, best-of                  145 ns -> 174 ns   (+29 ns / syscall)
    os.mkfifo, socket(AF_INET), in a CHILD of the filtered  EPERM, EPERM
    socket(AF_UNIX), a file write, a pipe                   unaffected
The per-syscall cost is the filter's own length — twelve instructions — and is paid by every
syscall the eval makes; on a training step that is dozens of syscalls against milliseconds of
compute, which is why Sandlock reports its whole envelope at ~5 ms startup and no measurable
steady-state cost.

WHY OFF IS THE DEFAULT. `egress` breaks two things real runs do — a Hugging Face download inside
the eval and a loopback rendezvous — and `mutators` alone has never been run through a real GPU
eval either, the same unretired unknown `Settings.landlock` records. The evidence that moves it
is the same: one real train+score completed under the policy. The rung ships so an operator can
turn it on for the runs whose declarations already stage every input, and so the probe
(`tools/dev_probe.py`) — which needs neither device nodes nor FIFOs — carries `mutators` always.

THE LAUNCHER, not a `preexec_fn`, for the reason `sandbox.py::_RLIMIT_LAUNCHER` records: evals run
from `anyio.to_thread` workers and a `preexec_fn` under a threaded parent is the deadlock shape
that pattern exists to avoid. The parent builds the program for ITS OWN machine and hands the child
the bytes as hex in argv — the child runs on the same kernel, and a program templated by numbers
into source is one hand-copied constant away from policing the wrong syscall. A foreign ABI (an
`int 0x80` call on x86_64) answers `ENOSYS` rather than a kill: a refusal the process can report
beats a silent death, and an eval has no business making one.

FAIL CLOSED, like `landlock._LAUNCHER`: a policy the launcher cannot parse, an architecture this
module has no table for, or a `prctl` the kernel refuses ends the launch at exit 126 with one line
naming why. An operator who asked for a fence and silently got none has lost its whole content.
"""
from __future__ import annotations

import ctypes
import errno
import os
import platform
import struct
import sys
from typing import Optional

# The policy vocabulary. `core/config.py::Settings._ENUM_FIELDS` spells the same three words for
# the reason `read_fence.POLICIES` is respelled there: `core` imports nothing above itself.
POLICIES = ("off", "mutators", "egress")

# The env variable the engine stamps onto an eval launch (`engine/resources.py::_fenced_env`); its
# value is the policy name and `sandbox.run_argv` wraps the launch when it is present.
SECCOMP_ENV = "LOOPLAB_SYSCALL_FENCE"

# AUDIT_ARCH_* and the syscall numbers this rung polices, per machine. `mknod` does not exist on
# aarch64 (glibc's `mknod(3)` is `mknodat(2)` there), which is why the table is per architecture and
# not a flat list: a number copied from one table into the other polices a different syscall.
_ARCH = {
    "x86_64": (0xC000003E, {"mknod": 133, "mknodat": 259, "socket": 41}),
    "aarch64": (0xC00000B7, {"mknodat": 33, "socket": 198}),
}
_AF_INET, _AF_INET6 = 2, 10

# classic BPF (linux/filter.h)
_BPF_LD, _BPF_W, _BPF_ABS = 0x00, 0x00, 0x20
_BPF_JMP, _BPF_JEQ, _BPF_K, _BPF_RET = 0x05, 0x10, 0x00, 0x06
_RET_ALLOW = 0x7FFF0000
_RET_ERRNO = 0x00050000
# struct seccomp_data offsets: nr @0, arch @4, args[0] low word @16 (little-endian)
_OFF_NR, _OFF_ARCH, _OFF_ARG0 = 0, 4, 16
_PR_SET_NO_NEW_PRIVS, _PR_SET_SECCOMP, _PR_GET_SECCOMP = 38, 22, 21
_SECCOMP_MODE_FILTER = 2


class SeccompUnavailable(RuntimeError):
    """This kernel/process/architecture cannot apply the filter, with the reason as the message."""


def _stmt(code: int, k: int) -> bytes:
    return struct.pack("HBBI", code, 0, 0, k)


def _jump(code: int, k: int, jt: int, jf: int) -> bytes:
    return struct.pack("HBBI", code, jt, jf, k)


def machine_table(machine: Optional[str] = None) -> tuple[int, dict]:
    """`(AUDIT_ARCH, {name: nr})` for `machine` (default: this one), or `SeccompUnavailable`."""
    name = machine or platform.machine()
    if name not in _ARCH:
        raise SeccompUnavailable(f"no syscall table for machine {name!r} (have {sorted(_ARCH)})")
    return _ARCH[name]


def program(policy: str, machine: Optional[str] = None) -> bytes:
    """The classic-BPF program enforcing `policy` on `machine`, as the bytes `prctl` takes."""
    if policy not in POLICIES or policy == "off":
        raise SeccompUnavailable(f"not a syscall-fence policy: {policy!r} (one of {POLICIES[1:]})")
    arch, nr = machine_table(machine)
    eperm = _RET_ERRNO | (errno.EPERM & 0xFFFF)
    prog = [
        _stmt(_BPF_LD | _BPF_W | _BPF_ABS, _OFF_ARCH),
        _jump(_BPF_JMP | _BPF_JEQ | _BPF_K, arch, 1, 0),
        _stmt(_BPF_RET | _BPF_K, _RET_ERRNO | (errno.ENOSYS & 0xFFFF)),   # a foreign ABI
        _stmt(_BPF_LD | _BPF_W | _BPF_ABS, _OFF_NR),
    ]
    for name in ("mknod", "mknodat"):
        if name in nr:
            prog += [_jump(_BPF_JMP | _BPF_JEQ | _BPF_K, nr[name], 0, 1),
                     _stmt(_BPF_RET | _BPF_K, eperm)]
    if policy == "egress":
        prog += [
            _jump(_BPF_JMP | _BPF_JEQ | _BPF_K, nr["socket"], 0, 5),
            _stmt(_BPF_LD | _BPF_W | _BPF_ABS, _OFF_ARG0),                # the address FAMILY
            _jump(_BPF_JMP | _BPF_JEQ | _BPF_K, _AF_INET, 2, 0),
            _jump(_BPF_JMP | _BPF_JEQ | _BPF_K, _AF_INET6, 1, 0),
            _stmt(_BPF_RET | _BPF_K, _RET_ALLOW),
            _stmt(_BPF_RET | _BPF_K, eperm),
        ]
    prog.append(_stmt(_BPF_RET | _BPF_K, _RET_ALLOW))
    return b"".join(prog)


class _Fprog(ctypes.Structure):
    _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.c_void_p)]


def _libc():
    return ctypes.CDLL(None, use_errno=True)


def available() -> Optional[str]:
    """None when this process could install a filter, else the one-line reason it could not."""
    if not sys.platform.startswith("linux"):
        return f"seccomp is Linux-only (this is {sys.platform})"
    try:
        machine_table()
    except SeccompUnavailable as exc:
        return str(exc)
    try:
        lib = _libc()
    except OSError as exc:
        return f"libc unavailable: {exc}"
    ctypes.set_errno(0)
    if lib.prctl(_PR_GET_SECCOMP, 0, 0, 0, 0) < 0:
        return f"prctl(PR_GET_SECCOMP) failed: {os.strerror(ctypes.get_errno())} (no CONFIG_SECCOMP?)"
    return None


def install(prog: bytes) -> None:
    """Install `prog` on THIS process — irreversible, inherited by everything it starts."""
    reason = available()
    if reason:
        raise SeccompUnavailable(reason)
    if not prog or len(prog) % 8:
        raise SeccompUnavailable("malformed filter program")
    lib = _libc()
    buf = ctypes.create_string_buffer(prog, len(prog))
    fprog = _Fprog(len(prog) // 8, ctypes.addressof(buf))
    ctypes.set_errno(0)
    if lib.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        raise SeccompUnavailable(f"prctl(PR_SET_NO_NEW_PRIVS) failed: {os.strerror(ctypes.get_errno())}")
    ctypes.set_errno(0)
    if lib.prctl(_PR_SET_SECCOMP, _SECCOMP_MODE_FILTER, ctypes.byref(fprog), 0, 0) != 0:
        raise SeccompUnavailable(f"prctl(PR_SET_SECCOMP) failed: {os.strerror(ctypes.get_errno())}")


def apply(policy: str) -> None:
    """Restrict THIS process (and everything it spawns) to `policy`."""
    install(program(policy))


# The exec'd launcher, the shape of `landlock._LAUNCHER`: argv = [policy, hexprog, "--", *argv].
_LAUNCHER = """import ctypes, os, sys
_a = sys.argv[1:]
_policy, _hex, _argv = _a[0], _a[1], _a[3:]


class _Fprog(ctypes.Structure):
    _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.c_void_p)]


def _die(msg):
    sys.stderr.write("LOOPLAB syscall fence: %s\\n" % (msg,))
    sys.exit(126)


if _policy not in ("mutators", "egress"):
    _die("not a policy: %r (an unparseable policy is a refusal, not a no-op)" % (_policy,))
try:
    _prog = bytes.fromhex(_hex)
except ValueError:
    _prog = b""
if not _prog or len(_prog) % 8:
    _die("malformed filter program for policy %r" % (_policy,))
_lib = ctypes.CDLL(None, use_errno=True)
_buf = ctypes.create_string_buffer(_prog, len(_prog))
_fp = _Fprog(len(_prog) // 8, ctypes.addressof(_buf))
ctypes.set_errno(0)
if _lib.prctl(38, 1, 0, 0, 0) != 0:
    _die("prctl(PR_SET_NO_NEW_PRIVS) failed: %s" % os.strerror(ctypes.get_errno()))
ctypes.set_errno(0)
if _lib.prctl(22, 2, ctypes.byref(_fp), 0, 0) != 0:
    _die("prctl(PR_SET_SECCOMP) failed: %s" % os.strerror(ctypes.get_errno()))
try:
    os.execvp(_argv[0], _argv)
except OSError as exc:
    sys.stderr.write("failed to launch: %s\\n" % (exc,))
    sys.exit(127)
"""


def launcher_source() -> str:
    """The `-c` source for `launch_argv`."""
    return _LAUNCHER


def launch_argv(python: str, policy: str, argv: list) -> list:
    """`argv` wrapped so it runs under `policy`. The program is built HERE, for this machine; a
    policy this module cannot build a program for still reaches the child, which refuses it with
    the launcher's own message (fail closed, never a silent unfenced launch)."""
    try:
        hexprog = program(policy).hex()
    except SeccompUnavailable:
        hexprog = ""
    return [python, "-c", launcher_source(), str(policy), hexprog, "--", *argv]


# The MUTATOR rung as source spliced into a caller's own generated launcher (`tools/dev_probe.py`),
# the twin of `landlock.no_mutation_source()`: applies IN PROCESS, and reports a reason instead of
# refusing to run, because the probe has two other rungs and this is the one that can be missing.
NO_MUTATION_FUNCTION = "_looplab_no_mknod_filter"
_NO_MUTATION_SOURCE = '''\
def _looplab_no_mknod_filter():
    """Kernel rung: `mknod`/`mknodat` answer EPERM for this process and anything it starts —
    the two mutators the audit hook cannot see and RLIMIT_FSIZE cannot bound (a FIFO holds no
    bytes). Returns None on success, else a one-line reason (`runtime/seccomp.py`)."""
    import ctypes
    import os

    class _Fprog(ctypes.Structure):
        _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.c_void_p)]

    prog = bytes.fromhex(%(hexprog)r)
    if not prog:
        return %(reason)r
    try:
        lib = ctypes.CDLL(None, use_errno=True)
    except Exception as exc:                       # noqa: BLE001 — no libc reachable (rare, stated)
        return "libc unavailable: %%s" %% (exc,)
    buf = ctypes.create_string_buffer(prog, len(prog))
    fprog = _Fprog(len(prog) // 8, ctypes.addressof(buf))
    ctypes.set_errno(0)
    if lib.prctl(38, 1, 0, 0, 0) != 0:
        return "prctl(PR_SET_NO_NEW_PRIVS) failed: %%s" %% os.strerror(ctypes.get_errno())
    ctypes.set_errno(0)
    if lib.prctl(22, 2, ctypes.byref(fprog), 0, 0) != 0:
        return "prctl(PR_SET_SECCOMP) failed: %%s" %% os.strerror(ctypes.get_errno())
    return None
'''


def no_mutation_source() -> str:
    """Source defining `NO_MUTATION_FUNCTION` for this machine — the `mutators` program as a hex
    literal, or the reason none could be built, which the function then reports."""
    try:
        hexprog, reason = program("mutators").hex(), ""
    except SeccompUnavailable as exc:
        hexprog, reason = "", str(exc)
    reason = reason or (available() or "")
    if reason:
        hexprog = ""
    return _NO_MUTATION_SOURCE % {"hexprog": hexprog, "reason": reason or "unavailable"}


def _self_check(policy: str) -> list[str]:
    """What a child under `policy` can and cannot do, one line each — the operator's proof."""
    import subprocess
    probe = (
        "import os, socket, sys, tempfile\n"
        "d = tempfile.mkdtemp()\n"
        "def show(name, fn):\n"
        "    try:\n"
        "        fn(); print(name, 'ALLOWED')\n"
        "    except OSError as e:\n"
        "        print(name, 'REFUSED errno', e.errno)\n"
        "show('mkfifo', lambda: os.mkfifo(os.path.join(d, 'f')))\n"
        "show('file write', lambda: open(os.path.join(d, 'w'), 'w').close())\n"
        "show('socket AF_INET', lambda: socket.socket(socket.AF_INET).close())\n"
        "show('socket AF_INET6', lambda: socket.socket(socket.AF_INET6).close())\n"
        "show('socket AF_UNIX', lambda: socket.socket(socket.AF_UNIX).close())\n"
        "show('pipe', lambda: [os.close(x) for x in os.pipe()])\n")
    out = subprocess.run(launch_argv(sys.executable, policy, [sys.executable, "-c", probe]),
                         capture_output=True, text=True)
    return [line for line in (out.stdout + out.stderr).splitlines() if line.strip()]


def main(argv: Optional[list] = None) -> int:
    """`python -m looplab.runtime.seccomp [mutators|egress]`: is the rung available here, and what
    does a child under the policy answer — the validation an operator runs before turning it on."""
    args = list(sys.argv[1:] if argv is None else argv)
    policy = args[0] if args else "egress"
    reason = available()
    print(f"machine={platform.machine()} policy={policy} "
          f"available={'yes' if reason is None else 'no: ' + reason}")
    if reason:
        return 2
    for line in _self_check(policy):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
