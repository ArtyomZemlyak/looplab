"""§375. Половину утверждения мерили каждый обход, вторую цитировали из комментария.

Докстринг `check_reference_use_band` записывает ДВА распределения — по импортам (`ref_pct`) и по
вызовам (`ref_call_pct`), — а гоняла проверка только первое. Это ровно та форма, ради прекращения
которой файл и заведён: число в комментарии, которое никто не перечитывает.

И это не одно и то же число: они расходятся на **37 из 152** проб (`remDL9` — 5.882 против 20.588),
так что дрейф по вызовам был бы невидим. Померено сегодня: импорты медиана 8.5 % (p25 5.6, p75 12.5,
max 33.3), вызовы медиана 8.3 % (p25 5.6, p75 12.5, max 38.9).
"""
from __future__ import annotations

import json
import subprocess
import sys

import pytest
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1] / "benchmarks"
sys.path.insert(0, str(BENCH))

import sweep_claims  # noqa: E402

STUB = '''#!/usr/bin/env python3
import json, sys
print(json.dumps({PAYLOAD}))
'''


def _bench(tmp_path, rows):
    tools = tmp_path / "looplab" / "benchmarks"
    tools.mkdir(parents=True)
    (tools / "probe_summary.py").write_text(STUB.replace("{PAYLOAD}", json.dumps(rows)),
                                            encoding="utf-8")
    return str(tmp_path)


def _row(probe, ref_pct, ref_call_pct, imports=1):
    return {"probe": probe, "task": "pagerank", "ref_pct": ref_pct,
            "ref_call_pct": ref_call_pct, "ref_imports": imports, "run_probe": 9}


def test_the_calls_half_is_reported(tmp_path):
    bench = _bench(tmp_path, [_row("a", 8.0, 20.0), _row("b", 8.0, 30.0)])
    _, said = sweep_claims.check_reference_use_band(bench)
    assert "by CALLS rather than imports" in said, said
    assert "median 30.0 %" in said or "median 20.0 %" in said, said


def test_the_two_halves_are_not_the_same_number(tmp_path):
    """Если бы они всегда совпадали, вторая половина была бы лишней. Они расходятся."""
    bench = _bench(tmp_path, [_row("a", 5.0, 25.0), _row("b", 5.0, 35.0)])
    _, said = sweep_claims.check_reference_use_band(bench)
    assert "5.0 % of run_probe spans" in said, said
    assert "max 35.0" in said, said


def test_a_corpus_without_the_calls_field_says_nothing_extra(tmp_path):
    """Старые сводки поля не несут: молчать честнее, чем печатать ноль."""
    rows = [dict(_row("a", 8.0, 0.0)), dict(_row("b", 8.0, 0.0))]
    for r in rows:
        del r["ref_call_pct"]
    _, said = sweep_claims.check_reference_use_band(_bench(tmp_path, rows))
    assert "by CALLS" not in said, said


def test_the_verdict_still_rests_on_the_imports_half(tmp_path):
    """Вторая половина ДОКЛАДЫВАЕТСЯ, но не меняет вердикт: утверждение списка про импорты, и
    подменять его тихо нельзя."""
    ok_rows = [_row(f"p{i}", 6.0, 99.0) for i in range(4)]
    ok, _ = sweep_claims.check_reference_use_band(_bench(tmp_path, ok_rows))
    assert ok, "вердикт поехал за вызовами"


@pytest.mark.corpus
def test_the_live_check_prints_both():
    """§379: this asked the WHOLE sweep and cost 381 s -- more than every other test in its
    selection combined -- to read one line. The claim is one check's; the check is called directly.
    Spawning twenty-one checks to see what one of them prints is not a stronger test, only a slower
    one."""
    _ok, said = sweep_claims.check_reference_use_band("/var/tmp/looplab-bench")
    if "cannot be driven" in said or "no probe" in said:
        return
    assert "by CALLS rather than imports" in said, said
