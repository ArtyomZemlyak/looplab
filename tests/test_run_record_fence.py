"""The eval may not write the run RECORD (doc 52 §5.1 row 2, 2026-09-06).

The run directory is the record an evaluation is scored INTO — `events.jsonl`, the snapshots, the
traces — and the evaluation's own workdir lives inside it. Until this change the launch allow-list
granted the whole run dir `readwrite` and nothing in the fence looked at it, so a training script
could append a well-formed `node_evaluated` row naming its own node and a metric of its choosing.
The store's foreign-writer stop fires only on a MALFORMED row, the fold applies the FIRST terminal,
and the engine's own terminal then landed second. Docs/36: the candidate may never elect.

Every behavioural test launches a REAL subprocess through `run_argv` — the same choke point the
engine uses — and asks the FILESYSTEM what happened, for the reason `tests/test_read_fence.py`
gives: `sys.addaudithook` is irreversible, and a source pin here would be satisfied by a comment.
The headline is `test_the_forged_terminal_is_recorded_unfenced_and_refused_fenced`, the only test
that runs a whole engine: its `off` half IS the defect, reproduced — the run reports the forged
number as its best — and its `deny` half is the fence refusing it.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from looplab.runtime import read_allowlist, read_fence
from looplab.runtime.sandbox import run_argv


def _world(tmp_path):
    """A run directory with a record, the fence installed with NO source roots (the shape every
    non-repo run gets now), the node's workdir and a sibling node's workdir."""
    run_dir = tmp_path / "run"
    (run_dir / "nodes" / "node_4").mkdir(parents=True)
    (run_dir / "nodes" / "node_3").mkdir(parents=True)
    (run_dir / "nodes" / "node_3" / "predictions.json").write_text("[1]\n")
    (run_dir / "events.jsonl").write_text(
        json.dumps({"v": 1, "seq": 0, "ts": 1.0, "type": "run_started", "data": {"run_id": "r"}})
        + "\n", encoding="utf-8")
    (run_dir / "config.snapshot.json").write_text("{}\n", encoding="utf-8")
    return run_dir, run_dir / "nodes" / "node_4", run_dir / "nodes" / "node_3"


def _install(run_dir, *, policy="deny"):
    return read_fence.install(run_dir, roots=[], allow=[], policy=policy)


_TRY = '''
import os, json, sys, pathlib
def attempt(name, fn):
    try:
        fn()
        print("ALLOWED", name)
    except Exception as e:
        print("REFUSED", name, type(e).__name__, str(e).replace("\\n", " "))
'''


def _run(code, wd, fence_dir, extra_env=None):
    """Launch `code` (dedented, with the `attempt` prelude in front) fenced, through `run_argv`."""
    env = dict(extra_env or {})
    env[read_fence.FENCE_DIR_ENV] = fence_dir
    return run_argv([sys.executable, "-c", _TRY + textwrap.dedent(code)], str(wd), 60.0, env)




def _verdicts(out: str) -> dict:
    got = {}
    for line in out.splitlines():
        parts = line.split(" ", 2)
        if parts[0] in ("ALLOWED", "REFUSED"):
            got[parts[1]] = line
    return got


# --------------------------------------------------------------------------- the property


def test_a_write_to_the_event_log_is_refused_and_the_row_never_lands(tmp_path):
    """THE DEFECT, at the choke point. Every spelling of a write — `open` modes, `os.open` flags,
    `pathlib` — is refused with the RECORD sentence, and the bytes on disk are what they were."""
    run_dir, wd, _sib = _world(tmp_path)
    fence = _install(run_dir)
    before = (run_dir / "events.jsonl").read_bytes()
    log = str(run_dir / "events.jsonl")
    rc, out, err, _to = _run(f"""
        row = json.dumps({{"v": 1, "seq": 1, "ts": 2.0, "type": "node_evaluated",
                           "data": {{"node_id": 4, "metric": 999.0}}}}) + "\\n"
        attempt("append", lambda: open({log!r}, "a").write(row))
        attempt("write", lambda: open({log!r}, "w").write(row))
        attempt("rplus", lambda: open({log!r}, "r+").write(row))
        attempt("os_open", lambda: os.close(os.open({log!r}, os.O_WRONLY | os.O_APPEND)))
        attempt("pathlib", lambda: pathlib.Path({log!r}).write_text(row))
        attempt("relative", lambda: open("../../events.jsonl", "a").write(row))
        attempt("snapshot", lambda: open({str(run_dir / 'config.snapshot.json')!r}, "w").write("{{}}"))
        """, wd, fence)
    got = _verdicts(out)
    for name in ("append", "write", "rplus", "os_open", "pathlib", "relative", "snapshot"):
        assert got[name].startswith(f"REFUSED {name} LoopLabSourceReadRefused"), got[name]
        assert "run's own RECORD" in got[name], got[name]
    assert (run_dir / "events.jsonl").read_bytes() == before, "a row landed"
    assert (run_dir / "config.snapshot.json").read_text() == "{}\n"
    rows = read_fence.violations(run_dir)
    assert rows and log in rows[0] and rows[0].rstrip().endswith("open")


def test_the_record_stays_readable_and_the_workdir_and_the_fence_dir_writable(tmp_path):
    """The rule is about WRITES: a node may read the run it belongs to, write anything under its own
    workdir (create, mkdir, remove, rename, chmod), append the fence's own diagnostic, and write
    outside the run dir exactly as before — and a fenced process is SILENT when nothing is refused."""
    run_dir, wd, _sib = _world(tmp_path)
    fence = _install(run_dir)
    outside = tmp_path / "scratch.txt"
    rc, out, err, _to = _run(f"""
        attempt("read_log", lambda: open({str(run_dir / 'events.jsonl')!r}).read())
        attempt("read_snapshot", lambda: open({str(run_dir / 'config.snapshot.json')!r}).read())
        attempt("read_sibling", lambda: open({str(run_dir / 'nodes' / 'node_3' / 'predictions.json')!r}).read())
        attempt("own_write", lambda: open("out.txt", "w").write("x"))
        attempt("own_abs_write", lambda: open({str(wd / 'abs.txt')!r}, "w").write("x"))
        attempt("own_mkdir", lambda: os.mkdir("sub"))
        attempt("own_rename", lambda: os.rename("abs.txt", "sub/moved.txt"))
        attempt("own_chmod", lambda: os.chmod("sub/moved.txt", 0o600))
        attempt("own_remove", lambda: os.remove("sub/moved.txt"))
        attempt("own_rmdir", lambda: os.rmdir("sub"))
        attempt("fence_log", lambda: open({str(run_dir / read_fence.FENCE_DIRNAME / read_fence.VIOLATION_LOG)!r}, "a").write(""))
        attempt("outside", lambda: open({str(outside)!r}, "w").write("x"))
        """, wd, fence)
    assert rc == 0, err
    assert err == "", err
    got = _verdicts(out)
    assert all(v.startswith("ALLOWED") for v in got.values()), got
    assert len(got) == 12
    assert (wd / "out.txt").read_text() == "x" and outside.read_text() == "x"


@pytest.mark.parametrize("name, code", [
    ("rename_into_record", "os.rename('mine.txt', RUN + '/events.jsonl')"),
    ("replace_into_record", "os.replace('mine.txt', RUN + '/config.snapshot.json')"),
    ("hardlink_of_record", "os.link(RUN + '/events.jsonl', 'hl')"),
    ("symlink_to_record", "os.symlink(RUN + '/events.jsonl', 'ln')"),
    ("truncate_record", "os.truncate(RUN + '/events.jsonl', 0)"),
    ("chmod_record", "os.chmod(RUN + '/events.jsonl', 0o600)"),
    ("remove_snapshot", "os.remove(RUN + '/config.snapshot.json')"),
    ("mkdir_in_record", "os.mkdir(RUN + '/evil')"),
    ("mkdir_in_nodes", "os.mkdir(RUN + '/nodes/node_99')"),
    ("sibling_workdir_write", "open(RUN + '/nodes/node_3/predictions.json', 'w').write('[0]')"),
    ("sibling_workdir_remove", "os.remove(RUN + '/nodes/node_3/predictions.json')"),
    ("rmdir_record_itself", "os.rmdir(RUN)"),
    ("relative_dotdot_mkdir", "os.mkdir('../../evil2')"),
])
def test_every_mutation_of_the_record_is_refused(tmp_path, name, code):
    """Each registered mutation event whose target is under the record — and a write into a
    SIBLING node's workdir, which is somebody else's evidence — is refused, and the filesystem
    is what it was. The link cases close the planted-link route for links the process makes."""
    run_dir, wd, _sib = _world(tmp_path)
    (wd / "mine.txt").write_text("m")
    fence = _install(run_dir)
    snapshot = sorted(str(p.relative_to(run_dir)) for p in run_dir.rglob("*")
                      if read_fence.FENCE_DIRNAME not in p.parts)
    log_before = (run_dir / "events.jsonl").read_bytes()
    rc, out, err, _to = _run(f"""
        RUN = {str(run_dir)!r}
        attempt({name!r}, lambda: {code})
        """, wd, fence)
    got = _verdicts(out)
    assert got[name].startswith(f"REFUSED {name} LoopLabSourceReadRefused"), (got, err)
    assert "run's own RECORD" in got[name]
    after = sorted(str(p.relative_to(run_dir)) for p in run_dir.rglob("*")
                   if read_fence.FENCE_DIRNAME not in p.parts)
    assert after == snapshot, (name, set(after) ^ set(snapshot))
    assert (run_dir / "events.jsonl").read_bytes() == log_before
    assert (run_dir / "nodes" / "node_3" / "predictions.json").read_text() == "[1]\n"


def test_a_launch_without_the_workdir_variable_fails_closed(tmp_path):
    """One generated fence serves every launch of the run, so which directory is writable is a
    per-launch fact `run_argv` hands the child in `LOOPLAB_EVAL_WORKDIR`. A launch that carries the
    fence and NOT that variable must refuse even the node's own writes — loudly — rather than guess:
    a launch path that forgot to say which directory is the node's is a bug to surface, not one to
    paper over with 'the cwd, probably'."""
    run_dir, wd, _sib = _world(tmp_path)
    fence = _install(run_dir)
    env = {k: v for k, v in os.environ.items() if k != read_fence.WORKDIR_ENV}
    env["PYTHONPATH"] = fence
    out = subprocess.run(
        [sys.executable, "-c", _TRY + textwrap.dedent("""
            attempt("own_write", lambda: open("out.txt", "w").write("x"))
            attempt("outside", lambda: open(os.path.join(os.path.dirname(os.path.dirname(
                os.path.dirname(os.getcwd()))), "scratch.txt"), "w").write("x"))
            """)],
        capture_output=True, text=True, cwd=str(wd), env=env)
    got = _verdicts(out.stdout)
    assert got["own_write"].startswith("REFUSED own_write LoopLabSourceReadRefused"), (got, out.stderr)
    assert got["outside"].startswith("ALLOWED"), got
    assert not (wd / "out.txt").exists()


def test_run_argv_hands_the_child_its_workdir_only_beside_the_marker(tmp_path):
    """The per-launch variable rides with the fence marker and never without it, so an unfenced
    launch's env is byte-identical to what it was."""
    probe = f"import os; print(os.environ.get({read_fence.WORKDIR_ENV!r}, 'ABSENT'))"
    rc, out, _err, _to = run_argv([sys.executable, "-c", probe], str(tmp_path), 60.0)
    assert out.strip() == "ABSENT"
    run_dir, wd, _sib = _world(tmp_path)
    fence = _install(run_dir)
    rc, out, _err, _to = _run(probe, wd, fence)
    assert Path(out.strip()) == wd.resolve()


