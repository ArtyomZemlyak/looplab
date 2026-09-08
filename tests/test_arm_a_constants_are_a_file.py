"""§355. Числа, на которых стоит всё сравнение A против B, были предложением в docs/56.

§181 перемерил плечо A на проверенной линейке, §193 добавил ещё два — и результат существовал только
markdown-таблицей. `grep -rl 0.9648` по всем json/py/txt/jsonl коробки не вернул ничего, кроме
случайных подстрок внутри спанов проб.

Перемерено 2026-09-08 из уцелевших логов кампании — тот солвер, который AlgoTuner реально отгрузил,
текст после `FILE IN CODE DIR solver.py:` до первой строки лога — и сложено файлом со всеми полями,
которые решают, можно ли усреднять это с числом плеча B. Модель прочитана из самого лога
(`Model: deepseek-v4-flash`), а не додумана, и собственные цифры кампании в файле совпадают со
столбцом §193 в точности — это и говорит, что извлечён нужный текст.

Первая попытка извлечения дала четыре `evaluator_error` подряд: граница блока искалась по строке с
временной меткой, а строки лога начинаются с `INFO - `, так что «солвер» съел хвост лога и не
компилировался. Четыре одинаковых отказа на четырёх разных солверах — признак прибора, а не данных.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1] / "benchmarks"
sys.path.insert(0, str(BENCH))

import sweep_claims  # noqa: E402

ROW = {"speedup": 1.4747, "s181_said": 1.5133, "regime": "__w22x1r3", "eval_workers": 22}


def _bench(tmp_path, tasks):
    algo = tmp_path / "looplab" / "benchmarks" / "algotune"
    algo.mkdir(parents=True)
    (algo / "arm_a_retimed.json").write_text(json.dumps({"tasks": tasks}), encoding="utf-8")
    return str(tmp_path)


def test_the_constants_are_read_from_the_file(tmp_path):
    ok, said = sweep_claims.check_arm_a_constants_are_a_file(_bench(tmp_path, {"discrete_log": ROW}))
    assert ok, said
    assert "discrete_log 1.4747 (__w22x1r3, -2.6 % vs §181)" in said, said


def test_a_missing_file_is_the_finding_not_a_pass(tmp_path):
    """Пока файла нет, числа берутся из прозы, и проверить их нечем. Это провал, а не «нечего
    проверять»."""
    (tmp_path / "looplab" / "benchmarks" / "algotune").mkdir(parents=True)
    ok, said = sweep_claims.check_arm_a_constants_are_a_file(str(tmp_path))
    assert not ok and "quoted from docs/56" in said, said


def test_an_entry_that_does_not_name_its_regime_fails(tmp_path):
    """Скорость без режима нельзя усреднить с числом плеча B — это и есть §314 на верхнем уровне."""
    ok, said = sweep_claims.check_arm_a_constants_are_a_file(
        _bench(tmp_path, {"discrete_log": dict(ROW, regime=None)}))
    assert not ok and "does not name the regime" in said, said


def test_a_constant_that_walked_away_is_reported(tmp_path):
    """Полоса ±5 % — это разброс питоновского солвера на двадцати двух воркерах, а не «примерно»."""
    ok, said = sweep_claims.check_arm_a_constants_are_a_file(
        _bench(tmp_path, {"discrete_log": dict(ROW, speedup=2.0)}))
    assert not ok and "moved" in said and "§181" in said, said


def test_an_entry_without_a_number_is_not_silently_skipped(tmp_path):
    ok, said = sweep_claims.check_arm_a_constants_are_a_file(
        _bench(tmp_path, {"discrete_log": dict(ROW, speedup=None)}))
    assert not ok and "has no number" in said, said


def test_the_live_file_carries_its_provenance():
    """Файл без происхождения — та же проза, только в json: чем мерили, каким солвером, откуда он."""
    path = BENCH / "algotune" / "arm_a_retimed.json"
    if not path.is_file():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "FILE IN CODE DIR solver.py:" in data["_source"], data["_source"]
    assert data["_model"] == "deepseek-v4-flash" and data["_subset"] == "test", data
    for task, row in data["tasks"].items():
        assert row["regime"] == "__w22x1r3", (task, row)
        assert row["subset_verified"] is True, (task, row)
        assert len(row["solver_sha256"]) == 12, (task, row)
        assert row["campaign_said"] is None or isinstance(row["campaign_said"], float), (task, row)
