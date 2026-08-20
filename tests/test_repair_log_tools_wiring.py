"""The CRASH/TIMEOUT TRIAGE judge may LOOK — and what that wiring must not change.

THE MEASUREMENT THIS FILE IS BUILT FROM (`runs/rubertlite-dr-unified-v8` node 3, 2026-08-15)
--------------------------------------------------------------------------------------------
The `train` stage declared a 22,000 s ceiling and was SIGKILLed at 22,003 s. Its `train.log` holds
TWO progress bars with different totals:

* the training bar reached `10590/10590 [5:29:35<00:00,  1.87s/it]` and the stage then printed
  `{'train_runtime': 19775.2984, …, 'epoch': 14.98}` — all 15 epochs, finished; and
* a RETRIEVAL bar then ran to `223/361 [31:29<19:50,  8.63s/it]` — about twenty minutes from
  finishing — and that is where the kill landed.

What the repair path is handed is `evaluate._eval_failure_text`'s `res.stderr[-500:]`. The durable
`node_repaired.error_in` for that attempt is 522 characters and contains the last two renders of the
SECOND bar and nothing else. The triage verdict read that bar's elapsed field as training progress —
*"node 3 is still in epoch 1 at 31:20"*, where `31:20` is verbatim the `222/361` render — and
prescribed halving the batch AND cutting `n_epochs` 15 -> 8. The evidence that refutes it sat 83,697
characters earlier in the same file, on an ordinary non-progress-bar line. (The epochs cut never
landed — `repair_verify` stamped `unmet` on that row — so the cost is not an altered experiment but
an inert fix: attempt 6 re-ran the same 10,590 steps into the same 22,000 s ceiling.)

So the property under test is not "the model should have been more careful". It is that **the tail is
only informative when the last 500 characters happen to belong to the phase being diagnosed, and
nothing in the tail says whether they do**. The fixtures below reproduce both cases with the real
record shapes, and the tests drive the tools over them.

`tests/test_log_tools.py` drives the tools themselves and
`tests/test_monitor_log_tools_wiring.py` the watchdog half of the same join; what is driven HERE is
the repair half — that the judge can tell the two logs apart, that the wiring reaches it, that the
attempt floor comes with it, and that turning it off reproduces the historical ask byte for byte.
"""
from __future__ import annotations

import inspect
import json

from looplab.agents.unified_agent import UnifiedAgent
from looplab.engine import crash_repair as cr
from looplab.core.models import RunState
from looplab.engine import train_monitor as tm
from looplab.engine.train_monitor import needs_log_snapshot
from looplab.events.types import LOG_ROLE_TRAINING, LOG_ROLE_WORK
from looplab.tools.log_tools import LogQueryTools


# ------------------------------------------------------------------ the two real log shapes
# Record shapes copied from the live logs, so the record splitter, the bar regex and the per-bar-total
# clock are exercised on what they actually meet. A tqdm bar overwrites ONE line with `\r` and emits
# no `\n` until it finishes, which is why each bar below is a single `\r`-joined record.

def _finished_then_cut_short() -> str:
    """`rubertlite-dr-unified-v8` node 3: 10,590 training steps COMPLETED, then a second bar on a
    different total killed at 223/361. Condensed in the number of renders, identical in shape."""
    train = "\r".join(
        [f"{100 * n // 10590:3d}%|{'█' * 20}| {n}/10590 [{n * 1867 // 1000 // 3600}:"
         f"{n * 1867 // 1000 % 3600 // 60:02d}:{n * 1867 // 1000 % 60:02d}<0:00:00,  1.87s/it]"
         for n in list(range(1, 10590, 353)) + [10590]])
    retrieve = "\r".join(
        [f"Retrieving:  {100 * n // 361:2d}%|{'█' * 12}     | {n}/361 "
         f"[{n * 863 // 1000 // 60:02d}:{n * 863 // 1000 % 60:02d}<19:50,  8.63s/it]"
         for n in list(range(1, 223, 7)) + [222, 223]])
    return (
        "2026-08-15 01:49:43.518 | INFO     | __main__:main:36 - starting run rdrop-dcl\n"
        + train + "\n"
        "{'train_runtime': 19775.2984, 'train_samples_per_second': 8780.733, "
        "'train_steps_per_second': 0.536, 'train_loss': 22.405478578158657, 'epoch': 14.98}\n"
        "2026-08-15 07:33:38.361 | INFO     | __main__:main:104 - Started testing...\n"
        + retrieve)


