"""§351. Возобновление перезапускает ДВИГАТЕЛЬ, а не драйвер, — и результат никто не считает.

`run_probe.sh` после двигателя делает два шага: `extract_champion.py`, потом оценку на TEST в
`final.json`. `resume_paused` (§338) поднимает только двигатель, а драйвер к тому времени давно вышел.
Проба, которая встала на паузу, была возобновлена и потом доработала, остаётся без обоих шагов.

`remDL13` — этот случай. Драйвер написал «чемпион: НЕТ» в 06:21 и вышел; два возобновления довели
пробу до $0.978 и узла с train-оценкой 5.3676; одиннадцать часов никто не сказал, что результат так
и не посчитан. Чемпион извлёкся даром — узел 1 с сиблингами Cython — и дал на TEST **5.1345**.

Обнаружение, а не починка: команды печатаются, а не выполняются. Оценка занимает 22-ядерную полосу,
и это решение оператора, а не монитора.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1] / "benchmarks"
sys.path.insert(0, str(BENCH))
sys.path.insert(0, str(BENCH / "algotune"))

import pulse as pulse_mod  # noqa: E402


def _probe(tmp_path, name, *, nodes=1, final=False, runs=True):
    root = tmp_path / "model-probes"
    d = root / name
    if runs:
        (d / "runs" / "discrete_log" / "run").mkdir(parents=True)
    else:
        d.mkdir(parents=True)
    if final:
        (d / "final.json").write_text(json.dumps({"speedup": 5.13}), encoding="utf-8")
    return str(root), {"nodes": nodes}


def test_an_ended_probe_with_nodes_and_no_final_is_named(tmp_path):
    root, got = _probe(tmp_path, "remDL13")
    said = pulse_mod.unscored_result(root, "remDL13", got)
    assert said and "NO final.json" in said, said
    assert "extract_champion.py --run-dir" in said and "--subset test" in said, said


def test_a_probe_that_was_scored_says_nothing(tmp_path):
    root, got = _probe(tmp_path, "remDL13", final=True)
    assert pulse_mod.unscored_result(root, "remDL13", got) is None


def test_a_probe_that_never_evaluated_is_not_this_case(tmp_path):
    """Проба без единого узла — другой факт («не дошла до оценки»), и у него другое лечение."""
    root, got = _probe(tmp_path, "remDL13", nodes=0)
    assert pulse_mod.unscored_result(root, "remDL13", got) is None


def test_a_probe_without_a_run_directory_is_not_claimed_recoverable(tmp_path):
    """Печатать «восстанавливается даром» там, где извлекать не из чего, — обещание, а не факт."""
    root, got = _probe(tmp_path, "remDL13", runs=False)
    assert pulse_mod.unscored_result(root, "remDL13", got) is None


def test_the_printed_command_names_the_regime_the_corpus_uses(tmp_path):
    """140 из 142 `final.json` на коробке — `__w22x1r3`. Незаданный ALGOTUNE_EVAL_WORKERS означает
    один воркер и `__lane22r3`: я так и сделал, получил 4.5794 вместо 5.1345, и число не сравнимо
    ни с чем. Команда обязана нести это на себе."""
    root, got = _probe(tmp_path, "remDL13")
    said = pulse_mod.unscored_result(root, "remDL13", got)
    assert "ALGOTUNE_EVAL_WORKERS=auto" in said, said
    assert "__w22x1r3" in said, said


def test_pulse_asks_the_question_for_every_ended_probe():
    src = (BENCH / "pulse.py").read_text(encoding="utf-8")
    assert "unscored_result(root, name," in src, "pulse больше не спрашивает про несчитанный результат"
