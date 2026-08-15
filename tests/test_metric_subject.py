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
# THE PATTERN DECLARATION — for the task family whose output path the AGENT names
#
# Measured 2026-08-15 over every `looplab_stages.json` in `runs/`: the four repo runs that train
# anything (v2, v6, v7, v8) declare 18 node outputs, ALL of them at
# `vectorsearch/experiments/<agent-chosen name>_rubert-tiny-lite/final/model.safetensors`, with TEN
# distinct spellings of that one segment. A literal `subject` written at submit binds 5 of 7 on v6
# and 0 of 5 on the live v8 — which is why all three of v8's evaluated nodes record
# `not_declared` under the shipped `audit` default.


def _experiments(wd: Path, name: str, ckpts=(), payload: bytes = b"final-weights") -> Path:
    """A node's own experiment directory, shaped like the live v8 ones: a `final/` checkpoint plus
    several `checkpoint-NNNN/` siblings that are byte-identical in SIZE to it and to each other."""
    exp = wd / "experiments" / name
    (exp / "final").mkdir(parents=True, exist_ok=True)
    (exp / "final" / "model.safetensors").write_bytes(payload)
    for step in ckpts:
        (exp / f"checkpoint-{step}").mkdir(parents=True, exist_ok=True)
        (exp / f"checkpoint-{step}" / "model.safetensors").write_bytes(b"x" * len(payload))
    return exp


def test_a_pattern_binds_the_one_artifact_an_agent_named_directory_holds(tmp_path):
    """THE GAP, closed and driven. The operator cannot write
    `experiments/dcl-unified_rubert-tiny-lite/final/model.safetensors` at submit — the middle segment
    is `vectorsearch/config.py::run_name`, i.e. a value the agent picks per experiment — so it
    declares the SHAPE and the engine resolves it against the real workdir."""
    import hashlib
    (tmp_path / "s.py").write_text("import json; print(json.dumps({'metric': 0.72}))\n",
                                   encoding="utf-8")
    _experiments(tmp_path, "dcl-unified_rubert-tiny-lite", ckpts=(2709, 3612, 4515))
    res = run_command_eval([sys.executable, "s.py"], str(tmp_path), 60, _METRIC,
                           subject_glob=["experiments/*/final/model.safetensors"])
    assert res.metric == 0.72
    prov = res.metric_subject
    assert prov["subject_bound"] is True, prov
    row = prov["subjects"][0]
    # The RESOLVED path is on the record, not just the pattern — a reader has to be able to check
    # WHICH artifact the number was bound to, since a pattern is a claim about a shape.
    assert row["path"] == "experiments/dcl-unified_rubert-tiny-lite/final/model.safetensors"
    assert row["glob"] == "experiments/*/final/model.safetensors"
    # …and it is bound to the BYTES, exactly as a literal is: `final/` and the three checkpoints are
    # the same size, so only content or inode identity tells them apart (the incident's property).
    assert row["digest"] == hashlib.sha256(b"final-weights").hexdigest()
    assert row["digest_mode"] == ms.DIGEST_FULL
    assert unbound_subject_violation_rows(prov, res.metric, "require") == []


def test_a_pattern_that_matches_several_same_sized_checkpoints_refuses_instead_of_picking(tmp_path):
    """THE INCIDENT SHAPE, one level in. Each live v8 node holds FOUR `model.safetensors` of
    92,174,712 bytes — `final/` plus `checkpoint-2709/3612/4515` — so a careless
    `experiments/*/*/model.safetensors` matches all four. Binding any one of them would record
    `subject_bound: True` about bytes nobody chose, which is strictly WORSE than the `not_declared`
    it replaced: it manufactures confidence. The refusal is the mechanism."""
    (tmp_path / "s.py").write_text("import json; print(json.dumps({'metric': 0.72}))\n",
                                   encoding="utf-8")
    _experiments(tmp_path, "dcl-unified_rubert-tiny-lite", ckpts=(2709, 3612, 4515))
    res = run_command_eval([sys.executable, "s.py"], str(tmp_path), 60, _METRIC,
                           subject_glob=["experiments/*/*/model.safetensors"])
    assert res.metric == 0.72                          # the number is still read — this is not a gate
    prov = res.metric_subject
    assert prov["subject_bound"] is False
    assert prov["unbound_reason"] == "ambiguous"
    row = prov["subjects"][0]
    assert row.get("path") in ("", None)               # nothing was bound, so nothing is named bound
    assert "digest" not in row                          # …and no identity was recorded for a pick
    assert len(row["matched"]) == 4                     # the four the operator has to choose between
    # The message names the FIX for this reason and shows what it matched, because on a finished run
    # the workdir the operator would go and look at may not exist any more.
    msg = ms.unbound_message(prov)
    assert "MORE THAN ONE" in msg and "final/model.safetensors" in msg
    # …and under the rung that gates, this is an excluded node rather than a blessed one.
    rows = unbound_subject_violation_rows(prov, res.metric, "require")
    assert [r["name"] for r in rows] == [SALVAGE_VIOLATION]
    assert rows[0]["salvage"]["unbound_reason"] == "ambiguous"


def test_two_experiment_directories_are_ambiguous_and_mtime_never_breaks_the_tie(tmp_path):
    """The rule that must not bend. Dropping the STALE matches and requiring uniqueness of the rest
    would resolve this beautifully — and it would promote mtime from a predicate that can only ever
    REFUSE into the thing that ELECTS which bytes the number is bound to. The candidate owns its
    workdir and nothing fences `os.utime` on its own files, so it would be choosing its own subject.

    Driven with the tie-break's own best case: one match is hours old and the other is this attempt's,
    which is exactly the shape a tie-break would be written for."""
    (tmp_path / "s.py").write_text("import json; print(json.dumps({'metric': 0.5}))\n",
                                   encoding="utf-8")
    old = _experiments(tmp_path, "seeded-baseline_rubert-tiny-lite", payload=b"someone-elses")
    _experiments(tmp_path, "mine_rubert-tiny-lite", payload=b"mine-this-attempt")
    stale = time.time() - 7200
    os.utime(old / "final" / "model.safetensors", (stale, stale))
    res = run_command_eval([sys.executable, "s.py"], str(tmp_path), 60, _METRIC,
                           subject_glob=["experiments/*/final/model.safetensors"])
    prov = res.metric_subject
    assert prov["subject_bound"] is False
    assert prov["unbound_reason"] == "ambiguous", prov      # NOT "the fresh one, obviously"
    assert len(prov["subjects"][0]["matched"]) == 2


