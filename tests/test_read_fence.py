"""The source-tree READ FENCE (`looplab/runtime/read_fence.py`).

Every behavioural test here launches a REAL subprocess through `run_argv` — the same choke point the
engine uses — and asserts on what that process could and could not read. That is deliberate on two
counts. First, the property is "a node's process cannot read the operator's source tree", and only a
real interpreter with the generated `sitecustomize` on its PYTHONPATH can be said to have it.
Second, `sys.addaudithook` is IRREVERSIBLE: installing the fence in the pytest process would fence
the test session itself for the rest of the run. The one in-process test (`test_predicate_*`) execs
the generated source under the probe name, which is the seam that yields the pure predicate WITHOUT
installing a hook.

The headline test is `test_a_source_read_is_refused_with_the_message`, and it is written to fail
without the fence: the same child, launched without the marker, reads the file and prints its
contents. That is the shape of the defect it closes — `runs/rubertlite-dr-unified-v6` node 4 read a
human's checkpoint out of the editable source tree and scored it, and every gate the run had passed.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from looplab.core.config import Settings
from looplab.runtime import read_fence
from looplab.runtime.sandbox import run_argv

# The fixture the whole file shares: an editable SOURCE tree holding the operator's checkpoint, a
# `data:` mount source that lives INSIDE it (the legal read channel, and the one shape a naive
# prefix fence would break), a node workdir, and a "model dir" standing in for an HF cache.
CHECKPOINT = "HUMAN CHECKPOINT FROM 2026-07-18"


def _world(tmp_path):
    src = tmp_path / "vectorizer-unified"
    (src / "experiments" / "baseline" / "final").mkdir(parents=True)
    (src / "experiments" / "baseline" / "final" / "model.safetensors").write_text(CHECKPOINT)
    (src / "corpus").mkdir()
    (src / "corpus" / "queries.jsonl").write_text("mounted data\n")
    sibling = tmp_path / "vectorizer-unified-notes"      # proves the root compare is not a substring
    sibling.mkdir()
    (sibling / "note.txt").write_text("sibling, not fenced\n")
    run_dir = tmp_path / "run"
    wd = run_dir / "nodes" / "node_4"
    wd.mkdir(parents=True)
    (wd / "own.txt").write_text("the node's own copy\n")
    models = tmp_path / "hf-cache"
    models.mkdir()
    (models / "config.json").write_text("{}\n")
    return src, sibling, run_dir, wd, models


def _install(run_dir, src, *, policy="deny", allow=()):
    """Install a fence the way the engine does, via the same input derivation."""
    spec = {"editables": [{"name": ".", "path": str(src)}],
            "data": {name: {"path": str(p)} for name, p in allow}}
    roots, allowed, dropped, swallowed = read_fence.fence_inputs(spec, allow=[str(run_dir)])
    assert not dropped and not swallowed
    return read_fence.install(run_dir, roots=roots, allow=allowed, policy=policy)


def _run(code, wd, fence_dir=None, extra_env=None):
    env = dict(extra_env or {})
    if fence_dir:
        env[read_fence.FENCE_DIR_ENV] = fence_dir
    return run_argv([sys.executable, "-c", textwrap.dedent(code)], str(wd), 60.0, env or None)


# --------------------------------------------------------------------------- the property


def test_a_source_read_is_refused_with_the_message(tmp_path):
    """A real process opening the operator's checkpoint is refused, and the refusal names the fix.

    Fails without the fence: the control half below is the SAME child with no marker, and it reads
    the file. This is exactly node 4's read."""
    src, _sib, run_dir, wd, _models = _world(tmp_path)
    target = src / "experiments" / "baseline" / "final" / "model.safetensors"
    code = f"print(open({str(target)!r}).read())"

    unfenced_rc, unfenced_out, _err, _to = _run(code, wd)
    assert unfenced_rc == 0 and CHECKPOINT in unfenced_out, "control: the read must succeed unfenced"

    fence = _install(run_dir, src)
    rc, out, err, timed_out = _run(code, wd, fence)
    assert rc != 0 and not timed_out
    assert CHECKPOINT not in out
    assert "LoopLabSourceReadRefused" in err
    assert read_fence.REFUSAL_MESSAGE.replace("{path}", str(target)) in err
    # The exact repair instructions, spelled out, because the reader is the repair loop.
    assert "Use a workdir-relative path" in err
    assert "`data:`/`references:` mount" in err and 'seed_mode: "all"' in err


def test_the_refusal_is_not_swallowed_by_an_oserror_handler(tmp_path):
    """`except OSError: <fall back>` is THE shape around a file read. A `PermissionError` would be
    caught by it and the fence would become the silent skip the requirements forbid."""
    src, _sib, run_dir, wd, _models = _world(tmp_path)
    target = src / "experiments" / "baseline" / "final" / "model.safetensors"
    fence = _install(run_dir, src)
    rc, out, err, _to = _run(f"""
        try:
            data = open({str(target)!r}).read()
        except (OSError, IOError):
            print("SWALLOWED — the fence was caught by a routine fallback")
        """, wd, fence)
    assert "SWALLOWED" not in out
    assert rc != 0 and "LoopLabSourceReadRefused" in err


