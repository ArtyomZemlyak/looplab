"""The STAGES turn says whether the scorer is frozen (doc 69 §3.4, item 69.5).

`minionerec-backbones-v10`: the scoring command was `bash MiniOneRec/looplab/run_experiment.sh`, a
script that prepares, trains AND scores and that nothing protected (`entrypoint_candidates` reads
only the Python forms and answers [] for a shell wrapper), while the STAGES turn told all 40 phases
the command "is FIXED … the final, protected `score` stage" that reads "a trained checkpoint". Node 7
declared a `train` stage and trained twice (~8 H200-hours), nodes 6 and 8 duplicated the prep, node 17
rewrote the scorer, and one phase wrote down the contradiction ("the scorer itself does prep + train
+ eval … maybe I declare no stages … But the instructions insist").

`adapters/repo_developer.py::scorer_frozen` answers the question that turn assumed, by the write
gate's own rule; under `Settings.developer_scorer_status` the turn says so when the scorer is NOT
frozen (naming the file) or cannot be named. A frozen scorer's turn, and every turn with the flag
OFF, is the historical text byte for byte.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from looplab.adapters.repo_developer import (_shell_script, scorer_frozen, scorer_status_enabled,
                                             scorer_status_note)
from looplab.adapters.repo_task import EvalSpec, LLMRepoDeveloper, RepoTask
from looplab.core.config import LEGACY_CONFIG_SNAPSHOT_DEFAULTS, Settings, settings_from_snapshot
from looplab.core.models import Idea

_M = {"kind": "stdout_json", "key": "metric"}
_IDEA = Idea(operator="draft", params={"lr": 0.1}, rationale="a change")
_ALL = ["**/*"]
_NOTE_HEAD = " THE CODE IT RUNS IS NOT FROZEN: "


# ------------------------------------------------------------------------------------ the rule
@pytest.mark.parametrize("ev,surface,protected,answer", [
    # The incident: a shell script nothing protects.
    ({"command": ["bash", "MiniOneRec/looplab/run_experiment.sh"]}, _ALL, [],
     (False, ("MiniOneRec/looplab/run_experiment.sh",))),
    # The same script under an explicit protect entry: frozen.
    ({"command": ["bash", "MiniOneRec/looplab/run_experiment.sh"]}, _ALL,
     ["MiniOneRec/looplab/run_experiment.sh"], (True, ("MiniOneRec/looplab/run_experiment.sh",))),
    # The Python forms, joined to the eval cwd the way `_entrypoint_protect` joins them.
    ({"command": ["python", "score.py"]}, _ALL, ["score.py"], (True, ("score.py",))),
    ({"command": ["python", "score.py"]}, _ALL, [], (False, ("score.py",))),
    ({"command": ["python", "score.py"], "cwd": "./sub/"}, _ALL, ["sub/score.py"],
     (True, ("sub/score.py",))),
    ({"command": ["python", "-m", "pkg.test"]}, _ALL, [],
     (False, ("pkg/test.py", "pkg/test/__main__.py"))),
    # `-m` names two spellings and the derived protect keeps the one that exists: either freezes.
    ({"command": ["python", "-m", "pkg.test"]}, _ALL, ["pkg/test/__main__.py"],
     (True, ("pkg/test.py", "pkg/test/__main__.py"))),
    # Outside the edit surface is refused by the write tools too.
    ({"command": ["python", "score.py"]}, ["src/**"], [], (True, ("score.py",))),
    # The host scores the node from outside every editable tree.
    ({"command": ["python", "score.py"], "host_scorer": {"command": ["/opt/s.py"]}}, _ALL, [],
     (True, ())),
    # Nothing the rule can name.
    ({"command": ["my-scorer", "--x"]}, _ALL, [], (None, ())),
    ({"command": ["bash", "-c", "python score.py"]}, _ALL, [], (None, ())),
    ({"command": []}, _ALL, [], (None, ())),
    ({}, _ALL, [], (None, ())),
], ids=["incident", "protected-script", "py-protected", "py-editable", "py-cwd", "module",
        "module-protected", "outside-surface", "host-scorer", "console-script", "inline", "empty",
        "no-command"])
def test_the_rule_is_the_write_gate_s_own(ev, surface, protected, answer):
    """MUTATIONS: `any` -> `all` (the module case), drop the cwd join, drop the host branch, drop the
    shell fallback -> each flips a row."""
    assert scorer_frozen(ev, surface, protected, []) == answer


@pytest.mark.parametrize("argv,script", [
    (["bash", "run.sh"], "run.sh"),
    (["/bin/bash", "./run.sh", "--epochs", "3"], "run.sh"),
    (["sh", "-e", "run.sh"], "run.sh"),
    (["bash", "-o", "pipefail", "run.sh"], "run.sh"),
    (["bash", "--rcfile", "rc", "run.sh"], "run.sh"),
    (["bash", "--", "run.sh"], "run.sh"),
    (["bash", "--", "-run.sh"], "-run.sh"),        # after `--` a leading dash is the script's name
    (["bash", "-ec", "python s.py"], None),
    (["bash", "-s"], None),
    (["bash"], None),
    (["bash", "../run.sh"], None),
    (["bash", "/opt/run.sh"], None),
    (["python", "run.sh"], None),
    (["env", "bash", "run.sh"], None),
])
def test_the_shell_reading_is_narrow(argv, script):
    assert _shell_script(argv) == script


def test_the_sentence_speaks_only_when_the_scorer_is_not_frozen():
    assert scorer_status_note(True, ("score.py",)) == ""
    assert scorer_status_note(False, ()) == "", "an entrypoint the build authors: nothing to repeat"
    editable = scorer_status_note(False, ("run.sh",))
    assert editable.startswith(_NOTE_HEAD + "`run.sh` is inside your editable surface")
    assert "changes the MEASUREMENT itself" in editable
    assert "runs AGAIN inside the `score` stage" in editable and "possibly none" in editable
    unnamed = scorer_status_note(None, ())
    assert unnamed.startswith(" THE CODE IT RUNS MAY NOT BE FROZEN: its argv names no file")
    assert unnamed.endswith(editable[editable.index(" READ what the command runs"):])


# ------------------------------------------------------------------------------------ driven
def _repo(tmp_path: Path, *, command, protect=(), files=("run.sh", "train.py", "score.py")) -> RepoTask:
    for name in files:
        (tmp_path / name).write_text("echo prep; python train.py; python score.py\n"
                                     if name.endswith(".sh") else "print('x')\n")
    return RepoTask(id="r", goal="g", direction="max", editable_path=str(tmp_path),
                    edit_surface=_ALL, protect=list(protect),
                    eval=EvalSpec(command=list(command), metric=_M))


def _stages_turn(monkeypatch, task, **kw) -> str:
    """The STAGES phase's user turn, driven through `implement` with the model replaced."""
    import looplab.agents.agent as agent_mod
    monkeypatch.setattr(LLMRepoDeveloper, "_time_budget_note", lambda self: "")
    monkeypatch.setattr(LLMRepoDeveloper, "_gpu_footprint_note", lambda self, idea: "")
    seen: list = []

    def fake_loop(client, tools, messages, emit_spec, *, finalize, fallback, **opts):
        seen.append((emit_spec["function"]["name"], list(messages)))
        if emit_spec["function"]["name"] == "declare_stages":
            return finalize({"stages": [{"name": "train", "command": ["python", "train.py"]}]})
        return finalize({"summary": "s"})

    monkeypatch.setattr(agent_mod, "drive_tool_loop", fake_loop)
    LLMRepoDeveloper(object(), task, plan_decompose=False, **kw).implement(_IDEA)
    (turn,) = [msgs[1]["content"] for name, msgs in seen if name == "declare_stages"]
    return turn