def test_a_pattern_that_matches_nothing_is_missing_and_not_an_absent_declaration(tmp_path):
    """The two states `UNBOUND_REASONS` is a closed vocabulary to keep apart: "the operator declared
    nothing" and "the operator declared something the pipeline did not produce" need opposite fixes,
    and a pattern is a DECLARATION even when it resolves to nothing."""
    (tmp_path / "s.py").write_text("import json; print(json.dumps({'metric': 0.5}))\n",
                                   encoding="utf-8")
    res = run_command_eval([sys.executable, "s.py"], str(tmp_path), 60, _METRIC,
                           subject_glob=["experiments/*/final/model.safetensors"])
    assert res.metric == 0.5
    assert res.metric_subject["unbound_reason"] == "missing"     # …and NOT "not_declared"
    assert "not produced" in ms.unbound_message(res.metric_subject)


def test_a_pattern_is_held_to_the_same_freshness_rule_as_a_literal(tmp_path):
    """A resolved match is not a different kind of subject: the unique match goes through the SAME
    `bind_one`, so this attempt's number still cannot be about an earlier attempt's checkpoint."""
    exp = _experiments(tmp_path, "mine_rubert-tiny-lite")
    stale = time.time() - 3600
    os.utime(exp / "final" / "model.safetensors", (stale, stale))
    res = run_command_eval([sys.executable, "-c", "print('{\"metric\": 0.9}')"], str(tmp_path), 60,
                           _METRIC, subject_glob=["experiments/*/final/model.safetensors"])
    assert res.metric == 0.9
    assert res.metric_subject["subject_bound"] is False
    assert res.metric_subject["unbound_reason"] == "stale"
    assert res.metric_subject["subjects"][0]["digest"]        # the identity is still recorded


def test_a_match_reached_through_a_symlink_out_of_the_workdir_escapes(tmp_path):
    """`Path.glob` FOLLOWS a symlinked directory, so a match CAN land outside the workdir — the
    v6-node-4 shape one door over, a candidate that plants `experiments -> <the human's tree>` and
    lets the operator's own pattern name somebody else's checkpoint. Containment is decided by the
    SAME `_confined` a literal goes through, because the resolved match is bound through the same
    `bind_one`; there is deliberately no second containment rule inside the resolver, and no
    pre-filter that would drop an escaping match from the count (that would let a candidate
    de-ambiguate its own subject by making the other matches escape)."""
    outside = tmp_path / "someone-else"
    (outside / "runA" / "final").mkdir(parents=True)
    (outside / "runA" / "final" / "model.safetensors").write_bytes(b"the human's checkpoint")
    wd = tmp_path / "wd"
    wd.mkdir()
    (wd / "s.py").write_text("import json; print(json.dumps({'metric': 0.5}))\n", encoding="utf-8")
    os.symlink(str(outside), str(wd / "experiments"))
    res = run_command_eval([sys.executable, "s.py"], str(wd), 60, _METRIC,
                           subject_glob=["experiments/*/final/model.safetensors"])
    assert res.metric == 0.5
    assert res.metric_subject["subject_bound"] is False
    assert res.metric_subject["unbound_reason"] == "escapes"


def test_an_escaping_match_beside_a_legitimate_one_is_ambiguous_and_not_quietly_dropped(tmp_path):
    """The NO-PRE-FILTER rule, which is the same rule as the mtime one: a fact the candidate controls
    may refuse a binding and may never elect one. Dropping the escaping match before counting reads
    like a kindness — it would let the node's own checkpoint bind — and it hands the candidate a
    primitive: plant a symlink out of the workdir over every experiment directory but the one whose
    bytes it wants the number attributed to, and the operator's own pattern names it uniquely."""
    outside = tmp_path / "someone-else"
    (outside / "final").mkdir(parents=True)
    (outside / "final" / "model.safetensors").write_bytes(b"the human's checkpoint")
    wd = tmp_path / "wd"
    (wd / "experiments").mkdir(parents=True)
    (wd / "s.py").write_text("import json; print(json.dumps({'metric': 0.5}))\n", encoding="utf-8")
    (wd / "experiments" / "mine_rubert-tiny-lite" / "final").mkdir(parents=True)
    (wd / "experiments" / "mine_rubert-tiny-lite" / "final" / "model.safetensors").write_bytes(b"m")
    os.symlink(str(outside), str(wd / "experiments" / "theirs_rubert-tiny-lite"))
    res = run_command_eval([sys.executable, "s.py"], str(wd), 60, _METRIC,
                           subject_glob=["experiments/*/final/model.safetensors"])
    prov = res.metric_subject
    assert prov["subject_bound"] is False
    assert prov["unbound_reason"] == "ambiguous", prov
    assert len(prov["subjects"][0]["matched"]) == 2


def test_the_two_declaration_shapes_compose_and_both_must_bind(tmp_path):
    """`subject_bound` is an AND over everything declared, whichever shape declared it — a metric
    that is a claim about two artifacts is not half-true when one of them is missing."""
    (tmp_path / "s.py").write_text("import json; print(json.dumps({'metric': 0.5}))\n",
                                   encoding="utf-8")
    _experiments(tmp_path, "mine_rubert-tiny-lite")
    (tmp_path / "preds.jsonl").write_bytes(b'{"id": 1}\n')
    both = dict(subject=["preds.jsonl"], subject_glob=["experiments/*/final/model.safetensors"])
    res = run_command_eval([sys.executable, "s.py"], str(tmp_path), 60, _METRIC, **both)
    prov = res.metric_subject
    assert prov["subject_bound"] is True
    assert [r["path"] for r in prov["subjects"]] == [
        "preds.jsonl", "experiments/mine_rubert-tiny-lite/final/model.safetensors"]
    # NEGATIVE CONTROL: the literal half alone failing takes the whole record down, and the reason
    # reported is that failure's and not the pattern's.
    res2 = run_command_eval([sys.executable, "s.py"], str(tmp_path), 60, _METRIC,
                            subject=["never-written.jsonl"],
                            subject_glob=["experiments/*/final/model.safetensors"])
    assert res2.metric_subject["subject_bound"] is False
    assert res2.metric_subject["unbound_reason"] == "missing"
    assert res2.metric_subject["subjects"][1]["bound"] is True   # the pattern half still bound


