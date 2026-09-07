"""The kernel SYSCALL rung (doc 52 row 28): `runtime/seccomp.py`, its plumbing, and the sentence a
kernel refusal is rewritten into before a judge reads it.

Every enforcement test drives a REAL child through `launch_argv` or `run_argv` — the point of the
rung is inheritance across `exec`, and a filter this process installed on itself would end the test
runner's own network. The translation tests drive `fence_refusal_note` on the shapes CPython prints.
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest

from looplab.core.config import Settings
from looplab.engine.failure_diagnosis import fence_refusal_note
from looplab.runtime import seccomp
from looplab.runtime.sandbox import run_argv
from tests._source_scan import eval_attempt_called_names

_NO_SECCOMP = seccomp.available()
needs_seccomp = pytest.mark.skipif(_NO_SECCOMP is not None, reason=str(_NO_SECCOMP))

_PROBE = textwrap.dedent("""
    import os, socket, sys, tempfile
    d = tempfile.mkdtemp()
    def show(name, fn):
        try:
            fn(); print(name, 'ALLOWED')
        except OSError as e:
            print(name, 'REFUSED', e.errno)
    show('mkfifo', lambda: os.mkfifo(os.path.join(d, 'f')))
    show('write', lambda: open(os.path.join(d, 'w'), 'w').close())
    show('inet', lambda: socket.socket(socket.AF_INET).close())
    show('unix', lambda: socket.socket(socket.AF_UNIX).close())
    show('pipe', lambda: [os.close(x) for x in os.pipe()])