def test_the_incident_s_turn_names_the_script_and_off_is_the_old_turn(tmp_path, monkeypatch):
    """MUTATIONS: drop the switch; skip the note; read the files off the source only (the working
    set holds them too) -> red."""
    task = _repo(tmp_path, command=["bash", "run.sh"])
    on = _stages_turn(monkeypatch, task, scorer_status=True)
    off = _stages_turn(monkeypatch, task)
    note = scorer_status_note(False, ("run.sh",))
    assert note in on and note not in off
    assert on.replace(note, "", 1) == off
    assert "The operator's SCORING command is FIXED" in off, "the historical contract sentence"


@pytest.mark.parametrize("command,protect,files", [
    (["bash", "run.sh"], ["run.sh"], ("run.sh", "train.py")),          # explicitly protected
    (["python", "score.py"], [], ("score.py", "train.py")),            # derived protection
    (["python", "score.py"], [], ("train.py",)),                       # the build authors it
], ids=["protected-script", "derived-entrypoint", "authored-entrypoint"])
def test_a_frozen_or_authored_scorer_s_turn_is_unchanged(tmp_path, monkeypatch, command, protect,
                                                         files):
    """MUTATION: drop the existence filter -> the authored entrypoint gains the sentence."""
    task = _repo(tmp_path, command=command, protect=protect, files=files)
    assert _stages_turn(monkeypatch, task, scorer_status=True) == _stages_turn(monkeypatch, task)


def test_a_command_naming_no_file_gets_the_hedged_sentence(tmp_path, monkeypatch):
    task = _repo(tmp_path, command=["my-scorer", "--quick"])
    assert scorer_status_note(None, ()) in _stages_turn(monkeypatch, task, scorer_status=True)


# ------------------------------------------------------------------------------------ the switch
def test_on_for_new_runs_off_for_a_pre_field_snapshot_and_off_at_every_constructor(tmp_path):
    assert Settings().developer_scorer_status is True
    assert LEGACY_CONFIG_SNAPSHOT_DEFAULTS["developer_scorer_status"] is False
    legacy = Settings().masked_snapshot()
    legacy.pop("developer_scorer_status")
    assert settings_from_snapshot(legacy).developer_scorer_status is False
    assert LLMRepoDeveloper.__new__(LLMRepoDeveloper)._scorer_status is False
    assert LLMRepoDeveloper(object(), _repo(tmp_path, command=["bash", "run.sh"]))._scorer_status \
        is False


def test_the_one_reader_and_the_backend_that_builds_the_developer(tmp_path):
    from looplab.agents.developer_backends import in_house_repo_developer
    assert scorer_status_enabled(SimpleNamespace()) is False
    assert scorer_status_enabled(Settings(developer_scorer_status=False)) is False
    task = _repo(tmp_path, command=["bash", "run.sh"])
    for flag in (True, False):
        settings = Settings(backend="llm", developer_scorer_status=flag)
        dev = in_house_repo_developer(task, settings, client=None, param_search=False,
                                      established=None)
        assert dev._scorer_status is flag
