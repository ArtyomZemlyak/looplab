"""§366. Вердикт по полосе судил ОДИН процесс, а оценка порождает двадцать два.

Померено 2026-09-09 с четырьмя живыми пробами: три генерировали и имели по ОДНОМУ процессу, а `pgr4`
считала — и держала ШЕСТЬ. Воркер, чья привязка ушла за полосу, конкурировал бы с чужим измерением,
пока всякий прибор докладывает полосу, на которой РОДИЛСЯ движок. Это форма посторонних линеек
2026-09-07, этажом ниже.

Судится объединение дерева: полоса — множество, и вопрос в том, остаётся ли дерево ЦЕЛИКОМ внутри
одной. Скан `/proc` внедряемый по той же причине, что и у `lanes.probes`: без поддельного `/proc`
случай сбежавшего воркера не покраснеет никогда.
"""
from __future__ import annotations

import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1] / "benchmarks"
sys.path.insert(0, str(BENCH))

import lanes  # noqa: E402

LANE = lanes.parse_lane("0-10,48-58")
OTHER = lanes.parse_lane("11-21,59-69")


def _proc(tmp_path, tree):
    """`tree`: {pid: (ppid, cpus)} against a fake /proc."""
    root = tmp_path / "proc"
    root.mkdir()
    for pid, (ppid, _cpus) in tree.items():
        d = root / str(pid)
        d.mkdir()
        (d / "stat").write_text(f"{pid} (python) S {ppid} 0 0" + " 0" * 40, encoding="utf-8")
    return str(root), (lambda p: tree[p][1] if p in tree else set())


def test_a_tree_wholly_inside_its_lane_is_clean(tmp_path):
    proc, aff = _proc(tmp_path, {100: (1, LANE), 101: (100, LANE), 102: (101, LANE)})
    cpus = lanes.tree_cpus(100, proc=proc, affinity=aff)
    assert cpus == LANE
    assert lanes.lane_fault(cpus) is None


def test_a_worker_that_escaped_the_lane_is_caught(tmp_path):
    """Ровно тот случай, ради которого правка: движок на месте, ребёнок — нет."""
    proc, aff = _proc(tmp_path, {100: (1, LANE), 101: (100, LANE | {11})})
    cpus = lanes.tree_cpus(100, proc=proc, affinity=aff)
    assert cpus != LANE
    assert lanes.lane_fault(cpus) is not None


def test_a_worker_on_the_service_pair_is_the_sharper_alarm(tmp_path):
    proc, aff = _proc(tmp_path, {100: (1, LANE), 101: (100, {44})})
    got = lanes.lane_fault(lanes.tree_cpus(100, proc=proc, affinity=aff))
    assert got and "SERVICE lane" in got, got


def test_a_grandchild_is_walked_too(tmp_path):
    """Двадцать два воркера рождаются не напрямую от движка: forkserver стоит между."""
    proc, aff = _proc(tmp_path, {100: (1, LANE), 101: (100, LANE), 102: (101, {44})})
    got = lanes.lane_fault(lanes.tree_cpus(100, proc=proc, affinity=aff))
    assert got and "SERVICE lane" in got, got


def test_a_process_that_exits_under_the_walk_is_not_a_leak(tmp_path):
    """`/proc` меняется под читателем; исчезнувший воркер — не утечка полосы."""
    proc, aff = _proc(tmp_path, {100: (1, LANE), 101: (100, LANE)})
    def flaky(pid):
        if pid == 101:
            raise OSError("gone")
        return LANE
    assert lanes.tree_cpus(100, proc=proc, affinity=flaky) == LANE


def test_pulse_judges_the_tree_not_the_engine():
    src = (BENCH / "pulse.py").read_text(encoding="utf-8")
    assert "lanes.tree_cpus(row[\"pid\"])" in src, "pulse снова судит только верхний процесс"
