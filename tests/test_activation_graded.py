"""The graded activation check: typed manifest entries, where a marker's printer lives, what the node
changed, and the deterministic verdict matrix (`engine/activation.py`).

minionerec-lora-v1 node 2, 2026-10-01: the node changed ONLY `MiniOneRec/looplab/experiment.env` and
declared `SFT_EVAL_SAMPLE=-2` / `SFT_RESUME_EVERY_MIN=0` -- env ASSIGNMENTS no code prints. Its third
attempt trained 6.8 h, printed 0.1126388, and the metric was withheld as `inert_path`; the repair
directive made the Developer add `echo` lines and a full 7 h re-run followed. These tests pin the
pure halves; the driven ones are in `tests/test_activation_graded_engine.py`.
"""
from __future__ import annotations

import json
import os
import time

import pytest

from looplab.engine import activation as act
from looplab.engine.activation import (CAUSE_TP1, CAUSE_TP2, CAUSE_TP3, CAUSE_UNKNOWN,
                                       CAUSE_UNVERIFIABLE, Emitter, check_activation)

INCIDENT_ENV = ("# SFT_EVAL_SAMPLE=-2 mirrors SAMPLE=-1: stream the WHOLE valid file\n"
                "SFT_EVAL_SAMPLE=-2\nSFT_LORA_R=16\nexport SFT_RESUME_EVERY_MIN=0\n")


# ------------------------------------------------------------ 1. the typed manifest, back-compat

def test_a_string_is_a_log_entry_and_a_string_manifest_is_byte_identical():
    assert act.normalize_entries(["  prefix cache: ON ", "", 7, "prefix cache: ON"]) == [
        {"kind": "log", "text": "prefix cache: ON"}]
    old = json.dumps({"markers": ["A", "B"]}, indent=1)        # what every earlier build wrote
    assert act.manifest_text(act.normalize_entries(["A", "B"])) == old
    assert act.manifest_text(["A", "B"]) == old


def test_each_kind_is_cleaned_and_an_unusable_entry_is_dropped():
    entries = act.normalize_entries([
        {"kind": "env", "name": "SFT_EVAL_SAMPLE", "equals": -2,
         "file": "MiniOneRec/looplab/experiment.env"},
        {"kind": "file", "path": "out/metrics.json", "json_eq": {"mode": "fast"}},
        {"kind": "log", "regex": r"cache hits=\d+"},
        {"kind": "env", "name": "X", "equals": "1", "file": "/etc/passwd"},     # absolute
        {"kind": "env", "name": "X", "equals": "1", "file": "../up.env"},        # escapes
        {"kind": "env", "name": "bad name", "equals": "1", "file": "a.env"},
        {"kind": "file", "path": "x", "json_eq": {"a": {"nested": 1}}},          # not a scalar
        {"kind": "log", "text": "a", "regex": "b"},                              # both
        {"kind": "probe", "code": "import os"},                                  # no probe kind
    ])
    assert entries == [
        {"kind": "env", "name": "SFT_EVAL_SAMPLE", "equals": "-2",
         "file": "MiniOneRec/looplab/experiment.env"},
        {"kind": "file", "path": "out/metrics.json", "fresh": True, "json_eq": {"mode": "fast"}},
        {"kind": "log", "regex": r"cache hits=\d+"}]
    # a typed entry is written as itself, a plain log text as the bare string it always was
    text = act.manifest_text(entries + [{"kind": "log", "text": "ON"}])
    assert json.loads(text)["markers"][-1] == "ON"
    assert act.normalize_entries(json.loads(text)["markers"]) == entries + [
        {"kind": "log", "text": "ON"}]


def test_none_is_kept_alone_and_dropped_beside_an_observable():
    assert act.normalize_entries([{"kind": "none", "why": "a pure config sweep"}]) == [
        {"kind": "none", "why": "a pure config sweep"}]
    assert act.normalize_entries([{"kind": "none", "why": "w"}, "ON"]) == [
        {"kind": "log", "text": "ON"}]
    assert act.normalize_entries([{"kind": "none"}]) == []                       # no reason


