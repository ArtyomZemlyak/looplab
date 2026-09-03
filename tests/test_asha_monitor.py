"""ASHA live-curve watchdog: pure rank/extraction helpers + the advisory/LLM-mediated-kill loop. The
loop is advisory-only unless `_asha_live_kill`, appends only the fold-IGNORED EV_ASHA_RANK /
EV_ASHA_VERDICT, and reuses the training monitor's kill_signal — so none of this touches folded
selection or replay. The rank test is EVIDENCE: the stop itself needs a confident `stop` from the LLM
judge, which is consulted INSIDE the rank gate and therefore can only ever narrow the kill set."""
import threading
import time

import anyio
import pytest

from looplab.adapters.tasks import normalize_task
from looplab.core.models import Event, Idea, Node, NodeStatus, RunState
from looplab.engine.asha_monitor import (
    AshaMonitorMixin, AshaVerdict, IntermediateSample, _curve_metric_at, asha_judge_context,
    asha_underperforming, extract_resource_curve, latest_extra_metrics, latest_intermediate,
    latest_intermediate_sample, latest_train_verdict, rank_bar, should_asha_kill,
    sibling_final_metrics, sibling_metrics_at_resource,
)
from looplab.engine.train_monitor import snapshot_training_logs
from looplab.events.types import (
    DIAGNOSTIC_EVENTS, EV_ASHA_RANK, EV_ASHA_VERDICT, EV_TRAIN_MONITOR_ALERT,
)


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


def test_sibling_resource_metrics_use_the_rung_curve_not_the_finished_endpoint():
    state = _fake_state([0.90, 0.85, 0.80])
    for i, early in zip((1, 2, 3), (0.10, 0.08, 0.09)):
        state.nodes[i].resource_curve = [[1.0, early], [8.0, state.nodes[i].metric]]   # rung 1 + endpoint 8
    spec = {"kind": "stdout_json", "key": "recall", "resource_key": "step"}
    rung1 = IntermediateSample(value=0.11, resource_key="step", resource=1.0)           # rung 1
    absent_rung = IntermediateSample(value=0.11, resource_key="step", resource=2.0)     # rung 2 (not persisted)

    # a rung-1 sample reads each sibling's rung-1 checkpoint, NEVER their finished endpoint (rung 8)
    assert sorted(sibling_metrics_at_resource(state, 0, spec, rung1)) == [0.08, 0.09, 0.10]
    assert sibling_metrics_at_resource(state, 0, spec, absent_rung) == []              # no rung-2 checkpoint


# --------------------------------------------------------------------- extract_resource_curve / durable curve (#7)

def test_extract_resource_curve_collapses_to_rungs_earliest_per_band():
    stdout = ('{"recall": 0.10, "step": 1}\n'          # rung 1
              '{"recall": 0.50, "step": 4}\n'          # rung 4 (band 4-7) -> EARLIEST step in the band wins
              '{"recall": 0.55, "step": 6}\n'          # rung 4, later step -> dropped
              '{"recall": 0.90, "step": 10}\n')        # rung 8
    spec = {"kind": "stdout_json", "key": "recall", "resource_key": "step"}
    assert extract_resource_curve(stdout, spec) == [[1.0, 0.10], [4.0, 0.50], [8.0, 0.90]]


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


def test_extract_resource_curve_collapses_a_full_run_to_geometric_rungs():
    # 100 steps collapse to the geometric rung schedule (powers of two) across the WHOLE run — the
    # EARLIEST (start-of-band) value per band — so a live node DEEP in the run finds a sibling checkpoint
    # at its rung, not the exact-coordinate gap the old first-31+endpoint retention left (#7 review).
    lines = "".join('{"recall": %f, "step": %d}\n' % (i / 100.0, i) for i in range(1, 101))
    spec = {"kind": "stdout_json", "key": "recall", "resource_key": "step"}
    curve = extract_resource_curve(lines, spec)
    # rung r keeps the EARLIEST step in [r, 2r) -> step r -> r/100
    assert curve == [[float(r), r / 100.0] for r in (1, 2, 4, 8, 16, 32, 64)]
    assert _curve_metric_at(curve, 50) == 0.32       # step 50 -> rung 32 -> start-of-band step 32


def test_curve_metric_at_snaps_the_query_to_its_rung():
    curve = [[1.0, 0.05], [8.0, 0.80]]               # rungs 1 and 8
    assert _curve_metric_at(curve, 1) == 0.05        # rung 1
    assert _curve_metric_at(curve, 1.5) == 0.05      # 1.5 -> rung 1
    assert _curve_metric_at(curve, 8) == 0.80        # rung 8
    assert _curve_metric_at(curve, 12) == 0.80       # 12 -> rung 8 (band [8, 16))
    assert _curve_metric_at(curve, 4) is None        # rung 4 not persisted -> no observation
    assert _curve_metric_at(curve, 0) is None        # non-positive -> no rung
    assert _curve_metric_at(None, 1) is None          # pre-#7 log (curve absent)
    assert _curve_metric_at([["bad"], [1.0, "x"], [2.0, 0.5]], 2) == 0.5   # malformed rows skipped; rung 2


def test_sibling_metrics_prefer_the_durable_curve_over_the_truncated_tail():
    # #7 core: the 500-char stdout_tail retains only each sibling's FINAL epoch (rung 8). A live node at
    # an EARLY step (1 -> rung 1) — the only time an ASHA kill actually saves compute — finds NO peer in
    # those tails. The durable per-rung curve keeps the early rung, so the population is discoverable.
    state = _fake_state(
        [0.90, 0.85, 0.80],
        tails=['{"recall": 0.90, "step": 10}\n',
               '{"recall": 0.85, "step": 10}\n',
               '{"recall": 0.80, "step": 10}\n'],
    )
    for i, early in zip((1, 2, 3), (0.10, 0.08, 0.09)):
        state.nodes[i].resource_curve = [[1.0, early], [8.0, state.nodes[i].metric]]   # rungs 1 and 8
    spec = {"kind": "stdout_json", "key": "recall", "resource_key": "step"}
    early_sample = IntermediateSample(value=0.11, resource_key="step", resource=1.0)   # rung 1

    # The tails alone hold nothing at rung 1; the curves supply all three early peers.
    assert sorted(sibling_metrics_at_resource(state, 0, spec, early_sample)) == [0.08, 0.09, 0.10]
    # And the endpoint rung is read from the same curves (step 10 -> rung 8).
    final_sample = IntermediateSample(value=0.5, resource_key="step", resource=10.0)   # rung 8
    assert sorted(sibling_metrics_at_resource(state, 0, spec, final_sample)) == [0.80, 0.85, 0.90]


def test_sibling_metrics_at_resource_finds_mid_run_peers_via_rungs():
    # The owner's scenario: a live node at step 50 (deep in a 100-step run). Under exact-coordinate
    # matching, completed peers had no coordinate 50, so no comparable population and the kill streak
    # reset until the endpoint. With the shared rung schedule step 50 -> rung 32, and each sibling
    # persisted a rung-32 checkpoint, so the mid-run population is discoverable.
    state = _fake_state([0.90, 0.85, 0.80])
    for i, mid in zip((1, 2, 3), (0.60, 0.58, 0.62)):
        state.nodes[i].resource_curve = [[1.0, 0.05], [32.0, mid], [64.0, state.nodes[i].metric]]
    spec = {"kind": "stdout_json", "key": "recall", "resource_key": "step"}
    sample = IntermediateSample(value=0.30, resource_key="step", resource=50.0)        # step 50 -> rung 32
    assert sorted(sibling_metrics_at_resource(state, 0, spec, sample)) == [0.58, 0.60, 0.62]


