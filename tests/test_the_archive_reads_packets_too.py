"""§381. Функция, доказывающая, что деньги брошенного плеча лежат на диске, не видела пакетов.

`archived_spend` разбирала файл сама и пропускала строку, у которой `type` не `llm_usage`. Но
crash-atomic пакет (`__looplab_event_batch_v1__`) — ровно то, чем кончается УПАВШИЙ прогон, а
указывают эту функцию только на упавшие деревья. Сегодня в четырёх архивах ноль пакетных строк, так
что $0.5078 не меняются: дефект латентный, а не неверное число. Чинится потому, что пакеты будут у
следующего брошенного плеча, а не у этого.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1] / "benchmarks"
sys.path.insert(0, str(BENCH))

import check_money  # noqa: E402


def _events(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def _usage(cost):
    return {"v": 1, "type": "llm_usage", "data": {"cost": cost}}


def test_money_sealed_in_a_crash_packet_is_counted(tmp_path):
    """Фикстура СПОРИТ с багом: половина денег снаружи пакета, половина внутри.

    Рядом лежат две ловушки, чтобы мутации краснели по делу: событие НЕ типа `llm_usage` с полем
    `data.cost` (снятый фильтр типа завысит сумму) и `llm_usage` с `cost_usd` вместо `cost` —
    поле из постоянного списка, чтение которого даёт другую цифру, а не ноль.
    """
    _events(tmp_path / "p" / "runs" / "t" / "run" / "events.jsonl", [
        _usage(0.25),
        {"v": 1, "type": "node_evaluated", "data": {"metric": 12.0, "cost": 9.0}},
        {"v": 1, "type": "llm_usage", "data": {"cost": 0.50, "cost_usd": 7.0}},
        {"v": 1, "type": "__looplab_event_batch_v1__", "data": {
            "events": [_usage(0.75), {"v": 1, "type": "tool_call",
                                      "data": {"cost": 4.0}}]}},
    ])
    cost, rows = check_money.archived_spend("p", archive=str(tmp_path))
    assert rows == 3, rows
    assert abs(cost - 1.50) < 1e-9, cost


def test_the_list_spelling_of_a_packets_type_is_counted_too(tmp_path):
    """Два написания — у ПОЛЯ `type`, а не у `data.events`.

    Первая версия этого теста «вспомнила», что `data.events` бывает JSON-строкой, и покраснела на
    выдуманном написании. Корпус пишет `"type": ["__looplab_event_batch_v1__"]` — список из одного
    элемента, — а тесты движка гоняют голую строку; `data.events` в обоих случаях СПИСОК
    (`events_read.is_packet` требует именно его). Сверяй дословно, а не по памяти.
    """
    _events(tmp_path / "p" / "runs" / "t" / "run" / "events.jsonl", [
        {"v": 1, "type": ["__looplab_event_batch_v1__"],
         "data": {"events": [_usage(0.10), _usage(0.20)]}},
    ])
    cost, rows = check_money.archived_spend("p", archive=str(tmp_path))
    assert rows == 2 and abs(cost - 0.30) < 1e-9, (rows, cost)


def test_a_row_named_a_packet_but_holding_no_events_is_not_swallowed(tmp_path):
    """`is_packet` требует ОБОИХ признаков: имя без `data.events` — обычная строка, не контейнер."""
    _events(tmp_path / "p" / "runs" / "t" / "run" / "events.jsonl", [
        {"v": 1, "type": "__looplab_event_batch_v1__", "data": {"note": "quoted in a message"}},
        _usage(0.60),
    ])
    cost, rows = check_money.archived_spend("p", archive=str(tmp_path))
    assert rows == 1 and abs(cost - 0.60) < 1e-9, (rows, cost)


def test_a_torn_last_line_costs_that_line_only(tmp_path):
    p = tmp_path / "p" / "runs" / "t" / "run" / "events.jsonl"
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps(_usage(0.40)) + '\n{"type": "llm_us\n', encoding="utf-8")
    cost, rows = check_money.archived_spend("p", archive=str(tmp_path))
    assert rows == 1 and abs(cost - 0.40) < 1e-9, (rows, cost)


def test_a_negative_cost_cannot_credit_the_arm(tmp_path):
    """Правило §112 держится и внутри пакета: списание считается ЦЕЛИКОМ, отрицательных нет."""
    _events(tmp_path / "p" / "runs" / "t" / "run" / "events.jsonl", [
        {"v": 1, "type": "__looplab_event_batch_v1__", "data": {
            "events": [_usage(0.30), _usage(-5.0)]}},
    ])
    cost, _ = check_money.archived_spend("p", archive=str(tmp_path))
    assert abs(cost - 0.30) < 1e-9, cost


def test_the_four_archived_arms_still_total_what_the_docstring_says():
    """Якорь §368: починка чтения НЕ двигает число, которым живёт сверка."""
    import os
    if not os.path.isdir(check_money.ARCHIVE):
        import pytest
        pytest.skip("runs-archive not mounted")
    total = sum(check_money.archived_spend(a)[0]
                for a in ("capA1", "capB1", "freeA1", "freeB1"))
    rows = sum(check_money.archived_spend(a)[1]
               for a in ("capA1", "capB1", "freeA1", "freeB1"))
    assert (round(total, 4), rows) == (0.5078, 157), (total, rows)
