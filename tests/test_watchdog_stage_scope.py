"""Live-watchdog stage scope, kill attribution, teardown cost, and post-kill visibility.

Four defects, all reproduced here against the pre-fix behaviour and then pinned:

H-1 The training monitor read the workdir's freshest ``*.log`` and judged it as "the live training
    log". Both watchdog tasks start BEFORE ``_run_eval`` and live across setup -> every stage -> the
    ALWAYS-appended ``score`` stage (``engine/eval_stages.py`` appends it to every Developer manifest
    and the agent cannot remove it), so the freshest file is ``setup.log`` during a minutes-long pip
    install and ``score.log`` immediately after a training SUCCEEDED. A scorer tail — short, CPU-only,
    framework warnings, no loss trajectory — matches three separate clauses of ``TrainingVerdict``'s
    ``broken`` contract at once, the changed-digest gate guarantees a fresh LLM call on every file
    switch, and one confident tick was the entire kill gate. Result: a finished multi-hour training
    discarded with no repair, no retry and no refund of its ``max_nodes`` slot.
H-4 ``EV_ASHA_VERDICT.kill`` was written from the DECISION, under an await, before
    ``claim_watchdog_kill`` — whose return value was discarded. A watchdog that LOST the claim left a
    durable ``kill: true`` row beside a terminal naming the other watchdog.
H-3 Neither watchdog re-checked ``cancel`` before starting its deliberately un-abandonable provider
    call, so the loser of a race still had to finish one and node teardown waited for it — bounded by
    the endpoint timeout twice over, once per watchdog.
H-5 A watchdog-killed node's attention item was filtered to nodes still ``pending``. With the kills on
    by default the kill terminal lands seconds after the alert, so the item and its one-time desktop
    notification were gone before any realistic poll: a killed experiment and no signal.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import anyio
import pytest

from looplab.core.models import Event
from looplab.core.tracing import JsonlSpanExporter, Tracer
from looplab.engine.asha_monitor import AshaMonitorMixin
from looplab.engine.train_monitor import (
    LOG_ROLE_AMBIGUOUS,
    LOG_ROLE_SCORE,
    LOG_ROLE_SETUP,
    LOG_ROLE_TRAINING,
    LOG_ROLE_UNKNOWN,
    LOG_ROLE_WORK,
    ActiveStageLog,
    TrainingMonitorMixin,
    TrainingVerdict,
    _MONITOR_SYSTEM,
    active_training_log,
    claim_watchdog_kill,
    eval_log_plan,
    monitor_stage_context,
    read_training_tail,
    resolve_stage_log,
    should_monitor_kill,
)
from looplab.events.types import EV_ASHA_VERDICT, EV_TRAIN_MONITOR_ALERT

# Bounds only the FAILURE case: an `until` drive cancels the instant its predicate holds, so a passing
# test never waits this out (same convention as tests/test_train_monitor.py).
_SETTLE_TIMEOUT_S = 15.0

# The exact evidence the review measured on a real workdir: a legitimate score stage runs on CPU and
# prints framework warnings, and has no loss trajectory at all.
_SCORE_TAIL = ("Some weights of the model were not initialized from the model checkpoint\n"
               "WARNING: CUDA not available, using CPU\n"
               '{"recall": 0.842}\n')
_SETUP_TAIL = ("Collecting torch==2.3.0\n"
               "  Using cached torch-2.3.0-cp311-manylinux1_x86_64.whl (779.1 MB)\n"
               "INFO: pip is looking at multiple versions of torchvision to determine which\n")
_TRAIN_TAIL = ('{"recall": 0.31, "step": 1, "loss": 2.9}\n'
               '{"recall": 0.44, "step": 2, "loss": 2.1}\n')

# The pipeline the Developer-manifest shape produces: declared work stages, then the engine's
# protected `score` stage appended last (eval_stages.py:90-93). Every one of its work stages is
# `LOG_ROLE_WORK` — judged, never kill authority (H-1(d)).
_STAGES = [{"name": "data_prep", "command": ["python", "prep.py"]},
           {"name": "train", "command": ["python", "train.py"]},
           {"name": "score", "command": ["python", "score.py"]}]
# The kill-ELIGIBLE shape: a one-stage pipeline is the single-command eval wearing a stage name, so
# its log is provably the whole eval's own work. Used by every test about the confirmation gate,
# because after H-1(d) a multi-stage pipeline can no longer reach a kill at all.
_TRAINING_STAGES = [{"name": "train", "command": ["python", "train.py"]}]


def _mtimes(wd: Path, order: list[str]) -> None:
    """Stamp explicit, strictly increasing mtimes — the LAST name is the live stage's log. Explicit
    because two `os.utime(..., None)` calls can land on the same filesystem timestamp and make the
    freshest-file question a coin flip."""
    base = time.time() - 1000.0
    for offset, name in enumerate(order):
        os.utime(wd / name, (base + 10 * offset, base + 10 * offset))


def _workdir(tmp_path, *, freshest: str, name: str = "node_0") -> Path:
    """A node workdir carrying every log a staged eval writes, with `freshest` last-written."""
    wd = tmp_path / name
    wd.mkdir()
    (wd / "setup.log").write_text(_SETUP_TAIL, encoding="utf-8")
    (wd / "train.log").write_text(_TRAIN_TAIL, encoding="utf-8")
    (wd / "score.log").write_text(_SCORE_TAIL, encoding="utf-8")
    others = [n for n in ("setup.log", "train.log", "score.log") if n != freshest]
    _mtimes(wd, others + [freshest])
    return wd


# --------------------------------------------------------------------------------- test doubles
class _FakeStore:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def append(self, event_type, data):
        self.events.append((event_type, dict(data)))

    def read_all(self):
        return [Event(seq=index, ts=0.0, type=event_type, data=dict(data))
                for index, (event_type, data) in enumerate(self.events)]

    def rows(self, event_type):
        return [data for (etype, data) in self.events if etype == event_type]


class _ScriptedClient:
    """The Developer's client, answering through `complete_tool` — the SAME structured path production
    takes — and recording every message list it was shown."""

    def __init__(self, *verdicts):
        self._verdicts = list(verdicts)
        self.calls = 0
        self.messages: list[list[dict]] = []

    def complete_tool(self, messages, schema):
        self.calls += 1
        self.messages.append([dict(m) for m in messages])
        return dict(self._verdicts[min(self.calls, len(self._verdicts)) - 1])

    @property
    def digests(self) -> list[str]:
        return [m[-1]["content"] for m in self.messages]


class _Developer:
    def __init__(self, client):
        self.client = client


class _Host(TrainingMonitorMixin, AshaMonitorMixin):
    """Both watchdogs on ONE host, sharing `kill_signal`/`cancel` exactly as `_evaluate` wires them."""

    def __init__(self, tmp_path, *, client=None, cadence=0.02, kill=True, asha_kill=True):
        self.tracer = Tracer(JsonlSpanExporter(tmp_path / "spans.jsonl"))
        self.store = _FakeStore()
        self._write_lock = anyio.Lock()
        self.developer = _Developer(client) if client is not None else None
        self._train_monitor_interval_s = cadence
        self._train_monitor_kill = kill
        self._train_monitor_kill_confidence = 0.8
        self._asha_live_kill = asha_kill
        self._asha_live_quantile = 0.5
        self._asha_live_min_siblings = 3
        self._asha_live_kill_confidence = 0.8
        self._cadence = cadence
        self.kill_signal: dict = {}
        self.cancel = threading.Event()
        self._spans_path = tmp_path / "spans.jsonl"

    def _monitor_cadence(self):
        return self._cadence

    def _asha_cadence(self):
        return self._cadence

    def spans(self, name):
        if not self._spans_path.exists():
            return []
        return [json.loads(line) for line in self._spans_path.read_text().splitlines()
                if line.strip() and json.loads(line).get("name") == name]


def _drive_train(host, workdir, *, plan=None, context="", until=None, window=0.3):
    async def _run():
        async with anyio.create_task_group() as tg:
            tg.start_soon(host._monitor_training, 0, 0, str(workdir), host.cancel,
                          context, host.kill_signal, None, plan)
            if until is None:
                await anyio.sleep(window)
            else:
                deadline = time.monotonic() + _SETTLE_TIMEOUT_S
                while not until(host) and time.monotonic() < deadline:
                    await anyio.sleep(0.005)
            tg.cancel_scope.cancel()

    anyio.run(_run)


# =========================================================== H-1: which log the judge is shown
def test_h1_the_freshest_log_heuristic_hands_the_score_stage_to_the_training_judge(tmp_path):
    """REPRODUCTION. With no stage information the resolver answers `score.log`, and the training-health
    judge is handed a scorer's tail under the header "LIVE TRAINING LOG"."""
    wd = _workdir(tmp_path, freshest="score.log")

    # The pre-fix read, still the behaviour whenever no plan is available.
    assert active_training_log(wd).name == "score.log"
    assert "CUDA not available" in read_training_tail(wd)

    client = _ScriptedClient({"status": "broken", "reason": "silent CPU fallback, loss never moved",
                              "confidence": 0.95})
    host = _Host(tmp_path, client=client)
    # WAIT FOR THE THING THIS TEST ASSERTS, not for the judge CALL that precedes it. `_drive_train`
    # cancels the task group the instant `until` is true, and the alert is appended LATER in the same
    # tick — inside a `CancelScope` that is shielded only when a stop or repair was decided, which is
    # exactly what this case does NOT decide. So `client.calls >= 1` opened a window between "the
    # judge was called" and "the alert was recorded" for the cancel to land in, and on a slow
    # filesystem it lands there often: measured 3/12 on master and 6/12 on this branch with
    # IDENTICAL content on a network mount, against 0/12 for the same content on local disk. The
    # code was never the variable; the harness was racing itself, which is the one way a guard can
    # fail that teaches nobody anything.
    #
    # This is the idiom the rest of this file already uses (see the `EV_TRAIN_MONITOR_ALERT` waits
    # below) and it strictly implies the old predicate: no alert is written without a call. The
    # `client.digests[0]` assertions that follow are therefore unchanged in meaning.
    _drive_train(host, wd, until=lambda h: h.store.rows(EV_TRAIN_MONITOR_ALERT))

    # The judge really is shown the SCORER's output — this is the wrong-log defect, not a theory.
    assert client.calls >= 1
    assert "CUDA not available" in client.digests[0] and "step" not in client.digests[0]

    # Pre-fix, the entire kill gate was: enabled (now the default) + status broken + confidence >= 0.8.
    verdict = TrainingVerdict(status="broken", reason="silent CPU fallback", confidence=0.95)
    assert verdict.status == "broken" and verdict.confidence >= 0.8
    # It no longer is. An UNATTRIBUTED log cannot kill however often it repeats, an identified
    # non-training stage cannot kill, and one confident tick about the real training stage is not
    # enough either — only a CONFIRMED verdict about an identified training stage acts.
    for role in (LOG_ROLE_UNKNOWN, LOG_ROLE_SETUP, LOG_ROLE_SCORE):
        assert should_monitor_kill(verdict, enabled=True, threshold=0.8,
                                   log_role=role, broken_streak=9) is False
    assert should_monitor_kill(verdict, enabled=True, threshold=0.8,
                               log_role=LOG_ROLE_TRAINING, broken_streak=1) is False
    assert should_monitor_kill(verdict, enabled=True, threshold=0.8,
                               log_role=LOG_ROLE_TRAINING, broken_streak=2) is True

    # ... and end to end: the unattributed verdict stays ADVISORY. The alert is still recorded (it is
    # evidence), but nothing was killed.
    assert host.store.rows(EV_TRAIN_MONITOR_ALERT), "an unattributed verdict is still recorded"
    assert host.kill_signal == {} and not host.cancel.is_set()


