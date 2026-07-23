"""ASHA live-curve watchdog: pure rank/extraction helpers + the advisory/opt-in-kill loop. The loop is
advisory-only unless `_asha_live_kill`, appends only the fold-IGNORED EV_ASHA_RANK, and reuses the
training monitor's kill_signal — so none of this touches folded selection or replay."""
import threading

import anyio

from looplab.adapters.tasks import normalize_task
from looplab.core.models import Event, Idea, Node, NodeStatus, RunState
from looplab.engine.asha_monitor import (
    AshaMonitorMixin, IntermediateSample, _curve_metric_at, asha_underperforming,
    extract_resource_curve, latest_intermediate, latest_intermediate_sample,
    sibling_final_metrics, sibling_metrics_at_resource,
)
from looplab.engine.train_monitor import snapshot_training_logs
from looplab.events.types import DIAGNOSTIC_EVENTS, EV_ASHA_RANK


# --------------------------------------------------------------------- latest_intermediate (reuses read_metric)

def test_latest_intermediate_reads_the_last_stdout_json_value():
    log = '{"recall": 0.10}\nsome noise\n{"recall": 0.42}\n'
    assert latest_intermediate(log, "/wd", {"kind": "stdout_json", "key": "recall"}) == 0.42


def test_latest_intermediate_regex_and_missing_and_nonfinite():
    assert latest_intermediate("step 1 acc=0.5\nstep 2 acc=0.7\n", "/wd",
                               {"kind": "stdout_regex", "pattern": r"acc=([0-9.]+)"}) == 0.7
    assert latest_intermediate("", "/wd", {"kind": "stdout_json", "key": "recall"}) is None
    assert latest_intermediate("no metric here", "/wd", {"kind": "stdout_json", "key": "recall"}) is None
    assert latest_intermediate('{"recall": "nan"}', "/wd", {"kind": "stdout_json", "key": "recall"}) is None


def test_latest_intermediate_only_reads_stdout_kinds():
    # Safety restriction: file_*/adapter/host_score kinds read a workdir file or EXEC agent code — never
    # run those on the raw live tail (sandbox bypass / stale-file / loop block). They get no live signal.
    log = '{"recall": 0.42}\n'
    for kind in ("file_json", "file_regex", "adapter", "host_score"):
        assert latest_intermediate(log, "/wd", {"kind": kind, "key": "recall",
                                                "path": "m.json", "pattern": "x"}) is None


def test_intermediate_resource_requires_an_explicit_key_on_the_same_record():
    log = '{"recall": 0.20, "step": 1}\n{"recall": 0.42, "step": 2}\n'
    implicit = latest_intermediate_sample(
        log, "/wd", {"kind": "stdout_json", "key": "recall"})
    explicit = latest_intermediate_sample(
        log, "/wd", {"kind": "stdout_json", "key": "recall", "resource_key": "step"})
    missing = latest_intermediate_sample(
        '{"recall": 0.42}\n', "/wd",
        {"kind": "stdout_json", "key": "recall", "resource_key": "step"})
    same_key = latest_intermediate_sample(
        log, "/wd", {"kind": "stdout_json", "key": "recall", "resource_key": "recall"})

    assert implicit == IntermediateSample(value=0.42)
    assert explicit == IntermediateSample(value=0.42, resource_key="step", resource=2.0)
    assert missing == IntermediateSample(value=0.42)
    assert same_key == IntermediateSample(value=0.42)


def test_metric_resource_key_survives_composable_normalization():
    normalized = normalize_task({
        "goal": "opt", "direction": "max", "repo": "/repo",
        "cmd": {"command": ["python", "t.py"],
                "metric": {"reader": "stdout_json", "key": "score", "resource_key": "step"}},
    })
    assert normalized["eval"]["metric"] == {
        "kind": "stdout_json", "key": "score", "resource_key": "step",
    }


# --------------------------------------------------------------------- sibling_final_metrics (pure)

def test_sibling_final_metrics_excludes_self_and_non_finite():
    state = _fake_state([0.8, 0.6, float("inf")])
    assert sorted(sibling_final_metrics(state, node_id=0)) == [0.6, 0.8]