def test_declaring_nothing_at_all_is_still_the_only_not_declared(tmp_path):
    """The `not_declared` path UNCHANGED where it is correct — the state 82 of 83 corpus metrics are
    in, and the one this change must not disturb. Both shapes absent, both empty, both whitespace."""
    for kw in ({}, {"subject": [], "subject_glob": []},
               {"subject": ["  "], "subject_glob": ["  "]}):
        res = run_command_eval([sys.executable, "-c", "print('{\"metric\": 0.5}')"], str(tmp_path),
                               60, _METRIC, **kw)
        assert res.metric == 0.5
        # Either spelling of "the operator said nothing", and no third one: the library records
        # NOTHING for an absent declaration (the engine stamps `absent_metric_subject()` there) and
        # `not_declared` for a whitespace-only one, which is the pre-existing behaviour for a literal
        # `subject` and must stay identical for a pattern.
        prov = res.metric_subject
        assert prov is None or prov["unbound_reason"] == "not_declared", (kw, prov)
    assert ms.bind([], str(tmp_path), since=None, globs=[])["unbound_reason"] == "not_declared"
    assert ms.absent_declaration()["unbound_reason"] == "not_declared"


def test_the_resolution_reads_the_filesystem_and_never_what_the_candidate_wrote_about_itself(tmp_path):
    """docs/36 — a wider action space must not widen the trusted set. The one list that would ALWAYS
    disambiguate correctly is the agent's own `expect.files`, and it is exactly the list this may not
    consult: the candidate authors that manifest, so resolving through it would let it choose which
    of its four same-sized checkpoints the recorded number is a claim about.

    Driven, not pinned: the manifest names ONE of the ambiguous matches, and the binding must still
    refuse. `producer` is still recorded from that manifest for the match a pattern resolves on its
    own — descriptive, and it decides nothing, which is what this shows."""
    (tmp_path / "t.py").write_text("print('trained')\n", encoding="utf-8")
    (tmp_path / "s.py").write_text("import json; print(json.dumps({'metric': 0.7}))\n",
                                   encoding="utf-8")
    _experiments(tmp_path, "mine_rubert-tiny-lite", ckpts=(4515,))
    picked = "experiments/mine_rubert-tiny-lite/final/model.safetensors"
    stages = [{"name": "train", "command": [sys.executable, "t.py"],
               "expect": {"files": [picked]}},
              {"name": "score", "command": [sys.executable, "s.py"]}]
    res = run_command_eval([], str(tmp_path), 60, _METRIC, stages=stages,
                           subject_glob=["experiments/*/*/model.safetensors"])
    assert res.metric == 0.7
    assert res.metric_subject["unbound_reason"] == "ambiguous", res.metric_subject
    # …while an UNAMBIGUOUS pattern still records the producer link `expect`/`needs` become two
    # projections of, so nothing was lost by refusing to let that map break a tie.
    ok = run_command_eval([], str(tmp_path), 60, _METRIC, stages=stages,
                          subject_glob=["experiments/*/final/model.safetensors"])
    assert ok.metric_subject["subject_bound"] is True
    assert ok.metric_subject["subjects"][0]["producer"] == "train"
    assert ok.metric_subject["subject_stage"] == "score"


def test_a_pattern_declares_no_needs_on_the_protected_score_stage_at_any_rung(tmp_path):
    """`verify_stage_inputs` stats LITERAL paths, so a pattern could only become a `needs` by being
    resolved a SECOND time, at a second instant, against a workdir a stage may still be writing —
    two resolutions of one declaration that can disagree. What is given up is the LATENCY the derived
    contract buys and nothing else: an unresolvable pattern is `missing` at bind time and the node is
    unselectable under `require` either way."""
    (tmp_path / "looplab_stages.json").write_text(
        json.dumps({"stages": [{"name": "train", "command": ["python", "t.py"]}]}), encoding="utf-8")
    es = {"command": ["python", "score.py"],
          "metric": {"kind": "stdout_json", "key": "m",
                     "subject_glob": ["experiments/*/final/model.safetensors"]}}
    for mode in ("off", "audit", "require"):
        assert "needs" not in _stage_mixin(mode)._resolve_stages(str(tmp_path), es)[-1], mode
    # The LITERAL half is untouched — it still derives one under `require` and only there.
    es2 = dict(es, metric=dict(es["metric"], subject=["out/model.bin"]))
    assert _stage_mixin("require")._resolve_stages(str(tmp_path), es2)[-1]["needs"] == ["out/model.bin"]


def test_the_operator_may_declare_a_pattern_and_a_bad_shape_is_refused_at_submit():
    from pydantic import ValidationError

    from looplab.adapters.repo_task import EvalSpec
    ok = EvalSpec(command=["python", "s.py"],
                  metric={"kind": "stdout_json", "key": "m",
                          "subject_glob": ["./experiments/*/final/model.safetensors"]})
    assert ok.metric["subject_glob"] == ["experiments/*/final/model.safetensors"]   # same path rule
    for bad in (["/abs/*/model.bin"], ["../*/escape.bin"], "not-a-list", [7]):
        with pytest.raises(ValidationError):
            EvalSpec(command=["python", "s.py"],
                     metric={"kind": "stdout_json", "key": "m", "subject_glob": bad})
    # `**` is NOT refused: recursion is what makes a pattern match the several same-sized
    # `checkpoint-*/` beside `final/`, and that case is already a refusal at BIND time. A second,
    # weaker submit-time guard against a case the first one covers is how two rules come to disagree.
    assert EvalSpec(command=["python", "s.py"],
                    metric={"kind": "stdout_json", "key": "m",
                            "subject_glob": ["**/final/model.safetensors"]}
                    ).metric["subject_glob"] == ["**/final/model.safetensors"]


