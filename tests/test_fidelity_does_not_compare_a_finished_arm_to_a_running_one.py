"""A running probe's probe-count is a lower bound; a finished one's is the answer.

`arm_fidelity` counts `run_probe` calls to check that the capped arm really made fewer than the
uncapped one. Mid-flight that comparison inverts by construction: the treated probes stop dead at
their cap of 12 while the controls are still climbing through 9, 10, 11. Three sweeps in a row the
tool printed "NO CONTRAST YET: the control has not out-probed the treatment" at exactly the moment
the intervention was working perfectly — treat 12.0, control 10.5, contrast −1.5 — and the sentence
reads as evidence about the intervention when it is evidence about the clock.

So the contrast is computed over FINISHED probes only, and `finished` is the EXISTENCE of
`final.json`, never its contents: §198's rule that this tool reads no scores still holds.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmarks"))

import arm_fidelity  # noqa: E402

REFUSAL = "(run_probe refused: this run has already made 12 probes, the cap set for this run.)"


def _probe(root: Path, name: str, executed: int, refused: int = 0, finished: bool = True,
           paused: bool = False, resumed: bool = False):
    d = root / name / "runs" / "edge_expansion" / "run"
    d.mkdir(parents=True)
    events = []
    if finished:
        events.append({"type": "run_finished", "data": {"reason": "budget_exhausted"}})
    if paused:
        events.append({"type": "pause", "data": {"reason": "a Developer session crashed"}})
    if resumed:
        events.append({"type": "resume", "data": {}})
    (d / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events),
                                    encoding="utf-8")
    spans = []
    for i in range(executed):
        spans.append({"kind": "tool", "name": "tool", "attributes": {
            "tool": "run_probe", "phase_span": f"s{i // 3}", "output": "ok"}})
    for j in range(refused):
        spans.append({"kind": "tool", "name": "tool", "attributes": {
            "tool": "run_probe", "phase_span": "s9", "output": REFUSAL}})
    (d / "spans.jsonl").write_text("".join(json.dumps(s) + "\n" for s in spans), encoding="utf-8")
    # A final.json is written by a PAUSED run too -- that is what made the first version of this
    # fix wrong -- so every probe here has one and it decides nothing.
    (root / name / "final.json").write_text(json.dumps({"speedup": 999.0}), encoding="utf-8")


def test_mid_flight_the_tool_refuses_to_state_a_contrast(tmp_path):
    """The exact shape observed: treated at the cap, controls still climbing, nothing finished."""
    _probe(tmp_path, "capA3", executed=12, refused=4, finished=False)
    _probe(tmp_path, "capB3", executed=12, refused=2, finished=False)
    _probe(tmp_path, "freeA3", executed=9, finished=False)
    _probe(tmp_path, "freeB3", executed=14, finished=False)
    got = arm_fidelity.report(str(tmp_path), ["capA3", "capB3"], ["freeA3", "freeB3"])
    assert got["contrast"] is None, (
        f'contrast {got["contrast"]} was stated from four running probes; a treated probe that has '
        "stopped at its cap against a control still climbing measures the clock")
    assert sorted(got["running"]) == ["capA3", "capB3", "freeA3", "freeB3"]


def test_a_finished_batch_still_reports_its_contrast(tmp_path):
    """Batch 1's real numbers: treat 12 and 12, control 31 and 21 -> +14."""
    _probe(tmp_path, "capA2", executed=12, refused=7)
    _probe(tmp_path, "capB2", executed=12, refused=5)
    _probe(tmp_path, "freeA2", executed=31)
    _probe(tmp_path, "freeB2", executed=21)
    got = arm_fidelity.report(str(tmp_path), ["capA2", "capB2"], ["freeA2", "freeB2"])
    assert got["contrast"] == 14, got
    assert got["treat_n"] == 2 and got["control_n"] == 2 and got["running"] == []