def test_earliest_per_band_does_not_false_flag_a_node_just_into_a_band():
    # Peer review: a live node just INTO a rung band sits at the band's LOW end. Storing the LATEST
    # (end-of-band) sibling value compared a healthy improving node against ~2× more training and
    # false-flagged it (and could spuriously kill). EARLIEST-per-band is the start-of-band checkpoint.
    # Min-objective, loss = 1/step: three siblings each trained steps 1..100.
    lines = "".join('{"loss": %f, "step": %d}\n' % (1.0 / i, i) for i in range(1, 101))
    spec = {"kind": "stdout_json", "key": "loss", "resource_key": "step"}
    curve = extract_resource_curve(lines, spec)
    assert dict(curve)[64.0] == 1 / 64          # rung 64 = start-of-band step 64, NOT end-of-band step 100
    state = _fake_state([0.5, 0.5, 0.5])
    for i in (1, 2, 3):
        state.nodes[i].resource_curve = curve
    sample = IntermediateSample(value=1 / 65, resource_key="step", resource=65.0)   # step 65 -> rung 64
    pop = sibling_metrics_at_resource(state, 0, spec, sample)
    assert pop == [1 / 64, 1 / 64, 1 / 64]
    # 1/65 < 1/64 -> the node is slightly AHEAD of the siblings' start-of-band checkpoint, NOT flagged
    # (the old end-of-band 1/100 checkpoint made asha_underperforming(1/65, [1/100…], min) spuriously True)
    assert asha_underperforming(1 / 65, pop, "min", quantile=0.5) is False


def test_sibling_with_no_curve_is_excluded_not_substituted_from_the_tail():
    # Peer review: a sibling contributes ONLY its per-rung curve (a START-of-band checkpoint). The
    # 500-char stdout_tail holds only FINAL epochs, so any in-band value there is END-of-band (~2× more
    # trained); substituting it would false-flag a live node just into the band and mix start/end-of-band
    # values in one population. A sibling with no curve at this rung is EXCLUDED, never tail-substituted.
    state = _fake_state(
        [0.5, 0.5, 0.5],
        tails=['{"loss": 0.20, "step": 64}\n{"loss": 0.10, "step": 100}\n'] * 3,   # tail: end-of-band only
    )
    # nodes 1,2 have NO resource_curve (default None) -> excluded; node 3 has a real curve -> contributes.
    state.nodes[3].resource_curve = [[64.0, 0.18]]                                   # rung 64 start-of-band
    spec = {"kind": "stdout_json", "key": "loss", "resource_key": "step"}
    sample = IntermediateSample(value=0.19, resource_key="step", resource=65.0)      # step 65 -> rung 64
    pop = sibling_metrics_at_resource(state, 0, spec, sample)
    assert pop == [0.18]        # only the curve sibling; the tail-only siblings' end-of-band 0.10 is NOT used


def test_extract_resource_curve_survives_a_huge_integer_coordinate():
    # Peer review: a solution printing a 400-digit-int step overflowed float() inside
    # extract_resource_curve (called in _evaluate's write lock), aborting the node terminal. A pathological
    # coordinate must degrade to "no rung", never crash — the finite line still yields its rung.
    huge = 10 ** 400
    stdout = ('{"recall": 0.5, "step": %d}\n' % huge) + '{"recall": 0.9, "step": 8}\n'
    spec = {"kind": "stdout_json", "key": "recall", "resource_key": "step"}
    assert extract_resource_curve(stdout, spec) == [[8.0, 0.90]]     # huge coord dropped, step 8 kept


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


def test_node_evaluated_normalizes_untrusted_resource_curve(tmp_path):
    # #7 review: Node assignment validation is off, so the fold must coerce untrusted `resource_curve`
    # event data to at most 32 sorted/unique/finite [resource, metric] pairs (or None) — a corrupt log
    # must never land a scalar / huge nested value on the Node that then rides snapshots.
    from looplab.events.eventstore import EventStore
    from looplab.events.replay import fold

    def _fold(name, curve):
        s = EventStore(tmp_path / name)
        s.append("run_started", {"run_id": "t", "task_id": "dr", "goal": "g", "direction": "max"})
        s.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                                  "idea": {"operator": "draft", "params": {}}})
        s.append("node_evaluated", {"node_id": 0, "metric": 0.9, "resource_curve": curve})
        return fold(s.read_all()).nodes[0].resource_curve

    assert _fold("scalar.jsonl", 5) is None                    # non-list -> None
    assert _fold("str.jsonl", "boom") is None
    assert _fold("dict.jsonl", {"1": 2}) is None
    # malformed/short/non-finite entries dropped; valid pairs kept, sorted, last-write-wins per resource
    assert _fold("mixed.jsonl", [["bad"], [10, 0.9], [1, 0.1], [1, 0.15], [None, 3],
                                 [float("inf"), 0.5]]) == [[1.0, 0.15], [10.0, 0.9]]
    # an oversized (corrupt) curve is bounded to <=32 coordinates with both endpoints kept
    out = _fold("big.jsonl", [[i, i / 1000.0] for i in range(200)])
    assert len(out) <= 32 and out[0][0] == 0.0 and out[-1][0] == 199.0


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


class _JudgeClient:
    """Stand-in for the Developer's LLM client. Answers through `complete_tool` — the SAME structured
    path production takes (`structured_judge` -> `parse_structured` -> tool_call), so the verdict is
    really schema-validated here — and records every prompt it was shown so a test can assert what
    evidence the judge actually received. `verdict=None` raises instead, standing in for an endpoint
    failure / unparseable answer."""

    def __init__(self, verdict=None):
        self._verdict = verdict
        self.calls = 0
        self.contexts: list[str] = []

    def complete_tool(self, messages, schema):
        self.calls += 1
        self.contexts.append(messages[-1]["content"])
        if self._verdict is None:
            raise RuntimeError("judge endpoint is down")
        return dict(self._verdict)


class _JudgeDeveloper:
    def __init__(self, client):
        self.client = client


class _AshaStub(AshaMonitorMixin):
    def __init__(self, *, kill=False, quantile=0.5, min_siblings=3, cadence=0.01,
                 judge=None, kill_confidence=0.8):
        self.tracer = _Tracer()
        self._write_lock = anyio.Lock()
        self.store = _FakeStore()
        self._asha_live_kill = kill
        self._asha_live_quantile = quantile
        self._asha_live_min_siblings = min_siblings
        self._asha_live_kill_confidence = kill_confidence
        self._cadence = cadence
        # No `developer` at all == the offline/toy path: `_asha_verdict` finds no client and returns
        # None, which `should_asha_kill` treats as "do not stop".
        self.judge = judge
        if judge is not None:
            self.developer = _JudgeDeveloper(judge)

    def _asha_cadence(self):
        return self._cadence


def _fake_state(finals, self_id=0, tails=None, curves=None):
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
            resource_curve=(curves[i - 1] if curves else None),   # the same-rung comparable source
        )
    return RunState(nodes=nodes)


