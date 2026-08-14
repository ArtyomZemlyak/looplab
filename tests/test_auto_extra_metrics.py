"""AUTO-CAPTURED vs DECLARED extra metrics — the record must say which door a number came through.

`runtime/command_eval.py` fills `extra_metrics` from two channels and only one was ever guarded:
the operator's own `EvalSpec.metrics` readers (which refuse an agent-authored `adapter` reader) and
`json_line_extras`, which takes EVERY other numeric key off the candidate's own stdout with no
declaration, no reader spec and no gate. Measured over `runs/`: declared produced 0 of 12 extra
metrics, auto produced 12 of 12 — including `speculation_cuda_probe_v=1.0`, a schema VERSION number
recorded as a metric — and every one of them reached the operator, MLflow and the reviewer in the
same visual place as the protected primary.

These drive the property in all three directions the record has to get right:
  * an UNDECLARED key the candidate printed is recorded as `auto`;
  * an operator-DECLARED reader's value is not mislabelled as `auto`;
  * a HISTORICAL row carrying no tag still loads, keeps its values, and reads back `unknown` —
    never `declared`.
Plus the two policy properties: the `auto_extra_metrics` gate drops exactly the undeclared values,
and it can never change how an ALREADY-RECORDED run replays (engine invariant #6).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import anyio
import pytest

from looplab.adapters.repo_task import EvalSpec, RepoTask
from looplab.core.models import (EXTRA_METRIC_AUTO, EXTRA_METRIC_DECLARED, EXTRA_METRIC_ENGINE,
                                 EXTRA_METRIC_UNKNOWN, authenticated_extra_metrics_only,
                                 extra_metric_channel, normalize_extra_metric_channels)
from looplab.engine.orchestrator import Engine
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.runtime.command_eval import run_command_eval
from looplab.runtime.sandbox import SubprocessSandbox
from looplab.search.policy import GreedyTree

_M = {"kind": "stdout_json", "key": "metric"}
_LAT = {"kind": "stdout_json", "key": "latency"}


def _prog(tmp_path, line: dict):
    (tmp_path / "p.py").write_text(
        "import json; print(json.dumps(%s))\n" % json.dumps(line), encoding="utf-8")
    return [sys.executable, "p.py"]


# --------------------------------------------------------------------------- the capture site

def test_an_undeclared_stdout_key_is_recorded_as_auto_captured(tmp_path):
    """DIRECTION 1. Nothing declared `sneaky`; the experiment simply printed it beside the metric."""
    res = run_command_eval(_prog(tmp_path, {"metric": 0.5, "sneaky": 7.0}), str(tmp_path), 60, _M)
    assert res.extra_metrics == {"sneaky": 7.0}
    assert res.extra_metrics_provenance == {"sneaky": EXTRA_METRIC_AUTO}


def test_an_operator_declared_reader_is_not_mislabelled_as_auto(tmp_path):
    """DIRECTION 2 (the positive control). The same number, this time through the guarded channel.

    Note the SHAPE of the trap: `latency` is BOTH a declared reader and another numeric key on the
    stdout line, so auto-capture also sees it. The value's precedence has always been
    declared-wins; the tag must follow it, or the record would claim `auto` about a value the
    operator's own reader produced."""
    res = run_command_eval(_prog(tmp_path, {"metric": 0.5, "latency": 50.0, "sneaky": 7.0}),
                           str(tmp_path), 60, _M, metrics={"latency": _LAT})
    assert res.extra_metrics == {"latency": 50.0, "sneaky": 7.0}
    assert res.extra_metrics_provenance == {"latency": EXTRA_METRIC_DECLARED,
                                            "sneaky": EXTRA_METRIC_AUTO}


