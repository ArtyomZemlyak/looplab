"""The Developer's writing sessions get a MEASUREMENT between plan steps (doc 53 item 10, our half).

The engine already evaluates every node it builds — `node_created`:`node_evaluated` is 1:1 in every
AlgoTune model probe. What no session that WRITES code ever saw was a number: the parent block that
carries `metric=` reaches only the single-session fallback (0 hits in 1,055 `plan_step` generations
across the probe corpus), and the operator's evaluation command was the model's own to call. Its
answer to that was to spend a whole bounded session pressing the button: 30 of 116 attributed plan
steps are titled as a measurement and nothing else, 21 of them wrote no file at all, together 317
LLM calls and 5,762 s for a command that takes 40 s.

So the engine runs the operator-pinned command BETWEEN steps and hands the result to the next one.
These cases pin the four things that has to be true of it: it runs (and only where it can be read),
it stays OFF unless the operator names a command the task actually pins, the writing sessions are
told what the parent measured, and NOTHING it produces can reach the node's files — the ruler stays
`engine/evaluate.py`'s.
"""
from __future__ import annotations

import sys
from pathlib import Path

from looplab.core.models import Idea, Node
from looplab.adapters.repo_task import EvalSpec, RepoTask
from looplab.adapters.repo_developer import LLMRepoDeveloper
from looplab.tools.dev_commands import DeveloperCommandRuntime

# Prints a line shaped like the arena's own feedback, stamps the LAST line of the staged solver.py
# (so a test can prove the command saw THIS step's edit, not the pristine source), counts its own
# invocations in an absolute file outside the candidate, and drops a file in its cwd so the
# disposable-workspace property is observable rather than assumed.
_MEASURE = '''\
import pathlib, sys
counter = pathlib.Path(sys.argv[1])
n = (int(counter.read_text()) if counter.exists() else 0) + 1
counter.write_text(str(n))
solver = pathlib.Path("solver.py")
tail = solver.read_text().strip().splitlines()[-1] if solver.exists() else "(no solver)"
pathlib.Path("leaked.py").write_text("# written by the command into its own workspace\\n")
print("Speedup: " + str(n) + ".0 | Valid Solutions: 100% | Invalid: 0 | Timeouts: 0")
print("staged=" + tail)
'''


def _task(root: Path, *, command_name="eval_train"):
    root.mkdir(parents=True, exist_ok=True)
    (root / "main.py").write_text("print('source')\n", encoding="utf-8")
    (root / "measure.py").write_text(_MEASURE, encoding="utf-8")
    return RepoTask(id="r", goal="g", direction="max", editable_path=str(root),
                    edit_surface=["*.py"], protect=[],
                    eval=EvalSpec(command=[sys.executable, "main.py"]),
                    developer_commands=[{"name": command_name,
                                         "command": [sys.executable, "measure.py",
                                                     str(root.parent / "calls.txt")]}])


def _dev(root: Path, **kw):
    kw.setdefault("plan_decompose", True)
    kw.setdefault("plan_min_steps", 2)
    kw.setdefault("command_runtime", DeveloperCommandRuntime(seed_mode="all"))
    return LLMRepoDeveloper(object(), _task(root), **kw)


def _install_fake_loop(monkeypatch, n_steps, capture):
    """Plan `n_steps` steps; every step session rewrites solver.py with its own marker line."""
    import looplab.agents.agent as agent_mod

    def fake_loop(client, tools, messages, emit_spec, *, finalize, fallback, **opts):
        name = emit_spec["function"]["name"]
        capture.append({"name": name, "messages": list(messages)})
        if name == "declare_stages":
            return finalize({"stages": [{"name": "train", "command": ["python", "main.py"]}]})
        if name == "propose_plan":
            return finalize({"steps": [{"title": f"S{i}", "detail": f"d{i}"}
                                       for i in range(1, n_steps + 1)]})
        idx = sum(1 for c in capture if c["name"] == "done")
        tools.execute("write_file", {"path": "solver.py", "content": f"X = {idx}\n# step {idx}\n"})
        return finalize({"summary": f"wrote step {idx}"})

    monkeypatch.setattr(agent_mod, "drive_tool_loop", fake_loop)