def test_a_regex_is_bounded_and_never_a_nested_repeat():
    assert act.compile_marker_regex(r"hits=\d+ ratio=0\.\d+") is not None
    assert act.compile_marker_regex("(a+)+$") is None
    assert act.compile_marker_regex("(?:x*)*") is None
    assert act.compile_marker_regex("x" * (act.MAX_REGEX_CHARS + 1)) is None
    assert act.compile_marker_regex("(unclosed") is None
    entry = {"kind": "log", "regex": r"cache hits=\d+"}
    assert act.log_entry_seen(entry, ["warmup\ncache hits=12\n"])
    assert not act.log_entry_seen(entry, ["cache hits=12 and more"])             # fullmatch per line


def test_read_manifest_and_read_markers_agree_on_strings(tmp_path):
    (tmp_path / act.ACTIVATION_MANIFEST_NAME).write_text(act.manifest_text(
        ["ON", {"kind": "env", "name": "A", "equals": "1", "file": "x.env"}]))
    assert act.read_markers(tmp_path) == ["ON"]                                 # the legacy reader
    assert [e["kind"] for e in act.read_manifest(tmp_path)] == ["log", "env"]
    (tmp_path / act.ACTIVATION_MANIFEST_NAME).write_text("{not json")
    assert act.read_manifest(tmp_path) == []


# ------------------------------------------------------------ static env / file predicates

def test_the_env_check_reads_the_last_assignment_in_the_named_file(tmp_path):
    (tmp_path / "exp.env").write_text(INCIDENT_ENV + "SFT_LORA_R=32  # override\n")
    assert act.env_assignment(INCIDENT_ENV, "SFT_EVAL_SAMPLE") == "-2"
    assert act.env_assignment(INCIDENT_ENV, "SFT_RESUME_EVERY_MIN") == "0"   # `export` form
    assert act.env_assignment('A="x # y"\n', "A") == "x # y"                 # quoted keeps all
    entry = {"kind": "env", "name": "SFT_LORA_R", "equals": "32", "file": "exp.env"}
    assert act.check_env_entry(entry, tmp_path)
    assert not act.check_env_entry(dict(entry, equals="16"), tmp_path)        # last one wins
    assert not act.check_env_entry(dict(entry, file="missing.env"), tmp_path)
    assert not act.check_env_entry(dict(entry, name="NOT_SET"), tmp_path)


def test_the_file_check_is_the_stage_expect_predicate(tmp_path):
    since = time.time()
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "m.json").write_text(json.dumps({"mode": {"name": "fast"}, "n": 3}))
    entry = {"kind": "file", "path": "out/m.json", "fresh": True,
             "json_eq": {"mode.name": "fast", "n": 3}}
    assert act.check_file_entry(entry, tmp_path, since - 1)
    assert not act.check_file_entry(dict(entry, json_eq={"n": True}), tmp_path, since - 1)
    old = since - 3600
    os.utime(tmp_path / "out" / "m.json", (old, old))
    assert not act.check_file_entry(entry, tmp_path, since)                   # an earlier attempt's
    assert act.check_file_entry(dict(entry, fresh=False), tmp_path, since)
    assert not act.check_file_entry({"kind": "file", "path": "nope", "fresh": False}, tmp_path, None)
    (tmp_path / "empty.txt").write_text("")
    assert not act.check_file_entry({"kind": "file", "path": "empty.txt", "fresh": False},
                                    tmp_path, None)


# ------------------------------------------------------------ 2. where a marker's printer lives

def test_a_config_occurrence_or_an_assignment_line_is_not_an_emitter():
    assert act.emitters_in_file("MiniOneRec/looplab/experiment.env", INCIDENT_ENV,
                                "SFT_EVAL_SAMPLE=-2") == []
    assert act.config_occurrences("MiniOneRec/looplab/experiment.env", INCIDENT_ENV,
                                  "SFT_EVAL_SAMPLE=-2")
    sh = "SFT_EVAL_SAMPLE=-2; export SFT_EVAL_SAMPLE\nexport SFT_RESUME_EVERY_MIN=0\n"
    assert act.emitters_in_file("run.sh", sh, "SFT_EVAL_SAMPLE=-2") == []
    assert act.emitters_in_file("run.sh", sh, "SFT_RESUME_EVERY_MIN=0") == []
    # …while a line that PRINTS it is one
    assert act.emitters_in_file("run.sh", 'echo "SFT_EVAL_SAMPLE=-2" >&2\n', "SFT_EVAL_SAMPLE=-2")


