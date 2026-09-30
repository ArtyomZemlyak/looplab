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

from looplab.adapters.repo_developer import (_argv_after_env, _exec_path, _shell_script,
                                             scorer_frozen, scorer_status_enabled,
                                             scorer_status_note, scorer_system_clause)
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
    # critic 2026-09-30 — the exact-membership rule is case-sensitive, like the write tools'.
    ({"command": ["python", "score.py"]}, _ALL, ["Score.py"], (False, ("score.py",))),
    # A non-canonical spelling names the file a write reaches.
    ({"command": ["python", "sub/./score.py"]}, _ALL, ["sub/score.py"], (True, ("sub/score.py",))),
    ({"command": ["python", "sub//score.py"]}, _ALL, [], (False, ("sub/score.py",))),
    ({"command": ["python", "score.py"], "cwd": "sub\\"}, _ALL, ["sub/score.py"],
     (True, ("sub/score.py",))),
    # An absolute cwd is the sandbox's to remap: this rule cannot name the file.
    ({"command": ["python", "score.py"], "cwd": "/abs/repo"}, _ALL, [], (None, ())),
    # The head IS the file (`exec` from the cwd), and `env` assignments are read through.
    ({"command": ["./run.sh", "--x"]}, _ALL, [], (False, ("run.sh",))),
    ({"command": ["./run.sh"]}, _ALL, ["run.sh"], (True, ("run.sh",))),
    ({"command": ["env", "FOO=1", "bash", "run.sh"]}, _ALL, [], (False, ("run.sh",))),
    ({"command": ["bash", "-euo", "pipefail", "run.sh"]}, _ALL, [], (False, ("run.sh",))),
], ids=["incident", "protected-script", "py-protected", "py-editable", "py-cwd", "module",
        "module-protected", "outside-surface", "host-scorer", "console-script", "inline", "empty",
        "no-command", "case", "dot-spelling", "double-slash", "backslash-cwd", "absolute-cwd",
        "exec-path", "exec-path-protected", "env-shell", "strict-mode-cluster"])
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
    (["env", "bash", "run.sh"], "run.sh"),
    # critic 2026-09-30 — every `o`/`O` in a cluster takes a value; the other shells; a long option
    # holding `c`/`s`; a `+` cluster; a Windows head; stdin.
    (["bash", "-euo", "pipefail", "run.sh"], "run.sh"),
    (["bash", "-eO", "extglob", "run.sh"], "run.sh"),
    (["bash", "-O", "extglob", "run.sh"], "run.sh"),
    (["bash", "+o", "pipefail", "run.sh"], "run.sh"),
    (["bash", "+O", "extglob", "run.sh"], "run.sh"),
    (["bash", "-oo", "a", "b", "run.sh"], "run.sh"),
    (["bash", "--init-file", "rc", "run.sh"], "run.sh"),
    (["zsh", "run.sh"], "run.sh"),
    (["dash", "run.sh"], "run.sh"),
    (["ksh", "run.sh"], "run.sh"),
    (["bash", "--posix", "run.sh"], "run.sh"),
    (["bash", "+e", "run.sh"], "run.sh"),
    (["C:\\Git\\bin\\bash.exe", "run.sh"], "run.sh"),
    (["bash", "-s", "run.sh"], None),
    (["bash", "-"], None),
    (["env", "-i", "bash", "run.sh"], None),
    (["env", "A=1", "B=2", "sh", "run.sh"], "run.sh"),
    # A lone `-` ends the options as `--` does, and a lone `+` is an empty cluster — measured with
    # bash and dash: `bash - run.sh` and `bash + run.sh` run `run.sh`, `bash - -c` opens a FILE named
    # `-c`, `bash + -c …` runs inline text.
    (["bash", "-", "run.sh"], "run.sh"),
    (["sh", "-e", "-", "run.sh"], "run.sh"),
    (["bash", "+", "run.sh"], "run.sh"),
    (["bash", "-", "-c"], "-c"),
    (["bash", "+", "-c", "echo x"], None),
])
def test_the_shell_reading_is_narrow(argv, script):
    assert _shell_script(argv) == script


@pytest.mark.parametrize("argv,path", [
    (["./run.sh"], "run.sh"),
    (["bin/score", "--x"], "bin/score"),
    (["env", "A=1", "./run.sh"], "run.sh"),
    (["score"], None),
    (["../run.sh"], None),
    (["/opt/run.sh"], None),
    ([], None),
])
def test_a_relative_head_is_the_file_it_executes(argv, path):
    assert _exec_path(argv) == path


