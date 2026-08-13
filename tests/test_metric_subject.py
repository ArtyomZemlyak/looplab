"""What a metric is ABOUT — the subject binding, the unbound violation, and the read allow-list.

Every test here DRIVES the property (tier 1 in CLAUDE.md's guard-test ladder): real files on disk,
a real `run_command_eval`, a real event log folded back into a `RunState`. The one thing that must
not be provable by a source pin is the enforcement, because "the violation row is minted" and "the
node is out of `feasible_nodes()`" are different facts and only the second one matters.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from looplab.core.config import Settings
from looplab.engine.metric_salvage import SALVAGE_VIOLATION, unbound_subject_violation_rows
from looplab.events.replay import fold
from looplab.runtime import landlock, metric_subject as ms, read_allowlist
from looplab.runtime.command_eval import run_command_eval

_METRIC = {"kind": "stdout_json", "key": "metric"}


def _writer(wd: Path, rel: str, payload: bytes = b"weights", metric: float = 0.5) -> list:
    """A command that writes `rel` and prints a metric — i.e. a node that produces its own subject."""
    src = (f"import json, os, pathlib\n"
           f"p = pathlib.Path({rel!r})\n"
           f"p.parent.mkdir(parents=True, exist_ok=True)\n"
           f"p.write_bytes({payload!r})\n"
           f"print(json.dumps({{'metric': {metric}}}))\n")
    (wd / "w.py").write_text(src, encoding="utf-8")
    return [sys.executable, "w.py"]


# --------------------------------------------------------------------------------------------
# The binding itself


def test_a_metric_carries_the_content_identity_of_its_declared_subject(tmp_path):
    res = run_command_eval(_writer(tmp_path, "out/model.bin"), str(tmp_path), 60, _METRIC,
                           subject=["out/model.bin"])
    assert res.metric == 0.5
    prov = res.metric_subject
    assert prov["subject_bound"] is True
    row = prov["subjects"][0]
    assert row["path"] == "out/model.bin" and row["kind"] == "file"
    # The referent, not just a boolean: the digest must be OF THE BYTES that are on disk.
    import hashlib
    assert row["digest"] == hashlib.sha256(b"weights").hexdigest()
    assert row["digest_mode"] == ms.DIGEST_FULL
    assert row["size"] == len(b"weights")


def test_two_same_size_subjects_are_told_apart_by_content_and_not_by_size(tmp_path):
    """THE incident's discriminator. The node's checkpoint and the human's are byte-identical in
    SIZE (92,174,712), so exists/non-empty/fresh — every predicate the artifact contract owns — are
    satisfied by both. Only content or inode identity separates them."""
    a, b = tmp_path / "a", tmp_path / "b"
    for d, payload in ((a, b"AAAAAAAA"), (b, b"BBBBBBBB")):
        d.mkdir()
        (d / "m.bin").write_bytes(payload)
    ra = ms.bind(["m.bin"], str(a), since=None)["subjects"][0]
    rb = ms.bind(["m.bin"], str(b), since=None)["subjects"][0]
    assert ra["size"] == rb["size"]                      # the contract's predicates cannot tell
    assert ra["digest"] != rb["digest"]                  # the binding can
    assert ra["identity"][:2] != rb["identity"][:2]      # (dev, ino) too


def test_an_undeclared_subject_is_unbound_and_says_which_fix(tmp_path):
    res = run_command_eval(_writer(tmp_path, "out/model.bin"), str(tmp_path), 60, _METRIC)
    assert res.metric_subject is None                    # nothing declared -> nothing recorded here
    prov = ms.bind([], str(tmp_path), since=None)
    assert prov["unbound_reason"] == "not_declared"
    assert "subject" in ms.unbound_message(prov)


@pytest.mark.parametrize("rel,reason", [("out/never.bin", "missing"), ("/etc/passwd", "escapes")])
def test_a_subject_that_cannot_be_bound_names_its_own_reason(tmp_path, rel, reason):
    res = run_command_eval(_writer(tmp_path, "out/model.bin"), str(tmp_path), 60, _METRIC,
                           subject=[rel])
    assert res.metric == 0.5                             # the number was still read…
    assert res.metric_subject["subject_bound"] is False  # …and it is not bound to anything
    assert res.metric_subject["unbound_reason"] == reason


def test_a_leftover_from_an_earlier_attempt_is_stale_not_bound(tmp_path):
    """The freshness rule `verify_stage_inputs` deliberately does NOT have. An input legitimately
    predates its stage; a SUBJECT cannot, or this attempt's number would be recorded as a claim
    about an earlier attempt's checkpoint — the measured shape in a reused workdir."""
    (tmp_path / "out").mkdir()
    old = tmp_path / "out" / "model.bin"
    old.write_bytes(b"stale")
    os.utime(old, (time.time() - 3600, time.time() - 3600))
    res = run_command_eval([sys.executable, "-c", "print('{\"metric\": 0.9}')"], str(tmp_path), 60,
                           _METRIC, subject=["out/model.bin"])
    assert res.metric == 0.9
    assert res.metric_subject["subject_bound"] is False
    assert res.metric_subject["unbound_reason"] == "stale"
    # the identity is still recorded — an operator debugging "why stale" needs to see WHICH file
    assert res.metric_subject["subjects"][0]["digest"]


