"""The training watchdog's kill path was UNREACHABLE for every pipeline this engine actually runs.

THE MEASUREMENT. `e5small-dr-unified-v2` resolves the pipeline `mine -> train -> score`. Only a log
the plan can PROVE is the run's own training carries kill authority (`eval_log_plan`), and a
multi-stage pipeline proves nothing about which of its stages is the training loop, so every stage
of it is `LOG_ROLE_WORK` — judged, recorded, narrated, advisory. That rule is right about the danger
it names (a `data_prep` log printing a flat `loss: 0.6931` drew `broken` at 0.9 and armed the gate)
and wrong about its reach: on that run NO stage of ANY node was ever kill-eligible, so the early
stop could not fire once in 7.3 hours of the following, taken verbatim from the run's alert rows:

    node 2, 31 x broken at confidence 0.85-0.95, e.g.
      "Loss descended to ~5.04 then jumped to 8.8534 and stayed perfectly flat (iqr=0,
       min=max=8.8534) for the final 4+ hours of training - model stopped learning, run wasted"
      "...grad_norm collapsed to ~0.0002..."
    -> ran all 57,600 steps and scored RECALL@100 = 0.000000.

The judge was right every tick and held no authority any tick, and nothing in the record said so:
the rows read as ordinary `broken` verdicts, indistinguishable from ones the gate had simply not
confirmed yet.

THE FIX, in two halves that must hold together:
  D-1 a manifest may DECLARE which stage is the training loop (`role: "training"`). It is the one
      lever that can only ever move a stage TOWARD liability — a kill has no repair, no retry and no
      refunded slot — so no declarer profits by it, and a manifest that says nothing keeps exactly
      the advisory role it has today.
  D-2 that authority is SPENT the moment the stage's declared artifact exists. This run's `train`
      stage also scores in-process (`RECALL@100: 0.793344` is a line in its `train.log`), which is
      the H-1 defect — kill authority reading scorer output — moved inside one stage where no plan
      can split it by filename. `expect.files` is the manifest's own output contract, so "the
      training already finished" is an exact filesystem fact rather than a reading of the text.
  D-3 and when the role is what refused an otherwise-complete kill, the alert row says so.
"""
from __future__ import annotations

import pytest

from looplab.engine.train_monitor import (
    MONITOR_REPAIR_REASON,
    LOG_ROLE_SCORE,
    LOG_ROLE_SETUP,
    LOG_ROLE_TRAINING,
    LOG_ROLE_WORK,
    TrainingVerdict,
    eval_log_plan,
    resolve_stage_log,
    should_monitor_kill,
    should_monitor_repair,
    training_authority_spent,
)
from looplab.events.types import EV_TRAIN_MONITOR_ALERT
from looplab.runtime.command_eval import STAGE_ROLE_TRAINING, validate_stages
from tests.test_watchdog_stage_scope import (  # noqa: F401 — the harness is the production path
    _Host,
    _ScriptedClient,
    _drive_train,
    _one_log_workdir,
)

# The run's real pipeline. `train` is where the loss lives and where the collapse happened.
_V2_STAGES = [
    {"name": "mine", "command": ["python", "-m", "vectorsearch.data.mine_negatives"]},
    {"name": "train", "command": ["python", "-m", "vectorsearch.train", "%params%"],
     "expect": {"files": ["vectorsearch/experiments/x/final/model.safetensors"]}},
    {"name": "score", "command": ["python", "-m", "vectorsearch.test"]},
]


def _declared(stages, name="train"):
    """The same list with `name` declaring itself the training loop — through the REAL validator, so
    a shape these tests accept is one the manifest handshake accepts."""
    out = [dict(s) for s in stages]
    for s in out:
        if s["name"] == name:
            s["role"] = STAGE_ROLE_TRAINING
    clean, err = validate_stages(out)
    assert err is None, err
    return clean


# The collapsed tail, trimmed from `runs/e5small-dr-unified-v2/nodes/node_2/train.log`.
_COLLAPSED_TAIL = "".join(
    f"step {57000 + i}/57600 loss: 8.8534 grad_norm: 0.0002 lr: 1.4e-07\n" for i in range(12))
# What node 1 was doing at the same time: descending, and never to be killed.
_DESCENDING_TAIL = "".join(
    f"step {i * 400}/57600 loss: {138.0 - i * 2.4:.4f} grad_norm: 0.51 lr: 2.0e-05\n"
    for i in range(12))

def _drive_growing(host, wd, plan, chunks, *, rows_needed):
    """Drive the monitor against a log that GROWS between ticks — which is what the real one did,
    and what the changed-digest gate requires before a second verdict is even asked for. A static
    fixture can only ever produce one judged tick on an advisory stage, so a streak (and therefore
    both the veto and the withheld-role counterfactual) is unreachable without this."""
    log = wd / "train.log"
    state = {"i": 0, "rows": 0}

    def _until(h):
        rows = len(h.store.rows(EV_TRAIN_MONITOR_ALERT))
        if rows > state["rows"] and state["i"] < len(chunks):
            state["rows"] = rows
            with log.open("a", encoding="utf-8") as fh:
                fh.write(chunks[state["i"]])
            state["i"] += 1
        return rows >= rows_needed or bool(h.kill_signal)

    _drive_train(host, wd, plan=plan, until=_until)


def _descending_chunks(n):
    """Successive slices of a curve that keeps going down, one per tick."""
    return ["".join(f"step {(k * 12 + i) * 400}/57600 "
                    f"loss: {104.0 - k * 8.0 - i * 0.5:.4f} grad_norm: 0.51 lr: 2.0e-05\n"
                    for i in range(12)) for k in range(1, n + 1)]


_COLLAPSED = {"status": "broken", "confidence": 0.9,
              "reason": "Loss frozen at exactly 8.8534 (IQR=0) for 4+ hours / ~46k steps after "
                        "rising from its 5.04 min, grad_norm collapsed to ~4e-4"}


