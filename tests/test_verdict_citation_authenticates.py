"""THE JUDGE READ THE SOURCE, FOUND THE MECHANISM, AND WAS OVERRULED BY ARITHMETIC.

WHAT HAPPENED (`runs/e5small-dr-unified-v4` node 3, 2026-08-20/21)
------------------------------------------------------------------
The training watchdog has code-reading tools and `_LOOK_INVITATION` has told it since 2026-08-18 to
use them before attributing `fault`: *"a loss that is frozen because the objective cannot descend as
written looks exactly like one frozen because the idea does not work, and only the source says
which."*

It did exactly that. Five of node 3's sixteen `broken` verdicts cite a file and line in their prose
— `loss.py:507`, `loss.py:511-517`, `loss.py:504-507`, **`loss.py:486`** (the line declaring
`neg_inf = torch.tensor(-1e9)`), `loss.py`. That sentinel is FINITE and reaches the batch mean
through `logsumexp`, so a row whose DCL mask removes every negative contributes about `-1e9/batch`
and the contrastive objective becomes unbounded below. The loss ran 40.07 -> -2.4e7 and the judge
named the mechanism thirteen times.

**Zero of twenty-four alerts stopped anything.** Five `broken` verdicts sat at or above the 0.8 bar,
one at 0.90 with the streak already satisfied, and `trajectory_vetoes_kill` blocked them: the loss
was descending, and the veto reads descent as evidence that nothing is wrong. For an objective
bounded below that is right. For this one, descent IS the symptom.

THE GAP WAS ONE FIELD
---------------------
`TrainingVerdict` had `status`/`fault`/`reason`/`confidence` and no citation FIELD, so the reference
lived in prose and nothing re-resolved it. The engine could not tell a verdict that had opened the
file from one that had invented the line number, and so — correctly, given what it knew — it
believed the measurement over the model.

The fix is not a better threshold. `failure_diagnosis.evidence_citation_resolves` already re-reads a
model-authored locator against the node's own workdir, confined, refusing `..`/absolute/symlink-out,
and answers three ways. Making the citation a FIELD lets that run, and a citation the engine itself
re-opened is an OUT-OF-BAND fact the model did not author. Text may NOMINATE; it may never DECIDE —
and this is what the authentication looks like here.

WHAT THIS FILE PINS, AND THE ONE IT PINS HARDEST
------------------------------------------------
The veto yields to a resolved citation **on the repair path and NEVER on the kill path**. A kill
discards a multi-hour training with no repair, no retry and no refunded slot; a repair-stop costs one
restart of a run the judge has just said is wasted, with the diagnosis attached. That asymmetry is
this file's own justification and the last test is what stops it eroding.

It is also why this is not the rung `train_monitor.py` refuses permanently. That refusal prices a
THRESHOLD on the trajectory and shows no bar separates the broken n74 (peak 2.54e+08) from champion
n48 (2.53e+08). This is not a threshold, and n48's run holds ZERO `train_monitor_alert` rows, so it
cannot enter this path in either direction.
"""
from __future__ import annotations

import inspect

import pytest

from looplab.engine import train_monitor as tm
from looplab.engine.train_monitor import (LossTrajectory, TrainingVerdict, citation_authenticates,
                                          should_monitor_kill, should_monitor_repair,
                                          trajectory_vetoes_kill)


def _verdict(status="broken", fault="implementation", confidence=0.9,
             source="code", locator="vectorsearch/training/loss.py:486"):
    return TrainingVerdict(status=status, fault=fault, confidence=confidence,
                           reason="the -1e9 sentinel reaches the batch mean",
                           evidence_source=source, evidence_locator=locator)


def _descending():
    """Node 3's real shape: monotonically descending, nothing `_anomaly_of` calls anomalous."""
    t = LossTrajectory(direction="descending", windows=6, points=46,
                       first=40.0686, last=-24157694.4, minimum=-24157694.4, anomaly="")
    assert trajectory_vetoes_kill(t) is True, "the fixture must be a trajectory the veto blocks"
    return t


def _gate(verdict, *, trajectory, resolved, streak=2, threshold=0.8):
    return should_monitor_repair(verdict, enabled=True, threshold=threshold,
                                 log_role=tm.LOG_ROLE_TRAINING, broken_streak=streak,
                                 confirm_ticks=2, trajectory=trajectory,
                                 citation_resolved=resolved)


# ------------------------------------------------------------------ the motivating case
def test_a_resolved_citation_gets_past_the_veto_that_blocked_node_3():
    assert _gate(_verdict(), trajectory=_descending(), resolved=True) is True