def _still_training() -> str:
    """THE NEGATIVE CONTROL — `rubertlite-dr-unified-v6` node 5 attempt 4, a timeout that really did
    land inside training: one bar, killed at 4614/7060. A repairer must reach the OPPOSITE conclusion
    on this log, or the tools have merely swapped one confident wrong answer for another."""
    bar = "\r".join(
        [f"{100 * n // 7060:3d}%|{'█' * 13}     | {n}/7060 [{n * 2920 // 1000 // 3600}:"
         f"{n * 2920 // 1000 % 3600 // 60:02d}:{n * 2920 // 1000 % 60:02d}<1:59:12,  2.92s/it]"
         for n in list(range(1, 4614, 151)) + [4613, 4614]])
    return ("2026-08-14 20:14:02.001 | INFO     | __main__:main:36 - starting run rdrop\n" + bar)


def _stderr_tail(text: str) -> str:
    """What the repair path ACTUALLY receives today: `_eval_failure_text`'s 500-char stderr tail with
    the failing stage prepended. Reproduced here rather than asserted about, because every test below
    is a comparison against it."""
    return f"[failed stage: train]\n{text[-500:]}"


def _tools_over(tmp_path, text: str, *, floor: int = 0) -> LogQueryTools:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "train.log").write_text(text, encoding="utf-8")
    plan = tm.EvalLogPlan(roles={tm._log_name_key("train.log"): ("train", LOG_ROLE_TRAINING)})
    sources = tm.monitor_log_sources(tmp_path, plan)
    assert [s.name for s in sources] == ["train.log"] and sources[0].floor == floor
    return LogQueryTools(lambda: tm.monitor_log_sources(tmp_path, plan))


# ---------------------------------------------------------------- the tail cannot answer the question
def test_the_tail_the_repairer_gets_contains_neither_answer():
    """The premise. If the 500-char tail already carried the training bar this whole change would be
    the wrong fix — so this is the check that would refute it, run on both fixtures."""
    cut_short = _stderr_tail(_finished_then_cut_short())
    assert "10590" not in cut_short and "epoch" not in cut_short
    assert "/361 [" in cut_short                        # only the SECOND bar survives the slice
    # ...and the tail of a run that really was still training carries the training bar, which is why
    # the tail was ever good enough: it is right exactly when the kill lands in the phase being asked
    # about, and it says nothing about whether it did.
    assert "/7060 [" in _stderr_tail(_still_training())


# ---------------------------------------------------- the property: the two logs are distinguishable
def test_the_tools_distinguish_a_finished_run_from_one_still_in_its_first_epoch(tmp_path):
    """THE PROPERTY, driven. Two logs whose 500-char tails are equally uninformative about training
    progress; one whole-run `metric_series` call separates them."""
    finished = _tools_over(tmp_path / "a", _finished_then_cut_short())
    (tmp_path / "b").mkdir(parents=True, exist_ok=True)
    running = _tools_over(tmp_path / "b", _still_training())

    done = finished.execute("metric_series", {"metric": "step", "whole_run": True,
                                              "bucket_s": 3600})
    mid = running.execute("metric_series", {"metric": "step", "whole_run": True, "bucket_s": 3600})

    # THE DISTINGUISHING FACT, stated as the comparison a repairer actually makes: did the step
    # counter reach a lane's OWN declared total? On node 3's log it did — 10,590 of 10,590 — and a
    # SECOND lane on a different total then started; on the control it stopped at 4,614 of 7,060.
    assert _lane_totals(done) == {10590, 361} and _observed_max(done) == 10590
    assert _lane_totals(mid) == {7060} and _observed_max(mid) == 4614
    assert _observed_max(done) in _lane_totals(done)          # a lane finished
    assert _observed_max(mid) not in _lane_totals(mid)        # none did


def _lanes(series: str):
    """The `step=<done>/<total>` receipts a `metric_series` answer carries per bucket. Parsed rather
    than substring-matched, so a formatting change fails here loudly instead of leaving a vacuous
    assertion behind."""
    import re
    pairs = [(int(a), int(b)) for a, b in re.findall(r"step=(\d+)/(\d+)", series)]
    assert pairs, f"no step receipts in:\n{series}"
    return pairs