# ------------------------------------------------------------------ D-1: the declaration is a fact
def test_d1_the_measured_run_could_not_be_stopped_and_now_can(tmp_path):
    """THE REPRODUCTION, both sides. The same pipeline, the same tail, the same verdict — the only
    difference is one key in the manifest."""
    before = eval_log_plan(_V2_STAGES)
    assert before.roles["train.log"] == ("train", LOG_ROLE_WORK)
    assert should_monitor_kill(TrainingVerdict(**_COLLAPSED), enabled=True, threshold=0.8,
                               log_role=before.roles["train.log"][1], broken_streak=31) is False

    after = eval_log_plan(_declared(_V2_STAGES))
    assert after.roles["train.log"] == ("train", LOG_ROLE_TRAINING)
    assert should_monitor_kill(TrainingVerdict(**_COLLAPSED), enabled=True, threshold=0.8,
                               log_role=after.roles["train.log"][1], broken_streak=31) is True


def test_d1_the_declared_stage_actually_ends_the_node(tmp_path):
    """End to end through the real monitor loop: the collapsed tail, judged live, stops the node."""
    wd = _one_log_workdir(tmp_path, "train.log", _COLLAPSED_TAIL)
    plan = eval_log_plan(_declared(_V2_STAGES))
    host = _Host(tmp_path, client=_ScriptedClient(_COLLAPSED), asha_kill=False)
    _drive_train(host, wd, plan=plan, until=lambda h: bool(h.kill_signal))

    assert host.kill_signal.get("kill") is True
    assert host.kill_signal.get("terminal_reason") == "monitor_broken"
    row = host.store.rows(EV_TRAIN_MONITOR_ALERT)[-1]
    assert row["log_role"] == LOG_ROLE_TRAINING and row["stop_decided"] is True


def test_d1_an_undeclared_pipeline_is_byte_for_byte_what_it_was(tmp_path):
    """The whole safety argument rests on this: declaring nothing changes nothing."""
    plan = eval_log_plan(_V2_STAGES)
    assert plan.roles == eval_log_plan([dict(s) for s in _V2_STAGES]).roles
    assert plan.training_artifacts == ()

    wd = _one_log_workdir(tmp_path, "train.log", _COLLAPSED_TAIL)
    host = _Host(tmp_path, client=_ScriptedClient(_COLLAPSED), asha_kill=False)
    _drive_train(host, wd, plan=plan,
                 until=lambda h: len(h.store.rows(EV_TRAIN_MONITOR_ALERT)) >= 2)
    assert host.kill_signal == {} and not host.cancel.is_set()
    rows = host.store.rows(EV_TRAIN_MONITOR_ALERT)
    assert rows and all(r["status"] == "broken" and "stop_decided" not in r for r in rows)


def test_d1_a_descending_curve_is_never_killed_even_when_declared(tmp_path):
    """v7 node 1's false `broken`: a judge called a genuinely descending run "pinned ... showing no
    learning trend". The declaration buys authority, never the last word — the engine's own measured
    trajectory still vetoes."""
    wd = _one_log_workdir(tmp_path, "train.log", _DESCENDING_TAIL)
    plan = eval_log_plan(_declared(_V2_STAGES))
    host = _Host(tmp_path, asha_kill=False, client=_ScriptedClient(
        {"status": "broken", "confidence": 0.95,
         "reason": "loss pinned at ~110, showing no learning trend from its initialization value"}))
    _drive_growing(host, wd, plan, _descending_chunks(4), rows_needed=4)

    assert host.kill_signal == {} and not host.cancel.is_set()
    rows = host.store.rows(EV_TRAIN_MONITOR_ALERT)
    assert any(r.get("trajectory_veto") for r in rows)
    # NON-VACUITY first: `all(...)` over an empty sequence is True, so without this the assertion
    # below passes on a drive that produced no rows at all — "the watchdog decided nothing"
    # spelled identically to "the watchdog never ran".
    assert rows, "no monitor rows were produced; this negative control would be true of nothing"
    assert all("stop_decided" not in r for r in rows)


def test_d1_the_scorer_can_never_buy_the_role(tmp_path):
    """Position beats declaration. An operator pipeline owns its scorer's NAME, so the check that
    matters is the positional one — a `role` key on the last stage must not undo it."""
    stages = [{"name": "train", "command": ["python", "t.py"]},
              {"name": "measure", "command": ["python", "m.py"], "role": STAGE_ROLE_TRAINING}]
    clean, err = validate_stages(stages)
    assert err is None
    plan = eval_log_plan(clean)
    assert plan.roles["measure.log"] == ("measure", LOG_ROLE_SCORE)
    assert plan.roles["train.log"] == ("train", LOG_ROLE_WORK)   # it declared nothing
    assert plan.training_artifacts == ()


def test_d1_the_manifest_may_name_exactly_one_training_stage():
    """Two declarations is the old rule ("everything that is not `score` is training") wearing a
    declaration — the rule that let a `data_prep` log hold a gun."""
    clean, err = validate_stages([
        {"name": "a", "command": ["x"], "role": STAGE_ROLE_TRAINING},
        {"name": "b", "command": ["x"], "role": STAGE_ROLE_TRAINING},
        {"name": "score", "command": ["x"]}])
    assert clean is None and "exactly one stage" in err


@pytest.mark.parametrize("role", ["train", "TRAINING ", "score", "work", "", True, 1, ["training"]])
def test_d1_an_unusable_role_is_refused_not_dropped(role):
    """Refused, never silently ignored: a manifest that reads as if a stage carries a role nothing
    applies is the failure the closed key sets exist to end. `TRAINING ` is accepted — case and
    surrounding space are not a different declaration."""
    clean, err = validate_stages([{"name": "train", "command": ["x"], "role": role},
                                  {"name": "score", "command": ["x"]}])
    if str(role).strip().lower() == STAGE_ROLE_TRAINING:
        assert err is None and clean[0]["role"] == STAGE_ROLE_TRAINING
    else:
        assert clean is None and "`role`" in err


