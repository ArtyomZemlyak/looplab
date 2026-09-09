"""§367. Проба обязана сдавать узел, который ЦИКЛ счёл лучшим, а не самый свежий файл на диске.

`run_probe.sh` несёт причину своими словами: свежий `solver.py` — «не то же самое, что лучший». На
`convex_hull` 27.08 узел 0 имел train-оценку 3.7777 против 2.7342 у узла 1, а `ls -t` вернул узел 1,
потому что он записан позже. Его и померили на тесте, и весь день это докладывалось как результат
пробы; настоящий чемпион на тесте не измерялся вообще.

Правило записано, и никто не проверял, что оно соблюдается. Сверяются два независимых места,
которым пришлось бы солгать согласованно: строка чемпиона, которую печатает извлекатель в
`probe.log`, и метрики узлов в `events.jsonl` самого прогона. По корпусу 2026-09-09: **141 проба
сходится, 0 расходятся.**
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1] / "benchmarks"
sys.path.insert(0, str(BENCH))

import sweep_claims  # noqa: E402


def _probe(tmp_path, name, nodes, shipped=None):
    """`nodes`: [(node_id, metric)]; `shipped`: (node_id, metric) or None for no champion line."""
    run = tmp_path / "model-probes" / name / "runs" / "t" / "run"
    run.mkdir(parents=True)
    (run / "events.jsonl").write_text("".join(
        json.dumps({"v": 1, "seq": i, "ts": float(i), "type": "node_evaluated",
                    "data": {"node_id": nid, "metric": m}}) + "\n"
        for i, (nid, m) in enumerate(nodes)), encoding="utf-8")
    log = "[00:00:00] start\n"
    if shipped:
        log += f"champion node {shipped[0]} (metric={shipped[1]}) -> /x/champion_solver.py\n"
    (tmp_path / "model-probes" / name / "probe.log").write_text(log, encoding="utf-8")
    return str(tmp_path)


def test_a_probe_that_shipped_its_best_node_agrees(tmp_path):
    bench = _probe(tmp_path, "good", [(0, 3.7777), (1, 2.7342)], shipped=(0, 3.7777))
    ok, said = sweep_claims.check_the_champion_is_the_best_evaluated_node(bench)
    assert ok, said
    assert "1 probe(s) shipped the best evaluated node" in said, said


def test_the_convex_hull_shape_is_caught(tmp_path):
    """Фикстура НЕ СОГЛАСНА с ошибкой: сдан узел 1 (2.7342), хотя узел 0 дал 3.7777."""
    bench = _probe(tmp_path, "convex", [(0, 3.7777), (1, 2.7342)], shipped=(1, 2.7342))
    ok, said = sweep_claims.check_the_champion_is_the_best_evaluated_node(bench)
    assert not ok, said
    assert "NOT THE BEST" in said and "convex shipped node 1" in said, said
    assert "node 0 scored 3.7777" in said, said


def test_the_right_metric_on_the_wrong_node_still_disagrees(tmp_path):
    """Совпадение ЧИСЛА не оправдывает: сдать другой узел с той же оценкой значит померить не тот
    файл. Сверяются и номер, и метрика."""
    bench = _probe(tmp_path, "tie", [(0, 5.0), (1, 5.0)], shipped=(1, 5.0))
    ok, said = sweep_claims.check_the_champion_is_the_best_evaluated_node(bench)
    assert not ok, said


def test_a_probe_that_shipped_nothing_is_not_counted(tmp_path):
    """Проба без чемпиона — это §351, другой факт и другое лечение."""
    bench = _probe(tmp_path, "none", [(0, 1.0)], shipped=None)
    ok, said = sweep_claims.check_the_champion_is_the_best_evaluated_node(bench)
    assert not ok and "no probe on this box records which node it shipped" in said, said


def test_zeros_do_not_become_the_best_node(tmp_path):
    """Ноль — не лучший узел; сравнение идёт по МАКСИМУМУ, а не по последнему."""
    bench = _probe(tmp_path, "z", [(0, 0.0), (1, 4.2), (2, 0.0)], shipped=(1, 4.2))
    ok, said = sweep_claims.check_the_champion_is_the_best_evaluated_node(bench)
    assert ok, said


def test_the_live_corpus_agrees():
    ok, said = sweep_claims.check_the_champion_is_the_best_evaluated_node("/var/tmp/looplab-bench")
    if "no probe on this box" in said:
        return
    assert ok, said
    assert "NOT THE BEST" not in said, said


def test_the_right_node_with_the_wrong_metric_disagrees(tmp_path):
    """Номер сходится, число — нет: два источника говорят разное про ОДИН узел. Сверять только
    номер значило бы принять чужую цифру, лишь бы имя совпало."""
    bench = _probe(tmp_path, "drift", [(0, 3.7777), (1, 2.7342)], shipped=(0, 9.99))
    ok, said = sweep_claims.check_the_champion_is_the_best_evaluated_node(bench)
    assert not ok, said
    assert "drift shipped node 0 (9.9900)" in said and "3.7777" in said, said