@pytest.mark.parametrize("freshest,tail_marker", [("score.log", "CUDA not available"),
                                                  ("setup.log", "pip is looking at multiple versions")])
def test_h1_with_the_stage_plan_a_non_training_stage_is_never_judged(tmp_path, freshest, tail_marker):
    """THE FIX. Given the eval's resolved stage list, the score stage and the dep install are not
    training and produce no verdict at all — no LLM call, no alert, no kill."""
    wd = _workdir(tmp_path, freshest=freshest)
    plan = eval_log_plan(_STAGES)

    resolved = resolve_stage_log(wd, plan)
    assert resolved.path.name == freshest
    assert resolved.role == (LOG_ROLE_SCORE if freshest == "score.log" else LOG_ROLE_SETUP)
    assert active_training_log(wd, plan) is None          # nothing a training judge may read
    assert read_training_tail(wd, plan=plan) == ""
    assert tail_marker in (wd / freshest).read_text()     # the bytes ARE there; they are just not ours

    client = _ScriptedClient({"status": "broken", "reason": "would have killed", "confidence": 0.99})
    host = _Host(tmp_path, client=client)
    _drive_train(host, wd, plan=plan, window=0.3)

    assert client.calls == 0, "a non-training stage must not even be sent to the judge"
    assert host.store.rows(EV_TRAIN_MONITOR_ALERT) == []
    assert host.kill_signal == {} and not host.cancel.is_set()


def test_h1_the_training_stage_log_is_what_reaches_the_judge(tmp_path):
    """The same workdir, with the TRAIN stage live: the plan reads that log and no other."""
    wd = _workdir(tmp_path, freshest="train.log")
    plan = eval_log_plan(_STAGES)

    resolved = resolve_stage_log(wd, plan)
    assert (resolved.stage, resolved.role) == ("train", LOG_ROLE_WORK)   # judged, but advisory
    assert active_training_log(wd, plan).name == "train.log"

    client = _ScriptedClient({"status": "healthy", "reason": "loss falling", "confidence": 0.9})
    host = _Host(tmp_path, client=client)
    _drive_train(host, wd, plan=plan, until=lambda _h: client.calls >= 1)

    assert "step" in client.digests[0] and "CUDA not available" not in client.digests[0]
    span = host.spans("train_monitor")[0]["attributes"]
    assert span.get("stage") == "train" and span.get("log_role") == LOG_ROLE_WORK


def test_h1_a_stray_candidate_log_is_ignored_rather_than_judged(tmp_path):
    """A `*.log` the plan cannot name (the candidate's own file) is not silently judged as training."""
    wd = _workdir(tmp_path, freshest="train.log")
    (wd / "checkpoint.log").write_text("this is not a stage log\n", encoding="utf-8")
    _mtimes(wd, ["setup.log", "score.log", "train.log", "checkpoint.log"])   # stray is freshest

    assert active_training_log(wd).name == "checkpoint.log"          # no plan -> old broad answer
    plan = eval_log_plan(_STAGES)
    assert active_training_log(wd, plan).name == "train.log"         # with a plan -> attributed only
    assert "not a stage log" not in read_training_tail(wd, plan=plan)


def test_h1_the_log_plan_names_every_shape_the_eval_can_write():
    """`eval_log_plan` is pure, and covers all three naming shapes `command_eval` produces."""
    staged = eval_log_plan(_STAGES)
    assert staged.stage_names == ("data_prep", "train", "score")
    # WORK, not TRAINING: nothing in a resolved pipeline says WHICH work stage trains (H-1(d)).
    assert staged.roles["data_prep.log"] == ("data_prep", LOG_ROLE_WORK)
    assert staged.roles["train.log"] == ("train", LOG_ROLE_WORK)
    assert staged.roles["score.log"] == ("score", LOG_ROLE_SCORE)
    assert staged.roles["setup.log"] == (None, LOG_ROLE_SETUP)
    assert "eval.log" not in staged.roles              # staged evals never write the single-command log

    # The classic single-command eval: one command that trains AND scores, so `eval.log` IS training.
    for empty in ([], None):
        single = eval_log_plan(empty)
        assert single.stage_names == ()
        assert single.roles["eval.log"] == (None, LOG_ROLE_TRAINING)
        assert single.roles["setup.log"] == (None, LOG_ROLE_SETUP)

    # A ONE-stage pipeline is that same shape wearing a stage name, so it keeps kill authority.
    operator = eval_log_plan([{"name": "train_and_eval", "command": ["python", "go.py"]}])
    assert operator.roles["train_and_eval.log"] == ("train_and_eval", LOG_ROLE_TRAINING)