def _lane_totals(series: str) -> set:
    return {total for _, total in _lanes(series)}


def _observed_max(series: str) -> int:
    """The largest step the answer reports anywhere in its window — the number that says how far the
    counter actually got, as opposed to where it happened to be when the process died."""
    import re
    values = [int(v) for v in re.findall(r"\bmax=(\d+)\b", series)]
    assert values, f"no max in:\n{series}"
    return max(values)


def test_a_search_finds_the_summary_line_the_tail_cut_off(tmp_path):
    """The second half of the answer, and the cheaper one: the run PRINTED that it finished, in plain
    text, 83,697 characters before the end on the real log. `read_log` in search mode finds it."""
    tools = _tools_over(tmp_path, _finished_then_cut_short())
    hit = tools.execute("read_log", {"mode": "search", "pattern": "train_runtime"})
    assert "'epoch': 14.98" in hit and "19775" in hit
    # And the phase boundary itself is nameable, which is the fact the misdiagnosis inverted.
    assert "Started testing" in tools.execute("read_log", {"mode": "search",
                                                           "pattern": "Started testing"})


def test_the_second_bar_is_reported_as_its_own_lane_not_as_training_progress(tmp_path):
    """Why the tail misleads even when it is read carefully: `31:29` is the RETRIEVAL bar's elapsed
    field. The tools keep the two lanes apart — a `223/361` bar and a `10590/10590` bar are different
    bars — which is the same per-bar-total clock rule `tools/log_tools.py::_Clock` documents."""
    tools = _tools_over(tmp_path, _finished_then_cut_short())
    tail = tools.execute("read_log", {"mode": "tail", "lines": 3})
    assert "/361 [" in tail
    whole = tools.execute("metric_series", {"metric": "step", "whole_run": True, "bucket_s": 3600})
    # The whole-run answer names BOTH lanes and the `223` never appears as progress toward 10,590 —
    # which is exactly the conflation the tail invites and the elapsed field `31:20` came from.
    assert (223, 361) in _lanes(whole) and (223, 10590) not in _lanes(whole)


# ------------------------------------------------------------------------------ the engine-side join
def test_the_repair_provider_is_the_watchdogs_derivation_and_not_a_second_one():
    """ONE derivation of the source map. Both gates delegate to `_log_query_tools`, so a hardening of
    what is lookable cannot land on one role and miss the other."""
    shared = inspect.getsource(tm._log_query_tools)
    assert "monitor_log_sources" in shared
    for gate in (tm.monitor_log_tools, tm.repair_log_tools):
        body = inspect.getsource(gate)
        assert "_log_query_tools" in body
        # ...and neither builds a provider of its own.
        assert "LogQueryTools(" not in body


def test_the_repair_gate_is_its_own_switch(tmp_path):
    """An operator who turned the TIMER-fired watchdog's tools off (up to ~200 agentic ticks per node)
    has said nothing about the one look that happens after a multi-hour node has already died."""
    (tmp_path / "train.log").write_text("x\n", encoding="utf-8")
    plan = tm.eval_log_plan([{"name": "train"}])

    class _Eng:
        _train_monitor_tools = False
        _repair_log_tools = True

    assert tm.monitor_log_tools(_Eng(), tmp_path, plan) is None
    assert tm.repair_log_tools(_Eng(), tmp_path, plan) is not None

    class _Off(_Eng):
        _repair_log_tools = False

    assert tm.repair_log_tools(_Off(), tmp_path, plan) is None


def test_the_repair_gate_is_total_over_an_engine_that_never_heard_of_it(tmp_path):
    """A free function taking the engine, `getattr`-total — the same reason `monitor_log_tools` is one
    (`tests/test_asha_monitor.py::_AshaStub` is the object that caught the mixin version)."""
    (tmp_path / "train.log").write_text("x\n", encoding="utf-8")
    assert tm.repair_log_tools(object(), tmp_path, tm.eval_log_plan([{"name": "train"}])) is None


def test_no_log_yet_means_no_tools_rather_than_an_empty_one(tmp_path):
    """A failure before the first stage opened its log (and every `solution.py` eval) gets `None`,
    which is what `_ask_triage` reads as "do not pass the keyword at all"."""
    class _Eng:
        _repair_log_tools = True

    assert tm.repair_log_tools(_Eng(), tmp_path, tm.eval_log_plan([{"name": "train"}])) is None