def _steps(capture):
    return [c["messages"][-1]["content"] for c in capture if c["name"] == "done"]


def _plan_turn(capture):
    return next(c["messages"][-1]["content"] for c in capture if c["name"] == "propose_plan")


# ---------------------------------------------------------------- it runs, and it is READ
def test_the_pinned_command_runs_between_steps_and_its_output_reaches_the_next_step(monkeypatch, tmp_path):
    cap: list = []
    _install_fake_loop(monkeypatch, 3, cap)
    dev = _dev(tmp_path / "repo", step_feedback_command="eval_train")
    dev.implement(Idea(operator="draft", params={}, rationale="a three-part change"))

    counter = tmp_path / "calls.txt"
    # After steps 1 and 2 — NOT after step 3, whose only reader would be the engine's own evaluation.
    assert counter.exists() and counter.read_text() == "2"

    s1, s2, s3 = _steps(cap)
    assert "MEASUREMENT OF THE WORK SO FAR" not in s1          # nothing has been edited yet
    assert "MEASUREMENT OF THE WORK SO FAR" in s2 and "MEASUREMENT OF THE WORK SO FAR" in s3
    # the arena-shaped numbers land verbatim, and each step sees ITS OWN predecessor's edit
    assert "Speedup: 1.0" in s2 and "Valid Solutions: 100%" in s2 and "staged=# step 1" in s2
    assert "Speedup: 2.0" in s3 and "staged=# step 2" in s3
    # ... and step 3 is not shown step 1's stale number
    assert "Speedup: 1.0" not in s3


def test_a_step_that_changes_nothing_buys_no_measurement(monkeypatch, tmp_path):
    """The command costs ~40 s of wall clock (n=76, median 39.6 s); a step that wrote nothing has
    nothing new to measure, and 36 % of the corpus's plan steps write nothing."""
    import looplab.agents.agent as agent_mod
    cap: list = []

    def fake_loop(client, tools, messages, emit_spec, *, finalize, fallback, **opts):
        name = emit_spec["function"]["name"]
        cap.append({"name": name, "messages": list(messages)})
        if name == "declare_stages":
            return finalize({"stages": [{"name": "train", "command": ["python", "main.py"]}]})
        if name == "propose_plan":
            return finalize({"steps": [{"title": f"S{i}", "detail": f"d{i}"} for i in (1, 2, 3)]})
        idx = sum(1 for c in cap if c["name"] == "done")
        if idx != 2:                                              # step 2 writes NOTHING
            tools.execute("write_file", {"path": "solver.py", "content": f"X = {idx}\n# step {idx}\n"})
        return finalize({"summary": f"step {idx}"})

    monkeypatch.setattr(agent_mod, "drive_tool_loop", fake_loop)
    dev = _dev(tmp_path / "repo", step_feedback_command="eval_train")
    dev.implement(Idea(operator="draft", params={}, rationale="x"))
    # step 1 wrote -> one measurement; step 2 wrote nothing -> none; step 3 is last -> none
    assert (tmp_path / "calls.txt").read_text() == "1"
    s1, s2, s3 = _steps(cap)
    assert "Speedup: 1.0" in s2 and "MEASUREMENT OF THE WORK SO FAR" not in s3


# ---------------------------------------------------------------- OFF unless the operator says so
def test_off_by_default_and_the_step_prompt_is_unchanged(monkeypatch, tmp_path):
    cap: list = []
    _install_fake_loop(monkeypatch, 3, cap)
    dev = _dev(tmp_path / "repo")                                # no step_feedback_command
    dev.implement(Idea(operator="draft", params={}, rationale="x"))
    assert not (tmp_path / "calls.txt").exists()                 # the command never ran
    assert all("MEASUREMENT OF THE WORK SO FAR" not in s for s in _steps(cap))
    assert "Do NOT plan a step whose only job is to measure" not in _plan_turn(cap)