def test_an_unconditional_print_is_told_from_a_guarded_one():
    py = ("print('BOOT_OK')\n"
          "def run(fast):\n"
          "    print('RUN_START')\n"
          "    if fast:\n"
          "        print('FAST_ON')\n"
          "    try:\n"
          "        go()\n"
          "    except Exception:\n"
          "        print('FAST_FALLBACK')\n")
    assert act.emitters_in_file("a.py", py, "BOOT_OK") == [Emitter("a.py", False)]
    assert act.emitters_in_file("a.py", py, "RUN_START") == [Emitter("a.py", False)]
    assert act.emitters_in_file("a.py", py, "FAST_ON") == [Emitter("a.py", True)]
    assert act.emitters_in_file("a.py", py, "FAST_FALLBACK") == [Emitter("a.py", True)]
    sh = ("echo 'CHECK_ECHO'\n"
          "if [ \"$FAST\" = 1 ]; then\n"
          "  echo 'FAST_ON'\n"
          "fi\n"
          "[ -n \"$X\" ] && echo 'X_ON'\n")
    assert act.emitters_in_file("r.sh", sh, "CHECK_ECHO") == [Emitter("r.sh", False)]
    assert act.emitters_in_file("r.sh", sh, "FAST_ON") == [Emitter("r.sh", True)]
    assert act.emitters_in_file("r.sh", sh, "X_ON") == [Emitter("r.sh", True)]


def test_tests_docstrings_and_comments_are_not_emitters():
    assert act.emitters_in_file("tests/test_x.py", "print('ON')\n", "ON") == []
    assert act.emitters_in_file("a.py", '"""prints ON"""\n# ON\nx = 1\n', "ON") == []
    assert act.emitters_in_file("r.sh", "# echo ON\n", "ON") == []


def test_the_tree_walk_finds_every_printer_and_says_when_it_stopped(tmp_path, monkeypatch):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "svc.py").write_text("def f(x):\n    if x:\n        print('NEW_ON')\n")
    (tmp_path / "pkg" / "exp.env").write_text("NEW_ON=1\n")
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "go").write_text("#!/bin/sh\necho NEW_ON\n")          # a shebang script
    (tmp_path / "README.md").write_text("NEW_ON is printed when the path runs\n")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "hook.py").write_text("print('NEW_ON')\n")
    scan = act.scan_emitters(tmp_path, ["NEW_ON", "NOWHERE"])
    assert scan.complete
    assert sorted(e.path for e in scan.found["NEW_ON"]) == ["bin/go", "pkg/svc.py"]
    assert scan.configured["NEW_ON"] == ["pkg/exp.env"]
    assert "NOWHERE" not in scan.found
    monkeypatch.setattr(act, "_SCAN_MAX_ENTRIES", 1)
    assert not act.scan_emitters(tmp_path, ["NEW_ON"]).complete


# ------------------------------------------------------------ 3. the change class

class _N:
    def __init__(self, files=None, code="", deleted=()):
        self.files, self.code, self.deleted = dict(files or {}), code, list(deleted)


def test_the_change_class_reads_the_node_s_cumulative_diff():
    env = "MiniOneRec/looplab/experiment.env"
    incident = _N({env: INCIDENT_ENV, act.ACTIVATION_MANIFEST_NAME: "{}",
                   "looplab_idea_report.json": "{}"})
    changed, code_changed = act.node_change(incident)                     # parent = the base repo
    assert changed == {env} and not code_changed
    assert act.change_class(changed, code_changed) == act.CHANGE_CONFIG_ONLY
    with_sh = _N({env: INCIDENT_ENV, "MiniOneRec/looplab/run_experiment.sh": "echo hi\n"})
    assert act.change_class(*act.node_change(with_sh)) == act.CHANGE_CODE   # .sh is code
    readme = _N({"README.md": "x"})
    assert act.change_class(*act.node_change(readme)) == act.CHANGE_CODE    # neither is code
    # against a parent: what the parent already held is not this node's change
    parent = _N({"svc.py": "print('x')\n"})
    child = _N({"svc.py": "print('x')\n", "conf.yaml": "a: 1\n"})
    assert act.node_change(child, [parent]) == (frozenset({"conf.yaml"}), False)
    assert act.change_class(*act.node_change(child, [parent])) == act.CHANGE_CONFIG_ONLY
    # the toy task's solution is `node.code`
    toy = _N(code="print('x')\n")
    assert act.node_change(toy) == (frozenset({act.SOLUTION_FILE}), True)
    assert act.change_class(*act.node_change(toy)) == act.CHANGE_CODE