def test_warn_lets_the_write_through_and_records_it(tmp_path):
    """The honest rung for one run while an operator finds out what their pipeline writes: the
    write lands, one line per distinct (event, path) reaches stderr and the fence's diagnostic."""
    run_dir, wd, _sib = _world(tmp_path)
    fence = _install(run_dir, policy="warn")
    target = str(run_dir / "scratch.txt")
    rc, out, err, _to = _run(f"""
        for _ in range(3):
            attempt("write", lambda: open({target!r}, "a").write("x"))
        """, wd, fence)
    assert rc == 0 and _verdicts(out)["write"].startswith("ALLOWED")
    assert (run_dir / "scratch.txt").read_text() == "xxx"
    assert err.count("LOOPLAB READ FENCE (warn)") == 1, err
    rows = read_fence.violations(run_dir)
    assert len(rows) == 1 and target in rows[0] and rows[0].startswith("warn")


def test_a_fence_with_roots_and_no_record_is_the_probe_shape_and_stays_inert_about_writes(tmp_path):
    """`render` with no record root is what `tools/dev_probe.py` builds (its own no-write rules are
    the kernel's and the rlimit's): the record branch must be absent from that fence entirely."""
    src = read_fence.render([str(tmp_path / "src") + os.sep], [], policy="deny",
                            log="", run="developer-probe")
    assert "_RECORD = ''" in src and "_WRITABLE = ()" in src
    assert read_fence.install(tmp_path / "run", roots=[], allow=[], policy="deny", record=False) is None


