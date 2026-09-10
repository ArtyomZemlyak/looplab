"""§405. «$1.5537 сверх бюджетов, худший +$0.1056» — величина без частоты.

Из этой строки читатель берёт горстку отстающих. Пересчитал независимо, по собственным спанам
`llm_usage` проб: сумма сходится до цента — и открывается то, чего строка не говорила:
**147 проб из 156 (94 %) заканчиваются ЗА бюджетом**, медиана +$0.0096, 81 из них в пределах цента,
64 между одним и пятью, две выше пяти.

Утверждение «проба за доллар стоит доллар» ложно не сильно, а ПОЧТИ ВСЕГДА — примерно на процент.
Величина без частоты подсказывает неверный вывод: та же форма, что «доля прохождения без
подверженности» (§396) и «тревога без досягаемости» (§402).
"""
from __future__ import annotations

import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1] / "benchmarks"
sys.path.insert(0, str(BENCH))

import sweep_claims  # noqa: E402


def test_the_sentence_carries_how_many_went_over():
    said = sweep_claims.overshoot_sentence(156, [0.01] * 147, 1.5537, 0.1056)
    assert "$1.5537 spent past the budgets" in said, said
    assert "worst +$0.1056" in said, said
    assert "147 of 156 (94 %) finish OVER budget" in said, said


def test_the_median_is_the_typical_overshoot_not_the_worst():
    said = sweep_claims.overshoot_sentence(3, [0.001, 0.002, 0.500], 0.503, 0.500)
    assert "median +$0.0020" in said, said
    assert "worst +$0.5000" in said, said


def test_a_corpus_where_nobody_overshot_says_nothing_extra():
    """Обратная сторона: если перерасхода нет, добавлять нечего — и добавляться не должно."""
    said = sweep_claims.overshoot_sentence(10, [], 0.0, 0.0)
    assert "finish OVER budget" not in said, said
    assert said.startswith("10 probe(s) with spend"), said


def test_the_percentage_is_of_probes_with_spend(tmp_path):
    said = sweep_claims.overshoot_sentence(200, [0.01] * 50, 0.5, 0.02)
    assert "50 of 200 (25 %)" in said, said


def test_the_live_corpus_still_reports_both_halves():
    """Через ВЫЗОВ: пересчёт по спанам дал ровно $1.5537 и 147 из 156 — сверено вторым прибором."""
    import os
    if not os.path.isdir("/var/tmp/looplab-bench/model-probes"):
        import pytest
        pytest.skip("no bench on this box")
    _ok, detail = sweep_claims.check_a_dollar_probe_costs_a_dollar("/var/tmp/looplab-bench")
    assert "spent past the budgets in total" in detail, detail
    assert "finish OVER budget" in detail, detail
    assert "a standing overshoot of about one per cent" in detail, detail
