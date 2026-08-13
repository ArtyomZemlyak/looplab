"""The Developer's PROBE and its boundary (F2, `looplab/tools/dev_probe.py`).

Every test here DRIVES the property rather than pinning the source (CLAUDE.md's tier 1): a refusal is
asserted by checking that the file the probe tried to create is NOT THERE, not by matching the
message — because the message is one comment away from vacuous and the effect is not. The whole point
of the module is that a probe cannot change anything, so "did it change anything" is the assertion.

These run a real interpreter per case (~100 ms each). That is deliberate: the boundary is composed of
a CPython audit hook, an `RLIMIT_FSIZE`, an inherited env var and a generated `sitecustomize`, and
none of those four is observable in-process.
"""
import os
import sys
import tempfile
from pathlib import Path

import pytest

from looplab.tools._base import RESULT_CAP, stream_tails
from looplab.tools.dev_probe import _MAX_TIMEOUT, DevProbeTools


def _probe(code, **kw):
    return DevProbeTools(timeout_s=kw.pop("timeout_s", 30), **kw).execute("run_probe", {"code": code})


@pytest.fixture()
def outside(tmp_path):
    """A directory the PROBE has no business touching — it stands in for site-packages, the run dir
    and the operator's tree all at once. Every mutation test aims here and then checks the disk."""
    d = tmp_path / "outside"
    d.mkdir()
    (d / "existing.txt").write_text("original", encoding="utf-8")
    return d


# --------------------------------------------------------------------------------- it works at all

def test_a_probe_answers_the_question_the_developer_would_otherwise_have_guessed():
    """The whole reason this exists: the Developer wrote a fake loguru rather than spend one line
    finding out the real one imports."""
    out = _probe("import json, sys; print('ok', json.dumps({'v': sys.version_info[0]}))")
    assert "exit=0" in out
    assert "ok {\"v\": 3}" in out


def test_a_probe_that_prints_nothing_says_so_rather_than_looking_like_an_empty_answer():
    out = _probe("1 + 1")
    assert "exit=0" in out and "only returns what it PRINTS" in out


def test_a_failing_probe_returns_the_programs_own_traceback_without_the_harness_frames():
    """The launcher's own frames sat in front of every failure — eight lines of a bounded result
    spent on our plumbing, reading as if the harness were what broke."""
    out = _probe("raise ValueError('boom')")
    assert "exit=1" in out and "ValueError: boom" in out
    assert "probe.py" in out                       # the program's own frame survives
    assert "runpy" not in out and "probe_launcher" not in out


# ------------------------------------------------------------------ rule 2: it cannot write. at all

def test_a_probe_cannot_create_a_file(outside):
    target = outside / "made.txt"
    out = _probe(f"open({str(target)!r}, 'w').write('x')")
    assert not target.exists(), "the probe created a file — rule 2 is not holding"
    assert "exit=0" not in out


def test_a_probe_cannot_create_a_file_through_os_open(outside):
    """`open()` is not the only spelling: the audit hook has to read `os.open`'s FLAGS, where the
    mode string it branches on is None."""
    target = outside / "made2.txt"
    out = _probe(f"import os; os.open({str(target)!r}, os.O_WRONLY | os.O_CREAT, 0o644)")
    assert not target.exists()
    assert "exit=0" not in out


def test_a_probe_cannot_truncate_or_append_to_a_file_that_already_exists(outside):
    existing = outside / "existing.txt"
    for mode in ("w", "a", "r+"):
        _probe(f"open({str(existing)!r}, {mode!r}).write('CLOBBERED')")
        assert existing.read_text(encoding="utf-8") == "original", f"mode {mode!r} got through"


def test_a_probe_cannot_delete_rename_or_mkdir(outside):
    existing = outside / "existing.txt"
    _probe(f"import os; os.remove({str(existing)!r})")
    assert existing.exists(), "the probe deleted a file"
    _probe(f"import os; os.rename({str(existing)!r}, {str(outside / 'moved.txt')!r})")
    assert existing.exists() and not (outside / "moved.txt").exists()
    _probe(f"import shutil; shutil.rmtree({str(outside)!r})")
    assert outside.exists(), "the probe removed a tree"
    _probe(f"import os; os.mkdir({str(outside / 'newdir')!r})")
    assert not (outside / "newdir").exists()


