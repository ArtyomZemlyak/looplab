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

ROOT = Path(__file__).resolve().parents[1]
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
# its shape — it is who authored the print statement.
#
# AND THAT QUESTION IS NOT ANSWERABLE FROM THE ARTIFACT (2026-08-14). The first implementation of
# this channel granted `engine` on `code.startswith(SPECULATION_CUDA_PROBE_CODE_PREFIX)` inside
# `runtime/sandbox.py` — where `code` is the candidate's own `solution.py`. The prefix is a public
# constant in the shipped tree, so that authenticated nothing at all: the forgery below earned the
# tag and survived `auto_extra_metrics=false`, the operator's "authenticated only" switch. The grant
# now requires the ENGINE's own assertion that it authored the artifact, carried from its role wiring
# (`engine/speculation_gate.py::engine_authored_artifacts`), and the prefix keeps its real job —
# saying WHICH engine artifact this is, and therefore which keys are its own.


def _toy_engine(run_dir, developer):
    from looplab.adapters.toytask import ToyTask
    task = ToyTask.load(ROOT / "examples" / "toy_task.json")
    researcher, _ = task.build_roles()
    return Engine(run_dir, task=task, researcher=researcher, developer=developer,
                  sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=1),
                  inline_repair=False, auto_install_deps=False)


def _solution_tier_terminal(run_dir, code, developer):
    """One solution-tier eval of exactly `code`, through the REAL engine, read back off disk.

    Seeds `node_created` directly so the artifact under test is the one the sandbox runs — the
    Developer object still matters, because it is what `engine_authored_artifacts` reads.
    """
    eng = _toy_engine(run_dir, developer)
    eng.store.append("run_started",
                     {"run_id": "r", "task_id": "t", "goal": "g", "direction": "min"})
    eng.store.append("node_created", {
        "node_id": 0, "parent_ids": [], "operator": "draft",
        "idea": {"operator": "draft", "params": {"x": 1.0, "y": 1.0}, "rationale": "seed"},
        "code": code})

    async def _one():
        await eng._evaluate(0, anyio.CapacityLimiter(1), None)

    anyio.run(_one)
    rows = [e.data for e in EventStore(Path(run_dir) / "events.jsonl").read_all()
            if e.type == "node_evaluated"]
    assert rows, "the eval recorded no terminal to inspect"
    return rows[0]


def test_the_probe_declares_its_own_keys_only_behind_an_engine_authorship_assertion():
    """THE RULE, as a truth table, and the conjunct that makes it a rule at all.

    Both halves are required and the authorship half has NO DEFAULT: a caller that cannot assert the
    engine wrote this artifact gets a TypeError, not the pre-2026-08-14 behaviour."""
    from looplab.agents.calibration import (SPECULATION_CUDA_PROBE_CODE_PREFIX,
                                            SPECULATION_CUDA_PROBE_EXTRA_METRIC_KEYS,
                                            engine_declared_extra_metric_keys)

    real = SPECULATION_CUDA_PROBE_CODE_PREFIX + "print(1)\n"
    assert engine_declared_extra_metric_keys(real, engine_authored=True) == (
        frozenset(SPECULATION_CUDA_PROBE_EXTRA_METRIC_KEYS))
    # THE DEFECT, at the unit: the identical bytes, with nobody attesting the engine wrote them.
    assert engine_declared_extra_metric_keys(real, engine_authored=False) == frozenset()
    # The old call is not a quieter answer, it is an error.
    with pytest.raises(TypeError):
        engine_declared_extra_metric_keys(real)
    # THE TRAP THE PREFIX STILL CLOSES, in an engine-authored run: a key NAME promotes nothing.
    assert engine_declared_extra_metric_keys(
        'import json; print(json.dumps({"speculation_cuda_probe_v": 1}))\n',
        engine_authored=True) == frozenset()
    # One byte off the front is not the engine's source.
    assert engine_declared_extra_metric_keys(
        " " + SPECULATION_CUDA_PROBE_CODE_PREFIX, engine_authored=True) == frozenset()
    # Total: a caller may pass whatever it has, and the fail-safe answer is always `auto`.
    for junk in (None, 123, b"bytes", ["code"]):
        assert engine_declared_extra_metric_keys(junk, engine_authored=True) == frozenset()