def test_h1_an_unresolvable_workdir_fails_closed(tmp_path):
    """No log yet, and a plan whose names are all absent, both degrade to "nothing to judge"."""
    wd = tmp_path / "empty"
    wd.mkdir()
    assert resolve_stage_log(wd) is None and resolve_stage_log(wd, eval_log_plan(_STAGES)) is None
    assert active_training_log(wd, eval_log_plan(_STAGES)) is None
    # A workdir holding ONLY logs the plan cannot name is the same answer, never a guess.
    (wd / "mystery.log").write_text("hello\n", encoding="utf-8")
    assert resolve_stage_log(wd, eval_log_plan(_STAGES)) is None


# ============================================ H-1(b): the stage identity has to reach the model
def test_h1b_stage_identity_reaches_the_model_without_touching_the_prompt_contract(tmp_path):
    """The second, independent layer: a model shown the wrong log can only decline if it is TOLD which
    stage it is looking at. Measured on the live endpoint, a scorer tail carrying the stage identity
    draws "cannot determine, the stage is 'score' not 'train'" / "healthy" — never `broken`."""
    wd = _workdir(tmp_path, freshest="train.log")
    plan = eval_log_plan(_STAGES)
    client = _ScriptedClient({"status": "healthy", "reason": "loss falling", "confidence": 0.9})
    host = _Host(tmp_path, client=client)
    _drive_train(host, wd, plan=plan, context="Optimizing metric 'recall'.",
                 until=lambda _h: client.calls >= 1)

    system, user = client.messages[0][0], client.messages[0][1]
    # The contract text is untouched: same system prompt, same log header, same closing instruction.
    assert system == {"role": "system", "content": _MONITOR_SYSTEM}
    assert "LIVE TRAINING LOG (recent tail):" in user["content"]
    assert user["content"].endswith("Classify this run's health from the log evidence above.")
    # The stage identity is present, and sits between the caller's context and the log it describes.
    body = user["content"]
    assert "stage 'train'" in body and "data_prep -> train -> score" in body
    assert "stage 2 of 3" in body
    assert body.index("Optimizing metric") < body.index("stage 'train'") < body.index("LIVE TRAINING LOG")


def test_h1b_stage_context_names_every_attribution_including_none():
    """Pure: what the model is told for each role, including the honest "unidentified" case."""
    plan = eval_log_plan(_STAGES)
    staged = monitor_stage_context(
        ActiveStageLog(path=Path("train.log"), stage="train", role=LOG_ROLE_TRAINING), plan)
    assert "stage 'train'" in staged and "stage 2 of 3" in staged

    single = monitor_stage_context(
        ActiveStageLog(path=Path("eval.log"), stage=None, role=LOG_ROLE_TRAINING),
        eval_log_plan([]))
    assert "ONE command" in single and "trains and scores" in single

    unknown = monitor_stage_context(
        ActiveStageLog(path=Path("mystery.log"), stage=None, role=LOG_ROLE_UNKNOWN), None)
    assert "could not be attributed" in unknown and "may not be the training stage" in unknown
    assert monitor_stage_context(None) == ""


# ================================================= H-1(c): one confident tick is not a kill gate
def test_h1c_a_single_confident_broken_tick_arms_the_gate_and_a_second_one_kills(tmp_path):
    wd = _workdir(tmp_path, freshest="train.log")
    plan = eval_log_plan(_TRAINING_STAGES)
    client = _ScriptedClient({"status": "broken", "reason": "loss is nan since step 40",
                              "confidence": 0.95})
    host = _Host(tmp_path, client=client)
    _drive_train(host, wd, plan=plan, until=lambda h: h.kill_signal.get("kill"))

    assert host.kill_signal.get("kill") is True
    assert host.kill_signal.get("terminal_reason") == "monitor_broken"
    assert client.calls == 2, "exactly one confirming look — no more, no less"
    spans = host.spans("train_monitor")
    assert spans[0]["attributes"].get("kill_armed") is True      # armed, did not act
    assert spans[0]["attributes"].get("kill") is None
    assert spans[1]["attributes"].get("kill") is True            # confirmed, acted
    # The confirming look is scheduled sooner than the ordinary cadence.
    assert spans[0]["attributes"]["next_check_s"] <= host._cadence


def test_h1c_a_broken_verdict_that_does_not_repeat_never_kills(tmp_path):
    """An alternating broken/healthy observer is never two-in-a-row, so it stays advisory forever."""
    wd = _workdir(tmp_path, freshest="train.log")
    plan = eval_log_plan(_TRAINING_STAGES)
    client = _ScriptedClient({"status": "broken", "reason": "looked bad", "confidence": 0.99},
                             {"status": "healthy", "reason": "recovered", "confidence": 0.99},
                             {"status": "broken", "reason": "looked bad again", "confidence": 0.99},
                             {"status": "healthy", "reason": "recovered again", "confidence": 0.99})
    host = _Host(tmp_path, client=client)
    _drive_train(host, wd, plan=plan, until=lambda _h: client.calls >= 4)

    assert host.kill_signal == {} and not host.cancel.is_set()
    statuses = [row["status"] for row in host.store.rows(EV_TRAIN_MONITOR_ALERT)]
    assert "broken" in statuses and "healthy" in statuses      # both edges still durably recorded


def test_h1c_the_confirming_look_happens_even_if_the_log_went_quiet(tmp_path):
    """A run that diverges and then stops printing must not survive by saying nothing: the arming tick
    bypasses the changed-digest gate exactly once, which is what makes the confirmation honest."""
    wd = _workdir(tmp_path, freshest="train.log")
    plan = eval_log_plan(_TRAINING_STAGES)
    before = (wd / "train.log").read_text()
    client = _ScriptedClient({"status": "broken", "reason": "diverged then silent", "confidence": 0.9})
    host = _Host(tmp_path, client=client)
    _drive_train(host, wd, plan=plan, until=lambda h: h.kill_signal.get("kill"))

    assert (wd / "train.log").read_text() == before            # the log never changed between ticks
    assert client.calls == 2 and host.kill_signal.get("kill") is True


# ================================================================ H-4: durable kill attribution
class _ClaimingJudge:
    """An ASHA judge whose provider call the SIBLING training monitor wins DURING — the real race, with
    the claim landing in the exact window the old code appended its row from."""

    def __init__(self, verdict, host):
        self._verdict = verdict
        self._host = host
        self.calls = 0

    def complete_tool(self, messages, schema):
        self.calls += 1
        claim_watchdog_kill(self._host.kill_signal, self._host.cancel,
                            reason="loss became NaN", terminal_reason="monitor_broken",
                            confidence=0.97)
        return dict(self._verdict)


def _asha_setup(tmp_path):
    """A live curve genuinely below three same-rung peers, so the deterministic rank gate fires."""
    wd = tmp_path / "node_0"
    wd.mkdir()
    (wd / "train.log").write_text('{"recall": 0.01, "step": 1}\n', encoding="utf-8")
    curves = [[[1.0, 0.05]], [[1.0, 0.07]], [[1.0, 0.09]]]
    return wd, {"kind": "stdout_json", "key": "recall", "resource_key": "step"}, curves


