"""A stage whose entry point the build never wrote costs a node two repairs and then the node.

MEASURED ON v13, which lost two of four nodes to exactly this:
    node 0  `mine`                exit 1 in 0.505 s  "No module named vectorsearch.mine_stage"
    node 2  `teacher_embeddings`  exit 1 in 0.209 s  "can't open file '.../teacher_embeddings.py'"
Each then bought TWO repair sessions of ~30 minutes — all four `inert`, all four `changed: []`,
`budget_exhausted: time` — and died. The stage cost half a second; the CASCADE cost ~2 h per node.

I REJECTED THIS CHECK TWO CYCLES AGO with "it saves 0.5 s against a 47-minute cost". That reasoned
about the stage's own duration and was wrong: the sub-second failure is what TRIGGERS the cascade.

THREE SITES WERE ELIMINATED BY THE TREE'S OWN REASONING before this one:
  * submit time — `repo_task.eval_entrypoint_unprotected` is DELIBERATELY silent on a resolvable
    but absent entrypoint, because "the Developer AUTHORS the eval entrypoint" is the designed flow;
  * the `declare_stages` tool — the stages phase runs with READ-ONLY tools, so the script cannot
    exist yet when the manifest is declared;
  * the stage runner — too late: the node exists and the repair cascade is the cost.
Only the IMPLEMENT emit has both the manifest and the final file ledger.
"""
from __future__ import annotations

import json

import pytest
import sys
from pathlib import Path

from looplab.core.models import Idea

from looplab.adapters.repo_task import entrypoint_candidates
from looplab.engine.repair_verify import build_declared_script_never_written


def _manifest(*commands):
    return json.dumps({"stages": [{"name": "s%d" % i, "command": c}
                                  for i, c in enumerate(commands)]})


def test_the_v13_case_is_refused():
    out = build_declared_script_never_written(
        _manifest(["python", "teacher_embeddings.py"]), {"train.py": "x"})
    assert "teacher_embeddings.py" in out
    assert "does not exist in the workspace" in out


def test_a_script_the_session_DID_write_is_left_alone():
    assert build_declared_script_never_written(
        _manifest(["python", "teacher_embeddings.py"]),
        {"teacher_embeddings.py": "print(1)\n"}) == ""


def test_the_module_form_is_NEVER_refused():
    """`python -m vectorsearch.mine_stage` and `python -m pytest` are IDENTICAL to the resolver —
    two candidates each, neither local — because that is how INSTALLED code looks. Refusing here
    would reject every legitimate installed-module stage, which is why `eval_stages` already treats
    the form as opaque. v13 node 0 is therefore NOT caught, and that is the correct trade."""
    for mod in ("vectorsearch.mine_stage", "pytest", "torch.distributed.run"):
        assert build_declared_script_never_written(
            _manifest(["python", "-m", mod]), {}) == ""


def test_the_contract_this_rule_leans_on_is_asserted_here():
    """`len(cands) == 1` IS the script form, per `entrypoint_candidates`' documented contract: it
    returns BOTH `-m` spellings and exactly one path for a script. If that contract ever changes,
    this rule silently widens — so the contract is pinned where the rule can see it."""
    assert len(entrypoint_candidates(["python", "-m", "pkg.mod"])) == 2
    assert len(entrypoint_candidates(["python", "score.py"])) == 1


def test_an_opaque_command_is_left_alone():
    """`python -c`, a launcher whose flag grammar decides which token is the script, a program found
    on PATH — the resolver answers [] and this rule must not invent a target.

    `["bash", "run.sh"]` and `["./scorer"]` USED to be in this list and were moved out on 2026-09-23
    (`shell_script_candidate`): for a shell the first positional token IS the file executed, and a
    relative argv[0] IS the program — neither is a guess. `minionerec-backbones-v3` node 0 paid exit
    127 plus two triage+repair cycles for a `bash …/prepare_cache.sh` no session wrote, which this
    list waved through. `bash -c` stays opaque (see the truth table below)."""
    for cmd in (["python", "-c", "print(1)"], ["bash", "-c", "python train.py"],
                ["torchrun", "--nproc_per_node", "2", "score.py"], ["scorer"]):
        assert build_declared_script_never_written(_manifest(cmd), {}) == ""
    assert "run.sh" in build_declared_script_never_written(_manifest(["bash", "run.sh"]), {})


