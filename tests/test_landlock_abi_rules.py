"""The Landlock rulesets follow the KERNEL they run on (review 2026-09-22, RTA-07).

`runtime/landlock.py` handled exactly the ABI-1 mutation set, on the recorded ground that "this box
is ABI 2" and an unsupported bit in `handled_access_fs` is EINVAL for the whole ruleset. Both halves
of that were true on 2026-08-13 and the first stopped being true: the box this review ran on answers
ABI 7, and `LANDLOCK_ACCESS_FS_TRUNCATE` (ABI 3) was still unhandled — so `truncate(2)` was policed by
NOTHING, on any path, under `landlock=enforce` (the run's own `events.jsonl`, which the allow-list
grants READ) and under the Developer probe's no-mutation ruleset, whose whole sentence is "it cannot
write anywhere". A handled-but-ungranted bit is a denial; an unhandled bit is not a rule at all.

Two tiers, like the rest of the Landlock tests:
  * the MASK CONSTRUCTION per ABI is a pure function and runs everywhere — including the value the
    generated launchers actually embed, read back out of their own source by `ast` so a comment can
    never satisfy it;
  * the refusal itself is driven through a real child process on a kernel that has ABI >= 3, and
    skips cleanly anywhere else (a container whose `landlock_create_ruleset` answers ENOSYS, or an
    ABI-2 kernel on which the bit cannot be handled at all).
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from looplab.runtime import landlock, read_allowlist

_NO_LANDLOCK = landlock.unavailable_reason()
_ABI = landlock.abi_version()
_NO_TRUNCATE_RIGHT = (_NO_LANDLOCK or (None if (_ABI or 0) >= 3 else
                      f"Landlock ABI {_ABI} predates LANDLOCK_ACCESS_FS_TRUNCATE (ABI 3)"))
_WORKTREE = str(Path(__file__).resolve().parents[1])


# ------------------------------------------------------------------ the masks, on every kernel

@pytest.mark.parametrize("abi", [None, 1, 2])
def test_a_pre_abi3_kernel_is_never_asked_to_handle_the_truncate_bit(abi):
    """EINVAL for the whole ruleset is the cost of handling a bit the kernel does not know, so below
    ABI 3 every mask is exactly the historical ABI-1 set."""
    read, write, handled = landlock.allowlist_masks(abi)
    assert not (read | write | handled) & landlock.FS_TRUNCATE
    assert not landlock.no_mutation_handled(abi) & landlock.FS_TRUNCATE
    assert handled == write                       # handled \ granted is exactly what a rule refuses


@pytest.mark.parametrize("abi", [3, 4, 7])
def test_from_abi3_truncate_is_handled_and_granted_only_where_writes_are(abi):
    read, write, handled = landlock.allowlist_masks(abi)
    assert handled & landlock.FS_TRUNCATE          # policed on every path...
    assert write & landlock.FS_TRUNCATE            # ...granted where a WRITE is (the workdir, /tmp)
    assert not read & landlock.FS_TRUNCATE         # ...and refused under a READ grant (the run dir)
    # The no-mutation rung grants nothing, so handling it IS refusing it — and it still handles no
    # read bit, which is what makes an empty ruleset correct for the probe.
    nm = landlock.no_mutation_handled(abi)
    assert nm & landlock.FS_TRUNCATE
    assert not nm & (landlock.FS_READ_FILE | landlock.FS_READ_DIR | landlock.FS_EXECUTE)


def _assigned_tuple(source: str, name: str):
    """The literal value bound to `name` by a tuple assignment in generated `source`."""
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Tuple):
            names = [t.id for t in node.targets[0].elts if isinstance(t, ast.Name)]
            if name in names:
                return dict(zip(names, ast.literal_eval(node.value)))[name]
    raise AssertionError(f"{name} is not assigned in the generated source")


def _handled_keyword(source: str) -> int:
    """The `handled_access_fs=` value the generated no-mutation rung passes the kernel."""
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.keyword) and node.arg == "handled_access_fs":
            return ast.literal_eval(node.value)
    raise AssertionError("no handled_access_fs= in the generated source")


@pytest.mark.parametrize("abi", [2, 3, 7])
def test_the_generated_launchers_embed_the_mask_for_the_abi_they_are_rendered_for(abi):
    """The launchers cannot import this package, so the masks are TEMPLATED in — and a template is
    only as right as the value it received. Read back from the code the child will execute."""
    read, write, handled = landlock.allowlist_masks(abi)
    src = landlock.launcher_source(abi=abi)
    assert _assigned_tuple(src, "_READ") == read
    assert _assigned_tuple(src, "_WRITE") == write
    assert _assigned_tuple(src, "_HANDLED") == handled
    assert _handled_keyword(landlock.no_mutation_source(abi=abi)) == \
        landlock.no_mutation_handled(abi)


def test_the_default_render_is_for_the_kernel_it_runs_on():
    assert _assigned_tuple(landlock.launcher_source(), "_HANDLED") == \
        landlock.allowlist_masks(_ABI)[2]
    assert _handled_keyword(landlock.no_mutation_source()) == landlock.no_mutation_handled(_ABI)


# --------------------------------------------------------- the refusal, on a kernel that has it

def _contains(parent: str, child: str) -> bool:
    parent, child = os.path.realpath(parent), os.path.realpath(child)
    return child == parent or child.startswith(parent.rstrip(os.sep) + os.sep)


def _eval_allow(tmp_path: Path, wd: Path, run: Path) -> list:
    """The derived eval allow-list MINUS every tier that also contains the test's tmp dir.

    pytest's tmp root lives under `/tmp`, which the derivation grants readwrite as a machine tier —
    so without this the run directory would be writable through `/tmp` and the test would be about
    `/tmp`, not about the run-dir rule it names."""
    return [(p, m) for p, m in read_allowlist.derive(workdir=str(wd), run_dir=str(run))
            if p in (os.path.realpath(str(wd)), os.path.realpath(str(run)))
            or not _contains(p, str(tmp_path))]


_TRUNCATE_PROG = textwrap.dedent("""\
    import ctypes, os, sys
    libc = ctypes.CDLL(None, use_errno=True)
    libc.truncate.argtypes = [ctypes.c_char_p, ctypes.c_long]
    ctypes.set_errno(0)
    rc = libc.truncate(sys.argv[1].encode(), 0)
    print("record", rc, ctypes.get_errno())
    # The node's OWN workdir must stay fully writable: O_TRUNC through open() and truncate(2).
    with open("scratch.txt", "w") as fh:
        fh.write("0123456789")
    with open("scratch.txt", "w") as fh:
        fh.write("ab")
    os.truncate("scratch.txt", 1)
    print("workdir", os.path.getsize("scratch.txt"))
    """)


@pytest.mark.skipif(_NO_TRUNCATE_RIGHT is not None, reason=str(_NO_TRUNCATE_RIGHT))
def test_the_eval_launcher_refuses_truncating_the_run_record_it_only_grants_read(tmp_path):
    """The reviewer's reproduction, as a guard: `coreutils truncate -s 0 <run>/events.jsonl` from
    inside a `landlock=enforce` eval emptied the run's record — a path the allow-list grants READ.
    libc `truncate(2)` is the syscall that command makes, and CPython's audit hook never sees it."""
    run = tmp_path / "run"
    wd = run / "nodes" / "node_0"
    wd.mkdir(parents=True)
    events = run / "events.jsonl"
    events.write_text('{"seq":1}\n' * 100, encoding="utf-8")
    size = events.stat().st_size
    (wd / "p.py").write_text(_TRUNCATE_PROG, encoding="utf-8")
    argv = landlock.launch_argv(sys.executable, landlock.format_env(_eval_allow(tmp_path, wd, run)),
                                [sys.executable, "p.py", str(events)])
    out = subprocess.run(argv, capture_output=True, text=True, cwd=str(wd), timeout=60)
    assert out.returncode == 0, out.stderr
    assert "record -1 13" in out.stdout, out.stdout          # EACCES from the kernel
    assert events.stat().st_size == size                     # ...and the record is intact
    assert "workdir 1" in out.stdout, out.stdout             # own workdir still writable