def test_the_solution_tier_sandbox_tags_its_extras_auto(tmp_path):
    """The `solution.py` path has no operator reader spec at ALL, so every extra there is auto."""
    res = SubprocessSandbox().run(
        'import json; print(json.dumps({"metric": 1.0, "recall": 0.25}))\n', str(tmp_path))
    assert res.extra_metrics == {"recall": 0.25}
    assert res.extra_metrics_provenance == {"recall": EXTRA_METRIC_AUTO}


# --------------------------------------------------------------------------- the record, driven

def _repo(tmp_path, line: dict):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "run.py").write_text(
        "import json; print(json.dumps(%s))\n" % json.dumps(line), encoding="utf-8")
    return repo


def _engine(tmp_path, repo, *, metrics=None, **knobs):
    task = RepoTask(id="aem", direction="max", editable_path=str(repo), edit_surface=["*.txt"],
                    eval=EvalSpec(command=[sys.executable, "run.py"], metric=_M,
                                  metrics=(metrics or {})))
    researcher, developer = task.build_roles()
    return Engine(tmp_path / "run", task=task, researcher=researcher, developer=developer,
                  sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=1), **knobs)


def _terminals(run_dir: Path) -> list[dict]:
    return [e.data for e in EventStore(run_dir / "events.jsonl").read_all()
            if e.type == "node_evaluated"]


def test_a_real_eval_writes_the_channel_into_the_event_log(tmp_path):
    """THE DRIVEN PROOF: a real run whose experiment prints an undeclared key beside a DECLARED one,
    then the event log read back off disk. Both directions in one record."""
    repo = _repo(tmp_path, {"metric": 1.0, "latency": 50.0, "sneaky": 7.0})
    state = anyio.run(_engine(tmp_path, repo, metrics={"latency": _LAT}).run)
    assert state.finished

    rows = _terminals(tmp_path / "run")
    assert rows, "the run recorded no terminal to inspect"
    data = rows[0]
    assert data["extra_metrics"] == {"latency": 50.0, "sneaky": 7.0}
    # The RECORD distinguishes them — not a derivation a later reader has to redo.
    assert data["extra_metrics_provenance"] == {"latency": EXTRA_METRIC_DECLARED,
                                                "sneaky": EXTRA_METRIC_AUTO}
    # ...and it survives the fold, which is what every consumer actually reads.
    node = state.nodes[data["node_id"]]
    assert extra_metric_channel(node.extra_metrics_provenance, "sneaky") == EXTRA_METRIC_AUTO
    assert extra_metric_channel(node.extra_metrics_provenance, "latency") == EXTRA_METRIC_DECLARED


def test_a_node_with_no_extra_metrics_writes_no_new_key(tmp_path):
    """The additive key is written only when it says something. An unconditional `{}` would change
    the `node_evaluated` bytes of every node in every run — including the CUDA-probe calibration
    nodes whose evidence `search/speculation_quality.py` re-derives — for no information."""
    repo = _repo(tmp_path, {"metric": 1.0})
    anyio.run(_engine(tmp_path, repo).run)
    data = _terminals(tmp_path / "run")[0]
    assert data["extra_metrics"] == {}
    assert "extra_metrics_provenance" not in data


# --------------------------------------------------------------------------- back-compat

def test_an_untagged_historical_row_loads_and_reads_unknown_never_declared(tmp_path):
    """DIRECTION 3. The 12 preserved rows carry no tag. They must still load, keep their values, and
    report `unknown` — the one reading that is honest, since all 12 were in fact auto-captured."""
    store = EventStore(tmp_path / "events.jsonl")
    store.append("run_started", {"run_id": "legacy", "task_id": "t", "direction": "max"})
    store.append("node_created", {"node_id": 0, "operator": "draft", "parent_ids": [],
                                  "idea": {"operator": "draft", "params": {}}})
    store.append("node_evaluated", {"node_id": 0, "metric": 0.9,
                                    "extra_metrics": {"train_auc": 0.99, "test_auc": 0.92}})
    node = fold(store.read_all()).nodes[0]

    assert node.extra_metrics == {"train_auc": 0.99, "test_auc": 0.92}   # nothing lost
    assert node.extra_metrics_provenance == {}                          # nothing invented
    for key in ("train_auc", "test_auc"):
        channel = extra_metric_channel(node.extra_metrics_provenance, key)
        assert channel == EXTRA_METRIC_UNKNOWN
        assert channel != EXTRA_METRIC_DECLARED, "an untagged value must never read as declared"


