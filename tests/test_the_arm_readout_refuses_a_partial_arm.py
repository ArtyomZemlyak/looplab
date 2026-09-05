"""The arm's analysis, fixed in code before the numbers exist.

§190 registered twelve batches and an exact stratified permutation. The rules for what COUNTS as a
probe accumulated afterwards, one incident at a time — `freeB3` excluded at $1.1056 (§213.1),
`capB4` capped but never reaching its cap (§243), a pause at the ceiling that is really an ending
(§228) — and each is a degree of freedom that could be re-decided after the fact to suit the number.
Written as code and run while the arm is incomplete, they cannot be.

The property these tests pin is the one that costs money to get wrong: the readout must REFUSE a
partial arm, and its admission rules must be the ones already written down.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmarks"))

import arm_readout  # noqa: E402


def _probe(root: Path, name: str, cap, spend: float, score, finished: bool = True):
    d = root / name / "runs" / "edge_expansion" / "run"
    d.mkdir(parents=True)
    (d / "config.snapshot.json").write_text(
        json.dumps({"developer_probe_max_calls": cap}), encoding="utf-8")
    events = [{"type": "llm_usage", "data": {"cost": spend}}]
    if finished:
        events.append({"type": "run_finished", "data": {"reason": "budget_exhausted"}})
    (d / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    if score is not None:
        (root / name / "final.json").write_text(json.dumps({"speedup": score}), encoding="utf-8")


def test_it_refuses_to_read_a_partial_arm(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(arm_readout, "ROOT", str(tmp_path))
    monkeypatch.setattr(arm_readout.arm_fidelity, "DEFAULT_ROOT", str(tmp_path))
    monkeypatch.setattr(arm_readout, "BATCHES", [(["t1", "t2"], ["c1", "c2"])])
    for n, cap in (("t1", 12), ("t2", 12), ("c1", 0), ("c2", 0)):
        _probe(tmp_path, n, cap, 1.0, 200.0)
    assert arm_readout.main(["--batches", "12"]) == 2
    out = capsys.readouterr().out
    assert "REFUSING TO READ THE ARM" in out, out


def test_a_probe_over_the_spend_ceiling_is_excluded(tmp_path, monkeypatch):
    """§213.1's criterion, written before any contrast was read: over $1.05 is not a $1 probe.
    `freeB3` finished at $1.1056 after I resumed it, and §228 is why the engine let it."""
    monkeypatch.setattr(arm_readout, "ROOT", str(tmp_path))
    monkeypatch.setattr(arm_readout.arm_fidelity, "DEFAULT_ROOT", str(tmp_path))
    _probe(tmp_path, "over", 0, 1.1056, 260.9543)
    got, why = arm_readout.admit("over", "control", 1.05)
    assert got is None and "over the $1.05 ceiling" in why, why
    _probe(tmp_path, "ok", 0, 1.0156, 258.2564)
    assert arm_readout.admit("ok", "control", 1.05)[0] == 258.2564


def test_a_probe_whose_config_disagrees_with_its_label_is_excluded(tmp_path, monkeypatch):
    """§243: behaviour alone cannot tell a treated probe that never reached its cap from a control.
    The run's own config can, and it is the admission rule."""
    monkeypatch.setattr(arm_readout, "ROOT", str(tmp_path))
    monkeypatch.setattr(arm_readout.arm_fidelity, "DEFAULT_ROOT", str(tmp_path))
    _probe(tmp_path, "mislabelled", 0, 1.0, 200.0)
    got, why = arm_readout.admit("mislabelled", "treat", 1.05)
    assert got is None and "not 12" in why, why
    _probe(tmp_path, "capB4", 12, 1.0095, 215.3809)
    assert arm_readout.admit("capB4", "treat", 1.05)[0] == 215.3809, (
        "capB4 stopped at eleven probes on its own and must still be in the arm")


def test_a_pause_at_the_ceiling_counts_as_ended(tmp_path, monkeypatch):
    """§228: sixteen corpus runs record a normal ending as a Developer crash, and the engine fix
    cannot reach probes already on disk."""
    monkeypatch.setattr(arm_readout, "ROOT", str(tmp_path))
    monkeypatch.setattr(arm_readout.arm_fidelity, "DEFAULT_ROOT", str(tmp_path))
    _probe(tmp_path, "paused_at_ceiling", 0, 1.0031, 227.0792, finished=False)
    assert arm_readout.admit("paused_at_ceiling", "control", 1.05)[0] == 227.0792
    _probe(tmp_path, "paused_midway", 0, 0.8645, 260.0, finished=False)
    got, why = arm_readout.admit("paused_midway", "control", 1.05)
    assert got is None and "has not ended" in why, why


def test_the_permutation_null_is_the_registered_one():
    """Six relabellings per batch of four, and the observed split is one of them. A one-sided test
    whose p can reach 0 has lost the observed arrangement from its own null."""
    batches = [([200.0, 210.0], [100.0, 110.0]), ([220.0, 230.0], [120.0, 130.0])]
    p = arm_readout.stratified_p(batches)
    assert p == 1 / 36, p
    flat = [([100.0, 100.0], [100.0, 100.0])]
    assert arm_readout.stratified_p(flat) == 1.0