@pytest.mark.skipif(_NO_TRUNCATE_RIGHT is not None, reason=str(_NO_TRUNCATE_RIGHT))
def test_the_check_command_ruleset_refuses_truncate_like_the_launcher_does(tmp_path):
    """`build_ruleset`/`apply` are what `looplab landlock-check` runs, and the launcher is what the
    eval runs — two spellings of one ruleset, so both must handle the bit on this kernel."""
    run = tmp_path / "run"
    wd = run / "nodes" / "node_0"
    wd.mkdir(parents=True)
    victim = run / "events.jsonl"
    victim.write_bytes(b"x" * 4096)
    allow = _eval_allow(tmp_path, wd, run)
    prog = (f"import sys; sys.path.insert(0, {_WORKTREE!r})\n"
            "from looplab.runtime import landlock\n"
            f"landlock.apply({allow!r})\n" + _TRUNCATE_PROG)
    out = subprocess.run([sys.executable, "-c", prog, str(victim)], capture_output=True,
                         text=True, cwd=str(wd), timeout=60)
    assert out.returncode == 0, out.stderr
    assert "record -1 13" in out.stdout, out.stdout
    assert victim.stat().st_size == 4096
    assert "workdir 1" in out.stdout, out.stdout


@pytest.mark.skipif(_NO_TRUNCATE_RIGHT is not None, reason=str(_NO_TRUNCATE_RIGHT))
def test_the_no_mutation_rung_refuses_a_native_truncate(tmp_path):
    """The probe's kernel rung promises "no write anywhere". A libc `truncate(2)` raises no audit
    event, so before this the only thing between it and the operator's files was `RLIMIT_FSIZE`,
    which bounds bytes WRITTEN and says nothing about bytes REMOVED."""
    victim = tmp_path / "victim.bin"
    victim.write_bytes(b"y" * 4096)
    prog = landlock.no_mutation_source() + textwrap.dedent(f"""
        print("rung", {landlock.NO_MUTATION_FUNCTION}() or "applied")
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        libc.truncate.argtypes = [ctypes.c_char_p, ctypes.c_long]
        ctypes.set_errno(0)
        print("truncate", libc.truncate({str(victim)!r}.encode(), 0), ctypes.get_errno())
        """)
    out = subprocess.run([sys.executable, "-c", prog], capture_output=True, text=True, timeout=60)
    assert "rung applied" in out.stdout, (out.stdout, out.stderr)
    assert "truncate -1 13" in out.stdout, out.stdout
    assert victim.stat().st_size == 4096
