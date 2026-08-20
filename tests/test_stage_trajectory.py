"""The inter-stage stage check is asked whether the loss is UNCHANGED FROM THE FIRST TRAINING STEP,
and is handed `run.out[-4000:]` of a `run.out` that is already a 64,000-byte tail clamp — a window
that structurally cannot contain the first training step.

Re-derived on `runs/rubertlite-dense-retrieval` (2026-08-20): 16 `node_failed` rows carry
`reason: no_metric` from a stage check, and TEN of those nodes were later reset by the operator, came
back with `train` `reused` at `seconds 0.0` — the very checkpoint the checker had condemned — and
SCORED 0.805 / 0.8412 / 0.8424 / 0.8379 / 0.8606 / 0.8265 / 0.8376 / 0.8662 / 0.8531 / 0.8147
against a run best of 0.8835. Node 1's own `train.log` is 1,214,400 bytes and runs `loss=33.9` ->
`loss=13.3` over 11,248 logged points; its last 4,000 characters hold THREE of them and all three
read `13.3`.

So every test here drives the DIFFERENCE between the two windows rather than describing either. The
first one is the negative control the rest rest on: the same reader, over the same bytes, answers the
question correctly from the whole log and cannot answer it at all from the tail.
"""
from __future__ import annotations

import os
from pathlib import Path

from looplab.engine.train_monitor import (STAGE_CHECK_TRAJECTORY_KIND, eval_log_plan,
                                          read_stage_trajectory, snapshot_training_logs,
                                          stage_check_trajectory, summarize_loss_window,
                                          summarize_trajectory, trajectory_acquits_stage_check)
from looplab.runtime.command_eval import (STAGE_CHECK_HARD_KINDS, STAGE_CHECK_INCONCLUSIVE,
                                          run_command_eval)

# The window `command_eval._run_stages` hands the checker, spelled here so a test that claims to be
# about the tail is actually about THAT tail.
CHECKER_WINDOW = 4000


def _tqdm(records):
    """A tqdm-shaped log: one `\\r`-delimited render per record, which is the shape every training log
    in `runs/` actually has (a whole multi-hour run inside ONE newline-delimited line)."""
    bar = "|" + "#" * 40 + "|"
    return "".join(f"\rEpoch {i // 40}: {bar} {i}/800 [00:{i % 60:02d}<00:00, 3.3it/s, "
                   f"loss={v}, v_num=0]" for i, v in enumerate(records))


def _converged(n=800, lo=13.3, hi=33.9, flat_from=0.55):
    """A log that FELL and then flattened — the shape 10 of the 16 condemned nodes have. `flat_from`
    is the fraction of the run after which nothing moves, so the last `CHECKER_WINDOW` characters
    hold one repeated value exactly as node 1's do."""
    knee = int(n * flat_from)
    values = [round(hi - (hi - lo) * (i / knee), 1) if i < knee else lo for i in range(n)]
    return _tqdm(values)


def _frozen(n=800, value=14.8):
    """A log that never moved: the case the verdict exists for."""
    return _tqdm([value] * n)


def _write(path, text):
    Path(path).write_text(text, encoding="utf-8")
    return str(path)


# ---------------------------------------------------------------- the control: the window, not the reader

def test_the_checkers_window_cannot_answer_the_question_the_whole_log_answers(tmp_path):
    """ONE reader, ONE log, TWO windows, opposite answers. This is the whole defect.

    If this test ever passes because both windows agree, the veto below is measuring nothing."""
    text = _converged()
    path = _write(tmp_path / "train.log", text)

    whole = read_stage_trajectory(path)
    assert whole.direction == "descending"                    # the loss demonstrably moved
    assert whole.first > whole.last and whole.points > 100

    # The SAME reduction over exactly what the checker is shown. `summarize_trajectory` needs two
    # numeric windows to state a direction at all, so give the tail every advantage: split it in half
    # and reduce each half, which is strictly more than the one window the checker's tail can be.
    tail = text[-CHECKER_WINDOW:]
    half = len(tail) // 2
    windows = [w for w in (summarize_loss_window(tail[:half]), summarize_loss_window(tail[half:]))
               if w is not None]
    from_tail = summarize_trajectory(windows)
    assert from_tail.direction != "descending", (
        "the tail must NOT be able to see the descent — if it can, this fixture no longer "
        "reproduces the defect and every assertion below is vacuous")
    assert from_tail.direction in ("flat", "unknown")
    # ...and the numbers say why: the tail holds a few points of one repeated value.
    assert from_tail.points < whole.points / 10