def test_d1_the_two_vocabularies_agree():
    """The manifest key and the durable log role are separate contracts with separate readers; a
    silent drift between them would make every declaration a no-op that still validates."""
    assert STAGE_ROLE_TRAINING == LOG_ROLE_TRAINING


# ------------------------------------------- D-2: the authority ends when the training does
def test_d2_the_authority_is_spent_once_the_declared_artifact_exists(tmp_path):
    """The in-stage scorer, which is what this run really does. Same stage, same declaration — but
    the checkpoint it promised is on disk, so the tail below it is no longer a training the kill
    could save."""
    wd = _one_log_workdir(tmp_path, "train.log", _COLLAPSED_TAIL)
    plan = eval_log_plan(_declared(_V2_STAGES))
    assert plan.training_artifacts == ("vectorsearch/experiments/x/final/model.safetensors",)
    assert training_authority_spent(wd, plan) is False
    assert resolve_stage_log(wd, plan).role == LOG_ROLE_TRAINING

    host = _Host(tmp_path, client=_ScriptedClient(_COLLAPSED), asha_kill=False)
    artifact = wd / plan.training_artifacts[0]
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"a finished checkpoint")
    assert training_authority_spent(wd, plan) is True

    _drive_train(host, wd, plan=plan,
                 until=lambda h: len(h.store.rows(EV_TRAIN_MONITOR_ALERT)) >= 2)
    assert host.kill_signal == {} and not host.cancel.is_set()
    row = host.store.rows(EV_TRAIN_MONITOR_ALERT)[-1]
    assert row["log_role"] == LOG_ROLE_WORK           # judged and recorded, just not armed
    assert row["status"] == "broken" and "stop_decided" not in row


def test_d2_a_role_not_bought_by_a_declaration_can_never_be_spent(tmp_path):
    """The single-command and one-stage evals keep their authority unconditionally — they never
    declared anything, so there is no promise whose arrival could end it."""
    for stages in ([], [{"name": "train", "command": ["x"]}]):
        plan = eval_log_plan(stages)
        assert plan.training_artifacts == ()
        assert training_authority_spent(tmp_path, plan) is False
    assert training_authority_spent(tmp_path, None) is False


def test_d2_a_declaration_with_nothing_to_spend_never_gets_the_gun(tmp_path):
    """The half D-2 left open: an authority with NO spend condition outlives its training.

    `training_authority_spent` is the entire price of admitting a declaration — the role ends the
    moment the stage's own promised artifact exists, because v2's `train` stage also scores
    in-process and a stage that does not distinguish its phases cannot be taken at its word about
    them. A `role: "training"` with no `expect.files` has nothing to observe, so the authority could
    never be handed back: it would hold the gun over the in-process scoring phase, which is H-1 with
    extra steps. `validate_stages` accepts such a manifest (the stage still runs exactly as
    declared); `eval_log_plan` refuses it the ROLE, which is the advisory behaviour it had before it
    declared anything — and a plan with no `LOG_ROLE_TRAINING` in it is the one the span already
    reports as `kill_reachable: false` from its first tick.
    """
    bare = [dict(stage) for stage in _V2_STAGES]
    for stage in bare:
        stage.pop("expect", None)
        if stage["name"] == "train":
            stage["role"] = STAGE_ROLE_TRAINING
    clean, err = validate_stages(bare)
    assert err is None, "the manifest itself is valid — only the AUTHORITY is refused"

    plan = eval_log_plan(clean)
    assert plan.roles["train.log"] == ("train", LOG_ROLE_WORK)
    assert plan.training_artifacts == ()
    assert training_authority_spent(tmp_path, plan) is False    # nothing to spend, so never spent
    # ...and with nothing to spend it, the kill it would otherwise have bought is refused.
    broken = TrainingVerdict(status="broken", reason="loss frozen at 8.8534", confidence=0.95)
    assert should_monitor_kill(broken, enabled=True, threshold=0.8,
                               log_role=plan.roles["train.log"][1], broken_streak=31) is False
    # The control: the SAME manifest with its output contract restored does buy the role.
    assert eval_log_plan(_declared(_V2_STAGES)).roles["train.log"] == ("train", LOG_ROLE_TRAINING)


def test_d2_an_unreadable_workdir_hands_the_gun_back(tmp_path, monkeypatch):
    """Fail-closed: what cannot be checked counts as spent, never as live. A node workdir is a FUSE
    mount, where a stat can fail for reasons that have nothing to do with the file."""
    plan = eval_log_plan(_declared(_V2_STAGES))
    assert training_authority_spent(tmp_path / "does-not-exist", plan) is False

    from pathlib import Path as _P
    for boom in (OSError("stale file handle"), ValueError("embedded null byte")):
        monkeypatch.setattr(_P, "exists", lambda self, _e=boom: (_ for _ in ()).throw(_e))
        assert training_authority_spent(tmp_path, plan) is True


def test_d3_an_unstoppable_pipeline_is_readable_from_the_first_tick(tmp_path):
    """The role gate is a property of the resolved PIPELINE, so "nothing here can be stopped" is
    knowable before any verdict exists — and it is what stayed invisible for three runs."""
    wd = _one_log_workdir(tmp_path, "train.log", _COLLAPSED_TAIL)
    host = _Host(tmp_path, client=_ScriptedClient(_COLLAPSED), asha_kill=False)
    _drive_train(host, wd, plan=eval_log_plan(_V2_STAGES),
                 until=lambda h: len(h.store.rows(EV_TRAIN_MONITOR_ALERT)) >= 1)
    assert host.spans("train_monitor")[0]["attributes"]["kill_reachable"] is False

    other = tmp_path / "declared"          # a fresh span file: `_Host` writes one per directory
    other.mkdir()
    host2 = _Host(other, client=_ScriptedClient(_COLLAPSED), asha_kill=False)
    _drive_train(host2, wd, plan=eval_log_plan(_declared(_V2_STAGES)),
                 until=lambda h: bool(h.kill_signal))
    assert host2.spans("train_monitor"), (
        "no train_monitor span was opened; `all(...)` over that empty list is vacuously True")
    assert all("kill_reachable" not in s["attributes"] for s in host2.spans("train_monitor"))