def test_the_attempt_owes_a_snapshot_to_a_reader_that_has_not_asked_yet():
    """`needs_log_snapshot`'s truth table. The repair path reads only AFTER the attempt has died, so
    the "before" has to be taken on its behalf at the start — and this is the rule that says so. It
    used to be an inline `or` inside `_evaluate`, reachable only by driving a whole sandboxed eval,
    which is exactly how the repair clause could be deleted with every guard still green."""
    def _eng(**flags):
        return type("E", (), {"_train_monitor": False, "_asha_live": False,
                              "_repair_log_tools": False, **flags})()

    spec = {"metric": {"key": "m"}}
    assert needs_log_snapshot(_eng(), spec) is False
    assert needs_log_snapshot(_eng(_train_monitor=True), spec) is True
    assert needs_log_snapshot(_eng(_asha_live=True), spec) is True
    # The clause this change adds, and the one whose absence nothing else would notice.
    assert needs_log_snapshot(_eng(_repair_log_tools=True), spec) is True
    # ...and no eval spec means no per-stage log at all (the `solution.py` toy/dataset paths), so
    # every reader pays nothing — `off == today` for the whole non-command surface.
    for flags in ({}, {"_train_monitor": True}, {"_repair_log_tools": True}):
        assert needs_log_snapshot(_eng(**flags), None) is False


def test_the_repairer_cannot_read_the_previous_attempts_curve_as_this_ones(tmp_path):
    """`LogSource.floor` carries the attempt boundary, and the repair path is the reader that needs it
    most: it runs when an attempt has just died, on a log every earlier attempt also appended to.
    Attempt N-1 here is a run that COMPLETED its bar; attempt N crashed in its first steps, and the
    repairer must not read the predecessor's `10590/10590` as its own."""
    log = tmp_path / "train.log"
    log.write_text(_finished_then_cut_short(), encoding="utf-8")
    snapshot = tm.snapshot_training_logs(tmp_path)
    with log.open("a", encoding="utf-8") as fh:
        fh.write("\n2026-08-15 08:13:35.101 | INFO | __main__:main:36 - starting run rdrop-dcl\n"
                 "  0%|  | 3/10590 [00:24<24:05:33,  8.19s/it]\n")
    plan = tm.EvalLogPlan(roles={tm._log_name_key("train.log"): ("train", LOG_ROLE_TRAINING)})
    source = tm.monitor_log_sources(tmp_path, plan, snapshot)[0]
    assert source.floor > 0
    tools = LogQueryTools(lambda: tm.monitor_log_sources(tmp_path, plan, snapshot))
    series = tools.execute("metric_series", {"metric": "step", "whole_run": True, "bucket_s": 3600})
    # The predecessor COMPLETED its bar and ran a second lane; this attempt is three steps in. A
    # repairer that could see past the floor would read `10590/10590` and `223/361` as its own.
    assert _lanes(series) == [(3, 10590)] and _observed_max(series) == 3
    # The floor is the SAME rule the digest reader lands on — one boundary, three readers.
    assert tm.read_training_tail_raw(tmp_path, snapshot=snapshot, plan=plan).lstrip().startswith(
        "2026-08-15 08:13:35.101")


# ------------------------------------------------------------------- the prompt contract, both ways
class _Recorder:
    """A pilot client that records what it was asked and then emits a verdict without looking."""

    def __init__(self):
        self.messages = []
        self.tool_specs = []

    def chat(self, messages, tool_specs, tool_choice="auto"):
        self.messages.append(list(messages))
        self.tool_specs.append(list(tool_specs or []))
        return {"content": "", "tool_calls": [
            {"id": "1", "function": {"name": "triage_crash",
                                     "arguments": json.dumps({"action": "repair",
                                                              "rationale": "r"})}}]}


def _agent(client, tools=None):
    return UnifiedAgent(researcher=object(), developer=object(), pilot_client=client,
                        pilot_tools=tools)


def _ask(client, tools):
    node = type("N", (), {"id": 3, "code": "print(1)"})()
    return _agent(client).triage_crash(node, "[failure kind: timeout]\nboom", 5, tools=tools)