def test_an_env_option_ends_the_reading():
    """`env -i …`, `env --chdir=a/b ./run.sh`: what runs, and from where, is `env`'s to decide — the
    rule names nothing rather than read the option as the program (a `--chdir=a/b` head holds a `/`
    and would be taken for the file `exec` runs)."""
    assert _argv_after_env(["env", "A=1", "B=2", "bash", "run.sh"]) == ["bash", "run.sh"]
    assert _argv_after_env(["env", "-i", "bash", "run.sh"]) is None
    assert _argv_after_env(["env", "A=1", "--chdir=a/b", "./run.sh"]) is None
    assert _argv_after_env(["python", "score.py"]) == ["python", "score.py"]
    assert _exec_path(["env", "--chdir=a/b", "./run.sh"]) is None
    assert scorer_frozen({"command": ["env", "--chdir=a/b", "./run.sh"]}, _ALL, [], []) == (None, ())


def test_only_the_spellings_that_exist_decide():
    """`-m` names two files and Python runs the one that is there; none there is a script this build
    authors — or, for `-m`, an installed module running something this rule cannot name (critic
    2026-09-30: `python -m torch.distributed.run … score.py` was silenced)."""
    module = {"command": ["python", "-m", "pkg.test"]}
    assert scorer_frozen(module, _ALL, ["pkg/test/__main__.py"], [],
                         exists=lambda f: f == "pkg/test.py") == (False, ("pkg/test.py",))
    launcher = {"command": ["python", "-m", "torch.distributed.run", "--nproc_per_node", "2",
                            "score.py"]}
    assert scorer_frozen(launcher, _ALL, [], [], exists=lambda f: False) == (None, ())
    assert scorer_frozen({"command": ["python", "score.py"]}, _ALL, [], [],
                         exists=lambda f: False) == (False, ())


_TAIL = (" READ what the command runs before you declare anything: work it already does itself "
         "(preparing data, training a model) runs AGAIN inside the `score` stage, so a stage of "
         "yours that repeats it pays for it twice. Declare only the work it does not already do — "
         "and when it does all of it, declare an EMPTY list (`stages: []`): the command alone is "
         "then this node's whole pipeline, and a pipeline carried over from the parent is dropped.")


def test_the_sentence_speaks_only_when_the_scorer_is_not_frozen():
    """Pinned whole (critic 2026-09-30: "how every node is scored" was false — an edit reaches this
    lineage — and the hedge said no file is named where the engine only cannot tell which)."""
    assert scorer_status_note(True, ("score.py",)) == ""
    assert scorer_status_note(False, ()) == "", "an entrypoint the build authors: nothing to repeat"
    assert scorer_status_note(False, ("a.py", "b.py")) == (
        _NOTE_HEAD + "`a.py` or `b.py` is inside your editable surface, so an edit to it changes "
        "the MEASUREMENT itself — what this node's number means — not only the work before it."
        + _TAIL)
    assert scorer_status_note(None, ()) == (
        " THE CODE IT RUNS MAY NOT BE FROZEN: the engine cannot tell from its argv which file it "
        "runs, so that file may be inside your editable surface, and an edit to it would change "
        "the MEASUREMENT itself — what this node's number means." + _TAIL)


# ------------------------------------------------------------------------------------ driven
def _repo(tmp_path: Path, *, command, protect=(), files=("run.sh", "train.py", "score.py")) -> RepoTask:
    for name in files:
        (tmp_path / name).write_text("echo prep; python train.py; python score.py\n"
                                     if name.endswith(".sh") else "print('x')\n")
    return RepoTask(id="r", goal="g", direction="max", editable_path=str(tmp_path),
                    edit_surface=_ALL, protect=list(protect),
                    eval=EvalSpec(command=list(command), metric=_M))


def _drive(monkeypatch, task, *, stages=None, parent=None, **kw):
    """Every phase's messages, driven through `implement` (or `implement_from(parent)`) with the model
    replaced; the STAGES phase declares `stages` (one `train` stage by default). Returns the phases'
    `(name, messages)` and the developer."""
    import looplab.agents.agent as agent_mod
    monkeypatch.setattr(LLMRepoDeveloper, "_time_budget_note", lambda self: "")
    monkeypatch.setattr(LLMRepoDeveloper, "_gpu_footprint_note", lambda self, idea: "")
    seen: list = []
    declared = ([{"name": "train", "command": ["python", "train.py"]}] if stages is None
                else stages)

    def fake_loop(client, tools, messages, emit_spec, *, finalize, fallback, **opts):
        seen.append((emit_spec["function"]["name"], list(messages)))
        if emit_spec["function"]["name"] == "declare_stages":
            return finalize({"stages": declared})
        return finalize({"summary": "s"})

    monkeypatch.setattr(agent_mod, "drive_tool_loop", fake_loop)
    dev = LLMRepoDeveloper(object(), task, plan_decompose=False, **kw)
    if parent is None:
        dev.implement(_IDEA)
    else:
        dev.implement_from(_IDEA, parent)
    return seen, dev