# ------------------------------------------------------- D-3: an unactionable verdict says so
def test_d3_the_row_records_that_the_role_is_what_refused(tmp_path):
    """31 rows in 7.3 hours could not be told apart from a gate that was merely still confirming."""
    wd = _one_log_workdir(tmp_path, "train.log", _COLLAPSED_TAIL)
    plan = eval_log_plan(_V2_STAGES)                       # undeclared: advisory
    host = _Host(tmp_path, client=_ScriptedClient(_COLLAPSED), asha_kill=False)
    _drive_growing(host, wd, plan, [_COLLAPSED_TAIL] * 4, rows_needed=3)

    rows = host.store.rows(EV_TRAIN_MONITOR_ALERT)
    assert host.kill_signal == {}                      # still advisory, that is the point
    assert rows[-1]["kill_role_withheld"] == LOG_ROLE_WORK
    span = host.spans("train_monitor")[-1]["attributes"]
    assert span.get("kill_role_withheld") == LOG_ROLE_WORK


def test_d3_a_verdict_that_would_not_have_killed_anyway_claims_nothing(tmp_path):
    """The counterfactual is about the GATE, not a second opinion about the run: a `watch` verdict,
    or one below the confidence bar, leaves the field absent."""
    wd = _one_log_workdir(tmp_path, "train.log", _COLLAPSED_TAIL)
    plan = eval_log_plan(_V2_STAGES)
    host = _Host(tmp_path, asha_kill=False, client=_ScriptedClient(
        {"status": "watch", "confidence": 0.99, "reason": "slow but descending"}))
    _drive_growing(host, wd, plan, [_COLLAPSED_TAIL] * 4, rows_needed=3)
    assert host.store.rows(EV_TRAIN_MONITOR_ALERT), (
        "no alert rows at all; this negative control needs something to be negative ABOUT")
    assert all("kill_role_withheld" not in r for r in host.store.rows(EV_TRAIN_MONITOR_ALERT))


# ==================================================================== F: bug or hypothesis?
#
# The role gate answered "may this stage be stopped". It could not answer the question underneath:
# stopped AS WHAT. `monitor_broken` is not in `FAILURE_REASONS`, so a kill was terminal — no repair,
# no retry — while the deterministic diverge watchdog one layer down fails a stage with `diverged`,
# which IS repairable and on this very run bought node 5 a Developer repair. Same illness, two
# outcomes: a NON-finite loss read as "probably a bug, go fix it", a loss frozen at a perfectly
# finite 8.8534 read as "the idea failed". A collapse is at least as often a wrong reduction, a
# wrong axis, an unshuffled loader or a schedule bug as it is a refuted hypothesis — and a verdict
# recorded against an idea that was never actually tested teaches the search the wrong lesson.
#
# So the verdict now carries WHOSE fault it is, and the two faults get opposite treatments.

_BUG = {"status": "broken", "confidence": 0.9, "fault": "implementation",
        "reason": "the log echoes normalize=False on the query tower while the loss is cosine — "
                  "every embedding lands on a different scale and the loss cannot descend"}
_IDEA = {"status": "broken", "confidence": 0.9, "fault": "hypothesis",
         "reason": "the recipe is applied exactly as written and this backbone simply collapses "
                   "under it — the loss is frozen and there is nothing in the code to change"}


def test_f_a_named_bug_stops_for_repair_on_any_judged_stage(tmp_path):
    """THE UNIVERSAL HALF. `mine` is not the training loop and never will be, but the code can be
    just as wrong about it — and a five-stage pipeline has four more like it. What made the role
    gate necessary was that the only available action was terminal; a repair-stop costs one restart
    of a run the judge has already called wasted, so it is admissible everywhere the judge reads."""
    for role in (LOG_ROLE_WORK, LOG_ROLE_TRAINING):
        assert should_monitor_repair(TrainingVerdict(**_BUG), enabled=True, threshold=0.8,
                                     log_role=role, broken_streak=2) is True
    # ...and it is what a KILL would have refused on the non-training stage.
    assert should_monitor_kill(TrainingVerdict(**_BUG), enabled=True, threshold=0.8,
                               log_role=LOG_ROLE_WORK, broken_streak=2) is False


def test_f_a_refuted_idea_is_never_repaired_away(tmp_path):
    """The other direction, and the one that protects the RECORD: a sound implementation of a bad
    idea is a real finding. It terminates (where the role allows) and is never handed back to be
    'fixed' into a different experiment."""
    assert should_monitor_repair(TrainingVerdict(**_IDEA), enabled=True, threshold=0.8,
                                 log_role=LOG_ROLE_TRAINING, broken_streak=9) is False
    assert should_monitor_kill(TrainingVerdict(**_IDEA), enabled=True, threshold=0.8,
                               log_role=LOG_ROLE_TRAINING, broken_streak=2) is True


@pytest.mark.parametrize("fault", ["unknown", "environment", "hypothesis"])
def test_f_only_a_named_bug_buys_a_repair(fault):
    """`unknown` is the schema's stated safe answer, and it must BE safe: anything the judge will
    not attribute to the code leaves the repair path shut."""
    verdict = TrainingVerdict(status="broken", reason="x", confidence=0.99, fault=fault)
    assert should_monitor_repair(verdict, enabled=True, threshold=0.8,
                                 log_role=LOG_ROLE_TRAINING, broken_streak=9) is False