def test_sibling_final_metrics_excludes_discarded_selection_evidence():
    state = _fake_state([0.8, 0.7, 0.6])
    state.nodes[1].tombstoned = True
    state.nodes[2].feasible = False
    state.aborted_nodes.append(3)
    assert sibling_final_metrics(state, node_id=0) == []


def test_sibling_resource_metrics_never_substitute_finished_endpoints():
    state = _fake_state(
        [0.90, 0.85, 0.80],
        tails=[
            '{"recall": 0.10, "step": 1}\n{"recall": 0.90, "step": 10}\n',
            '{"recall": 0.08, "step": 1}\n{"recall": 0.85, "step": 10}\n',
            '{"recall": 0.09, "step": 1}\n{"recall": 0.80, "step": 10}\n',
        ],
    )
    spec = {"kind": "stdout_json", "key": "recall", "resource_key": "step"}
    same_step = IntermediateSample(value=0.11, resource_key="step", resource=1.0)
    absent_step = IntermediateSample(value=0.11, resource_key="step", resource=2.0)

    assert sorted(sibling_metrics_at_resource(state, 0, spec, same_step)) == [0.08, 0.09, 0.10]
    assert sibling_metrics_at_resource(state, 0, spec, absent_step) == []


# --------------------------------------------------------------------- extract_resource_curve / durable curve (#7)

def test_extract_resource_curve_sorts_and_keeps_latest_per_resource():
    stdout = ('{"recall": 0.10, "step": 1}\n'
              '{"recall": 0.50, "step": 5}\n'
              '{"recall": 0.55, "step": 5}\n'          # re-emitted step 5 -> the LATEST value wins
              '{"recall": 0.90, "step": 10}\n')
    spec = {"kind": "stdout_json", "key": "recall", "resource_key": "step"}
    assert extract_resource_curve(stdout, spec) == [[1.0, 0.10], [5.0, 0.55], [10.0, 0.90]]


def test_extract_resource_curve_requires_a_declared_stdout_json_resource_key():
    stdout = '{"recall": 0.9, "step": 10}\n'
    # no declared resource_key -> not eligible (we never guess step/epoch is fidelity)
    assert extract_resource_curve(stdout, {"kind": "stdout_json", "key": "recall"}) is None
    # resource_key == metric key -> not a distinct resource -> None
    assert extract_resource_curve(
        stdout, {"kind": "stdout_json", "key": "step", "resource_key": "step"}) is None
    # non stdout_json kind -> None (never mine a workdir file / a regex line for a curve)
    assert extract_resource_curve(
        stdout, {"kind": "stdout_regex", "key": "recall", "resource_key": "step"}) is None
    # nothing parses -> None (no signal, never a spurious empty curve)
    assert extract_resource_curve(
        "", {"kind": "stdout_json", "key": "recall", "resource_key": "step"}) is None
    assert extract_resource_curve(
        "no json here", {"kind": "stdout_json", "key": "recall", "resource_key": "step"}) is None
    assert extract_resource_curve(stdout, None) is None


def test_extract_resource_curve_downsamples_but_keeps_endpoints():
    # 100 distinct coordinates, cap 32 -> at most 32 points; the FIRST (earliest) and last survive so an
    # early live sample still has a same-resource peer after the downsample.
    lines = "".join('{"recall": %f, "step": %d}\n' % (i / 100.0, i) for i in range(1, 101))
    spec = {"kind": "stdout_json", "key": "recall", "resource_key": "step"}
    curve = extract_resource_curve(lines, spec, max_points=32)
    assert 2 <= len(curve) <= 32
    assert curve[0][0] == 1.0 and curve[-1][0] == 100.0        # endpoints retained
    assert curve == sorted(curve)                              # ascending by resource
    assert extract_resource_curve(lines, spec, max_points=1000) == [
        [float(i), i / 100.0] for i in range(1, 101)]          # no downsample when under the cap


