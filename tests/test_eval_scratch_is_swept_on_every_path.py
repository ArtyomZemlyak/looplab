"""§408. Уборка стояла только на счастливом пути, и мусор копился в огороженном пространстве имён.

`looplab_eval` создаёт `results/LoopLab-<pid>/<task>/` на каждый вызов (иначе два узла, считающиеся
одновременно, затирали бы solver друг друга) и подметает это в конце `emit` — ПОСЛЕ печати
результата. Значит оценка, которая бросила, отказала или была убита, свой каталог оставляет.

Померено 2026-09-10: в дереве `AlgoTune/results` лежат **16** таких каталогов, с 4 по 8 сентября, у
всех pid мёртв. Внутри — копия РЕФЕРЕНСА (не кандидата), так что утечки чужого ответа нет; но это то
самое пространство имён `results/<model>/<task>/`, откуда чужие модели вынесены забором
(`.foreign_results_moved`), и каталог, названный по мёртвому процессу, неотличим от живого для
любого, кто читает дерево потом. Записка от 2026-08-22 в самом файле помнит **77** таких после ОДНОЙ
кампании — она и добавила уборку, но добавила её на счастливый путь.

Пункт 6 обхода этого не видел: он проверяет, что дерево обходится без `PermissionError`, а не что в
нём не растёт мусор.
"""
from __future__ import annotations

import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1] / "benchmarks"
sys.path.insert(0, str(BENCH / "algotune"))

import looplab_eval  # noqa: E402

import pytest  # noqa: E402


def test_the_sweep_runs_when_the_body_raises(tmp_path, monkeypatch):
    """Опровергатель: тело падает, каталог всё равно должен быть убран."""
    doomed = tmp_path / "results" / "LoopLab-999" / "edge_expansion"
    doomed.mkdir(parents=True)
    (doomed / "solver.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(looplab_eval, "_ARTEFACTS", [doomed.parent])
    def boom():
        raise RuntimeError("driven: the evaluation died")
    monkeypatch.setattr(looplab_eval, "_main", boom)
    monkeypatch.delenv("ALGOTUNE_KEEP_EVAL_ARTEFACTS", raising=False)
    with pytest.raises(RuntimeError, match="driven"):
        looplab_eval.main()
    assert not doomed.parent.exists(), "the scratch survived a raising evaluation"


def test_the_sweep_runs_when_the_body_exits_non_zero(tmp_path, monkeypatch):
    """Отказ — не исключение: `main` возвращает код, и мусор всё равно надо убрать."""
    doomed = tmp_path / "results" / "LoopLab-998" / "pde_heat1d"
    doomed.mkdir(parents=True)
    monkeypatch.setattr(looplab_eval, "_ARTEFACTS", [doomed.parent])
    monkeypatch.setattr(looplab_eval, "_main", lambda: 2)
    monkeypatch.delenv("ALGOTUNE_KEEP_EVAL_ARTEFACTS", raising=False)
    assert looplab_eval.main() == 2
    assert not doomed.parent.exists(), "the scratch survived a refusal"


def test_the_keep_flag_still_wins(tmp_path, monkeypatch):
    """`ALGOTUNE_KEEP_EVAL_ARTEFACTS=1` — это про СПОР о цифре; удалять доказательство по умолчанию
    в отладочной сессии хуже, чем оставить мусор."""
    kept = tmp_path / "results" / "LoopLab-997" / "discrete_log"
    kept.mkdir(parents=True)
    monkeypatch.setattr(looplab_eval, "_ARTEFACTS", [kept.parent])
    monkeypatch.setattr(looplab_eval, "_main", lambda: 0)
    monkeypatch.setenv("ALGOTUNE_KEEP_EVAL_ARTEFACTS", "1")
    assert looplab_eval.main() == 0
    assert kept.exists(), "the keep flag stopped being honoured"


class _Explodes:
    """Запись реестра, которая БРОСАЕТ при первом же вопросе.

    Первая версия теста подсовывала `/proc/no-such-thing/deep`: `is_dir()` и `exists()` там просто
    False, ничего не бросается, и мутация «пусть падение уборки летит наружу» проходила ЗЕЛЁНОЙ —
    фикстура не спорила с багом.
    """

    def is_dir(self):
        raise OSError("driven: the filesystem answered with an error")

    def exists(self):
        raise OSError("driven")


def test_a_cleanup_failure_never_changes_the_verdict(monkeypatch):
    """Уборка — лучшее усилие: её падение не должно превращать результат в ошибку."""
    monkeypatch.setattr(looplab_eval, "_ARTEFACTS", [_Explodes()])
    monkeypatch.setattr(looplab_eval, "_main", lambda: 0)
    monkeypatch.delenv("ALGOTUNE_KEEP_EVAL_ARTEFACTS", raising=False)
    assert looplab_eval.main() == 0


def test_the_registry_is_emptied_so_a_second_sweep_deletes_nothing(tmp_path, monkeypatch):
    """Реестр должен опустеть: иначе повторный вызов снесёт каталог, который к тому времени мог
    завести УЖЕ ДРУГОЙ прогон с тем же именем."""
    doomed = tmp_path / "results" / "LoopLab-996" / "pagerank"
    doomed.mkdir(parents=True)
    monkeypatch.setattr(looplab_eval, "_ARTEFACTS", [doomed.parent])
    monkeypatch.setattr(looplab_eval, "_main", lambda: 0)
    monkeypatch.delenv("ALGOTUNE_KEEP_EVAL_ARTEFACTS", raising=False)
    looplab_eval.main()
    assert looplab_eval._ARTEFACTS == [], looplab_eval._ARTEFACTS
    reborn = doomed.parent
    reborn.mkdir(parents=True)
    looplab_eval.main()
    assert reborn.exists(), "a second sweep deleted a directory it no longer owns"


@pytest.mark.corpus
def test_the_stand_is_not_accumulating_scratch_any_more():
    """Якорь на живое дерево: он ЗНАЕТ про сегодняшние 16 и краснеет, если их станет больше."""
    import os
    import re
    root = "/var/tmp/looplab-bench/AlgoTune/results"
    if not os.path.isdir(root):
        pytest.skip("no AlgoTune checkout on this box")
    stale = [d for d in os.listdir(root) if re.match(r"(LoopLab|DevEval\w*)-\d+$", d)
             and not os.path.isdir("/proc/" + d.rsplit("-", 1)[1])]
    assert len(stale) <= 16, (
        f"{len(stale)} stale eval scratch dirs under results/ (was 16 when §408 measured it); "
        "the sweep on every path is not holding")