def test_a_key_missing_from_a_present_map_still_reads_unknown():
    """A partially-tagged map does not lend its authority to the keys it omits — a later merge that
    forgot to tag must degrade to `unknown`, not inherit its neighbours' channel."""
    channels = {"declared_one": EXTRA_METRIC_DECLARED}
    assert extra_metric_channel(channels, "declared_one") == EXTRA_METRIC_DECLARED
    assert extra_metric_channel(channels, "forgotten") == EXTRA_METRIC_UNKNOWN


def test_the_channel_map_rejects_untrusted_shapes():
    """Same discipline as `normalize_extra_metrics`: this arrives from an untyped, possibly
    hand-edited event log, and a value outside the WRITER vocabulary is dropped (-> `unknown`)
    rather than coerced into one."""
    assert normalize_extra_metric_channels(None) == {}
    assert normalize_extra_metric_channels(["auto"]) == {}
    assert normalize_extra_metric_channels({"a": "declared", "b": "trusted", "c": 1,
                                            2: "auto"}) == {"a": "declared"}
    # `unknown` is a READER-side answer and is never storable — writing it must not make it stick.
    assert normalize_extra_metric_channels({"a": EXTRA_METRIC_UNKNOWN}) == {}


@pytest.mark.parametrize("run_name", ["spec-live-0804", "live-periodic", "live-stagnation"])
def test_the_preserved_corpus_still_folds_and_reports_unknown(run_name):
    """The real historical logs, not a reconstruction. Every extra metric in `runs/` predates the
    channel record, so every one of them must fold and answer `unknown`."""
    # `runs/` is untracked, so a git WORKTREE checkout of this tree does not contain it — walk up
    # to whichever ancestor holds the preserved corpus rather than skipping in the one place a
    # coding agent is most likely to be working.
    log = None
    for root in Path(__file__).resolve().parents:
        candidate = root / "runs" / run_name / "events.jsonl"
        if candidate.exists():
            log = candidate
            break
    if log is None:
        pytest.skip(f"preserved run {run_name} is not on this box")
    state = fold(EventStore(log).read_all())
    seen = 0
    for node in state.nodes.values():
        for key in node.extra_metrics:
            seen += 1
            assert extra_metric_channel(node.extra_metrics_provenance, key) == EXTRA_METRIC_UNKNOWN
    assert seen, f"{run_name} was chosen because it HAS extra metrics"


# --------------------------------------------------------------------------- the gate

def test_the_gate_drops_the_undeclared_values_and_keeps_the_declared_one(tmp_path):
    repo = _repo(tmp_path, {"metric": 1.0, "latency": 50.0, "sneaky": 7.0})
    engine = _engine(tmp_path, repo, metrics={"latency": _LAT}, auto_extra_metrics=False)
    anyio.run(engine.run)
    data = _terminals(tmp_path / "run")[0]
    assert data["extra_metrics"] == {"latency": 50.0}
    assert data["extra_metrics_provenance"] == {"latency": EXTRA_METRIC_DECLARED}