def test_the_same_verdict_without_the_re_read_is_still_blocked():
    """The authentication is the RE-READ, not the claim. A verdict that cites a file the engine
    could not open, or cites nothing at all, is a model reading a log — which may nominate and may
    not decide, exactly as before this change."""
    for resolved in (None, False):
        assert _gate(_verdict(), trajectory=_descending(), resolved=resolved) is False
    # …and citing nothing is not the same fact as citing something absent, so both are kept.
    assert citation_authenticates(_verdict(source="none", locator=""), resolved=None) is False
    assert citation_authenticates(_verdict(), resolved=False) is False
    assert citation_authenticates(_verdict(), resolved=True) is True


def test_a_citation_cannot_rescue_a_verdict_that_blames_the_idea():
    """`hypothesis` means the code did what it was told and the IDEA failed — a real finding to be
    RECORDED, never repaired away. No file can substantiate a claim about an idea, so a citation
    must not turn one into a stop."""
    assert citation_authenticates(_verdict(fault="hypothesis"), resolved=True) is False
    assert _gate(_verdict(fault="hypothesis"), trajectory=_descending(), resolved=True) is False
    for fault in ("environment", "unknown"):
        assert _gate(_verdict(fault=fault), trajectory=_descending(), resolved=True) is False


def test_a_citation_does_not_replace_any_other_conjunct():
    """It lifts the VETO and nothing else. The confidence bar, the repeated-verdict requirement and
    the status all still hold — an authenticated citation is evidence the finding is re-derivable,
    not a licence to act on one uncertain tick."""
    traj = _descending()
    assert _gate(_verdict(confidence=0.5), trajectory=traj, resolved=True) is False   # below the bar
    assert _gate(_verdict(), trajectory=traj, resolved=True, streak=1) is False       # not repeated
    assert _gate(_verdict(status="watch"), trajectory=traj, resolved=True) is False   # not broken
    assert should_monitor_repair(_verdict(), enabled=False, threshold=0.8,
                                 log_role=tm.LOG_ROLE_TRAINING, broken_streak=2,
                                 trajectory=traj, citation_resolved=True) is False    # switched off


def test_nothing_changes_when_the_trajectory_never_vetoed():
    """`off == today` for the whole population this does not concern: where the measurement did not
    refuse, the citation is not consulted and the answer is what it always was."""
    for traj in (None, LossTrajectory(direction="flat", windows=3, points=9, anomaly="")):
        for resolved in (None, False, True):
            assert _gate(_verdict(), trajectory=traj, resolved=resolved) is True


# ------------------------------------------------------------------ THE asymmetry
def test_the_kill_path_cannot_be_reached_by_a_citation_at_all():
    """THE property this whole change rests on, and it is structural rather than conditional:
    `should_monitor_kill` takes no `citation_resolved` parameter, so no caller can hand it one.

    A kill discards a multi-hour training with no repair, no retry and no refunded slot. A
    repair-stop costs ONE restart of a run the judge has just said is wasted, with the diagnosis
    attached. The permanent refusal in this module prices the KILL and is right to; nothing here
    touches it."""
    assert "citation_resolved" not in inspect.signature(should_monitor_kill).parameters
    assert should_monitor_kill(_verdict(), enabled=True, threshold=0.8,
                               log_role=tm.LOG_ROLE_TRAINING, broken_streak=2,
                               confirm_ticks=2, trajectory=_descending()) is False
    # …and the veto's own rule is untouched: it still says "descending, not anomalous -> refuse".
    assert trajectory_vetoes_kill(_descending()) is True


def test_the_veto_still_refuses_a_kill_on_the_node_the_permanent_refusal_protects():
    """n48-shaped: a big descending loss on a run that scored 0.8835. It must remain unkillable by
    the arithmetic, which is what the refusal in this module guarantees — and the citation rung
    cannot reach it, because a champion's run carries no watchdog verdict to authenticate."""
    n48 = LossTrajectory(direction="descending", windows=20, points=140,
                         first=7.71, last=-2.53e08, minimum=-2.53e08, anomaly="")
    assert trajectory_vetoes_kill(n48) is True
    assert should_monitor_kill(_verdict(), enabled=True, threshold=0.8,
                               log_role=tm.LOG_ROLE_TRAINING, broken_streak=5,
                               confirm_ticks=2, trajectory=n48) is False


# ------------------------------------------------------------------ the schema and the prompt
def test_the_verdict_carries_a_citation_the_engine_can_re_read():
    fields = TrainingVerdict.model_fields
    assert {"evidence_source", "evidence_locator"} <= set(fields)
    # Defaults keep every historical caller and test double valid: a verdict that cites nothing is
    # exactly what the judge produced before this existed.
    bare = TrainingVerdict(status="broken", reason="r")
    assert bare.evidence_source == "none" and bare.evidence_locator == ""
    assert citation_authenticates(bare, resolved=True) is False   # `unknown` fault, and no locator


def test_the_checklist_is_spliced_only_when_the_tools_are_wired():
    """`train_monitor_tools=false` must reproduce the historical message byte for byte — the
    property that makes the whole tool rung shippable. Asked of the assembly's SOURCE because
    driving it needs a live client."""
    src = inspect.getsource(tm)
    assert '((_CITE_INVITATION + "\\n\\n") if tools is not None else "")' in src
    assert "_LOOK_INVITATION" in src and "_CITE_INVITATION" in src


