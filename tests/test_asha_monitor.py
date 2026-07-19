"""ASHA live-curve watchdog: pure rank/extraction helpers + the advisory/opt-in-kill loop. The loop is
advisory-only unless `_asha_live_kill`, appends only the fold-IGNORED EV_ASHA_RANK, and reuses the
training monitor's kill_signal — so none of this touches folded selection or replay."""
import threading
import types

import anyio

from looplab.engine.asha_monitor import (
    AshaMonitorMixin, asha_underperforming, latest_intermediate, sibling_final_metrics,
)
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


# --------------------------------------------------------------------- sibling_final_metrics (pure)

def test_sibling_final_metrics_excludes_self_and_non_finite():
    state = types.SimpleNamespace(nodes={
        0: types.SimpleNamespace(metric=None),          # this node (pending) — excluded anyway
        1: types.SimpleNamespace(metric=0.8),
        2: types.SimpleNamespace(metric=0.6),
        3: types.SimpleNamespace(metric=float("inf")),  # non-finite — dropped
        4: types.SimpleNamespace(metric=None),          # not evaluated — dropped
    })
    assert sorted(sibling_final_metrics(state, node_id=0)) == [0.6, 0.8]


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
        return []

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


def _fake_state(finals, self_id=0):
    nodes = {self_id: types.SimpleNamespace(metric=None)}
    for i, m in enumerate(finals, start=1):
        nodes[i] = types.SimpleNamespace(metric=m)
    return types.SimpleNamespace(nodes=nodes)


def _run_loop(stub, workdir, spec, direction, kill_signal, monkeypatch, finals, *, window=0.12):
    monkeypatch.setattr("looplab.events.replay.fold", lambda events: _fake_state(finals))

    async def drive():
        cancel = threading.Event()
        async with anyio.create_task_group() as tg:
            tg.start_soon(AshaMonitorMixin._monitor_asha, stub, 0, 0, str(workdir), cancel,
                          spec, direction, kill_signal)
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


def test_loop_opt_in_kill_fires_only_when_enabled_and_past_grace(tmp_path, monkeypatch):
    wd = tmp_path / "node_0"
    wd.mkdir()
    (wd / "train.log").write_text('{"recall": 0.10}\n', encoding="utf-8")

    # kill OFF -> advisory only, no kill signal even though it underperforms.
    off = {}
    _run_loop(_AshaStub(kill=False, min_siblings=3), wd, {"kind": "stdout_json", "key": "recall"},
              "max", off, monkeypatch, finals=[0.8, 0.7, 0.6])
    assert off.get("kill") is not True

    # kill ON -> after the underperformance persists past the grace window, it tree-kills.
    on = {}
    _run_loop(_AshaStub(kill=True, min_siblings=3, cadence=0.01), wd,
              {"kind": "stdout_json", "key": "recall"}, "max", on, monkeypatch,
              finals=[0.8, 0.7, 0.6], window=0.2)
    assert on.get("kill") is True
    assert on.get("terminal_reason") == "asha_underperforming"


def test_asha_rank_is_diagnostic():
    assert EV_ASHA_RANK in DIAGNOSTIC_EVENTS      # fold-ignored -> splice-neutral by construction