# ------------------------------------------------------------ 4. the verdict matrix

def _v(entries, *, printed=(), emitters=None, changed=(), cls=act.CHANGE_CODE, satisfied=None,
       mode="graded"):
    entries = act.normalize_entries(entries)
    return check_activation(
        entries, printed={act.entry_label(e): act.entry_label(e) in printed for e in entries},
        emitters=emitters or {}, changed=frozenset(changed), change_cls=cls,
        satisfied=satisfied or {}, mode=mode)


def test_the_matrix_blocks_tp1_tp2_tp3_and_warns_only_on_an_unprintable_config_change():
    mine, old = Emitter("svc.py", True), Emitter("lib/old.py", True)
    tp1 = _v(["ON"], emitters={"ON": [mine]}, changed={"svc.py"})
    assert (tp1.verdict, dict(tp1.causes)) == ("block", {"ON": CAUSE_TP1})
    for cls in (act.CHANGE_CODE, act.CHANGE_CONFIG_ONLY):                     # TP2, both classes
        tp2 = _v(["ON"], emitters={"ON": [old]}, changed={"exp.env"}, cls=cls)
        assert (tp2.verdict, dict(tp2.causes)) == ("block", {"ON": CAUSE_TP2})
    tp3 = _v(["ON"], emitters={"ON": []}, changed={"svc.py"})
    assert (tp3.verdict, dict(tp3.causes)) == ("block", {"ON": CAUSE_TP3})
    warn = _v(["SFT_EVAL_SAMPLE=-2"], emitters={"SFT_EVAL_SAMPLE=-2": []}, changed={"exp.env"},
              cls=act.CHANGE_CONFIG_ONLY)
    assert (warn.verdict, warn.grade) == ("warn", "weak")
    assert dict(warn.causes) == {"SFT_EVAL_SAMPLE=-2": CAUSE_UNVERIFIABLE}
    assert warn.record()["missing"] == ["SFT_EVAL_SAMPLE=-2"]
    unknown = _v(["ON"], emitters={"ON": None}, cls=act.CHANGE_CONFIG_ONLY)
    assert (unknown.verdict, dict(unknown.causes)) == ("block", {"ON": CAUSE_UNKNOWN})
    # strict is the historical rule: missing = blocked, whatever the class
    strict = _v(["SFT_EVAL_SAMPLE=-2"], emitters={"SFT_EVAL_SAMPLE=-2": []},
                cls=act.CHANGE_CONFIG_ONLY, mode="strict")
    assert strict.verdict == "block"
    # a block anywhere blocks
    assert _v(["A", "B"], emitters={"A": [], "B": [mine]}, changed={"svc.py"},
              cls=act.CHANGE_CONFIG_ONLY).verdict == "block"


def test_the_grade_says_how_strong_the_proof_was():
    mine_if, mine_flat = Emitter("svc.py", True), Emitter("svc.py", False)
    assert _v(["ON"], printed={"ON"}, emitters={"ON": [mine_if]}, changed={"svc.py"}).grade == "strong"
    assert _v(["ON"], printed={"ON"}, emitters={"ON": [mine_flat]}, changed={"svc.py"}).grade == "weak"
    assert _v(["ON"], printed={"ON"}, emitters={"ON": [Emitter("old.py", True)]}).grade == "medium"
    env = {"kind": "env", "name": "A", "equals": "1", "file": "x.env"}
    ok = _v([env], satisfied={"A=1 in x.env": True})
    assert (ok.verdict, ok.grade, ok.kinds) == ("ok", "weak", ("env",))
    bad = _v([env], satisfied={"A=1 in x.env": False})
    assert (bad.verdict, bad.missing) == ("block", ("A=1 in x.env",))
    f = {"kind": "file", "path": "o.json"}
    assert _v([f], satisfied={"file o.json": True}).grade == "medium"
    assert _v([{"kind": "none", "why": "sweep"}]).verdict == "ok"


