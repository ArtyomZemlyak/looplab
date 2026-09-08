"""The figure the money cue was meant to shrink had been quoted from a docstring since it was taken.

`engine/proposal_cues.py` records "$3.6067 of $100.2691 corpus spend (3.6 %) lands AFTER the last
evaluated node ... 16 of 69 runs end holding one". Driven 2026-09-08 through `probe_summary --json`,
which already computes the per-probe share: **5.8 % of $142.5275 over 141 probes with a node, median
1.5 %, and 72 of them end holding an unfinished draw**. The corpus grew and the share grew with it.

And there is no control group left: `cue_reach` says every probe on this box now carries the money
cue in `propose` (141 of 141), so this number cannot say whether the cue helped -- only that the
waste is still there with it everywhere.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1] / "benchmarks"
sys.path.insert(0, str(BENCH))

import sweep_claims  # noqa: E402

# THE ROWS GO IN A FILE, NOT INTO THE SOURCE. Embedding `json.dumps(rows)` in the stub's body makes
# `true`/`false`/`null` Python identifiers -- the first version died with `NameError: name 'true' is
# not defined` and the check reported "probe_summary produced no json", which is exactly what it
# would say about a real tool that crashed. A fixture must fail differently from the thing it stands
# in for.
STUB = '''#!/usr/bin/env python3
import pathlib, sys
print(pathlib.Path(__file__).with_name("rows.json").read_text())
'''


def _bench(tmp_path, rows) -> str:
    tools = tmp_path / "looplab" / "benchmarks"
    tools.mkdir(parents=True)
    (tools / "probe_summary.py").write_text(STUB, encoding="utf-8")
    (tools / "rows.json").write_text(json.dumps(rows), encoding="utf-8")
    return str(tmp_path)


def _row(spent, after_pct, reached=True):
    return {"probe": "p", "spent": spent, "after_pct": after_pct, "reached_a_node": reached}


def test_the_quoted_pair_would_still_hold_if_the_corpus_said_so(tmp_path):
    rows = [_row(100.2691 / 69, 3.6) for _ in range(69)]
    for r in rows[16:]:
        r["after_pct"] = 0.0          # only 16 hold a draw
    for r in rows[:16]:
        r["after_pct"] = 3.6 * 69 / 16
    ok, detail = sweep_claims.check_waste_after_the_last_node(_bench(tmp_path, rows))
    assert ok, detail
    assert "16 end holding" in detail, detail


def test_todays_corpus_refutes_it(tmp_path):
    rows = [_row(1.0, 5.8) for _ in range(141)]
    ok, detail = sweep_claims.check_waste_after_the_last_node(_bench(tmp_path, rows))
    assert not ok, detail
    assert "5.8 % of $141.0000" in detail, detail
    assert "141 end holding" in detail, detail


def test_the_share_is_weighted_by_money_and_not_averaged(tmp_path):
    """A probe that spent a dollar and a probe that spent a cent do not carry equal weight in a
    CORPUS share. The fixture makes the two answers differ: money-weighted 1.0 %, mean of shares
    50.5 %."""
    rows = [_row(100.0, 1.0), _row(0.01, 100.0)]
    _, detail = sweep_claims.check_waste_after_the_last_node(_bench(tmp_path, rows))
    assert "1.0 % of $100.0100" in detail, detail


def test_a_probe_that_never_reached_a_node_is_not_in_the_denominator(tmp_path):
    """`after the last evaluated node` is undefined for a run that evaluated none -- counting its
    whole spend as tail would make the metric grow every time a probe dies early."""
    rows = [_row(1.0, 10.0), _row(9.0, 0.0, reached=False)]
    _, detail = sweep_claims.check_waste_after_the_last_node(_bench(tmp_path, rows))
    assert "1 probe(s) with a node" in detail, detail
    assert "of $1.0000" in detail, detail


def test_a_zero_denominator_is_refused_rather_than_divided_by(tmp_path):
    """Measured while writing this: guessing the money key (`spend`/`usd` instead of `spent`) summed
    to zero and the check reported "0.0 % of $0.0000" without noticing it had no money at all."""
    rows = [{"probe": "p", "after_pct": 5.0, "reached_a_node": True}]
    ok, detail = sweep_claims.check_waste_after_the_last_node(_bench(tmp_path, rows))
    assert not ok and "money key changed name" in detail, detail
