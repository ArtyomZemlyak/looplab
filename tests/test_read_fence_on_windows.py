"""The read fence's Windows branches, driven on this POSIX box.

MEASURED on the Windows CI leg (GitHub Actions run 35785582444, review 2026-09-22, WIN-FENCE): the
READ rung held there (`_resolve` has had an `_NT` branch since the drive-letter bypass) while every
MUTATION of the operator's tree ran THROUGH — remove, rename, rmdir, link, symlink, truncate, chmod,
utime, and `shutil.rmtree` of the root itself — and the fence could not re-render or repair itself
over its own hardened file. Two defects, both on branches no POSIX run executes:

* `_mutation_path` tested "absolute" as "starts with os.sep". A Windows absolute path starts with a
  drive letter, so every one was joined onto the cwd as if relative — a spelling no root prefixes —
  and the mutation rung, the record rung and `_SELF` (all of which read that resolution) answered
  None. `_resolve_links` split a drive-root child at 'C:', which Windows resolves to that drive's
  CURRENT directory.
* `install`/`reassert` rewrite the fence with an atomic replace, which on POSIX ignores the 0444
  destination; on Windows the mode is the read-only attribute and the replace is refused.

The generated template is executed here with the host's `os` swapped for one whose path module IS
`ntpath` and whose name is "nt" — the exact inputs its `_NT` branches read — and the replace/unlink
rules of Windows reproduced by `tests/_windows_emulation.py`-style doubles.
"""
from __future__ import annotations

import builtins
import errno
import ntpath
import os
import stat
import types

import pytest

from looplab.runtime import read_fence
from _posix_gates import DIRECTORY_OPS_IGNORE_READONLY


def _windows_os(cwd: str) -> types.ModuleType:
    fake = types.ModuleType("os")
    fake.__dict__.update(os.__dict__)
    fake.path = ntpath
    fake.sep, fake.altsep, fake.name = "\\", "/", "nt"
    fake.getcwd = lambda: cwd
    return fake


def _fence_as_windows(monkeypatch, roots, *, cwd="C:\\work"):
    """The generated fence's namespace as a Windows interpreter would build it (probe seam, so no
    audit hook is installed): `_NT` baked True and `os`/`os.path` answering like Windows."""
    src = read_fence.render(roots, (), policy="deny", log="", run="")
    # `_NT` is baked from `os.name` at render time: False here, and ALREADY True on a real Windows
    # host, where there is nothing to flip -- this precondition failed every case there (CI run
    # 35804658308, review 2026-09-22 round 2) before a single assertion about the fence ran.
    assert src.count("_NT = False") + src.count("_NT = True") == 1, (
        "the template no longer bakes _NT as expected")
    src = src.replace("_NT = False", "_NT = True")
    fake_os = _windows_os(cwd)
    real_import = builtins.__import__

    def _import(name, *args, **kwargs):
        return fake_os if name == "os" else real_import(name, *args, **kwargs)

    scope = dict(vars(builtins))
    scope["__import__"] = _import
    ns = {"__name__": read_fence._PROBE_NAME, "__builtins__": scope}
    exec(compile(src, "sitecustomize.py", "exec"), ns)
    # ntpath's own `abspath` reads the REAL cwd for a relative name; answer it like Windows too.
    monkeypatch.setattr(os, "getcwd", lambda: cwd)
    return ns


ROOT = "C:\\src\\repo\\"


def test_an_absolute_windows_path_under_a_root_is_a_fenced_mutation(monkeypatch):
    ns = _fence_as_windows(monkeypatch, (ROOT,))
    target = "C:\\src\\repo\\experiments\\final\\model.safetensors"
    assert ns["_mutation_path"](target, None) == target
    assert ns["_fenced_target"](target, None) == target, (
        "a drive-letter path was read as relative: the mutation rung is inert on Windows")
    assert ns["_fenced_target"]("C:/src/repo/experiments/x.bin", None) is not None, (
        "Windows accepts forward slashes, and so must the rule")


def test_a_path_outside_every_root_is_not_fenced(monkeypatch):
    ns = _fence_as_windows(monkeypatch, (ROOT,))
    assert ns["_fenced_target"]("C:\\src\\repository\\x.txt", None) is None
    assert ns["_fenced_target"]("D:\\src\\repo\\x.txt", None) is None


def test_a_relative_name_is_judged_from_the_cwd_it_will_act_in(monkeypatch):
    ns = _fence_as_windows(monkeypatch, (ROOT,), cwd="C:\\src\\repo\\experiments")
    assert ns["_fenced_target"]("model.safetensors", None) == \
        "C:\\src\\repo\\experiments\\model.safetensors"
    assert ns["_fenced_target"]("..\\..\\other\\x", None) is None


def _refused(hook, event, args) -> bool:
    try:
        hook(event, args)
    except Exception as exc:                       # the fence's own refusal type, by name
        assert type(exc).__name__ == "LoopLabSourceReadRefused", exc
        return True
    return False


def test_the_fence_protects_its_own_file_on_windows(monkeypatch):
    """`_SELF` reads the same resolution: on Windows a child could delete the generated fence and
    unfence every later process of the run (STAGE2 ESCAPED unlink/chmod on the CI leg)."""
    ns = _fence_as_windows(monkeypatch, (ROOT,))
    ns["_SELF"] = ("C:\\run\\.looplab-fence\\",)
    assert _refused(ns["_hook"], "os.remove", ("C:\\run\\.looplab-fence\\sitecustomize.py", -1))
    assert _refused(ns["_hook"], "os.chmod", ("C:\\run\\.looplab-fence\\sitecustomize.py", 0o666,
                                               -1))