# ---------------------------------------------------------------- the measurement over a finished log

def test_a_frozen_loss_is_measured_as_frozen(tmp_path):
    """The positive control for the OTHER direction: the veto must not rescue a stage that really
    never moved, or it has simply retired the verdict."""
    path = _write(tmp_path / "train.log", _frozen())
    trajectory = read_stage_trajectory(path)
    assert trajectory.direction == "flat" and trajectory.points > 100
    assert trajectory_acquits_stage_check(STAGE_CHECK_TRAJECTORY_KIND, trajectory) == (False, "")


def test_a_loss_that_climbed_is_also_not_unchanged(tmp_path):
    """`rubertlite-dense-retrieval` node 22: the loss ran 18.9 -> 17.6 and then climbed to 32.6 and
    plateaued, so its tail reads a constant `32.0` and the checker wrote "Loss remains constant at 32
    across all epochs". That node scored 0.8147. A `descending`-only predicate loses it; the claim
    being refuted is UNCHANGED, and rising refutes it."""
    values = [17.6] * 300 + [round(17.6 + (32.6 - 17.6) * (i / 100), 1) for i in range(100)] + [32.0] * 400
    path = _write(tmp_path / "train.log", _tqdm(values))
    trajectory = read_stage_trajectory(path)
    assert trajectory.direction == "rising"
    acquitted, note = trajectory_acquits_stage_check(STAGE_CHECK_TRAJECTORY_KIND, trajectory)
    assert acquitted and "rising" in note


def test_a_diverged_log_is_never_acquitted(tmp_path):
    """The four genuinely-failed nodes (n15 `loss=inf` for 20 epochs, n60 `nan`, n68 `-2e+10`,
    n74 `-2.35e+08`) name `nan_or_inf_loss`, which the veto cannot reach BY KIND — and `anomalous` is
    the second, independent refusal for a diverged run the model happened to label the other way."""
    text = _tqdm([33.9 - 0.02 * i for i in range(400)] + ["nan"] * 400)
    path = _write(tmp_path / "train.log", text)
    trajectory = read_stage_trajectory(path)
    assert trajectory.anomalous and "non-finite" in trajectory.anomaly
    assert trajectory_acquits_stage_check(STAGE_CHECK_TRAJECTORY_KIND, trajectory) == (False, "")


def test_a_log_that_never_writes_a_record_boundary_is_still_streamed(tmp_path):
    """A script printing with `end=""` writes one enormous line with no `\\n` and no `\\r`. The
    boundary-seeking reader has nothing to split on there, so the carry is bounded and split anyway —
    an unbounded carry is the slurp this function streams to avoid. Driven at a chunk size small
    enough that the forced split really has to fire."""
    values = [round(33.9 - 0.02 * i, 2) for i in range(800)]
    path = _write(tmp_path / "train.log", "".join(f" loss={v} " for v in values))
    trajectory = read_stage_trajectory(path, windows=8)
    assert trajectory.windows >= 2                            # it really split, rather than slurping
    assert trajectory.direction == "descending"
    assert trajectory.points > len(values) - trajectory.windows   # at most one point lost per split


def test_a_log_with_no_loss_at_all_leaves_the_verdict_alone(tmp_path):
    """A silent stage, an unreadable log and a missing one are the same answer: nothing measured, so
    nothing is contradicted and the checker's verdict stands exactly as it did before this rung."""
    quiet = read_stage_trajectory(_write(tmp_path / "prep.log", "loading shards\ndone\n"))
    missing = read_stage_trajectory(str(tmp_path / "nope.log"))
    for trajectory in (quiet, missing):
        assert trajectory.windows == 0 and trajectory.direction == "unknown"
        assert trajectory_acquits_stage_check(STAGE_CHECK_TRAJECTORY_KIND, trajectory) == (False, "")