def test_the_unbound_vocabulary_stays_closed_and_the_new_slug_is_in_it():
    assert "ambiguous" in ms.UNBOUND_REASONS
    # Every slug a record can carry must have a message naming ITS fix — a reason that fell back to
    # the `unreadable` sentence would send the operator hunting the wrong failure.
    assert set(ms.UNBOUND_MESSAGES) == set(ms.UNBOUND_REASONS)


def test_a_pattern_bound_node_stays_selectable_through_a_real_fold(tmp_path):
    """The property that matters, driven end to end rather than asserted about a dict: a node whose
    subject was declared as a PATTERN and bound must be in `feasible_nodes()` under the rung that
    gates. Nothing on the selection path reads `metric_provenance`, so the only thing that could go
    wrong is the violation row, and that is what this observes."""
    from looplab.events.eventstore import EventStore
    from looplab.events.types import EV_NODE_CREATED, EV_NODE_EVALUATED, EV_RUN_STARTED
    wd = tmp_path / "node0"
    wd.mkdir()
    (wd / "s.py").write_text("import json; print(json.dumps({'metric': 0.72}))\n", encoding="utf-8")
    _experiments(wd, "dcl-unified_rubert-tiny-lite", ckpts=(2709, 3612, 4515))
    res = run_command_eval([sys.executable, "s.py"], str(wd), 60, _METRIC,
                           subject_glob=["experiments/*/final/model.safetensors"])
    store = EventStore(tmp_path / "events.jsonl")
    store.append(EV_RUN_STARTED, {"run_id": "r1", "task_id": "t", "goal": "g", "direction": "max"})
    store.append(EV_NODE_CREATED, {"node_id": 0, "parent_ids": [], "operator": "draft",
                                   "idea": {"operator": "draft", "params": {}}, "code": ""})
    store.append(EV_NODE_EVALUATED, {
        "node_id": 0, "metric": res.metric, "metric_provenance": res.metric_subject,
        "violations": unbound_subject_violation_rows(res.metric_subject, res.metric, "require")})
    st = fold(store.read_all())
    assert [n.id for n in st.feasible_nodes()] == [0]
    assert st.nodes[0].metric_provenance["subject_bound"] is True
    assert st.nodes[0].metric_provenance["subjects"][0]["glob"]


# --------------------------------------------------------------------------------------------
# The one attempt shape where an OLDER artifact is the subject on purpose


def _reuse_pipeline(wd: Path) -> list:
    """train writes the checkpoint ONCE — a second run of it leaves the earlier attempt's bytes in
    place, which is exactly the state a stage-scoped re-run scores and the state a NORMAL attempt
    must still call stale. No `expect` on `train`, so the same two stages can be driven both ways:
    the reuse skips the stage entirely, and the full re-run must reach `score` for the subject to be
    bound at all."""
    (wd / "t.py").write_text(
        "import pathlib\n"
        "p = pathlib.Path('out/model.bin'); p.parent.mkdir(parents=True, exist_ok=True)\n"
        "if not p.exists():\n"
        "    p.write_bytes(b'trained')\n", encoding="utf-8")
    (wd / "s.py").write_text("print('{\"metric\": 0.7}')\n", encoding="utf-8")
    return [{"name": "train", "command": [sys.executable, "t.py"]},
            {"name": "score", "command": [sys.executable, "s.py"], "needs": ["out/model.bin"]}]


def test_a_checkpoint_the_engine_itself_chose_to_reuse_is_the_subject_not_a_stale_leftover(tmp_path):
    """The measured shape: `runs/rubertlite-dense-retrieval` has 21 nodes whose `train` row is
    `reused` (0.0 s) with `score` re-run beside it, and `runs/rubert-dr-0807` three more. On every
    one of those the checkpoint predates the attempt BY CONSTRUCTION — that is what `start_stage`
    means — so binding the subject against this attempt's clock calls the engine's own reuse
    `stale`, records an operator-blaming message under the shipped `audit` default, and under
    `require` mints the `metric_salvaged` row that takes the node out of `feasible_nodes()`.

    Both directions are driven here, because scoping this rule and DELETING it look identical from
    the reuse side: the same on-disk state under a NORMAL attempt must still be stale (the positive
    control), and so must a `start_stage` that names no stage in the pipeline — that reuses nothing,
    the pipeline runs from the top, and the fail-safe direction is the strict floor.
    """
    import hashlib
    stages = _reuse_pipeline(tmp_path)
    kw = dict(stages=stages, subject=["out/model.bin"])

    first = run_command_eval([], str(tmp_path), 60, _METRIC, **kw)
    assert first.metric == 0.7 and first.metric_subject["subject_bound"] is True

    # Age it past `_FRESH_EPS`: from here on it is an EARLIER attempt's checkpoint in the workdir
    # every later attempt reuses — indistinguishable on disk from the leftover the rule exists for.
    old = os.path.getmtime(tmp_path / "out" / "model.bin") - 3600
    os.utime(tmp_path / "out" / "model.bin", (old, old))

    reused = run_command_eval([], str(tmp_path), 60, _METRIC, start_stage="score", **kw)
    assert [s["status"] for s in reused.stages] == ["reused", "ok"]
    prov = reused.metric_subject
    assert prov["subject_bound"] is True, prov
    assert "unbound_reason" not in prov
    # …bound to the REAL bytes, not merely flagged as acceptable.
    assert prov["subjects"][0]["digest"] == hashlib.sha256(b"trained").hexdigest()
    assert unbound_subject_violation_rows(prov, reused.metric, "require") == []

    # POSITIVE CONTROL, same bytes and same workdir: a full attempt re-ran `train`, it wrote nothing
    # new, and this attempt's number cannot be about the old checkpoint.
    normal = run_command_eval([], str(tmp_path), 60, _METRIC, **kw)
    assert [s["status"] for s in normal.stages] == ["ok", "ok"]
    assert normal.metric == 0.7                              # the number is read either way…
    assert normal.metric_subject["subject_bound"] is False    # …and has no referent
    assert normal.metric_subject["unbound_reason"] == "stale"
    rows = unbound_subject_violation_rows(normal.metric_subject, normal.metric, "require")
    assert [r["name"] for r in rows] == [SALVAGE_VIOLATION]

    # SECOND CONTROL: an unknown `start_stage` reuses NOTHING (`_run_stages` runs from index 0), so
    # the relaxation must not fire on the mere presence of the argument.
    typo = run_command_eval([], str(tmp_path), 60, _METRIC, start_stage="scor", **kw)
    assert [s["status"] for s in typo.stages] == ["ok", "ok"]
    assert typo.metric_subject["unbound_reason"] == "stale"