def test_the_run_record_is_not_writable_on_windows(monkeypatch):
    """The record rung, both ways in: a mutation event and a WRITE-mode `open` (the forged
    `node_evaluated` vector) — and the launch's own workdir stays writable."""
    ns = _fence_as_windows(monkeypatch, (ROOT,))
    ns["_RECORD"] = "C:\\run\\"
    ns["_WRITABLE"] = ("C:\\run\\.looplab-fence\\", "C:\\run\\nodes\\node_4\\")
    hook = ns["_hook"]
    assert _refused(hook, "os.remove", ("C:\\run\\events.jsonl", -1))
    assert _refused(hook, "open", ("C:\\run\\events.jsonl", "a",
                                   os.O_WRONLY | os.O_APPEND | os.O_CREAT))
    assert not _refused(hook, "os.remove", ("C:\\run\\nodes\\node_4\\scratch.txt", -1))
    assert not _refused(hook, "open", ("C:\\run\\events.jsonl", "r", os.O_RDONLY)), (
        "reading the record is legal")


# ------------------------------------------------------------------- rewriting a hardened fence

def _refuse_replacing_readonly(monkeypatch):
    """`MoveFileEx(..., REPLACE_EXISTING)` refuses a READ-ONLY destination on Windows."""
    real_replace = os.replace
    refused = []

    def _replace(src, dst, *args, **kwargs):
        try:
            info = os.lstat(dst)
        except FileNotFoundError:
            info = None
        if info is not None and not info.st_mode & stat.S_IWUSR:
            refused.append(dst)
            raise PermissionError(errno.EACCES, "Access is denied (emulated read-only)", dst)
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", _replace)
    return refused


def _windows_named(monkeypatch):
    """`read_fence`'s own view of the platform: its `os` answers "nt", everything else is real."""
    fake = types.ModuleType("os")
    fake.__dict__.update(os.__dict__)
    fake.name = "nt"
    monkeypatch.setattr(read_fence, "os", fake)


def _world(tmp_path):
    src = tmp_path / "repo"
    (src / "experiments").mkdir(parents=True)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    return src, run_dir


def test_a_changed_fence_is_rewritten_over_its_own_hardened_file(tmp_path, monkeypatch):
    src, run_dir = _world(tmp_path)
    first = read_fence.install(run_dir, roots=(str(src) + os.sep,), allow=(), policy="deny")
    target = os.path.join(first, "sitecustomize.py")
    assert not os.stat(target).st_mode & 0o222, "precondition: the fence is hardened"
    refused = _refuse_replacing_readonly(monkeypatch)
    _windows_named(monkeypatch)

    again = read_fence.install(run_dir, roots=(str(src) + os.sep,), allow=(), policy="warn")

    assert again == first
    assert "'warn'" in open(target, encoding="utf-8").read()
    assert not os.stat(target).st_mode & 0o222, "the rewritten fence must be hardened again"
    assert not refused, f"the replace hit the read-only destination: {refused}"


def test_a_tampered_fence_is_repaired_over_its_own_hardened_file(tmp_path, monkeypatch):
    src, run_dir = _world(tmp_path)
    fence_dir = read_fence.install(run_dir, roots=(str(src) + os.sep,), allow=(), policy="deny")
    target = os.path.join(fence_dir, "sitecustomize.py")
    os.chmod(target, 0o644)
    with open(target, "a", encoding="utf-8") as fh:
        fh.write("\n# tampered\n")
    os.chmod(target, 0o444)                          # the tamperer put the bit back
    refused = _refuse_replacing_readonly(monkeypatch)
    _windows_named(monkeypatch)

    note = read_fence.reassert(fence_dir)

    assert note is not None and "repaired before this launch" in note, note
    assert "# tampered" not in open(target, encoding="utf-8").read()
    assert not refused


def test_a_top_level_directory_on_a_drive_is_as_broad_as_one_on_the_root(monkeypatch):
    """`_too_broad` counted the drive as a component, so on Windows `C:\\Users` and `C:\\Program Files`
    -- where a per-user or a system Python lives, the analogue of `/home` and `/usr` -- were two parts
    and narrow enough to fence. Measured on the CI leg as `/usr` resolving to `D:\\usr`: the probe that
    `test_dev_probe` requires to refuse ran instead (run 35785582444)."""
    monkeypatch.setattr(read_fence, "os", _windows_os("C:\\work"))
    monkeypatch.setenv("USERPROFILE", "C:\\Users\\runner")
    for broad in ("C:\\", "C:\\Users\\", "C:\\Program Files\\", "D:\\usr\\", "C:\\Users\\runner\\"):
        assert read_fence._too_broad(broad), broad
    for narrow in ("D:\\a\\looplab\\", "C:\\src\\repo\\", "C:\\Users\\runner\\repo\\"):
        assert not read_fence._too_broad(narrow), narrow


@DIRECTORY_OPS_IGNORE_READONLY
def test_posix_leaves_the_hardened_file_to_the_replace(tmp_path, monkeypatch):
    """Off Windows nothing is un-hardened: POSIX replaces a 0444 destination as a directory
    operation, which is what `install`'s own comment has always relied on."""
    src, run_dir = _world(tmp_path)
    fence_dir = read_fence.install(run_dir, roots=(str(src) + os.sep,), allow=(), policy="deny")
    target = os.path.join(fence_dir, "sitecustomize.py")
    chmods = []
    real_chmod = os.chmod
    monkeypatch.setattr(os, "chmod", lambda p, m, *a, **k: (chmods.append(m), real_chmod(p, m))[1])
    read_fence.install(run_dir, roots=(str(src) + os.sep,), allow=(), policy="warn")
    assert chmods == [read_fence.FENCE_FILE_MODE], chmods