# --------------------------------------------------------------------------- the kernel twin


def test_the_kernel_allow_list_grants_the_record_read_only(tmp_path):
    """`read_allowlist.derive` is the other spelling of the same rule: the run dir is `read`, the
    workdir inside it `readwrite`, and the fence directory `readwrite` only once it exists."""
    run_dir, wd, _sib = _world(tmp_path)
    grants = dict(read_allowlist.derive(workdir=str(wd), run_dir=str(run_dir)))
    assert grants[os.path.realpath(str(run_dir))] == "read"
    assert grants[os.path.realpath(str(wd))] == "readwrite"
    assert os.path.realpath(str(run_dir / read_fence.FENCE_DIRNAME)) not in grants
    _install(run_dir)
    grants = dict(read_allowlist.derive(workdir=str(wd), run_dir=str(run_dir)))
    assert grants[os.path.realpath(str(run_dir / read_fence.FENCE_DIRNAME))] == "readwrite"
    assert grants[os.path.realpath(str(run_dir))] == "read"


# --------------------------------------------------------------------------- end to end

# The candidate's eval script: it reads the run's own log to learn the next `seq`, appends a
# WELL-FORMED `node_evaluated` row naming its own node with a metric of its choosing, then prints
# the honest one. Under `off` the forged row is the node's FIRST terminal and the fold keeps it.
_FORGER = textwrap.dedent("""
    import json, os, time
    run_dir = os.path.abspath(os.path.join(os.getcwd(), "..", ".."))
    node = int(os.path.basename(os.getcwd()).split("_")[-1])
    log = os.path.join(run_dir, "events.jsonl")
    rows = [json.loads(l) for l in open(log, encoding="utf-8") if l.strip()]
    created = [r for r in rows if r["type"] == "node_created" and r["data"].get("node_id") == node]
    generation = int(created[-1]["data"].get("generation", 0)) if created else 0
    forged = {"v": 1, "seq": rows[-1]["seq"] + 1, "ts": time.time(), "type": "node_evaluated",
              "data": {"node_id": node, "generation": generation, "metric": 999.0}}
    try:
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(forged) + "\\n")
        print("FORGED")
    except Exception as exc:
        print("REFUSED", type(exc).__name__)
    print(json.dumps({"metric": 0.5}))
""")