def test_a_running_probe_is_counted_but_not_compared(tmp_path):
    """One finished pair plus a running one: the contrast comes from the finished pair alone, and
    the running probe is still NAMED -- an arm silently dropping a probe is its own failure."""
    _probe(tmp_path, "capA2", executed=12, refused=7)
    _probe(tmp_path, "freeA2", executed=26)
    _probe(tmp_path, "capB3", executed=12, refused=1, finished=False)
    got = arm_fidelity.report(str(tmp_path), ["capA2", "capB3"], ["freeA2"])
    assert got["contrast"] == 14 and got["treat_n"] == 1, got
    assert got["running"] == ["capB3"]
    assert got["rows"]["capB3"]["executed"] == 12, "a running probe must still be counted and shown"


def test_a_paused_probe_is_not_a_finished_one(tmp_path):
    """`freeB3`, 2026-09-04: auto-paused at node 2 -- "a Developer session crashed (LLM
    unreachable)" -- with $0.86 of $1.00 spent, and it wrote a `final.json` all the same (602 bytes,
    speedup 260.9543). The first version of this fix asked whether that file EXISTS and counted the
    probe as a completed control. It is not finished; it is OWED work."""
    _probe(tmp_path, "capA2", executed=12, refused=7)
    _probe(tmp_path, "freeA2", executed=26)
    _probe(tmp_path, "freeB3", executed=34, finished=False, paused=True)
    got = arm_fidelity.report(str(tmp_path), ["capA2"], ["freeA2", "freeB3"])
    assert got["control_n"] == 1 and got["contrast"] == 14, (
        f'the paused probe was counted into the contrast: {got}')
    assert got["paused"] == ["freeB3"], got
    assert "freeB3" in got["running"], "a paused probe must still be named, not silently dropped"


def test_finished_is_an_EVENT_and_not_a_file(tmp_path):
    """§198: this tool reads no scores. The signal is the TYPE `run_finished`, never `final.json`'s
    contents -- parsing that file would put a score on this screen."""
    _probe(tmp_path, "capA2", executed=12)
    _probe(tmp_path, "freeA2", executed=26)
    for n in ("capA2", "freeA2"):
        (tmp_path / n / "final.json").write_text("", encoding="utf-8")   # empty: decides nothing
    got = arm_fidelity.report(str(tmp_path), ["capA2"], ["freeA2"])
    assert got["contrast"] == 14 and got["running"] == [], got


def test_a_probe_that_never_started_is_not_a_zero(tmp_path):
    """No spans at all is a probe that has not begun, and averaging a 0 into the arm would report
    an intervention effect made of a probe that never ran."""
    _probe(tmp_path, "capA2", executed=12)
    _probe(tmp_path, "freeA2", executed=26)
    (tmp_path / "capB2").mkdir()
    got = arm_fidelity.report(str(tmp_path), ["capA2", "capB2"], ["freeA2"])
    assert got["treat_median"] == 12 and got["treat_n"] == 1, got
    # NOR IS IT "STILL RUNNING". Mutation showed the first two assertions pass with the
    # never-started filter deleted, because an unstarted probe is unfinished either way; the
    # difference only shows here. A probe listed as running is one an operator will wait for.
    assert got["running"] == [], (
        f'{got["running"]} reported as still running, but capB2 has no spans at all -- it has not '
        "started, and naming it makes the arm look like it is waiting on work that never began")


def test_a_resumed_probe_is_running_again_and_not_still_paused(tmp_path):
    """Events are append-only, so "is there a pause event" answers PAUSED for ever. `freeB3`'s
    order is `pause 12:32:26`, `resume 12:36:06`, then paid calls to 13:03:16 -- it was running,
    and the tool reported it paused and owed work while it spent money."""
    _probe(tmp_path, "capA2", executed=12, refused=7)
    _probe(tmp_path, "freeA2", executed=26)
    _probe(tmp_path, "freeB3", executed=34, finished=False, paused=True, resumed=True)
    got = arm_fidelity.report(str(tmp_path), ["capA2"], ["freeA2", "freeB3"])
    assert got["paused"] == [], (
        f'{got["paused"]} still reported paused after a resume event; the state is the LAST '
        "lifecycle event, not any of them")
    assert "freeB3" in got["running"], "a resumed probe is running and must still be named"
    assert got["control_n"] == 1 and got["contrast"] == 14, got