def test_a_name_the_task_does_not_pin_is_silence_not_an_invented_command(monkeypatch, tmp_path):
    cap: list = []
    _install_fake_loop(monkeypatch, 3, cap)
    dev = _dev(tmp_path / "repo", step_feedback_command="not_pinned_here")
    dev.implement(Idea(operator="draft", params={}, rationale="x"))
    assert not (tmp_path / "calls.txt").exists()
    assert all("MEASUREMENT OF THE WORK SO FAR" not in s for s in _steps(cap))
    # and the planner is not told about a measurement it will never be handed
    assert "Do NOT plan a step whose only job is to measure" not in _plan_turn(cap)


def test_the_planner_is_told_the_measurement_is_free(monkeypatch, tmp_path):
    """26 % of the corpus's plan steps exist only to press this button. A planner that does not know
    the engine presses it will keep buying it with a whole session."""
    cap: list = []
    _install_fake_loop(monkeypatch, 3, cap)
    dev = _dev(tmp_path / "repo", step_feedback_command="eval_train")
    dev.implement(Idea(operator="draft", params={}, rationale="x"))
    plan = _plan_turn(cap)
    assert "Do NOT plan a step whose only job is to measure" in plan
    assert "eval_train" in plan


# ---------------------------------------------------------------- the parent's number reaches the writers
def test_the_writing_sessions_are_told_what_the_parent_measured(monkeypatch, tmp_path):
    """`implement_from` has always computed `parent experiment #N, metric=M` and put it in a block
    the plan path never sends: 0 of 1,055 `plan_step` generations in the probe corpus carry it."""
    cap: list = []
    _install_fake_loop(monkeypatch, 2, cap)
    dev = _dev(tmp_path / "repo")
    parent = Node(id=7, operator="improve", idea=Idea(operator="improve", params={}),
                  code="", files={"solver.py": "BASE = 1\n"}, metric=0.81)
    dev.implement_from(Idea(operator="improve", params={}, rationale="patch it"), parent)
    for turn in _steps(cap) + [_plan_turn(cap)]:
        assert "MEASURED STARTING POINT" in turn
        assert "parent experiment #7" in turn and "metric=0.81" in turn


def test_a_fresh_build_has_no_starting_point_and_says_nothing(monkeypatch, tmp_path):
    cap: list = []
    _install_fake_loop(monkeypatch, 2, cap)
    dev = _dev(tmp_path / "repo")
    dev.implement(Idea(operator="draft", params={}, rationale="from scratch"))
    assert all("MEASURED STARTING POINT" not in t for t in _steps(cap) + [_plan_turn(cap)])


# ---------------------------------------------------------------- the ruler stays the engine's
def test_the_measurement_is_a_prompt_input_and_can_never_become_a_node_file(monkeypatch, tmp_path):
    """The hard requirement: a number produced by this cheap in-session run must not be able to
    reach the reported speedup or the champion. It cannot, structurally — the command runs in a
    disposable candidate tree that is deleted on return, so nothing it writes (here `leaked.py`)
    comes back, and its stdout only ever becomes prompt text."""
    cap: list = []
    _install_fake_loop(monkeypatch, 3, cap)
    dev = _dev(tmp_path / "repo", step_feedback_command="eval_train")
    out = dev.implement(Idea(operator="draft", params={}, rationale="x"))

    assert out == ""                                             # no sentinel: the files are the answer
    assert (tmp_path / "calls.txt").read_text() == "2"           # it really ran
    assert set(dev.last_files) == {"looplab_stages.json", "solver.py"}
    assert "leaked.py" not in dev.last_files                     # the command's own writes stay behind
    assert not (tmp_path / "repo" / "leaked.py").exists()        # and never touch the source tree
    assert all("Speedup" not in body for body in dev.last_files.values())
    assert dev.last_files["solver.py"] == "X = 3\n# step 3\n"    # the last step's edit, unperturbed
