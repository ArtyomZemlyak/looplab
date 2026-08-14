"""The two live-eval watchdog JUDGES may LOOK — and what that wiring must not change.

The tools themselves are driven in `tests/test_log_tools.py`. What is driven HERE is the join: which
logs the engine is willing to name, that the attempt boundary is ONE rule with two readers, and — the
property that makes this shippable — that `train_monitor_tools=false` reproduces the historical
single completion byte for byte, prompt included.
"""
from __future__ import annotations

import inspect

from looplab.engine import asha_monitor as am
from looplab.engine import train_monitor as tm
from looplab.events.types import (
    LOG_ROLE_AMBIGUOUS,
    LOG_ROLE_SCORE,
    LOG_ROLE_SETUP,
    LOG_ROLE_TRAINING,
    LOG_ROLE_WORK,
)


class _Recorder:
    """A client stand-in that records the messages it was asked to complete."""

    def __init__(self):
        self.calls = []

    def complete_tool(self, messages, schema):
        self.calls.append(list(messages))
        return {"status": "healthy", "reason": "steady", "confidence": 0.5}


class _Engine(tm.TrainingMonitorMixin, am.AshaMonitorMixin):
    def __init__(self, *, tools_on: bool):
        self._train_monitor_tools = tools_on
        self.client = _Recorder()
        self.developer = type("D", (), {"client": self.client})()


# --------------------------------------------------------------------------- what the engine names
def test_the_source_map_is_exactly_the_stage_logs_the_plan_can_name(tmp_path):
    """Rule 2 of `tools/log_tools.py`'s boundary. The reach is the eval's OWN declared output — the
    one region that provably holds nothing but what this node produced."""
    for name in ("train.log", "setup.log", "score.log", "stray.log"):
        (tmp_path / name).write_text("x\n")
    plan = tm.eval_log_plan([{"name": "train"}, {"name": "score"}])
    names = {s.name: s.role for s in tm.monitor_log_sources(tmp_path, plan)}
    # `train` is stage 1 of 2, which the plan cannot PROVE is the training loop, so it is
    # LOG_ROLE_WORK — still readable, still judged, just without kill authority. The role rides on
    # every source, so the model is always told which phase it is reading.
    assert names == {"train.log": LOG_ROLE_WORK, "setup.log": LOG_ROLE_SETUP,
                     "score.log": LOG_ROLE_SCORE}
    # `stray.log` is not in the plan, so it is not nameable at all — the same rule
    # `resolve_stage_log` applies to what may be JUDGED, applied to what may be READ.
    assert "stray.log" not in names


def test_a_log_the_plan_cannot_attribute_is_not_readable(tmp_path):
    """`LOG_ROLE_AMBIGUOUS` is 'two possible writers', and bytes nobody can attribute to a phase are
    not evidence — the same reason `resolve_stage_log` refuses to judge them."""
    (tmp_path / "setup.log").write_text("x\n")
    plan = tm.eval_log_plan([{"name": "setup"}, {"name": "train"}, {"name": "score"}])
    assert plan.roles[tm._log_name_key("setup.log")][1] == LOG_ROLE_AMBIGUOUS
    assert [s.name for s in tm.monitor_log_sources(tmp_path, plan)] == []


def test_the_source_map_carries_the_attempt_floor_the_digest_uses(tmp_path):
    """ONE boundary, two readers. A role that could seek past a floor the digest respects would read
    a dead attempt's curve as the live one's — which is the whole reason `attempt_byte_floor` was
    lifted out of `read_training_tail_raw` rather than copied."""
    log = tmp_path / "train.log"
    log.write_text("PREVIOUS ATTEMPT\n")
    snapshot = tm.snapshot_training_logs(tmp_path)
    with log.open("a") as fh:
        fh.write("this attempt loss: 1.0\n")
    plan = tm.EvalLogPlan(roles={tm._log_name_key("train.log"): (None, LOG_ROLE_TRAINING)})
    source = tm.monitor_log_sources(tmp_path, plan, snapshot)[0]
    assert source.floor == len("PREVIOUS ATTEMPT\n")
    # ...and it is the SAME floor the digest reader lands on.
    assert "PREVIOUS ATTEMPT" not in tm.read_training_tail_raw(tmp_path, snapshot=snapshot, plan=plan)


def test_a_truncate_and_regrow_fails_open_to_zero_for_both_readers(tmp_path):
    """The extraction must not have changed the boundary's own answers. A rotation/truncate starts
    at byte zero; an unreadable cursor fails CLOSED."""
    log = tmp_path / "train.log"
    log.write_text("first attempt, quite long\n")
    snapshot = tm.snapshot_training_logs(tmp_path)
    log.write_text("brand new attempt\n")           # truncate-and-regrow
    with log.open("rb") as fh:
        assert tm.attempt_byte_floor(fh, log, snapshot) == 0
    incomplete = tm.TrainingLogSnapshot({}, complete=False)
    with log.open("rb") as fh:
        assert tm.attempt_byte_floor(fh, log, incomplete) is None


def test_no_log_yet_means_no_tools_rather_than_an_empty_one(tmp_path):
    assert tm.monitor_log_sources(tmp_path, tm.eval_log_plan([])) == []
    assert tm.monitor_log_tools(_Engine(tools_on=True), tmp_path, tm.eval_log_plan([])) is None