# Wall-clock ceiling for "the loop produced what this test is waiting for". Bounds only the FAILURE
# case: with `until` the loop cancels the instant the predicate holds, so a passing test never waits it
# out. `window` alone is a FIXED budget (0.08-0.2s at a 0.01-0.05s cadence) that a loaded full-suite
# host can miss entirely, leaving the test reading an empty alert list while passing in isolation.
# A test asserting an alert is ABSENT must still wait the fixed window; only waiting FOR one can poll.
_LOOP_SETTLE_TIMEOUT_S = 15.0


def _run_loop(stub, workdir, spec, direction, kill_signal, monkeypatch, finals, *,
              tails=None, curves=None, log_snapshot=None, window=0.12, until=None):
    monkeypatch.setattr(
        "looplab.events.replay.fold", lambda events: _fake_state(finals, tails=tails, curves=curves))

    async def drive():
        cancel = threading.Event()
        async with anyio.create_task_group() as tg:
            tg.start_soon(AshaMonitorMixin._monitor_asha, stub, 0, 0, str(workdir), cancel,
                          spec, direction, kill_signal, log_snapshot)
            if until is None:
                await anyio.sleep(window)
            else:
                deadline = time.monotonic() + _LOOP_SETTLE_TIMEOUT_S
                while not until(stub) and time.monotonic() < deadline:
                    await anyio.sleep(0.005)
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

    def _recovered(current):
        flags = [data["underperforming"] for event_type, data in current.store.events
                 if event_type == EV_ASHA_RANK]
        return flags[:2] == [True, False]

    _run_loop(stub, wd, {"kind": "stdout_json", "key": "recall"}, "max", {}, monkeypatch,
              finals=[0.80, 0.70, 0.60], until=_recovered)
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
    # WAIT FOR THE ROW, do not bet 80 ms that it arrives. This used a fixed `window=0.08`, which is
    # a wall-clock wager on the monitor emitting its second rank inside that slice — it holds when
    # the test runs alone and loses under full-suite load (observed once in a whole-suite run, green
    # in isolation and on three consecutive runs of this file). `until=` with the file's own settle
    # deadline is the idiom four other tests here already use, and it cannot weaken the guard: the
    # assertion below is unchanged and still demands exactly [True, False], so an extra or a missing
    # transition still fails.
    _run_loop(stub, wd, {"kind": "stdout_json", "key": "recall"}, "max", {}, monkeypatch,
              finals=[0.80, 0.70, 0.60],
              until=lambda s: len([1 for t, _d in s.store.events if t == EV_ASHA_RANK]) >= 2)
    transitions = [data["underperforming"] for event_type, data in stub.store.events
                   if event_type == EV_ASHA_RANK]
    assert transitions == [True, False]


