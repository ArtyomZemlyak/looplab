"""`benchmarks/cue_reach.py` must resolve chained prompts, not read the stored suffix.

The failure this guards is not hypothetical and not old: a sweep on 2026-09-03 measured the money
cue at 31.7 % of `plan_step` spans by grepping `attributes.input`, and the true figure on the same
files is 99.3 %. `core/tracing.py` stores only the NEW messages when `input_from` is set, so any
phase whose prompts chain reads as blind. The fixture below is that shape in miniature: the cue is
in the base span and the child carries it, so a tool that reads `input` alone scores 1 of 2 and a
tool that resolves scores 2 of 2.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmarks"))

import cue_reach  # noqa: E402


def _write(tmp_path: Path) -> Path:
    """Two chained `plan_step` generations; the cue is stated once, in the parent."""
    base = {
        "kind": "generation", "span_id": "aaaa", "start": 1.0,
        "attributes": {
            "phase": "plan_step", "cost": "0.10", "input_carry": 0, "input_from": None,
            "input": [{"role": "user", "content": "BUDGET: $0.5000 of $1.0000 spent."}],
        },
    }
    child = {
        "kind": "generation", "span_id": "bbbb", "start": 2.0,
        "attributes": {
            "phase": "plan_step", "cost": "0.30", "input_carry": 1, "input_from": "aaaa",
            "input": [{"role": "user", "content": "now write the next step"}],
        },
    }
    path = tmp_path / "spans.jsonl"
    path.write_text("".join(json.dumps(s) + "\n" for s in (base, child)), encoding="utf-8")
    return path


def test_a_carried_cue_counts_for_the_span_that_carried_it(tmp_path):
    rows, (grand, blind) = cue_reach.reach([_write(tmp_path)], cue_reach.MONEY, naive=True)
    assert len(rows) == 1
    phase, n, hit, cost, naive_hit = rows[0]
    assert (phase, n) == ("plan_step", 2)
    # The whole point: both spans were told the budget, and only one of them stores the sentence.
    assert hit == 2, f"resolved reach should be 2 of 2, got {hit}"
    assert naive_hit == 1, f"the truncated read should score 1 of 2, got {naive_hit}"
    # Cost accounting follows the resolved answer, so nothing is charged to a blind phase.
    assert abs(grand - 0.40) < 1e-9
    assert blind == 0.0


def test_the_default_pattern_covers_all_three_wordings(tmp_path):
    """One regex that names only `Spend guidance` reports the Developer and Researcher as blind."""
    spans = [
        {"kind": "generation", "span_id": "a", "start": 1.0,
         "attributes": {"phase": "propose", "cost": "0.1", "input_carry": 0,
                        "input": [{"role": "user", "content": "\nSpend guidance: $0.1 of $1.0"}]}},
        {"kind": "generation", "span_id": "b", "start": 2.0,
         "attributes": {"phase": "plan", "cost": "0.1", "input_carry": 0,
                        "input": [{"role": "user", "content": "BUDGET: $0.2000 of $1.0000 spent"}]}},
        {"kind": "generation", "span_id": "c", "start": 3.0,
         "attributes": {"phase": "deep_research", "cost": "0.1", "input_carry": 0,
                        "input": [{"role": "user", "content": "BUDGET: $0.3000 of $1.0000 spent"}]}},
    ]
    path = tmp_path / "spans.jsonl"
    path.write_text("".join(json.dumps(s) + "\n" for s in spans), encoding="utf-8")

    rows, _ = cue_reach.reach([path], cue_reach.MONEY)
    assert {r[0]: r[2] for r in rows} == {"propose": 1, "plan": 1, "deep_research": 1}

    narrow, _ = cue_reach.reach([path], r"Spend guidance")
    assert {r[0]: r[2] for r in narrow} == {"propose": 1, "plan": 0, "deep_research": 0}, (
        "a pattern that names one wording must be visibly narrower -- this is the second way to "
        "get the reach question wrong, and it has to stay visible in a test")


def test_a_probe_tree_is_walked_not_just_a_run_dir(tmp_path):
    """`spans_of` takes what a sweep actually types: a probe dir with runs/<task>/run/ inside."""
    deep = tmp_path / "probeX" / "runs" / "edge_expansion" / "run"
    deep.mkdir(parents=True)
    _write(deep)
    assert cue_reach.spans_of(tmp_path / "probeX") == [deep / "spans.jsonl"]
    assert cue_reach.spans_of(deep) == [deep / "spans.jsonl"]