def test_f_a_repair_stop_keeps_every_arithmetic_conjunct_the_kill_has():
    """Cheaper to be wrong is not free to be wrong. The confidence bar, the repeated verdict and the
    measured-trajectory veto are the same — only the role gate differs, and only because the cost
    it is proportioned to is different."""
    bug = TrainingVerdict(**_BUG)
    assert should_monitor_repair(bug, enabled=False, threshold=0.8,
                                 log_role=LOG_ROLE_WORK, broken_streak=9) is False
    assert should_monitor_repair(bug, enabled=True, threshold=0.95,
                                 log_role=LOG_ROLE_WORK, broken_streak=9) is False
    assert should_monitor_repair(bug, enabled=True, threshold=0.8,
                                 log_role=LOG_ROLE_WORK, broken_streak=1) is False
    for role in (LOG_ROLE_SCORE, LOG_ROLE_SETUP):
        assert should_monitor_repair(bug, enabled=True, threshold=0.8,
                                     log_role=role, broken_streak=9) is False


def test_f_the_repair_reason_is_repairable_and_not_the_diverge_word():
    """It has to be its own word. `diverged` means a NON-finite loss and the opposite directive
    ('stabilise the numerics'); this one means a finite loss that stopped descending. The repo has
    already paid for that conflation once — three repair rounds halving a batch size at ~3
    GPU-minutes each while the real instability went untouched."""
    from looplab.core.models import FAILURE_REASONS
    from looplab.core.config import Settings

    assert MONITOR_REPAIR_REASON == "not_learning"
    assert MONITOR_REPAIR_REASON in FAILURE_REASONS         # therefore repairable by default
    assert MONITOR_REPAIR_REASON != "diverged"
    assert MONITOR_REPAIR_REASON in Settings().inline_repair_reasons
    # ...and an operator who narrows the setting gets the same answer here as for any other class.
    assert MONITOR_REPAIR_REASON not in Settings(inline_repair_reasons=("crash",)).inline_repair_reasons


def test_f_the_bug_stop_actually_names_the_repairable_terminal(tmp_path):
    """End to end through the real loop: the claim the watchdog files is what `_evaluate` reads to
    decide between a terminal and a repair, so the reason must arrive on the signal itself."""
    wd = _one_log_workdir(tmp_path, "train.log", _COLLAPSED_TAIL)
    host = _Host(tmp_path, client=_ScriptedClient(_BUG), asha_kill=False)
    _drive_growing(host, wd, eval_log_plan(_V2_STAGES), [_COLLAPSED_TAIL] * 4, rows_needed=4)

    assert host.kill_signal.get("kill") is True             # stopped, on a WORK stage
    assert host.kill_signal.get("terminal_reason") == MONITOR_REPAIR_REASON
    row = host.store.rows(EV_TRAIN_MONITOR_ALERT)[-1]
    assert row["fault"] == "implementation" and row["repair_decided"] is True
    assert row["log_role"] == LOG_ROLE_WORK                 # ...which a kill could never have used


def test_f_the_attribution_is_recorded_even_when_it_leads_nowhere(tmp_path):
    """"The code is wrong" and "the idea is wrong" are what the search must tell apart afterwards.
    A run that recorded only the second learned the wrong lesson from every bug — so the fault lands
    on the durable row whether or not the gate acted on it."""
    wd = _one_log_workdir(tmp_path, "train.log", _COLLAPSED_TAIL)
    host = _Host(tmp_path, client=_ScriptedClient(_IDEA), asha_kill=False)
    _drive_growing(host, wd, eval_log_plan(_V2_STAGES), [_COLLAPSED_TAIL] * 4, rows_needed=3)

    rows = host.store.rows(EV_TRAIN_MONITOR_ALERT)
    assert host.kill_signal == {}                           # a WORK stage, an idea: nothing acts
    assert rows[-1]["fault"] == "hypothesis"
    assert "repair_decided" not in rows[-1]
    assert rows[-1]["kill_role_withheld"] == LOG_ROLE_WORK


def test_f_repair_wins_over_the_terminal_kill_on_the_training_stage(tmp_path):
    """Precedence, on the one stage where both are available. A bug the judge can point at is a
    thing to fix; only an implementation it will NOT blame reaches the gun."""
    wd = _one_log_workdir(tmp_path, "train.log", _COLLAPSED_TAIL)
    host = _Host(tmp_path, client=_ScriptedClient(_BUG), asha_kill=False)
    _drive_growing(host, wd, eval_log_plan(_declared(_V2_STAGES)),
                   [_COLLAPSED_TAIL] * 4, rows_needed=4)
    assert host.kill_signal.get("terminal_reason") == MONITOR_REPAIR_REASON

    other = tmp_path / "idea"
    other.mkdir()
    wd2 = _one_log_workdir(other, "train.log", _COLLAPSED_TAIL, dirname="node_1")
    host2 = _Host(other, client=_ScriptedClient(_IDEA), asha_kill=False)
    _drive_growing(host2, wd2, eval_log_plan(_declared(_V2_STAGES)),
                   [_COLLAPSED_TAIL] * 4, rows_needed=4)
    assert host2.kill_signal.get("terminal_reason") == "monitor_broken"