def test_several_missing_scripts_are_all_named_but_bounded():
    cmds = [["python", "s%d.py" % i] for i in range(9)]
    out = build_declared_script_never_written(_manifest(*cmds), {})
    assert "s0.py" in out and "s5.py" in out
    assert "s6.py" not in out, "the message names at most six"


def test_a_malformed_manifest_answers_rather_than_raising():
    """It runs inside an emit path that has already cost minutes; a broken manifest is a different
    rung's problem and must not become an exception here."""
    for bad in ("", "not json", "[]", json.dumps({"stages": "nope"}),
                json.dumps({"stages": [None, 7, {"command": None}]})):
        assert build_declared_script_never_written(bad, {}) == ""


def test_the_bounce_names_the_cost_so_the_model_can_act():
    out = build_declared_script_never_written(_manifest(["python", "a.py"]), {})
    assert "before emitting" in out
    assert "repair" in out


def _fresh_repo_dev(monkeypatch, *, stages, writes, plan_steps=()):
    """Drive a REAL `LLMRepoDeveloper.implement()` over the real repo fixture, with only the
    documented `looplab.agents.agent.drive_tool_loop` seam faked — and, unlike the shared harness in
    `test_repo_dev_plan.py`, this fake CALLS the `validate` it is handed, which is the whole
    question. Returns the refusals the emit path produced."""
    import looplab.agents.agent as agent_mod

    refusals: list = []

    def fake_loop(client, tools, messages, emit_spec, *, finalize, fallback, **opts):
        name = emit_spec["function"]["name"]
        if name == "declare_stages":
            return finalize({"stages": list(stages)})
        if name == "propose_plan":
            return finalize({"steps": list(plan_steps)})
        for path, body in (writes or {}).items():
            tools.execute("write_file", {"path": path, "content": body})
        args = {"summary": "built it"}
        validate = opts.get("validate")
        if validate is not None and (refusal := validate(args)):
            refusals.append(refusal)
        return finalize(args)

    monkeypatch.setattr(agent_mod, "drive_tool_loop", fake_loop)

    from looplab.adapters.repo_task import EvalSpec, LLMRepoDeveloper, RepoTask
    fixture = Path(__file__).resolve().parent / "fixtures" / "repo_fixture"
    task = RepoTask(id="r", goal="g", direction="max", editable_path=str(fixture),
                    edit_surface=["*.py"], protect=[],
                    eval=EvalSpec(command=[sys.executable, "ttrain.py"],
                                  metric={"kind": "stdout_json", "key": "metric"}))
    dev = LLMRepoDeveloper(object(), task,
                           plan_decompose=bool(plan_steps), plan_min_steps=2)
    dev.implement(Idea(operator="draft", params={}, rationale="x"))
    return refusals


def test_THE_REAL_BUILD_PATH_BOUNCES_A_DECLARED_SCRIPT_THAT_IS_NOT_THERE(monkeypatch):
    """DRIVEN, not pinned — and the pin is why this test exists in this shape.

    The rule first shipped inside `_validate_repair`'s `if not error:` arm, which is only installed
    on the `else:` of `if is_fresh_repo:`. Since `is_fresh_repo = error is None and self._editables`,
    that arm needed `error is None AND no editables` — a toy/bare developer with no repo, i.e. NEVER
    a repo build. The v13 cascade it was measured against was therefore untouched.

    Its guard was `inspect.getsource` + three `src.index()` lookups in order, which CLAUDE.md records
    verbatim as unable to prove a call executes — and here it proved the opposite of the truth: the
    ordering it pinned was inside the dead branch.
    """
    refusals = _fresh_repo_dev(
        monkeypatch,
        stages=[{"name": "train", "command": ["python", "vectorsearch/mine_stage.py"]}],
        writes={"solution.py": "print(1)\n"})
    assert refusals, "a repo build declaring an absent script must be bounced"
    assert "vectorsearch/mine_stage.py" in refusals[0]