def test_tools_off_reproduces_the_historical_ask_byte_for_byte(tmp_path):
    """The shippability property. `repair_log_tools=false` must leave the paid call it has always
    made exactly as it was — prompt text is a contract."""
    without = _Recorder()
    _ask(without, None)
    with_tools = _Recorder()
    _ask(with_tools, _tools_over(tmp_path, _finished_then_cut_short()))

    a = without.messages[0][1]["content"]
    b = with_tools.messages[0][1]["content"]
    assert a != b
    assert UnifiedAgent._REPAIR_LOOK_INVITATION + "\n" not in a
    # ...and the ONLY difference is the invitation, spliced at the position pattern `budget`/`depth`
    # already use: additive, immediately above the evidence block, nothing else reworded or moved.
    assert b.replace(UnifiedAgent._REPAIR_LOOK_INVITATION + "\n", "", 1) == a
    # The system prompt is untouched in both directions.
    assert without.messages[0][0] == with_tools.messages[0][0]


def test_the_tools_actually_reach_the_judges_toolset(tmp_path):
    """The wiring, end to end through the real `drive_tool_loop`: the two log tools are in the specs
    the endpoint is offered, beside whatever standing pilot tools there were."""
    client = _Recorder()
    _ask(client, _tools_over(tmp_path, _finished_then_cut_short()))
    offered = {(s.get("function") or {}).get("name") for s in client.tool_specs[0]}
    assert {"read_log", "metric_series"} <= offered
    # ...and with no tools the toolset is exactly what it always was.
    bare = _Recorder()
    _ask(bare, None)
    assert not ({"read_log", "metric_series"} & {(s.get("function") or {}).get("name")
                                                 for s in bare.tool_specs[0]})


def test_a_standing_pilot_tool_cannot_be_shadowed_by_the_per_call_provider(tmp_path):
    """`CompositeTools` dedups FIRST-provider-wins, so the pilot toolset goes FIRST: a per-call
    provider must never be able to take over a standing tool by reusing its name. Driven through the
    real loop — the model calls `read_log`, and what answers must be the standing provider."""
    class _Standing:
        def specs(self):
            return [{"type": "function", "function": {"name": "read_log", "description": "d",
                                                      "parameters": {"type": "object",
                                                                     "properties": {}}}}]

        def execute(self, name, args):
            return "STANDING-PROVIDER-ANSWERED"

        def bind_state(self, state, parent=None):
            return None

    client = _Caller("read_log", {"mode": "tail"})
    UnifiedAgent(researcher=object(), developer=object(), pilot_client=client,
                 pilot_tools=_Standing()).triage_crash(
        type("N", (), {"id": 3, "code": ""})(), "boom", 1,
        tools=_tools_over(tmp_path, _still_training()))
    assert client.results == ["STANDING-PROVIDER-ANSWERED"]


class _Caller(_Recorder):
    """A pilot client that calls ONE named tool on its first turn, records the result the loop fed
    back, and emits on the second — so a test can observe what a tool call actually returned."""

    def __init__(self, tool: str, args: dict):
        super().__init__()
        self._tool, self._args, self.results, self._turn = tool, args, [], 0

    def chat(self, messages, tool_specs, tool_choice="auto"):
        self.messages.append(list(messages))
        self.tool_specs.append(list(tool_specs or []))
        self._turn += 1
        if self._turn > 1:
            self.results.extend(m["content"] for m in messages if m.get("role") == "tool")
            return super().chat(messages, tool_specs, tool_choice)
        return {"content": "", "tool_calls": [
            {"id": "t1", "function": {"name": self._tool, "arguments": json.dumps(self._args)}}]}


def test_the_judge_can_actually_run_the_tools_it_is_offered(tmp_path):
    """End to end through the real `drive_tool_loop`: a triage judge that calls `metric_series` on
    node 3's log shape gets back the answer the tail could not give it, inside its own turn."""
    client = _Caller("metric_series", {"metric": "step", "whole_run": True, "bucket_s": 3600})
    verdict = UnifiedAgent(researcher=object(), developer=object(),
                           pilot_client=client).triage_crash(
        type("N", (), {"id": 3, "code": ""})(),
        _stderr_tail(_finished_then_cut_short()), 5,
        tools=_tools_over(tmp_path, _finished_then_cut_short()))
    assert verdict["action"] == "repair"
    assert len(client.results) == 1
    assert _lane_totals(client.results[0]) == {10590, 361}
    assert _observed_max(client.results[0]) == 10590


