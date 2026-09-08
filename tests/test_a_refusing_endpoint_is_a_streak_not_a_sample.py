"""«Последний вызов вернулся 503» — это один отсчёт величины, у которой есть длительность.

§333. `remDL13` шла двадцать четыре минуты, получая 503 подряд — `litellm.ServiceUnavailableError:
No available workers (all circuits open or unhealthy)`, то есть пул провайдера лежал, — а `pulse`
печатал «last call came back 503, not 200». Правило самого списка про другую стену устроено так же:
«три подряд 504 с латентностью ровно 300 с — это потолок nginx, а не зависание». Одним отсчётом ни
то, ни другое не выражается.

Леджер дописывается в порядке событий, поэтому серия считается одним сравнением на строку: 200
обнуляет, повтор того же не-200 продлевает, ДРУГОЙ не-200 начинает новую — четыре 401 и четыре 503
это не один сбой из восьми строк.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1] / "benchmarks"
sys.path.insert(0, str(BENCH))

import check_money  # noqa: E402


def _ledger(tmp_path, rows) -> str:
    p = tmp_path / "meter.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return str(p)


def _row(ts, arm, status):
    return {"ts": ts, "arm": arm, "status": status}


def test_the_streak_counts_consecutive_failures_and_says_when_it_began(tmp_path):
    rows = [_row(100.0, "a", 200), _row(200.0, "a", 503), _row(210.0, "a", 503),
            _row(220.0, "a", 503)]
    got = check_money.endpoint_health(_ledger(tmp_path, rows))["streak"]["a"]
    assert got == {"count": 3, "since": 200.0, "status": "503"}


def test_a_success_clears_the_streak(tmp_path):
    rows = [_row(100.0, "a", 503), _row(110.0, "a", 503), _row(120.0, "a", 200)]
    assert check_money.endpoint_health(_ledger(tmp_path, rows))["streak"] == {}


def test_two_different_failures_are_two_outages(tmp_path):
    """Четыре 401 и четыре 503 — не один сбой из восьми строк: 401 это истёкший ключ, 503 —
    отказ пула, и лечатся они противоположным."""
    rows = [_row(100.0, "a", 401), _row(110.0, "a", 401),
            _row(120.0, "a", 503), _row(130.0, "a", 503)]
    got = check_money.endpoint_health(_ledger(tmp_path, rows))["streak"]["a"]
    assert got == {"count": 2, "since": 120.0, "status": "503"}


def test_arms_do_not_share_a_streak(tmp_path):
    rows = [_row(100.0, "a", 503), _row(105.0, "b", 200), _row(110.0, "a", 503)]
    st = check_money.endpoint_health(_ledger(tmp_path, rows))["streak"]
    assert st["a"]["count"] == 2 and "b" not in st


def test_pulse_prints_the_streak_rather_than_the_last_sample():
    src = (BENCH / "pulse.py").read_text(encoding="utf-8")
    assert "consecutive" in src and 'health.get("streak")' in src
    # И запасной вариант остаётся: строка без серии всё ещё сообщает статус, а не молчит.
    assert "last call came back" in src


def test_a_streak_of_one_is_printed_as_the_single_sample_it_is():
    """Одиночный 401 между двумя 200 — не серия, и «1 consecutive 401s over 0 min» о нём хуже, чем
    «came back 401». §122 видел четыре таких за сорок секунд, и они значили истёкший ключ."""
    src = (BENCH / "pulse.py").read_text(encoding="utf-8")
    assert 'run["count"] >= 2' in src, "серия из одного напечатается как серия"