def test_controls_stay_readable(tmp_path):
    """The workdir, a data mount whose source lives INSIDE the fenced root, site-packages, the
    stdlib, a model/HF cache dir and a sibling directory with a shared name prefix all stay open."""
    src, sibling, run_dir, wd, models = _world(tmp_path)
    mount = wd / "corpus"                       # how `link_input` materializes a `data:` mount
    mount.symlink_to(src / "corpus")
    fence = _install(run_dir, src, allow=[("corpus", src / "corpus")])
    import pydantic                              # a real site-packages file, outside every root
    rc, out, err, _to = _run(f"""
        import json, os
        reads = {{}}
        reads["workdir"] = open("own.txt").read().strip()
        reads["mount_through_symlink"] = open("corpus/queries.jsonl").read().strip()
        reads["mount_at_source"] = open({str(src / 'corpus' / 'queries.jsonl')!r}).read().strip()
        reads["site_packages"] = os.path.basename(open({pydantic.__file__!r}).name)
        reads["stdlib"] = os.path.basename(open(json.__file__).name)
        reads["model_dir"] = open({str(models / 'config.json')!r}).read().strip()
        reads["prefix_sibling"] = open({str(sibling / 'note.txt')!r}).read().strip()
        open(os.path.join({str(tmp_path)!r}, "scratch"), "w").write("x")   # writes are audited too
        reads["tmp"] = "ok"
        print(json.dumps(reads))
        """, wd, fence)
    assert rc == 0, err
    # A fenced process must be SILENT when nothing is refused. The first spelling of the
    # sitecustomize chain popped `sys.modules["sitecustomize"]`, which made importlib's own
    # `sys.modules.pop(name)` raise and put
    # `Error in sitecustomize; set PYTHONVERBOSE for traceback: KeyError: 'sitecustomize'`
    # on the stderr of every fenced eval — into eval.log, into the captured tail, into the repair
    # loop's input. Nothing else here would have failed on it.
    assert err == "", err
    got = json.loads(out.strip().splitlines()[-1])
    assert got == {
        "workdir": "the node's own copy",
        "mount_through_symlink": "mounted data",
        "mount_at_source": "mounted data",
        "site_packages": os.path.basename(pydantic.__file__),
        "stdlib": "__init__.py",
        "model_dir": "{}",
        "prefix_sibling": "sibling, not fenced",
        "tmp": "ok",
    }


def test_a_grandchild_process_is_fenced_too(tmp_path):
    """PYTHONPATH is inherited, so a dataloader worker / torchrun rank / a shell script's python is
    fenced at no extra cost. This is the whole reason the mechanism is `sitecustomize` and not an
    in-process patch of the eval's entry module."""
    src, _sib, run_dir, wd, _models = _world(tmp_path)
    target = src / "experiments" / "baseline" / "final" / "model.safetensors"
    fence = _install(run_dir, src)
    rc, out, err, _to = _run(f"""
        import subprocess, sys
        r = subprocess.run([sys.executable, "-c", "print(open({str(target)!r}).read())"],
                           capture_output=True, text=True)
        print("GRANDCHILD_RC", r.returncode)
        print(r.stderr)
        """, wd, fence)
    assert rc == 0                              # the parent itself read nothing
    assert "GRANDCHILD_RC 1" in out
    assert "LoopLabSourceReadRefused" in out
    assert CHECKPOINT not in out


def test_chdir_into_the_source_tree_is_refused(tmp_path):
    """The hot path bails on a relative path with no `..` without touching the filesystem, which is
    only sound while the cwd cannot BE the source tree. Refusing the chdir is what keeps it sound."""
    src, _sib, run_dir, wd, _models = _world(tmp_path)
    fence = _install(run_dir, src)
    rc, out, err, _to = _run(f"""
        import os
        os.chdir({str(src)!r})
        print(open("experiments/baseline/final/model.safetensors").read())
        """, wd, fence)
    assert rc != 0 and CHECKPOINT not in out
    assert "LoopLabSourceReadRefused" in err


# --------------------------------------------------------------------------- the rungs


def test_warn_rung_reads_through_and_records_it(tmp_path):
    """The defensible-for-one-run rung: the read happens, but it is on the node's stderr and in the
    run's own diagnostic, so an operator finding out what their pipeline reads is not flying blind."""
    src, _sib, run_dir, wd, _models = _world(tmp_path)
    target = src / "experiments" / "baseline" / "final" / "model.safetensors"
    fence = _install(run_dir, src, policy="warn")
    rc, out, err, _to = _run(f"print(open({str(target)!r}).read())", wd, fence)
    assert rc == 0 and CHECKPOINT in out                     # warn does NOT stop the read …
    assert "LOOPLAB READ FENCE (warn)" in err                # … it makes it observable
    assert read_fence.REFUSAL_MESSAGE.replace("{path}", str(target)) in err
    rows = read_fence.violations(run_dir)
    assert len(rows) == 1 and rows[0].startswith("warn\t") and str(target) in rows[0]


def test_warn_is_bounded_per_distinct_path(tmp_path):
    """A retry loop must not turn the diagnostic into a gigabyte of the same line."""
    src, _sib, run_dir, wd, _models = _world(tmp_path)
    target = src / "experiments" / "baseline" / "final" / "model.safetensors"
    fence = _install(run_dir, src, policy="warn")
    rc, _out, err, _to = _run(f"""
        for _ in range(50):
            open({str(target)!r}).read()
        """, wd, fence)
    assert rc == 0
    assert err.count("LOOPLAB READ FENCE (warn)") == 1
    assert len(read_fence.violations(run_dir)) == 1


def test_off_installs_nothing(tmp_path):
    src, _sib, run_dir, wd, _models = _world(tmp_path)
    assert _install(run_dir, src, policy="off") is None
    assert not (run_dir / read_fence.FENCE_DIRNAME).exists()


def test_deny_records_the_refusal_beside_the_run(tmp_path):
    """The stderr traceback is the primary channel, but a child that swallows every exception would
    take the evidence with it. The run keeps its own copy."""
    src, _sib, run_dir, wd, _models = _world(tmp_path)
    target = src / "experiments" / "baseline" / "final" / "model.safetensors"
    fence = _install(run_dir, src)
    _run(f"""
        try:
            open({str(target)!r}).read()
        except BaseException:
            pass
        """, wd, fence)
    rows = read_fence.violations(run_dir)
    assert len(rows) == 1 and rows[0].startswith("deny\t") and str(target) in rows[0]


# --------------------------------------------------------------------------- the predicate


