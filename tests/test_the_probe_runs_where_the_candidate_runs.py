"""The Developer's probe and environment inspector answer about the TASK's environment.

Measured 2026-09-23 on a MiniOneRec inference run. The eval ran on a conda env with transformers 5.7.0;
the engine's venv had no transformers at all. A node wrote `cache.key_cache` -- an attribute
transformers 5 removed -- behind a fallback that printed one line, and the path switched itself off at
eval warmup. The node scored 1.004 with every list byte-identical to the parent: the unchanged tree,
measured again, recorded as an idea that does not help.

A probe could not have caught it, for four independent reasons, each of which is a test below:

  1. the probe ran on `sys.executable`, where `import transformers` is ModuleNotFoundError;
  2. on the task's interpreter `import transformers` still died, because `filelock` makes a temp
     directory AT IMPORT and the probe could write nowhere;
  3. the task's assets were a `data:` mount beside the source tree, and `fence_inputs` drops every
     allow entry outside the editable roots -- so the kernel read rung never granted them;
  4. `-P` does not exist before CPython 3.11, and that env is 3.10.

`env_inspect` had the first defect too: `pkg_info("transformers")` said "(not installed)".
"""
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from looplab.adapters.repo_task import RepoTask
from looplab.runtime import landlock
from looplab.tools import dev_probe
from looplab.tools.dev_probe import DevProbeTools, render_launcher
from looplab.tools.env_inspect import EnvInspectTools

_NO_LANDLOCK = landlock.unavailable_reason()
needs_landlock = pytest.mark.skipif(bool(_NO_LANDLOCK),
                                    reason=f"the probe's kernel rungs are unavailable: {_NO_LANDLOCK}")
posix_only = pytest.mark.skipif(os.name != "posix", reason="interpreter stand-ins are shell scripts")


def _another_path_to_python(tmp_path: Path) -> str:
    """A path to a real interpreter that is NOT `sys.executable`. The probe compares WRITTEN paths
    (two venvs over one base binary differ in exactly the way that matters), so a symlink is a
    different interpreter to it -- and needs nothing installed beyond the stdlib the launcher uses."""
    link = tmp_path / "taskenv" / "bin" / "python3"
    link.parent.mkdir(parents=True)
    link.symlink_to(os.path.realpath(sys.executable))
    return str(link)


def _fake_interpreter(tmp_path: Path, body: str) -> str:
    """A shell script standing in for an interpreter, for the paths where only its ANSWER matters."""
    script = tmp_path / "fakeenv" / "bin" / "python3"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(script)


# ------------------------------------------------------------ the task names its own interpreter

def _scorer(tmp_path: Path) -> str:
    """The host scorer's program must exist and live outside the editable tree."""
    script = tmp_path / "harness" / "score.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("print('{\"metric\": 1}')\n")
    return str(script)


def test_the_task_interpreter_is_declared_or_read_off_the_task_s_own_commands(tmp_path):
    host = {"command": ["/opt/envs/reco/bin/python", _scorer(tmp_path)]}
    repo = tmp_path / "repo"
    repo.mkdir()
    # Derived from the host scorer when nothing more specific names one.
    assert RepoTask(goal="g", editable_path=str(repo),
                    eval={"host_scorer": host}).task_python() == "/opt/envs/reco/bin/python"
    # A stage is more candidate-specific than the host scorer, so it wins.
    staged = {"stages": [{"name": "train", "command": ["/opt/envs/train/bin/python3.10", "t.py"]}],
              "host_scorer": host}
    assert RepoTask(goal="g", editable_path=str(repo),
                    eval=staged).task_python() == "/opt/envs/train/bin/python3.10"
    # The operator's declaration beats every derivation.
    declared = dict(staged, python="/opt/envs/declared/bin/python")
    assert RepoTask(goal="g", editable_path=str(repo),
                    eval=declared).task_python() == "/opt/envs/declared/bin/python"
    # Something that is not an interpreter is not one, however it is spelled.
    assert RepoTask(goal="g", editable_path=str(repo),
                    eval={"command": ["/usr/bin/make", "eval"]}).task_python() == ""