def test_the_LAST_PLAN_STEP_carries_the_shot(monkeypatch):
    """`developer_plan_decompose` defaults to True, so a repo build with >=2 steps takes the PLAN
    sub-path and never reaches the single-session implement. A guard installed on only one of the
    two fresh-repo sub-paths is the same defect one branch further in."""
    refusals = _fresh_repo_dev(
        monkeypatch,
        stages=[{"name": "train", "command": ["python", "absent_trainer.py"]}],
        writes={"solution.py": "print(1)\n"},
        plan_steps=[{"title": "A", "detail": "a"}, {"title": "B", "detail": "b"}])
    assert refusals, "the plan path must bounce too"
    assert "absent_trainer.py" in refusals[0]


def test_a_stage_naming_a_COMMITTED_repo_script_is_LEFT_ALONE(monkeypatch):
    """THE FALSE POSITIVE the ledger test produced, and the reason `exists` had to replace it.

    `RepoWriteTools.files` starts EMPTY and holds only what THIS session authored — its own
    docstring says writes are "COLLECTED ... rather than applied to disk" — while the node runs in
    the repo COPY plus that overlay. A Developer declaring the repo's own committed trainer and
    editing only a config had it called "a script this session never wrote": factually wrong, it
    spends the session's single shared bounce, and the advice ("write the file") invites the model
    to overwrite a working trainer with a stub.
    """
    refusals = _fresh_repo_dev(
        monkeypatch,
        stages=[{"name": "train", "command": ["python", "ttrain.py"]}],   # committed in the fixture
        writes={"config.json": '{"lr": 0.1}\n'})
    assert refusals == [], f"a committed repo script is not missing: {refusals}"


def test_the_rule_still_answers_the_LEDGER_when_no_workspace_view_is_offered():
    """`exists` is optional so the rule stays a pure function — and the fallback is the WEAKER
    reading, which is why a caller that CAN answer must pass it."""
    assert build_declared_script_never_written(_manifest(["python", "a.py"]), {}) != ""
    assert build_declared_script_never_written(
        _manifest(["python", "a.py"]), {}, exists=lambda _p: True) == ""


def test_an_unreadable_workspace_is_not_evidence_of_ABSENCE():
    """It runs inside an emit path that has already cost minutes, and a raising `exists` says
    nothing about the file. Fail toward silence: a false bounce spends the one shot and misdirects
    the model, while a missed one costs what it always cost."""
    def _boom(_p):
        raise OSError("stat failed")

    assert build_declared_script_never_written(
        _manifest(["python", "a.py"]), {}, exists=_boom) == ""


def test_the_one_shot_is_SHARED_with_the_repair_rung():
    """`_bounced` is one list for both paths on purpose: a session gets ONE bounce, whichever rung
    fires. Two independent shots would spend the session arguing instead of editing — the reason
    the repair rung states for being one-shot in the first place.

    It is HOISTED above the `is_fresh_repo` split, because the build rung now runs on the fresh-repo
    path (see the driving tests above) while the repair rung runs on the other side of it. A list
    declared inside either branch would be two lists."""
    import inspect

    from looplab.adapters import repo_developer
    src = inspect.getsource(repo_developer)
    assert src.count("_bounced: list = []") == 1, "one shot per session, not one per rung"
    assert src.count("_bounced.append(True)") == 2, "both rungs SPEND it"
    assert src.count("if _bounced:") == 2, "and both rungs check it before firing"


def test_the_build_rung_SPENDS_the_shot_and_does_not_fire_twice(monkeypatch):
    """Driven, because "one shot" is a runtime property and a source count cannot see it: a second
    bounce would spend the session arguing instead of editing."""
    import looplab.agents.agent as agent_mod

    seen: list = []

    def fake_loop(client, tools, messages, emit_spec, *, finalize, fallback, **opts):
        name = emit_spec["function"]["name"]
        if name == "declare_stages":
            return finalize({"stages": [{"name": "train", "command": ["python", "gone.py"]}]})
        args = {"summary": "built it"}
        validate = opts.get("validate")
        if validate is not None:
            seen.append(validate(args))
            seen.append(validate(args))          # the model emits again after the bounce
        return finalize(args)

    monkeypatch.setattr(agent_mod, "drive_tool_loop", fake_loop)

    from looplab.adapters.repo_task import EvalSpec, LLMRepoDeveloper, RepoTask
    fixture = Path(__file__).resolve().parent / "fixtures" / "repo_fixture"
    task = RepoTask(id="r", goal="g", direction="max", editable_path=str(fixture),
                    edit_surface=["*.py"], protect=[],
                    eval=EvalSpec(command=[sys.executable, "ttrain.py"],
                                  metric={"kind": "stdout_json", "key": "metric"}))
    dev = LLMRepoDeveloper(object(), task, plan_decompose=False)
    dev.implement(Idea(operator="draft", params={}, rationale="x"))

    assert seen and seen[0] and "gone.py" in seen[0], "the first emit is bounced"
    assert seen[1] is None, "and the second is not — the shot is spent"