def test_f_a_repairable_stop_does_not_terminalize_in_the_kill_branch():
    """THE PLUMBING, pinned on control flow rather than on text.

    `_evaluate`'s watchdog branch used to `return` unconditionally after appending `node_failed`,
    which is why a kill could never become a repair however it was classified: the inline-repair
    gate lives further down the same loop and the early return jumped over it. The property is a
    fact about the branch — a reason the operator's `inline_repair_reasons` selects must FALL
    THROUGH, and only an unselected one may take the terminal — so it is read off the tree.

    It is rung 3 of the ladder, and rung 3's blind spot is that it proves the gate is SHAPED right,
    never that it is REACHED — an unconditional `return` spliced in ahead of it, or `and False` on
    its test, leaves every assertion below green. This docstring used to price the driven version at
    "a whole engine, a sandbox and a real subprocess kill for one boolean" and that was wrong: the
    three tests directly below drive the real `_evaluate` with a stubbed sibling watchdog and a
    scripted `RunResult`, no sandbox and no subprocess. Keep BOTH — this one names the shape a
    reader has to preserve, those name the behaviour.
    """
    import ast

    from _source_scan import function_tree
    from looplab.engine.evaluate import EvaluateMixin

    tree = function_tree(EvaluateMixin._evaluate)
    branches = [node for node in ast.walk(tree)
                if isinstance(node, ast.If) and "kill_signal.get('kill')" in ast.unparse(node.test)]
    assert len(branches) == 1, "the watchdog-kill branch is no longer unique; re-derive this test"
    branch = branches[0]

    gates = [node for node in branch.body
             if isinstance(node, ast.If) and "_inline_repair_reasons" in ast.unparse(node.test)]
    assert len(gates) == 1, (
        "the watchdog branch must ASK whether the reason is repairable before terminalizing; "
        f"found {len(gates)} such gates in {ast.unparse(branch)[:400]}")
    gate = gates[0]

    # The repairable side falls through: no return, and no terminal event appended.
    repairable = ast.unparse(ast.Module(body=gate.body, type_ignores=[]))
    assert "return" not in repairable, "a repairable watchdog stop must not end the node here"
    assert "EV_NODE_FAILED" not in repairable, (
        "...and must not write the node's terminal either — the attempt loop owns it (invariant #2)")
    # The unselected side still terminalizes exactly as before.
    unrepairable = ast.unparse(ast.Module(body=gate.orelse, type_ignores=[]))
    assert "EV_NODE_FAILED" in unrepairable and "return" in unrepairable

    # ...and the reason that reaches the repair loop is the WATCHDOG's, not the exit code's.
    assigns = [node for node in ast.walk(tree)
               if isinstance(node, ast.Assign)
               and any(getattr(t, "id", None) == "reason" for t in node.targets)
               and "_failure_reason" in ast.unparse(node.value)]
    assert assigns and all(isinstance(a.value, ast.BoolOp) and isinstance(a.value.op, ast.Or)
                           and "watchdog_reason" in ast.unparse(a.value.values[0])
                           for a in assigns), (
        "a tree-killed process exits -9 with no traceback, which `_failure_reason` reads as "
        "oom/crash — the watchdog's own reason must win when it stopped this attempt")


# --------------------------------------------------------------- ...and the same join, DRIVEN
#
# The AST test above is rung 3, and rung 3 has a documented blind spot this exact branch sits in:
# it proves the gate is SHAPED right, never that it is REACHED. Both of the mutations that
# reinstate the original defect leave every assertion in it green — an unconditional `return`
# inserted into `branch.body` ahead of the gate, and `if reason in self._inline_repair_reasons and
# False:`. Nothing else in the suite covered the join: every `not_learning` / `monitor_broken` test
# drives `_monitor_training`, which ends at the kill SIGNAL, and every repair test drives a node
# that failed on its own exit code. The one thing neither side exercises is the hand-off between
# them, which is the whole feature.
#
# So it is driven here, and the "whole engine, a sandbox and a real subprocess kill" the AST test
# priced it at is not needed: the watchdog is a stubbed sibling task that CLAIMS the shared signal
# exactly as `_monitor_training` does (through `claim_watchdog_kill`, the production function), and
# the eval is a scripted `RunResult` — a tree-killed `-9` with no traceback, which is what a real
# watchdog kill leaves behind and which `_failure_reason` would read as `oom`/`crash`. Everything
# between the signal and the terminal is the real `_evaluate`.
def _drive_watchdog_stop(tmp_path, terminal_reason, **kw):
    """Stop the FIRST attempt from the watchdog, then let the retry succeed. Returns the log.

    Imported locally: the predicate tests above are pure and must stay importable without the
    engine, and this is the one case in the file that needs a run.
    """
    import anyio
    from pathlib import Path

    from looplab.adapters.toytask import ToyTask
    from looplab.core.models import Idea
    from looplab.engine.orchestrator import Engine
    from looplab.engine.train_monitor import claim_watchdog_kill
    from looplab.events.eventstore import EventStore
    from looplab.runtime.sandbox import RunResult, SubprocessSandbox
    from looplab.search.policy import GreedyTree

    src = "def solve(x=1.0, y=1.0):\n    return (x-3)**2\n"

    class _Dev:
        def __init__(self):
            self.repairs = 0

        def implement(self, idea):
            return src

        def repair(self, idea, code, error):
            self.repairs += 1
            self.last_error = error
            return src + f"# fix {self.repairs}\n"

    class _Judge:
        def propose(self, state, parent):
            return Idea(operator="x", params={"x": 1.0, "y": 1.0})

        def triage_crash(self, node, error, attempt, **_kw):
            return {"action": "repair", "rationale": "the objective cannot descend as written"}

    dev, run_dir = _Dev(), tmp_path / "run"
    task_file = Path(__file__).resolve().parents[1] / "examples" / "toy_task.json"
    eng = Engine(run_dir, task=ToyTask.load(task_file), researcher=_Judge(), developer=dev,
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=1),
                 auto_install_deps=False, inline_repair=True, **kw)
    eng.store.append("run_started",
                     {"run_id": "r", "task_id": "t", "goal": "g", "direction": "min"})
    eng.store.append("node_created", {
        "node_id": 0, "parent_ids": [], "operator": "draft",
        "idea": {"operator": "draft", "params": {"x": 1.0, "y": 1.0}, "rationale": "seed"},
        "code": src})

    # The command-eval shape, because that is the only path the watchdogs are started on. The eval
    # itself is scripted, so no sandbox, no subprocess and no stage log are involved.
    eng._train_monitor = True
    eng._eval_spec = {"cmd": ["true"], "metric": {"key": "m"}}
    eng._resolved_stages = lambda *a, **k: []
    attempts, stopped = {"n": 0}, {"done": False}

    async def _watchdog(node_id, generation, workdir, cancel, ctx, kill_signal, snap, plan):
        # ONE stop, on the first attempt only — counted HERE rather than off the eval, because the
        # watchdog is a sibling task and may reach this line before the eval has started. A watchdog
        # that killed every attempt would prove only that the loop spins.
        if not stopped["done"]:
            stopped["done"] = True
            claim_watchdog_kill(kill_signal, cancel, reason="loss frozen at exactly 8.8534",
                                terminal_reason=terminal_reason, confidence=0.9)

    def _eval(node, workdir, env=None, profile=None, cancel=None, start_stage=None):
        attempts["n"] += 1
        if attempts["n"] == 1:       # tree-killed: -9, no traceback, no metric
            return RunResult(exit_code=-9, stdout="", stderr="", metric=None, timed_out=False)
        return RunResult(exit_code=0, stdout="", stderr="", metric=4.0, timed_out=False)

    eng._monitor_training = _watchdog
    eng._run_eval = _eval

    async def _bounded():
        with anyio.move_on_after(90) as scope:
            await eng._evaluate(0, anyio.CapacityLimiter(1), None)
        return scope.cancelled_caught

    assert not anyio.run(_bounded), "the eval did not terminate"
    return list(EventStore(run_dir / "events.jsonl").read_all()), dev


