"""A number measured under a different RULER may not be ranked against this one (doc 68 §1).

THE HOLE, found by the critic's pass over doc 68 on 2026-09-26 and live in the default config:
`eval_stages.py::_eval_pipeline` scores a node at `idea.eval_profile`, or at the Strategist's
fidelity, and the RuleStrategist puts seed-phase nodes on `smoke` and endgame nodes on `full`.
`select_best_node` then ranks every node in one pool on one `node.metric`, and nothing on either
record said which profile its number came from — so a `full` number and a `smoke` number were
ordered as if one ruler had produced both. The operator's own incident (doc 68 §0 a) is the other
half: an eval decoder silently inherited each checkpoint's `generation_config`, one backbone was
scored at `repetition_penalty=1.1` and the rest at 1.0, and no record the engine owns could see it.

What shipped, and what each test below drives:
  * `command_eval.eval_protocol` — the resolved profile's OVERRIDES, by `build_command`'s own rule;
  * `sandbox.json_line_fingerprint` — an `eval_fingerprint` the eval prints about itself;
  * `comparability.protocol_record` — both, plus the host scorer's program digest, as REFUSAL-ONLY
    facets beside the key (`substrate`'s rule, per facet: both sides must carry one to refuse, and
    agreeing certifies nothing), so the within-run champion caveat and every ranking surface refuse
    a mixed field without a rule of their own.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import anyio
import pytest

from looplab.adapters.repo_task import EvalSpec, NoOpRepoDeveloper, RepoParamResearcher, RepoTask
from looplab.core.models import Idea, Node
from looplab.engine.champion_caveats import (CHAMPION_CAVEAT_MIXED_COMPARABILITY,
                                             champion_metric_caveats)
from looplab.engine.comparability import (DIFFERENT, SAME, UNKNOWN, comparability_notice,
                                          comparability_record, comparability_status,
                                          group_token, protocol_record, record_of)
from looplab.runtime import command_eval
from looplab.runtime.sandbox import EVAL_FINGERPRINT_MAX_CHARS, json_line_fingerprint
from tests.factories import make_engine

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "repo_fixture"
_M = {"kind": "stdout_json", "key": "metric"}
_SMOKE = {"overrides": ["steps=1"], "timeout": 30}
_FULL = {"overrides": ["steps=100"], "timeout": 60}
_KEY = {"version": 1, "authority": "measured", "keys": {"measured": "K" * 16}}


# ------------------------------------------------------------------ 1. the protocol, by one rule
@pytest.mark.parametrize("profiles,asked", [
    ({"smoke": _SMOKE, "full": _FULL}, None),
    ({"smoke": _SMOKE, "full": _FULL}, "full"),
    ({"smoke": _SMOKE, "full": _FULL}, "smoke"),
    ({"smoke": _SMOKE}, "full"),          # an undeclared `full` is the BASE command, never smoke
    ({"full": _FULL}, None),              # an undeclared `smoke` default is the base command too
    ({}, None),
])
def test_the_protocol_is_exactly_what_build_command_appends(profiles, asked):
    """ONE rule: the overrides the protocol records are the tokens `build_command` put on the
    command, so the record cannot describe a ruler the eval did not run under."""
    spec = {"command": ["python", "score.py"], "metric": _M, "profiles": profiles}
    cmd, _timeout = command_eval.build_command(spec, {}, asked)
    protocol = command_eval.eval_protocol(spec, asked)
    assert cmd[2:] == protocol["overrides"]
    assert protocol["profile"] == ((asked or "smoke")
                                   if (asked or "smoke") in profiles else None)


def test_two_undeclared_names_are_one_ruler_and_the_timeout_is_not_part_of_it():
    """The OVERRIDES decide, not the name: an undeclared `smoke` and an undeclared `full` both run
    the base command, so they must not refuse each other. And a timeout moves under an operator's
    `budget_extend` mid-run — folding it in would refuse every pair either side of the extension."""
    base = {"command": ["python", "score.py"], "metric": _M}
    assert (protocol_record(eval_protocol=command_eval.eval_protocol(base, "full"))
            == protocol_record(eval_protocol=command_eval.eval_protocol(base, None)))
    slow = {**base, "profiles": {"smoke": {**_SMOKE, "timeout": 999}}}
    fast = {**base, "profiles": {"smoke": _SMOKE}}
    assert (protocol_record(eval_protocol=command_eval.eval_protocol(slow, None))
            == protocol_record(eval_protocol=command_eval.eval_protocol(fast, None)))


def test_a_grandfathered_profile_is_recorded_as_build_command_ran_it_and_junk_never_raises():
    """A grandfathered snapshot's `overrides` may be a bare string, which `build_command` appends
    CHARACTER BY CHARACTER — the record must say the same thing the command did, however odd.
    A profile `build_command` itself refuses (not a mapping) is its refusal to make; the record
    answers `[]` rather than raising when asked directly."""
    odd = {"command": ["x"], "profiles": {"smoke": {"overrides": "steps=1"}}}
    cmd, _t = command_eval.build_command(odd, {}, None)
    assert command_eval.eval_protocol(odd, None)["overrides"] == cmd[1:] == list("steps=1")
    for junk in ({"smoke": "not-a-dict"}, ["smoke"]):
        with pytest.raises(AttributeError):
            command_eval.build_command({"command": ["x"], "profiles": junk}, {}, None)
        assert command_eval.eval_protocol({"command": ["x"], "profiles": junk}, None)["overrides"] == []


# ------------------------------------------------------------------- 2. what the eval says itself
def _fp(value) -> str:
    """What `json_line_fingerprint` returns for a printed value: the sha256 of its canonical JSON."""
    import hashlib
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True).encode("ascii")).hexdigest()


def test_the_last_printed_fingerprint_wins_and_junk_is_silence():
    text = "\n".join([
        json.dumps({"eval_fingerprint": {"repetition_penalty": 1.0}}),
        "chatter",
        json.dumps({"metric": 0.07, "eval_fingerprint": {"repetition_penalty": 1.1, "beams": 5}}),
        "trailing chatter",
    ])
    # A DIGEST of the canonical JSON, key order irrelevant — never the value itself.
    assert json_line_fingerprint(text) == _fp({"beams": 5, "repetition_penalty": 1.1})
    assert json_line_fingerprint(json.dumps({"eval_fingerprint": "decoder-v2"})) == _fp("decoder-v2")
    for silent in ("", "no json here", json.dumps({"metric": 1.0}),
                   json.dumps({"eval_fingerprint": None}), json.dumps({"eval_fingerprint": {}}),
                   json.dumps({"eval_fingerprint": ""}), json.dumps({"eval_fingerprint": []})):
        assert json_line_fingerprint(silent) is None, silent
    # REFUSED past the bound, never truncated: two long values sharing a prefix must not read equal.
    huge = json.dumps({"eval_fingerprint": "x" * (EVAL_FINGERPRINT_MAX_CHARS + 1)})
    assert json_line_fingerprint(huge) is None


def test_a_deeply_nested_fingerprint_can_not_take_the_node_down():
    """THE CRITIC'S CRASH, driven at the unit that used to carry it: a ~2 kB list 983 levels deep
    passed the size bound, and the terminal's digest then raised `RecursionError` — `node_failed
    engine_error`, run paused. The worker now hands on a flat digest (or nothing), and the digest
    of a digest cannot recurse."""
    for depth in (900, 983, 5000):
        line = json.dumps({"metric": 0.5, "eval_fingerprint": 0}).replace(
            "0}", "[" * depth + "]" * depth + "}")
        digest = json_line_fingerprint(line)
        assert digest is None or len(digest) == 64
        record = protocol_record(fingerprint=digest)
        assert record is None or set(record) == {"fingerprint"}


def test_a_numeric_fingerprint_is_not_captured_as_an_extra_metric():
    """`json_line_extras` scrapes every other numeric key off the metric line; a fingerprint is a
    statement ABOUT the measurement and must not surface as a second metric beside it."""
    from looplab.runtime.sandbox import json_line_extras

    line = json.dumps({"metric": 0.5, "eval_fingerprint": 2.0, "recall": 0.9})
    assert json_line_extras(line) == {"recall": 0.9}


# -------------------------------------------------------------------- 3. the rule over records
def _with(protocol):
    return {**_KEY, "protocol": protocol} if protocol else dict(_KEY)


def test_a_protocol_mismatch_refuses_and_says_which_ruler_moved():
    smoke = protocol_record(eval_protocol={"overrides": ["steps=1"]})
    full = protocol_record(eval_protocol={"overrides": ["steps=100"]})
    assert comparability_status(_with(smoke), _with(full)) == DIFFERENT
    notice = comparability_notice(_with(smoke), _with(full))
    assert notice.startswith("NOT COMPARABLE") and "eval profile" in notice
    edited = protocol_record(host_scorer={"program_sha256": "a" * 64})
    original = protocol_record(host_scorer={"program_sha256": "b" * 64})
    assert "host scorer" in comparability_notice(_with(edited), _with(original))
    printed = protocol_record(fingerprint=_fp({"repetition_penalty": 1.1}))
    other = protocol_record(fingerprint=_fp({"repetition_penalty": 1.0}))
    assert "eval_fingerprint" in comparability_notice(_with(printed), _with(other))


def test_agreement_certifies_nothing_and_absence_refuses_nothing():
    """The inversion, per facet: a matching protocol at a non-certifying authority is still
    UNKNOWN, and a facet only one side recorded — a run that began printing a fingerprint half-way —
    is silence rather than a second ruler."""
    inferred = {"version": 1, "authority": "inferred", "keys": {"inferred": "I" * 16}}
    same = protocol_record(eval_protocol={"overrides": []})
    assert comparability_status({**inferred, "protocol": same},
                                {**inferred, "protocol": dict(same)}) == UNKNOWN
    one_sided = protocol_record(eval_protocol={"overrides": []}, fingerprint=_fp("decoder-v2"))
    assert comparability_status(_with(one_sided), _with(same)) == SAME
    assert comparability_status(_with(None), _with(one_sided)) == SAME


def test_the_record_carries_protocol_outside_keys_and_the_group_token_ignores_it():
    """Outside `keys` because equality there means SAME and a matching ruler certifies nothing;
    out of the persisted partition token because a grouping may never be stricter than the
    evidence (`group_token`'s own contract)."""
    task = {"kind": "repo", "eval": {"command": ["python", "score.py"], "metric": _M}}
    smoke = comparability_record(task=task, protocol=protocol_record(
        eval_protocol={"overrides": ["steps=1"]}))
    full = comparability_record(task=task, protocol=protocol_record(
        eval_protocol={"overrides": ["steps=100"]}))
    assert "protocol" in smoke and "protocol" not in smoke["keys"]
    assert group_token(smoke) == group_token(full) != ""
    assert comparability_record(task=task, protocol={"profile": "", "junk": "x"}) \
        == comparability_record(task=task)
    for junk in (None, "x", 3, [], {"overrides": "steps=1"}):
        assert protocol_record(eval_protocol=junk, host_scorer=junk) is None
    assert protocol_record(fingerprint={"not": "a digest"}) is None, "only a digest string rides"


# -------------------------------------------------------- 4. DRIVEN: the dispatcher and the fold
def _task(*, profiles=None, command=None) -> RepoTask:
    return RepoTask(id="protocol", goal="maximize metric", direction="max",
                    editable_path=str(FIXTURE), edit_surface=["*.json"],
                    protect=["ttrain_cli.py"],
                    eval=EvalSpec(command=command or [sys.executable, "ttrain_cli.py"],
                                  metric=_M, timeout=60,
                                  profiles=profiles if profiles is not None
                                  else {"smoke": _SMOKE, "full": _FULL}))


def test_the_dispatcher_records_the_profile_it_ran_and_the_fingerprint_printed(tmp_path):
    """A REAL eval, twice: the record on each result is the profile THIS dispatch resolved, and a
    fingerprint the scorer printed rides beside it."""
    engine = make_engine(tmp_path / "run", task=_task(), researcher=RepoParamResearcher({}),
                         developer=NoOpRepoDeveloper(), n_seeds=1, max_nodes=1)
    wd = tmp_path / "wd"
    wd.mkdir()
    (wd / "ttrain_cli.py").write_text(
        "import json, sys\n"
        "steps = [a for a in sys.argv[1:] if a.startswith('steps=')]\n"
        "print(json.dumps({'metric': 0.5, 'eval_fingerprint': {'argv': steps}}))\n",
        encoding="utf-8")
    node = Node(id=0, operator="draft", idea=Idea(operator="draft", params={}, rationale="r"),
                code="")
    smoke = engine._run_eval(node, str(wd), None, None)
    full = engine._run_eval(node, str(wd), None, "full")
    assert smoke.metric == full.metric == 0.5, "one number, two rulers — the record must say so"
    assert smoke.eval_protocol == {"profile": "smoke", "overrides": ["steps=1"]}
    assert full.eval_protocol == {"profile": "full", "overrides": ["steps=100"]}
    assert smoke.eval_fingerprint == _fp({"argv": ["steps=1"]})
    # The task side as the terminal reads it off the snapshot (the run has not written one yet).
    task = {"kind": "repo", "eval": {"command": [sys.executable, "ttrain_cli.py"], "metric": _M}}
    records = [comparability_record(task=task,
                                    protocol=protocol_record(eval_protocol=r.eval_protocol,
                                                             fingerprint=r.eval_fingerprint))
               for r in (smoke, full)]
    assert records[0]["keys"] == records[1]["keys"]
    assert comparability_status(*records) == DIFFERENT


def test_an_operator_declared_pipeline_records_no_overrides_it_never_ran(tmp_path):
    """THE CRITIC'S FALSE DIFFERENT, driven: an OPERATOR-declared `eval.stages` list runs verbatim and
    never executes the profile's command, so smoke and full dispatch byte-identical chains — and
    must record the same protocol, or two nodes measured identically read as two rulers."""
    stages = [{"name": "score", "command": [sys.executable, "ttrain_cli.py"]}]
    task = RepoTask(id="staged", goal="g", direction="max", editable_path=str(FIXTURE),
                    edit_surface=["*.json"], protect=["ttrain_cli.py"],
                    eval=EvalSpec(command=[sys.executable, "ttrain_cli.py"], metric=_M,
                                  timeout=60, stages=stages,
                                  profiles={"smoke": _SMOKE, "full": _FULL}))
    engine = make_engine(tmp_path / "run", task=task, researcher=RepoParamResearcher({}),
                         developer=NoOpRepoDeveloper(), n_seeds=1, max_nodes=1)
    node = Node(id=0, operator="draft", idea=Idea(operator="draft", params={}, rationale="r"),
                code="")
    wd = tmp_path / "wd"
    wd.mkdir()
    _cmd_s, _t, chain_s, smoke = engine._eval_pipeline(node, str(wd), None)
    _cmd_f, _t, chain_f, full = engine._eval_pipeline(node, str(wd), "full")
    assert [s["command"] for s in chain_s] == [s["command"] for s in chain_f], "identical chains"
    assert smoke["overrides"] == full["overrides"] == []
    assert protocol_record(eval_protocol=smoke) == protocol_record(eval_protocol=full)


def test_the_settle_record_keeps_both_facets(tmp_path):
    """A node finalized from its settle record (`engine/settled_recovery.py`) must carry the facets a
    live terminal records — silence there would be a hole, not a lie, but it is the incident path."""
    from looplab.engine.settled_recovery import result_from_record, settled_result_record
    from looplab.runtime.sandbox import RunResult

    live = RunResult(exit_code=0, stdout="", stderr="", metric=0.5, timed_out=False,
                     eval_protocol={"profile": "full", "overrides": ["steps=100"]},
                     eval_fingerprint=_fp({"repetition_penalty": 1.0}))
    record = settled_result_record(live, stdout_tail="", stderr_tail="")
    back, reason = result_from_record(json.loads(json.dumps(record)))
    assert reason == "" and back is not None
    assert back.eval_protocol == live.eval_protocol
    assert back.eval_fingerprint == live.eval_fingerprint


class _AlternatingProfiles(RepoParamResearcher):
    """Seeds on `smoke`, children on `full` — the RuleStrategist's seed/endgame split in miniature."""

    def propose(self, state, parent):
        idea = super().propose(state, parent)
        return idea.model_copy(update={"eval_profile": "full" if parent is not None else "smoke"})


def _run(tmp_path, researcher):
    task = _task()
    engine = make_engine(tmp_path / "run", task=task, researcher=researcher,
                         developer=NoOpRepoDeveloper(), n_seeds=1, max_nodes=2)
    # What `looplab run` publishes before the engine starts (`cli/run_cmds.py::
    # _publish_run_snapshots`): the terminal reads the task side of the key off this file, and
    # without it no key family exists for the protocol to ride beside.
    (tmp_path / "run").mkdir(parents=True, exist_ok=True)
    (tmp_path / "run" / "task.snapshot.json").write_text(
        json.dumps(task.model_dump(mode="json")), encoding="utf-8")
    return anyio.run(engine.run)