def test_the_gate_is_expressed_over_the_tag_so_the_two_cannot_drift():
    """`authenticated_extra_metrics_only` keeps what the CANDIDATE did not author — an `unknown`
    value is dropped with the auto ones, because a reader that cannot prove where a value came from
    must not admit it here either. It keeps `engine` beside `declared`, and each kept value keeps
    its OWN channel: flattening them to `declared` would relabel a harness diagnostic as an
    operator's reading."""
    extras = {"declared_one": 1.0, "auto_one": 2.0, "untagged": 3.0, "probe_one": 4.0}
    channels = {"declared_one": EXTRA_METRIC_DECLARED, "auto_one": EXTRA_METRIC_AUTO,
                "probe_one": EXTRA_METRIC_ENGINE}
    kept, kept_channels = authenticated_extra_metrics_only(extras, channels)
    assert kept == {"declared_one": 1.0, "probe_one": 4.0}
    assert kept_channels == {"declared_one": EXTRA_METRIC_DECLARED,
                             "probe_one": EXTRA_METRIC_ENGINE}


def test_the_gate_is_on_by_default(tmp_path):
    """Default ON = today's behaviour. Flipping it would DELETE information from the record —
    including the CUDA-probe proof `speculation_quality` authenticates by its own means, and every
    legitimate `train_auc` an operator reads — and a run cannot be un-gated after the fact."""
    from looplab.core.config import Settings
    from looplab.engine.options import EngineOptions
    assert Settings().auto_extra_metrics is True
    assert EngineOptions().auto_extra_metrics is True
    repo = _repo(tmp_path, {"metric": 1.0, "sneaky": 7.0})
    assert _engine(tmp_path, repo).auto_extra_metrics is True


# ------------------------------------------------------- invariant #6: an old run replays the same

def test_the_gate_cannot_change_how_an_already_recorded_run_replays(tmp_path):
    """ENGINE INVARIANT #6, proved rather than argued.

    The v8 run executing while this shipped pinned its settings at `run_started`, and the worry a
    new default raises is that a resume/replay would read the run differently. It cannot, and the
    reason is structural: `auto_extra_metrics` is a WRITE-side policy consulted at the ONE place the
    `node_evaluated` payload is built. The fold never reads it. So the same log folds to the same
    state under either value — including a log that predates the flag entirely.

    Driven both ways over one log: same nodes, same metrics, same extras, same channels."""
    import looplab.core.config as config_mod

    store = EventStore(tmp_path / "events.jsonl")
    store.append("run_started", {"run_id": "v8ish", "task_id": "t", "direction": "max"})
    store.append("node_created", {"node_id": 0, "operator": "draft", "parent_ids": [],
                                  "idea": {"operator": "draft", "params": {}}})
    store.append("node_evaluated", {"node_id": 0, "metric": 0.9,
                                    "extra_metrics": {"train_auc": 0.99}})
    store.append("node_created", {"node_id": 1, "operator": "draft", "parent_ids": [],
                                  "idea": {"operator": "draft", "params": {}}})
    store.append("node_evaluated", {"node_id": 1, "metric": 0.8,
                                    "extra_metrics": {"declared_lat": 5.0, "printed": 1.0},
                                    "extra_metrics_provenance": {"declared_lat": "declared",
                                                                 "printed": "auto"}})
    rows = store.read_all()

    def _snapshot():
        state = fold(rows)
        return json.dumps(state.model_dump(mode="json"), sort_keys=True, default=str)

    with_default = _snapshot()
    # The strongest form: flip the SHIPPED DEFAULT itself, not just an instance knob.
    original = config_mod.Settings.model_fields["auto_extra_metrics"].default
    try:
        config_mod.Settings.model_fields["auto_extra_metrics"].default = False
        assert _snapshot() == with_default
    finally:
        config_mod.Settings.model_fields["auto_extra_metrics"].default = original

    state = fold(rows)
    assert state.nodes[0].extra_metrics == {"train_auc": 0.99}       # the untagged historical row
    assert state.nodes[1].extra_metrics == {"declared_lat": 5.0, "printed": 1.0}
    assert state.nodes[1].extra_metrics_provenance == {"declared_lat": "declared",
                                                       "printed": "auto"}