def _rows(evs, *types):
    return [e.data for e in evs if e.type in types and e.data.get("node_id") == 0]


def test_f_a_repairable_watchdog_stop_reaches_the_developer_and_the_node_is_retried(tmp_path):
    """THE JOIN, end to end: a `not_learning` stop hands the node back and the retry stands.

    This is what `e5small-dr-unified-v2` node 2 could not buy. `monitor_broken` is not in
    `FAILURE_REASONS`, so before the fault split a watchdog stop was terminal — no repair, no retry,
    no refunded slot — and the run recorded 0.0 as the RESULT OF A HYPOTHESIS whose implementation
    was never checked.
    """
    evs, dev = _drive_watchdog_stop(tmp_path, MONITOR_REPAIR_REASON)
    assert dev.repairs == 1, "the watchdog's own diagnosis must buy exactly one repair here"
    # The diagnosis reaches the Developer FIRST — the killed process's tail says only that it died.
    assert "loss frozen at exactly 8.8534" in dev.last_error
    assert "IMPLEMENTATION rather than the idea" in dev.last_error
    # ...and the terminal is the RETRY's, not the stop's.
    assert [e.type for e in evs if e.type in ("node_evaluated", "node_failed")] == ["node_evaluated"]
    assert _rows(evs, "node_evaluated")[0]["metric"] == 4.0
    # The watchdog's reason wins over the exit-code classifier, and the DIRECTIVE follows it: `-9`
    # with no traceback is exactly what `_failure_reason` reads as an OOM, and "reduce memory" is
    # the wrong instruction for a loss that stopped descending (`crash_repair.py` keys the directive
    # off the reason, so the wrong reason is a wrong repair however good the Developer is).
    assert dev.last_error.startswith(f"[failure kind: {MONITOR_REPAIR_REASON}]")


def test_f_an_unrepairable_watchdog_stop_still_terminalizes_at_once(tmp_path):
    """The negative control, and the property the fall-through must not have cost: a verdict the
    judge attributed to the IDEA ends the node where it always did, with no Developer call."""
    evs, dev = _drive_watchdog_stop(tmp_path, "monitor_broken")
    assert dev.repairs == 0 and not _rows(evs, "node_repaired")
    failed = _rows(evs, "node_failed")
    assert len(failed) == 1 and failed[0]["reason"] == "monitor_broken"
    assert not _rows(evs, "node_evaluated")


def test_f_an_operator_who_narrows_the_reasons_gets_the_terminal_back(tmp_path):
    """`inline_repair_reasons` is the operator's answer for every other failure class and it is the
    answer here too — a `not_learning` the operator excluded takes the terminal branch, carrying its
    own reason rather than being re-classified into one of the reasons that remain."""
    evs, dev = _drive_watchdog_stop(
        tmp_path, MONITOR_REPAIR_REASON, inline_repair_reasons=("crash", "timeout", "oom"))
    assert dev.repairs == 0 and not _rows(evs, "node_repaired")
    failed = _rows(evs, "node_failed")
    assert len(failed) == 1 and failed[0]["reason"] == MONITOR_REPAIR_REASON


# ============================================================ G: the judge may read the code
#
# `fault` asks a question the log alone often cannot answer. A frozen loss looks identical whether
# the objective cannot descend AS WRITTEN or the idea simply does not work, and only the source
# says which — so a judge that can read every byte of the log and not one line of the program that
# wrote it is guessing at exactly the attribution that decides between a repair and a verdict.
# It now gets the same read-only scouts every other agent has, rooted at the NODE WORKDIR.

def _looking_host(tmp_path, **kw):
    """A host with the LOOK tools on. `_Host` mirrors `_evaluate`'s wiring and does not set
    `train_monitor_tools`, so the shared harness runs with them off — which is the historical
    one-shot path and exactly what the last test here pins. Everything about looking needs the
    other state."""
    host = _Host(tmp_path, **kw)
    host._train_monitor_tools = True
    return host


def _workdir_with_code(tmp_path, name="node_0"):
    wd = tmp_path / name
    (wd / "vectorsearch").mkdir(parents=True)
    (wd / "train.log").write_text(_COLLAPSED_TAIL, encoding="utf-8")
    (wd / "vectorsearch" / "loss.py").write_text(
        "def nll_cos(q, d, temperature=0.05):\n"
        "    # normalize is never applied to the document tower\n"
        "    q = torch.nn.functional.normalize(q, dim=-1)\n"
        "    return (q @ d.T / temperature).logsumexp(-1).mean()\n", encoding="utf-8")
    return wd