def test_a_probe_cannot_read_a_file_it_may_not_read_but_may_read_the_ones_it_may(outside):
    """Rule 2 is about WRITES only. A probe that could not read would be useless — reading is the
    whole job — so this pins that the write rule did not quietly become a read rule."""
    out = _probe(f"print(open({str(outside / 'existing.txt')!r}).read())")
    assert "exit=0" in out and "original" in out


def test_the_kernel_backstop_is_armed_independently_of_the_audit_hook():
    """RLIMIT_FSIZE 0 is what holds when the hook cannot see the write — a C extension going
    straight to the syscall, or an audit event CPython adds after this was written.

    It is NOT a superset of the hook and the module docstring says so: mutating the hook's write
    rules out while leaving this in place still lets a raw `open` create an EMPTY file and truncate
    an existing one to zero (measured — four tests above go red). The rlimit bounds CONTENT; the hook
    bounds EXISTENCE. Both, or neither claim holds."""
    out = _probe("import resource; print('FSIZE', resource.getrlimit(resource.RLIMIT_FSIZE))")
    assert "FSIZE (0, 0)" in out


# ------------------------------------------------------- rule 3: it cannot start another program...

def test_a_probe_cannot_start_another_program(outside):
    target = outside / "by_shell.txt"
    out = _probe("import subprocess; subprocess.run(['/bin/sh', '-c', "
                 f"'echo hi > {target}'])")
    assert not target.exists(), "a subprocess ran and wrote a file"
    assert "exit=0" not in out


def test_a_probe_cannot_start_another_program_through_os_system(outside):
    target = outside / "by_system.txt"
    _probe(f"import os; os.system('touch {target}')")
    assert not target.exists()


def test_a_probe_cannot_start_another_program_through_posix_spawn(outside):
    target = outside / "by_spawn.txt"
    _probe("import os; os.posix_spawn('/bin/sh', ['/bin/sh', '-c', "
           f"'touch {target}'], os.environ)")
    assert not target.exists()


# ------------------------------ ...which is what makes the SOURCE-TREE READ FENCE total on this surface

@pytest.fixture()
def fenced(tmp_path):
    """A repo spec shaped like a real one: an editable source tree holding a human's artifact, and a
    `data:` mount SOURCE inside it, which is the sanctioned read channel and must stay readable."""
    src = tmp_path / "src" / "repo"
    (src / "experiments").mkdir(parents=True)
    (src / "experiments" / "final.txt").write_text("A HUMAN'S CHECKPOINT", encoding="utf-8")
    mount = src / "datasets"
    mount.mkdir()
    (mount / "train.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    spec = {"editables": [{"name": ".", "path": str(src)}],
            "data": {"train": {"path": str(mount), "mount": True}}}
    return DevProbeTools(spec, timeout_s=30), src, mount


def test_a_probe_cannot_read_the_operators_editable_source_tree(fenced):
    tools, src, _mount = fenced
    secret = src / "experiments" / "final.txt"
    out = tools.execute("run_probe", {"code": f"print(open({str(secret)!r}).read())"})
    assert "A HUMAN'S CHECKPOINT" not in out
    assert "exit=0" not in out


def test_a_probe_cannot_reach_the_source_tree_by_running_cat(fenced):
    """THE test for this surface. The read fence is a CPython audit hook and covers nothing outside
    an interpreter, so a shell tool would defeat it entirely — `cat <source>/final.txt` returns the
    bytes and no fence ever sees the read. Rule 3 (no new program) is what closes that, and this is
    the assertion that says so: the secret must not appear in the tool result."""
    tools, src, _mount = fenced
    secret = src / "experiments" / "final.txt"
    for prog in (f"import subprocess; print(subprocess.run(['cat', {str(secret)!r}], "
                 "capture_output=True, text=True).stdout)",
                 f"import os; os.system('cat {secret}')",
                 f"import subprocess; subprocess.call(['cp', {str(secret)!r}, './stolen'])"):
        out = tools.execute("run_probe", {"code": prog})
        assert "A HUMAN'S CHECKPOINT" not in out, f"the source tree leaked through: {prog}"