# ---------------------------------------------------------------- what the veto may reach

def test_the_veto_reaches_exactly_one_hard_kind(tmp_path):
    """Driven over the REGISTRY, so a hard kind added later inherits the property instead of silently
    escaping the count. A descending trajectory is the input that makes the reachable kind acquit;
    every other kind must refuse the same evidence."""
    trajectory = read_stage_trajectory(_write(tmp_path / "train.log", _converged()))
    assert trajectory.direction == "descending"           # the input really can produce an acquittal
    acquitted = {kind for kind in STAGE_CHECK_HARD_KINDS
                 if trajectory_acquits_stage_check(kind, trajectory)[0]}
    assert len(STAGE_CHECK_HARD_KINDS) > 1                # the loop is not over an empty registry
    assert acquitted == {STAGE_CHECK_TRAJECTORY_KIND}
    # ...and `inconclusive` — a verdict that already fails nothing — is not a thing to acquit either.
    assert trajectory_acquits_stage_check(STAGE_CHECK_INCONCLUSIVE, trajectory)[0] is False


# ---------------------------------------------------------------- the attempt boundary

def test_a_previous_attempts_curve_is_not_this_attempts(tmp_path):
    """Stage logs are opened `"a"` (`sandbox._tee_drain`), so a repaired stage appends to its
    predecessor's bytes. Both directions are driven: without the floor the earlier attempt's descent
    is read as this attempt's, WITH it the frozen retry is seen for what it is."""
    path = tmp_path / "train.log"
    _write(path, _converged())                            # attempt 1: fell 33.9 -> 13.3
    snapshot = snapshot_training_logs(tmp_path)            # the "before", taken between attempts
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(_frozen(value=14.8))                      # attempt 2: never moved

    floorless = read_stage_trajectory(str(path))
    assert floorless.direction == "descending", "the splice must really be misreadable, or the floor proves nothing"
    assert trajectory_acquits_stage_check(STAGE_CHECK_TRAJECTORY_KIND, floorless)[0] is True

    plan = eval_log_plan([{"name": "train", "check": True}, {"name": "score"}])
    floored = stage_check_trajectory(tmp_path, "train", plan=plan, snapshot=snapshot)
    assert floored.direction == "flat"
    assert trajectory_acquits_stage_check(STAGE_CHECK_TRAJECTORY_KIND, floored) == (False, "")
    assert floored.points < floorless.points               # it really read fewer bytes


def test_an_unattributable_log_is_not_evidence(tmp_path):
    """`eval_log_plan`'s ambiguity rule, reused rather than re-decided: two stages folding onto one
    basename (or a stage called `setup` shadowing the dep install's own `setup.log`) means nobody can
    say which phase wrote the bytes. Same refusal `monitor_log_sources` makes."""
    _write(tmp_path / "setup.log", _converged())
    shadowed = eval_log_plan([{"name": "setup", "check": True}, {"name": "score"}])
    assert stage_check_trajectory(tmp_path, "setup", plan=shadowed).windows == 0
    # ...and a stage the plan does not name at all cannot borrow another stage's log.
    _write(tmp_path / "train.log", _converged())
    plan = eval_log_plan([{"name": "train", "check": True}, {"name": "score"}])
    assert stage_check_trajectory(tmp_path, "elsewhere", plan=plan).windows == 0
    assert stage_check_trajectory(tmp_path, "train", plan=plan).windows > 0    # the plan CAN say yes


# ---------------------------------------------------------------- end to end, through the real clamp

class _Fake:
    def __init__(self, reply):
        self.reply = reply
        self.seen = []

    def complete_text(self, msgs):
        self.seen.append(msgs)
        return self.reply


def _engine(tmp_path):
    from looplab.adapters.toytask import ToyTask
    from looplab.engine.orchestrator import Engine
    from looplab.runtime.sandbox import SubprocessSandbox
    from looplab.search.policy import GreedyTree
    task = ToyTask.load(Path(__file__).resolve().parents[1] / "examples" / "toy_task.json")
    researcher, developer = task.build_roles()
    engine = Engine(tmp_path / "run", task=task, researcher=researcher, developer=developer,
                    sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=2, max_nodes=3))
    engine._eval_spec = {"metric": {"reader": "stdout_regex", "pattern": "RECALL@100: ([0-9.]+)"}}
    return engine