def test_a_bare_python_is_resolved_on_the_task_s_path_and_never_on_the_engine_s(tmp_path):
    envbin = tmp_path / "env" / "bin"
    envbin.mkdir(parents=True)
    exe = envbin / "python3"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    on_task_path = RepoTask(goal="g", editable_path=str(tmp_path),
                            eval={"command": ["python3", "run.py"], "env": {"PATH": str(envbin)}})
    assert on_task_path.task_python() == str(exe)
    # No PATH declared: resolving `python3` on the ENGINE's PATH returns the engine's interpreter,
    # which is the very answer this exists to replace. Nothing is derived.
    assert RepoTask(goal="g", editable_path=str(tmp_path),
                    eval={"command": ["python3", "run.py"]}).task_python() == ""


def test_a_relative_declared_interpreter_is_refused_at_admission(tmp_path):
    with pytest.raises(ValueError, match="absolute"):
        RepoTask(goal="g", editable_path=str(tmp_path), eval={"python": "bin/python"})


def test_the_repo_spec_carries_it_to_the_developer(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    task = RepoTask(goal="g", editable_path=str(repo),
                    eval={"host_scorer": {"command": ["/opt/envs/reco/bin/python", _scorer(tmp_path)]}})
    assert task.repo_spec()["task_python"] == "/opt/envs/reco/bin/python"


# ------------------------------------------------------------ 1. the probe runs there

@needs_landlock
def test_the_probe_runs_on_the_task_s_interpreter(tmp_path):
    python = _another_path_to_python(tmp_path)
    out = DevProbeTools({"task_python": python}, timeout_s=60).execute(
        "run_probe", {"code": "import sys; print(sys.executable)"})
    assert "exit=0" in out, out
    assert python in out.split("stdout:", 1)[1], out
    assert "ran on the task's interpreter" in out


@needs_landlock
def test_a_missing_task_interpreter_is_said_not_silently_swapped(tmp_path):
    out = DevProbeTools({"task_python": str(tmp_path / "gone" / "python")}, timeout_s=60).execute(
        "run_probe", {"code": "print('hi')"})
    assert "exit=0" in out
    assert "does not exist" in out and "ENGINE" in out


@needs_landlock
def test_no_task_interpreter_keeps_the_probe_where_it_always_was():
    out = DevProbeTools({}, timeout_s=60).execute(
        "run_probe", {"code": "import sys; print(sys.executable)"})
    assert sys.executable in out and "task's interpreter" not in out


# ------------------------------------------------------------ 2. an import may make a temp file

@needs_landlock
def test_a_probe_can_run_code_that_makes_a_temp_file_and_cleans_it_up():
    """`filelock`'s import-time check, in miniature: a TemporaryDirectory, a file with bytes in it,
    and the descriptor-relative `rmtree` a TemporaryDirectory cleans up with."""
    out = DevProbeTools({}, timeout_s=60).execute("run_probe", {"code": (
        "import os, tempfile\n"
        "with tempfile.TemporaryDirectory() as d:\n"
        "    p = os.path.join(d, 'f.txt')\n"
        "    open(p, 'w').write('x' * 4096)\n"
        "    os.symlink(p, os.path.join(d, 'link'))\n"
        "    print('size', os.path.getsize(p))\n"
        "print('cleaned', not os.path.exists(d))\n")})
    assert "exit=0" in out, out
    assert "size 4096" in out and "cleaned True" in out


@needs_landlock
def test_the_scratch_is_the_only_place_that_became_writable(tmp_path):
    beside = tmp_path / "beside.txt"
    out = DevProbeTools({}, timeout_s=60).execute("run_probe", {"code": (
        "import os, tempfile\n"
        f"for p in ({str(beside)!r}, os.path.join(os.getcwd(), 'replica.txt'),\n"
        "          os.path.join(os.path.dirname(tempfile.gettempdir()), 'sibling.txt')):\n"
        "    try:\n"
        "        open(p, 'w').write('x')\n"
        "        print('WROTE', p)\n"
        "    except BaseException as e:\n"
        "        print('refused', type(e).__name__)\n")})
    assert "WROTE" not in out, out
    assert out.count("refused") == 3
    assert not beside.exists()


def test_without_a_scratch_the_launcher_is_the_historical_one():
    src = render_launcher("/p.py")
    assert "_SCRATCH = None" in src
    assert "_looplab_no_mutation_ruleset(_SCRATCH)" in src


# ------------------------------------------------------------ 3. the task's assets are readable

@needs_landlock
def test_a_data_mount_beside_the_source_tree_is_readable_and_its_neighbours_are_not(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "model.py").write_text("x = 1\n")
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "catalog.txt").write_text("item-1\nitem-2\n")
    withheld = tmp_path / "scoring"
    withheld.mkdir()
    (withheld / "reference.json").write_text('{"answers": "SECRET"}')
    spec = {"editables": [{"name": ".", "path": str(src), "surface": ["**"]}],
            "data": {"assets": {"path": str(assets), "mount": True}}}
    tools = DevProbeTools(spec, timeout_s=60)
    got = tools.execute("run_probe", {"code": f"print(open({str(assets / 'catalog.txt')!r}).read())"})
    assert "exit=0" in got and "item-2" in got, got
    denied = tools.execute("run_probe",
                           {"code": f"print(open({str(withheld / 'reference.json')!r}).read())"})
    assert "SECRET" not in denied and "exit=0" not in denied


