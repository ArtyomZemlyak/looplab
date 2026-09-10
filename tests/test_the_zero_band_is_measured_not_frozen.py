"""§407. «Все 12 нулей корпуса — 41-47 с» — фраза, которую корпус перерос.

`pulse` объясняет, почему секунды нуля были диагнозом, ссылаясь на состояние корпуса. Померено
2026-09-10:

* нулей **14**, а не 12;
* `eval_seconds` идут от **4.2 с до 60.9 с**, медиана 42.1; внутри 41-47 — только семь;
* случай, о котором правило предупреждает, в корпусе ЕСТЬ: `remDL13` узел 0 — ноль на **4.2 с**,
  `remEE6` узел 3 — на 8.3 с.

Замороженное число и замороженная полоса, обе теперь неверны. Логика от этого не зависела (диагноз
берётся из ПРИЧИНЫ моста, §342, секунды — запасной вариант), но объяснение, на которое читатель
опирается, обязано быть верным. Здесь стоит якорь, который покраснеет, когда оно снова устареет.
"""
from __future__ import annotations

import glob
import statistics
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1] / "benchmarks"
sys.path.insert(0, str(BENCH))

import events_read  # noqa: E402

import pytest  # noqa: E402


def _corpus_zeros():
    out = []
    for run in glob.glob("/var/tmp/looplab-bench/model-probes/*/runs/*/run/events.jsonl"):
        probe = run.split("/model-probes/")[1].split("/")[0]
        for row in events_read.iter_events(run):
            if row.get("type") != "node_evaluated":
                continue
            data = row.get("data") or {}
            if data.get("metric") == 0 and isinstance(data.get("eval_seconds"), (int, float)):
                out.append((probe, float(data["eval_seconds"])))
    return out


def test_the_comment_no_longer_freezes_a_count_and_a_band():
    """Фраза осталась в файле как ЦИТАТА того, что там стояло, — и первая версия этого теста
    грепала именно её, поэтому падала на исправленном комментарии. Проверять надо не упоминание, а
    то, что старое утверждение больше не ВЫСКАЗЫВАЕТСЯ: рядом стоят измеренные числа и пометка, что
    оно перерослось (§358 с другой стороны)."""
    src = (BENCH / "pulse.py").read_text(encoding="utf-8")
    assert "4.2 s to 60.9 s" in src, "the measured spread is gone"
    assert "outgrown by the corpus" in src, "the sentence is stated as current again"
    quoted = src.index("All 12 corpus zeros are the")
    assert "It read" in src[max(0, quoted - 200):quoted], "the old claim is no longer marked as a quote"


@pytest.mark.corpus
def test_the_corpus_still_holds_a_zero_under_five_seconds():
    """Якорь: исчезнет быстрый ноль — и правило «ноль до пяти секунд = отказ мерить» снова станет
    теорией, о чём стоит узнать."""
    zeros = _corpus_zeros()
    if not zeros:
        pytest.skip("no zeros on this box")
    fast = [(p, s) for p, s in zeros if s < 5]
    assert fast, f"no zero under five seconds any more; the spread is now {min(s for _p,s in zeros):.1f}-{max(s for _p,s in zeros):.1f}"


@pytest.mark.corpus
def test_the_zero_spread_is_wider_than_the_old_sentence_claimed():
    zeros = _corpus_zeros()
    if len(zeros) < 5:
        pytest.skip("too few zeros to speak of a spread")
    secs = sorted(s for _p, s in zeros)
    inside = [s for s in secs if 41 <= s <= 47]
    assert len(inside) < len(secs), (
        "every zero is inside 41-47 again, so the old sentence would be true and this test is the "
        "thing that is stale")
    assert secs[0] < 41 or secs[-1] > 47, (secs[0], secs[-1])
    assert abs(statistics.median(secs) - 42.1) < 15, statistics.median(secs)
