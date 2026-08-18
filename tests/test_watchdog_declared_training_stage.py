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
    LOG_ROLE_SCORE,
    LOG_ROLE_TRAINING,
    LOG_ROLE_WORK,
    TrainingVerdict,
    eval_log_plan,
    resolve_stage_log,
    should_monitor_kill,
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
    assert all("kill_role_withheld" not in r for r in host.store.rows(EV_TRAIN_MONITOR_ALERT))