def test_log_spans_re_read_only_the_failed_attempt_s_bytes(tmp_path):
    from looplab.engine.eval_log_plan import snapshot_training_logs
    log = tmp_path / "eval.log"
    log.write_text("attempt 0: NEW_PATH_ON\n")
    snap = snapshot_training_logs(tmp_path)
    with open(log, "a") as fh:
        fh.write("attempt 1: fallback\n")
    texts, spans = act.attempt_log_texts(tmp_path, time.time() - 60, snap,
                                         frozenset({"eval.log"}))
    assert texts == ["attempt 1: fallback\n"]
    with open(log, "a") as fh:
        fh.write("attempt 2: NEW_PATH_ON\n")                       # a later append vouches for nothing
    assert act.read_log_spans(spans) == ["attempt 1: fallback\n"]
    log.write_text("x")                                            # truncated: no re-check at all
    assert act.read_log_spans(spans) is None


# ------------------------------------------------------------ 6. the settings

def test_the_settings_vocabulary_is_the_engine_s_and_an_old_snapshot_resumes_strict():
    """core spells the vocabulary out (it imports nothing above itself); this pins the two equal."""
    from looplab.core.config import (LEGACY_CONFIG_SNAPSHOT_DEFAULTS, Settings,
                                     settings_from_snapshot)
    from looplab.engine.options import EngineOptions
    assert dict(Settings._ENUM_FIELDS)["activation_check"] == act.ACTIVATION_MODES
    assert dict(Settings._ENUM_FIELDS)["activation_unverified_gate"] == act.ACTIVATION_GATES
    assert Settings().activation_check == EngineOptions().activation_check == "graded"
    assert Settings().activation_unverified_gate == "audit"
    with pytest.raises(ValueError):
        Settings(activation_check="lenient")
    assert LEGACY_CONFIG_SNAPSHOT_DEFAULTS["activation_check"] == "strict"
    assert settings_from_snapshot({}).activation_check == "strict"       # a pre-field snapshot
    assert settings_from_snapshot({"activation_check": "graded"}).activation_check == "graded"
    assert EngineOptions.from_settings(Settings(activation_check="off")).activation_check == "off"


# ------------------------------------------------------------ 2. the declaration lint

from looplab.engine.repair_verify import (activation_declaration_lint,  # noqa: E402
                                          activation_markers_not_in_code)

_INCIDENT_MARKERS = ["SFT_EVAL_SAMPLE=-2", "SFT_RESUME_EVERY_MIN=0"]
_INCIDENT_WRITTEN = {"MiniOneRec/looplab/experiment.env": INCIDENT_ENV,
                     act.ACTIVATION_MANIFEST_NAME: act.manifest_text(_INCIDENT_MARKERS)}
_ORIGINALS = {"MiniOneRec/looplab/run_experiment.sh":
              "set -a; . \"$(dirname \"$0\")/experiment.env\"; set +a\npython sft.py\n",
              "MiniOneRec/sft.py": "import os\nn = int(os.environ['SFT_EVAL_SAMPLE'])\n"}


def test_the_incident_s_markers_are_rewritten_to_env_instead_of_passing():
    """minionerec-lora-v1 node 2: the historical lint took the `.env` whole and PASSED both markers;
    the graded lint knows a config file prints nothing and records what the file sets instead.
    MUTATION: count a config occurrence as an emitter -> the markers stay `log` and pass."""
    assert activation_markers_not_in_code(_INCIDENT_MARKERS, _INCIDENT_WRITTEN) == ""  # the defect
    lint = activation_declaration_lint(_INCIDENT_MARKERS, _INCIDENT_WRITTEN, originals=_ORIGINALS)
    assert lint.change_class == act.CHANGE_CONFIG_ONLY
    assert list(lint.entries) == [
        {"kind": "env", "name": "SFT_EVAL_SAMPLE", "equals": "-2",
         "file": "MiniOneRec/looplab/experiment.env"},
        {"kind": "env", "name": "SFT_RESUME_EVERY_MIN", "equals": "0",
         "file": "MiniOneRec/looplab/experiment.env"}]
    assert lint.bounce == "" and lint.warning == ""
    assert all("not a line any code prints" in n and "Do not add an echo" in n for n in lint.notes)


def test_the_manifest_itself_is_never_the_file_an_env_entry_is_checked_in():
    """The manifest is a `.json` holding every marker verbatim; a config value the change did not
    set in a file of its own must not be pinned to it (`MiniOneRec/...` sorts after it)."""
    written = {act.ACTIVATION_MANIFEST_NAME: act.manifest_text(["Z_FLAG=1"])}
    lint = activation_declaration_lint(["Z_FLAG=1"], written, originals={"z/conf.env": "Z_FLAG=1\n"})
    assert list(lint.entries) == [{"kind": "env", "name": "Z_FLAG", "equals": "1",
                                   "file": "z/conf.env"}]