def test_the_checklist_asks_for_what_the_engine_can_actually_check():
    """A prompt that asks for something unverifiable teaches the model that the field is decorative.
    It asks for a path and a line, which is exactly the shape `evidence_citation_resolves` parses."""
    text = tm._CITE_INVITATION
    assert "evidence_locator" in text and "evidence_source" in text
    assert "RE-READS" in text
    # It asks for the SHAPE `evidence_citation_resolves` parses — a workdir-relative path, a colon,
    # a line number — and asks for it in words rather than by pasting a specimen path. A specimen
    # would read to `tests/test_claim_pins.py` as a citation INTO THIS REPO of a file that lives in
    # the candidate's tree, and that guard is right: a path this repo cannot resolve is a dead
    # citation regardless of who it was written for.
    assert "path inside this run's workdir" in text
    assert "colon and the line number" in text


# ------------------------------------------------------------------ the re-read itself
def test_the_re_read_is_confined_to_the_node_workdir(tmp_path):
    """The locator is model-authored text reaching a filesystem call. `evidence_citation_resolves`
    owns the fence; this pins that the fence is the one being used."""
    from looplab.engine.failure_diagnosis import evidence_citation_resolves

    (tmp_path / "vectorsearch" / "training").mkdir(parents=True)
    (tmp_path / "vectorsearch" / "training" / "loss.py").write_text("x = 1\n", encoding="utf-8")
    ok = {"source": "code", "locator": "vectorsearch/training/loss.py:486"}
    assert evidence_citation_resolves(ok, tmp_path) is True
    assert evidence_citation_resolves({"source": "code", "locator": "no/such.py:1"}, tmp_path) is False
    for escape in ("../../etc/passwd", "/etc/passwd"):
        assert evidence_citation_resolves({"source": "code", "locator": escape}, tmp_path) is not True
    assert evidence_citation_resolves({"source": "none", "locator": ""}, tmp_path) is None


def test_the_engine_re_reads_rather_than_trusting_the_verdict(tmp_path):
    """End to end over the two functions the live path composes, on a workdir where the cited file
    exists and one where it does not — the same verdict, opposite outcomes."""
    from looplab.engine.failure_diagnosis import evidence_citation_resolves

    (tmp_path / "vectorsearch" / "training").mkdir(parents=True)
    (tmp_path / "vectorsearch" / "training" / "loss.py").write_text(
        "neg_inf = torch.tensor(-1e9)\n", encoding="utf-8")
    empty = tmp_path / "empty"
    empty.mkdir()

    v = _verdict()
    cite = {"source": v.evidence_source, "locator": v.evidence_locator}
    assert _gate(v, trajectory=_descending(),
                 resolved=evidence_citation_resolves(cite, tmp_path)) is True
    assert _gate(v, trajectory=_descending(),
                 resolved=evidence_citation_resolves(cite, empty)) is False

# ------------------------------------------------------------------ the arming counterfactual
def test_a_citation_bearing_verdict_arms_the_prompt_re_look():
    """`_confirmation_would_act` asks "would a REPEAT of this verdict act", and every input the real
    gate reads has to be the one it will read — its own docstring's phrase is "cannot drift from
    them". Adding `citation_resolved` to the gate and not to the counterfactual re-introduced
    exactly that drift: a first `broken` tick carrying a RESOLVED citation would act on its repeat,
    the counterfactual said it would not, so the monitor did not arm and the second look waited a
    full cadence — up to THIRTY MINUTES — instead of `_MONITOR_CONFIRM_DELAY_S`. On a node burning
    ~4 GPU-hours per attempt that is the entire value of arming, lost silently."""
    traj = _descending()
    armed = tm._confirmation_would_act(
        _verdict(), enabled=True, threshold=0.8, log_role=tm.LOG_ROLE_TRAINING,
        trajectory=traj, confirm_ticks=2, citation_resolved=True)
    assert armed is True
    # …and without the re-read it still does not arm, because the repeat still would not act.
    assert tm._confirmation_would_act(
        _verdict(), enabled=True, threshold=0.8, log_role=tm.LOG_ROLE_TRAINING,
        trajectory=traj, confirm_ticks=2, citation_resolved=None) is False


def test_the_counterfactual_inherits_the_asymmetry_rather_than_restating_it():
    """It hands the citation to the REPAIR predicate and cannot hand it to the KILL one, because
    that function takes no such parameter. The asymmetry is inherited from the gates instead of
    being re-listed at the arming site, which is the whole reason this function exists."""
    import inspect

    src = inspect.getsource(tm._confirmation_would_act)
    assert "citation_resolved=citation_resolved" in src
    kill_call = src[src.index("should_monitor_kill("):src.index("or should_monitor_repair")]
    assert "citation_resolved" not in kill_call