def test_the_resume_recovery_rejects_a_bool_node_id_from_the_event_log(tmp_path, monkeypatch):
    """The other half of the resume recovery, and the half that drifts when a site re-inlines the
    scan: `isinstance(True, int)` is True and `True == 1`, so a row carrying `node_id: true` matches a
    plain `== node_id` test against node 1 and hands this lifecycle ANOTHER one's open episode.

    Adopting it publishes a recovery edge for an episode this node never had — Attention and the
    digest then show a warning-then-recovered history that did not happen. `last_lifecycle_row` owns
    that guard for both watchdogs (doc 25 EC-04); this drives the asha resume through it.
    """
    wd = tmp_path / "node_1"
    wd.mkdir()
    (wd / "train.log").write_text('{"recall": 0.90}\n', encoding="utf-8")   # above every peer: healthy

    idea = Idea(operator="draft", params={}, rationale="asha test")
    nodes = {1: Node(id=1, operator="draft", idea=idea, status=NodeStatus.pending)}
    for index, metric in enumerate([0.80, 0.70, 0.60], start=2):
        nodes[index] = Node(id=index, operator="draft", idea=idea, metric=metric,
                            status=NodeStatus.evaluated)
    monkeypatch.setattr("looplab.events.replay.fold", lambda events: RunState(nodes=nodes))

    stub = _AshaStub(kill=False, quantile=0.5, min_siblings=3)
    stub.store.events.append((EV_ASHA_RANK, {
        "node_id": True, "generation": 1, "underperforming": True,
        "intermediate": 0.3, "quantile": 0.5, "population": 3,
    }))

    async def drive():
        cancel = threading.Event()
        async with anyio.create_task_group() as tg:
            tg.start_soon(AshaMonitorMixin._monitor_asha, stub, 1, 1, str(wd), cancel,
                          {"kind": "stdout_json", "key": "recall"}, "max", {}, None)
            await anyio.sleep(0.12)
            tg.cancel_scope.cancel()

    anyio.run(drive)
    published = [data for event_type, data in stub.store.events[1:] if event_type == EV_ASHA_RANK]
    assert published == [], (
        "the resume recovery adopted a bool `node_id` row as this lifecycle's own episode "
        f"and published a phantom edge: {published}")


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
    # Wait for the audit alert rather than for the clock: a FIXED 0.2s budget is not enough on a
    # loaded host, and the empty list then read as "no alert was recorded".
    _run_loop(endpoint_stub, wd, {"kind": "stdout_json", "key": "recall"}, "max",
              endpoint_only, monkeypatch, finals=[0.8, 0.7, 0.6], window=0.2,
              until=lambda s: any(t == EV_ASHA_RANK for t, _d in s.store.events))
    assert endpoint_only.get("kill") is not True
    endpoint_alerts = [d for event, d in endpoint_stub.store.events if event == EV_ASHA_RANK]
    assert endpoint_alerts and endpoint_alerts[0]["kill_comparable"] is False

    # The live curve is already better than peers were at the SAME rung, even though it is naturally
    # below their finished endpoints. The old endpoint-only comparison killed this healthy improving run.
    # Peers contribute their rung-1 START-of-band checkpoint via the durable resource_curve (step 1 ->
    # rung 1, step 10 -> rung 8); a tail is never substituted.
    improving = {}
    peer_resource_curves = [
        [[1.0, 0.05], [8.0, 0.80]],
        [[1.0, 0.07], [8.0, 0.70]],
        [[1.0, 0.09], [8.0, 0.60]],
    ]
    stub = _AshaStub(kill=True, min_siblings=3, cadence=0.01)
    _run_loop(stub, wd, spec, "max", improving, monkeypatch,
              finals=[0.8, 0.7, 0.6], curves=peer_resource_curves, window=0.2)
    assert improving.get("kill") is not True
    alerts = [d for event, d in stub.store.events if event == EV_ASHA_RANK]
    assert alerts and alerts[0]["kill_comparable"] is True  # endpoint warning remains diagnostic
    assert alerts[0]["endpoint_underperforming"] is True
    assert alerts[0]["resource_underperforming"] is False

    # With enough truly same-resource evidence AND a confident stop verdict from the judge, persistent
    # underperformance can still free compute. The rank flag alone no longer stops anything: the judge
    # is what turns the evidence into a decision (see the LLM-mediated section below).
    (wd / "train.log").write_text('{"recall": 0.01, "step": 1}\n', encoding="utf-8")
    on = {}
    stopper = _JudgeClient({"status": "stop", "reason": "flat at 1% while peers were at 5-9%",
                            "confidence": 0.95})
    _run_loop(_AshaStub(kill=True, min_siblings=3, cadence=0.01, judge=stopper), wd, spec, "max", on,
              monkeypatch, finals=[0.8, 0.7, 0.6], curves=peer_resource_curves, window=0.2,
              until=lambda s: bool(on.get("kill")))
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

    # A judge that WOULD stop this node: the ASHA watchdog must still lose the claim to the training
    # monitor's earlier one, so the node keeps exactly one terminal explanation.
    _run_loop(
        _AshaStub(kill=True, min_siblings=3, cadence=0.01,
                  judge=_JudgeClient({"status": "stop", "reason": "far behind at the same step",
                                      "confidence": 0.99})), wd,
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


def test_asha_verdict_is_diagnostic():
    # The judge runs in a concurrent per-eval task, so its row's splice position is thread-dependent:
    # it MUST be fold-ignored. The kill itself is carried by the node's ordinary single terminal.
    assert EV_ASHA_VERDICT in DIAGNOSTIC_EVENTS


# ------------------------------------------------------- the LLM judge: pure decision surface

def test_rank_bar_is_the_ordering_the_rank_test_uses():
    # The bar handed to the judge must be exactly the one `asha_underperforming` compares against —
    # one spelling of the WORST->BEST ordering, so the prompt can never describe a different test.
    pop = [0.1, 0.2, 0.3, 0.9]
    for direction in ("min", "max"):
        for q in (0.0, 0.25, 0.5, 1.0):
            bar = rank_bar(pop, direction, quantile=q)
            worse = bar + 0.01 if direction == "min" else bar - 0.01
            better = bar - 0.01 if direction == "min" else bar + 0.01
            assert asha_underperforming(worse, pop, direction, quantile=q) is True
            assert asha_underperforming(better, pop, direction, quantile=q) is False
    assert rank_bar([], "min") is None and rank_bar(pop, "min", quantile=1.5) is None


@pytest.mark.parametrize("status", ["continue", "watch", "stop"])
def test_should_asha_kill_never_widens_beyond_the_rank_test(status):
    """THE core safety property: whatever the judge says, no kill without the deterministic rank flag.

    The loop only reaches the judge inside the rank gate, and this predicate re-requires that same bit —
    so a model that answers 'stop' to everything (or a prompt-injected log that talks it into stopping)
    can still only ever stop a node the old quantile test would ALSO have stopped."""
    confident = AshaVerdict(status=status, reason="whatever", confidence=1.0)
    for rank in (False, None):                    # rank test says "fine" / "cannot decide"
        assert should_asha_kill(confident, enabled=True, threshold=0.8,
                                rank_underperforming=rank) is False
    # With the rank flag set, ONLY a confident 'stop' acts — the LLM narrows, never widens.
    assert should_asha_kill(confident, enabled=True, threshold=0.8,
                            rank_underperforming=True) is (status == "stop")


def test_should_asha_kill_requires_enabled_confident_and_present_verdict():
    stop = AshaVerdict(status="stop", reason="hopeless", confidence=0.9)
    assert should_asha_kill(stop, enabled=True, threshold=0.8, rank_underperforming=True) is True
    assert should_asha_kill(stop, enabled=False, threshold=0.8, rank_underperforming=True) is False
    assert should_asha_kill(stop, enabled=True, threshold=0.95, rank_underperforming=True) is False
    # No verdict at all (no client / endpoint failure / call cap) is NOT authority to kill.
    assert should_asha_kill(None, enabled=True, threshold=0.0, rank_underperforming=True) is False


@pytest.mark.parametrize("bar", [0.0, 0.5, 0.8, 1.0])
def test_the_confidence_bar_is_inclusive_at_exactly_the_threshold(bar):
    """`confidence >= threshold` is the documented contract, and the operator-facing knob is spelled
    as a MINIMUM. `>` instead of `>=` is invisible everywhere except at equality — and equality is
    precisely where an operator who set the bar to their model's typical confidence lands, so the
    off-by-one silently disables the intervention they configured.

    Both directions, so neither comparison can be widened either: one ulp under the bar never kills.
    """
    import math

    at_the_bar = AshaVerdict(status="stop", reason="hopeless", confidence=bar)
    assert should_asha_kill(at_the_bar, enabled=True, threshold=bar,
                            rank_underperforming=True) is True, f"confidence == threshold == {bar}"
    if bar > 0.0:
        just_under = AshaVerdict(status="stop", reason="hopeless",
                                 confidence=math.nextafter(bar, 0.0))
        assert should_asha_kill(just_under, enabled=True, threshold=bar,
                                rank_underperforming=True) is False, f"just under {bar}"


@pytest.mark.parametrize("confidence", [float("nan"), float("inf"), None, "high"])
def test_non_finite_judge_confidence_cannot_kill_even_at_zero_threshold(confidence):
    # `min(1.0, NaN)` is 1.0 in Python: a non-finite confidence must be treated as INVALID, never as
    # maximal authority (the same trap `train_monitor._normalize_monitor_confidence` exists for).
    verdict = AshaVerdict.model_construct(status="stop", reason="x", confidence=confidence)
    assert should_asha_kill(verdict, enabled=True, threshold=0.0, rank_underperforming=True) is False


@pytest.mark.parametrize("threshold", [float("nan"), None, object(), [0.8], "not a number"])
def test_an_unreadable_threshold_fails_closed(threshold):
    """An operator/Strategist knob that is not a usable number must SPARE the node, and must not
    raise on the way there.

    The NaN arm is deliberately an OUTCOME assertion, not a mechanism one: IEEE says
    `1.0 >= nan` is already False, so `should_asha_kill`'s explicit `bar == bar` guard cannot change
    the answer and a test written against it guards a dead branch. (It stays in the source as the
    readable statement of intent, next to `_normalize_monitor_confidence`, whose NaN handling is
    NOT redundant — `min(1.0, nan)` is 1.0.)

    The arms with teeth are the non-numeric ones: `float(threshold)` raises TypeError/ValueError on
    them, and the `except … return False` that turns that into "do not kill" is the only thing
    between a mistyped knob and an exception raised inside the watchdog's tick — where the blanket
    per-tick handler swallows it and the watcher just silently stops deciding.
    """
    stop = AshaVerdict(status="stop", reason="hopeless", confidence=1.0)
    assert should_asha_kill(stop, enabled=True, threshold=threshold,
                            rank_underperforming=True) is False


# ------------------------------------------------------- the judge's context (pure)

def test_latest_extra_metrics_reads_the_newest_metric_record():
    log = ('{"recall": 0.1, "step": 1, "loss": 2.5, "lr": 0.01, "note": "warmup"}\n'
           '{"recall": 0.2, "step": 2, "loss": 1.5, "lr": 0.01}\n')
    spec = {"kind": "stdout_json", "key": "recall", "resource_key": "step"}
    # the metric and the declared resource are already context; everything else numeric is extra.
    assert latest_extra_metrics(log, spec) == {"loss": 1.5, "lr": 0.01}
    assert latest_extra_metrics(log, {"kind": "stdout_regex", "pattern": "x"}) == {}
    assert latest_extra_metrics("", spec) == {} and latest_extra_metrics("noise", spec) == {}
    # non-numeric values are dropped by the shared extra_metrics contract, never rendered as text.
    assert "note" not in latest_extra_metrics('{"recall": 0.1, "note": "hi", "loss": 2.0}\n',
                                              {"kind": "stdout_json", "key": "recall"})


def test_latest_train_verdict_matches_only_this_node_lifecycle():
    def _rows(*payloads):
        return [Event(seq=i, ts=0.0, type=EV_TRAIN_MONITOR_ALERT, data=dict(p))
                for i, p in enumerate(payloads)]

    healthy = {"node_id": 4, "generation": 1, "status": "healthy", "reason": "loss falling",
               "confidence": 0.7}
    broken = {"node_id": 4, "generation": 1, "status": "broken", "reason": "loss is nan",
              "confidence": 0.9}
    assert latest_train_verdict(_rows(healthy, broken), 4, 1) == {
        "status": "broken", "reason": "loss is nan", "confidence": 0.9}
    assert latest_train_verdict(_rows(broken), 4, 0) is None       # a stale generation is not evidence
    assert latest_train_verdict(_rows(broken), 5, 1) is None       # another node's verdict is not ours
    assert latest_train_verdict([], 4, 1) is None
    # Untrusted rows: a bool node_id, a string generation, an unknown status -> no verdict, not a guess.
    assert latest_train_verdict(_rows({**broken, "node_id": True}), 1, 1) is None
    assert latest_train_verdict(_rows({**broken, "generation": "1"}), 4, 1) is None
    assert latest_train_verdict(_rows({**broken, "status": "melted"}), 4, 1) is None
    # A non-finite confidence stays out of the dict instead of being reported as 0.0-with-authority.
    assert latest_train_verdict(_rows({**broken, "confidence": float("nan")}), 4, 1) == {
        "status": "broken", "reason": "loss is nan"}


def test_judge_context_carries_curve_bar_direction_extras_and_train_verdict():
    context = asha_judge_context(
        node_id=7, generation=0, direction="max", metric_key="recall",
        sample=IntermediateSample(value=0.01, resource_key="step", resource=2.0),
        live_curve=[[1.0, 0.005], [2.0, 0.01]],
        comparable_population=[0.05, 0.07, 0.09], quantile=0.5, under_streak=3,
        endpoint_population=[0.6, 0.7, 0.8],
        extra_metrics={"loss": 2.31}, train_verdict={"status": "broken", "confidence": 0.9,
                                                     "reason": "loss is nan since step 40"})
    assert "MAXIMIZE `recall`" in context and "higher is better" in context
    assert "1 -> 0.005, 2 -> 0.01" in context                    # the SHAPE of the live curve
    assert "step=2" in context and "recall=0.01" in context
    assert "loss=2.31" in context                                # extra_metrics the rank test ignored
    assert "0.05, 0.07, 0.09" in context                         # same-resource peers, worst -> best
    assert "50% bar is 0.07" in context                          # the exact bar that flagged this node
    assert "0.6, 0.7, 0.8" in context and "background" in context  # endpoints, explicitly discounted
    assert "3 consecutive checks" in context                     # how long the flag has held
    assert "broken" in context and "loss is nan since step 40" in context   # the train-monitor verdict


def test_judge_context_degrades_without_optional_evidence():
    # No curve, no extras, no train verdict, no resource on the sample: the judge simply gets less.
    context = asha_judge_context(
        node_id=1, generation=0, direction="min", metric_key="loss",
        sample=IntermediateSample(value=1.5), live_curve=None,
        comparable_population=[], quantile=0.5, under_streak=3, endpoint_population=[])
    assert "MINIMIZE `loss`" in context and "loss=1.5" in context
    assert "bar" not in context and "background" not in context


# ------------------------------------------------------- the judge inside the loop

def test_asha_verdict_needs_a_client_and_goes_through_the_shared_structured_judge():
    offline = _AshaStub()                      # no developer at all (toy / offline path)
    assert offline._asha_verdict("evidence") is None
    live = _AshaStub(judge=_JudgeClient({"status": "watch", "reason": "still improving",
                                         "confidence": 0.4}))
    verdict = live._asha_verdict("evidence")
    assert isinstance(verdict, AshaVerdict) and verdict.status == "watch"
    assert live.judge.calls == 1 and "evidence" in live.judge.contexts[0]
    # An endpoint failure is swallowed into "no verdict" — which cannot kill.
    assert _AshaStub(judge=_JudgeClient(None))._asha_verdict("evidence") is None


def _kill_setup(tmp_path):
    """A node whose live curve is genuinely below three same-rung peers — the rank test fires."""
    wd = tmp_path / "node_0"
    wd.mkdir()
    (wd / "train.log").write_text('{"recall": 0.01, "step": 1}\n', encoding="utf-8")
    curves = [[[1.0, 0.05], [8.0, 0.80]], [[1.0, 0.07], [8.0, 0.70]], [[1.0, 0.09], [8.0, 0.60]]]
    return wd, {"kind": "stdout_json", "key": "recall", "resource_key": "step"}, curves


@pytest.mark.parametrize("verdict,killed", [
    ({"status": "stop", "reason": "flat while peers climbed", "confidence": 0.95}, True),
    ({"status": "stop", "reason": "maybe hopeless", "confidence": 0.5}, False),   # below threshold
    ({"status": "watch", "reason": "behind but still climbing", "confidence": 0.99}, False),
    ({"status": "continue", "reason": "gap is inside the peer spread", "confidence": 0.99}, False),
    (None, False),                                       # endpoint failure => no verdict => no kill
])
def test_kill_happens_only_on_a_confident_stop_verdict(tmp_path, monkeypatch, verdict, killed):
    wd, spec, curves = _kill_setup(tmp_path)
    signal = {}
    judge = _JudgeClient(verdict)
    stub = _AshaStub(kill=True, min_siblings=3, cadence=0.01, judge=judge)
    _run_loop(stub, wd, spec, "max", signal, monkeypatch, finals=[0.8, 0.7, 0.6], curves=curves,
              window=0.2, until=(lambda s: bool(signal.get("kill"))) if killed else None)

    assert bool(signal.get("kill")) is killed
    assert judge.calls >= 1, "the rank flag must reach the judge"
    rows = [d for (t, d) in stub.store.events if t == EV_ASHA_VERDICT]
    assert rows, "every judged rank flag records its decision, kill or not"
    assert rows[0]["kill"] is killed
    assert rows[0]["status"] == (verdict["status"] if verdict else "unavailable")
    if killed:
        assert signal.get("terminal_reason") == "asha_underperforming"
        # the judge's own words ride the terminal explanation, next to the deterministic evidence.
        assert "flat while peers climbed" in signal.get("reason", "")
        assert "sibling observations at the same resource" in signal.get("reason", "")


@pytest.mark.parametrize("knob", ["none", "missing", "not-a-number", "bool"])
def test_an_unset_kill_confidence_knob_never_becomes_a_zero_threshold(tmp_path, monkeypatch, knob):
    """The fail-safe behind `_monitor_asha`'s deliberate no-`or`-coercion read of the knob.

    `float(x or 0.0)`/`else 0.0` would turn an unset, None, non-numeric or bool `asha_live_kill_confidence`
    into a ZERO bar — i.e. EVERY `stop` verdict kills, at any confidence the model happened to emit.
    Now that the intervention ships on by default, that is the wrong direction to fail in, and it is a
    silent one: nothing about a killed node says the threshold was zero. The documented default (0.8)
    is what an absent/unreadable knob must resolve to.
    """
    wd, spec, curves = _kill_setup(tmp_path)
    signal = {}
    # A genuine 'stop', but nowhere near the 0.8 default. It kills iff the bar collapsed to zero.
    judge = _JudgeClient({"status": "stop", "reason": "hard to say", "confidence": 0.1})
    stub = _AshaStub(kill=True, min_siblings=3, cadence=0.01, judge=judge)
    if knob == "missing":
        del stub._asha_live_kill_confidence          # never configured at all
    else:
        stub._asha_live_kill_confidence = {"none": None, "not-a-number": "0.9",
                                           "bool": True}[knob]

    # SETTLE on the verdict row instead of a fixed 0.2s slice. This test's precondition is that the
    # judge was actually consulted, and a fixed window makes that a race the loaded full-suite run
    # loses: observed failing once in a 17-minute serial gate ("precondition: ... the judge was
    # consulted", judge.calls == 0) while passing 6/6 standalone. The predicate is the VERDICT row,
    # not `judge.calls`: the row is appended strictly AFTER `kill = stop_decided and
    # claim_watchdog_kill(...)`, so the "no kill" assertion below cannot pass merely because the loop
    # stopped before the decision was made. Its sibling
    # `test_the_judge_is_never_consulted_where_the_rank_test_would_not_kill` keeps its fixed window
    # on purpose — it proves an ABSENCE, and there is no event whose arrival could settle that.
    _run_loop(stub, wd, spec, "max", signal, monkeypatch, finals=[0.8, 0.7, 0.6], curves=curves,
              until=lambda s: any(t == EV_ASHA_VERDICT for (t, _d) in s.store.events))

    assert judge.calls >= 1, "precondition: the rank gate held and the judge was consulted"
    assert signal.get("kill") is not True, (
        f"an {knob!r} kill-confidence knob became a zero threshold and a 0.1-confidence stop killed "
        "the node")
    rows = [d for (t, d) in stub.store.events if t == EV_ASHA_VERDICT]
    assert rows and rows[0]["kill"] is False, rows


def test_the_judge_is_never_consulted_where_the_rank_test_would_not_kill(tmp_path, monkeypatch):
    """The LLM cannot widen the kill set: with a judge that stops EVERYTHING, none of the cases the
    deterministic rank test spares even reaches it."""
    wd = tmp_path / "node_0"
    wd.mkdir()
    always_stop = {"status": "stop", "reason": "stop everything", "confidence": 1.0}
    spec = {"kind": "stdout_json", "key": "recall", "resource_key": "step"}
    curves = [[[1.0, 0.05], [8.0, 0.80]], [[1.0, 0.07], [8.0, 0.70]], [[1.0, 0.09], [8.0, 0.60]]]

    # (a) the live curve is BETTER than the peers were at the same rung -> no rank flag.
    (wd / "train.log").write_text('{"recall": 0.30, "step": 1}\n', encoding="utf-8")
    healthy_signal, healthy_judge = {}, _JudgeClient(always_stop)
    _run_loop(_AshaStub(kill=True, min_siblings=3, cadence=0.01, judge=healthy_judge), wd, spec,
              "max", healthy_signal, monkeypatch, finals=[0.8, 0.7, 0.6], curves=curves, window=0.2)
    assert healthy_signal.get("kill") is not True and healthy_judge.calls == 0

    # (b) no declared resource_key -> only an endpoint rank, which may never stop a run.
    (wd / "train.log").write_text('{"recall": 0.01}\n', encoding="utf-8")
    endpoint_signal, endpoint_judge = {}, _JudgeClient(always_stop)
    _run_loop(_AshaStub(kill=True, min_siblings=3, cadence=0.01, judge=endpoint_judge), wd,
              {"kind": "stdout_json", "key": "recall"}, "max", endpoint_signal, monkeypatch,
              finals=[0.8, 0.7, 0.6], curves=curves, window=0.2)
    assert endpoint_signal.get("kill") is not True and endpoint_judge.calls == 0

    # (c) too few same-resource peers -> the evidence floor is not met.
    (wd / "train.log").write_text('{"recall": 0.01, "step": 1}\n', encoding="utf-8")
    thin_signal, thin_judge = {}, _JudgeClient(always_stop)
    _run_loop(_AshaStub(kill=True, min_siblings=3, cadence=0.01, judge=thin_judge), wd, spec, "max",
              thin_signal, monkeypatch, finals=[0.8, 0.7, 0.6], curves=[curves[0], None, None],
              window=0.2)
    assert thin_signal.get("kill") is not True and thin_judge.calls == 0

    # (d) the intervention itself is off -> advisory only, and no paid call is made either.
    off_signal, off_judge = {}, _JudgeClient(always_stop)
    _run_loop(_AshaStub(kill=False, min_siblings=3, cadence=0.01, judge=off_judge), wd, spec, "max",
              off_signal, monkeypatch, finals=[0.8, 0.7, 0.6], curves=curves, window=0.2)
    assert off_signal.get("kill") is not True and off_judge.calls == 0


def test_grace_window_is_re_armed_when_the_judge_spares_the_node(tmp_path, monkeypatch):
    """A spared node must earn the next consult with a fresh streak — otherwise the judge is asked
    every tick until repetition eventually produces a 'stop' out of a borderline curve."""
    wd, spec, curves = _kill_setup(tmp_path)
    judge = _JudgeClient({"status": "watch", "reason": "still early", "confidence": 0.9})
    stub = _AshaStub(kill=True, min_siblings=3, cadence=0.01, judge=judge)
    # Wait for the durable ROWS, not for `judge.calls`: a SPARED verdict's append is deliberately
    # unshielded, so cancelling the instant the second call returns can preempt the row it is about
    # to write and read as `judge.calls == 2, ticks == 1` on a loaded host. Same property, no race.
    def _rows(s):
        return [d for (t, d) in s.store.events if t == EV_ASHA_VERDICT]

    _run_loop(stub, wd, spec, "max", {}, monkeypatch, finals=[0.8, 0.7, 0.6], curves=curves,
              window=0.3, until=lambda s: len(_rows(s)) >= 2)
    ticks = len(_rows(stub))
    assert judge.calls == ticks >= 1
    # Every consult costs a full grace window (>2 underperforming ticks) — never one call per tick.
    for row in [d for (t, d) in stub.store.events if t == EV_ASHA_VERDICT]:
        assert row["under_streak"] == 3


def test_judge_sees_the_train_monitor_verdict_when_one_exists(tmp_path, monkeypatch):
    wd, spec, curves = _kill_setup(tmp_path)
    judge = _JudgeClient({"status": "stop", "reason": "nan loss confirms it", "confidence": 0.9})
    stub = _AshaStub(kill=True, min_siblings=3, cadence=0.01, judge=judge)
    # The sibling watchdog already published its health verdict for THIS node lifecycle …
    stub.store.events.append((EV_TRAIN_MONITOR_ALERT, {
        "node_id": 0, "generation": 0, "status": "broken",
        "reason": "loss is nan since step 40", "confidence": 0.92}))
    # … and one for a DIFFERENT node, which must not leak into this node's decision.
    stub.store.events.append((EV_TRAIN_MONITOR_ALERT, {
        "node_id": 9, "generation": 0, "status": "healthy", "reason": "other node is fine"}))
    signal = {}
    _run_loop(stub, wd, spec, "max", signal, monkeypatch, finals=[0.8, 0.7, 0.6], curves=curves,
              window=0.2, until=lambda s: bool(signal.get("kill")))

    assert judge.contexts, "the judge must have been consulted"
    context = judge.contexts[0]
    assert "loss is nan since step 40" in context and "broken" in context
    assert "other node is fine" not in context
    rows = [d for (t, d) in stub.store.events if t == EV_ASHA_VERDICT]
    assert rows and rows[0]["train_monitor_status"] == "broken"   # legible in the durable record too


def test_judge_context_reaches_the_prompt_without_a_train_verdict(tmp_path, monkeypatch):
    wd, spec, curves = _kill_setup(tmp_path)
    judge = _JudgeClient({"status": "watch", "reason": "early", "confidence": 0.6})
    stub = _AshaStub(kill=True, min_siblings=3, cadence=0.01, judge=judge)
    _run_loop(stub, wd, spec, "max", {}, monkeypatch, finals=[0.8, 0.7, 0.6], curves=curves,
              window=0.2, until=lambda s: judge.calls >= 1)
    context = judge.contexts[0]
    assert "health monitor" not in context          # nothing invented when the monitor never spoke
    assert "recall=0.01" in context and "step=1" in context
    assert "0.05, 0.07, 0.09" in context and "50% bar is 0.07" in context


def test_kill_decision_appends_only_diagnostics_never_a_terminal(tmp_path, monkeypatch):
    """Invariant #2 at this watchdog's boundary: the monitor NEVER writes a terminal. It records
    fold-ignored diagnostics and hands the decision to `_evaluate` through `kill_signal`, which writes
    the node's single `node_failed`."""
    wd, spec, curves = _kill_setup(tmp_path)
    signal = {}
    stub = _AshaStub(kill=True, min_siblings=3, cadence=0.01,
                     judge=_JudgeClient({"status": "stop", "reason": "hopeless", "confidence": 0.95}))
    _run_loop(stub, wd, spec, "max", signal, monkeypatch, finals=[0.8, 0.7, 0.6], curves=curves,
              window=0.2, until=lambda s: bool(signal.get("kill")))

    assert signal.get("kill") is True
    kinds = {t for (t, _d) in stub.store.events}
    assert kinds <= {EV_ASHA_RANK, EV_ASHA_VERDICT}, f"watchdog wrote non-diagnostic events: {kinds}"
    assert kinds <= DIAGNOSTIC_EVENTS
    # …and it stops watching after claiming, so a second tick cannot re-decide or re-spend.
    assert len([d for (t, d) in stub.store.events if t == EV_ASHA_VERDICT]) == 1


def test_verdict_reason_is_redacted_before_storage(tmp_path, monkeypatch):
    wd, spec, curves = _kill_setup(tmp_path)
    stub = _AshaStub(kill=True, min_siblings=3, cadence=0.01,
                     judge=_JudgeClient({"status": "stop", "reason": "token sk-secret in the log",
                                         "confidence": 0.95}))
    stub._redact = lambda text: text.replace("sk-secret", "[redacted]")
    signal = {}
    _run_loop(stub, wd, spec, "max", signal, monkeypatch, finals=[0.8, 0.7, 0.6], curves=curves,
              window=0.2, until=lambda s: bool(signal.get("kill")))
    rows = [d for (t, d) in stub.store.events if t == EV_ASHA_VERDICT]
    assert rows and "sk-secret" not in rows[0]["reason"] and "[redacted]" in rows[0]["reason"]
    assert "sk-secret" not in signal.get("reason", "")



# ---------------------------------------- the durable log: one terminal, splice-neutral, offline replay

def _kill_against_a_real_log(tmp_path, judge, *, extra_events=()):
    """Drive the REAL watchdog loop against a REAL `EventStore` — no monkeypatched fold, no stub store.

    The log is a genuine run prefix: a finished sibling whose `node_evaluated` carries the durable
    per-rung `resource_curve` the rank test reads. The loop therefore ranks against real folded state and,
    on a confident stop verdict, fills `kill_signal` exactly as it does in an eval. Returns the store and
    the signal so the caller can assert what the DURABLE log looks like afterwards.
    """
    from looplab.events.eventstore import EventStore

    workdir = tmp_path / "node_1"
    workdir.mkdir()
    (workdir / "train.log").write_text('{"metric": 0.01, "step": 1}\n', encoding="utf-8")
    store = EventStore(tmp_path / "events.jsonl")
    store.append("run_started", {"run_id": "t", "task_id": "dr", "goal": "g", "direction": "max"})
    for node_id in (0, 1):
        store.append("node_created", {"node_id": node_id, "parent_ids": [], "operator": "draft",
                                      "idea": {"operator": "draft", "params": {}}})
    store.append("node_evaluated", {"node_id": 0, "metric": 0.95,
                                    "resource_curve": [[1.0, 0.90], [8.0, 0.95]]})
    for event_type, data in extra_events:
        store.append(event_type, dict(data))

    stub = _AshaStub(kill=True, min_siblings=1, cadence=0.01, judge=judge)
    stub.store = store
    signal: dict = {}

    async def drive():
        cancel = threading.Event()
        async with anyio.create_task_group() as tg:
            tg.start_soon(AshaMonitorMixin._monitor_asha, stub, 1, 0, str(workdir), cancel,
                          {"kind": "stdout_json", "key": "metric", "resource_key": "step"},
                          "max", signal, None)
            deadline = time.monotonic() + _LOOP_SETTLE_TIMEOUT_S
            while not signal.get("kill") and time.monotonic() < deadline:
                await anyio.sleep(0.005)
            tg.cancel_scope.cancel()

    anyio.run(drive)
    return store, signal


def test_a_judged_kill_leaves_one_terminal_and_a_replay_that_never_calls_the_llm(tmp_path, monkeypatch):
    """The durable end of the contract, on a real event log.

    Deliberately stops at the boundary this watchdog owns: it fills `kill_signal` and `_evaluate` writes
    the node's ONE terminal (`engine/evaluate.py`, the `kill_signal.get("kill") and not ok` branch —
    a single append followed by `return`). So the log here is exactly what that produces, and what must
    hold of it is that the watchdog contributed only fold-IGNORED rows, that the node has exactly one
    terminal, and that replaying it reconstructs the failed node WITHOUT the judge.
    """
    from looplab.events.replay import fold

    judge = _JudgeClient({"status": "stop", "reason": "flat at 0.01 while the peer was at 0.90",
                          "confidence": 0.95})
    store, signal = _kill_against_a_real_log(tmp_path, judge)
    assert signal.get("kill") is True and signal.get("terminal_reason") == "asha_underperforming"
    assert judge.calls >= 1, "the rank flag must have reached the judge"

    # The watchdog itself contributed ONLY fold-ignored diagnostics …
    watchdog_rows = [e for e in store.read_all() if e.type in (EV_ASHA_RANK, EV_ASHA_VERDICT)]
    assert watchdog_rows and {e.type for e in watchdog_rows} <= DIAGNOSTIC_EVENTS
    verdicts = [e.data for e in watchdog_rows if e.type == EV_ASHA_VERDICT]
    assert verdicts and verdicts[0]["status"] == "stop" and verdicts[0]["kill"] is True
    assert not [e for e in store.read_all() if e.type in ("node_evaluated", "node_failed")
                and e.data.get("node_id") == 1]

    # … and the terminal `_evaluate` writes from that signal is spliced in HERE, by hand, so the rest
    # of this test can assert what the durable LOG looks like around it (one terminal, splice-neutral
    # diagnostics, offline replay). That the real writer produces exactly this row is a separate
    # property with its own test — `test_the_engine_writes_the_one_terminal_the_watchdog_asked_for`
    # below drives `engine/evaluate.py`'s `kill_signal` branch — because a test that appends the row
    # it then asserts about proves nothing about the code that is supposed to append it.
    store.append("node_failed", {"node_id": 1, "generation": 0, "reason": "asha_underperforming",
                                 "error": "live watchdog stopped the run early: "
                                          + str(signal.get("reason", ""))[:400],
                                 "eval_seconds": 1.0})
    events = list(store.read_all())
    terminals = [e for e in events if e.type in ("node_evaluated", "node_failed")
                 and e.data.get("node_id") == 1]
    assert len(terminals) == 1 and terminals[0].type == "node_failed"
    assert "flat at 0.01" in terminals[0].data["error"]      # the judge's words ride the terminal

    # REPLAY reads that terminal and never re-invokes the judge. Break every path into the model first:
    # if the fold touched one, this raises instead of quietly making a paid call.
    def _boom(*_a, **_k):
        raise AssertionError("replay must never invoke the ASHA judge")

    monkeypatch.setattr(AshaMonitorMixin, "_asha_verdict", _boom)
    monkeypatch.setattr("looplab.core.parse.parse_structured", _boom)
    monkeypatch.setattr("looplab.trust.judge.structured_judge", _boom)
    calls_before = judge.calls
    state = fold(events)
    assert state.nodes[1].status is NodeStatus.failed
    assert state.nodes[1].error_reason == "asha_underperforming"
    assert state.nodes[0].status is NodeStatus.evaluated and state.nodes[0].metric == 0.95
    assert judge.calls == calls_before, "replay must be offline"

    # SPLICE-NEUTRALITY: the judge appends from a CONCURRENT per-eval task, so its row's byte position is
    # thread-dependent. Fold-ignored means that cannot matter — moving every EV_ASHA_VERDICT row, or
    # dropping it as a pre-upgrade log has, must leave folded state identical.
    def _projection(rows):
        st = fold(list(rows))
        return [(n.id, n.status, n.metric, n.error_reason) for n in st.nodes.values()]

    baseline = _projection(events)
    assert _projection([e for e in events if e.type != EV_ASHA_VERDICT]) == baseline
    assert _projection([e for e in events if e.type == EV_ASHA_VERDICT]
                       + [e for e in events if e.type != EV_ASHA_VERDICT]) == baseline
    # A duplicate terminal from a corrupt/replayed log still folds to the SAME node (first wins).
    assert _projection(events + [terminals[0]]) == baseline


def _watchdog_killed_engine(tmp_path, *, reason: str):
    """A REAL `Engine` on a real command-eval task, whose ASHA watchdog task is replaced by one that
    immediately fills `kill_signal` and cancels — so `engine/evaluate.py` reaches its `kill_signal`
    branch and writes the terminal itself.

    The eval command sleeps; the watchdog's `cancel` tree-kills it, so the eval comes back with no
    metric (`ok` False) exactly as it does after a real early kill.
    """
    import sys

    from looplab.adapters.repo_task import EvalSpec, RepoTask
    from looplab.engine.orchestrator import Engine
    from looplab.events.eventstore import EventStore
    from looplab.runtime.sandbox import SubprocessSandbox
    from looplab.search.policy import GreedyTree

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "run.py").write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    (repo / "params.txt").write_text("x=0\n", encoding="utf-8")
    task = RepoTask(id="asha_kill", goal="raise the metric", direction="max",
                    editable_path=str(repo), edit_surface=["*.txt"],
                    eval=EvalSpec(command=[sys.executable, "run.py"],
                                  metric={"kind": "stdout_json", "key": "metric"}))

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    store = EventStore(run_dir / "events.jsonl")
    store.append("run_started", {"run_id": "r", "task_id": task.id, "goal": "g", "direction": "max"})
    store.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                                  "idea": {"operator": "draft", "params": {}, "rationale": ""},
                                  "code": ""})
    researcher, developer = task.build_roles()
    engine = Engine(run_dir, task=task, researcher=researcher, developer=developer,
                    sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=2),
                    role_factory=task.build_roles, asha_live=True, asha_live_kill=True,
                    auto_install_deps=False)

    async def _stop_at_once(_self, _node_id, _generation, _workdir, cancel, _spec, _direction,
                            kill_signal=None, _log_snapshot=None, _log_plan=None):
        kill_signal.update({"kill": True, "reason": reason,
                            "terminal_reason": "asha_underperforming"})
        cancel.set()

    engine._monitor_asha = _stop_at_once.__get__(engine, type(engine))
    return engine, store


