"""§401. Три обхода подряд начинались с «эта проба тратит медленнее сестёр» — и это одно явление.

Каждый раз ответ был один: вызовы ДЛИННЕЕ, а не медленнее. По 153 законченным пробам с 30+
оплаченными вызовами это монотонно:

    1 узел   n= 10   800 токенов
    2 узла   n= 39   577
    3 узла   n= 88   423
    4+       n= 16   428

Проба, получившая мало узлов, пишет длинные тела. Куда направлена причинность, таблица не решает —
эссе на вызов оставляет меньше бюджета на оценки, а прогон, который не может посадить узел, только и
может, что писать. §392 видит ту же популяцию со стороны денег.

И отдельный урок: ведро «ноль узлов» пришлось выбросить. В нём было шесть проб, и ни одна не про то:
четыре БРОШЕННЫХ плеча, чьи деревья остались только в архиве, одна, умершая на 93-токенных вызовах, и
живая, чей профиль ещё пишется. У них вызовы короткие потому, что они ОСТАНОВИЛИСЬ, — и объединение
превратило 1952 токена в 405, развернув тренд, ради которого таблица и заведена.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1] / "benchmarks"
sys.path.insert(0, str(BENCH))

import probe_summary  # noqa: E402


def _ledger(tmp_path, rows):
    p = tmp_path / "meter.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return str(p)


def _calls(arm, n, tokens, status=200):
    return [{"arm": arm, "status": status, "completion_tokens": tokens, "ts": float(i)}
            for i in range(n)]


def test_the_median_is_taken_per_probe_then_across_probes(tmp_path):
    """Инача одна болтливая проба с сотней вызовов утянет ведро на себя."""
    rows = _calls("a", 40, 1000) + _calls("b", 200, 100)
    got = probe_summary.completions_by_node_count({"a": 1, "b": 1}, _ledger(tmp_path, rows),
                                                  finished={"a", "b"})
    assert got == [(1, 2, 550.0)], got


def test_a_probe_with_too_few_calls_is_not_a_profile(tmp_path):
    rows = _calls("a", 29, 1000) + _calls("b", 40, 500)
    got = probe_summary.completions_by_node_count({"a": 1, "b": 1}, _ledger(tmp_path, rows),
                                                  finished={"a", "b"})
    assert got == [(1, 1, 500.0)], got


def test_an_unfinished_probe_is_left_out(tmp_path):
    """Живая или брошенная проба коротка потому, что остановилась, — не потому, что кратко пишет."""
    rows = _calls("done", 40, 500) + _calls("abandoned", 40, 90)
    got = probe_summary.completions_by_node_count({"done": 1, "abandoned": 0},
                                                  _ledger(tmp_path, rows), finished={"done"})
    assert got == [(1, 1, 500.0)], got


def test_without_a_finished_set_nothing_is_excluded(tmp_path):
    """`finished=None` — старое поведение, чтобы вызывающий решал явно, а не по умолчанию."""
    rows = _calls("a", 40, 500) + _calls("b", 40, 90)
    got = probe_summary.completions_by_node_count({"a": 1, "b": 0}, _ledger(tmp_path, rows))
    assert got == [(0, 1, 90.0), (1, 1, 500.0)], got


def test_four_or_more_nodes_share_one_bucket(tmp_path):
    rows = _calls("a", 40, 400) + _calls("b", 40, 500) + _calls("c", 40, 600)
    got = probe_summary.completions_by_node_count({"a": 4, "b": 7, "c": 12},
                                                  _ledger(tmp_path, rows), finished={"a","b","c"})
    assert got == [(4, 3, 500.0)], got


def test_a_non_200_does_not_count_as_a_call(tmp_path):
    rows = _calls("a", 40, 500) + _calls("a", 60, 9000, status=503)
    got = probe_summary.completions_by_node_count({"a": 1}, _ledger(tmp_path, rows), finished={"a"})
    assert got == [(1, 1, 500.0)], got


def test_a_missing_ledger_is_no_table_rather_than_a_crash(tmp_path):
    assert probe_summary.completions_by_node_count({"a": 1}, str(tmp_path / "nope.jsonl")) == []


def test_the_live_corpus_still_shows_the_trend():
    """Якорь: если тренд исчезнет, об этом стоит узнать, а не продолжать печатать таблицу."""
    import os
    ledger = "/var/tmp/looplab-bench/meter/meter.jsonl"
    if not os.path.exists(ledger):
        import pytest
        pytest.skip("no ledger on this box")
    import glob
    sys.path.insert(0, str(BENCH))
    import events_read
    probes, finished = {}, set()
    for d in sorted(glob.glob("/var/tmp/looplab-bench/model-probes/*/")):
        name = d.rstrip("/").split("/")[-1]
        n = sum(1 for p in glob.glob(d + "runs/*/*/events.jsonl")
                for e in events_read.iter_events(p) if e.get("type") == "node_evaluated")
        probes[name] = n
        if os.path.isfile(d + "champion_solver.py"):
            finished.add(name)
    got = dict((nodes, med) for nodes, _n, med in
               probe_summary.completions_by_node_count(probes, ledger, finished=finished))
    assert got, "no finished probe has 30+ priced calls"
    if 1 in got and 3 in got:
        assert got[1] > got[3], got


def test_the_footer_note_carries_the_within_run_correction():
    """§403. Таблица межпробная, и читатель берёт из неё «длинные вызовы мешают узлам».

    Внутри прогона стрелка смотрит НАОБОРОТ: медиана ответа 284 токена ДО первого узла и 538 ПОСЛЕ,
    и растёт она в 127 пробах из 154. Примечание обязано это нести, иначе таблица подсказывает
    вывод, который собственные данные не поддерживают.
    """
    src = (BENCH / "probe_summary.py").read_text(encoding="utf-8")
    assert "127 of 154" in src and "284 -> 538" in src, "the within-run correction is gone"
    assert "not 'long calls prevent nodes'" in src, "the note stopped naming what it refutes"
