"""§396. У проверки есть доля прохождения и есть ПОДВЕРЖЕННОСТЬ, и порознь они ничего не значат.

«150 проб сдали лучший оценённый узел» — зелёная галочка, которая молчит о том, проверялось ли
правило вообще. Будь лучший узел всегда последним, `ls -t` и `state.best()` совпадали бы везде, и
проверка проходила бы на корпусе, который не может её провалить.

Померено 2026-09-09 по 143 прогонам с двумя и более оценёнными узлами: в **103 (72 %)** лучший узел
НЕ последний, и наивное правило сдало бы узел с медианой на **499 % меньшего** метрика. Дефект §367
не был близким промахом — это обычный случай.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1] / "benchmarks"
sys.path.insert(0, str(BENCH))

import sweep_claims  # noqa: E402


def _run(root, probe, task, nodes):
    """`nodes` — [(node_id, metric, ts)] в порядке записи."""
    run = root / "model-probes" / probe / "runs" / task / "run"
    run.mkdir(parents=True)
    (run / "events.jsonl").write_text("".join(
        json.dumps({"v": 1, "type": "node_evaluated", "ts": ts,
                    "data": {"node_id": nid, "metric": m}}) + "\n"
        for nid, m, ts in nodes), encoding="utf-8")


def test_a_corpus_where_the_best_is_always_last_reports_zero_exposure(tmp_path):
    """Опровергатель: правило верно, проверка проходит — и подверженность ноль, о чём и надо сказать."""
    _run(tmp_path, "a", "t", [(0, 1.0, 100.0), (1, 2.0, 200.0)])
    _run(tmp_path, "b", "t", [(0, 5.0, 100.0), (1, 9.0, 200.0)])
    with_choice, differ, lost = sweep_claims.where_the_newest_rule_would_differ(str(tmp_path))
    assert (with_choice, differ, lost) == (2, 0, None)


def test_a_run_whose_best_is_not_newest_counts_as_exposure(tmp_path):
    _run(tmp_path, "a", "t", [(0, 10.0, 100.0), (1, 2.0, 200.0)])
    with_choice, differ, lost = sweep_claims.where_the_newest_rule_would_differ(str(tmp_path))
    assert (with_choice, differ) == (1, 1)
    assert abs(lost - 400.0) < 1e-6, lost          # 10 against 2 is +400 %


def test_a_single_node_run_is_not_a_choice(tmp_path):
    """Один узел — не выбор: сдавать нечего, и в знаменатель подверженности он не идёт."""
    _run(tmp_path, "solo", "t", [(0, 3.0, 100.0)])
    assert sweep_claims.where_the_newest_rule_would_differ(str(tmp_path)) == (0, 0, None)


def test_the_median_is_over_the_runs_that_differ_not_over_all(tmp_path):
    """Иначе совпадающие прогоны разбавили бы цифру до бессмыслицы."""
    _run(tmp_path, "a", "t", [(0, 10.0, 100.0), (1, 2.0, 200.0)])     # +400 %
    _run(tmp_path, "b", "t", [(0, 4.0, 100.0), (1, 1.0, 200.0)])      # +300 %
    _run(tmp_path, "c", "t", [(0, 1.0, 100.0), (1, 9.0, 200.0)])      # agrees
    with_choice, differ, lost = sweep_claims.where_the_newest_rule_would_differ(str(tmp_path))
    assert (with_choice, differ) == (3, 2)
    assert abs(lost - 350.0) < 1e-6, lost


def test_a_zero_metric_newest_does_not_divide_by_zero(tmp_path):
    """`capA10` — реальный случай: последний узел оценён в 0.0000."""
    _run(tmp_path, "a", "t", [(0, 249.0, 100.0), (1, 0.0, 200.0)])
    with_choice, differ, lost = sweep_claims.where_the_newest_rule_would_differ(str(tmp_path))
    assert (with_choice, differ) == (1, 1) and lost is None


def test_the_reported_line_carries_both_numbers(tmp_path):
    """Сентенция, а не греп: проверяется то, что проверка ВОЗВРАЩАЕТ (§395's lesson)."""
    _run(tmp_path, "a", "t", [(0, 10.0, 100.0), (1, 2.0, 200.0)])
    (tmp_path / "model-probes" / "a" / "probe.log").write_text(
        "champion node 0 (metric=10.0000)\n", encoding="utf-8")
    ok, detail = sweep_claims.check_the_champion_is_the_best_evaluated_node(str(tmp_path))
    assert ok, detail
    assert "1 probe(s) shipped the best evaluated node" in detail, detail
    assert "1 of 1 run(s) with a choice" in detail, detail
    assert "400 % less metric" in detail, detail


def test_a_box_with_no_choice_anywhere_says_the_rule_is_untested(tmp_path):
    """Самое важное сообщение: галочка на корпусе, который не может её провалить."""
    _run(tmp_path, "solo", "t", [(0, 3.0, 100.0)])
    (tmp_path / "model-probes" / "solo" / "probe.log").write_text(
        "champion node 0 (metric=3.0000)\n", encoding="utf-8")
    _ok, detail = sweep_claims.check_the_champion_is_the_best_evaluated_node(str(tmp_path))
    assert "the rule is untested here" in detail, detail


def test_newest_means_by_TIME_not_by_node_id(tmp_path):
    """Фикстура СПОРИТ с багом: узел с меньшим id записан ПОЗЖЕ.

    Во всех остальных фикстурах порядок id совпадает с порядком времени, и мутация «брать последний
    по id» приходила ЗЕЛЁНОЙ — правило про то, что подсунул бы `ls -t`, то есть про ВРЕМЯ ЗАПИСИ.
    Так бывает у возобновлённого прогона, который дооценивает ранний узел.
    """
    _run(tmp_path, "a", "t", [(5, 1.0, 200.0), (2, 9.0, 300.0)])
    with_choice, differ, lost = sweep_claims.where_the_newest_rule_would_differ(str(tmp_path))
    # newest BY TIME is node 2, which is also the best -> the naive rule would agree here
    assert (with_choice, differ, lost) == (1, 0, None)
    # ...and by id it would be node 5, i.e. a disagreement that does not exist