# -------------------------------------------------------------------- the off path is byte-identical
def _prompt(engine, **kw) -> str:
    engine._training_verdict("DIGEST", "CONTEXT", "STAGE", "TRAJECTORY", kw.get("tools"))
    return engine.client.calls[-1][-1]["content"]


def test_tools_off_reproduces_the_historical_message_byte_for_byte(tmp_path):
    """Prompt strings are contracts. The invitation is SPLICED at the same position pattern as
    `stage_context` and `trajectory_text`, so the off path is not 'close to' the old message."""
    off = _prompt(_Engine(tools_on=False))
    assert off == ("CONTEXT\n\nSTAGE\n\nTRAJECTORY\n\n"
                   "LIVE TRAINING LOG (recent tail):\nDIGEST"
                   "\n\nClassify this run's health from the log evidence above.")
    assert tm._LOOK_INVITATION not in off


def test_the_invitation_appears_only_with_tools_and_only_at_its_own_position(tmp_path):
    (tmp_path / "train.log").write_text("\r1/2 [0:00:01<0:00:01] loss: 1.0\n")
    plan = tm.EvalLogPlan(roles={tm._log_name_key("train.log"): ("train", LOG_ROLE_TRAINING)})
    tools = tm.monitor_log_tools(_Engine(tools_on=True), tmp_path, plan)
    assert tools is not None
    on = _prompt(_Engine(tools_on=False), tools=tools)
    assert tm._LOOK_INVITATION in on
    # Everything the historical message said is still there, in order, unchanged.
    assert on.index("TRAJECTORY") < on.index(tm._LOOK_INVITATION) < on.index("LIVE TRAINING LOG")
    assert on.endswith("Classify this run's health from the log evidence above.")


def test_the_setting_off_yields_no_tools_at_all(tmp_path):
    (tmp_path / "train.log").write_text("\r1/2 [0:00:01<0:00:01] loss: 1.0\n")
    plan = tm.EvalLogPlan(roles={tm._log_name_key("train.log"): ("train", LOG_ROLE_TRAINING)})
    assert tm.monitor_log_tools(_Engine(tools_on=False), tmp_path, plan) is None
    assert tm.monitor_log_tools(_Engine(tools_on=True), tmp_path, plan) is not None


def test_both_watchdogs_build_their_provider_through_the_one_switch():
    """`monitor_log_tools` is the single place either watchdog gets a provider, so a run cannot end up
    with a looking training monitor and a blind ASHA judge (or the reverse)."""
    from tests._source_scan import called_names
    for fn in (tm.TrainingMonitorMixin._monitor_training, am.AshaMonitorMixin._monitor_asha):
        assert "monitor_log_tools" in called_names(fn)
    # The provider is built at the LOOP, not inside the verdict: the verdict method takes it as an
    # argument, which is what lets the off path (`tools=None`) be the historical call.
    assert "monitor_log_tools" not in called_names(am.AshaMonitorMixin._asha_verdict)
    assert "monitor_log_tools" not in called_names(tm.TrainingMonitorMixin._training_verdict)


def test_the_builder_is_a_free_function_and_not_a_mixin_method():
    """It takes the ENGINE rather than being a method, for `engine/speculation_gate.py`'s reason. As a
    method it has to live on a mixin one of the two watchdogs does not inherit, and an ASHA-only
    object then raises AttributeError inside `_monitor_asha`'s per-tick containment `except` — a
    watchdog that silently stops producing verdicts for the rest of a multi-hour eval.
    `tests/test_asha_monitor.py::_AshaStub` is exactly that object, and it is what caught this."""
    import looplab.engine.shared as sh
    assert callable(tm.monitor_log_tools)
    for cls in (tm.TrainingMonitorMixin, am.AshaMonitorMixin, sh.SharedEngineMixin):
        assert "_monitor_tools" not in vars(cls)
    # It is TOTAL over a partially-built engine: an object carrying none of the watchdog state at all
    # answers "no tools" instead of raising into a caller that would swallow it.
    assert tm.monitor_log_tools(object(), "/nonexistent") is None


def test_the_judge_turn_budget_is_below_the_one_shot_verifiers():
    """This judge fires on a TIMER up to `_MAX_MONITOR_LLM_CALLS` times per node, so a turn budget is
    multiplied by ~200 in a way the two one-shot trust verifiers' never is."""
    from looplab.trust.judge import JUDGE_MAX_TURNS
    assert 0 < tm._MONITOR_LOOK_TURNS < JUDGE_MAX_TURNS


def test_the_asha_judge_now_passes_the_tools_argument_it_always_accepted():
    source = inspect.getsource(am.AshaMonitorMixin._asha_verdict)
    assert "tools=tools" in source
    assert am._ASHA_LOOK_INVITATION not in inspect.getsource(am.AshaMonitorMixin._monitor_asha)


# ------------------------------------------------------------------- the trusted set does not widen
def test_looking_cannot_reach_the_kill_decision():
    """docs/36: a wider action space must not widen the trusted set. `should_monitor_kill`'s conjuncts
    are unchanged, and the deterministic trajectory veto is still the thing that stands between a
    `broken` verdict and a dead node."""
    from tests._source_scan import names_read
    read = names_read(tm.should_monitor_kill)
    assert "trajectory_vetoes_kill" in inspect.getsource(tm.should_monitor_kill)
    for forbidden in ("LogQueryTools", "read_log", "metric_series", "_monitor_tools"):
        assert forbidden not in inspect.getsource(tm.should_monitor_kill)
        assert forbidden not in read
