"""«Does the model use the reference» has two answers per probe, and the quoted band is the old
corpus's.

Driven 2026-09-08 through `probe_summary --json` over 142 probes: the reference is reached in a
median 8.3 % of a probe's own `run_probe` spans (p25 5.3, p75 12.5, max 30.0), and only **30 of
142** fall inside the quoted 4.9-8.3 %. The list is right that 3.0 % is wrong; the band it offers
instead describes a third of today's corpus.

And `ref_imports` counts occurrences anywhere in the run -- all 142 probes have at least one --
while `ref_pct` is the share of probe spans. **20 probes** import the reference in code they wrote
and never touch it from a probe, so the two fields disagree about the same probe by construction.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1] / "benchmarks"
sys.path.insert(0, str(BENCH))

import sweep_claims  # noqa: E402

STUB = '''#!/usr/bin/env python3
import pathlib
print(pathlib.Path(__file__).with_name("rows.json").read_text())
'''


def _bench(tmp_path, rows) -> str:
    tools = tmp_path / "looplab" / "benchmarks"
    tools.mkdir(parents=True)
    (tools / "probe_summary.py").write_text(STUB, encoding="utf-8")
    (tools / "rows.json").write_text(json.dumps(rows), encoding="utf-8")
    return str(tmp_path)


def _row(ref_pct, imports=3):
    return {"probe": "p", "ref_pct": ref_pct, "ref_call_pct": ref_pct, "ref_imports": imports}


def test_a_corpus_inside_the_band_holds_the_claim(tmp_path):
    rows = [_row(x) for x in (5.0, 6.0, 7.0, 8.0, 5.5, 6.5, 7.5, 8.2)]
    ok, detail = sweep_claims.check_reference_use_band(_bench(tmp_path, rows))
    assert ok, detail
    assert "8 of 8 inside" in detail, detail


def test_todays_spread_refutes_it(tmp_path):
    """A third inside is not a baseline. The fixture keeps the MEDIAN at 8.3 -- inside the band --
    so a check that looked only at the middle would call this a match."""
    rows = [_row(x) for x in (0.0, 2.0, 5.3, 8.3, 8.3, 12.5, 20.0, 30.0)]
    ok, detail = sweep_claims.check_reference_use_band(_bench(tmp_path, rows))
    assert not ok, detail
    assert "8.3 % of run_probe spans" in detail, detail
    assert "3 of 8 inside" in detail, detail   # 5.3, 8.3, 8.3


def test_the_two_answers_per_probe_are_counted(tmp_path):
    """The split the corpus actually shows: written code imports it, the probe spans never do."""
    rows = [_row(0.0, imports=7), _row(0.0, imports=0), _row(9.0, imports=2)]
    _, detail = sweep_claims.check_reference_use_band(_bench(tmp_path, rows))
    assert "1 import it in written code and never from a probe" in detail, detail


def test_a_probe_with_no_run_probe_span_is_not_counted_as_zero(tmp_path):
    """`ref_pct` is None when the model never called the tool; folding that into the distribution
    as 0.0 would drag the median down with runs that were never asked the question."""
    rows = [_row(9.0), {"probe": "q", "ref_pct": None, "ref_imports": 5}]
    _, detail = sweep_claims.check_reference_use_band(_bench(tmp_path, rows))
    assert "1 probe(s):" in detail, detail