def test_a_candidate_that_pastes_the_public_probe_prefix_into_its_own_solution_earns_nothing():
    """THE REPRODUCTION, driven, in the auditor's own shape — and the switch that used to admit it.

    `CliAgentDeveloper` is an external coding agent with a real shell whose output IS `solution.py`,
    and `tools/env_inspect.py::read_installed` pages `core/calibration.py` out verbatim with no
    package allow-list, so the prefix is available to it. Prefix + one `print` used to return
    `{'device_count': 'engine', 'alloc_bytes': 'engine', 'my_auc': 'auto'}`."""
    from looplab.agents.calibration import SPECULATION_CUDA_PROBE_CODE_PREFIX
    from looplab.core.models import apply_engine_extra_metric_channels
    from looplab.runtime.sandbox import stdout_extra_metric_channels

    forged = SPECULATION_CUDA_PROBE_CODE_PREFIX + '\nprint({"metric": .99, "device_count": 8})\n'
    extras = {"device_count": 8.0, "alloc_bytes": 4096.0, "my_auc": .99}

    # The capture tier answers `auto` for everything — it is handed an opaque string and runs it.
    captured = stdout_extra_metric_channels(extras)
    assert set(captured.values()) == {EXTRA_METRIC_AUTO}
    # And the grant refuses to raise any of it, because no engine authored these bytes.
    granted = apply_engine_extra_metric_channels(captured, forged, engine_authored=False)
    assert granted == captured

    # The cost of the old answer, stated where it landed: the operator's "authenticated only" switch
    # kept the forged pair. Now it keeps nothing.
    kept, kept_channels = authenticated_extra_metrics_only(extras, granted)
    assert kept == {} and kept_channels == {}


def test_the_authorship_assertion_reads_the_engines_own_wiring_and_never_the_artifact(tmp_path):
    """The conjunct's own truth table. It is true in exactly one configuration — the calibration
    profile, whose Developer is the engine's probe splicer and whose `calibration_gpu_probe` flag is
    deliberately not a Settings/env/UI knob (`cli/__init__.py::_make_calibration_roles` is its only
    writer). Every other run, including one whose artifact carries the prefix, answers False."""
    from looplab.agents.roles import ToyObjectiveDeveloper
    from looplab.engine.speculation_gate import engine_authored_artifacts

    splicer = ToyObjectiveDeveloper(calibration_gpu_probe=True)
    assert engine_authored_artifacts(_toy_engine(tmp_path / "cal", splicer)) is True
    # The SAME class with the flag off — an ordinary Toy run — is not an engine-authored run.
    assert engine_authored_artifacts(
        _toy_engine(tmp_path / "toy", ToyObjectiveDeveloper())) is False

    # Exact type, no subclass: a wrapper is not the splicer, matching the calibration role gate.
    class _Wrapper(ToyObjectiveDeveloper):
        pass

    assert engine_authored_artifacts(
        _toy_engine(tmp_path / "wrap", _Wrapper(calibration_gpu_probe=True))) is False
    # Total and fail-closed on anything that is not an engine at all.
    assert engine_authored_artifacts(object()) is False
    assert engine_authored_artifacts(None) is False


def test_the_real_calibration_artifact_still_tags_its_four_keys_engine_and_nothing_else():
    """The other direction: the artifact the calibration DEVELOPER really builds keeps its channel,
    with a genuine measurement printed beside the probe's keys because the whole claim is that the
    two populations separate."""
    from looplab.agents.roles import (SPECULATION_CUDA_PROBE_EXTRA_METRIC_KEYS,
                                      ToyObjectiveDeveloper)
    from looplab.core.models import apply_engine_extra_metric_channels
    from looplab.core.models import Idea
    from looplab.runtime.sandbox import stdout_extra_metric_channels

    code = ToyObjectiveDeveloper(calibration_gpu_probe=True).implement(
        Idea(operator="draft", params={"x": 1.25, "y": -2.5}, rationale="t",
             footprint={"gpus": 1}))
    extras = {**{k: 1.0 for k in SPECULATION_CUDA_PROBE_EXTRA_METRIC_KEYS}, "train_auc": 0.99}

    channels = apply_engine_extra_metric_channels(
        stdout_extra_metric_channels(extras), code, engine_authored=True)
    assert channels == {**{k: EXTRA_METRIC_ENGINE for k in SPECULATION_CUDA_PROBE_EXTRA_METRIC_KEYS},
                        "train_auc": EXTRA_METRIC_AUTO}
    # The SAME extras off an ordinary artifact stay `auto` in every position, even in that run.
    assert set(apply_engine_extra_metric_channels(
        stdout_extra_metric_channels(extras), "print('hi')\n",
        engine_authored=True).values()) == {EXTRA_METRIC_AUTO}