def _node():
    from looplab.core.models import Idea, Node
    return Node(id=1, operator="improve",
                idea=Idea(operator="improve", params={}, rationale="uniformity-weighted contrastive loss"))


def test_the_checker_is_handed_the_measurement_and_its_refusal_is_vetoed(tmp_path, monkeypatch):
    """The whole rung, end to end: a checker that names `loss_unchanged_from_first_step` about a log
    that fell 33.9 -> 13.3 no longer ends the node, and the row keeps BOTH readings."""
    stages = [{"name": "train", "check": True}, {"name": "score"}]
    engine = _engine(tmp_path)
    kill = _Fake(f"FAIL {STAGE_CHECK_TRAJECTORY_KIND}: loss stagnant at 13.3 throughout epoch 19")
    monkeypatch.setattr(engine, "_reflect_client", lambda: kill)
    # The callback is built BEFORE the stage writes anything — that is where the attempt's "before"
    # comes from, and writing the log first would make the whole file the previous attempt's.
    check = engine._stage_check_fn(_node(), str(tmp_path), stages)
    _write(tmp_path / "train.log", _converged())

    verdict = check("train", "loss=13.3\nloss=13.3\n")
    assert verdict is not None and verdict.kind == STAGE_CHECK_INCONCLUSIVE   # the node survives
    assert STAGE_CHECK_TRAJECTORY_KIND in verdict.concern                     # what the model said
    assert "11" not in verdict.concern or "33.9" in verdict.concern           # ...and the numbers
    assert "33.9" in verdict.concern and "13.3" in verdict.concern

    # The MODEL was told too, in the same call — the fix is to the question, not only to the answer.
    blob = " ".join(m["content"] for m in kill.seen[0])
    assert "TRAJECTORY MEASURED BY THE ENGINE" in blob
    assert "DIRECTION: descending" in blob


def test_a_stage_that_really_froze_is_still_failed(tmp_path, monkeypatch):
    """The same call, the same verdict, a log that never moved: the kill must still land, or the veto
    has retired `loss_unchanged_from_first_step` instead of grounding it."""
    stages = [{"name": "train", "check": True}, {"name": "score"}]
    engine = _engine(tmp_path)
    kill = _Fake(f"FAIL {STAGE_CHECK_TRAJECTORY_KIND}: loss constant at 14.8 throughout epoch 19")
    monkeypatch.setattr(engine, "_reflect_client", lambda: kill)
    check = engine._stage_check_fn(_node(), str(tmp_path), stages)
    _write(tmp_path / "train.log", _frozen())

    verdict = check("train", "loss=14.8\n")
    assert verdict is not None and verdict.kind == STAGE_CHECK_TRAJECTORY_KIND
    blob = " ".join(m["content"] for m in kill.seen[0])
    assert "DIRECTION: flat" in blob            # the model was shown the same measurement


def test_without_a_workdir_the_prompt_and_the_verdict_are_what_they_always_were(tmp_path, monkeypatch):
    """The library shape and every double in the suite call `_stage_check_fn(node)`. That path must
    not gain a measurement block, and must not gain a veto."""
    engine = _engine(tmp_path)
    kill = _Fake(f"FAIL {STAGE_CHECK_TRAJECTORY_KIND}: loss constant at 14.8")
    monkeypatch.setattr(engine, "_reflect_client", lambda: kill)
    verdict = engine._stage_check_fn(_node())("train", "loss=14.8\n")
    assert verdict is not None and verdict.kind == STAGE_CHECK_TRAJECTORY_KIND
    blob = " ".join(m["content"] for m in kill.seen[0])
    assert "TRAJECTORY MEASURED BY THE ENGINE" not in blob