def _drive_asha(host, wd, spec, monkeypatch, curves, *, plan=None, until=None, window=0.3):
    from looplab.core.models import Idea, Node, NodeStatus, RunState

    idea = Idea(operator="draft", params={}, rationale="watchdog race")
    nodes = {0: Node(id=0, operator="draft", idea=idea, status=NodeStatus.pending)}
    for index, metric in enumerate([0.80, 0.70, 0.60], start=1):
        nodes[index] = Node(id=index, operator="draft", idea=idea, metric=metric,
                            status=NodeStatus.evaluated, resource_curve=curves[index - 1])
    monkeypatch.setattr("looplab.events.replay.fold", lambda events: RunState(nodes=nodes))

    async def _run():
        async with anyio.create_task_group() as tg:
            tg.start_soon(host._monitor_asha, 0, 0, str(wd), host.cancel, spec, "max",
                          host.kill_signal, None, plan)
            if until is None:
                await anyio.sleep(window)
            else:
                deadline = time.monotonic() + _SETTLE_TIMEOUT_S
                while not until(host) and time.monotonic() < deadline:
                    await anyio.sleep(0.005)
            tg.cancel_scope.cancel()

    anyio.run(_run)


def test_h4_a_lost_claim_is_recorded_as_kill_false_next_to_the_winner(tmp_path, monkeypatch):
    """The durable row and the terminal must agree. The judge decides `stop`, the training monitor wins
    the claim while that call is in flight, and the row records the OUTCOME, not the intention."""
    wd, spec, curves = _asha_setup(tmp_path)
    host = _Host(tmp_path, client=None)
    judge = _ClaimingJudge({"status": "stop", "reason": "flat while peers climbed", "confidence": 0.95},
                           host)
    host.developer = _Developer(judge)
    _drive_asha(host, wd, spec, monkeypatch, curves,
                until=lambda h: h.store.rows(EV_ASHA_VERDICT))

    rows = host.store.rows(EV_ASHA_VERDICT)
    assert len(rows) == 1
    assert rows[0]["status"] == "stop" and rows[0]["stop_decided"] is True
    assert rows[0]["kill"] is False, "the loser must not claim the terminal it did not win"
    assert rows[0]["kill_superseded_by"] == "monitor_broken"
    # The one terminal the node will actually get names the winner, and only the winner.
    assert host.kill_signal["terminal_reason"] == "monitor_broken"
    assert "NaN" in host.kill_signal["reason"]


def test_h4_the_winner_records_kill_true(tmp_path, monkeypatch):
    """Control: with no sibling in the race the same decision claims, and the row says so."""
    wd, spec, curves = _asha_setup(tmp_path)
    host = _Host(tmp_path, client=None)

    class _Judge:
        calls = 0

        def complete_tool(self, messages, schema):
            type(self).calls += 1
            return {"status": "stop", "reason": "hopeless", "confidence": 0.95}

    host.developer = _Developer(_Judge())
    _drive_asha(host, wd, spec, monkeypatch, curves, until=lambda h: h.kill_signal.get("kill"))

    row = host.store.rows(EV_ASHA_VERDICT)[0]
    assert row["stop_decided"] is True and row["kill"] is True
    assert "kill_superseded_by" not in row
    assert host.kill_signal["terminal_reason"] == "asha_underperforming"


def test_h4_the_training_monitor_records_the_same_attribution_pair(tmp_path):
    """The sibling shape: when ASHA wins first, the training monitor's alert says so too."""
    wd = _workdir(tmp_path, freshest="train.log")
    plan = eval_log_plan(_TRAINING_STAGES)
    host = _Host(tmp_path, client=_ScriptedClient(
        {"status": "broken", "reason": "loss is nan", "confidence": 0.95}))
    # ASHA already owns this eval's terminal by the time the confirming verdict lands.
    claim_watchdog_kill(host.kill_signal, threading.Event(), reason="below the sibling bar",
                        terminal_reason="asha_underperforming")
    _drive_train(host, wd, plan=plan,
                 until=lambda h: any(r.get("stop_decided") for r in h.store.rows(EV_TRAIN_MONITOR_ALERT)))

    decided = [r for r in host.store.rows(EV_TRAIN_MONITOR_ALERT) if r.get("stop_decided")]
    assert decided and decided[0]["kill"] is False
    assert decided[0]["kill_superseded_by"] == "asha_underperforming"
    assert host.kill_signal["terminal_reason"] == "asha_underperforming"   # unchanged by the loser


def test_h4_neither_bit_claims_the_node_actually_stopped():
    """`events/types.py` used to document `EV_ASHA_VERDICT.kill` as "whether the node was actually
    stopped". It cannot mean that, and not only because of the claim race: `_evaluate` converts a claim
    into a terminal ONLY when the eval had not already produced a usable result, so a kill claimed
    against an eval that just succeeded still ends in `node_evaluated`. The row is written before that
    is decided, so the registry note must describe a DECISION and a CLAIM, and point at the node's own
    terminal for the outcome."""
    import inspect

    from looplab.engine import evaluate
    from looplab.events import types

    # The gate that makes a claimed kill non-binding on an already-successful eval.
    source = inspect.getsource(evaluate.EvaluateMixin._evaluate)
    assert 'kill_signal.get("kill") and not ok' in source

    # ...and it is not the only pre-emption: `superseded` (a reset) and an operator `abort` both
    # return from `_evaluate` BEFORE the kill branch is reached, so a `kill: true` row can sit beside
    # either of those terminals too. Pin all three, in the order the function tests them.
    positions = [source.index(marker) for marker in
                 ("if superseded:", "if aborted and not ok:", 'if kill_signal.get("kill") and not ok:')]
    assert positions == sorted(positions), "the kill branch must stay LAST of the four outcomes"

    note = inspect.getsource(types)
    marker = note.index("EV_ASHA_VERDICT = ")
    verdict_note = note[note.index("EV_ASHA_RANK = "):marker]
    assert "records whether the node was actually stopped" not in verdict_note
    assert "stop_decided" in verdict_note and "kill_superseded_by" in verdict_note
    assert "node's single terminal" in verdict_note or "single `node_failed`" in verdict_note
    # The note must name every terminal a `kill: true` row can accompany, not only `node_evaluated`.
    assert "superseded" in verdict_note and "aborted" in verdict_note


def test_h4_an_ordinary_advisory_alert_carries_no_kill_attribution(tmp_path):
    """The attribution pair appears only on a row that DECIDED something — a 'watch' row is silent."""
    wd = _workdir(tmp_path, freshest="train.log")
    host = _Host(tmp_path, client=_ScriptedClient(
        {"status": "watch", "reason": "plateauing", "confidence": 0.6}))
    _drive_train(host, wd, plan=eval_log_plan(_STAGES),
                 until=lambda h: h.store.rows(EV_TRAIN_MONITOR_ALERT))

    row = host.store.rows(EV_TRAIN_MONITOR_ALERT)[0]
    assert row["status"] == "watch"
    assert "kill" not in row and "stop_decided" not in row


# ======================================================= H-3: teardown must not pay for the loser
class _BlockingJudge:
    """Stands in for a real endpoint call: slow, and (by design) un-abandonable once started."""

    def __init__(self, seconds: float):
        self.seconds = seconds
        self.calls = 0
        self.started = threading.Event()

    def complete_tool(self, messages, schema):
        self.calls += 1
        self.started.set()
        time.sleep(self.seconds)
        return {"status": "stop", "reason": "too slow to matter", "confidence": 0.99}


