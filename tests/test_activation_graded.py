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
