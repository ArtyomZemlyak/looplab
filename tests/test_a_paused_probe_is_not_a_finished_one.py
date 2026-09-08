"""«Проб не идёт» — одно предложение про два противоположных состояния.

§334. `remDL13` авто-приостановилась, когда провайдер лёг посреди предложения: Исследователь получил
503, вернул деградированный фолбэк, предлагать было нечего, узлов ноль. `run_probe.sh` отчитался
`rc=0` и «чемпион: НЕТ», а `pulse` сказал ровно то же, что говорит о безупречно законченной пробе, —
«no bench probe running». Это разные распоряжения: одна сделана, вторая держит $0.1141 оплаченного
состояния и просит `looplab resume`, а перезапуск платит за него второй раз.

Распоряжение берётся из `arm_fidelity._paused`, которое решает по ТРАТЕ, а не по слову «pause»:
прогон, упёршийся в потолок денег, завершён, как бы это ни было записано (§228, §213).
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1] / "benchmarks"
sys.path.insert(0, str(BENCH))

import pulse  # noqa: E402


def _probe(root: Path, name: str, events: list) -> None:
    d = root / "model-probes" / name / "runs" / "t" / "run"
    d.mkdir(parents=True)
    (d / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")


def _usage(cost):
    return {"type": "llm_usage", "data": {"cost": cost}}


PAUSE = {"type": "pause", "data": {"reason": "auto-paused: the Researcher's LLM provider failed"}}
FINISH = {"type": "run_finished", "data": {"reason": "budget_exhausted"}}


def test_a_paused_run_with_money_left_is_owed_work(tmp_path):
    _probe(tmp_path, "remDL13", [_usage(0.11), PAUSE])
    got = pulse.paused_probes(str(tmp_path))
    assert [r["probe"] for r in got] == ["remDL13"], got
    assert abs(got[0]["spend"] - 0.11) < 1e-9


def test_a_finished_run_is_not(tmp_path):
    _probe(tmp_path, "done", [_usage(0.9), FINISH])
    assert pulse.paused_probes(str(tmp_path)) == []


def test_a_pause_at_the_ceiling_is_complete_however_it_was_worded(tmp_path):
    """§228: 16 из 105 прогонов, дошедших до потолка, записаны как «Developer session crashed» —
    они ЗАВЕРШЕНЫ, и звать их «должными работу» это то, что стоило freeB3 лишних $0.1056 (§213)."""
    _probe(tmp_path, "atceiling", [_usage(1.0041), PAUSE])
    assert pulse.paused_probes(str(tmp_path)) == []


def test_an_old_pause_is_history_not_news(tmp_path):
    _probe(tmp_path, "lastweek", [_usage(0.2), PAUSE])
    p = tmp_path / "model-probes" / "lastweek" / "runs" / "t" / "run" / "events.jsonl"
    old = time.time() - 8 * 86_400
    import os
    os.utime(p, (old, old))
    assert pulse.paused_probes(str(tmp_path)) == []


def test_the_line_tells_the_operator_not_to_relaunch():
    src = (BENCH / "pulse.py").read_text(encoding="utf-8")
    assert "do NOT relaunch (that pays twice)" in src
    assert "`looplab resume`" in src
