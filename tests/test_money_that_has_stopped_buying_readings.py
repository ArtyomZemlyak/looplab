"""§347. Доля трат после последнего узла — не живой сигнал, и прибор по соседству это уже записал.

Я померил `remDL13` живьём: 55.5 % трат ушло после узла 0, при том что худшая ЗАКОНЧИВШАЯСЯ проба
этой коробки держала 47.4 %. Добавил в `pulse` строку «всё ещё платит и больше не учится».

Через сорок минут проба оценила второй узел (5.3676), и то же число стало **0.44 %**.

`probe_summary` держит эту причину дословно, и я её не прочитал:

    `after%` MEANS TWO DIFFERENT THINGS depending on whether the run is over ... For a RUNNING one
    it is just "time since the last node", which grows until the next one lands and then collapses.

Оно и помечает живую цифру плюсом ровно поэтому. Тревогу дала ФОРМА метрики, а не состояние
прогона: величина, растущая между узлами и схлопывающаяся на каждом, не может судиться полосой,
собранной по терминальным значениям.

Заодно всплыло, что два прибора считают этот хвост по-разному: `probe_summary` берёт спаны
`generation` по времени их НАЧАЛА, а сумма `llm_usage` из дерева относит к «после» и тот вызов,
который был уже в полёте, когда узел приземлился. На `accPde` это 47.394 % против 48.310 %.
Правильный из двух — первый: вызов, начатый до узла, не был решением, принятым после него.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1] / "benchmarks"
sys.path.insert(0, str(BENCH))
sys.path.insert(0, str(BENCH / "algotune"))

import pulse as pulse_mod  # noqa: E402


def _log(tmp_path, events):
    p = tmp_path / "events.jsonl"
    p.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    return str(p)


def _spend(cost):
    return {"v": 1, "seq": 0, "ts": 1.0, "type": "llm_usage", "data": {"cost": cost}}


def _node(metric=1.5):
    return {"v": 1, "seq": 0, "ts": 1.0, "type": "node_evaluated",
            "data": {"node_id": 0, "generation": 0, "metric": metric, "eval_seconds": 4.0,
                     "violations": []}}


def test_the_tail_collapses_when_the_next_node_lands(tmp_path):
    """Свидетельство, из-за которого строку убрали: та же проба, один следующий узел — и 75 %
    становятся нулём. Величина такой формы не может быть порогом для живого прогона."""
    before = pulse_mod.pulse(_log(tmp_path, [_spend(1.0), _node(), _spend(3.0)]))
    assert abs(pulse_mod.tail_after_the_last_node(before) - 75.0) < 1e-9
    after = pulse_mod.pulse(_log(tmp_path, [_spend(1.0), _node(), _spend(3.0), _node()]))
    assert pulse_mod.tail_after_the_last_node(after) == 0.0


def test_pulse_does_not_judge_a_running_probe_by_it():
    """Строка была написана и убрана в один обход. Тест держит её убранной: `probe_summary`
    помечает живую цифру плюсом именно потому, что судить по ней нечего."""
    src = (BENCH / "pulse.py").read_text(encoding="utf-8")
    assert "TAIL_WORST_IN_THE_CORPUS" not in src, "порог по терминальным значениям вернулся"
    assert "still paying and no longer learning" not in src, "вердикт по живой пробе вернулся"
    assert "collapses" in src, "причина, по которой его нет, должна остаться на месте"


def test_the_reason_is_where_it_was_missed_the_first_time():
    """Причина лежала в `probe_summary` и была прочитана только после того, как ошибка повторилась.
    Если эта фраза оттуда исчезнет, следующий читатель придёт к тому же выводу заново."""
    src = (BENCH / "probe_summary.py").read_text(encoding="utf-8")
    assert "time since the last node" in src and "collapses" in src, \
        "пояснение про живую цифру ушло из probe_summary"


def test_a_zero_is_an_answer_and_resets_the_tail(tmp_path):
    """Ноль — ОТВЕТ арены: деньги после него не купили следующего чтения так же, как после забитого
    узла. Считать только забитые значило бы звать пробу продуктивной, пока она тратит."""
    got = pulse_mod.pulse(_log(tmp_path, [_spend(1.0), _node(0.0), _spend(1.0)]))
    assert got["nodes"] == 0 and got["zeros"] == 1
    assert abs(pulse_mod.tail_after_the_last_node(got) - 50.0) < 1e-9


def test_a_probe_with_no_node_has_no_tail(tmp_path):
    """Без единого узла доли «после последнего узла» не существует — это не 100 %."""
    assert pulse_mod.tail_after_the_last_node(
        pulse_mod.pulse(_log(tmp_path, [_spend(1.0), _spend(2.0)]))) is None


def test_a_probe_that_spent_nothing_is_not_divided_by_zero(tmp_path):
    assert pulse_mod.tail_after_the_last_node(
        pulse_mod.pulse(_log(tmp_path, [_node()]))) is None