def test_the_engine_writes_the_one_terminal_the_watchdog_asked_for(tmp_path):
    """The other half of the contract, driven through the REAL writer (`engine/evaluate.py`, the
    `kill_signal.get("kill") and not ok` branch) rather than hand-appended by the test.

    Two things must survive the handoff and neither is the watchdog's to enforce: the judge's own
    words ride the terminal (they are the entire explanation an operator gets for a training that was
    stopped early), and `terminal_reason` reaches the fold as `asha_underperforming` — degrading it to
    the training monitor's default `monitor_broken` misattributes the kill to the other watchdog and
    tells failure-reflection the wrong thing about WHY the node died.
    """
    from looplab.events.replay import fold

    words = "flat at 0.01 while the peer was at 0.90"
    engine, store = _watchdog_killed_engine(tmp_path, reason=words)
    anyio.run(engine._evaluate, 0, anyio.CapacityLimiter(1))

    events = list(store.read_all())
    terminals = [e for e in events if e.type in ("node_evaluated", "node_failed")
                 and e.data.get("node_id") == 0]
    assert len(terminals) == 1 and terminals[0].type == "node_failed", [e.type for e in events]
    assert terminals[0].data["reason"] == "asha_underperforming", terminals[0].data
    assert words in terminals[0].data["error"], terminals[0].data

    state = fold(events)
    assert state.nodes[0].status is NodeStatus.failed
    assert state.nodes[0].error_reason == "asha_underperforming"


def test_the_judge_reads_the_train_monitor_verdict_off_a_real_log(tmp_path):
    """`latest_train_verdict` against a real store, inside the real loop: the sibling watchdog's row is a
    DIAGNOSTIC event the fold never carries, so this is the one path that proves the judge still sees it."""
    judge = _JudgeClient({"status": "stop", "reason": "the nan loss confirms it", "confidence": 0.9})
    _store, signal = _kill_against_a_real_log(
        tmp_path, judge,
        extra_events=[(EV_TRAIN_MONITOR_ALERT,
                       {"node_id": 1, "generation": 0, "status": "broken",
                        "reason": "loss is nan since step 40", "confidence": 0.92}),
                      (EV_TRAIN_MONITOR_ALERT,
                       {"node_id": 7, "generation": 0, "status": "healthy",
                        "reason": "another node is fine", "confidence": 0.8})])
    assert signal.get("kill") is True
    context = judge.contexts[0]
    assert "loss is nan since step 40" in context and "broken" in context
    assert "another node is fine" not in context