def _stages_turn(monkeypatch, task, **kw) -> str:
    """The STAGES phase's user turn."""
    seen, _dev = _drive(monkeypatch, task, **kw)
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


# ------------------------------------------------------------------------------------ critic 2026-09-30
def _systems(seen) -> list:
    return [msgs[0]["content"] for _name, msgs in seen]


def test_every_phase_s_system_body_says_what_holds_of_the_scorer_s_code(tmp_path, monkeypatch):
    """The system body told EVERY phase the scorer's code "is PROTECTED and your write/edit tools will
    refuse it" — the phases that can rewrite it included — while the STAGES turn said the opposite
    (driven: a step session wrote `run_experiment.sh`). MUTATIONS: drop the swap; ask with the flag
    off -> the historical sentence in an unfrozen scorer's phases."""
    from looplab.adapters.repo_developer import _SCORER_CODE_PROTECTED
    task = _repo(tmp_path, command=["bash", "run.sh"])
    on, _ = _drive(monkeypatch, task, scorer_status=True)
    off, _ = _drive(monkeypatch, task)
    clause = scorer_system_clause((False, ("run.sh",)))
    assert clause.startswith("That covers the stage, NOT the code it runs here: `run.sh`")
    assert scorer_system_clause((False, ("a.py", "b.py"))).startswith(
        "That covers the stage, NOT the code it runs here: `a.py` or `b.py` is inside")
    for system in _systems(on):
        assert clause in system and _SCORER_CODE_PROTECTED not in system
    for system_on, system_off in zip(_systems(on), _systems(off)):
        assert system_on.replace(clause, _SCORER_CODE_PROTECTED) == system_off
    assert len(_systems(on)) >= 2, "the STAGES phase and the implement session"


def test_a_frozen_or_authored_scorer_keeps_the_historical_system_body(tmp_path, monkeypatch):
    for n, (command, protect, files) in enumerate([
            (["bash", "run.sh"], ["run.sh"], ("run.sh", "train.py")),
            (["python", "score.py"], [], ("train.py",))]):
        (tmp_path / str(n)).mkdir()
        task = _repo(tmp_path / str(n), command=command, protect=protect, files=files)
        on, _ = _drive(monkeypatch, task, scorer_status=True)
        off, _ = _drive(monkeypatch, task)
        assert _systems(on) == _systems(off), command


def test_the_implement_note_does_not_say_an_unfrozen_scorer_only_scores(tmp_path, monkeypatch):
    """MUTATION: keep "only SCORES" for an unfrozen scorer -> the incident's `run.sh` (prep, train,
    score) is described as a scorer."""
    task = _repo(tmp_path, command=["bash", "run.sh"])
    on, _ = _drive(monkeypatch, task, scorer_status=True)
    off, _ = _drive(monkeypatch, task)
    implement_on = [m[1]["content"] for name, m in on if name != "declare_stages"][0]
    implement_off = [m[1]["content"] for name, m in off if name != "declare_stages"][0]
    assert "only SCORES" in implement_off and "only SCORES" not in implement_on
    assert "do not assume it only scores" in implement_on


def _parent_with_train_stage():
    import json
    from types import SimpleNamespace
    manifest = json.dumps({"stages": [{"name": "train", "command": ["python", "train.py"]}]})
    return SimpleNamespace(id=7, metric=0.5, deleted=[],
                           files={"looplab_stages.json": manifest, "train.py": "print('t')\n"})


def test_an_empty_declaration_drops_the_parent_s_pipeline(tmp_path, monkeypatch):
    """The critic's `d8`: node 7's child, told to declare nothing, was bounced twice and shipped the
    parent's `train` stage — the double training went on in every descendant. Under the flag an
    EMPTY declaration is accepted: the manifest leaves the working set and the implement note says the
    command runs alone. MUTATIONS: refuse the empty list; keep the manifest."""
    task = _repo(tmp_path, command=["bash", "run.sh"])
    seen, dev = _drive(monkeypatch, task, stages=[], parent=_parent_with_train_stage(),
                       scorer_status=True)
    assert "looplab_stages.json" not in dev.last_files
    assert "looplab_stages.json" in dev.last_deleted
    implement = [m[1]["content"] for name, m in seen if name != "declare_stages"][0]
    assert "NO pipeline stages are declared for this node" in implement
    assert "carried over from the parent" not in implement