def test_a_probe_may_still_read_a_declared_data_mount_inside_that_tree(fenced):
    """A mount SOURCE is legally allowed to live inside the editable tree and is the sanctioned read
    channel — 'validate the data' is half of what the operator asked for. The allow-list comes from
    the SAME `read_fence.fence_inputs` the engine's own fence uses."""
    tools, _src, mount = fenced
    out = tools.execute("run_probe", {"code": f"print(open({str(mount / 'train.csv')!r}).read())"})
    assert "exit=0" in out and "1,2" in out


def test_a_task_with_no_editable_tree_gets_no_fence_and_still_probes():
    out = _probe("print('fine')", repo_spec={})
    assert "exit=0" in out and "fine" in out


# ------------------------------------------------------- rule 4: it cannot disturb a sibling's GPU

def test_a_probe_sees_no_gpu_so_it_cannot_allocate_on_one_a_running_node_holds():
    out = _probe("import os; print('CVD=%r' % os.environ.get('CUDA_VISIBLE_DEVICES'))")
    assert "CVD=''" in out


# --------------------------------------------------------------------------- the disposable replica

class _Staged:
    """Stand-in for the live `RepoWriteTools` — only `.files` is read."""

    def __init__(self, files):
        self.files = files


def test_the_probe_runs_in_a_copy_of_what_the_developer_has_staged():
    tools = DevProbeTools(timeout_s=30,
                          staged=_Staged({"conf.py": "N = 41\n", "sub/mod.py": "X = 1\n"}))
    out = tools.execute("run_probe", {"code": "import conf; print('N+1 =', conf.N + 1)"})
    assert "exit=0" in out and "N+1 = 42" in out
    assert "2 staged file(s) replicated" in out


def test_the_replica_cannot_flow_back_into_the_build():
    """One-way by construction: the staged dict the probe was handed must be exactly what it was."""
    files = {"conf.py": "N = 41\n"}
    tools = DevProbeTools(timeout_s=30, staged=_Staged(files))
    tools.execute("run_probe", {"code": "open('conf.py', 'w').write('N = 0')"})
    assert files == {"conf.py": "N = 41\n"}


def test_the_probes_whole_world_is_deleted_when_it_returns():
    """No side effect is not a claim about intent — it is what makes the span-not-event decision
    correct, so it has to be observed."""
    before = {p for p in Path(tempfile.gettempdir()).glob("looplab-probe-*")}
    _probe("print('x')")
    _probe("raise SystemExit(3)")
    _probe("import time; time.sleep(0.05)")
    assert {p for p in Path(tempfile.gettempdir()).glob("looplab-probe-*")} == before


# ------------------------------------------------------------- the bounded output projection

def test_a_verbose_probe_is_tailed_and_still_fits_the_agent_loops_result_cap():
    out = _probe("print('L' * 500_000)")
    assert len(out) <= RESULT_CAP
    assert "truncated" in out


def test_a_verbose_stdout_cannot_push_the_traceback_out_of_the_result():
    """The failure this shape exists to prevent: a chatty probe whose stderr — the reason it failed —
    was dropped by the loop's blunt head-cut. The split is the shared `_base.stream_tails`."""
    out = _probe("import sys; print('L' * 500_000); "
                 "sys.stderr.write('MARKER-THE-REAL-ERROR\\n'); raise ValueError('boom')")
    assert "MARKER-THE-REAL-ERROR" in out or "ValueError: boom" in out
    assert len(out) <= RESULT_CAP


def test_the_two_stream_split_is_the_one_shared_rule_and_not_a_second_copy():
    """`shell_tools.run_command` and `dev_probe.run_probe` report the same shape; two copies is how
    they would come to disagree about which half of a failure survives."""
    import looplab.tools.shell_tools as sh

    assert sh._stream_tails is stream_tails


def test_a_probe_that_will_not_stop_is_killed_and_says_which_bound_it_hit():
    out = _probe("import time; time.sleep(30)", timeout_s=2)
    assert "TIMEOUT" in out and "eval stage" in out


# ------------------------------------------------------------------------------ the provider contract

def test_the_timeout_is_clamped_at_both_ends():
    assert DevProbeTools(timeout_s=10_000).timeout_s == _MAX_TIMEOUT
    assert DevProbeTools(timeout_s=0).timeout_s > 0
    assert DevProbeTools(timeout_s=-5).timeout_s > 0


def test_a_junk_tool_call_never_raises():
    t = DevProbeTools()
    assert "unknown tool" in t.execute("nope", {})
    assert t.execute("run_probe", {}).startswith("(run_probe:")
    assert t.execute("run_probe", None).startswith("(run_probe:")
    assert t.execute("run_probe", {"code": "   "}).startswith("(run_probe:")