def _gap_build(monkeypatch, *, write_in_gap: bool):
    """A real `implement()` whose plan steps never write the declared script (the v3 node-0 shape:
    every step ended on its budget). Returns `(developer, gap_sessions)`."""
    import looplab.agents.agent as agent_mod

    gap_sessions: list = []
    gap_title = "Write the script(s) the stage manifest runs"

    def fake_loop(client, tools, messages, emit_spec, *, finalize, fallback, **opts):
        name = emit_spec["function"]["name"]
        if name == "declare_stages":
            return finalize({"stages": [{"name": "prep", "command": ["bash", "prep_cache.sh"]}]})
        if name == "propose_plan":
            return finalize({"steps": [{"title": "A", "detail": "a"}, {"title": "B", "detail": "b"}]})
        user = str(messages[-1].get("content", ""))
        if gap_title in user:
            gap_sessions.append(user)
            if write_in_gap:
                tools.execute("write_file", {"path": "prep_cache.sh", "content": "echo ok\n"})
        else:
            tools.execute("write_file", {"path": "solution.py", "content": "print(1)\n"})
        return finalize({"summary": "built it"})

    monkeypatch.setattr(agent_mod, "drive_tool_loop", fake_loop)
    from looplab.adapters.repo_task import EvalSpec, LLMRepoDeveloper, RepoTask
    fixture = Path(__file__).resolve().parent / "fixtures" / "repo_fixture"
    task = RepoTask(id="r", goal="g", direction="max", editable_path=str(fixture),
                    edit_surface=["*"], protect=[],
                    eval=EvalSpec(command=[sys.executable, "ttrain.py"],
                                  metric={"kind": "stdout_json", "key": "metric"}))
    dev = LLMRepoDeveloper(object(), task, plan_decompose=True, plan_min_steps=2)
    dev.implement(Idea(operator="draft", params={}, rationale="x"))
    return dev, gap_sessions


def test_a_build_that_ENDS_without_the_declared_script_gets_ONE_session_to_write_it(monkeypatch):
    """minionerec-backbones-v3 node 0 (2026-09-23): the last step ran out of budget, so the one-shot
    bounce had no turn to buy and the build shipped `prepare_cache.sh` undeclared-but-unwritten —
    exit 127, then a triage and a repair that each ran out of time too. Mutation: drop the
    `_close_declared_script_gap` call (no gap session; the script is never shipped)."""
    dev, gaps = _gap_build(monkeypatch, write_in_gap=True)
    assert len(gaps) == 1 and "prep_cache.sh" in gaps[0], gaps
    assert "prep_cache.sh" in dev.last_files, "the gap session's script must ship with the build"


def test_the_gap_session_is_spent_at_most_once(monkeypatch):
    """If the focused session also leaves the gap, the build proceeds as before — no loop."""
    dev, gaps = _gap_build(monkeypatch, write_in_gap=False)
    assert len(gaps) == 1
    assert "prep_cache.sh" not in dev.last_files


def test_no_gap_session_when_the_declared_script_exists(monkeypatch):
    """The committed-script case must not buy a session: `exists` sees the repo, not just the ledger."""
    refusals = _fresh_repo_dev(
        monkeypatch, stages=[{"name": "train", "command": ["python", "written.py"]}],
        writes={"written.py": "print(1)\n"})
    assert not refusals