# ------------------------------------------------------------ 4. interpreters older than -P

@posix_only
def test_minus_p_is_passed_only_to_interpreters_that_have_it(tmp_path, monkeypatch):
    monkeypatch.setattr(dev_probe, "_SAFE_PATH_FLAG_CACHE", {})
    old = _fake_interpreter(tmp_path / "a", "echo 3 10")
    new = _fake_interpreter(tmp_path / "b", "echo 3 12")
    mute = _fake_interpreter(tmp_path / "c", "exit 1")
    assert dev_probe._safe_path_flag(old) == []
    assert dev_probe._safe_path_flag(new) == ["-P"]
    assert dev_probe._safe_path_flag(mute) == []


def test_the_launcher_removes_its_own_directory_where_minus_p_did_not():
    src = render_launcher("/p.py")
    assert "del sys.path[0]" in src
    assert src.index("del sys.path[0]") < src.index("_REFUSAL =")


# ------------------------------------------------------------ the grader fence follows the probe

@posix_only
def test_a_grader_fence_that_cannot_be_rebuilt_in_the_task_interpreter_refuses_the_probe(tmp_path):
    mute = _fake_interpreter(tmp_path, "exit 1")
    tools = DevProbeTools({"task_python": mute}, timeout_s=60,
                          protect_roots={"graderpkg": (str(tmp_path / "grader"),)})
    out = tools.execute("run_probe", {"code": "print('should not run')"})
    assert "should not run" not in out
    assert "refused" in out and "graderpkg" in out and "could not say where" in out


# ------------------------------------------------------------ env_inspect answers there too

@posix_only
def test_pkg_info_answers_from_the_task_s_interpreter(tmp_path):
    fake = _fake_interpreter(tmp_path, 'echo "fakepkg 9.9"; echo "location: /env/fakepkg"')
    tools = EnvInspectTools(task_python=fake)
    assert tools.execute("pkg_info", {"name": "fakepkg"}).startswith("fakepkg 9.9")


@posix_only
def test_source_reading_tools_say_which_interpreter_they_read(tmp_path):
    fake = _fake_interpreter(tmp_path, "echo unused")
    out = EnvInspectTools(task_python=fake).execute("py_api", {"target": "json.dumps"})
    assert out.startswith("(NOTE: this answer comes from the ENGINE's interpreter")
    assert "run_probe" in out
    # And not a word of it when the task runs on the engine's own interpreter.
    same = EnvInspectTools(task_python=sys.executable).execute("py_api", {"target": "json.dumps"})
    assert "NOTE" not in same


# ------------------------------------------------------------ what `import flashinfer` needed
#
# Measured 2026-09-23, the first plan of the first node built with the fixes above: the probe ran on
# the task's interpreter and read transformers' source, but `./assets` did not exist in its cwd and
# `import flashinfer` -- the attention library the whole task is about -- died four ways in a row: a
# `makedirs(exist_ok=True)` of a directory already there, a JIT log opened for writing under `~`,
# `/dev/null` opened read-write by `subprocess`, and `ctypes.util.find_library` running `ldconfig -p`
# and then `gcc`. Each is answered narrowly below; the rule "a probe changes nothing" is unchanged.