def test_the_staged_path_binds_at_the_score_stage_and_names_the_producing_stage(tmp_path):
    (tmp_path / "t.py").write_text(
        "import pathlib\n"
        "p = pathlib.Path('out/model.bin'); p.parent.mkdir(parents=True, exist_ok=True)\n"
        "p.write_bytes(b'trained')\n", encoding="utf-8")
    (tmp_path / "s.py").write_text("print('{\"metric\": 0.7}')\n", encoding="utf-8")
    stages = [{"name": "train", "command": [sys.executable, "t.py"],
               "expect": {"files": ["out/model.bin"]}},
              {"name": "score", "command": [sys.executable, "s.py"], "needs": ["out/model.bin"]}]
    res = run_command_eval([], str(tmp_path), 60, _METRIC, stages=stages, subject=["out/model.bin"])
    assert res.metric == 0.7
    assert res.metric_subject["subject_bound"] is True
    assert res.metric_subject["subject_stage"] == "score"
    # `expect` and `needs` become two projections of ONE relation once the artifact has identity.
    assert res.metric_subject["subjects"][0]["producer"] == "train"


# --------------------------------------------------------------------------------------------
# The enforcement — and that it lands on SELECTION, not merely in the record


def test_only_require_mints_a_row_and_it_is_the_existing_salvage_vocabulary():
    unbound = {"subject_bound": False, "unbound_reason": "not_declared", "subjects": []}
    assert unbound_subject_violation_rows(unbound, 0.5, "off") == []
    assert unbound_subject_violation_rows(unbound, 0.5, "audit") == []
    rows = unbound_subject_violation_rows(unbound, 0.5, "require")
    # NOT a second exclusion vocabulary: `metric_unmeasured`, serve/report.py, lessons_distill and
    # speculation_quality all key on this exact name, and a new slug would still exclude the node
    # while silently ceasing to be recognised by every one of them.
    assert [r["name"] for r in rows] == [SALVAGE_VIOLATION]
    assert rows[0]["salvage"]["condition"] == "metric_subject_unbound"
    assert unbound_subject_violation_rows({"subject_bound": True}, 0.5, "require") == []