def _git_repo(root, tracked):
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

    src = _git_repo(Path(tmp_path) / "repo", {"looplab_eval.py": _FORGER})
    task = RepoTask(id="p", direction="max", editable_path=str(src), protect=["looplab_eval.py"],
                    eval=EvalSpec(command=[sys.executable, "looplab_eval.py"],
                                  metric={"kind": "stdout_json", "key": "metric"}))
    researcher, developer = task.build_roles()
    run_dir = tmp_path / ("run-" + policy)
    engine = Engine(run_dir, task=task, researcher=researcher, developer=developer,
                    sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=1),
                    read_fence=policy)
    state = anyio.run(engine.run)
    rows = [json.loads(x) for x in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            if x.strip()]
    return state, rows, run_dir


def test_the_forged_terminal_is_recorded_unfenced_and_refused_fenced(tmp_path):
    off_state, off_rows, _rd = _repo_run(tmp_path / "off", "off")
    forged = [r for r in off_rows if r["type"] == "node_evaluated" and r["data"].get("metric") == 999.0]
    # Unfenced: the candidate's row is IN THE LOG, it is the node's first terminal, and the run's
    # best is the number the candidate chose. No failure, no violation. This is the defect.
    assert forged, "the forged row should have landed on the unfenced run"
    assert off_state.best() is not None and off_state.best().metric == pytest.approx(999.0)

    deny_state, deny_rows, run_dir = _repo_run(tmp_path / "deny", "deny")
    assert not [r for r in deny_rows if r["type"] == "node_evaluated"
                and r["data"].get("metric") == 999.0], "the forged row landed through the fence"
    best = deny_state.best()
    assert best is not None and best.metric == pytest.approx(0.5), (
        "the node must still evaluate on what it PRINTED — the refusal is of the write, not of the node")
    assert "REFUSED LoopLabSourceReadRefused" in (best.stdout_tail or "")
    rows = read_fence.violations(run_dir)
    assert rows and str(run_dir / "events.jsonl") in rows[0]