def test_h3_asha_does_not_start_a_paid_judge_call_after_a_sibling_already_claimed(tmp_path, monkeypatch):
    """The loser used to re-check `cancel` only at the top of its loop, so it could still START an
    un-abandonable judge call after the training monitor had killed the node — and `_evaluate`'s
    task-group cancel then waited a whole endpoint timeout for a verdict about a dead node."""
    wd, spec, curves = _asha_setup(tmp_path)
    judge = _BlockingJudge(5.0)
    host = _Host(tmp_path, client=None)
    host.developer = _Developer(judge)

    # The sibling wins DURING this tick's fold — `_observe` reads the store in the worker thread,
    # which is exactly the window between the top-of-loop check and the judge gate.
    real_read_all = host.store.read_all
    reads = {"n": 0}

    def _read_all():
        reads["n"] += 1
        if reads["n"] >= 4:                       # 1 resume recovery + one per tick; tick 3 fires the gate
            claim_watchdog_kill(host.kill_signal, host.cancel, reason="loss became NaN",
                                terminal_reason="monitor_broken", confidence=0.97)
        return real_read_all()

    host.store.read_all = _read_all

    started = time.monotonic()
    _drive_asha(host, wd, spec, monkeypatch, curves,
                until=lambda h: any(s["attributes"].get("cancelled_before_call")
                                    for s in h.spans("asha_judge")))
    elapsed = time.monotonic() - started

    assert judge.calls == 0, "the loser must not buy a verdict about a node that is already ending"
    assert not judge.started.is_set()
    assert host.store.rows(EV_ASHA_VERDICT) == []          # nothing decided, nothing recorded
    assert host.kill_signal["terminal_reason"] == "monitor_broken"
    # Teardown is no longer serialized behind a second provider call: the whole drive finishes far
    # inside the 5s that one blocked, un-abandonable call would have cost.
    assert elapsed < 2.0, f"teardown waited {elapsed:.2f}s — it should not have started the call"


def test_h3_the_training_monitor_also_declines_to_start_a_doomed_call(tmp_path, monkeypatch):
    """Same guard on the sibling: the ASHA watchdog wins while the tail is being read."""
    import looplab.engine.train_monitor as tm

    wd = _workdir(tmp_path, freshest="train.log")
    client = _ScriptedClient({"status": "broken", "reason": "unused", "confidence": 0.99})
    host = _Host(tmp_path, client=client)
    real_tail = tm.read_training_tail

    def _tail(*args, **kwargs):
        claim_watchdog_kill(host.kill_signal, host.cancel, reason="below the sibling bar",
                            terminal_reason="asha_underperforming")
        return real_tail(*args, **kwargs)

    monkeypatch.setattr(tm, "read_training_tail", _tail)
    _drive_train(host, wd, plan=eval_log_plan(_STAGES),
                 until=lambda h: any(s["attributes"].get("cancelled_before_call")
                                     for s in h.spans("train_monitor")))

    assert client.calls == 0
    assert host.store.rows(EV_TRAIN_MONITOR_ALERT) == []
    assert host.kill_signal["terminal_reason"] == "asha_underperforming"


# ============================================================ H-5: the operator must see the kill
pytest.importorskip("fastapi")
from looplab.events.eventstore import EventStore                       # noqa: E402
from looplab.events.types import (                                     # noqa: E402
    EV_ASHA_RANK, EV_NODE_CREATED, EV_NODE_EVALUATED, EV_NODE_FAILED, EV_NODE_RESET,
    EV_RUN_STARTED)
from looplab.serve.attention import project_run_attention              # noqa: E402


def _attention_store(root: Path) -> EventStore:
    rd = root / "demo"
    rd.mkdir(parents=True, exist_ok=True)
    store = EventStore(rd / "events.jsonl")
    store.append(EV_RUN_STARTED, {"run_id": "demo", "task_id": "task", "goal": "g",
                                  "direction": "min"})
    store.append(EV_NODE_CREATED, {"node_id": 0, "generation": 0, "parent_ids": [],
                                   "operator": "draft",
                                   "idea": {"operator": "draft", "rationale": "test"}})
    return store


def _items(store, kind):
    return [item for item in project_run_attention("demo", store.read_all(), engine_running=True)
            if item["kind"] == kind]


def test_h5_a_monitor_killed_node_keeps_its_attention_item(tmp_path):
    """The alert and its kill terminal land seconds apart, so the pending-only filter deleted the
    operator's only signal before any realistic poll could see it."""
    store = _attention_store(tmp_path)
    store.append(EV_TRAIN_MONITOR_ALERT, {"node_id": 0, "generation": 0, "status": "broken",
                                          "reason": "loss diverged to NaN", "confidence": 0.95})
    while_pending = _items(store, "train_monitor")
    assert len(while_pending) == 1

    store.append(EV_NODE_FAILED, {"node_id": 0, "generation": 0, "reason": "monitor_broken",
                                  "error": "live watchdog stopped the run early", "eval_seconds": 1.0})
    after_kill = _items(store, "train_monitor")
    assert len(after_kill) == 1, "the watchdog's own kill must not erase the watchdog's signal"
    # Same stable id, so a client that already saw it is not re-notified — and still notifiable
    # (browser && !derived) for one that has not.
    assert after_kill[0]["id"] == while_pending[0]["id"]
    assert after_kill[0]["browser"] is True and after_kill[0]["derived"] is False
    assert after_kill[0]["node_id"] == 0 and after_kill[0]["node_generation"] == 0
    assert "stopped this experiment early" in after_kill[0]["detail"]
    # The redaction contract still holds: no LLM-derived verdict prose in the envelope.
    assert "NaN" not in json.dumps(after_kill[0]) and "diverged" not in json.dumps(after_kill[0])


def test_h5_an_asha_killed_node_keeps_its_item_too(tmp_path):
    store = _attention_store(tmp_path)
    store.append(EV_ASHA_RANK, {"node_id": 0, "generation": 0, "underperforming": True,
                                "intermediate": 0.01, "quantile": 0.5, "population": 3,
                                "direction": "max"})
    assert len(_items(store, "asha")) == 1
    store.append(EV_NODE_FAILED, {"node_id": 0, "generation": 0, "reason": "asha_underperforming",
                                  "error": "live watchdog stopped the run early", "eval_seconds": 1.0})
    after = _items(store, "asha")
    assert len(after) == 1 and "stopped it early" in after[0]["detail"]
    assert after[0]["browser"] is False        # still the softer, inbox-only tier


@pytest.mark.parametrize("terminal,data", [
    (EV_NODE_FAILED, {"reason": "crash", "error": "boom", "eval_seconds": 1.0}),
    (EV_NODE_EVALUATED, {"metric": 0.3, "eval_seconds": 1.0}),
])
def test_h5_an_ordinary_terminal_still_retires_a_stale_alert(tmp_path, terminal, data):
    """The original property is preserved exactly: a node that finished for any OTHER reason has a
    stale, non-actionable alert and it leaves the inbox."""
    store = _attention_store(tmp_path)
    store.append(EV_TRAIN_MONITOR_ALERT, {"node_id": 0, "generation": 0, "status": "broken",
                                          "reason": "loss diverged", "confidence": 0.95})
    assert len(_items(store, "train_monitor")) == 1
    store.append(terminal, {"node_id": 0, "generation": 0, **data})
    assert _items(store, "train_monitor") == []


def test_h5_a_kill_from_the_OTHER_watchdog_does_not_resurrect_this_ones_item(tmp_path):
    """Attribution matters in the feed too: an ASHA kill is not the training monitor's signal."""
    store = _attention_store(tmp_path)
    store.append(EV_TRAIN_MONITOR_ALERT, {"node_id": 0, "generation": 0, "status": "broken",
                                          "reason": "loss diverged", "confidence": 0.95})
    store.append(EV_NODE_FAILED, {"node_id": 0, "generation": 0, "reason": "asha_underperforming",
                                  "error": "stopped", "eval_seconds": 1.0})
    assert _items(store, "train_monitor") == []


def test_h5_a_reset_lifecycle_still_drops_the_killed_items_alert(tmp_path):
    """Bounded by the CURRENT attempt: a later generation of the same node retires the old episode."""
    store = _attention_store(tmp_path)
    store.append(EV_TRAIN_MONITOR_ALERT, {"node_id": 0, "generation": 0, "status": "broken",
                                          "reason": "loss diverged", "confidence": 0.95})
    store.append(EV_NODE_FAILED, {"node_id": 0, "generation": 0, "reason": "monitor_broken",
                                  "error": "stopped", "eval_seconds": 1.0})
    assert len(_items(store, "train_monitor")) == 1
    store.append(EV_NODE_RESET, {"node_id": 0, "generation": 0, "from_stage": "eval"})
    assert _items(store, "train_monitor") == []