def test_a_program_big_enough_to_be_authoring_is_refused_with_that_reason():
    """A 'probe' the size of a module is the Developer routing its authoring around the recorded
    channel — which is the one thing the read-only decision exists to prevent."""
    out = DevProbeTools().execute("run_probe", {"code": "#" * 50_000})
    assert "write_file" in out and "QUESTION" in out


def test_the_provider_speaks_the_duck_typed_tool_contract():
    t = DevProbeTools()
    specs = t.specs()
    assert [s["function"]["name"] for s in specs] == ["run_probe"]
    assert t.bind_state(None, None) is None      # optional hook, second arg required at dispatch


# ---------------------------------------------------------- it is a SPAN, not a domain event

def test_the_probe_cannot_append_a_domain_event():
    """The load-bearing decision: rules 2-4 mean a probe has no side effect, so engine invariant #3
    has nothing to gate and the probe is recorded as a `tool` span like every other Developer tool
    call. A negative pin, deliberately (CLAUDE.md): what must not come back is the TEXT — an import
    of the event store here would be someone concluding the opposite without reading why."""
    src = Path(__import__("looplab.tools.dev_probe", fromlist=["_"]).__file__).read_text("utf-8")
    body = src.split('"""', 2)[2]                 # skip the module docstring, which discusses events
    for banned in ("looplab.events", "EventStore", "store.append", "EV_"):
        assert banned not in body, f"{banned!r} appeared in dev_probe's code"


# --------------------------------------------------------------------- the Developer-side wiring

def _developer(probe: bool):
    from looplab.adapters.repo_developer import LLMRepoDeveloper

    dev = LLMRepoDeveloper.__new__(LLMRepoDeveloper)
    dev._probe = probe
    dev._probe_repo_spec = {}
    dev._probe_timeout_s = 30.0
    dev._cross_run_read_tools = False
    dev._editables = []
    return dev


def test_the_probe_reaches_every_developer_phase_through_the_one_composition_point():
    """`_scout_tools` is what all four phases (stages / plan / step / single-session) compose, so a
    probe added there reaches the read-only phases too — a stages phase that cannot check whether a
    library imports declares a pipeline around a library that is not there."""
    names = [s["function"]["name"] for t in _developer(True)._scout_tools(None) for s in t.specs()]
    assert "run_probe" in names
    assert [s["function"]["name"] for t in _developer(False)._scout_tools(None) for s in t.specs()] == []


def test_the_setting_off_restores_the_previous_system_prompt_byte_for_byte():
    """An operator turning the probe off must get the run they had before this shipped, prompt
    included — which is why the clause is spliced at its original position, not appended."""
    import looplab.adapters.repo_developer as rd
    from looplab.core.prompts import render

    off, on = _developer(False), _developer(True)
    off.prompts = on.prompts = None
    assert off._system_body(render) == rd._REPO_DEV_SYSTEM_BODY
    assert "There is NO shell / bash / run-command tool" in off._system_body(render)
    body_on = on._system_body(render)
    assert "There is NO shell / bash / run-command tool" not in body_on
    assert "PROBE BEFORE YOU WORK AROUND SOMETHING" in body_on
    # The head and tail are shared verbatim: only the execution clause differs.
    assert body_on.startswith(rd._REPO_DEV_SYSTEM_BODY_HEAD)
    assert body_on.endswith(rd._REPO_DEV_SYSTEM_BODY_TAIL)


def test_an_operator_prompt_override_still_wins_in_both_configurations():
    from looplab.core.prompts import render

    for flag in (True, False):
        dev = _developer(flag)
        dev.prompts = {"repo_developer_system_body": "MY OWN BODY"}
        assert dev._system_body(render) == "MY OWN BODY"


@pytest.mark.skipif(os.name == "nt", reason="the boundary's kernel half is POSIX rlimits")
def test_the_engines_own_interpreter_is_what_answers():
    """`env_inspect` answers by IMPORTING in the engine's interpreter; a probe on a different one
    could contradict `pkg_info` about the same package and the Developer would have no way to tell
    which answer was about its eval."""
    out = _probe("import sys; print('EXE', sys.executable)")
    assert f"EXE {sys.executable}" in out
