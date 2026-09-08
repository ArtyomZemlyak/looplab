"""§358. `pulse` печатал полосу и никогда её не судил.

Две причины проверять это на ЭТОЙ коробке, а не считать очевидным:

* всякий измеряющий инструмент здесь запускается пришпиленным к СЕРВИСНОЙ паре 44-47,92-95, так что
  проба, налезшая на неё, была бы возмущена тем самым обходом, который её меряет, — второй прибор
  меняет то, что меряет первый;
* проба, пришпиленная к ПОДМНОЖЕСТВУ полосы, — это ровно форма посторонних линеек 2026-09-07: они
  шли на двух ядрах, `{0, 48}`, и часами писали в живой кэш, пока всякий инструмент сообщал ту
  полосу, на которой они родились.

Полоса — это МНОЖЕСТВО: `48-58,0-10` и `0-10,48-58` одна и та же, и два места, сравнивающих строки,
разошлись бы на этом. Поэтому определения лежат в `lanes.py`, а не у каждого вызывающего.
"""
from __future__ import annotations

import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1] / "benchmarks"
sys.path.insert(0, str(BENCH))

import lanes  # noqa: E402


def test_a_probe_exactly_on_a_bench_lane_is_clean():
    for spec in lanes.BENCH_LANES:
        assert lanes.lane_fault(lanes.parse_lane(spec)) is None, spec


def test_the_same_lane_written_backwards_is_the_same_lane():
    """Сравнение строк развалилось бы здесь."""
    assert lanes.lane_fault(lanes.parse_lane("48-58,0-10")) is None


def test_overlapping_the_service_pair_is_the_sharper_alarm():
    """Даже одно ядро: инструменты обхода живут там, и нагрузка пошла бы в измеряемое."""
    got = lanes.lane_fault(lanes.parse_lane("0-10,48-58") | {44})
    assert got and "SERVICE lane" in got and "[44]" in got, got


def test_a_subset_of_a_lane_is_named_as_such():
    """Форма посторонних линеек: два ядра из двадцати двух."""
    got = lanes.lane_fault({0, 48})
    assert got and "SUBSET" in got and "2 of the 22" in got, got


def test_a_set_on_no_lane_at_all_is_reported():
    got = lanes.lane_fault({44 + 1000, 44 + 1001})
    assert got and "is on no bench lane" in got, got


def test_no_affinity_is_not_silently_fine():
    assert lanes.lane_fault(set()) == "no cpu affinity at all"


def test_the_service_pair_matches_the_one_the_snapshot_uses():
    """Копия живёт в `snapshot.sh` как shell-умолчание и импортировать эту не может. Если они
    разойдутся, обход и снимок будут спорить о том, где стоят инструменты."""
    import re
    sh = (BENCH / "snapshot_timer.sh").read_text(encoding="utf-8")
    got = re.search(r'SNAPSHOT_SERVICE_LANE:-([^}"\s]+)', sh)
    assert got, "снимок больше не называет сервисную полосу"
    # ТОЧНОЕ равенство, не вхождение: `44-47` — подстрока `44-47,92-95`, и проверка на `in`
    # пропустила бы урезанную пару целиком.
    assert got.group(1) == lanes.SERVICE_LANE, (got.group(1), lanes.SERVICE_LANE)


def test_pulse_prints_the_fault():
    src = (BENCH / "pulse.py").read_text(encoding="utf-8")
    assert 'lanes.lane_fault(row["cpus"])' in src, "pulse снова не судит полосу"
    # ВЕТКА, а не только строка: `if False:` оставляет и вызов, и текст на месте.
    assert "        if fault:\n            print(f'      LANE: {name} {fault}')" in src, \
        "вердикт посчитан и не напечатан"