@needs_landlock
def test_a_data_mount_is_linked_into_the_cwd_under_the_name_the_eval_uses(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    assets = tmp_path / "assets_store"
    assets.mkdir()
    (assets / "catalog.txt").write_text("item-1\n")
    spec = {"editables": [{"name": ".", "path": str(src), "surface": ["**"]}],
            "data": {"assets": {"path": str(assets), "mount": True}}}
    out = DevProbeTools(spec, timeout_s=60).execute(
        "run_probe", {"code": "print(open('assets/catalog.txt').read())"})
    assert "exit=0" in out and "item-1" in out, out
    # Linked by the workspace seed where the repo is seeded, by `_link_mounts` where it is not.
    assert "seeded here as its evaluation sees it" in out or "data mounts linked here" in out
    denied = DevProbeTools(spec, timeout_s=60).execute(
        "run_probe", {"code": "open('assets/new.txt', 'w')"})
    assert "exit=0" not in denied and not (assets / "new.txt").exists()


@needs_landlock
def test_making_a_directory_that_already_exists_is_not_a_change(tmp_path):
    there = tmp_path / "cache"
    there.mkdir()
    out = DevProbeTools({}, timeout_s=60).execute("run_probe", {"code": (
        "import os\n"
        f"os.makedirs({str(there)!r}, exist_ok=True)\n"
        "print('existing: ok')\n"
        "try:\n"
        f"    os.makedirs({str(tmp_path / 'fresh')!r}, exist_ok=True)\n"
        "    print('CREATED')\n"
        "except BaseException as e:\n"
        "    print('fresh:', type(e).__name__)\n")})
    assert "existing: ok" in out and "CREATED" not in out, out
    assert not (tmp_path / "fresh").exists()


@needs_landlock
def test_dev_null_may_be_opened_for_writing_and_nothing_else_may():
    out = DevProbeTools({}, timeout_s=60).execute("run_probe", {"code": (
        "import os\n"
        "open(os.devnull, 'w').write('x'); os.close(os.open(os.devnull, os.O_RDWR)); print('null ok')\n")})
    assert "null ok" in out, out


@needs_landlock
def test_only_the_ldconfig_cache_query_may_run_and_ctypes_compiles_nothing():
    out = DevProbeTools({}, timeout_s=60).execute("run_probe", {"code": (
        "import ctypes.util, shutil, subprocess\n"
        "print('gcc fallback:', ctypes.util._findLib_gcc('m'))\n"
        "print('find_library ran:', True if ctypes.util.find_library('c') or True else False)\n"
        "for cmd in (['/bin/cat', '/etc/hostname'], ['ldconfig', '-v']):\n"
        "    try:\n"
        "        subprocess.run(cmd, capture_output=True); print('RAN', cmd[0])\n"
        "    except BaseException as e:\n"
        "        print('refused', cmd[0], type(e).__name__)\n")})
    assert "gcc fallback: None" in out and "find_library ran: True" in out, out
    assert "RAN" not in out and out.count("refused") == 2


def test_the_ldconfig_exception_is_exactly_one_argument_list():
    src = render_launcher("/p.py")
    assert 'argv[1] == "-p"' in src and "len(argv) == 2" in src


@needs_landlock
def test_the_declared_probe_env_expands_scratch_and_cannot_override_safety():
    spec = {"probe_env": {"MY_CACHE": "{scratch}/lib", "CUDA_VISIBLE_DEVICES": "0"}}
    out = DevProbeTools(spec, timeout_s=60).execute("run_probe", {"code": (
        "import os, tempfile\n"
        "print('cache under scratch:', os.environ['MY_CACHE'].startswith(tempfile.gettempdir()))\n"
        "print('gpus:', repr(os.environ.get('CUDA_VISIBLE_DEVICES')))\n")})
    assert "cache under scratch: True" in out, out
    assert "gpus: ''" in out


def test_probe_env_is_task_authored_validated_and_carried(tmp_path):
    run = ["/opt/envs/reco/bin/python", "run.py"]
    task = RepoTask(goal="g", editable_path=str(tmp_path),
                    eval={"command": run, "probe_env": {"FLASHINFER_WORKSPACE_BASE": "{scratch}"}})
    assert task.repo_spec()["probe_env"] == {"FLASHINFER_WORKSPACE_BASE": "{scratch}"}
    with pytest.raises(ValueError):
        RepoTask(goal="g", editable_path=str(tmp_path),
                 eval={"command": run, "probe_env": {"OPENAI_API_KEY": "sk-x"}})


# ------------------------------------------------------------ the repo's own code, as the eval sees it
#
# Measured 2026-09-23: two of thirty MiniOneRec probes died on `No module named 'service'` -- the
# Developer wanted to run the repo's own code on CPU against its change, and the replica held only
# what the node had staged.

class _Staged:
    def __init__(self, files=None, deleted=None):
        self.files = dict(files or {})
        self.deleted = list(deleted or [])


def _repo(tmp_path):
    src = tmp_path / "repo"
    (src / "pkg").mkdir(parents=True)
    (src / "pkg" / "__init__.py").write_text("")
    (src / "pkg" / "core.py").write_text("VALUE = 'from the repo'\n")
    (src / "pkg" / "gone.py").write_text("X = 1\n")
    return src


@needs_landlock
def test_the_repo_s_own_modules_import_with_nothing_staged(tmp_path):
    src = _repo(tmp_path)
    spec = {"editables": [{"name": ".", "path": str(src), "surface": ["**"]}]}
    out = DevProbeTools(spec, timeout_s=60).execute(
        "run_probe", {"code": "from pkg.core import VALUE; print(VALUE)"})
    assert "exit=0" in out and "from the repo" in out, out
    assert "seeded here as its evaluation sees it" in out


@needs_landlock
def test_the_staged_overlay_wins_over_the_seed_and_deletions_apply(tmp_path):
    src = _repo(tmp_path)
    spec = {"editables": [{"name": ".", "path": str(src), "surface": ["**"]}]}
    staged = _Staged({"pkg/core.py": "VALUE = 'staged'\n"}, deleted=["pkg/gone.py"])
    out = DevProbeTools(spec, timeout_s=60, staged=staged).execute("run_probe", {"code": (
        "import os\nfrom pkg.core import VALUE\nprint(VALUE, os.path.exists('pkg/gone.py'))\n")})
    assert "staged False" in out, out


@needs_landlock
def test_the_original_tree_stays_fenced_when_its_copy_is_seeded(tmp_path):
    src = _repo(tmp_path)
    spec = {"editables": [{"name": ".", "path": str(src), "surface": ["**"]}]}
    out = DevProbeTools(spec, timeout_s=60).execute(
        "run_probe", {"code": f"print(open({str(src / 'pkg' / 'core.py')!r}).read())"})
    assert "from the repo" not in out and "exit=0" not in out


@needs_landlock
def test_a_repo_over_the_cap_is_not_copied_and_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(dev_probe, "_MAX_SEED_BYTES", 1)
    src = _repo(tmp_path)
    spec = {"editables": [{"name": ".", "path": str(src), "surface": ["**"]}]}
    out = DevProbeTools(spec, timeout_s=60).execute(
        "run_probe", {"code": "import os; print(os.path.exists('pkg'))"})
    assert "NOT seeded" in out and "False" in out, out


def test_sizing_a_tree_stops_as_soon_as_it_is_over_the_cap(tmp_path):
    """The seed only asks "does it fit". Walking a tree to its end first cost 952 s on a root too
    broad to fence, which the probe then refused anyway."""
    for i in range(5):
        (tmp_path / f"f{i}.bin").write_bytes(b"x" * 10)
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "huge.pack").write_bytes(b"x" * 1000)       # ignored, as the seed ignores it
    assert dev_probe._bytes_left_after(tmp_path, 100) == 50
    assert dev_probe._bytes_left_after(tmp_path, 15) == -5          # stopped at the second file
    assert dev_probe._bytes_left_after(tmp_path / "missing", 100) is None
