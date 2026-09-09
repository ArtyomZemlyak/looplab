"""§385. Ноль плеча A печатался как скорость и делал снос непроверяемым.

Проверка «константы плеча A — файл, а не фраза» считала снос как `100 * (got - was) / was` под
`if was`. Задача, у которой константа §181 равна **0.0**, давала снос ровно `+0.0 %` при ЛЮБОМ
перемере, и пятипроцентные ворота на ней не могли сработать никогда. Это `pagerank` — ровно та
задача, где плечо A сдало решатель, не дающий ни одного валидного ответа, то есть строка, в которой
изменение важнее всего: переход от «нет валидных ускорений» к числу — это разница между плечом,
которое упало, и плечом, которое отработало.

И вторая половина, §342: в докстринге проверки строка `pagerank` всегда кончалась словом
`no_valid_speedups`, а код печатал `pagerank 0.0000 (__w22x1r3, +0.0 % vs §181)` — ноль, одетый
скоростью, рядом с тремя настоящими, с потерянной причиной, которую файл записал.
"""
from __future__ import annotations

import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1] / "benchmarks"
sys.path.insert(0, str(BENCH))

import sweep_claims  # noqa: E402


def test_a_zero_that_becomes_a_number_is_a_change_of_kind():
    """Главный опровергатель: раньше это печаталось как «+0.0 %» и проходило."""
    drift, problem = sweep_claims.retimed_verdict(1.2345, 0.0)
    assert drift is None, drift
    assert problem and "change of kind" in problem and "1.2345" in problem, problem


def test_a_number_that_becomes_a_zero_is_a_change_of_kind_too():
    drift, problem = sweep_claims.retimed_verdict(0.0, 1.5133)
    assert drift is None
    assert problem and "stopped producing anything scorable" in problem, problem


def test_two_zeros_agree_and_raise_nothing():
    """`pagerank` сегодня: §181 и перемер оба говорят «нет валидных ускорений»."""
    assert sweep_claims.retimed_verdict(0.0, 0.0) == (None, None)


def test_an_ordinary_drift_is_still_measured_in_per_cent():
    drift, problem = sweep_claims.retimed_verdict(1.4747, 1.5133)
    assert problem is None
    assert abs(drift - (-2.5507)) < 0.01, drift


def test_a_drift_past_five_per_cent_is_a_problem():
    drift, problem = sweep_claims.retimed_verdict(1.6, 1.5133)
    assert problem is None or "moved" in problem, problem
    drift, problem = sweep_claims.retimed_verdict(1.7, 1.5133)
    assert problem and "moved" in problem, problem


def test_the_zero_row_says_why_and_never_shows_a_speedup():
    row = {"speedup": 0.0, "regime": "__w22x1r3", "no_speedup_reason": "no_valid_speedups"}
    line = sweep_claims.retimed_line("pagerank", row, None)
    assert "NO VALID SPEEDUPS" in line and "no_valid_speedups" in line, line
    assert "0.0000" not in line, line
    assert "% vs §181" not in line, line


def test_a_zero_with_no_recorded_reason_says_that_rather_than_inventing_one():
    row = {"speedup": 0.0, "regime": "__w22x1r3", "no_speedup_reason": None}
    assert "unrecorded" in sweep_claims.retimed_line("pagerank", row, None)


def test_an_ordinary_row_still_reads_as_a_speedup():
    row = {"speedup": 1.4747, "regime": "__w22x1r3", "no_speedup_reason": None}
    # Настоящий снос строки, а не круглое число: -2.55 форматируется в -2.5, и ожидание
    # "-2.6" было моей ошибкой, а не кода.
    drift, _ = sweep_claims.retimed_verdict(1.4747, 1.5133)
    line = sweep_claims.retimed_line("discrete_log", row, drift)
    assert line == "discrete_log 1.4747 (__w22x1r3, -2.6 % vs §181)", line


def test_the_file_on_this_box_still_has_the_row_the_rule_is_about():
    """Якорь: если `pagerank` перестанет быть нулевой строкой, эта защита станет теорией."""
    import json
    path = BENCH / "algotune" / "arm_a_retimed.json"
    row = json.loads(path.read_text(encoding="utf-8"))["tasks"]["pagerank"]
    assert row["speedup"] == 0.0 and row["no_speedup_reason"] == "no_valid_speedups", row
    assert row["s181_said"] == 0.0, row