def test_the_subject_and_the_secondary_readers_share_one_derivation_of_this_attempts_freshness():
    """The two decisions were derived 300 lines apart and disagreed, which is the whole defect: the
    tail relaxed the constraint/extra/cross-check readers under a stage-scoped re-run while the
    subject binding kept the strict floor, so a reuse kept its gates and lost its metric's referent.
    One rule, stated where it can be reviewed."""
    from looplab.runtime.command_eval import attempt_freshness_floor, reused_stage_count
    stages = [{"name": "train", "command": []}, {"name": "score", "command": []}]
    assert reused_stage_count(stages, "score") == 1          # `train` is reused
    assert reused_stage_count(stages, "train") == 0          # re-run from the top: nothing reused
    assert reused_stage_count(stages, "typo") == 0           # unknown name -> full re-run
    assert reused_stage_count(None, "score") == 0            # a single command reuses nothing
    assert attempt_freshness_floor(1000.0, stages, "score") is None
    assert attempt_freshness_floor(1000.0, stages, "train") == 1000.0
    assert attempt_freshness_floor(1000.0, stages, "typo") == 1000.0
    assert attempt_freshness_floor(1000.0, None, "score") == 1000.0
    assert attempt_freshness_floor(1000.0) == 1000.0


def test_a_reused_nodes_metric_stays_selectable_under_require(tmp_path):
    """The property that matters, driven through a real eval and a real fold: the reuse the engine
    chose must leave the node in `feasible_nodes()`. A record that merely says `subject_bound` while
    the violation row is still minted would fail the same way the defect did."""
    from looplab.events.eventstore import EventStore
    from looplab.events.types import EV_NODE_CREATED, EV_NODE_EVALUATED, EV_RUN_STARTED
    wd = tmp_path / "node0"
    wd.mkdir()
    stages = _reuse_pipeline(wd)
    kw = dict(stages=stages, subject=["out/model.bin"])
    run_command_eval([], str(wd), 60, _METRIC, **kw)                      # attempt 1: trains
    old = os.path.getmtime(wd / "out" / "model.bin") - 3600
    os.utime(wd / "out" / "model.bin", (old, old))
    res = run_command_eval([], str(wd), 60, _METRIC, start_stage="score", **kw)   # the re-run

    store = EventStore(tmp_path / "events.jsonl")
    store.append(EV_RUN_STARTED, {"run_id": "r1", "task_id": "t", "goal": "g", "direction": "max"})
    store.append(EV_NODE_CREATED, {"node_id": 0, "parent_ids": [], "operator": "draft",
                                   "idea": {"operator": "draft", "params": {}}, "code": ""})
    store.append(EV_NODE_EVALUATED, {
        "node_id": 0, "metric": res.metric, "metric_provenance": res.metric_subject,
        "violations": unbound_subject_violation_rows(res.metric_subject, res.metric, "require")})
    st = fold(store.read_all())
    assert st.nodes[0].feasible is not False
    assert [n.id for n in st.feasible_nodes()] == [0]
    assert st.nodes[0].metric_provenance["subject_bound"] is True


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
    `run_argv` is byte-identical to what it was.

    Reads the marker under its OWN name (`landlock.LANDLOCK_ENV`) rather than a literal: the two
    spellings drifted apart deliberately, and a literal here would go on asserting the absence of
    the wrong variable.
    """
    from looplab.runtime.sandbox import run_argv
    rc, out, _err, _to = run_argv(
        [sys.executable, "-c",
         f"import os; print(os.environ.get({landlock.LANDLOCK_ENV!r}, 'ABSENT'))"],
        str(tmp_path), 60)
    assert rc == 0 and out.strip() == "ABSENT"


def test_the_policy_env_var_and_the_wire_allowlist_var_are_different_names(monkeypatch, tmp_path):
    """`LOOPLAB_LANDLOCK` is the OPERATOR's policy knob; it must never reach `parse_env`.

    `Settings` is flat with `env_prefix="LOOPLAB_"`, so `Settings.landlock` OWNS `LOOPLAB_LANDLOCK` —
    and that is the spelling this repo's own validation instruction hands the operator
    (`cli/inspect_cmds.py`: "run ONE real eval with `LOOPLAB_LANDLOCK=enforce`"; the `landlock` row in
    `docs/guide/configuration.md`). `run_argv` builds its child env from `os.environ` first, so while
    the derived wire allow-list shared that name the policy word `enforce` arrived at `parse_env` as
    an allow-list record and raised `LandlockUnavailable` out of the UNIVERSAL launch choke point —
    every stage, the metric adapter, `run_setup` and every Developer probe, with no return code, no
    stderr and no node terminal. Driving `run_argv` is the point: a pin on the two constants alone
    would not have caught it, because the collision is with `Settings`' derived env name and not with
    any literal in this module.
    """
    from looplab.core.config import Settings
    from looplab.runtime.sandbox import run_argv

    monkeypatch.setenv("LOOPLAB_LANDLOCK", "enforce")
    # The operator really did set the knob they were told to set...
    assert Settings().landlock == "enforce"
    # ...and the launch choke point still launches. THIS is the assertion that goes red on the
    # defect — the crash is a raise out of `run_argv`, not a wrong value anywhere.
    # (`landlock="off"` is still the effective rung: nothing derived an allow-list, which is exactly
    # why a policy word must not be able to act as one.)
    rc, out, _err, _to = run_argv(
        [sys.executable, "-c", "print('RAN')"], str(tmp_path), 60)
    assert rc == 0 and out.strip() == "RAN"
    # The separation itself, so a later merge cannot quietly re-collide the two names.
    assert landlock.LANDLOCK_ENV != "LOOPLAB_LANDLOCK"


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


def _stage_mixin(mode):
    from looplab.engine.eval_stages import EvalStagesMixin
    return type("_E", (EvalStagesMixin,), {"metric_subject": mode})()


def test_the_engine_appended_score_stage_declares_what_it_reads(tmp_path):
    """Doc 35 §3a's ONE missing wire: `needs` shipped, and the only stage the operator owns was the
    only stage that could not declare it — under the rung that GATES, which is `require` alone."""
    (tmp_path / "looplab_stages.json").write_text(
        json.dumps({"stages": [{"name": "train", "command": ["python", "t.py"]}]}), encoding="utf-8")
    es = {"command": ["python", "score.py"],
          "metric": {"kind": "stdout_json", "key": "m", "subject": ["out/model.bin"]}}
    chain = _stage_mixin("require")._resolve_stages(str(tmp_path), es)
    assert [s["name"] for s in chain] == ["train", "score"]
    assert chain[-1]["needs"] == ["out/model.bin"]
    # …and NEITHER of the two rungs below it acquires a stage contract. `off` is the historical
    # behaviour; `audit` is the one that regressed — it is documented as "no selection effect", and a
    # `needs` here makes a declared-but-missing subject cost the node its whole metric, which is
    # strictly harsher than the `require` rung it is supposed to be milder than.
    for mode in ("off", "audit"):
        assert "needs" not in _stage_mixin(mode)._resolve_stages(str(tmp_path), es)[-1], mode


def test_the_stage_contract_is_only_ever_written_for_a_DECLARED_subject(tmp_path):
    """Why the live blast radius was nil, as a property rather than a corpus count: no `subject`, no
    `needs`, at every rung. Measured 2026-08-14, 0 of the 113 task snapshots under `runs/` and
    `examples/` declare `eval.metric.subject` — `rubertlite-dr-unified-v8`, running at the `audit`
    default, among them."""
    (tmp_path / "looplab_stages.json").write_text(
        json.dumps({"stages": [{"name": "train", "command": ["python", "t.py"]}]}), encoding="utf-8")
    for metric in ({"kind": "stdout_json", "key": "m"},                  # no subject key at all
                   {"kind": "stdout_json", "key": "m", "subject": []},   # declared empty
                   {"kind": "stdout_json", "key": "m", "subject": ["  "]}):   # whitespace only
        for mode in ("off", "audit", "require"):
            es = {"command": ["python", "score.py"], "metric": metric}
            assert "needs" not in _stage_mixin(mode)._resolve_stages(str(tmp_path), es)[-1]


@pytest.mark.parametrize("mode", ["off", "audit", "require"])
def test_a_present_subject_is_untouched_at_every_rung(tmp_path, mode):
    """The control the other three cases are read against: when the pipeline produces what the
    operator declared, every rung scores the node and records the same identity."""
    wd = tmp_path / mode
    wd.mkdir()
    chain = [{"name": "train", "command": _writer(wd, "out/model.bin")},
             {"name": "score", "command": [sys.executable, "s.py"]}]
    if mode == "require":
        chain[-1]["needs"] = ["out/model.bin"]
    (wd / "s.py").write_text("import json; print(json.dumps({'metric': 0.5}))\n", encoding="utf-8")
    res = run_command_eval([sys.executable, "s.py"], str(wd), 60, _METRIC, stages=chain,
                           subject=(["out/model.bin"] if mode != "off" else None))
    assert res.metric == 0.5 and res.failed_stage is None
    assert [s["status"] for s in res.stages] == ["ok", "ok"]
    if mode == "off":
        assert res.metric_subject is None            # `off` is byte-identical to before this shipped
    else:
        assert res.metric_subject["subject_bound"] is True


def test_a_MISSING_subject_under_audit_keeps_the_metric_and_names_the_real_reason(tmp_path):
    """THE DEFECT, driven. `audit` is documented "RECORD, always … no violation, no selection
    effect", and the derived `needs` made it the harshest rung of the three: the scorer never ran,
    the node's metric was None, and the record said `not_declared` about a subject that WAS
    declared — the one distinction `UNBOUND_REASONS` is a closed vocabulary to preserve."""
    (tmp_path / "t.py").write_text("print('trained, wrote nothing')\n", encoding="utf-8")
    (tmp_path / "s.py").write_text("import json; print(json.dumps({'metric': 0.5}))\n",
                                   encoding="utf-8")
    chain = [{"name": "train", "command": [sys.executable, "t.py"]},
             {"name": "score", "command": [sys.executable, "s.py"]}]   # no `needs`: the audit rung
    res = run_command_eval([sys.executable, "s.py"], str(tmp_path), 60, _METRIC, stages=chain,
                           subject=["out/model.bin"])
    assert res.metric == 0.5                                  # the number survives — no gate
    assert res.failed_stage is None and [s["status"] for s in res.stages] == ["ok", "ok"]
    assert res.metric_subject["subject_bound"] is False
    assert res.metric_subject["unbound_reason"] == "missing"  # …and NOT "not_declared"
    # The enforcement rung is where the consequence lives, and it is the DOCUMENTED one: the metric
    # is kept and carries the `metric_salvaged` violation, rather than never being read.
    assert unbound_subject_violation_rows(res.metric_subject, res.metric, "audit") == []
    rows = unbound_subject_violation_rows(res.metric_subject, res.metric, "require")
    assert rows[0]["value"] == 0.5 and rows[0]["salvage"]["unbound_reason"] == "missing"
    assert "not produced" in rows[0]["salvage"]["detail"]     # the fix for THIS reason, not another


def test_a_MISSING_subject_under_require_refuses_early_and_still_records_the_true_reason(tmp_path):
    """`require` keeps the gate, and that is the whole of what it keeps: the refusal fires before the
    scorer runs (latency), and because the subject is bound BEFORE the input contract the record
    still names `missing` rather than degrading to the engine's absent-declaration fallback."""
    from looplab.runtime.command_eval import NEEDS_IS_METRIC_SUBJECT

    (tmp_path / "t.py").write_text("print('trained, wrote nothing')\n", encoding="utf-8")
    (tmp_path / "s.py").write_text("import json; print(json.dumps({'metric': 0.5}))\n",
                                   encoding="utf-8")
    chain = [{"name": "train", "command": [sys.executable, "t.py"]},
             {"name": "score", "command": [sys.executable, "s.py"], "needs": ["out/model.bin"]}]
    res = run_command_eval([sys.executable, "s.py"], str(tmp_path), 60, _METRIC, stages=chain,
                           subject=["out/model.bin"])
    assert res.metric is None and res.failed_stage == "score"
    assert [s["status"] for s in res.stages] == ["ok", "needs_failed"]
    assert res.metric_subject["unbound_reason"] == "missing"
    # WHOSE failure the message says it is. The Developer may edit neither the protected `score`
    # stage nor the operator's declaration, so the refusal must name the one repair it does own.
    concern = res.stages[-1]["concern"]
    assert NEEDS_IS_METRIC_SUBJECT.strip()[:40] in concern
    assert "PRODUCE this path" in concern and "OPERATOR" in concern


