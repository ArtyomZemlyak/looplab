"""§387. Леджер ставит время НАЧАЛА вызова, а все приборы читали его как конец.

Строка попадает в леджер, когда вызов ВЕРНУЛСЯ — иначе в ней не было бы `latency_ms`. Но поле `ts` —
момент, когда вызов ушёл. Померено на 1008 вызовах, сведённых между леджером и собственными
спанами `llm_usage` пяти проб: `ts + latency_ms` попадает в спан пробы с точностью **0.00 с** и по
медиане, и по p90, а один `ts` промахивается на 11.6 с по медиане и на 181 с по p90 (худшее ~640 с).

§335 и §339 записали убеждение словами «леджер записывает вызов, когда тот заканчивается». Половина
верна; та половина, которая решает число, — нет. `call age`, построенный на `ts`, объявлял последний
вызов на целую задержку более старым, чем он есть: у `remDL14` с генерациями по 357-387 с «545 с
тишины» на деле были 188. Тот же сдвиг решал `_still_calling` — то есть считается ли плечо,
сидящее в генерации, способным ещё выдать спан, за который счётчик уже взял деньги.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1] / "benchmarks"
sys.path.insert(0, str(BENCH))

import check_money  # noqa: E402


def _ledger(tmp_path, rows):
    p = tmp_path / "meter.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return str(p)


def _row(arm, ts, latency_s, status=200, cost=0.001):
    return {"arm": arm, "ts": ts, "latency_ms": latency_s * 1000.0,
            "status": status, "cost": cost, "stream": True, "metered": True}


def test_a_calls_end_is_its_start_plus_its_latency():
    assert check_money.completed_at(_row("a", 1000.0, 357.0)) == 1357.0


def test_a_row_without_a_latency_falls_back_to_its_start():
    """Не выдумывать длительность: без `latency_ms` конец неизвестен, и начало — нижняя граница."""
    assert check_money.completed_at({"arm": "a", "ts": 1000.0}) == 1000.0
    assert check_money.completed_at({"arm": "a", "ts": 1000.0, "latency_ms": None}) == 1000.0


def test_an_unreadable_stamp_is_zero_not_an_exception():
    assert check_money.completed_at({"arm": "a"}) == 0.0
    assert check_money.completed_at({"arm": "a", "ts": "-"}) == 0.0


def test_the_newest_row_is_the_one_that_CAME_BACK_last(tmp_path):
    """Фикстура СПОРИТ с багом: вызов, начавшийся РАНЬШЕ, заканчивается ПОЗЖЕ.

    По началу «новейшая» — короткая строка (ts=1500); по концу — длинная (1000+900=1900).
    Пока порядок брался по `ts`, эти две строки были неразличимы от правильного ответа только
    потому, что в живом леджере длинные вызовы редко перекрывают короткие.
    """
    path = _ledger(tmp_path, [_row("a", 1000.0, 900.0, cost=0.02),
                              _row("a", 1500.0, 30.0, status=503, cost=0.0)])
    newest = check_money.endpoint_health(path)["newest"]["a"]
    assert newest[0] == 1900.0, newest          # completion of the LONG call
    assert newest[1] == "200", newest           # ...and therefore its status, not the 503's
    assert newest[2] == 1000.0, newest          # the start is kept, and it is the long call's


def test_two_overlapping_calls_are_ordered_by_when_they_came_back(tmp_path):
    """Второй опровергатель, и он нужен отдельно.

    Мутация «сравнивать НАЧАЛО новой строки с сохранённым КОНЦОМ» на предыдущей фикстуре приходила
    ЗЕЛЁНОЙ: там короткий вызов начинался внутри длинного, `1500 > 1900` ложно, и неверный код
    случайно давал верный ответ. Ловит её только ПЕРЕКРЫТИЕ, в котором позже начавшийся вызов и
    заканчивается позже: правильный ответ — он, а сравнение начала с концом его отвергнет.
    """
    path = _ledger(tmp_path, [_row("a", 1000.0, 900.0),                    # 1000 -> 1900
                              _row("a", 1500.0, 900.0, status=503, cost=0.0)])  # 1500 -> 2400
    newest = check_money.endpoint_health(path)["newest"]["a"]
    assert newest[0] == 2400.0, newest
    assert newest[1] == "503", newest
    assert newest[2] == 1500.0, newest


def test_the_arm_that_is_refusing_is_read_from_the_row_that_came_back_last(tmp_path):
    path = _ledger(tmp_path, [_row("a", 1000.0, 10.0),
                              _row("a", 1100.0, 5.0, status=503, cost=0.0)])
    health = check_money.endpoint_health(path)
    assert health["refusing"] == ["a"], health


def test_an_arm_in_a_long_generation_still_counts_as_calling(tmp_path, monkeypatch):
    """Плечо, чей вызов ушёл 400 с назад и вернулся 20 с назад, ЗВОНИТ.

    По старому чтению `now - ts = 400 c` — больше окна в 300 с, и плечо считалось замолчавшим,
    хотя счётчик уже взял с него деньги за вызов, спан которого ещё не записан.
    """
    now = 10_000.0
    path = _ledger(tmp_path, [_row("a", now - 400.0, 380.0)])
    newest = check_money.endpoint_health(path)["newest"]
    monkeypatch.setattr(check_money, "_ended", lambda *a, **k: False)
    assert check_money._still_calling("/nonexistent", "a", newest, now=now) is True
    # ...and one that really did stop is still read as stopped.
    other = tmp_path / "x"
    other.mkdir()
    newest2 = check_money.endpoint_health(_ledger(other, [_row("a", now - 4000.0, 30.0)]))["newest"]
    assert check_money._still_calling("/nonexistent", "a", newest2, now=now) is False


def test_pulse_no_longer_says_the_ledger_records_a_call_when_it_ends():
    src = (BENCH / "pulse.py").read_text(encoding="utf-8")
    assert "the ledger records a call when it ends" not in src, "§335's false half is back"
    assert "CAME BACK" in src, "the open-call sentence lost the correction"


def test_the_live_ledger_still_carries_the_latency_the_fix_depends_on():
    """Якорь: если `latency_ms` исчезнет из строк, поправка молча выродится в старое поведение."""
    import os
    path = "/var/tmp/looplab-bench/meter/meter.jsonl"
    if not os.path.exists(path):
        import pytest
        pytest.skip("no live ledger on this box")
    rows = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if line.startswith("{"):
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    pass
    tail = [r for r in rows[-200:] if r.get("status") == 200]
    have = [r for r in tail if isinstance(r.get("latency_ms"), (int, float)) and r["latency_ms"] > 0]
    assert len(have) >= 0.9 * len(tail), f"only {len(have)} of {len(tail)} recent 200s carry a latency"


def test_a_calls_age_is_never_negative(tmp_path, monkeypatch, capsys):
    """§389. Ловушка, которую §387 сделал видимой: часы читались ДО леджера.

    На загруженной коробке обход занимает минуты, вызов успевает вернуться ПОСЛЕ снятия часов — и
    `call age` печатался как **-207s**. Отрицательный возраст не маленькая ошибка, а предложение,
    которое не может быть истинным. Часы теперь снимаются ПОСЛЕ чтения леджера (правило, записанное
    в этом же файле двумя сотнями строк ниже), а печать зажата снизу нулём — потому что
    ПОДСУНУТЫЕ часы всё ещё могут оказаться раньше строки.
    """
    import pulse
    src = (BENCH / "pulse.py").read_text(encoding="utf-8")
    ledger_first = src.index("health = check_money.endpoint_health(ledger)")
    clock = src.index("now = args.now if args.now is not None else time.time()")
    assert clock > ledger_first, "the clock is read before the ledger again"
    assert "max(0.0, now - called[0])" in src, "the negative age is printable again"