def test_the_pipeline_actually_continues_past_a_vetoed_stage(tmp_path, monkeypatch):
    """Through `run_command_eval` itself, with the real `run.out[-4000:]` clamp in the loop: the
    training writes its own descending curve, the checker condemns it, and the `score` stage still
    runs and still produces the metric."""
    engine = _engine(tmp_path)
    kill = _Fake(f"FAIL {STAGE_CHECK_TRAJECTORY_KIND}: loss stagnant at 13.3, no learning progress")
    monkeypatch.setattr(engine, "_reflect_client", lambda: kill)

    # A training that prints the curve `_converged` describes, so the LOG is written by the eval
    # rather than by the test — the floor, the plan and the path are all the production ones.
    Path(tmp_path, "train.py").write_text(
        "import sys\n"
        "hi, lo, n, knee = 33.9, 13.3, 800, 440\n"
        "for i in range(n):\n"
        "    v = round(hi - (hi - lo) * (i / knee), 1) if i < knee else lo\n"
        "    sys.stdout.write('\\rEpoch %d: %d/800 [00:00<00:00, 3.3it/s, loss=%s, v_num=0]' % (i // 40, i, v))\n"
        "sys.stdout.write('\\n')\n", encoding="utf-8")
    Path(tmp_path, "score.py").write_text("print('RECALL@100: 0.8662')\n", encoding="utf-8")
    stages = [{"name": "train", "command": ["python", "train.py"], "check": True},
              {"name": "score", "command": ["python", "score.py"]}]

    result = run_command_eval(
        ["true"], str(tmp_path), 120,
        {"kind": "stdout_regex", "pattern": "RECALL@100: ([0-9.]+)"},
        stages=stages, log_dir=str(tmp_path),
        check_fn=engine._stage_check_fn(_node(), str(tmp_path), stages))

    assert os.path.exists(tmp_path / "train.log")                # the production log path
    assert result.failed_stage is None and result.metric == 0.8662
    rows = {row["name"]: row for row in result.stages}
    assert rows["train"]["status"] == "ok" and rows["score"]["status"] == "ok"
    recorded = rows["train"]["check_inconclusive"]
    # BOTH readings survive the runtime's 300-char clamp — the engine's direction AND the model's
    # kind. The clamp is real (`str(_text)[:300]`), so this is the budget the note is written to;
    # composed the other way round it ate the finding.
    assert len(recorded) <= 300
    assert "DIRECTION descending" in recorded                    # what the engine measured
    assert STAGE_CHECK_TRAJECTORY_KIND in recorded               # what the model said
    assert "11" in recorded or "800" in recorded                 # ...and the numbers behind it
    assert "concern" not in rows["train"]                        # nothing was failed


def test_the_same_pipeline_dies_when_the_loss_really_never_moved(tmp_path, monkeypatch):
    """The negative control for the test above, differing in ONE thing: what the training printed."""
    engine = _engine(tmp_path)
    kill = _Fake(f"FAIL {STAGE_CHECK_TRAJECTORY_KIND}: loss constant at 14.8, no learning")
    monkeypatch.setattr(engine, "_reflect_client", lambda: kill)
    Path(tmp_path, "train.py").write_text(
        "import sys\n"
        "for i in range(800):\n"
        "    sys.stdout.write('\\rEpoch %d: %d/800 [00:00<00:00, 3.3it/s, loss=14.8, v_num=0]' % (i // 40, i))\n"
        "sys.stdout.write('\\n')\n", encoding="utf-8")
    Path(tmp_path, "score.py").write_text("print('RECALL@100: 0.8662')\n", encoding="utf-8")
    stages = [{"name": "train", "command": ["python", "train.py"], "check": True},
              {"name": "score", "command": ["python", "score.py"]}]

    result = run_command_eval(
        ["true"], str(tmp_path), 120,
        {"kind": "stdout_regex", "pattern": "RECALL@100: ([0-9.]+)"},
        stages=stages, log_dir=str(tmp_path),
        check_fn=engine._stage_check_fn(_node(), str(tmp_path), stages))

    assert result.failed_stage == "train" and result.metric is None
    rows = {row["name"]: row for row in result.stages}
    assert rows["train"]["status"] == "check_failed"
    assert "score" not in rows                                   # the pipeline really stopped