def test_a_stage_needs_that_is_not_the_subject_keeps_the_plain_refusal(tmp_path):
    """The sentence above is scoped to the DERIVED contract. An agent-authored `needs` between two
    of its own stages is a stage the agent CAN edit, and telling it otherwise would be false."""
    from looplab.runtime.command_eval import NEEDS_IS_METRIC_SUBJECT

    (tmp_path / "t.py").write_text("print('trained')\n", encoding="utf-8")
    (tmp_path / "s.py").write_text("import json; print(json.dumps({'metric': 0.5}))\n",
                                   encoding="utf-8")
    chain = [{"name": "train", "command": [sys.executable, "t.py"]},
             {"name": "score", "command": [sys.executable, "s.py"], "needs": ["out/other.bin"]}]
    res = run_command_eval([sys.executable, "s.py"], str(tmp_path), 60, _METRIC, stages=chain,
                           subject=["out/model.bin"])
    assert res.stages[-1]["status"] == "needs_failed"
    assert NEEDS_IS_METRIC_SUBJECT.strip()[:40] not in res.stages[-1]["concern"]


def test_needs_alone_does_not_catch_the_incident_and_this_is_why_it_is_not_the_gate(tmp_path):
    """The measurement that demotes floor option 1. `verify_stage_inputs` is a PRESENCE check: the
    node really did write the checkpoint its `needs` names, so the contract passes and the stage
    goes on to read somewhere else entirely. Reproduced here in miniature."""
    from looplab.runtime.command_eval import verify_stage_inputs
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "model.bin").write_bytes(b"the node's own, correct, fresh checkpoint")
    # No `since=`: `verify_stage_inputs` deliberately has NO freshness rule (its docstring says so —
    # an input legitimately predates its stage), and passing one would contradict the very point this
    # test makes. That absence is exactly WHY `needs` cannot be the gate: it is a presence check, the
    # node really did write this file, and the stage read somewhere else anyway.
    assert verify_stage_inputs(["out/model.bin"], str(tmp_path), stage="score") is None


