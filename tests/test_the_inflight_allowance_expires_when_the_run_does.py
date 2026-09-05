""""Spans the engine has not written yet" is only an excuse while the engine is still writing.

§175 caught one version of this: the in-flight allowance forgiving `oldCK9` $0.076944 an hour and a
half after that probe finished, because the expiry watched the LEDGER rather than the arm. The fix
made the grace per-arm and time-based, which leaves the same hole exactly 300 seconds wide -- for
five minutes after a probe writes `run_finished`, a real leak on it is still excused as unwritten
spans, when the engine has demonstrably finished writing. Batches end every ninety minutes, so that
window opens on schedule, at the moment a probe's final accounting matters most.

The decisive fact was on disk the whole time. These tests pin that both conditions are required, in
ONE function, so the two call sites cannot drift apart.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmarks"))

import check_money  # noqa: E402

NOW = 1_000_000.0


def _probe(bench: Path, name: str, rows):
    d = bench / "model-probes" / name / "runs" / "edge_expansion" / "run"
    d.mkdir(parents=True)
    (d / "events.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    (d / "spans.jsonl").write_text("", encoding="utf-8")


def _running(bench, name, spend=0.42):
    _probe(bench, name, [{"type": "llm_usage", "data": {"cost": spend}}])


def _finished(bench, name):
    _probe(bench, name, [{"type": "llm_usage", "data": {"cost": 1.01}},
                         {"type": "run_finished", "data": {"reason": "budget_exhausted"}}])


def _paused_at_ceiling(bench, name):
    _probe(bench, name, [{"type": "llm_usage", "data": {"cost": 1.004}},
                         {"type": "pause", "data": {}}])


def _paused_below(bench, name):
    _probe(bench, name, [{"type": "llm_usage", "data": {"cost": 0.40}},
                         {"type": "pause", "data": {}}])


def test_a_running_arm_with_a_recent_row_is_still_calling(tmp_path):
    _running(tmp_path, "live")
    assert check_money._still_calling(str(tmp_path), "live", {"live": (NOW - 10, "200")}, now=NOW)


def test_a_finished_arm_with_a_recent_row_is_not(tmp_path):
    """The 300-second hole, closed: the row is fresh, the run is over, and the engine will write no
    more spans for it."""
    _finished(tmp_path, "done")
    assert not check_money._still_calling(str(tmp_path), "done", {"done": (NOW - 10, "200")},
                                          now=NOW)


def test_a_running_arm_gone_quiet_is_not(tmp_path):
    """The condition §175 added is still required; this fix adds to it, it does not replace it."""
    _running(tmp_path, "quiet")
    assert not check_money._still_calling(str(tmp_path), "quiet",
                                          {"quiet": (NOW - 4000, "200")}, now=NOW)


def test_a_pause_at_the_ceiling_counts_as_ended(tmp_path):
    """§228: 16 corpus runs reached full budget and were paused by a blanket handler dressing the
    ceiling refusal as a provider failure. Those runs are over, and their money is final."""
    _paused_at_ceiling(tmp_path, "ceil")
    assert not check_money._still_calling(str(tmp_path), "ceil", {"ceil": (NOW - 10, "200")},
                                          now=NOW)


def test_a_pause_below_the_ceiling_does_not(tmp_path):
    """§213: calling a genuinely paused run finished is what sent freeB3 back for another $0.1056.
    A run that can be resumed can still write the span its ledger row is waiting for."""
    _paused_below(tmp_path, "owed")
    assert check_money._still_calling(str(tmp_path), "owed", {"owed": (NOW - 10, "200")}, now=NOW)


def test_an_arm_with_no_tree_at_all_is_judged_on_its_ledger_row(tmp_path):
    """An abandoned probe has no tree to read; an unreadable tree is not an ending, so the time
    condition alone decides -- which is what the abandoned category already handles downstream."""
    (tmp_path / "model-probes").mkdir(parents=True)
    assert check_money._still_calling(str(tmp_path), "ghost", {"ghost": (NOW - 10, "200")}, now=NOW)
    assert not check_money._still_calling(str(tmp_path), "ghost", {"ghost": (NOW - 4000, "200")},
                                          now=NOW)


def test_an_unknown_arm_is_not_calling(tmp_path):
    (tmp_path / "model-probes").mkdir(parents=True)
    assert not check_money._still_calling(str(tmp_path), "nobody", {}, now=NOW)


def test_a_tree_that_cannot_be_read_is_not_an_ending(tmp_path, monkeypatch):
    """A missing tree does not RAISE -- `probe_calls` globs nothing and reports `finished: False` --
    so the fixture above never reaches the handler, and a mutation making an unreadable tree count
    as ended sailed through. The handler needs a failure that is actually a failure.

    The direction matters: treating "I could not tell" as "it ended" withdraws the allowance from an
    arm that may genuinely have a call in flight, and turns a read error into a false leak."""
    def boom(root, name):
        raise OSError("permission denied")
    monkeypatch.setattr(check_money.arm_fidelity, "probe_calls", boom)
    assert check_money._ended(str(tmp_path), "unreadable") is False
    assert check_money._still_calling(str(tmp_path), "unreadable",
                                      {"unreadable": (NOW - 10, "200")}, now=NOW)