def _predicate(roots, allow=()):
    """The generated fence's own `_fenced`, WITHOUT installing its irreversible audit hook."""
    src = read_fence.render(roots, allow, policy="deny", log="", run="")
    ns = {"__name__": read_fence._PROBE_NAME}
    exec(compile(src, "sitecustomize.py", "exec"), ns)
    assert "_hook" in ns and ns.get("_POLICY") == "deny"
    return ns["_fenced"]


@pytest.mark.parametrize("path,refused", [
    ("/src/repo/experiments/final/model.safetensors", True),
    ("/src/repo/", True),
    ("/src/repo", False),               # the root itself is not a file; opening it is an IsADirectory
    ("/src/repo/data/queries.jsonl", False),          # allow-listed mount source wins over the root
    ("/src/repository/other.txt", False),             # shared prefix, different tree
    ("/src/other/x", False),
    ("relative/path.txt", False),                     # cannot leave the cwd, and cwd is the workdir
    ("./x.txt", False),
    ("/tmp/x", False),
])
def test_predicate_truth_table(path, refused):
    fenced = _predicate(("/src/repo/",), ("/src/repo/data/",))
    assert (fenced(path) is not None) is refused


def test_predicate_normalizes_the_shapes_open_actually_receives():
    fenced = _predicate(("/src/repo/",))
    assert fenced(b"/src/repo/x") == "/src/repo/x"          # os.open takes bytes
    assert fenced(Path("/src/repo/x")) == "/src/repo/x"     # every os.PathLike
    assert fenced("/src/repo/a/../b") == "/src/repo/b"      # a `..` inside the root still lands
    assert fenced("/src/repo/../repo2/x") is None           # …and one that leaves it does not
    assert fenced(7) is None                                # os.fdopen on an already-open fd
    assert fenced(object()) is None                         # not a path at all -> never our problem


def test_the_directory_predicate_catches_the_root_itself():
    """`open` names a file INSIDE a root; `os.chdir` names the root, which carries no trailing
    separator and therefore misses the prefix test the hot path uses."""
    src = read_fence.render(("/src/repo/",), ("/src/repo/data/",), policy="deny", log="", run="")
    ns = {"__name__": read_fence._PROBE_NAME}
    exec(compile(src, "sitecustomize.py", "exec"), ns)
    assert ns["_fenced"]("/src/repo") is None                # the open path is deliberately unchanged
    assert ns["_fenced_dir"]("/src/repo") == "/src/repo"
    # Both spellings of the root are caught, and both REPORT the resolved form: `_fenced_dir` runs
    # `realpath` (it is a rare-path check — see `test_chdir_through_a_symlink_is_refused` for the
    # hole that closes), and `realpath` drops a trailing separator. What is asserted is that the
    # root itself is refused under either spelling, which is the property this test names.
    assert ns["_fenced_dir"]("/src/repo/") == "/src/repo"
    assert ns["_fenced_dir"]("/src/repo/data") is None       # an allow-listed mount stays enterable
    assert ns["_fenced_dir"]("/src/repository") is None


def test_a_root_too_broad_to_fence_is_dropped_not_accepted():
    """Fencing `$HOME` or `/` would refuse reads the interpreter itself needs, so a run with such an
    editable is left UNFENCED rather than made unable to start python at all — and it is reported."""
    home = os.path.expanduser("~")
    roots, _allow, dropped, _swallowed = read_fence.fence_inputs(
        {"editables": [{"name": ".", "path": home}, {"name": "x", "path": "/"},
                       {"name": "y", "path": "/usr"}]})
    assert roots == [] and len(dropped) == 3


# --------------------------------------------------------------------------- the plumbing


def test_run_argv_prepends_the_fence_and_keeps_an_existing_pythonpath(tmp_path):
    src, _sib, run_dir, wd, _models = _world(tmp_path)
    fence = _install(run_dir, src)
    rc, out, _err, _to = _run("import os; print(os.environ['PYTHONPATH'])", wd, fence,
                              extra_env={"PYTHONPATH": "/opt/theirs"})
    assert rc == 0
    assert out.strip().splitlines()[-1] == os.pathsep.join([fence, "/opt/theirs"])


def test_run_argv_leaves_a_docker_argv_alone(tmp_path, monkeypatch):
    """A `docker run` argv launches the CLI on the HOST; that PYTHONPATH would name a directory the
    container cannot see. The untrusted tiers are fenced by construction instead — the source tree
    is never bind-mounted in."""
    src, _sib, run_dir, wd, _models = _world(tmp_path)
    fence = _install(run_dir, src)
    fake = tmp_path / "docker"
    fake.write_text("#!/bin/sh\nexec env\n")
    fake.chmod(0o755)
    rc, out, _err, _to = run_argv([str(fake), "run", "--rm", "img"], str(wd), 60.0,
                                  {read_fence.FENCE_DIR_ENV: fence})
    assert rc == 0
    assert f"PYTHONPATH={fence}" not in out
    assert f"{read_fence.FENCE_DIR_ENV}={fence}" in out      # the marker still rides along, inert


def test_prepend_is_idempotent():
    env = {}
    read_fence.prepend_pythonpath(env, "/f")
    read_fence.prepend_pythonpath(env, "/f")
    assert env["PYTHONPATH"] == "/f"
    env = {"PYTHONPATH": os.pathsep.join(["/a", "/f", "/b"])}
    read_fence.prepend_pythonpath(env, "/f")
    assert env["PYTHONPATH"] == os.pathsep.join(["/f", "/a", "/b"])


def test_install_is_atomic_and_rewrites_only_on_change(tmp_path):
    src, _sib, run_dir, _wd, _models = _world(tmp_path)
    d = Path(_install(run_dir, src))
    first = (d / "sitecustomize.py").stat().st_mtime_ns
    assert _install(run_dir, src) == str(d)
    assert (d / "sitecustomize.py").stat().st_mtime_ns == first     # unchanged policy -> no rewrite
    _install(run_dir, src, policy="warn")
    assert "'warn'" in (d / "sitecustomize.py").read_text()
    assert not list(d.glob("*.tmp"))                                # no partial file left behind


