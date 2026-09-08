"""§348. Правило, на котором держится вся колонка плеча B, и его никто не проверял.

Каждый узел LoopLab оценивается на TRAIN; TEST — судейская половина, и прогон не должен её видеть.
Это причина, по которой `compare_arms._arm_b_final` отказывается ставить собственную метрику
чемпиона в одну колонку с тестовым результатом плеча A — сверено дословно:

    every LoopLab node is evaluated on TRAIN (mirroring AlgoTuner's agent loop), so
    `state.best().metric` is a TRAIN number

Нарушение не выглядело бы как поломка. Оно выглядело бы как хороший счёт.

Померено 2026-09-08 по всему корпусу: 392 оценённых узла, 392 просили `train`, 392 подтверждены,
0 без свидетельства — все по `patch_marker_present`. Проверка заведена, чтобы это осталось
измерением, а не тем, что все и так знают.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1] / "benchmarks"
sys.path.insert(0, str(BENCH))

import sweep_claims  # noqa: E402


def _bench(tmp_path, nodes):
    """`nodes`: (probe, node_id, evidence-dict-or-None)."""
    for probe, node_id, ev in nodes:
        d = tmp_path / "model-probes" / probe / "runs" / "t" / "run"
        d.mkdir(parents=True, exist_ok=True)
        tail = {"speedup": 1.0, "eval_seconds": 4.0}
        if ev is not None:
            tail["subset_evidence"] = ev
        row = {"v": 1, "seq": node_id, "ts": 1.0, "type": "node_evaluated",
               "data": {"node_id": node_id, "metric": 1.0, "stdout_tail": json.dumps(tail)}}
        with open(d / "events.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
    (tmp_path / "model-probes").mkdir(exist_ok=True)
    return str(tmp_path)


TRAIN = {"asked": "train", "verified": True, "reason": "patch_marker_present"}


def test_a_corpus_of_train_nodes_holds(tmp_path):
    ok, said = sweep_claims.check_every_node_was_graded_on_train(
        _bench(tmp_path, [("remDL13", 0, TRAIN), ("accEE", 0, TRAIN)]))
    assert ok, said
    assert "2 evaluated node(s): 2 asked train and verified" in said, said


def test_one_node_on_the_graded_half_fails_the_whole_check(tmp_path):
    """Фикстура НЕ СОГЛАСНА с ошибкой: один узел, попросивший `test`, обязан провалить проверку."""
    ok, said = sweep_claims.check_every_node_was_graded_on_train(
        _bench(tmp_path, [("remDL13", 0, TRAIN),
                          ("accEE", 3, {"asked": "test", "verified": True,
                                        "reason": "patch_marker_present"})]))
    assert not ok, said
    assert "GRADED ON THE WRONG HALF" in said and "accEE/node 3" in said, said
    assert "asked='test'" in said, said


def test_an_unverified_node_fails_even_though_it_asked_for_train(tmp_path):
    """`verified` — сверка с пропатченным файлом, а не повтор того, что просили. Попросить train и
    не получить подтверждения — не то же самое, что оценка на train."""
    ok, said = sweep_claims.check_every_node_was_graded_on_train(
        _bench(tmp_path, [("accEE", 1, {"asked": "train", "verified": False,
                                        "reason": "marker_absent"})]))
    assert not ok, said
    assert "verified=False" in said, said


def test_a_node_without_evidence_is_not_passed_over(tmp_path):
    """«Непроверяемо» — не «проверено». Это состояние, которое произвело бы будущее изменение
    харнесса, и промолчать о нём значит объявить его нормой."""
    ok, said = sweep_claims.check_every_node_was_graded_on_train(
        _bench(tmp_path, [("remDL13", 0, TRAIN), ("accEE", 0, None)]))
    assert not ok, said
    assert "1 carry NO subset evidence" in said, said


def test_an_empty_box_is_refused_not_passed(tmp_path):
    (tmp_path / "model-probes").mkdir()
    ok, said = sweep_claims.check_every_node_was_graded_on_train(str(tmp_path))
    assert not ok and "no evaluated node" in said, said


def test_the_rule_is_still_worded_that_way_in_compare_arms():
    """Цитата сверена дословно. Если формулировка уйдёт из `compare_arms`, эта проверка защищает
    правило, которого там больше нет."""
    src = (BENCH / "algotune" / "compare_arms.py").read_text(encoding="utf-8")
    assert "Every LoopLab node is\n    evaluated on TRAIN" in src, \
        "формулировка правила изменилась -- сверить заново, а не подгонять тест"
