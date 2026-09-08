"""§350. Поле, заведённое чтобы не смешивать два режима, само их смешало.

`observed_regime` глобило кэш и возвращало режим САМОГО НОВОГО ФАЙЛА. Оценка, которая ЧИТАЕТ
запись кэша, её mtime не трогает — значит «самый новый» это просто тот режим, который минтили
последним. У всех четырёх задач обхода есть обе записи, и последовательные написаны 09-06, а широкие
датированы 08-31: каждое ШИРОКОЕ чтение с тех пор штамповалось `lane22r3`.

Поймано на себе: четыре чтения, снятые на тихой коробке 2026-09-08, ушли в ПОСЛЕДОВАТЕЛЬНЫЙ пул —
ровно то смешение, которое §314 запрещает, произведённое полем, заведённым его предотвращать.

`looplab_eval.py` штампует `eval_regime()` на собственном выводе — то, что арена реально разрешила.
Оттуда и берётся. Запасного пути на листинг кэша НЕТ: догадка здесь и положила четыре чтения не в
тот пул, а строку без режима §340 обрабатывает честно.
"""
from __future__ import annotations

import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1] / "benchmarks"
sys.path.insert(0, str(BENCH))

import ruler_selfcheck  # noqa: E402


def _eval(key):
    return {"speedup": 1.0, "eval_regime": {"key": key}}


def test_the_run_names_its_own_regime():
    assert ruler_selfcheck.observed_regime("pde_heat1d", "test", [_eval("__w22x1r3")]) == "w22x1r3"
    assert ruler_selfcheck.observed_regime("pde_heat1d", "test", [_eval("__lane22r3")]) == "lane22r3"


def test_a_run_that_said_nothing_gets_no_guess():
    """Догадка здесь и есть дефект. `None` честнее: §340 умеет с ней обращаться."""
    assert ruler_selfcheck.observed_regime("pde_heat1d", "test", []) is None
    assert ruler_selfcheck.observed_regime("pde_heat1d", "test", [{"speedup": 1.0}]) is None
    assert ruler_selfcheck.observed_regime("pde_heat1d", "test", None) is None


def test_the_newest_cache_file_does_not_decide_it(tmp_path, monkeypatch):
    """Тот самый механизм: обе записи на диске, последовательная НОВЕЕ — и всё равно широкий прогон
    обязан назваться широким."""
    d = tmp_path / ".baseline_times"
    d.mkdir()
    old = d / "pde_heat1d__test__w22x1r3.json"
    new = d / "pde_heat1d__test__lane22r3.json"
    old.write_text("{}", encoding="utf-8")
    new.write_text("{}", encoding="utf-8")
    import os
    os.utime(old, (1_700_000_000, 1_700_000_000))
    os.utime(new, (1_800_000_000, 1_800_000_000))
    monkeypatch.setattr(ruler_selfcheck, "baseline_dir", lambda: str(d))
    assert ruler_selfcheck.observed_regime("pde_heat1d", "test", [_eval("__w22x1r3")]) == "w22x1r3"


def test_the_caller_hands_the_evaluations_over():
    """Функция бесполезна, если прогон её не кормит: в `main` собираются сами ответы оценок."""
    src = (BENCH / "ruler_selfcheck.py").read_text(encoding="utf-8")
    assert "seen_evals.append(row)" in src, "ответы оценок больше не собираются"
    assert "observed_regime(args.task, args.subset, seen_evals)" in src, \
        "режим снова определяется без ответа прогона"


def test_no_fallback_to_the_cache_listing():
    src = (BENCH / "ruler_selfcheck.py").read_text(encoding="utf-8")
    body = src.split("def observed_regime")[1].split("\ndef ")[0]
    assert "glob(" not in body, "запасной путь на листинг кэша вернулся -- это и был дефект"