def test_an_unbound_metric_is_counted_and_visible_but_never_selectable(tmp_path):
    """The property, driven through a real fold rather than asserted about a row.

    The node must stay EVALUATED (it counts, it is in the budget, the UI and the lineage see it) and
    must be out of `feasible_nodes()`, which is what champion selection and breeding read.
    """
    from looplab.events.eventstore import EventStore
    from looplab.events.types import EV_NODE_CREATED, EV_NODE_EVALUATED, EV_RUN_STARTED
    store = EventStore(tmp_path / "events.jsonl")
    store.append(EV_RUN_STARTED, {"run_id": "r1", "task_id": "t", "goal": "g", "direction": "max"})
    for nid in (0, 1):
        store.append(EV_NODE_CREATED, {"node_id": nid, "parent_ids": [], "operator": "draft",
                                       "idea": {"operator": "draft", "params": {"x": float(nid)}},
                                       "code": ""})
    store.append(EV_NODE_EVALUATED, {"node_id": 0, "metric": 0.9, "violations": []})
    prov = {"subject_bound": False, "unbound_reason": "not_declared", "subjects": []}
    store.append(EV_NODE_EVALUATED, {
        "node_id": 1, "metric": 0.99, "metric_provenance": prov,
        "violations": unbound_subject_violation_rows(prov, 0.99, "require")})
    st = fold(store.read_all())
    unbound = st.nodes[1]
    assert unbound.status == "evaluated" and unbound.metric == 0.99      # counted, visible
    assert unbound.feasible is False                                     # and never selectable
    assert [n.id for n in st.feasible_nodes()] == [0]
    assert unbound.metric_provenance["unbound_reason"] == "not_declared"


def test_the_record_is_additive_so_a_log_with_no_provenance_still_folds(tmp_path):
    """Invariant #5, and it is not optional here: EVERY existing run's log has no provenance."""
    from looplab.events.eventstore import EventStore
    from looplab.events.types import EV_NODE_CREATED, EV_NODE_EVALUATED, EV_RUN_STARTED
    store = EventStore(tmp_path / "events.jsonl")
    store.append(EV_RUN_STARTED, {"run_id": "r1", "task_id": "t", "goal": "g", "direction": "max"})
    store.append(EV_NODE_CREATED, {"node_id": 0, "parent_ids": [], "operator": "draft",
                                   "idea": {"operator": "draft", "params": {"x": 1.0}}, "code": ""})
    store.append(EV_NODE_EVALUATED, {"node_id": 0, "metric": 0.4})
    st = fold(store.read_all())
    assert st.nodes[0].metric_provenance is None and st.nodes[0].feasible is not False


# --------------------------------------------------------------------------------------------
# Vocabularies, spelled in two layers on purpose (core imports nothing above itself)


def test_the_settings_vocabularies_match_the_modules_that_own_them():
    assert dict(Settings._ENUM_FIELDS)["metric_subject"] == ms.MODES
    assert dict(Settings._ENUM_FIELDS)["landlock"] == landlock.POLICIES
    assert Settings().metric_subject == "audit" and Settings().landlock == "off"


def test_a_resumed_pre_2026_08_13_run_keeps_the_record_it_was_written_with():
    from looplab.core.config import LEGACY_CONFIG_SNAPSHOT_DEFAULTS
    assert LEGACY_CONFIG_SNAPSHOT_DEFAULTS["metric_subject"] == "off"


def test_an_unknown_rung_from_another_binary_degrades_to_the_conservative_one():
    assert ms.settle_mode("require") == "require"
    assert ms.settle_mode("enforce-everything") == "audit"     # not the strictest, not "off"
    assert ms.settle_mode(None) == "audit"


# --------------------------------------------------------------------------------------------
# The read allow-list


def test_the_allow_list_carries_the_declared_mounts_and_never_the_editable_root(tmp_path):
    src = tmp_path / "src"
    (src / "data").mkdir(parents=True)
    spec = {"editables": [{"path": str(src)}],
            "data": {"train": {"path": str(src / "data"), "edit": False}}}
    allow = dict(read_allowlist.derive(workdir=str(tmp_path / "wd"), run_dir=str(tmp_path / "run"),
                                       repo_spec=spec))
    # The mount source is granted even though it lives INSIDE the editable tree — that is the
    # measured false positive (rubertlite-dense-retrieval node 36) this derivation exists to avoid.
    assert allow[os.path.realpath(str(src / "data"))] == "read"
    assert os.path.realpath(str(src)) not in allow      # the editable root itself is never granted