def test_a_marker_nothing_prints_is_none_on_a_config_change_and_bounced_on_a_code_change():
    config_only = activation_declaration_lint(["LORA ON"], {"conf/a.yaml": "lora: true\n"})
    assert [e["kind"] for e in config_only.entries] == ["none"] and not config_only.bounce
    code = activation_declaration_lint(["LORA ON"], {"train.py": "print('lora')\n"})
    assert [e["kind"] for e in code.entries] == ["log"]
    assert "'LORA ON'" in code.bounce and "Do not add an echo" in code.bounce


def test_a_printer_in_code_the_session_never_touched_keeps_the_marker():
    """TP2's declaration: the change sets a flag, existing code prints when the path runs. The whole
    tree is searched, so the marker stays a log entry the settle check holds to."""
    originals = {"svc/engine.py": "def go(fast):\n    if fast:\n        print('FAST PATH ON')\n"}
    lint = activation_declaration_lint(["FAST PATH ON"], {"conf/run.env": "FAST=1\n"},
                                       originals=originals)
    assert list(lint.entries) == [{"kind": "log", "text": "FAST PATH ON"}]
    assert not lint.bounce and not lint.warning
    # …and an unreadable tree never bounces what it could not search
    unread = activation_declaration_lint(["FAST PATH ON"], {"svc/new.py": "x = 1\n"},
                                         originals={}, originals_complete=False)
    assert not unread.bounce and [e["kind"] for e in unread.entries] == ["log"]


def test_an_unconditional_echo_added_for_the_check_is_warned():
    """69.8: an `echo` the change added outside every branch proves the script ran, never the path."""
    lint = activation_declaration_lint(
        ["SFT_EVAL_SAMPLE=-2"],
        {"run.sh": 'echo "SFT_EVAL_SAMPLE=-2" >&2\npython sft.py\n'},
        before=lambda p: "python sft.py\n")
    assert [e["kind"] for e in lint.entries] == ["log"]
    assert "outside any branch" in lint.warning.replace("OUTSIDE", "outside") and not lint.bounce
    guarded = activation_declaration_lint(
        ["FAST ON"], {"run.sh": 'if [ "$FAST" = 1 ]; then\n  echo "FAST ON"\nfi\n'})
    assert not guarded.warning


def _graded_build(monkeypatch, tmp_path, done_args, writes, validate_refusals=None):
    """A REAL `LLMRepoDeveloper.implement()` with `activation_graded=True`, writing `writes` and then
    emitting `done_args`; returns its files."""
    import sys
    import looplab.agents.agent as agent_mod
    from looplab.adapters.repo_task import EvalSpec, LLMRepoDeveloper, RepoTask
    from looplab.core.models import Idea

    def fake_loop(client, tools, messages, emit_spec, *, finalize, fallback, **opts):
        name = emit_spec["function"]["name"]
        if name == "declare_stages":
            return finalize({"stages": []})
        if name == "propose_plan":
            return finalize({"steps": []})
        for path, content in writes.items():
            tools.execute("write_file", {"path": path, "content": content})
        validate = opts.get("validate")
        if validate is not None and validate_refusals is not None:
            refusal = validate(dict(done_args))
            if refusal:
                validate_refusals.append(refusal)
        return finalize(dict(done_args))

    monkeypatch.setattr(agent_mod, "drive_tool_loop", fake_loop)
    repo = tmp_path / "repo"
    (repo / "looplab").mkdir(parents=True)
    (repo / "looplab" / "experiment.env").write_text("SFT_EVAL_SAMPLE=0\n")
    (repo / "train.py").write_text("import os\nprint(os.environ.get('SFT_EVAL_SAMPLE'))\n")
    task = RepoTask(id="r", goal="g", direction="max", editable_path=str(repo),
                    edit_surface=["*.py", "*.env", "looplab/*.env"], protect=[],
                    eval=EvalSpec(command=[sys.executable, "train.py"],
                                  metric={"kind": "stdout_json", "key": "metric"}))
    dev = LLMRepoDeveloper(object(), task, plan_decompose=False, activation_graded=True)
    spec = dev._emit_spec()["function"]["parameters"]["properties"]["activation_markers"]
    assert "oneOf" in spec["items"] and "a flag" not in spec["description"]
    dev.implement(Idea(operator="draft", params={}, rationale="x"))
    return dev.last_files