def test_a_prior_sitecustomize_still_runs(tmp_path):
    """This directory is PREPENDED to PYTHONPATH, so it shadows anyone else's `sitecustomize`
    (coverage's subprocess support, a distro's). Shadowing one silently would be a real regression in
    someone else's tooling."""
    src, _sib, run_dir, wd, _models = _world(tmp_path)
    theirs = tmp_path / "theirs"
    theirs.mkdir()
    (theirs / "sitecustomize.py").write_text("import sys; sys.stderr.write('THEIRS RAN\\n')\n")
    fence = _install(run_dir, src)
    target = src / "experiments" / "baseline" / "final" / "model.safetensors"
    rc, _out, err, _to = _run(f"print(open({str(target)!r}).read())", wd, fence,
                              extra_env={"PYTHONPATH": str(theirs)})
    assert "THEIRS RAN" in err                       # …chained …
    assert rc != 0 and "LoopLabSourceReadRefused" in err     # …and ours still won the hook


# --------------------------------------------------------------------------- the engine wiring


def test_engine_stamps_the_marker_for_a_repo_task_only(tmp_path):
    """A toy run must be byte-identical to what it was: no fence directory, and the unpinned env
    stays the `None` three other modules read as 'whole box, legacy behavior'."""
    from tests.factories import make_engine

    toy = make_engine(tmp_path / "toy")
    assert toy._resource_eval_env(None) is None
    assert toy._read_fence_dir() is None
    assert not (Path(toy.run_dir) / read_fence.FENCE_DIRNAME).exists()

    src, _sib, _rd, _wd, _models = _world(tmp_path / "w")
    repo = make_engine(tmp_path / "repo")
    repo._repo_spec = {"editables": [{"name": ".", "path": str(src)}],
                       "data": {"corpus": {"path": str(src / "corpus")}}}
    env = repo._resource_eval_env(None)
    assert env is not None and env[read_fence.FENCE_DIR_ENV] == repo._read_fence_dir()
    assert "CUDA_VISIBLE_DEVICES" not in env          # the unpinned branch is otherwise untouched
    generated = (Path(repo.run_dir) / read_fence.FENCE_DIRNAME / "sitecustomize.py").read_text()
    assert str(src) + os.sep in generated
    assert str(src / "corpus") + os.sep in generated  # the data mount is allow-listed, not fenced


def test_engine_policy_off_stamps_nothing(tmp_path):
    from tests.factories import make_engine

    src, _sib, _rd, _wd, _models = _world(tmp_path / "w")
    e = make_engine(tmp_path / "run", read_fence="off")
    e._repo_spec = {"editables": [{"name": ".", "path": str(src)}]}
    assert e._read_fence_dir() is None
    assert e._resource_eval_env(None) is None


def test_a_pinned_reservation_still_carries_both(tmp_path):
    """The fence must not displace the GPU pin, and the pin must not displace the fence."""
    from tests.factories import make_engine

    src, _sib, _rd, _wd, _models = _world(tmp_path / "w")
    e = make_engine(tmp_path / "run")
    e._repo_spec = {"editables": [{"name": ".", "path": str(src)}]}
    e._gpu_physical_ids = {0: "3"}
    env = e._resource_eval_env({"gpu_ids": [0]}, base={"LOOPLAB_EVAL_SEED": "7"})
    assert env["CUDA_VISIBLE_DEVICES"] == "3"
    assert env["LOOPLAB_EVAL_SEED"] == "7"
    assert env[read_fence.FENCE_DIR_ENV] == e._read_fence_dir()


# --------------------------------------------------------------------------- node 4, end to end

# `runs/rubertlite-dr-unified-v6/nodes/node_4` in miniature, and the ONLY test here that runs a whole
# engine. The scorer is the operator's protected entrypoint; it resolves a checkpoint path out of an
# editable CONFIG, and that config carries an absolute path into the source tree where a human's
# stale checkpoint sits. Nothing in the node's declared artifacts is wrong, so `expect` passes and
# the run records the stale number as the node's metric. The `off` half of this test IS that
# corruption, reproduced; the `deny` half is the fence refusing it.
_SCORER = (
    "import json\n"
    "path = open('config.txt').read().strip()\n"
    "print(json.dumps({'metric': float(open(path).read().strip())}))\n"
)
HUMAN_METRIC = 0.224975      # what the stale source-tree checkpoint scores


def _git_repo(root, tracked):
    import subprocess
    root.mkdir(parents=True, exist_ok=True)
    for name, text in tracked.items():
        (root / name).write_text(text, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "add", *tracked], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "i"],
                   cwd=root, check=True, capture_output=True)
    return root


def _repo_run(tmp_path, policy):
    import anyio
    from looplab.adapters.repo_task import EvalSpec, RepoTask
    from looplab.engine.orchestrator import Engine
    from looplab.runtime.sandbox import SubprocessSandbox
    from looplab.search.policy import GreedyTree

    src = Path(tmp_path) / "vectorizer-unified"
    src.mkdir(parents=True)
    (src / "checkpoint.txt").write_text(f"{HUMAN_METRIC}\n")     # the human's 2026-07-18 artifact
    _git_repo(src, {"looplab_eval.py": _SCORER,
                    "config.txt": str(src / "checkpoint.txt") + "\n"})
    task = RepoTask(id="p", direction="max", editable_path=str(src),
                    protect=["looplab_eval.py"],
                    eval=EvalSpec(command=[sys.executable, "looplab_eval.py"],
                                  metric={"kind": "stdout_json", "key": "metric"}))
    researcher, developer = task.build_roles()
    run_dir = tmp_path / ("run-" + policy)
    engine = Engine(run_dir, task=task, researcher=researcher, developer=developer,
                    sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=1),
                    read_fence=policy)
    state = anyio.run(engine.run)
    errors = [json.loads(x)["data"].get("error", "")
              for x in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
              if json.loads(x)["type"] == "node_failed"]
    return state, errors, src