def test_a_run_that_ranked_smoke_against_full_caveats_its_champion(tmp_path):
    """THE HOLE, DRIVEN through the real engine, the real terminal and the real fold: two nodes,
    one per profile, each with an otherwise identical key — the field is mixed, so the champion
    carries `mixed_comparability`. Values are untouched: this qualifies a number, never moves one."""
    state = _run(tmp_path, _AlternatingProfiles({"x": (2.9, 3.1)}, seed=0))
    scored = [n for n in state.nodes.values() if n.metric is not None]
    assert len(scored) == 2, [(n.id, n.status, n.error) for n in state.nodes.values()]
    records = [record_of(n) for n in scored]
    assert all(r and r.get("protocol", {}).get("profile") for r in records), records
    assert records[0]["keys"] == records[1]["keys"], "the key alone could not refuse this pair"
    assert records[0]["protocol"]["profile"] != records[1]["protocol"]["profile"]
    assert CHAMPION_CAVEAT_MIXED_COMPARABILITY in champion_metric_caveats(state)


def test_one_profile_across_the_run_raises_nothing(tmp_path):
    """The negative control: the same run with every node on `smoke` is one ruler, and the caveat
    that fires above must stay silent here or it would mean nothing."""
    state = _run(tmp_path, RepoParamResearcher({"x": (2.9, 3.1)}, seed=0))
    scored = [n for n in state.nodes.values() if n.metric is not None]
    assert len(scored) == 2
    assert len({record_of(n)["protocol"]["profile"] for n in scored}) == 1
    assert CHAMPION_CAVEAT_MIXED_COMPARABILITY not in champion_metric_caveats(state)