def test_an_older_triage_seam_never_sees_the_new_keyword():
    """`triage_crash` is a duck-typed seam. Passing `tools` unconditionally would make an
    implementation written against the old signature raise TypeError, which `_ask_triage`'s
    fail-closed handler reads as a DEAD PROVIDER — stopping the node and pausing the run."""
    def old(node, error, attempt, *, state=None, brief=""):
        return {"action": "repair", "rationale": "old-style"}

    assert "tools" not in cr._accepted_kwargs(old, {"history": "", "tools": object()})

    def new(node, error, attempt, *, state=None, brief="", tools=None):
        return {"action": "repair", "rationale": "new"}

    assert "tools" in cr._accepted_kwargs(new, {"tools": object()})

    # Driven, because the cost of getting this wrong is not a TypeError anyone sees: it is a node
    # stopped and a RUN paused with a "check your credits" banner. An old seam asked WITH log tools
    # available must still answer its own verdict.
    class _Old:
        def triage_crash(self, node, error, attempt, *, state=None, brief=""):
            return {"action": "abandon", "rationale": "old-style"}

    out = _EngineStub(_Old())._triage_crash(RunState(), object(), "boom", 1, log_tools=object())
    assert out["action"] == "abandon"


def test_the_turn_grant_is_additive_and_never_caps_an_unlimited_budget():
    """The cost bound. Four extra turns over a FINITE operator budget; an operator who configured no
    turn cap keeps none — the loop is bounded there by `agent_emit_after`/`agent_emit_force`."""
    assert UnifiedAgent._REPAIR_LOOK_TURNS == 4
    assert UnifiedAgent._REPAIR_LOOK_TURNS < tm._MONITOR_LOOK_TURNS

    seen = {}

    class _Client(_Recorder):
        pass

    def _drive(client, tools, messages, emit_spec, *, finalize, fallback, **opts):
        seen[len(seen)] = opts.get("max_turns")
        return finalize({"action": "repair", "rationale": "r"})

    import looplab.agents.agent as agent_mod
    original = agent_mod.drive_tool_loop
    agent_mod.drive_tool_loop = _drive
    try:
        node = type("N", (), {"id": 1, "code": ""})()
        UnifiedAgent(researcher=object(), developer=object(), pilot_client=_Client(),
                     agent_max_turns=10).triage_crash(node, "boom", 1, tools=object())
        UnifiedAgent(researcher=object(), developer=object(), pilot_client=_Client(),
                     agent_max_turns=0).triage_crash(node, "boom", 1, tools=object())
        UnifiedAgent(researcher=object(), developer=object(), pilot_client=_Client(),
                     agent_max_turns=10).triage_crash(node, "boom", 1)
    finally:
        agent_mod.drive_tool_loop = original
    assert seen == {0: 14, 1: 0, 2: 10}