def test_curve_metric_at_reads_the_exact_coordinate_only():
    curve = [[1.0, 0.05], [10.0, 0.80]]
    assert _curve_metric_at(curve, 1.0) == 0.05
    assert _curve_metric_at(curve, 10.0) == 0.80
    assert _curve_metric_at(curve, 5.0) is None      # no such coordinate -> no observation
    assert _curve_metric_at(None, 1.0) is None       # pre-#7 log (curve absent)
    assert _curve_metric_at([["bad"], [1.0, "x"], [2.0, 0.5]], 2.0) == 0.5   # malformed rows skipped


def test_sibling_metrics_prefer_the_durable_curve_over_the_truncated_tail():
    # #7 core: the 500-char stdout_tail retains only each sibling's FINAL epoch (step 10). A live node at
    # an EARLY step (1) — the only time an ASHA kill actually saves compute — finds NO same-resource peer
    # in those tails. The durable resource_curve, mined from the FULL stdout at node_evaluated, keeps the
    # early coordinate, so the same-resource population is now discoverable. The curve is read first.
    state = _fake_state(
        [0.90, 0.85, 0.80],
        tails=['{"recall": 0.90, "step": 10}\n',
               '{"recall": 0.85, "step": 10}\n',
               '{"recall": 0.80, "step": 10}\n'],
    )
    for i, early in zip((1, 2, 3), (0.10, 0.08, 0.09)):
        state.nodes[i].resource_curve = [[1.0, early], [10.0, state.nodes[i].metric]]
    spec = {"kind": "stdout_json", "key": "recall", "resource_key": "step"}
    early_sample = IntermediateSample(value=0.11, resource_key="step", resource=1.0)

    # The tails alone hold nothing at step 1; the curves supply all three early peers.
    assert sorted(sibling_metrics_at_resource(state, 0, spec, early_sample)) == [0.08, 0.09, 0.10]
    # And the endpoint step is still readable from the same curves.
    final_sample = IntermediateSample(value=0.5, resource_key="step", resource=10.0)
    assert sorted(sibling_metrics_at_resource(state, 0, spec, final_sample)) == [0.80, 0.85, 0.90]


def test_node_evaluated_folds_resource_curve_and_old_logs_default_none(tmp_path):
    from looplab.events.eventstore import EventStore
    from looplab.events.replay import fold

    def _log(name, evaluated):
        s = EventStore(tmp_path / name)
        s.append("run_started", {"run_id": "t", "task_id": "dr", "goal": "g", "direction": "max"})
        s.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                                  "idea": {"operator": "draft", "params": {}}})
        s.append("node_evaluated", evaluated)
        return fold(s.read_all())

    curve = [[1.0, 0.1], [10.0, 0.9]]
    st = _log("with_curve.jsonl", {"node_id": 0, "metric": 0.9, "resource_curve": curve})
    assert st.nodes[0].resource_curve == curve
    # A pre-#7 node_evaluated carries no resource_curve -> reader default None (byte-identical replay).
    st_old = _log("no_curve.jsonl", {"node_id": 0, "metric": 0.9})
    assert st_old.nodes[0].resource_curve is None


# --------------------------------------------------------------------- asha_underperforming (pure)

def test_underperforming_direction_min_and_max_at_median():
    pop = [0.2, 0.4, 0.6]                                 # median 0.4
    # direction min (lower better): a value WORSE than the median (> 0.4) underperforms.
    assert asha_underperforming(0.5, pop, "min", quantile=0.5) is True
    assert asha_underperforming(0.3, pop, "min", quantile=0.5) is False
    # direction max (higher better): a value < median underperforms.
    assert asha_underperforming(0.3, pop, "max", quantile=0.5) is True
    assert asha_underperforming(0.5, pop, "max", quantile=0.5) is False


