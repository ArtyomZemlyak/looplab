"""§402. Тревога, до которой нельзя дотянуться, — это зелёная галочка ни о чём.

Проверка «трата ДО первого узла» судит МЕДИАНУ против полосы, а медиана — ранговая статистика: она
не двигается, пока не сдвинется большая часть корпуса. Померено: чтобы вывести медиану
`edge_expansion` из (20, 45), нужно **104 новых пробы по 100 %** — на корпусе из 118. Такой сигнал
не достижим никаким правдоподобным ходом событий, и галочка рядом ничего не говорит о том, ради чего
утверждение заведено: о пробе, сжигающей бюджет до первой оценки.

А такие пробы есть: 12 из 153 (7.8 %) лежат вне полосы своей задачи — `remDL3` 77 %, `remDL6` 66 %,
`capA9` 64 %, `remPde9` 39 % при нижней границе 40, — и проверка о них молчала.
"""
from __future__ import annotations

import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1] / "benchmarks"
sys.path.insert(0, str(BENCH))

import sweep_claims  # noqa: E402


def test_a_median_deep_inside_its_band_costs_many_probes():
    """Десять проб на 30 % в полосе (20, 45): чтобы медиана вышла за 45, нужно больше десяти."""
    vals = [30.0] * 10
    k = sweep_claims.probes_to_move_the_median_out(vals, (20.0, 45.0))
    assert k is not None and k >= 10, k


def test_a_median_near_the_edge_costs_few():
    """Полоса (20, 45), медиана 44: одной пробы на 100 % уже почти хватает."""
    vals = [44.0] * 3
    assert sweep_claims.probes_to_move_the_median_out(vals, (20.0, 45.0)) <= 4


def test_a_median_already_outside_has_no_price():
    """Она уже провалена — цена бессмысленна, и None это говорит."""
    assert sweep_claims.probes_to_move_the_median_out([90.0, 92.0], (20.0, 45.0)) is None


def test_an_empty_corpus_or_no_band_answers_nothing():
    assert sweep_claims.probes_to_move_the_median_out([], (20.0, 45.0)) is None
    assert sweep_claims.probes_to_move_the_median_out([30.0], None) is None


def test_the_cap_is_returned_rather_than_looping_forever():
    """Полоса с верхней границей 100 не выводится ничем: ответ — потолок, а не зависание."""
    assert sweep_claims.probes_to_move_the_median_out([50.0] * 5, (0.0, 100.0), cap=25) == 25


def test_probes_outside_their_own_band_are_found_and_ranked():
    by_task = {"t": [(77.0, "worst"), (30.0, "fine"), (10.0, "low")],
               "u": [(50.0, "ok")]}
    bands = {"t": (15.0, 55.0), "u": (20.0, 60.0)}
    n, worst = sweep_claims.probes_outside_their_band(by_task, bands)
    assert n == 2, worst
    assert worst[0][1] == "worst", worst
    assert {p for _t, p, _v in worst} == {"worst", "low"}, worst


def test_a_task_without_a_band_contributes_nobody():
    """Незапиннённая задача не даёт «нарушителей» — судить её не по чему."""
    n, worst = sweep_claims.probes_outside_their_band({"new": [(99.0, "p")]}, {})
    assert (n, worst) == (0, []), worst


def test_the_live_check_names_both_the_price_and_the_offenders():
    """Через ВЫЗОВ, на живом корпусе (§399's lesson): цифры считает та же функция, что печатает."""
    import os
    if not os.path.isdir("/var/tmp/looplab-bench/model-probes"):
        import pytest
        pytest.skip("no bench on this box")
    ok, detail = sweep_claims.check_waste_before_the_first_node("/var/tmp/looplab-bench")
    assert "sit OUTSIDE their task's band" in detail, detail
    assert "new probes at 100 % needed to move each median OUT" in detail, detail
    assert "never about one bad run" in detail, detail