""")


def _child(policy: str, code: str = _PROBE, **kw) -> dict:
    argv = seccomp.launch_argv(sys.executable, policy, [sys.executable, "-c", code])
    out = subprocess.run(argv, capture_output=True, text=True, **kw)
    assert out.returncode == 0, (out.stdout, out.stderr)
    return dict(line.split(" ", 1) for line in out.stdout.strip().splitlines())


def test_the_policy_vocabulary_is_the_one_settings_spells():
    row = dict(Settings._ENUM_FIELDS)["syscall_fence"]
    assert tuple(row) == seccomp.POLICIES
    assert Settings().syscall_fence == "off"


def test_the_program_checks_the_architecture_first_and_ends_by_allowing():
    prog = seccomp.program("mutators", machine="x86_64")
    assert len(prog) % 8 == 0
    head, tail = prog[:8], prog[-8:]
    assert head == bytes.fromhex("2000000004000000"), "ld [arch] is the first instruction"
    assert tail == bytes.fromhex("06000000" "0000ff7f"), "ret ALLOW is the last"
    assert len(seccomp.program("egress", machine="x86_64")) == len(prog) + 6 * 8
    assert len(seccomp.program("mutators", machine="aarch64")) == len(prog) - 2 * 8, "no mknod there"
    with pytest.raises(seccomp.SeccompUnavailable):
        seccomp.program("off")
    with pytest.raises(seccomp.SeccompUnavailable):
        seccomp.program("mutators", machine="riscv64")


@needs_seccomp
def test_mutators_refuse_mkfifo_and_leave_files_and_sockets_alone():
    seen = _child("mutators")
    assert seen["mkfifo"] == "REFUSED 1", seen
    assert seen["write"] == "ALLOWED" and seen["unix"] == "ALLOWED" and seen["pipe"] == "ALLOWED"
    assert seen["inet"] == "ALLOWED", "mutators is not an egress policy"


@needs_seccomp
def test_egress_refuses_inet_sockets_and_a_grandchild_inherits_the_filter():
    seen = _child("egress")
    assert seen["mkfifo"] == "REFUSED 1" and seen["inet"] == "REFUSED 1", seen
    assert seen["unix"] == "ALLOWED" and seen["write"] == "ALLOWED"
    grandchild = textwrap.dedent("""
        import subprocess, sys
        r = subprocess.run([sys.executable, "-c",
            "import socket\\ntry:\\n    socket.socket(socket.AF_INET)\\n    print('inet ALLOWED')\\n"
            "except OSError as e:\\n    print('inet REFUSED', e.errno)"], capture_output=True, text=True)
        print(r.stdout.strip())
    """)
    assert _child("egress", grandchild)["inet"] == "REFUSED 1"


@needs_seccomp
def test_a_policy_the_launcher_cannot_parse_refuses_the_launch():
    argv = seccomp.launch_argv(sys.executable, "bogus", [sys.executable, "-c", "print('RAN')"])
    out = subprocess.run(argv, capture_output=True, text=True)
    assert out.returncode == 126 and "not a policy" in out.stderr and "RAN" not in out.stdout


@needs_seccomp
def test_run_argv_wraps_only_when_the_env_carries_the_policy(tmp_path):
    code = "import ctypes; print(ctypes.CDLL(None).prctl(21, 0, 0, 0, 0))"   # PR_GET_SECCOMP
    rc, out, err, _ = run_argv([sys.executable, "-c", code], str(tmp_path), 60)
    assert rc == 0 and out.strip() == "0", (out, err)
    rc, out, err, _ = run_argv([sys.executable, "-c", code], str(tmp_path), 60,
                               {seccomp.SECCOMP_ENV: "mutators"})
    assert rc == 0 and out.strip() == "2", (out, err)


def test_run_argv_leaves_a_docker_argv_alone(tmp_path, monkeypatch):
    seen = {}

    def fake_popen(argv, **kw):
        seen["argv"] = list(argv)
        raise OSError("stop here")
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    run_argv(["docker", "run", "--rm", "img"], str(tmp_path), 60, {seccomp.SECCOMP_ENV: "egress"})
    assert seen["argv"][0] == "docker", "a container launch is never wrapped in a host launcher"


def test_the_engine_stamps_the_policy_only_when_it_is_on(tmp_path):
    from tests.factories import make_engine
    off = make_engine(tmp_path / "off")
    assert seccomp.SECCOMP_ENV not in (off._fenced_env({}) or {})
    on = make_engine(tmp_path / "on", syscall_fence="egress")
    assert on._fenced_env({})[seccomp.SECCOMP_ENV] == "egress"
    bogus = make_engine(tmp_path / "bogus", syscall_fence="bogus")
    assert seccomp.SECCOMP_ENV not in (bogus._fenced_env({}) or {}), "a word outside the vocabulary stamps nothing"


@needs_seccomp
def test_the_probe_launcher_carries_the_mknod_rung_and_it_holds_against_a_native_caller(tmp_path):
    from looplab.tools.dev_probe import render_launcher
    source = render_launcher(str(tmp_path / "program.py"))
    assert seccomp.NO_MUTATION_FUNCTION in source
    # The rung alone, in a fresh interpreter: a ctypes call into libc — no audit event, no Python
    # `os.mkfifo` for the hook to wrap — is refused by the KERNEL.
    code = seccomp.no_mutation_source() + textwrap.dedent(f"""
        import ctypes, os
        reason = {seccomp.NO_MUTATION_FUNCTION}()
        assert reason is None, reason
        libc = ctypes.CDLL(None, use_errno=True)
        rc = libc.mkfifo({str(tmp_path / 'fifo').encode()!r}, 0o600)
        print('mkfifo', rc, ctypes.get_errno())
        print('exists', os.path.exists({str(tmp_path / 'fifo')!r}))
    """)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert "mkfifo -1 1" in out.stdout and "exists False" in out.stdout


class _Res:
    def __init__(self, stderr):
        self.stderr = stderr


def test_a_landlock_eacces_is_rewritten_into_the_fences_sentence_only_when_the_rung_is_on():
    err = ("Traceback (most recent call last):\n  File \"train.py\", line 3, in <module>\n"
           "PermissionError: [Errno 13] Permission denied: '/data/teacher.pt'\n")
    note = fence_refusal_note(_Res(err), landlock="enforce", exists=lambda p: p == "/data/teacher.pt")
    assert "KERNEL READ ALLOW-LIST" in note and "`/data/teacher.pt` exists on this box" in note
    assert "not a missing or unreadable file" in note and "DECLARATION" in note
    assert fence_refusal_note(_Res(err), landlock="off") == "", "an unfenced run's EACCES is a permissions problem"
    absent = fence_refusal_note(_Res(err), landlock="enforce", exists=lambda p: False)
    assert "is not present on this box" in absent


def test_a_seccomp_eperm_names_the_policy_and_what_it_refuses():
    err = "OSError: [Errno 1] Operation not permitted\n"
    egress = fence_refusal_note(_Res(err), syscall_fence="egress")
    assert "SYSCALL FENCE (`syscall_fence=egress`)" in egress and "loopback included" in egress
    mutators = fence_refusal_note(_Res(err), syscall_fence="mutators")
    assert "`mknod`/`mkfifo`" in mutators and "IPv4/IPv6" not in mutators
    assert fence_refusal_note(_Res(err)) == ""
    assert fence_refusal_note(_Res(""), landlock="enforce", syscall_fence="egress") == ""
    assert fence_refusal_note(None, landlock="enforce") == ""


def test_the_note_reaches_the_triage_intake_and_the_repair_headline():
    # Both readers of a failed eval — the triage judge's `engine_facts` and the Developer's repair
    # headline — are built inside the eval phases; the AST sees the two calls.
    assert eval_attempt_called_names().count("fence_refusal_note") >= 2