def test_the_run_report_names_an_unbound_node_rather_than_silently_excluding_it(tmp_path):
    """It rides the `metric_salvaged` row, so without its own branch it falls out of BOTH of the
    report's exclusion lists — excluded and unmentioned, which is the failure this mechanism is
    against."""
    from looplab.events.eventstore import EventStore
    from looplab.events.types import EV_NODE_CREATED, EV_NODE_EVALUATED, EV_RUN_STARTED
    from looplab.serve.report import _report_context
    store = EventStore(tmp_path / "events.jsonl")
    store.append(EV_RUN_STARTED, {"run_id": "r1", "task_id": "t", "goal": "g", "direction": "max"})
    store.append(EV_NODE_CREATED, {"node_id": 0, "parent_ids": [], "operator": "draft",
                                   "idea": {"operator": "draft", "params": {"x": 1.0}}, "code": ""})
    prov = {"subject_bound": False, "unbound_reason": "not_declared", "subjects": []}
    store.append(EV_NODE_EVALUATED, {
        "node_id": 0, "metric": 0.9, "metric_provenance": prov,
        "violations": unbound_subject_violation_rows(prov, 0.9, "require")})
    text = _report_context(fold(store.read_all()))
    assert "bound to NO subject" in text and "eval.metric.subject" in text
    # …and it must NOT be reported as a constraint breach, which would accuse the run of something
    # that did not happen.
    assert "violated a constraint" not in text


def test_a_task_that_declares_no_subject_is_recorded_as_unbound_not_as_silence():
    """`not_declared` is the state 82 of 83 corpus metrics are in — the universal case, not the rare
    one. A `require` rung that recorded only MIS-declared subjects would fire on the exception and
    never on the rule.

    The rule is a NAMED function rather than a dict literal at the engine's call site precisely so it
    can be driven here: buried inside `_run_eval` it is reachable only through a whole simulated eval
    (CLAUDE.md's ladder, tier 2). Its shape must equal what `bind([])` returns, or the engine and the
    runtime would have two spellings of "no subject" for a reader to recognise.
    """
    from looplab.runtime.command_eval import absent_metric_subject
    assert ms.absent_declaration() == ms.bind([], "", since=None)
    assert absent_metric_subject() == ms.absent_declaration()
    rows = unbound_subject_violation_rows(absent_metric_subject(), 0.5, "require")
    assert [r["name"] for r in rows] == [SALVAGE_VIOLATION]
    assert rows[0]["salvage"]["unbound_reason"] == "not_declared"