def test_underperforming_quantile_smaller_is_more_conservative():
    pop = [0.1, 0.2, 0.3, 0.9]                            # direction min: best=0.1, worst=0.9
    # quantile 0.0 = the WORST peer (bar 0.9) — conservative: only a value worse than the worst flags.
    assert asha_underperforming(0.15, pop, "min", quantile=0.0) is False
    assert asha_underperforming(1.5, pop, "min", quantile=0.0) is True    # worse than the worst (0.9)
    # quantile 1.0 = the BEST peer (bar 0.1) — aggressive: anything worse than the best flags.
    assert asha_underperforming(0.15, pop, "min", quantile=1.0) is True
    # the median bar (0.2) does not flag a value better than it.
    assert asha_underperforming(0.15, pop, "min", quantile=0.5) is False
    # unknowns -> None (never act)
    assert asha_underperforming(None, pop, "min") is None
    assert asha_underperforming(0.5, [], "min") is None
    assert asha_underperforming(0.5, pop, "min", quantile=1.5) is None


# --------------------------------------------------------------------- the loop (stub-driven)

class _FakeStore:
    def __init__(self):
        self.events = []

    def read_all(self):
        return [Event(seq=index, ts=0.0, type=event_type, data=dict(data))
                for index, (event_type, data) in enumerate(self.events)]

    def append(self, etype, data):
        self.events.append((etype, dict(data)))