def test_a_solution_tier_run_records_engine_only_when_the_engine_authored_the_artifact(tmp_path,
                                                                                       monkeypatch):
    """THE RECORD, DRIVEN BOTH WAYS — a real subprocess, a real stdout line, a real event log.

    The CUDA prefix cannot run without a device, so the engine-owned source here is a stand-in
    declared the same way; what is real is the whole path from `node.code` through `json_line_extras`
    and the engine's grant into `node_evaluated.extra_metrics_provenance`. The same bytes, evaluated
    by two engines that differ only in their Developer, record two different channel maps — which is
    the property, because the artifact cannot be what decides."""
    import looplab.agents.calibration as calibration_mod
    from looplab.agents.roles import ToyObjectiveDeveloper

    probe = "# engine-owned probe source\n"
    # The grant resolves the classifier through the calibration MODULE on every call, so the probe
    # that owns the declaration is also the one seam a test patches — there is no second copy of the
    # key tuple anywhere that could answer differently.
    monkeypatch.setattr(
        calibration_mod, "engine_declared_extra_metric_keys",
        lambda code, *, engine_authored: (
            frozenset({"harness_v", "device_count"})
            if engine_authored and isinstance(code, str) and code.startswith(probe)
            else frozenset()))

    code = probe + ('import json; print(json.dumps('
                    '{"metric": 1.0, "harness_v": 2.0, "device_count": 1.0, "recall": 0.25}))\n')

    engine_authored = _solution_tier_terminal(
        tmp_path / "spliced", code, ToyObjectiveDeveloper(calibration_gpu_probe=True))
    assert engine_authored["extra_metrics"] == {"harness_v": 2.0, "device_count": 1.0,
                                                "recall": 0.25}
    assert engine_authored["extra_metrics_provenance"] == {"harness_v": EXTRA_METRIC_ENGINE,
                                                           "device_count": EXTRA_METRIC_ENGINE,
                                                           "recall": EXTRA_METRIC_AUTO}

    # THE NEGATIVE CONTROL, and it is the whole fix: byte-identical source, an ordinary Developer.
    forged = _solution_tier_terminal(tmp_path / "forged", code, ToyObjectiveDeveloper())
    assert forged["extra_metrics"] == engine_authored["extra_metrics"]
    assert set(forged["extra_metrics_provenance"].values()) == {EXTRA_METRIC_AUTO}


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


def test_the_two_classification_tuples_partition_the_channel_vocabulary():
    """`EXTRA_METRIC_UNAUTHENTICATED` is written as its own tuple so "a channel added later must be
    classified on purpose in both directions" — and until 2026-08-15 NOTHING read it, so nothing
    made that true.

    A fifth channel would have joined `EXTRA_METRIC_CHANNELS`, defaulted out of BOTH tuples, and been
    treated as unauthenticated-by-omission by every reader that tests membership of the trusted half
    (`nodes_extra_metrics` is one) — silently, because no test that does not already know the new
    channel's name can notice. The import-time assertion is the reader that closes it; this drives
    that it really refuses, by re-executing the module with a fifth channel spliced in.
    """
    import looplab.core.models as models

    assert (set(models.EXTRA_METRIC_AUTHENTICATED) | set(models.EXTRA_METRIC_UNAUTHENTICATED)
            == set(models.EXTRA_METRIC_CHANNELS) | {models.EXTRA_METRIC_UNKNOWN})
    assert not (set(models.EXTRA_METRIC_AUTHENTICATED)
                & set(models.EXTRA_METRIC_UNAUTHENTICATED))

    source = Path(models.__file__).read_text(encoding="utf-8")
    fifth = source.replace(
        'EXTRA_METRIC_CHANNELS = (EXTRA_METRIC_DECLARED, EXTRA_METRIC_AUTO, EXTRA_METRIC_ENGINE)',
        'EXTRA_METRIC_CHANNELS = (EXTRA_METRIC_DECLARED, EXTRA_METRIC_AUTO, EXTRA_METRIC_ENGINE,\n'
        '                         "attested")')
    assert fifth != source, "the vocabulary moved; this splice no longer names it"
    namespace: dict = {"__name__": "looplab.core.models_fifth_channel_probe",
                       "__file__": models.__file__}
    with pytest.raises(AssertionError, match="classified as authenticated or not"):
        exec(compile(fifth, models.__file__, "exec"), namespace)
    # Executed into a THROWAWAY namespace, never reloaded: rebinding `looplab.core.models` would give
    # the process a second `Node`/`RunState` while every importer still holds the first.
    assert "attested" not in models.EXTRA_METRIC_CHANNELS
