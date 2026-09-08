"""§356. Пересчитанная константа плеча A существует — а таблица берёт цифру чужой кампании.

`_arm_a` читает `agent_summary.json`: то, что кампания плеча A сообщила про СЕБЯ, померенное на её
коробке и её линейке. §181/§193 перемерили те же солверы через наш мост на линейке, которой делится
каждое число плеча B, а §355 сложил результат файлом. Расхождение — до 12 % (`edge_expansion` 1.1087
против 0.9759), и сравнение шло бы дальше, молча пользуясь первой цифрой.

Числа НЕ подменяются. Какая из двух принадлежит таблице — решение оператора, и §298 записан ровно
про то, чего стоит тихая подстановка «исправленной» цифры. Говорится: файл есть, вот что в нём, и
разница больше шума.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1] / "benchmarks"
sys.path.insert(0, str(BENCH / "algotune"))

import compare_arms  # noqa: E402


def _file(tmp_path, tasks):
    p = tmp_path / "arm_a_retimed.json"
    p.write_text(json.dumps({"tasks": tasks}), encoding="utf-8")
    return p


def test_a_material_disagreement_is_named(tmp_path):
    p = _file(tmp_path, {"edge_expansion": {"speedup": 0.9759, "regime": "__w22x1r3"}})
    got = compare_arms.retimed_disagreements({"edge_expansion": 1.1087}, p)
    assert len(got) == 1 and "1.1087" in got[0] and "0.9759" in got[0], got
    assert "__w22x1r3" in got[0] and "-12.0 %" in got[0], got


def test_agreement_inside_noise_says_nothing(tmp_path):
    p = _file(tmp_path, {"pde_heat1d": {"speedup": 1.1000, "regime": "__w22x1r3"}})
    assert compare_arms.retimed_disagreements({"pde_heat1d": 1.1010}, p) == []


def test_a_task_the_campaign_never_scored_is_not_a_disagreement(tmp_path):
    """`pagerank` у кампании — None. Ноль против None это не расхождение, а отсутствие числа."""
    p = _file(tmp_path, {"pagerank": {"speedup": 0.0, "regime": "__w22x1r3"}})
    assert compare_arms.retimed_disagreements({"pagerank": None}, p) == []


def test_a_missing_file_is_silent_not_an_error(tmp_path):
    """Файла может не быть на чужой коробке; это не повод валить сравнение."""
    assert compare_arms.retimed_disagreements({"edge_expansion": 1.1}, tmp_path / "nope.json") == []


def test_the_numbers_are_not_swapped(tmp_path):
    """Тихая подстановка «исправленной» цифры — это §298. Проверка НАЗЫВАЕТ, а не заменяет."""
    src = (BENCH / "algotune" / "compare_arms.py").read_text(encoding="utf-8")
    block = src.split("def retimed_disagreements")[1].split("\ndef ")[0]
    assert "a[task] =" not in block and "a.update" not in block, block[:400]
    assert "NOTE:" in src, "расхождение должно доходить до страницы"


def test_the_live_file_disagrees_with_the_live_summary():
    """Якорь в настоящих данных: на этой коробке расхождение есть по всем трём задачам, где у
    кампании есть число."""
    path = BENCH / "algotune" / "arm_a_retimed.json"
    if not path.is_file():
        return
    a = {"edge_expansion": 1.1087, "pde_heat1d": 1.1010, "discrete_log": 1.5419, "pagerank": None}
    got = compare_arms.retimed_disagreements(a, path)
    assert len(got) == 3, got