# ==================================================================================================
# H-1(d) WHICH STAGES MAY KILL, H-2 case, H-3(b) what breaks the streak, H-4(b) bounding the arm,
# H-5(b) durable stage attribution. Five further defects in the SAME scoping fix, each reproduced
# against a real engine + a real `deepseek-v4-flash` before being pinned here.
#
# H-1(d) `eval_log_plan` gave LOG_ROLE_TRAINING — i.e. kill authority — to every stage whose name was
#     not the exact string `score`. Live, over `data_prep -> train -> score` with a `data_prep` stage
#     printing framework warnings, `CUDA not available - falling back to CPU` and a flat `loss:
#     0.6931`, the judge answered `broken` 0.9 WHILE BEING TOLD it was looking at `data_prep`, and
#     armed the kill gate; the reviewer's run killed the node in 14.5 s, before `train` ever started.
#     `data_prep`/`preprocess`/`download`/`export`/`quantize`/`predict`/`submit` were all equally
#     armed, and a stage named `setup` shadowed `setup.log` — into which `command_eval` writes the dep
#     install — making pip output kill-eligible.
# H-2  `validate_stages` refuses the reserved scorer name case-INSENSITIVELY; `eval_log_plan` compared
#     `name == "score"`. Two byte-identical live runs differing only in capitalisation ended
#     `node_evaluated metric=0.7` (`score`) and `node_failed monitor_broken` in 14.6 s (`Score`).
#     `os.path.normcase` is identity outside Windows, so this was live on every platform we run.
# H-3(b) "two CONSECUTIVE broken verdicts; anything else re-arms from zero" was false for a tick with
#     NO PARSEABLE verdict: the streak was only ever touched inside `if verdict is not None`. The
#     commit's own cited mitigation — the model answering `unknown` — fails schema validation, so the
#     arm survived it. Reproduced: arm, six `unknown` ticks, one `broken` -> killed.
# H-4(b) The arming tick's changed-digest bypass held for as long as the arm, and the arm had no
#     bound: on a frozen log with a dead endpoint, 25 LLM calls carrying ONE distinct prompt, no kill,
#     log bytes unchanged — one billable call per cadence up to `_MAX_MONITOR_LLM_CALLS`.
# H-5(b) `log_role`/`stage` existed only on the trace span (a self-described high-volume sidecar), so
#     `watchdog_reflection` told the next Researcher "node 0: training flagged broken ... loss is
#     stuck at its initialization value" about a node whose training had not started.
# ==================================================================================================
_PREP_TAIL = ("CUDA not available - falling back to CPU. This will be slow.\n"
              "FutureWarning: `resume_download` is deprecated and will be removed in version 1.0.0.\n"
              "loss: 0.6931\nloss: 0.6931\nloss: 0.6931\n")


def _one_log_workdir(tmp_path, name: str, text: str, *, dirname: str = "node_0") -> Path:
    """A workdir holding exactly ONE stage log, so `resolve_stage_log`'s freshest-file answer is
    unambiguous without any mtime stamping."""
    wd = tmp_path / dirname
    wd.mkdir()
    (wd / name).write_text(text, encoding="utf-8")
    return wd


class _FailingClient:
    """An endpoint that never yields a usable verdict. `raises=False` answers with a status OUTSIDE
    `TrainingVerdict.status`'s Literal (what `deepseek-v4-flash` really does when it declines: it
    says `unknown`), so pydantic rejects it and `_training_verdict` returns None — the exact shape the
    old streak logic treated as "nothing happened"."""

    def __init__(self, *, raises: bool = False, before=()):
        self._before = list(before)
        self._raises = raises
        self.calls = 0
        self.messages: list[list[dict]] = []

    def complete_tool(self, messages, schema):
        self.calls += 1
        self.messages.append([dict(m) for m in messages])
        if self._before:
            return dict(self._before.pop(0))
        if self._raises:
            raise RuntimeError("endpoint 502")
        return {"status": "unknown", "reason": "cannot determine from this stage's output",
                "confidence": 0.7}


def test_h1d_a_pipeline_work_stage_is_judged_but_can_never_kill(tmp_path):
    """THE REPRODUCTION, closed. `data_prep` is read and judged exactly as before — the advisory
    record is the watchdog's main job — but however confidently and however often it says `broken`,
    it cannot end the node."""
    wd = _one_log_workdir(tmp_path, "data_prep.log", _PREP_TAIL)
    plan = eval_log_plan(_STAGES)

    resolved = resolve_stage_log(wd, plan)
    assert (resolved.stage, resolved.role) == ("data_prep", LOG_ROLE_WORK)
    assert active_training_log(wd, plan).name == "data_prep.log"     # still READ: advisory needs it

    client = _ScriptedClient({"status": "broken", "reason": "silent CPU fallback, loss stuck at 0.6931",
                              "confidence": 0.9})
    host = _Host(tmp_path, client=client)
    _drive_train(host, wd, plan=plan, until=lambda h: len(h.store.rows(EV_TRAIN_MONITOR_ALERT)) >= 2)

    # judged, recorded, narrated — and harmless.
    rows = host.store.rows(EV_TRAIN_MONITOR_ALERT)
    assert rows and all(r["status"] == "broken" for r in rows)
    assert host.kill_signal == {} and not host.cancel.is_set()
    assert all("stop_decided" not in r for r in rows)
    span = host.spans("train_monitor")[0]["attributes"]
    assert span["log_role"] == LOG_ROLE_WORK and span.get("kill_armed") is None


def test_h1d_kill_authority_belongs_only_to_a_log_that_is_the_whole_eval():
    """The pure decision surface, over every role the plan can produce."""
    verdict = TrainingVerdict(status="broken", reason="diverged", confidence=0.99)
    for role in (LOG_ROLE_WORK, LOG_ROLE_SETUP, LOG_ROLE_SCORE,
                 LOG_ROLE_AMBIGUOUS, LOG_ROLE_UNKNOWN):
        assert should_monitor_kill(verdict, enabled=True, threshold=0.8,
                                   log_role=role, broken_streak=99) is False
    assert should_monitor_kill(verdict, enabled=True, threshold=0.8,
                               log_role=LOG_ROLE_TRAINING, broken_streak=2) is True
    # ... and only those two shapes earn that role: the single command, and a one-stage pipeline.
    assert eval_log_plan([]).roles["eval.log"][1] == LOG_ROLE_TRAINING
    assert eval_log_plan(_TRAINING_STAGES).roles["train.log"][1] == LOG_ROLE_TRAINING
    for stages in (_STAGES, [{"name": "train", "command": ["x"]}, {"name": "score", "command": ["x"]}]):
        assert eval_log_plan(stages).roles["train.log"][1] == LOG_ROLE_WORK


def test_h1d_a_stage_that_shadows_setup_log_never_makes_pip_output_kill_eligible(tmp_path):
    """`command_eval` writes the dep install into `setup.log` whatever the pipeline is called, so a
    stage named `setup` gives that filename two writers. Unattributable -> no tick at all (it used to
    "win the name" and inherit training authority over pip output)."""
    plan = eval_log_plan([{"name": "setup", "command": ["python", "prep.py"]}] + _STAGES[1:])
    assert plan.roles["setup.log"] == (None, LOG_ROLE_AMBIGUOUS)

    wd = _one_log_workdir(tmp_path, "setup.log", _SETUP_TAIL)
    assert resolve_stage_log(wd, plan).role == LOG_ROLE_AMBIGUOUS
    assert active_training_log(wd, plan) is None and read_training_tail(wd, plan=plan) == ""

    client = _ScriptedClient({"status": "broken", "reason": "would have killed", "confidence": 0.99})
    host = _Host(tmp_path, client=client)
    _drive_train(host, wd, plan=plan, window=0.3)
    assert client.calls == 0 and host.kill_signal == {}