def test_off_an_empty_declaration_is_the_failed_phase_it_always_was(tmp_path, monkeypatch):
    task = _repo(tmp_path, command=["bash", "run.sh"])
    seen, dev = _drive(monkeypatch, task, stages=[], parent=_parent_with_train_stage())
    assert "looplab_stages.json" in dev.last_files
    implement = [m[1]["content"] for name, m in seen if name != "declare_stages"][0]
    assert "carried over from the parent" in implement


def test_the_empty_declaration_is_bounced_off_and_accepted_on(tmp_path, monkeypatch):
    """Through the phase's own validator: `validate_stages` stays the definition of a manifest."""
    import looplab.agents.agent as agent_mod
    task = _repo(tmp_path, command=["bash", "run.sh"])
    verdicts: dict = {}

    def fake_loop(client, tools, messages, emit_spec, *, finalize, fallback, validate=None, **opts):
        if emit_spec["function"]["name"] == "declare_stages":
            verdicts.setdefault("got", []).append(validate({"stages": []}))
            return finalize({"stages": [{"name": "train", "command": ["python", "train.py"]}]})
        return finalize({"summary": "s"})

    monkeypatch.setattr(agent_mod, "drive_tool_loop", fake_loop)
    monkeypatch.setattr(LLMRepoDeveloper, "_time_budget_note", lambda self: "")
    monkeypatch.setattr(LLMRepoDeveloper, "_gpu_footprint_note", lambda self, idea: "")
    LLMRepoDeveloper(object(), task, plan_decompose=False, scorer_status=True).implement(_IDEA)
    LLMRepoDeveloper(object(), task, plan_decompose=False).implement(_IDEA)
    on, off = verdicts["got"]
    assert on is None and off, (on, off)


def test_the_turn_reads_the_mounts_and_the_surface_the_write_tools_read(tmp_path, monkeypatch):
    """The critic's `d13`: `lib/run.sh` in a named editable whose surface is `**/*.py` is refused by the
    write tools — frozen. MUTATIONS: pass no prefixes, or the whole surface -> the turn says
    "NOT FROZEN"."""
    from looplab.adapters.repo_task import EditableSpec
    root, lib = tmp_path / "root", tmp_path / "lib"
    root.mkdir()
    lib.mkdir()
    (root / "main.py").write_text("print(1)\n")
    (lib / "run.sh").write_text("python train.py\n")
    (lib / "train.py").write_text("print(1)\n")
    task = RepoTask(id="r", goal="g", direction="max", editable_path=str(root), edit_surface=_ALL,
                    editables=[EditableSpec(name="lib", path=str(lib), surface=["**/*.py"])],
                    eval=EvalSpec(command=["bash", "lib/run.sh"], metric=_M))
    assert _stages_turn(monkeypatch, task, scorer_status=True) == _stages_turn(monkeypatch, task)
    on, _ = _drive(monkeypatch, task, scorer_status=True)
    off, _ = _drive(monkeypatch, task)
    assert _systems(on) == _systems(off), "the system body reads the same mounts"
    (tmp_path / "narrow").mkdir()
    narrow = _repo(tmp_path / "narrow", command=["python", "score.py"])
    narrow.edit_surface = ["src/**"]
    assert _stages_turn(monkeypatch, narrow, scorer_status=True) == _stages_turn(monkeypatch, narrow)
    # A SHELL script outside the surface: no derived protection names it (`entrypoint_candidates`
    # reads the Python forms only), so the SURFACE alone freezes it — in the turn and in the body.
    (tmp_path / "shell").mkdir()
    shell = _repo(tmp_path / "shell", command=["bash", "run.sh"])
    shell.edit_surface = ["src/**"]
    assert "run.sh" not in shell._editable_mounts()[0]["protect"]
    assert _stages_turn(monkeypatch, shell, scorer_status=True) == _stages_turn(monkeypatch, shell)
    on, _ = _drive(monkeypatch, shell, scorer_status=True)
    off, _ = _drive(monkeypatch, shell)
    assert _systems(on) == _systems(off)


def test_the_derived_protection_normalizes_the_spelling_it_protects(tmp_path):
    """`python sub/./score.py` protected `sub/./score.py`, a name no write reaches, and left
    `sub/score.py` editable (critic 2026-09-30)."""
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "score.py").write_text("print(1)\n")
    task = RepoTask(id="r", goal="g", direction="max", editable_path=str(tmp_path),
                    edit_surface=_ALL, eval=EvalSpec(command=["python", "sub/./score.py"],
                                                     metric=_M))
    (mount,) = task._editable_mounts()
    assert "sub/score.py" in mount["protect"]