def test_the_flag_is_not_pinned_in_run_started(tmp_path):
    """It is deliberately absent from the `run_started` payload. A new UNCONDITIONAL key there
    would revoke every issued speculation-calibration receipt, whose check compares that payload's
    exact key SET (`search/speculation_quality.py::_CALIBRATION_RUN_STARTED_FIELDS`) — and pinning
    it would buy nothing, because the fold never reads it (see the test above)."""
    from looplab.engine import orchestrator as orch
    from looplab.search.speculation_quality import _CALIBRATION_RUN_STARTED_FIELDS
    assert "auto_extra_metrics" not in _CALIBRATION_RUN_STARTED_FIELDS
    assert "auto_extra_metrics" not in orch.RUN_START_PINNED_FIELDS


# ------------------------------------------- the THIRD channel: the engine's own spliced diagnostic
#
# The residual today's tagging left open, stated in its own words: "tagging makes that visible; it
# does not stop a version number from being CALLED a metric." Re-measured over the WHOLE corpus (238
# `*.jsonl` under `runs/`, not the three logs the first count sampled): 1,642 values, 10 distinct
# keys, 1,636 of them the CUDA probe's four. The separating property is not the key's name and not
# its shape — it is who authored the print statement, which the engine can answer, byte-exactly, for
# the source it splices itself.


def test_the_probe_declares_its_own_keys_only_behind_its_byte_exact_source():
    """THE RULE, as a truth table. The key set is consulted ONLY after the artifact's source is
    authenticated — so the name alone can never promote anything."""
    from looplab.agents.calibration import (SPECULATION_CUDA_PROBE_CODE_PREFIX,
                                            SPECULATION_CUDA_PROBE_EXTRA_METRIC_KEYS,
                                            engine_declared_extra_metric_keys)

    assert engine_declared_extra_metric_keys(SPECULATION_CUDA_PROBE_CODE_PREFIX + "print(1)\n") == (
        frozenset(SPECULATION_CUDA_PROBE_EXTRA_METRIC_KEYS))
    # THE TRAP THIS CLOSES, and the reason a name list would have been wrong: an agent-authored
    # script printing the probe's own key names declares nothing at all.
    assert engine_declared_extra_metric_keys(
        'import json; print(json.dumps({"speculation_cuda_probe_v": 1}))\n') == frozenset()
    # One byte off the front is not the engine's source.
    assert engine_declared_extra_metric_keys(
        " " + SPECULATION_CUDA_PROBE_CODE_PREFIX) == frozenset()
    # Total: a caller may pass whatever it has, and the fail-safe answer is always `auto`.
    for junk in (None, 123, b"bytes", ["code"]):
        assert engine_declared_extra_metric_keys(junk) == frozenset()


def test_the_real_calibration_artifact_tags_its_four_keys_engine_and_nothing_else(tmp_path):
    """Driven against the artifact the calibration DEVELOPER really builds, not a reconstruction —
    and with a genuine measurement printed beside the probe's keys, because the whole claim is that
    the two populations separate. `stdout_extra_metric_channels` takes the exact bytes the sandbox
    is about to run, which is the one place source and output meet."""
    from looplab.agents.roles import (SPECULATION_CUDA_PROBE_EXTRA_METRIC_KEYS,
                                      ToyObjectiveDeveloper)
    from looplab.core.models import Idea
    from looplab.runtime.sandbox import stdout_extra_metric_channels

    code = ToyObjectiveDeveloper(calibration_gpu_probe=True).implement(
        Idea(operator="draft", params={"x": 1.25, "y": -2.5}, rationale="t",
             footprint={"gpus": 1}))
    extras = {**{k: 1.0 for k in SPECULATION_CUDA_PROBE_EXTRA_METRIC_KEYS}, "train_auc": 0.99}

    channels = stdout_extra_metric_channels(extras, code)
    assert channels == {**{k: EXTRA_METRIC_ENGINE for k in SPECULATION_CUDA_PROBE_EXTRA_METRIC_KEYS},
                        "train_auc": EXTRA_METRIC_AUTO}
    # The SAME extras off an ordinary artifact stay `auto` in every position.
    assert set(stdout_extra_metric_channels(extras, "print('hi')\n").values()) == {EXTRA_METRIC_AUTO}