# ------------------------------------------------------------------------ H-2: `Score` vs `score`
@pytest.mark.parametrize("scorer", ["score", "Score", "SCORE", "scoring", "evaluate", "eval"])
def test_h2_the_scorer_is_identified_by_position_not_by_the_string_score(tmp_path, scorer):
    """An operator-declared pipeline owns its scoring stage's NAME. `command_eval` reads the metric
    from the LAST stage's stdout, so that is what identifies the scorer — no spelling involved."""
    stages = [{"name": "train", "command": ["python", "train.py"]},
              {"name": scorer, "command": ["python", f"{scorer}.py"]}]
    plan = eval_log_plan(stages)
    assert plan.roles[f"{scorer}.log"] == (scorer, LOG_ROLE_SCORE)

    wd = _one_log_workdir(tmp_path, f"{scorer}.log", _SCORE_TAIL)
    assert active_training_log(wd, plan) is None                  # not judged, so not killable
    client = _ScriptedClient({"status": "broken", "reason": "would have killed", "confidence": 0.99})
    host = _Host(tmp_path, client=client)
    _drive_train(host, wd, plan=plan, window=0.3)
    assert client.calls == 0 and host.kill_signal == {} and not host.cancel.is_set()


def test_h2_the_reserved_name_backstop_matches_validate_stages_exactly():
    """The two definitions of "this name means the scorer" must not drift: `validate_stages` refuses
    the reserved name case-INSENSITIVELY, so any spelling it would refuse to the agent must read as a
    scorer here even in the middle of an operator's pipeline (where it is NOT refused)."""
    from looplab.runtime.command_eval import validate_stages

    for spelling in ("score", "Score", "SCORE", "sCoRe"):
        # the Developer manifest path: reserved, refused, never reaches a plan at all.
        clean, err = validate_stages([{"name": spelling, "command": ["python", "s.py"]}],
                                     reserved=("score",))
        assert clean is None and err is not None
        # the operator path: accepted (the operator owns scoring) — and read as the scorer.
        clean, err = validate_stages([{"name": spelling, "command": ["python", "s.py"]},
                                      {"name": "train", "command": ["python", "t.py"]}])
        assert err is None
        plan = eval_log_plan(clean)
        assert plan.roles[f"{spelling}.log".lower() if os.name == "nt" else f"{spelling}.log"] \
            == (spelling, LOG_ROLE_SCORE), "a reserved spelling is the scorer wherever it appears"


# ------------------------------------------- H-3(b): a tick with no parseable answer breaks the arm
def test_h3b_a_verdict_that_does_not_parse_re_arms_the_gate_from_zero(tmp_path):
    """REPRODUCTION + fix. `unknown` is not a `TrainingVerdict.status`, so the answer never validates
    and the tick yields no verdict. Under the docstring that is "anything else" and must disarm — the
    old code only touched the streak on a parsed verdict, so the arm survived six of these and the
    NEXT `broken` killed as though the two were consecutive."""
    wd = _one_log_workdir(tmp_path, "train.log", _TRAIN_TAIL)
    plan = eval_log_plan(_TRAINING_STAGES)          # kill-ELIGIBLE, so only the streak is in question
    broken = {"status": "broken", "reason": "loss is nan", "confidence": 0.95}
    client = _FailingClient(before=[broken] + [{"status": "unknown", "reason": "?", "confidence": 0.7}] * 6
                            + [broken])
    host = _Host(tmp_path, client=client)
    _drive_train(host, wd, plan=plan, until=lambda _h: client.calls >= 8)

    assert host.kill_signal == {} and not host.cancel.is_set()
    spans = host.spans("train_monitor")
    assert spans[0]["attributes"].get("kill_armed") is True          # it really did arm...
    assert any(s["attributes"].get("verdict_unparsed") for s in spans)   # ...and really was disarmed
    assert not any(s["attributes"].get("kill") for s in spans)


def test_h3b_an_endpoint_failure_between_two_broken_verdicts_never_confirms(tmp_path):
    """The same rule for a raising endpoint: an outage is transparent, not safe."""
    wd = _one_log_workdir(tmp_path, "train.log", _TRAIN_TAIL)
    log = wd / "train.log"
    plan = eval_log_plan(_TRAINING_STAGES)
    broken = {"status": "broken", "reason": "loss is nan", "confidence": 0.95}

    class _FlakyClient(_FailingClient):
        """Fails the SECOND call. Each call also grows the log, so the changed-digest gate keeps
        letting ticks through — otherwise a frozen log legitimately goes quiet after the disarm and
        the question this test asks (does a failure break the streak?) never comes up.

        The appended line moves its STEP and prints the nan this client's own verdict names. It
        used to descend 2.0 -> 1.9 -> 1.8, which the measured-trajectory conjunct (2026-08-14) reads
        — correctly — as a run that is plainly still learning, and refuses to kill: `_TRAIN_TAIL`
        opens 2.9 -> 2.1, so ANY flat continuation is still a net descent from the first reading.
        That refusal is about a different question than the one this test asks, and a fixture whose
        log contradicts its own verdict is what let the two questions collide. A nan is what "loss
        is nan" looks like in a log, and it withdraws the veto by the anomaly rung, so the
        confirmation-streak property is tested with nothing else in the way."""

        def complete_tool(self, messages, schema):
            self.calls += 1
            log.write_text(log.read_text() + f'{{"step": {self.calls + 2}, "loss": nan}}\n',
                           encoding="utf-8")
            if self.calls == 2:
                raise RuntimeError("endpoint 502")
            return dict(broken)

    client = _FlakyClient()
    host = _Host(tmp_path, client=client)
    _drive_train(host, wd, plan=plan, until=lambda h: h.kill_signal.get("kill"))

    # It DOES eventually kill — calls 3 and 4 are two clean consecutive broken verdicts. The point is
    # that call 3 could not confirm call 1 across the failure in between: a single-tick gate, or a
    # streak that survives an unparseable answer, would have killed on call 3.
    assert host.kill_signal.get("kill") is True
    assert client.calls >= 4, "the failed tick did not count toward the confirmation"


def test_h3b_a_stale_arm_expires_on_the_clock(tmp_path, monkeypatch):
    """The wall-clock half of the bound: an arm older than `_MONITOR_ARM_TTL_S` is not evidence, so a
    slow endpoint cannot leave a kill primed indefinitely."""
    import looplab.engine.train_monitor as tm

    monkeypatch.setattr(tm, "_MONITOR_ARM_TTL_S", 0.0)               # every arm is instantly stale
    wd = _one_log_workdir(tmp_path, "train.log", _TRAIN_TAIL)
    client = _ScriptedClient({"status": "broken", "reason": "loss is nan", "confidence": 0.95})
    host = _Host(tmp_path, client=client)
    _drive_train(host, wd, plan=eval_log_plan(_TRAINING_STAGES), window=0.4)

    assert client.calls >= 1 and host.kill_signal == {}, "a stale arm must not confirm itself"


# --------------------------------------------------------- H-4(b): the armed re-look is bounded
def test_h4b_the_armed_re_look_is_bounded_to_one_identical_prompt(tmp_path):
    """A frozen log + an endpoint that stops answering. The arm buys exactly ONE re-ask (the
    confirming look the commit intends), then disarms — instead of re-sending the same prompt every
    cadence for the rest of the eval."""
    wd = _one_log_workdir(tmp_path, "train.log", _TRAIN_TAIL)
    frozen = (wd / "train.log").read_text()
    client = _FailingClient(before=[{"status": "broken", "reason": "diverged then silent",
                                     "confidence": 0.95}], raises=True)
    host = _Host(tmp_path, client=client)
    _drive_train(host, wd, plan=eval_log_plan(_TRAINING_STAGES), window=0.5)   # ~25 ticks at 0.02s

    assert (wd / "train.log").read_text() == frozen                  # the log never changed
    assert client.calls == 2, "arming tick + exactly one confirming look, then quiet"
    assert host.kill_signal == {}
    spans = host.spans("train_monitor")
    assert spans[0]["attributes"].get("kill_armed") is True
    assert spans[1]["attributes"].get("confirm_digest_unchanged") is True   # a byte-identical re-ask
    assert spans[1]["attributes"].get("verdict_unparsed") is True