def test_an_editable_mount_is_writable_and_a_plain_one_is_not(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    allow = dict(read_allowlist.derive(
        workdir=str(tmp_path), repo_spec={"data": {"ro": {"path": str(tmp_path / "a")},
                                                   "rw": {"path": str(tmp_path / "b"),
                                                          "edit": True}}}))
    assert allow[os.path.realpath(str(tmp_path / "a"))] == "read"
    assert allow[os.path.realpath(str(tmp_path / "b"))] == "readwrite"


def test_a_missing_DECLARED_mount_survives_derivation_while_a_missing_machine_tier_does_not(tmp_path):
    """The asymmetry that made the first end-to-end launch refuse everything: `/lib32` is absent on
    this image and is not a fact about anything, while a declared mount that is not there is a real
    disagreement the launcher must refuse BY NAME rather than run without."""
    gone = str(tmp_path / "not-there")
    allow = dict(read_allowlist.derive(workdir=str(tmp_path),
                                       repo_spec={"data": {"d": {"path": gone}}}))
    assert os.path.realpath(gone) in allow
    assert "/lib32" not in allow or os.path.exists("/lib32")


def test_the_wire_format_round_trips_and_refuses_a_malformed_record():
    """The separator collision this shipped with for an hour: `os.pathsep` IS ':' on POSIX, so
    `mode:path` records could not be partitioned, every record was DROPPED, and an empty allow-list
    denies EVERYTHING."""
    allow = [("/usr", "read"), ("/tmp/x", "readwrite")]
    assert landlock.parse_env(landlock.format_env(allow)) == allow
    assert os.pathsep not in landlock.format_env(allow) or os.pathsep == "\x1e"
    with pytest.raises(landlock.LandlockUnavailable):
        landlock.parse_env("read:/usr")            # the old spelling is refused, never dropped


# --------------------------------------------------------------------------------------------
# Landlock, where the kernel has it


_NO_LANDLOCK = landlock.unavailable_reason()


@pytest.mark.skipif(_NO_LANDLOCK is not None, reason=str(_NO_LANDLOCK))
def test_landlock_refuses_a_path_outside_the_allow_list_in_a_child_process(tmp_path):
    """Driven through a real `subprocess`, not through this process: the point of the kernel rung is
    that it covers readers the audit hook cannot see, and inheritance across `exec` is the mechanism.
    """
    wd, denied = tmp_path / "wd", tmp_path / "denied"
    wd.mkdir()
    denied.mkdir()
    (denied / "secret.txt").write_text("nope", encoding="utf-8")
    allow = [(p, m) for p, m in read_allowlist.derive(workdir=str(wd), run_dir=str(wd))
             if p != os.path.realpath(str(tmp_path.parent)) and p != "/tmp"]
    prog = ("import sys\n"
            "try:\n"
            "    open(sys.argv[1]).read(); print('READ')\n"
            "except OSError as e:\n"
            "    print('REFUSED', e.errno)\n")
    (wd / "p.py").write_text(prog, encoding="utf-8")
    argv = landlock.launch_argv(sys.executable, landlock.format_env(allow),
                                [sys.executable, str(wd / "p.py"), str(denied / "secret.txt")])
    out = subprocess.run(argv, capture_output=True, text=True, cwd=str(wd))
    assert out.stdout.strip() == "REFUSED 13", (out.stdout, out.stderr)


@pytest.mark.skipif(_NO_LANDLOCK is not None, reason=str(_NO_LANDLOCK))
def test_an_incomplete_allow_list_refuses_the_launch_rather_than_enforcing_a_partial_one(tmp_path):
    """Under an allow-list a rule that could not be added is a DENIAL. 211 candidate rules produced
    55 accepted ones in the measurement; enforcing that silently is how legitimate work dies
    somewhere nobody can predict."""
    with pytest.raises(landlock.LandlockUnavailable) as err:
        landlock.apply([(str(tmp_path), "readwrite"), (str(tmp_path / "absent"), "read")])
    assert "absent" in str(err.value)


@pytest.mark.skipif(_NO_LANDLOCK is not None, reason=str(_NO_LANDLOCK))
def test_an_empty_allow_list_refuses_the_launch_instead_of_denying_everything(tmp_path):
    argv = landlock.launch_argv(sys.executable, "", [sys.executable, "-c", "print('RAN')"])
    out = subprocess.run(argv, capture_output=True, text=True)
    assert out.returncode == 126 and "empty allow-list" in out.stderr
    assert "RAN" not in out.stdout


def test_the_landlock_marker_is_absent_from_a_child_env_by_default(tmp_path):
    """`Settings.landlock` ships `off`, so no launch on any run today carries the marker and
    `run_argv` is byte-identical to what it was."""
    from looplab.runtime.sandbox import run_argv
    rc, out, _err, _to = run_argv(
        [sys.executable, "-c",
         "import os; print(os.environ.get('LOOPLAB_LANDLOCK', 'ABSENT'))"], str(tmp_path), 60)
    assert rc == 0 and out.strip() == "ABSENT"


# --------------------------------------------------------------------------------------------
# The declaration channel


def test_the_operator_may_declare_a_subject_and_a_bad_shape_is_refused_at_submit():
    from pydantic import ValidationError

    from looplab.adapters.repo_task import EvalSpec
    ok = EvalSpec(command=["python", "s.py"],
                  metric={"kind": "stdout_json", "key": "m", "subject": ["./out/model.bin"]})
    assert ok.metric["subject"] == ["out/model.bin"]        # normalized by the shared path rule
    for bad in (["/abs/model.bin"], ["../escape.bin"], "not-a-list"):
        with pytest.raises(ValidationError):
            EvalSpec(command=["python", "s.py"],
                     metric={"kind": "stdout_json", "key": "m", "subject": bad})


def test_the_engine_appended_score_stage_declares_what_it_reads(tmp_path):
    """Doc 35 §3a's ONE missing wire: `needs` shipped, and the only stage the operator owns was the
    only stage that could not declare it."""
    from looplab.engine.eval_stages import EvalStagesMixin

    class _E(EvalStagesMixin):
        metric_subject = "audit"

    (tmp_path / "looplab_stages.json").write_text(
        json.dumps({"stages": [{"name": "train", "command": ["python", "t.py"]}]}), encoding="utf-8")
    es = {"command": ["python", "score.py"],
          "metric": {"kind": "stdout_json", "key": "m", "subject": ["out/model.bin"]}}
    chain = _E()._resolve_stages(str(tmp_path), es)
    assert [s["name"] for s in chain] == ["train", "score"]
    assert chain[-1]["needs"] == ["out/model.bin"]
    # …and the `off` rung must not acquire a stage contract the run never had.
    class _Off(EvalStagesMixin):
        metric_subject = "off"
    assert "needs" not in _Off()._resolve_stages(str(tmp_path), es)[-1]


def test_needs_alone_does_not_catch_the_incident_and_this_is_why_it_is_not_the_gate(tmp_path):
    """The measurement that demotes floor option 1. `verify_stage_inputs` is a PRESENCE check: the
    node really did write the checkpoint its `needs` names, so the contract passes and the stage
    goes on to read somewhere else entirely. Reproduced here in miniature."""
    from looplab.runtime.command_eval import verify_stage_inputs
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "model.bin").write_bytes(b"the node's own, correct, fresh checkpoint")
    assert verify_stage_inputs(["out/model.bin"], str(tmp_path), stage="score",
                               since=time.time()) is None