class _Span:
    def set(self, *a, **k):
        pass

    def set_many(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Tracer:
    def span(self, *a, **k):
        return _Span()


class _AshaStub(AshaMonitorMixin):
    def __init__(self, *, kill=False, quantile=0.5, min_siblings=3, cadence=0.01):
        self.tracer = _Tracer()
        self._write_lock = anyio.Lock()
        self.store = _FakeStore()
        self._asha_live_kill = kill
        self._asha_live_quantile = quantile
        self._asha_live_min_siblings = min_siblings
        self._cadence = cadence

    def _asha_cadence(self):
        return self._cadence


def _fake_state(finals, self_id=0, tails=None):
    idea = Idea(operator="draft", params={}, rationale="asha test")
    nodes = {
        self_id: Node(id=self_id, operator="draft", idea=idea, status=NodeStatus.pending),
    }
    for i, m in enumerate(finals, start=1):
        nodes[i] = Node(
            id=i,
            operator="draft",
            idea=idea,
            metric=m,
            status=NodeStatus.evaluated,
            stdout_tail=(tails[i - 1] if tails else ""),
        )
    return RunState(nodes=nodes)


def _run_loop(stub, workdir, spec, direction, kill_signal, monkeypatch, finals, *,
              tails=None, log_snapshot=None, window=0.12):
    monkeypatch.setattr(
        "looplab.events.replay.fold", lambda events: _fake_state(finals, tails=tails))

    async def drive():
        cancel = threading.Event()
        async with anyio.create_task_group() as tg:
            tg.start_soon(AshaMonitorMixin._monitor_asha, stub, 0, 0, str(workdir), cancel,
                          spec, direction, kill_signal, log_snapshot)
            await anyio.sleep(window)
            tg.cancel_scope.cancel()

    anyio.run(drive)


def test_loop_records_asha_rank_when_underperforming(tmp_path, monkeypatch):
    wd = tmp_path / "node_0"
    wd.mkdir()
    (wd / "train.log").write_text('{"recall": 0.30}\n', encoding="utf-8")   # far below finished peers
    stub = _AshaStub(kill=False, quantile=0.5, min_siblings=3)
    _run_loop(stub, wd, {"kind": "stdout_json", "key": "recall"}, "max", {}, monkeypatch,
              finals=[0.80, 0.70, 0.60])
    alerts = [d for (t, d) in stub.store.events if t == EV_ASHA_RANK]
    assert alerts, "an underperforming intermediate must record one EV_ASHA_RANK"
    assert alerts[0]["node_id"] == 0 and alerts[0]["population"] == 3


def test_loop_records_recovery_transition_after_underperformance(tmp_path, monkeypatch):
    wd = tmp_path / "node_0"
    wd.mkdir()
    samples = iter(['{"recall": 0.30}\n', '{"recall": 0.90}\n'])
    latest = ['{"recall": 0.90}\n']

    def _tail(_workdir, **_kwargs):
        try:
            latest[0] = next(samples)
        except StopIteration:
            pass
        return latest[0]

    monkeypatch.setattr("looplab.engine.train_monitor.read_training_tail_raw", _tail)
    stub = _AshaStub(kill=False, quantile=0.5, min_siblings=3)
    _run_loop(stub, wd, {"kind": "stdout_json", "key": "recall"}, "max", {}, monkeypatch,
              finals=[0.80, 0.70, 0.60], window=0.08)
    transitions = [d["underperforming"] for (t, d) in stub.store.events if t == EV_ASHA_RANK]
    assert transitions[:2] == [True, False]


def test_resumed_asha_monitor_closes_pre_crash_episode(tmp_path, monkeypatch):
    wd = tmp_path / "node_0"
    wd.mkdir()
    (wd / "train.log").write_text('{"recall": 0.90}\n', encoding="utf-8")
    stub = _AshaStub(kill=False, quantile=0.5, min_siblings=3)
    stub.store.events.append((EV_ASHA_RANK, {
        "node_id": 0, "generation": 0, "underperforming": True,
        "intermediate": 0.3, "quantile": 0.5, "population": 3,
    }))
    _run_loop(stub, wd, {"kind": "stdout_json", "key": "recall"}, "max", {}, monkeypatch,
              finals=[0.80, 0.70, 0.60], window=0.08)
    transitions = [data["underperforming"] for event_type, data in stub.store.events
                   if event_type == EV_ASHA_RANK]
    assert transitions == [True, False]


def test_loop_stays_quiet_when_on_track_or_too_few_siblings(tmp_path, monkeypatch):
    wd = tmp_path / "node_0"
    wd.mkdir()
    (wd / "train.log").write_text('{"recall": 0.90}\n', encoding="utf-8")   # above the peers -> fine
    stub = _AshaStub(quantile=0.5, min_siblings=3)
    _run_loop(stub, wd, {"kind": "stdout_json", "key": "recall"}, "max", {}, monkeypatch,
              finals=[0.80, 0.70, 0.60])
    assert not [t for (t, _d) in stub.store.events if t == EV_ASHA_RANK]

    # Underperforming, but only 2 finished siblings (< min_siblings=3) -> never ranks.
    stub2 = _AshaStub(quantile=0.5, min_siblings=3)
    (wd / "train.log").write_text('{"recall": 0.10}\n', encoding="utf-8")
    _run_loop(stub2, wd, {"kind": "stdout_json", "key": "recall"}, "max", {}, monkeypatch,
              finals=[0.80, 0.70])
    assert not [t for (t, _d) in stub2.store.events if t == EV_ASHA_RANK]


def test_loop_opt_in_kill_requires_comparable_resource_evidence(tmp_path, monkeypatch):
    wd = tmp_path / "node_0"
    wd.mkdir()
    (wd / "train.log").write_text('{"recall": 0.10, "step": 1}\n', encoding="utf-8")
    spec = {"kind": "stdout_json", "key": "recall", "resource_key": "step"}

    # kill OFF -> advisory only, no kill signal even though it underperforms.
    off = {}
    _run_loop(_AshaStub(kill=False, min_siblings=3), wd, spec, "max", off, monkeypatch,
              finals=[0.8, 0.7, 0.6])
    assert off.get("kill") is not True

    # Even with intervention enabled, an ordinary metric contract has no declared notion of progress.
    # Keep the endpoint rank as an audit signal, but never invent a resource and kill from it.
    endpoint_only = {}
    endpoint_stub = _AshaStub(kill=True, min_siblings=3, cadence=0.01)
    _run_loop(endpoint_stub, wd, {"kind": "stdout_json", "key": "recall"}, "max",
              endpoint_only, monkeypatch, finals=[0.8, 0.7, 0.6], window=0.2)
    assert endpoint_only.get("kill") is not True
    endpoint_alerts = [d for event, d in endpoint_stub.store.events if event == EV_ASHA_RANK]
    assert endpoint_alerts and endpoint_alerts[0]["kill_comparable"] is False

    # The live curve is already better than peers were at the SAME step, even though it is naturally
    # below their finished endpoints. The old endpoint-only comparison killed this healthy improving run.
    improving = {}
    peer_curves = [
        '{"recall": 0.05, "step": 1}\n{"recall": 0.80, "step": 10}\n',
        '{"recall": 0.07, "step": 1}\n{"recall": 0.70, "step": 10}\n',
        '{"recall": 0.09, "step": 1}\n{"recall": 0.60, "step": 10}\n',
    ]
    stub = _AshaStub(kill=True, min_siblings=3, cadence=0.01)
    _run_loop(stub, wd, spec, "max", improving, monkeypatch,
              finals=[0.8, 0.7, 0.6], tails=peer_curves, window=0.2)
    assert improving.get("kill") is not True
    alerts = [d for event, d in stub.store.events if event == EV_ASHA_RANK]
    assert alerts and alerts[0]["kill_comparable"] is True  # endpoint warning remains diagnostic
    assert alerts[0]["endpoint_underperforming"] is True
    assert alerts[0]["resource_underperforming"] is False

    # With enough truly same-resource evidence, persistent underperformance can still free compute.
    (wd / "train.log").write_text('{"recall": 0.01, "step": 1}\n', encoding="utf-8")
    on = {}
    _run_loop(_AshaStub(kill=True, min_siblings=3, cadence=0.01), wd, spec, "max", on,
              monkeypatch, finals=[0.8, 0.7, 0.6], tails=peer_curves, window=0.2)
    assert on.get("kill") is True
    assert on.get("terminal_reason") == "asha_underperforming"


def test_loop_endpoint_warning_cannot_kill_without_same_resource_or_with_old_attempt_log(
        tmp_path, monkeypatch):
    wd = tmp_path / "node_0"
    wd.mkdir()
    log = wd / "train.log"
    log.write_text('{"recall": 0.01, "step": 1}\n', encoding="utf-8")
    snapshot = snapshot_training_logs(wd)
    with open(log, "a", encoding="utf-8") as fh:
        fh.write("new attempt started; no metric yet\n")

    signal = {}
    stub = _AshaStub(kill=True, min_siblings=3, cadence=0.01)
    _run_loop(
        stub, wd,
        {"kind": "stdout_json", "key": "recall", "resource_key": "step"},
        "max", signal, monkeypatch, finals=[0.8, 0.7, 0.6],
        tails=[
            '{"recall": 0.8, "step": 10}\n',
            '{"recall": 0.7, "step": 10}\n',
            '{"recall": 0.6, "step": 10}\n',
        ],
        log_snapshot=snapshot,
        window=0.15,
    )

    assert signal.get("kill") is not True
    assert not [event for event, _data in stub.store.events if event == EV_ASHA_RANK]


def test_asha_kill_cannot_overwrite_training_monitor_terminal(tmp_path, monkeypatch):
    wd = tmp_path / "node_0"
    wd.mkdir()
    (wd / "train.log").write_text(
        '{"recall": 0.10, "step": 10}\n', encoding="utf-8")
    claimed = {
        "kill": True,
        "reason": "loss became NaN",
        "terminal_reason": "monitor_broken",
        "confidence": 0.97,
    }

    _run_loop(
        _AshaStub(kill=True, min_siblings=3, cadence=0.01), wd,
        {"kind": "stdout_json", "key": "recall", "resource_key": "step"},
        "max", claimed, monkeypatch, finals=[0.8, 0.7, 0.6],
        tails=[
            '{"recall": 0.8, "step": 10}\n',
            '{"recall": 0.7, "step": 10}\n',
            '{"recall": 0.6, "step": 10}\n',
        ],
        window=0.2,
    )

    assert claimed == {
        "kill": True,
        "reason": "loss became NaN",
        "terminal_reason": "monitor_broken",
        "confidence": 0.97,
    }


def test_asha_rank_is_diagnostic():
    assert EV_ASHA_RANK in DIAGNOSTIC_EVENTS      # fold-ignored -> splice-neutral by construction