def test_h4b_a_frozen_log_with_a_dead_endpoint_stops_re_asking(tmp_path):
    """The un-armed half of the same runaway: `last_digest` is committed only on a usable verdict, so
    an endpoint that never answers re-sent a byte-identical prompt every cadence. It now retires the
    digest after `_MONITOR_SAME_DIGEST_RETRIES` and goes quiet until the log actually changes."""
    import looplab.engine.train_monitor as tm

    wd = _one_log_workdir(tmp_path, "train.log", _TRAIN_TAIL)
    client = _FailingClient(raises=True)
    host = _Host(tmp_path, client=client)
    _drive_train(host, wd, plan=eval_log_plan(_TRAINING_STAGES), window=0.5)

    assert client.calls == tm._MONITOR_SAME_DIGEST_RETRIES, "the same question, asked twice, then dropped"
    assert any(s["attributes"].get("digest_retired") for s in host.spans("train_monitor"))
    prompts = {m[-1]["content"] for m in getattr(client, "messages", [])} if hasattr(client, "messages") else set()
    assert len(prompts) <= 1                                          # one distinct prompt, at most

    # ... and a log that DOES change is judged again, so the retirement is not a permanent mute.
    (wd / "train.log").write_text(_TRAIN_TAIL + '{"recall": 0.51, "step": 3, "loss": 1.4}\n',
                                  encoding="utf-8")
    _drive_train(host, wd, plan=eval_log_plan(_TRAINING_STAGES), window=0.2)
    assert client.calls > tm._MONITOR_SAME_DIGEST_RETRIES


# ------------------------------------------------- H-5(b): the attribution reaches the DURABLE row
def test_h5b_every_alert_names_the_eval_phase_it_was_about(tmp_path):
    """`log_role`/`stage` on the alert, not only on the trace span. Without them an audit — or the
    next Researcher's prompt — cannot tell a verdict about the training from one about `data_prep`."""
    wd = _one_log_workdir(tmp_path, "data_prep.log", _PREP_TAIL)
    host = _Host(tmp_path, client=_ScriptedClient(
        {"status": "broken", "reason": "loss stuck at 0.6931", "confidence": 0.9}))
    _drive_train(host, wd, plan=eval_log_plan(_STAGES),
                 until=lambda h: h.store.rows(EV_TRAIN_MONITOR_ALERT))

    row = host.store.rows(EV_TRAIN_MONITOR_ALERT)[0]
    assert row["log_role"] == LOG_ROLE_WORK and row["stage"] == "data_prep"

    # The single-command shape has no stage NAME to give, and says so by omission rather than by
    # inventing one — but it still records the role, which is the half that carries kill authority.
    wd2 = _one_log_workdir(tmp_path, "eval.log", _TRAIN_TAIL, dirname="node_1")
    host2 = _Host(tmp_path / "h2", client=_ScriptedClient(
        {"status": "watch", "reason": "plateauing", "confidence": 0.5}))
    (tmp_path / "h2").mkdir(exist_ok=True)
    _drive_train(host2, wd2, plan=eval_log_plan([]),
                 until=lambda h: h.store.rows(EV_TRAIN_MONITOR_ALERT))
    row2 = host2.store.rows(EV_TRAIN_MONITOR_ALERT)[0]
    assert row2["log_role"] == LOG_ROLE_TRAINING and "stage" not in row2


def test_h5b_watchdog_reflection_does_not_call_a_data_prep_verdict_training():
    """The consequence that made this visible: the prompt cue fed to the NEXT Researcher asserted
    "training flagged broken ... loss is stuck at its initialization value" about a node whose
    training had not started."""
    from looplab.events.digest import watchdog_reflection
    from looplab.events.replay import fold

    def _rows(alert: dict):
        return [Event(seq=0, ts=0.0, type="run_started",
                      data={"run_id": "r", "goal": "g", "direction": "max"}),
                Event(seq=1, ts=0.0, type="node_created",
                      data={"node_id": 0, "parent_ids": [], "operator": "draft",
                            "idea": {"operator": "draft", "params": {}}, "code": "#"}),
                Event(seq=2, ts=0.0, type=EV_TRAIN_MONITOR_ALERT, data=alert)]

    base = {"node_id": 0, "generation": 0, "status": "broken",
            "reason": "loss is stuck at its initialization value", "confidence": 0.9}
    rows = _rows(dict(base, log_role=LOG_ROLE_WORK, stage="data_prep"))
    out = watchdog_reflection(rows, state=fold(rows))
    assert "stage 'data_prep' flagged broken" in out
    assert "training flagged" not in out

    # An OLD row (written before the fields existed) must render byte-identically to before.
    legacy_rows = _rows(dict(base))
    legacy = watchdog_reflection(legacy_rows, state=fold(legacy_rows))
    assert "training flagged broken" in legacy

    # ... and a verdict that really IS about the training keeps saying so.
    training_rows = _rows(dict(base, log_role=LOG_ROLE_TRAINING))
    assert "training flagged broken" in watchdog_reflection(training_rows, state=fold(training_rows))


def test_h3b_a_switch_to_another_stages_log_re_arms_the_gate_from_zero(tmp_path):
    """CONTROL (pre-existing behaviour, re-pinned): two different subjects can never confirm each
    other. The gate arms on `eval.log`, the freshest file becomes `setup.log` (the dep install, which
    produces no tick at all), and when `eval.log` is live again — byte-identical — the arm is gone, so
    the changed-digest gate applies and nothing is re-asked or killed."""
    wd = _one_log_workdir(tmp_path, "eval.log", _TRAIN_TAIL)
    (wd / "setup.log").write_text(_SETUP_TAIL, encoding="utf-8")
    _mtimes(wd, ["setup.log", "eval.log"])                    # eval.log live
    plan = eval_log_plan([])                                  # the single-command shape: kill-ELIGIBLE
    client = _ScriptedClient({"status": "broken", "reason": "loss is nan", "confidence": 0.95})
    host = _Host(tmp_path, client=client)

    # ONE monitor run — the arming state is task-local, so switching the live file between separate
    # `_drive_train` calls would prove nothing (each call starts a fresh, unarmed observer).
    phase = {"n": 0, "at": 0.0}

    def _switch_then_return(h) -> bool:
        now = time.monotonic()
        if phase["n"] == 0 and h.spans("train_monitor"):
            _mtimes(wd, ["eval.log", "setup.log"])            # the dep install becomes live...
            phase.update(n=1, at=now)
        elif phase["n"] == 1 and now - phase["at"] > 0.15:
            _mtimes(wd, ["setup.log", "eval.log"])            # ...and eval.log is back, SAME bytes
            phase.update(n=2, at=now)
        elif phase["n"] == 2 and now - phase["at"] > 0.25:
            return True
        return False

    _drive_train(host, wd, plan=plan, until=_switch_then_return)

    assert host.spans("train_monitor")[0]["attributes"].get("kill_armed") is True
    assert phase["n"] == 2, "the drive never reached the switch-back phase"
    assert client.calls == 1, "the arm did not survive the switch, so nothing was re-asked"
    assert host.kill_signal == {} and not host.cancel.is_set()


def test_h1d_a_stage_list_with_unusable_rows_cannot_promote_a_survivor_to_training():
    """Dropping a row the plan cannot name would RENUMBER the pipeline: a three-stage list with two
    unusable rows would collapse to a "one-stage pipeline" — the shape that DOES carry kill authority
    — and hand it to whichever row survived. `_resolve_stages` only ever emits validated dicts, so
    this is a fail-closed guard rather than a live path, and it fails in the safe direction."""
    broken = eval_log_plan([{"name": None, "command": ["x"]}, "not a stage",
                            {"name": "train", "command": ["x"]}])
    assert broken.roles["train.log"] == ("train", LOG_ROLE_WORK)
    # ... while a genuinely one-stage pipeline is unaffected.
    assert eval_log_plan(_TRAINING_STAGES).roles["train.log"] == ("train", LOG_ROLE_TRAINING)