# ------------------------------------------------------------------------------ the trust line
def test_looking_cannot_widen_what_the_verdict_may_SAY():
    """docs/36. The tools widen this judge's CONTEXT; what it may ANSWER stays a closed vocabulary,
    so no metric, champion, selectability or violation can move because a model read a log. Driven,
    not pinned: a seam that has just read the log and tries to say something outside the vocabulary
    — a metric, a selectability claim, a bare `reason` key — is normalized away.

    THE VERDICT GAINED A FOURTH KEY ON 2026-08-20 and this test is where that has to be argued,
    because until then the property was spelled "it may not name a failure `reason` at all".
    `failure_kind` is a reason, deliberately — see `tests/test_triage_llm_failure_classifier.py` for
    the incident — and it is admissible for reasons the raw `reason` key below is not:

      * it is a CLOSED vocabulary read off `triage.py::JUDGED_FAILURE_REASONS`, and only the three
        kinds the engine itself inferred from the dead process's TEXT. Every AUTHENTICATED
        classification — both watchdog verdicts, the deadline, the drift refusal, the stage-contract
        statuses — is outside it and unreachable from the wire;
      * those three are disjoint from `metric_salvage.NEVER_SALVAGED_REASONS`, so the answer cannot
        move a number into or out of the record, which is what the paragraph above is really about.

    The junk keys are unchanged and still dropped, and `reason` — the ENGINE's own column — is still
    among them: naming the field the engine writes is not the same permission as answering the
    question the engine asked."""
    from looplab.engine.triage import (AGENT_TRIAGE_ACTIONS, DEFAULT_TRIAGE_ACTION,
                                       JUDGED_FAILURE_REASONS, judged_failure_reason)
    from looplab.engine.metric_salvage import NEVER_SALVAGED_REASONS
    assert set(AGENT_TRIAGE_ACTIONS) == {"repair", "abandon", "reject_idea"}
    assert set(JUDGED_FAILURE_REASONS) & set(NEVER_SALVAGED_REASONS) == set()

    class _Overreaching:
        def triage_crash(self, node, error, attempt, *, state=None, brief="", tools=None, **kw):
            assert tools is not None                      # it really was handed the log tools
            return {"action": "repair", "rationale": "the log says it finished all 15 epochs",
                    "reason": "ok", "metric": 0.99, "selectable": True,
                    "missing_dependency": "torch"}

    state = RunState()
    eng = _EngineStub(_Overreaching())
    out = eng._triage_crash(state, object(), "boom", 1, reason="timeout", log_tools=object())
    assert set(out) == {"action", "failure_kind", "rationale", "missing_dependency"}
    assert out["action"] in AGENT_TRIAGE_ACTIONS and "finished all 15 epochs" in out["rationale"]
    # It said `reason: ok`, and that is not the field it was asked about: the kind is empty, so the
    # engine keeps its own — and `timeout` is AUTHENTICATED, so nothing here could have moved it
    # even if the seam had filled the field in.
    assert out["failure_kind"] == ""
    assert judged_failure_reason("timeout", out)[0] == "timeout"

    class _Relabelling(_Overreaching):
        def triage_crash(self, node, error, attempt, *, state=None, brief="", tools=None, **kw):
            return {"action": "repair", "rationale": "r", "failure_kind": "diverged"}

    relabel = _EngineStub(_Relabelling())._triage_crash(state, object(), "boom", 1, reason="crash",
                                                       log_tools=object())
    assert judged_failure_reason("crash", relabel)[0] == "crash", (
        "a watchdog verdict is not in the judge's vocabulary and may not be reached by writing it")

    class _Inventing(_Overreaching):
        def triage_crash(self, node, error, attempt, *, state=None, brief="", tools=None, **kw):
            return {"action": "salvage_cause_fix", "rationale": "x"}

    stopped = _EngineStub(_Inventing())._triage_crash(state, object(), "boom", 1,
                                                      log_tools=object())
    assert stopped["action"] == DEFAULT_TRIAGE_ACTION       # coerced, never invented


class _EngineStub(cr.CrashRepairMixin):
    """The narrowest engine `_triage_crash` needs: a researcher seam and a tracer."""

    _inline_repair_attempts = 5

    def __init__(self, researcher):
        self.researcher = researcher

        class _Span:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        self.tracer = type("T", (), {"span": lambda *a, **k: _Span()})()


def test_the_repair_path_reads_and_only_reads(tmp_path):
    """Rule 4. The provider handed to triage is the same read-only surface the watchdogs get — two
    tools, no write, no execution, nothing outside the named logs."""
    tools = _tools_over(tmp_path, _still_training())
    assert {(s.get("function") or {}).get("name") for s in tools.specs()} == {"read_log",
                                                                             "metric_series"}
    refusal = tools.execute("read_log", {"log": "../../etc/passwd", "mode": "tail"})
    assert "no log named" in refusal and "train.log" in refusal


def test_a_work_role_log_is_readable_and_still_carries_no_authority(tmp_path):
    """The repair judge may read a stage the plan cannot PROVE is training — "has the scorer even
    started" is exactly the question this exists to answer — and the ROLE rides on the source so the
    model is always told which phase it is reading."""
    for name in ("train.log", "score.log"):
        (tmp_path / name).write_text(_still_training(), encoding="utf-8")
    plan = tm.eval_log_plan([{"name": "train"}, {"name": "score"}])
    roles = {s.name: s.role for s in tm.monitor_log_sources(tmp_path, plan)}
    assert roles["train.log"] == LOG_ROLE_WORK
    tools = LogQueryTools(lambda: tm.monitor_log_sources(tmp_path, plan))
    assert LOG_ROLE_WORK in tools.execute("read_log", {"log": "train.log", "mode": "tail",
                                                       "lines": 1})

