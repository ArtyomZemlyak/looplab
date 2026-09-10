"""§373. Таблицу мощности считали по распределению, в котором четыре прогона ещё шли.

`champions()` брала `max(metric)` у КАЖДОГО прогона задачи с хоть одним оценённым узлом — идущие
включительно. Померено 2026-09-09 при четырёх живых пробах `pagerank`: инструмент сообщал «10
pagerank champions» по шести законченным и четырём работающим, чей «чемпион» — то, что случайно
набрал их ПЕРВЫЙ узел. Разброс, который уходил в симуляцию:

    только законченные: n=6  медиана 52.44  sd  9.8
    с идущими:          n=10 медиана 52.44  sd 11.8

На пятую часть больше дисперсии, а мощность делит именно на неё. Частичная величина, смешанная с
полными, решает деньги — §360 в инструменте, который назначает цену плеча.

Фильтра два, потому что они отвечают на разные вопросы. Живой процесс — то чтение, на котором §360
остановился (`arm_fidelity.is_finished` зовёт `freeB3` и `remDL` незаконченными, хотя те стоят
неделями). `min_spend` — правило `outlier_check.corpus`, и одинаковое число здесь и есть смысл: два
инструмента над одним корпусом не должны расходиться в том, какие прогоны в нём.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

BENCH = Path(__file__).resolve().parents[1] / "benchmarks"
sys.path.insert(0, str(BENCH))

import arm_power  # noqa: E402


def _run(root, probe, task, metrics, spend):
    """Каждая проба здесь несёт ОТГРУЖЕННУЮ карточку: §374 исключает из нуля всё остальное, и без
    этой строки фикстуры про живость и трату проверяли бы совсем другой фильтр."""
    d = root / probe / "runs" / task / "run"
    d.mkdir(parents=True)
    (root / probe / "INSTRUMENT.txt").write_text(
        f"task:           {task}\ncard_args:      {arm_power.SHIPPED_CARD}\n", encoding="utf-8")
    rows = [{"v": 1, "seq": 0, "ts": 0.0, "type": "llm_usage", "data": {"cost": spend}}]
    rows += [{"v": 1, "seq": i + 1, "ts": float(i + 1), "type": "node_evaluated",
              "data": {"node_id": i, "metric": m}} for i, m in enumerate(metrics)]
    (d / "events.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def test_a_running_probe_is_not_a_champion(tmp_path):
    """Тот самый случай: у идущей пробы «чемпион» — её первый узел."""
    # ТРАТА ЖИВОЙ ПРОБЫ ВЫШЕ ПОРОГА: иначе её отсеет `min_spend`, и фильтр по живости останется
    # непроверенным -- первая фикстура дала ей $0.4, и мутация «живые снова в распределении»
    # уходила зелёной.
    _run(tmp_path, "done", "pagerank", [50.0, 55.0], 1.0)
    _run(tmp_path, "live", "pagerank", [25.0], 0.95)
    got = arm_power.champions(str(tmp_path), "pagerank",
                              live=[{"probe": "live", "lane": "0-10", "pid": 1}])
    assert got == [55.0], got


def test_a_finished_probe_with_the_same_shape_is_kept(tmp_path):
    """Отличие делает ЖИВОСТЬ, а не число узлов: законченная проба с одним узлом остаётся."""
    _run(tmp_path, "done", "pagerank", [50.0], 1.0)
    assert arm_power.champions(str(tmp_path), "pagerank", live=[]) == [50.0]


def test_a_barely_started_run_is_not_a_run(tmp_path):
    """`min_spend` — то же правило, что у `outlier_check.corpus`: два инструмента над одним
    корпусом не должны расходиться в том, какие прогоны в нём."""
    _run(tmp_path, "stub", "pagerank", [3.0], 0.05)
    assert arm_power.champions(str(tmp_path), "pagerank", live=[]) == []


def test_the_champion_is_the_best_node_not_the_last(tmp_path):
    _run(tmp_path, "done", "pagerank", [50.0, 55.0, 12.0], 1.0)
    assert arm_power.champions(str(tmp_path), "pagerank", live=[]) == [55.0]


def test_another_task_is_not_pooled_in(tmp_path):
    _run(tmp_path, "a", "pagerank", [50.0], 1.0)
    _run(tmp_path, "b", "edge_expansion", [200.0], 1.0)
    assert arm_power.champions(str(tmp_path), "pagerank", live=[]) == [50.0]


def test_the_live_corpus_refuses_rather_than_borrowing_running_runs():
    """Шести законченных пробам мало, и инструмент обязан ОТКАЗАТЬСЯ, а не добирать до десяти
    теми, кто ещё работает."""
    got = arm_power.champions("/var/tmp/looplab-bench/model-probes", "pagerank")
    if not got:
        # SKIP, not `return`: this anchor reads the LIVE bench corpus, and a box without one
        # must report "not checked" rather than print a green dot for a check that never ran.
        pytest.skip("no pagerank champions on this box")
    assert len(got) >= 1
    import subprocess
    r = subprocess.run([sys.executable, str(BENCH / "arm_power.py"), "--task", "pagerank",
                        "--trials", "20", "--batches", "2"],
                       capture_output=True, text=True, timeout=900)
    if len(got) < 10:
        assert r.returncode == 2 and "refusing to simulate" in r.stderr, (r.returncode, r.stderr)
