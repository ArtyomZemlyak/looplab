"""A training-monitor row that ACTED never also says the role or the curve withheld the action.

Review 2026-09-22, ENG3-09 (doc EM-04, still live). `_monitor_training` writes two COUNTERFACTUAL
receipts onto the durable `train_monitor_alert` row: `trajectory_veto` ("the measured curve is what
refused") and `kill_role_withheld` ("every conjunct cleared except the stage's role"). Both were
keyed on `not stop_decided` — but `stop_decided` is `(not repair_decided) and ...`, so a tick that
decided a REPAIR-stop left `stop_decided` False and both counterfactuals still armed. Driven by
`review/ENG3/em04.py`: a confident, cited `implementation` fault about a WORK-role stage produced ONE
row carrying `repair_decided`, `stop_decided`, `kill: true` AND `kill_role_withheld: "work"` — the
node was stopped and the record says the role prevented it. Every reader of the row (attention,
narration, "which watchdog stopped what") had to disbelieve one half of it.

The rule now: a counterfactual is asked only when the monitor did NOT act — `acted = stop_decided or
repair_decided`. The control below keeps the receipt it exists for: a stage whose role withheld a
kill that nothing else took still says so.
"""
from __future__ import annotations

import threading

import anyio

from looplab.core.tracing import JsonlSpanExporter, Tracer
from looplab.engine.train_monitor import (MONITOR_REPAIR_REASON, TrainingMonitorMixin,
                                          TrainingVerdict, eval_log_plan, snapshot_training_logs)

_SETTLE_S = 10.0


class _Store:
    def __init__(self):
        self.rows: list = []

    def read_all(self):
        return []

    def append(self, etype, data):
        self.rows.append((etype, dict(data)))


class _Host(TrainingMonitorMixin):
    pass


def _drive(tmp_path, verdict_fn, *, until, training_role=False,
           initial_log="step 1 loss: 0.5\nstep 2 loss: 0.5\n"):
    """The REAL `_monitor_training` over a three-stage pipeline. With NO declared `training` role,
    `train.log` is a WORK-role log: judged, repairable, never kill-eligible."""
    wd = tmp_path / "node_0"
    wd.mkdir()
    (wd / "train.py").write_text("LOSS = -1e9\n")        # what the verdict's citation points at
    # The training grant is AUTHORITY and needs a promised artifact (`eval_log_plan`); it is spent
    # once that artifact exists, so `model.pt` is deliberately never written here.
    train = ({"name": "train", "role": "training", "expect": {"files": ["model.pt"]}}
             if training_role else {"name": "train"})
    plan = eval_log_plan([{"name": "prep"}, train, {"name": "score"}])
    snap = snapshot_training_logs(wd)
    (wd / "train.log").write_text(initial_log)
    host = _Host()
    host.tracer = Tracer(JsonlSpanExporter(tmp_path / "spans.jsonl"))
    host.store = _Store()
    host._write_lock = anyio.Lock()
    host._train_monitor_kill = True
    host._monitor_cadence = lambda: 0.01
    host._training_verdict = lambda *a, **k: verdict_fn(wd)
    kill_signal: dict = {}

    async def main():
        cancel = threading.Event()
        async with anyio.create_task_group() as tg:
            tg.start_soon(host._monitor_training, 0, 0, str(wd), cancel, "", kill_signal, snap,
                          plan)
            with anyio.move_on_after(_SETTLE_S):
                while not until(host.store.rows):
                    await anyio.sleep(0.01)
            tg.cancel_scope.cancel()

    anyio.run(main)
    host.tracer.shutdown()
    return [d for _t, d in host.store.rows], kill_signal


def _broken(fault: str) -> TrainingVerdict:
    return TrainingVerdict(status="broken", fault=fault, reason="mask sentinel", confidence=0.95,
                           evidence_source="code", evidence_locator="train.py:1")


def test_a_repair_stop_row_does_not_also_say_the_role_withheld_the_kill(tmp_path):
    """MUTATION: key `role_withheld` back on `not stop_decided` -> the acting row carries
    `kill_role_withheld: "work"` beside `repair_decided` again."""
    rows, kill_signal = _drive(tmp_path, lambda _wd: _broken("implementation"),
                               until=lambda rows: any(d.get("repair_decided") for _t, d in rows))
    acted = [d for d in rows if d.get("repair_decided")]
    assert len(acted) == 1, rows
    row = acted[0]
    assert row["stop_decided"] is True and row["kill"] is True
    assert "kill_role_withheld" not in row, row
    assert "trajectory_veto" not in row, row
    assert kill_signal.get("terminal_reason") == MONITOR_REPAIR_REASON


def test_a_repair_stop_row_does_not_also_say_the_curve_vetoed_the_kill(tmp_path):
    """The other counterfactual, on the stage that CAN be killed: a descending curve vetoes the kill,
    an authenticated citation still decides the repair — and the row that says the monitor stopped
    the stage must not also say the measurement refused to. The log opens at 10.0 and each look
    appends a run of far lower values, so the second verdict sees a two-window descending
    trajectory (a fall is never an anomaly; only an explosion is). MUTATION: key `trajectory_veto`
    back on `not stop_decided` -> `trajectory_veto: true` beside `repair_decided`."""
    looks = {"n": 0}

    def _descending(wd):
        looks["n"] += 1
        with open(wd / "train.log", "a", encoding="utf-8") as fh:
            fh.writelines(f"step {10 * looks['n'] + i} loss: {1.0 / looks['n']:.6f}\n"
                          for i in range(9))
        return _broken("implementation")

    rows, kill_signal = _drive(tmp_path, _descending, training_role=True,
                               initial_log="".join(f"step {i} loss: 10.0\n" for i in range(4)),
                               until=lambda rows: any(d.get("repair_decided") for _t, d in rows))
    acted = [d for d in rows if d.get("repair_decided")]
    assert len(acted) == 1, rows
    row = acted[0]
    assert row["log_role"] == "training", row          # the stage the kill gate could have ended
    assert (row.get("trajectory") or {}).get("direction") == "descending", row
    assert "trajectory_veto" not in row, row
    assert "kill_role_withheld" not in row, row
    assert kill_signal.get("terminal_reason") == MONITOR_REPAIR_REASON


def test_the_role_receipt_still_lands_when_nothing_acted(tmp_path):
    """The receipt's own purpose survives: a confident `broken` the judge pins on the HYPOTHESIS is
    not a repair, and the work role refuses the kill — so nothing acted, and the row must say the
    role is what held. The log grows each look, so the confirming verdict is a fresh reading."""
    looks = {"n": 0}

    def _hypothesis(wd):
        looks["n"] += 1
        with open(wd / "train.log", "a", encoding="utf-8") as fh:
            fh.write(f"step {looks['n'] + 2} loss: 0.5\n")
        return _broken("hypothesis")

    rows, kill_signal = _drive(
        tmp_path, _hypothesis,
        until=lambda rows: any(d.get("kill_role_withheld") for _t, d in rows))
    withheld = [d for d in rows if d.get("kill_role_withheld")]
    assert withheld, rows
    assert withheld[0]["kill_role_withheld"] == "work"
    assert "stop_decided" not in withheld[0] and "repair_decided" not in withheld[0]
    assert not kill_signal.get("kill")