@pytest.mark.parametrize("command, expected", [
    (["bash", "MiniOneRec/looplab/prepare_cache.sh"], ["MiniOneRec/looplab/prepare_cache.sh"]),
    (["sh", "-e", "run.sh", "--fast"], ["run.sh"]),
    (["/bin/bash", "./tools/run.sh"], ["tools/run.sh"]),
    (["./run.sh"], ["run.sh"]),
    (["tools/run.sh", "x"], ["tools/run.sh"]),
    (["bash", "-c", "python train.py"], []),          # the program is a string, not a file
    (["bash", "-ec", "make"], []),
    (["bash", "/opt/run.sh"], []),                    # absolute: outside the workdir question
    (["bash", "../escape.sh"], []),
    (["bash", "$SCRIPT"], []),
    (["python", "train.py"], []),                     # the python form is entrypoint_candidates'
    (["make", "train"], []),
    ([], []),
    (None, []),
])
def test_the_shell_form_truth_table(command, expected):
    from looplab.engine.repair_verify import shell_script_candidate
    assert shell_script_candidate(command) == expected


def test_the_v3_node0_manifest_is_refused():
    """The exact manifest that cost v3 node 0 two triage+repair cycles."""
    manifest = _manifest(["bash", "MiniOneRec/looplab/prepare_cache.sh"])
    refusal = build_declared_script_never_written(manifest, {}, exists=lambda _p: False)
    assert "MiniOneRec/looplab/prepare_cache.sh" in refusal
    assert not build_declared_script_never_written(manifest, {}, exists=lambda _p: True)


# ------------------------------------------------------------ the same shot for a declared marker

def _marker_gap_build(monkeypatch, *, write_in_gap: bool):
    """A real `implement()` whose steps declare an activation marker and never write the code that
    prints it (MiniOneRec inf12 node 4, 2026-09-24). Returns `(developer, gap_sessions)`."""
    import looplab.agents.agent as agent_mod

    gap_sessions: list = []
    gap_title = "Write the new code path this build declares but never wrote"

    def fake_loop(client, tools, messages, emit_spec, *, finalize, fallback, **opts):
        name = emit_spec["function"]["name"]
        if name == "declare_stages":
            return finalize({"stages": []})
        if name == "propose_plan":
            return finalize({"steps": [{"title": "A", "detail": "a"}, {"title": "B", "detail": "b"}]})
        user = str(messages[-1].get("content", ""))
        if gap_title in user:
            gap_sessions.append(user)
            if write_in_gap:
                tools.execute("write_file", {"path": "dedup.py",
                                             "content": "print('DEDUP_STEP1_ACTIVE')\n"})
            return finalize({"summary": "wrote it"})
        return finalize({"summary": "planned it", "activation_markers": ["DEDUP_STEP1_ACTIVE"]})

    monkeypatch.setattr(agent_mod, "drive_tool_loop", fake_loop)
    from looplab.adapters.repo_task import EvalSpec, LLMRepoDeveloper, RepoTask
    fixture = Path(__file__).resolve().parent / "fixtures" / "repo_fixture"
    task = RepoTask(id="r", goal="g", direction="max", editable_path=str(fixture),
                    edit_surface=["*"], protect=[],
                    eval=EvalSpec(command=[sys.executable, "ttrain.py"],
                                  metric={"kind": "stdout_json", "key": "metric"}))
    dev = LLMRepoDeveloper(object(), task, plan_decompose=True, plan_min_steps=2)
    dev.implement(Idea(operator="draft", params={}, rationale="dedupe the first decode step"))
    return dev, gap_sessions


def test_a_build_that_ENDS_with_its_marker_unwritten_gets_ONE_session_to_write_it(monkeypatch):
    """Mutation: drop the `_close_declared_marker_gap` call — no session, the path never ships."""
    dev, gaps = _marker_gap_build(monkeypatch, write_in_gap=True)
    assert len(gaps) == 1 and "DEDUP_STEP1_ACTIVE" in gaps[0], gaps
    assert "dedupe the first decode step" in gaps[0], "the session is told what the idea is"
    assert "dedup.py" in dev.last_files


def test_the_marker_gap_session_is_spent_at_most_once_and_keeps_the_declaration(monkeypatch):
    dev, gaps = _marker_gap_build(monkeypatch, write_in_gap=False)
    assert len(gaps) == 1
    assert "DEDUP_STEP1_ACTIVE" in dev.last_files.get("looplab_activation.json", ""), (
        "withdrawing the marker would credit the parent's metric to an idea that never ran")