def test_g_the_judge_can_read_the_code_that_is_running(tmp_path):
    """The whole point, driven through the real provider: a symbol the LOG never mentions is
    findable, and it is found in the workdir the eval is actually executing."""
    from looplab.engine.train_monitor import monitor_code_tools

    wd = _workdir_with_code(tmp_path)
    tools = monitor_code_tools(_looking_host(tmp_path), wd)
    assert sorted(s["function"]["name"] for s in tools.specs()) == [
        "find_files", "grep", "list_dir", "read_file"]

    hit = tools.execute("grep", {"pattern": "normalize"})
    assert "loss.py" in hit and "document tower" in hit, hit
    assert "temperature=0.05" in tools.execute("read_file", {"path": "vectorsearch/loss.py"})


def test_g_the_scouts_are_rooted_at_the_workdir_and_cannot_leave_it(tmp_path):
    """Rooted at the WORKDIR, not at the editable source — the code on trial is the code that is
    running, and the two are different filesystems (a distinction that already cost a run). That
    root is also the containment: it is the one region that provably holds only what THIS node
    produced, which is what `monitor_log_sources` already relies on."""
    from looplab.engine.train_monitor import monitor_code_tools

    wd = _workdir_with_code(tmp_path)
    (tmp_path / "secret.txt").write_text("operator-only material", encoding="utf-8")
    tools = monitor_code_tools(_looking_host(tmp_path), wd)

    # asserted on the FILE, not on the pattern: a miss echoes the pattern back in its own answer.
    assert "secret.txt" not in tools.execute("grep", {"pattern": "material"})
    assert "secret.txt" not in tools.execute("find_files", {"root": ".", "pattern": "*.txt"})
    for escape in ("../secret.txt", "/etc/hostname", "../../etc/hostname"):
        answer = tools.execute("read_file", {"path": escape})
        assert "operator-only" not in answer and "localhost" not in answer, (escape, answer[:200])


def test_g_no_workdir_and_no_tools_both_answer_none(tmp_path):
    """Fail-closed on both halves, and the OFF path must stay the historical one-shot call."""
    from looplab.engine.train_monitor import monitor_code_tools, monitor_tools

    class _Off:
        _train_monitor_tools = False

    wd = _workdir_with_code(tmp_path)
    assert monitor_code_tools(_Off(), wd) is None
    assert monitor_tools(_Off(), wd, eval_log_plan(_V2_STAGES)) is None
    assert monitor_code_tools(_looking_host(tmp_path), tmp_path / "does-not-exist") is None


def test_g_the_two_providers_cannot_shadow_each_other(tmp_path):
    """`CompositeTools` de-dups by NAME with the FIRST provider winning, so a collision here would
    be silent: a general file reader taking over `read_log` would answer without this attempt's
    byte floor and hand the judge a dead attempt's curve.

    Today nothing collides, which makes the ORDER unfalsifiable and is why this pins the property
    that actually holds — the two name sets are DISJOINT — rather than asserting an order no
    mutation can break. It stays a live guard: the day either provider grows a name the other has,
    `collisions` fills and this fails, which is the moment the order stops being cosmetic."""
    from looplab.engine.train_monitor import (monitor_code_tools, monitor_log_tools,
                                              monitor_tools)

    wd = _workdir_with_code(tmp_path)
    host, plan = _looking_host(tmp_path), eval_log_plan(_declared(_V2_STAGES))
    logs = {s["function"]["name"] for s in monitor_log_tools(host, wd, plan).specs()}
    code = {s["function"]["name"] for s in monitor_code_tools(host, wd).specs()}
    assert not (logs & code), f"a shadowing collision is now possible: {sorted(logs & code)}"

    tools = monitor_tools(host, wd, plan)
    assert sorted(s["function"]["name"] for s in tools.specs()) == sorted(logs | code)
    assert not getattr(tools, "collisions", []), tools.collisions
    # ...and each half still answers its own kind of question through the composed set.
    assert "8.8534" in tools.execute("read_log", {"log": "train.log"})
    assert "temperature=0.05" in tools.execute("read_file", {"path": "vectorsearch/loss.py"})


def test_g_the_invitation_names_the_code_only_when_the_tools_are_wired(tmp_path):
    """Prompt text is a contract: with tools off the message must be byte-identical to the one this
    feature found, and with them on it must actually say the code is readable — a tool the model is
    never told about is a tool it does not use."""
    from looplab.engine.train_monitor import _LOOK_INVITATION

    for probe in ("read_file", "grep", "fault"):
        assert probe in _LOOK_INVITATION, probe

    wd = _workdir_with_code(tmp_path)
    seen = {}

    class _Recorder(_ScriptedClient):
        def complete_tool(self, messages, schema):
            seen.setdefault("with", messages[-1]["content"])
            return super().complete_tool(messages, schema)

    host = _looking_host(tmp_path, client=_Recorder(_COLLAPSED), asha_kill=False)
    _drive_train(host, wd, plan=eval_log_plan(_V2_STAGES),
                 until=lambda h: len(h.store.rows(EV_TRAIN_MONITOR_ALERT)) >= 1)
    assert _LOOK_INVITATION in seen["with"]

    other = tmp_path / "off"
    other.mkdir()
    wd2 = _workdir_with_code(other, name="node_1")
    seen2 = {}

    class _Recorder2(_ScriptedClient):
        def complete_tool(self, messages, schema):
            seen2.setdefault("without", messages[-1]["content"])
            return super().complete_tool(messages, schema)

    host2 = _Host(other, client=_Recorder2(_COLLAPSED), asha_kill=False)
    host2._train_monitor_tools = False
    _drive_train(host2, wd2, plan=eval_log_plan(_V2_STAGES),
                 until=lambda h: len(h.store.rows(EV_TRAIN_MONITOR_ALERT)) >= 1)
    assert _LOOK_INVITATION not in seen2["without"]