def test_the_graded_developer_writes_the_rewritten_declaration(monkeypatch, tmp_path):
    refusals: list = []
    files = _graded_build(monkeypatch, tmp_path,
                          {"summary": "s", "activation_markers": ["SFT_EVAL_SAMPLE=-2"]},
                          {"looplab/experiment.env": "SFT_EVAL_SAMPLE=-2\n"}, refusals)
    assert refusals == []                                   # a config value is rewritten, not bounced
    assert json.loads(files[act.ACTIVATION_MANIFEST_NAME]) == {"markers": [
        {"kind": "env", "name": "SFT_EVAL_SAMPLE", "equals": "-2",
         "file": "looplab/experiment.env"}]}


def test_the_historical_developer_is_unchanged(monkeypatch):
    from looplab.adapters.repo_task import LLMRepoDeveloper
    dev = LLMRepoDeveloper.__new__(LLMRepoDeveloper)          # never ran __init__: historical
    prop = dev._emit_spec()["function"]["parameters"]["properties"]["activation_markers"]
    assert prop == LLMRepoDeveloper._ACTIVATION_MARKERS_PROPERTY


# ------------------------------------------------------------ the change's own config assignment

from pathlib import Path  # noqa: E402

_INCIDENT_DATA = Path(__file__).parent / "data" / "activation_incident"


def test_the_real_node_2_declaration_is_rewritten_to_env_over_the_real_base_tree():
    """minionerec-lora-v1 node 2, the REAL bytes: the files its seq-585 repair wrote
    (`experiment.env`, the manifest) against excerpts of the base repo it ran on -- `sft_resume.py`,
    whose `why_off = "SFT_RESUME_EVERY_MIN=0"` reason string the emitter scan reads as an existing
    printer, and `run_experiment.sh`, whose guarded `SFT_EVAL_SAMPLE=-2` assignment is one too.
    Both markers are values the node's changed config sets, so both are env entries.
    MUTATION: drop `normalize_config_assignments` -> marker 2 stays a log marker."""
    env = "MiniOneRec/looplab/experiment.env"
    written = {env: (_INCIDENT_DATA / "experiment.env").read_text(),
               act.ACTIVATION_MANIFEST_NAME: (_INCIDENT_DATA / "looplab_activation.json").read_text()}
    base = {env: (_INCIDENT_DATA / "experiment.base.env").read_text(),
            "MiniOneRec/sft_resume.py": (_INCIDENT_DATA / "sft_resume.excerpt.py").read_text(),
            "MiniOneRec/looplab/run_experiment.sh":
                (_INCIDENT_DATA / "run_experiment.excerpt.sh").read_text()}
    markers = json.loads(written[act.ACTIVATION_MANIFEST_NAME])["markers"]
    assert markers == _INCIDENT_MARKERS
    assert act.scan_texts(base, markers).found.get("SFT_RESUME_EVERY_MIN=0")   # the false printer
    lint = activation_declaration_lint(markers, written, before=base.get, originals=base)
    assert lint.change_class == act.CHANGE_CONFIG_ONLY
    assert list(lint.entries) == [
        {"kind": "env", "name": "SFT_EVAL_SAMPLE", "equals": "-2", "file": env},
        {"kind": "env", "name": "SFT_RESUME_EVERY_MIN", "equals": "0", "file": env}]
    assert not lint.bounce and not lint.warning


def test_an_assignment_marker_the_touched_config_does_not_set_is_not_rewritten_to_env():
    lint = activation_declaration_lint(["USE_X=1"], {"conf/run.env": "USE_X=0\n"})
    assert all(e["kind"] != "env" for e in lint.entries)
    assert act.config_assignment_entry("USE_X=1", {"conf/run.env": "USE_X=0\nUSE_X=1\n"}) == {
        "kind": "env", "name": "USE_X", "equals": "1", "file": "conf/run.env"}   # the LAST wins
    assert act.config_assignment_entry("USE_X=1", {"conf/run.env": "USE_X=1\nUSE_X=0\n"}) is None
    assert act.config_assignment_entry("USE_X=1", {"run.sh": "USE_X=1\n"}) is None   # not config
    assert act.config_assignment_entry("cache ON now", {"a.yaml": "cache ON now\n"}) is None