def test_a_solution_tier_run_of_engine_owned_source_records_engine_end_to_end(tmp_path, monkeypatch):
    """The capture site DRIVEN — a real subprocess, a real stdout line, a real `RunResult`.

    The CUDA prefix cannot run without a device, so the engine-owned source here is a stand-in
    spliced the same way; what is real is the whole path from `code` through `json_line_extras` to
    the recorded channel map. The byte-exact half of the rule is the test above."""
    import looplab.agents.calibration as calibration_mod

    probe = "# engine-owned probe source\n"
    # The sandbox resolves the classifier through the calibration MODULE on every call, so the probe
    # that owns the declaration is also the one seam a test patches — there is no copy in `runtime`
    # that could answer differently.
    monkeypatch.setattr(
        calibration_mod, "engine_declared_extra_metric_keys",
        lambda code: (frozenset({"harness_v", "device_count"})
                      if isinstance(code, str) and code.startswith(probe) else frozenset()))

    code = probe + ('import json; print(json.dumps('
                    '{"metric": 1.0, "harness_v": 2.0, "device_count": 1.0, "recall": 0.25}))\n')
    res = SubprocessSandbox().run(code, str(tmp_path))

    assert res.extra_metrics == {"harness_v": 2.0, "device_count": 1.0, "recall": 0.25}
    assert res.extra_metrics_provenance == {"harness_v": EXTRA_METRIC_ENGINE,
                                            "device_count": EXTRA_METRIC_ENGINE,
                                            "recall": EXTRA_METRIC_AUTO}


def test_the_gate_no_longer_deletes_the_cuda_proof_the_receipt_path_re_derives():
    """WHY THE THIRD CHANNEL IS WORTH A CHANNEL, proved against the real validator.

    `search/speculation_quality.py::_validate_cuda_probe_artifact` admits a calibration node by its
    EXACT extra-metric key schema. Before the engine channel existed, the only way to express
    `auto_extra_metrics=false` was "keep the declared ones", which dropped all four probe keys and
    made the validator refuse — so the gate and the calibration receipt could not both be had. Now
    the gate keeps them, because they were never the candidate's numbers."""
    from looplab.agents.calibration import (SPECULATION_CUDA_PROBE_CODE_PREFIX,
                                            SPECULATION_CUDA_PROBE_DEVICE_COUNT_METRIC,
                                            SPECULATION_CUDA_PROBE_STATIC_EXTRA_METRICS)
    from looplab.core.models import NodeStatus
    from looplab.search.speculation_quality import _validate_cuda_probe_artifact

    extras = {**dict(SPECULATION_CUDA_PROBE_STATIC_EXTRA_METRICS),
              SPECULATION_CUDA_PROBE_DEVICE_COUNT_METRIC: 1}
    channels = {k: EXTRA_METRIC_ENGINE for k in extras}

    kept, kept_channels = authenticated_extra_metrics_only(extras, channels)
    assert kept == extras, "the gate must not delete the engine's own proof"
    assert set(kept_channels.values()) == {EXTRA_METRIC_ENGINE}

    class _Node:
        code = SPECULATION_CUDA_PROBE_CODE_PREFIX + "print('x')\n"
        status = NodeStatus.evaluated
        extra_metrics = kept

    _validate_cuda_probe_artifact(_Node())        # accepts — no exception is the assertion

    # ...and the counter-proof: keeping only `declared` is what used to happen, and it refuses.
    class _Stripped(_Node):
        extra_metrics = {}

    with pytest.raises(ValueError):
        _validate_cuda_probe_artifact(_Stripped())