def test_the_eval_dispatch_branch_records_the_absent_declaration_itself():
    """That the engine CALLS the rule, from the branch that is scoped to an operator eval spec.

    Tier 3 (AST, never substrings) and it says only what tier 3 can say: the call is in the text of
    `_run_eval`. What it protects is the scoping — a toy/dataset eval has no `eval.metric` field and
    must not be told it forgot one, so the call has to live inside `if self._eval_spec:` and nowhere
    broader.
    """
    import ast
    import inspect

    from looplab.engine.eval_dispatch import EvalDispatchMixin
    tree = ast.parse(inspect.getsource(EvalDispatchMixin._run_eval).lstrip())
    called = {n.func.attr for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert "absent_metric_subject" in called
    # …and it is inside the `self._eval_spec` branch, not beside it.
    guarded = [b for b in ast.walk(tree)
               if isinstance(b, ast.If)
               and any(isinstance(x, ast.Attribute) and x.attr == "_eval_spec"
                       for x in ast.walk(b.test))]
    assert guarded, "the operator-eval-spec branch is gone — re-derive this scoping"
    inner = {n.func.attr for b in guarded for n in ast.walk(b)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert "absent_metric_subject" in inner


# --------------------------------------------------------------------------------------------
# `looplab landlock-check` — the GATE over that derivation
#
# It is the documented procedure for flipping `Settings.landlock` to enforce, and nothing drove its
# body until 2026-08-15. It read `task.get("repo")` — the repo ROOT PATH, censused `None` 45 times and
# `str` once across the 46 preserved `runs/*/task.snapshot.json`, never a dict — so `spec` was always
# None and the operator's mounts were dropped from a list it then reported `skipped: 0` about, or the
# `str` shape (which ships in `examples/repo_composable_task.json`) raised `AttributeError` out of
# `mount_sources`. These drive the COMMAND, not the derivation under it.


def _snapshot_run(tmp_path, name: str, task: dict) -> Path:
    run_dir = tmp_path / name
    run_dir.mkdir(parents=True)
    (run_dir / "task.snapshot.json").write_text(json.dumps(task), encoding="utf-8")
    return run_dir


def _repo_task(tmp_path, **extra) -> dict:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    (repo / "ttrain.py").write_text("print('{\"metric\": 1.0}')\n", encoding="utf-8")
    task = {"id": "t", "goal": "g", "direction": "max", "repo": str(repo),
            "cmd": {"command": ["python", "ttrain.py"],
                    "metric": {"reader": "stdout_json", "key": "metric"}, "timeout": 60}}
    task.update(extra)
    return task


def _landlock_check(run_dir: Path):
    from typer.testing import CliRunner

    from looplab.cli import app
    return CliRunner().invoke(app, ["landlock-check", str(run_dir), "--no-probe"])


@pytest.mark.skipif(_NO_LANDLOCK is not None, reason=str(_NO_LANDLOCK))
def test_landlock_check_verifies_the_operators_own_declared_mounts(tmp_path):
    """The whole point of the gate: what it prints has to CONTAIN the mounts the task declares, and
    say how many it found — `mounts: 0` on a repo task is the fact the operator checks against their
    own `data:` block, and an omission that is not printed is how the old version read as a pass."""
    corpus, papers = tmp_path / "corpus", tmp_path / "papers"
    corpus.mkdir()
    papers.mkdir()
    run_dir = _snapshot_run(tmp_path, "run", _repo_task(
        tmp_path,
        data={"corpus": {"path": str(corpus), "mount": True, "edit": False}},
        references=[{"name": "papers", "path": str(papers)}]))
    result = _landlock_check(run_dir)
    assert result.exit_code == 0, result.output
    assert "declared mounts from task.snapshot.json (2)" in result.output
    assert str(corpus) in result.output and str(papers) in result.output
    assert "skipped: 0" in result.output
    # ...and they are in the RULESET, not merely echoed above it.
    rules = result.output.split("allow-list (", 1)[1]
    assert f"read      {os.path.realpath(corpus)}" in rules
    assert f"read      {os.path.realpath(papers)}" in rules


@pytest.mark.skipif(_NO_LANDLOCK is not None, reason=str(_NO_LANDLOCK))
def test_landlock_check_survives_the_shipped_string_repo_shape(tmp_path):
    """`repo` is a PATH. Handing it to `mount_sources` raised `AttributeError: 'str' object has no
    attribute 'get'` — the shape `examples/repo_composable_task.json` ships and one preserved run
    carries."""
    result = _landlock_check(_snapshot_run(tmp_path, "run", _repo_task(tmp_path)))
    assert result.exit_code == 0, result.output
    assert "AttributeError" not in result.output
    assert "declared mounts from task.snapshot.json (0)" in result.output


@pytest.mark.skipif(_NO_LANDLOCK is not None, reason=str(_NO_LANDLOCK))
def test_landlock_check_refuses_rather_than_passing_when_it_cannot_see_the_task(tmp_path):
    """A gate that cannot see the thing it gates must fail closed. "This task declares no mounts" and
    "I could not look" must not print the same, because what follows a green light is
    `LOOPLAB_LANDLOCK=enforce` on a real GPU eval and a missing mount refuses mid-training."""
    from looplab.cli import REFUSAL_EXIT_CODE

    empty = tmp_path / "no-snapshot"
    empty.mkdir()
    missing = _landlock_check(empty)
    assert missing.exit_code == REFUSAL_EXIT_CODE
    assert "cannot see the mounts it exists to verify" in missing.output
    assert "skipped: 0" not in missing.output and "allow-list (" not in missing.output

    broken = tmp_path / "corrupt"
    broken.mkdir()
    (broken / "task.snapshot.json").write_text("{not json", encoding="utf-8")
    result = _landlock_check(broken)
    assert result.exit_code == REFUSAL_EXIT_CODE
    assert "cannot rebuild the task" in result.output and "allow-list (" not in result.output


@pytest.mark.skipif(_NO_LANDLOCK is not None, reason=str(_NO_LANDLOCK))
def test_landlock_check_still_exits_nonzero_when_a_declared_mount_is_not_on_this_box(tmp_path):
    """The fail-closed half that already worked, now reachable because the mount survives to the
    ruleset at all: a declared path this box does not have is a rule Landlock cannot add, i.e. a
    DENIAL, and a green `skipped: 0` about it would be the same lie one rung later."""
    run_dir = _snapshot_run(tmp_path, "run", _repo_task(
        tmp_path, data={"corpus": {"path": str(tmp_path / "absent"), "mount": True}}))
    result = _landlock_check(run_dir)
    assert result.exit_code == 1
    assert "NOT on this box" in result.output
    assert "would be DENIED" in result.output


@pytest.mark.skipif(_NO_LANDLOCK is not None, reason=str(_NO_LANDLOCK))
def test_landlock_check_names_the_kind_when_a_task_genuinely_declares_nothing(tmp_path):
    """A toy/dataset run has no repo spec at all. That is an ANSWER and prints as one — naming the
    kind it read, so the line can never be confused with the refusal above."""
    result = _landlock_check(_snapshot_run(tmp_path, "run", {
        "kind": "quadratic", "id": "q", "goal": "min", "direction": "min"}))
    assert result.exit_code == 0, result.output
    assert "declares no repo spec" in result.output and "'quadratic'" in result.output