def test_the_node_4_corruption_is_reproduced_and_then_refused(tmp_path):
    off_state, off_errors, _src = _repo_run(tmp_path / "off", "off")
    best = off_state.best()
    # Unfenced: the run FINISHES, reports a best node, and the number it reports came out of the
    # operator's source tree. No failure, no violation, nothing to look at. This is the defect.
    assert not off_errors and best is not None and best.metric == pytest.approx(HUMAN_METRIC)

    deny_state, deny_errors, src = _repo_run(tmp_path / "deny", "deny")
    assert deny_state.best() is None, "a read of the source tree must not produce a metric"
    assert deny_errors, "the refusal has to reach a node terminal, not vanish"
    joined = "\n".join(deny_errors)
    assert "is under the operator's SOURCE tree, not this node's workspace" in joined
    assert str(src / "checkpoint.txt") in joined            # the offending path, by name
    assert 'seed_mode: "all"' in joined                     # …and the fix, in the repair loop's input
    rows = read_fence.violations(tmp_path / "deny" / "run-deny")
    assert rows and str(src / "checkpoint.txt") in rows[0]


# ------------------------------------------------------------------- mutation (docs/34, 2026-08-13)
#
# The fence gated `open` and nothing else. `os.remove`, `os.rename`, `os.truncate` and `os.chmod`
# raise their OWN audit events and none of them raises `open`, so a node's eval code could delete or
# rename the operator's editable tree while every read of it was refused. Measured before the fix:
# 14 of 14 mutation probes went THROUGH, `shutil.rmtree(<the source root>)` included.
#
# Every test below launches a REAL fenced subprocess and then asks the FILESYSTEM what happened,
# rather than reading the fence's source. A source pin here would be satisfied by a comment; only
# "the file is still on disk" distinguishes a refusal from a refusal that did not fire.


def _survives(target: Path, code: str, wd, fence) -> tuple:
    """Run `code` fenced, and report `(the file is still there, stderr)`."""
    rc, out, err, timed_out = _run(code, wd, fence)
    assert not timed_out
    return target.exists(), (rc, out, err)


def test_a_source_delete_is_refused_and_the_file_is_still_there(tmp_path):
    """The headline. Fails without the fence: the control half is the SAME child with no marker and
    it really deletes the operator's checkpoint."""
    src, _sib, run_dir, wd, _models = _world(tmp_path)
    target = src / "experiments" / "baseline" / "final" / "model.safetensors"
    code = f"import os; os.remove({str(target)!r}); print('DELETED')"

    rc, out, _err, _to = _run(code, wd)
    assert rc == 0 and "DELETED" in out and not target.exists(), "control: the delete must succeed"
    target.write_text(CHECKPOINT)                       # put the operator's file back

    fence = _install(run_dir, src)
    alive, (rc, out, err) = _survives(target, code, wd, fence)
    assert alive, "the operator's file was DELETED from inside a fenced node process"
    assert rc != 0 and "DELETED" not in out
    assert "LoopLabSourceReadRefused" in err
    assert read_fence.MUTATION_REFUSAL_MESSAGE.replace("{path}", str(target)) in err
    assert "may not create, delete, rename, truncate, link or change" in err
    assert target.read_text() == CHECKPOINT             # …and untouched, not merely present


def test_rmtree_of_the_source_root_itself_is_refused(tmp_path):
    """The worst case, and the one the prefix test alone could not see: `shutil.rmtree(<root>)`.

    The root directory's own name carries no trailing separator, so opening it is not refused; then
    rmtree unlinks every file under it with `os.remove(<bare name>, dir_fd=<fd of the dir>)`, which
    CPython audits with the RELATIVE name. Measured before the fix: the whole tree, gone."""
    src, _sib, run_dir, wd, _models = _world(tmp_path)
    fence = _install(run_dir, src)
    target = src / "experiments" / "baseline" / "final" / "model.safetensors"
    _rc, _out, err, _to = _run(f"import shutil; shutil.rmtree({str(src)!r}); print('GONE')", wd, fence)
    assert "LoopLabSourceReadRefused" in err
    assert src.exists() and target.read_text() == CHECKPOINT


def test_the_mutation_refusal_is_not_swallowed_by_an_oserror_handler(tmp_path):
    """`except OSError:` is exactly as routine around a cleanup `os.remove` as it is around a read,
    so the mutation refusal keeps the same deliberate non-`OSError` shape. A kernel-level fence
    (landlock, an LD_PRELOAD shim) can only return EACCES and would be caught here — which is why
    docs/34 recommends one ALONGSIDE this hook rather than instead of it."""
    src, _sib, run_dir, wd, _models = _world(tmp_path)
    target = src / "experiments" / "baseline" / "final" / "model.safetensors"
    fence = _install(run_dir, src)
    rc, out, err, _to = _run(f"""
        import os
        try:
            os.remove({str(target)!r})
        except OSError:
            print("SWALLOWED — a routine cleanup handler ate the refusal")
        """, wd, fence)
    assert "SWALLOWED" not in out
    assert rc != 0 and "LoopLabSourceReadRefused" in err and target.exists()


# One operation per registered event. The dict IS the two-way registry check: its keys must equal
# `MUTATION_EVENTS`, so an event added to the fence without a probe here — or a probe whose event
# was dropped from the fence — fails rather than passing silently.
_MUTATION_PROBES = {
    "os.remove": "os.remove(T)",
    "os.rename": "os.rename(T, T + '.moved')",
    "os.truncate": "os.truncate(T, 0)",
    "os.chmod": "os.chmod(T, 0o600)",
    "os.chown": "os.chown(T, -1, -1)",
    "os.utime": "os.utime(T, (0, 0))",
    "os.mkdir": "os.mkdir(os.path.join(os.path.dirname(T), 'new'))",
    "os.rmdir": "os.rmdir(os.path.dirname(T))",
    "os.symlink": "os.symlink('/etc/hostname', os.path.join(os.path.dirname(T), 'planted'))",
    "os.link": "os.link(T, 'stolen-hardlink')",
    "os.setxattr": "os.setxattr(T, 'user.looplab', b'1')",
    "os.removexattr": "os.removexattr(T, 'user.looplab')",
}


