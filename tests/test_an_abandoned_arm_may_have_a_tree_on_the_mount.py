"""§368. «Нет дерева на диске» мерилось только по `/var/tmp`, а `/var/tmp` однажды стёрли.

Снимочная машинерия существует ровно против этого. `capA1`, `capB1`, `freeA1` и `freeB1` держат
деревья под `runs-archive/model-probes` со **157** генерационными спанами и **$0.5078** между ними,
а сверка списывает за четверых **$0.8987** как необъяснённые.

Ни счёт, ни арифметика здесь не меняются: правило сверки — брошенное плечо вычитается ЦЕЛИКОМ и
потому не должно ещё и раскладываться, — несущее, и §112 записан о том, чего стоит переставить её
чтения. Меняется то, что строка перестаёт утверждать отсутствие свидетельства, лежащего на
постоянном разделе в одном `glob` отсюда. Та же форма, о которой предупреждает сам список про
`remDL`: деньги списаны как потерянные рядом с деревом, которое есть.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1] / "benchmarks"
sys.path.insert(0, str(BENCH))

import check_money  # noqa: E402


def _archived(tmp_path, probe, costs):
    run = tmp_path / probe / "runs" / "t" / "run"
    run.mkdir(parents=True)
    (run / "events.jsonl").write_text("".join(
        json.dumps({"v": 1, "seq": i, "ts": float(i), "type": "llm_usage", "data": {"cost": c}}) + "\n"
        for i, c in enumerate(costs)), encoding="utf-8")
    return str(tmp_path)


def test_an_archived_tree_is_found_and_summed(tmp_path):
    arch = _archived(tmp_path, "capA1", [0.1, 0.0168])
    cost, rows = check_money.archived_spend("capA1", arch)
    assert rows == 2 and abs(cost - 0.1168) < 1e-9, (cost, rows)


def test_a_probe_with_no_archived_tree_reads_zero(tmp_path):
    (tmp_path / "empty").mkdir()
    assert check_money.archived_spend("svcCacheCheck", str(tmp_path)) == (0.0, 0)


def test_a_negative_cost_cannot_credit_the_arm(tmp_path):
    """Отрицательная цена — это не возврат денег; она не должна уменьшать объяснённое."""
    arch = _archived(tmp_path, "x", [0.5, -0.4])
    cost, rows = check_money.archived_spend("x", arch)
    assert abs(cost - 0.5) < 1e-9 and rows == 2, (cost, rows)


def test_a_torn_line_costs_that_line_only(tmp_path):
    run = tmp_path / "y" / "runs" / "t" / "run"
    run.mkdir(parents=True)
    # Рваная строка обязана НЕСТИ слово `llm_usage`, иначе её отсеет дешёвый префильтр и разбор
    # до неё не дойдёт вовсе -- первая фикстура так и промахнулась мимо ветки, которую проверяла.
    (run / "events.jsonl").write_text(
        '{"type": "llm_usage", "data": {"cost": 0.25}}\n'
        '{"type": "llm_usage", "data": {"cost": 0.2\n', encoding="utf-8")
    assert check_money.archived_spend("y", str(tmp_path)) == (0.25, 1)


def test_the_report_names_the_archive_and_the_remainder():
    src = (BENCH / "check_money.py").read_text(encoding="utf-8")
    assert "no tree under model-probes" in src, "строка снова утверждает отсутствие дерева вообще"
    # Фраза разрезана переносом строки в исходнике, поэтому пришпилены обе половины: искать
    # целую строку значило бы проверять форматирование, а не содержание.
    assert "span(s) in the runs-archive)" in src, "архив не назван рядом с суммой"
    assert "NOT unexplained" in src, "остаток не отделён от объяснённого"
    # И блок печати обязан ЗВАТЬ функцию, а не повторять её своими словами: §361 — упоминание
    # модуля не равно вызову, и та же мутация здесь уходила зелёной.
    assert "line, arch = abandoned_line(p, c)" in src, "печать снова мимо общей формулировки"


def test_the_arithmetic_is_untouched():
    """§112: порядок и правило «брошенное вычитается целиком» несущие. Правка §368 — только текст."""
    src = (BENCH / "check_money.py").read_text(encoding="utf-8")
    assert "gap = live[\"cost_usd\"] - spans_total" in src, "разрыв стал считаться иначе"
    assert "archived_spend" not in src.split("gap = live")[1].split("\n")[0], \
        "архив попал в арифметику разрыва"


def test_the_live_archive_holds_what_the_report_says():
    """Якорь в настоящих данных: четыре плеча, которые списывались целиком."""
    total = sum(check_money.archived_spend(p)[0]
                for p in ("capA1", "capB1", "freeA1", "freeB1"))
    if total == 0.0:
        return
    assert abs(total - 0.5078) < 0.01, total


def test_the_sentence_says_what_the_archive_holds(tmp_path):
    """Формулировка и есть правка (§342): тест, читающий число из словаря, пропускает блок печати,
    который утверждает, что свидетельства нет."""
    arch = _archived(tmp_path, "capA1", [0.1, 0.0168])
    line, kept = check_money.abandoned_line("capA1", 0.2791, arch)
    assert line == "capA1 $0.2791 ($0.1168 of it has 2 span(s) in the runs-archive)", line
    assert abs(kept - 0.1168) < 1e-9


def test_an_arm_with_nothing_archived_gets_the_bare_sentence(tmp_path):
    line, kept = check_money.abandoned_line("svcCacheCheck", 0.0011, str(tmp_path))
    assert line == "svcCacheCheck $0.0011", line
    assert kept == 0.0