def test_the_probe_table_covers_exactly_the_registry():
    assert set(_MUTATION_PROBES) == set(read_fence.MUTATION_EVENTS)


@pytest.mark.parametrize("event", sorted(_MUTATION_PROBES))
def test_every_registered_mutation_event_is_refused(tmp_path, event):
    """Each registered event, driven through a real fenced child against the operator's tree."""
    src, _sib, run_dir, wd, _models = _world(tmp_path)
    target = src / "experiments" / "baseline" / "final" / "model.safetensors"
    fence = _install(run_dir, src)
    rc, out, err, _to = _run(f"""
        import os
        T = {str(target)!r}
        {_MUTATION_PROBES[event]}
        print('THROUGH')
        """, wd, fence)
    assert "THROUGH" not in out and rc != 0
    assert "LoopLabSourceReadRefused" in err, f"{event} was not refused"
    assert target.exists() and target.read_text() == CHECKPOINT
    # …and the run keeps its own record of WHICH event was attempted.
    rows = read_fence.violations(run_dir)
    assert rows and rows[0].startswith("deny\t") and rows[0].endswith("\t" + event)


def test_mutation_arg_shapes_match_the_interpreter(tmp_path):
    """`MUTATION_EVENTS` says WHERE each event carries its paths. Re-derive that from the running
    interpreter instead of trusting the table: perform every operation under a recording audit hook
    and check the declared indices are the ones that actually hold the paths we passed.

    This is the guard that keeps the table from rotting. A CPython release that inserts an argument
    would leave the fence checking `args[0]` of something that is no longer a path — the fence would
    still install, still run, and silently stop refusing. Run in a SUBPROCESS because
    `sys.addaudithook` is irreversible and would otherwise fence the rest of the session."""
    probe = tmp_path / "shapes.py"
    probe.write_text(textwrap.dedent("""
        import json, os, sys
        D = sys.argv[1]
        SEEN = {}
        RECORDING = [False]

        def hook(event, args):
            if RECORDING[0] and event.startswith("os."):
                SEEN.setdefault(event, []).append(args)

        sys.addaudithook(hook)

        def mk(name):
            p = os.path.join(D, name)
            with open(p, "wb") as fh:
                fh.write(b"x")
            return p

        A, B = mk("a"), os.path.join(D, "b")
        sub = os.path.join(D, "sub")
        os.mkdir(sub)
        RECORDING[0] = True
        os.chmod(A, 0o600); os.chown(A, -1, -1); os.utime(A, (0, 0))
        os.truncate(A, 1); os.setxattr(A, "user.p", b"1"); os.removexattr(A, "user.p")
        os.symlink(A, os.path.join(D, "sym")); os.link(A, os.path.join(D, "hard"))
        os.mkdir(os.path.join(D, "made")); os.rmdir(sub)
        os.rename(A, B); os.remove(B)
        RECORDING[0] = False
        print(json.dumps({k: [[a if isinstance(a, (str, int)) else None for a in args]
                              for args in v] for k, v in SEEN.items()}))
        """), encoding="utf-8")
    work = tmp_path / "shapework"
    work.mkdir()
    # Not through `_run`/`run_argv`: this child must be UNFENCED (it performs every mutation for
    # real) and it takes an argv the fenced helper does not pass.
    res = subprocess.run([sys.executable, str(probe), str(work)], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    seen = json.loads(res.stdout.strip().splitlines()[-1])

    assert set(read_fence.MUTATION_EVENTS) <= set(seen), (
        "this interpreter did not raise every registered event: "
        f"{sorted(set(read_fence.MUTATION_EVENTS) - set(seen))}")
    for event, slots in read_fence.MUTATION_EVENTS.items():
        args = seen[event][0]
        assert len(slots) <= len(args)
        for path_i, fd_i in slots:
            value = args[path_i]
            assert isinstance(value, str) and value.startswith(str(work)), (
                f"{event}: args[{path_i}] is {value!r}, not one of the paths we passed — the "
                f"registry's path slot moved")
            if fd_i is not None:
                assert fd_i < len(args) and isinstance(args[fd_i], int), (
                    f"{event}: args[{fd_i}] is not a dir_fd on this interpreter")


def test_unlinkat_through_a_dir_fd_is_refused(tmp_path):
    """CPython audits the RELATIVE name for a dir_fd call, which no prefix compare can match. The
    fence resolves the descriptor through /proc/self/fd — affordable only because this never runs
    on the `open` hot path."""
    src, _sib, run_dir, wd, _models = _world(tmp_path)
    final = src / "experiments" / "baseline" / "final"
    fence = _install(run_dir, src)
    rc, _out, err, _to = _run(f"""
        import os
        d = os.open({str(final)!r}, os.O_RDONLY)
        os.remove('model.safetensors', dir_fd=d)
        """, wd, fence)
    assert rc != 0 and "LoopLabSourceReadRefused" in err
    assert (final / "model.safetensors").exists()


def test_chdir_through_a_symlink_is_refused(tmp_path):
    """The fence's relative-path fast bail is sound only because a process cannot `chdir` into a
    root. It compared `abspath`, so a workdir symlink pointing at the source was enterable and every
    bare relative name then read the tree — measured THROUGH. `os.chdir` is rare, so it resolves."""
    src, _sib, run_dir, wd, _models = _world(tmp_path)
    (wd / "sneak").symlink_to(src)
    fence = _install(run_dir, src)
    rc, out, err, _to = _run("""
        import os
        os.chdir('sneak')
        print(open('experiments/baseline/final/model.safetensors').read())
        """, wd, fence)
    assert rc != 0 and CHECKPOINT not in out and "LoopLabSourceReadRefused" in err


def test_a_mutation_under_a_symlinked_directory_is_refused(tmp_path):
    """The same resolution, on the mutation side: the DIRNAME is resolved (memoized), so a delete
    aimed through a workdir symlink lands on the real tree and is refused."""
    src, _sib, run_dir, wd, _models = _world(tmp_path)
    (wd / "sneak").symlink_to(src / "experiments" / "baseline" / "final")
    fence = _install(run_dir, src)
    target = src / "experiments" / "baseline" / "final" / "model.safetensors"
    rc, _out, err, _to = _run("import os; os.remove('sneak/model.safetensors')", wd, fence)
    assert rc != 0 and "LoopLabSourceReadRefused" in err
    assert target.read_text() == CHECKPOINT


def test_the_nodes_own_cleanup_is_untouched(tmp_path):
    """The false-positive half, and the one that decides whether this is shippable: everything a
    node legitimately does to its OWN workspace still works — a training run writes, renames,
    chmods, truncates and deletes constantly. A mutation of an allow-listed `data:` mount SOURCE is
    also permitted: the allow-list means "this is not the fenced source tree", and it is uniform
    across reads and writes rather than being a second, different policy."""
    src, _sib, run_dir, wd, models = _world(tmp_path)
    fence = _install(run_dir, src, allow=[("corpus", src / "corpus")])
    rc, out, err, _to = _run(f"""
        import os, shutil
        os.makedirs('ckpt/step_1', exist_ok=True)
        open('ckpt/step_1/model.bin', 'wb').write(b'weights')
        os.chmod('ckpt/step_1/model.bin', 0o644)
        os.truncate('ckpt/step_1/model.bin', 4)
        os.rename('ckpt/step_1', 'ckpt/step_2')
        os.symlink('ckpt/step_2', 'latest')
        os.link('ckpt/step_2/model.bin', 'ckpt/hardlink.bin')
        os.utime('ckpt/step_2/model.bin', (0, 0))
        shutil.rmtree('ckpt/step_2')
        os.remove('latest')
        open({str(models / 'config.json')!r} + '.tmp', 'w').write('{{}}')     # the model cache
        os.remove({str(models / 'config.json')!r} + '.tmp')
        open({str(src / 'corpus' / 'scratch.txt')!r}, 'w').write('mount')     # allow-listed source
        os.remove({str(src / 'corpus' / 'scratch.txt')!r})
        print('ALL LEGAL WORK DONE')
        """, wd, fence)
    assert rc == 0, err
    assert "ALL LEGAL WORK DONE" in out
    assert not read_fence.violations(run_dir), "a legal workspace mutation raised a violation"


def test_warn_lets_a_mutation_through_and_records_it(tmp_path):
    """`warn` is the honest rung for one run while an operator finds out what their pipeline
    touches. It must not silently become a deny for mutations."""
    src, _sib, run_dir, wd, _models = _world(tmp_path)
    target = src / "experiments" / "baseline" / "final" / "model.safetensors"
    fence = _install(run_dir, src, policy="warn")
    rc, out, err, _to = _run(f"import os; os.remove({str(target)!r}); print('DELETED')", wd, fence)
    assert rc == 0 and "DELETED" in out and not target.exists()
    assert "LOOPLAB READ FENCE (warn)" in err
    rows = read_fence.violations(run_dir)
    assert len(rows) == 1 and rows[0].startswith("warn\t") and rows[0].endswith("\tos.remove")


# ------------------------------------------------------- the cwd the fast bail rests on, and shapes


def test_a_relative_read_from_a_cwd_inside_the_source_tree_is_refused(tmp_path):
    """The fast bail for relative paths rested on "the cwd cannot be inside a root, because
    `os.chdir` into one is refused". A launcher sets the cwd at process CREATION —
    `subprocess.run(cwd=…)`, `bash -c 'cd <repo> && python …'` — and `fork_exec` does that chdir in
    C, raising no audit event. So the premise was false and the whole fence was one `cd` away from
    inert: this is the v6-node-4 read spelled relatively."""
    src, _sib, run_dir, _wd, _models = _world(tmp_path)
    code = "print(open('experiments/baseline/final/model.safetensors').read())"

    unfenced_rc, unfenced_out, _err, _to = _run(code, src)
    assert unfenced_rc == 0 and CHECKPOINT in unfenced_out, "control: the read succeeds unfenced"

    fence = _install(run_dir, src)
    rc, out, err, timed_out = _run(code, src, fence)          # cwd IS the source tree
    assert rc != 0 and not timed_out and CHECKPOINT not in out
    assert "LoopLabSourceReadRefused" in err
    # The refusal names the RESOLVED path, so the repair loop is told which file, not './x'.
    assert str(src / "experiments" / "baseline" / "final" / "model.safetensors") in err


def test_a_source_path_spelled_with_duplicate_or_dot_separators_is_refused(tmp_path):
    """The roots are `realpath`-ed once and the hot path is a byte-exact prefix compare, so every
    other spelling of the same file was silently allowed. `f"{root}/{sub}"` produces the duplicate
    separator whenever root ends in one — this is ordinary composition, not an attack."""
    src, _sib, run_dir, wd, _models = _world(tmp_path)
    fence = _install(run_dir, src)
    base = str(src / "experiments" / "baseline" / "final")
    for spelling in (base.replace(str(src), str(src) + "/") + "/model.safetensors",
                     str(src) + "/./experiments/baseline/final/model.safetensors",
                     base + "/./model.safetensors",
                     base + "/../final/model.safetensors"):
        rc, out, err, _to = _run(f"print(open({spelling!r}).read())", wd, fence)
        assert rc != 0 and CHECKPOINT not in out, f"{spelling} bypassed the fence"
        assert "LoopLabSourceReadRefused" in err


def test_an_allow_prefix_that_contains_a_root_is_refused_and_reported(tmp_path):
    """`_fenced` consults the allow tuple AFTER the root tuple, so an allow entry containing a root
    does not narrow the fence — it disables it. Reachable by ordinary spelling: `--out .` beside an
    editable `./repo` makes `resources.py` pass the repo's own parent as `allow=[run_dir]`."""
    src, _sib, _run_dir, wd, _models = _world(tmp_path)
    roots, allowed, dropped, swallowed = read_fence.fence_inputs(
        {"editables": [{"name": ".", "path": str(src)}]}, allow=[str(tmp_path)])
    assert roots and not dropped
    assert allowed == [], "an ancestor of a root must never reach the allow tuple"
    assert swallowed == [str(tmp_path) + os.sep], "…and it must be REPORTED, not silently dropped"

    # A strict DESCENDANT is a real carve-out and still survives — that is what mounts rely on.
    _r, kept, _d, _s = read_fence.fence_inputs(
        {"editables": [{"name": ".", "path": str(src)}]}, allow=[str(src / "corpus")])
    assert kept == [str(src / "corpus") + os.sep]

    # …and the fence a swallowed allow-list would have disabled actually refuses.
    fence = read_fence.install(tmp_path / "run", roots=roots, allow=allowed, policy="deny")
    rc, out, _err, _to = _run(
        f"print(open({str(src / 'experiments' / 'baseline' / 'final' / 'model.safetensors')!r}).read())",
        wd, fence)
    assert rc != 0 and CHECKPOINT not in out


def test_an_unrecognised_policy_settles_to_deny_not_to_warn(tmp_path):
    """`Settings` validates the enum; `EngineOptions.read_fence`, a hand-edited snapshot and a
    Strategist value do not. The generated hook tests `_POLICY == "deny"` exactly, so an unknown
    spelling used to fall through to the WARN branch: every source read allowed while the
    operator's config said `deny`. Every sibling knob settles unknown to the CONSERVATIVE rung."""
    assert [read_fence.settle_policy(v) for v in ("deny", "warn", "off", " OFF ")] == [
        "deny", "warn", "off", "off"]
    for rogue in ("Deny", "denied", "", None, "warn-only", 0, object()):
        assert read_fence.settle_policy(rogue) == "deny"

    src, _sib, run_dir, wd, _models = _world(tmp_path)
    fence = _install(run_dir, src, policy="Deny")             # capitalized: not in POLICIES
    rc, out, err, _to = _run(
        f"print(open({str(src / 'experiments' / 'baseline' / 'final' / 'model.safetensors')!r}).read())",
        wd, fence)
    assert rc != 0 and CHECKPOINT not in out and "LoopLabSourceReadRefused" in err


def test_the_warn_rung_bounds_stderr_the_same_way_it_bounds_the_log(tmp_path):
    """The 256-entry bound gated only `_seen.add`/`_record`, so once saturated `first` was True for
    EVERY subsequent read — including repeats of one path. That inverts the guard: a write+flush
    syscall pair per `open()` on the hot read path, flooding the captured stderr that `eval.log` and
    the repair feedback are built from."""
    log = tmp_path / "violations.log"
    src = read_fence.render(("/src/repo/",), (), policy="warn", log=str(log))
    ns = {"__name__": read_fence._PROBE_NAME}
    exec(compile(src, "<fence>", "exec"), ns)                # probe name: no audit hook installed
    captured = io.StringIO()
    with contextlib.redirect_stderr(captured):
        for i in range(300):
            ns["_report"](f"/src/repo/shard-{i}.bin", "open", ns["_MESSAGE"])
        for _ in range(1000):
            # the retry loop the bound exists for
            ns["_report"]("/src/repo/hot.bin", "open", ns["_MESSAGE"])
    lines = [ln for ln in captured.getvalue().splitlines() if ln.strip()]
    assert len(lines) == 256, f"stderr must honour the same bound as the log, got {len(lines)}"
    assert len([ln for ln in log.read_text().splitlines() if ln.strip()]) == 256
    # Both sinks stop at the SAME point, so neither can report a path the other omitted. The log's
    # PATH is column 4 of five — the EVENT is last, since a read and a delete of one file are two
    # incidents (which is also why `_seen` is keyed on the pair).
    assert {ln.split("\t")[3] for ln in log.read_text().splitlines() if ln.strip()} == {
        ln.split("refused: ", 1)[1].split(" is under", 1)[0] for ln in lines}


def test_the_bound_separates_a_read_from_a_mutation_of_the_same_path(tmp_path):
    """`_seen` is keyed by (event, path), not by path: a node that reads the operator's checkpoint
    and then deletes it must produce BOTH lines, and each must carry its own message — the read one
    says "use a workdir-relative path", which is not advice about a delete."""
    log = tmp_path / "violations.log"
    src = read_fence.render(("/src/repo/",), (), policy="warn", log=str(log))
    ns = {"__name__": read_fence._PROBE_NAME}
    exec(compile(src, "<fence>", "exec"), ns)
    captured = io.StringIO()
    with contextlib.redirect_stderr(captured):
        for _ in range(3):
            ns["_report"]("/src/repo/model.bin", "open", ns["_MESSAGE"])
            ns["_report"]("/src/repo/model.bin", "os.remove", ns["_MUTATION_MESSAGE"])
    rows = [ln for ln in log.read_text().splitlines() if ln.strip()]
    assert [ln.split("\t")[-1] for ln in rows] == ["open", "os.remove"]
    err = captured.getvalue()
    assert "Use a workdir-relative path" in err
    assert "may not create, delete, rename, truncate, link or change" in err


def test_settings_vocabulary_matches_the_module(tmp_path):
    """`core` cannot import `runtime`, so the policy vocabulary is spelled twice. Pin them equal —
    a third rung added on one side only would be accepted by config and ignored by the fence."""
    enum = dict((name, vocab) for name, vocab in Settings._ENUM_FIELDS if name == "read_fence")
    assert tuple(enum["read_fence"]) == tuple(read_fence.POLICIES)
    assert Settings().read_fence == "deny"
    with pytest.raises(ValueError):
        Settings(read_fence="warn-only")
