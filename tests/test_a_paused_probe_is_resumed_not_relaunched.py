"""Шлюз падал дважды за смену на 23.8 и 13.5 минуты, лестница ретраев движка — ~2.5.

§338. Отказ длиннее лестницы на порядок кончается одинаково: Исследователь получает деградированный
фолбэк, предлагать нечего, прогон АВТО-ПРИОСТАНАВЛИВАЕТСЯ, держа оплаченное состояние. Дважды за
день я снимал это руками.

Пауза правильная и остаётся: движок отказывается работать на пустых фолбэках — иначе отказ
провайдера превратился бы в плоский бессмысленный результат. Не хватало второй половины: заметить,
что эндпойнт вернулся, и подобрать прогон с того места, где он стоит.

Три отказа, потому что автоматическое возобновление ТРАТИТ: только то, что `_paused` считает паузой
(решает по трате, §228); только когда эндпойнт отвечает, и проверка идёт через СЛУЖЕБНОЕ плечо;
и не больше `--max-resumes` раз на пробу, считая по её же событиям.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1] / "benchmarks"
sys.path.insert(0, str(BENCH))

import resume_paused  # noqa: E402


def _probe(root: Path, name: str, events: list, lane: str = "11-21,59-69") -> None:
    d = root / "model-probes" / name / "runs" / "discrete_log" / "run"
    d.mkdir(parents=True)
    (d / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    (root / "model-probes" / name / "INSTRUMENT.txt").write_text(
        f"probe:          {name}\nlane:           {lane}\n", encoding="utf-8")


PAUSE = {"type": "pause", "data": {"reason": "auto-paused: the provider failed"}}
EXITED = {"type": "run_loop_exited", "data": {"reason": "paused"}}


def _usage(c):
    return {"type": "llm_usage", "data": {"cost": c}}


def test_it_reports_a_paused_probe_and_does_not_launch_by_default(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(resume_paused, "endpoint_answers", lambda *a, **k: True)
    _probe(tmp_path, "p1", [_usage(0.35), PAUSE, EXITED])
    resume_paused.main(["--bench", str(tmp_path)])
    out = capsys.readouterr().out
    assert "p1: paused with $0.3500 held" in out, out
    assert "Run with --launch" in out, out
    # И ПОЛОСА В НАПЕЧАТАННОЙ КОМАНДЕ — ЕЁ СОБСТВЕННАЯ. Возобновление на другой полосе это другое
    # измерение (§262: 3 % между полосами; §314: порядок между ширинами), а подсказка оператору —
    # ровно та команда, которую он выполнит.
    assert "taskset -c 11-21,59-69" in out, out


def test_the_endpoint_must_answer_first(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(resume_paused, "endpoint_answers", lambda *a, **k: False)
    _probe(tmp_path, "p2", [_usage(0.35), PAUSE, EXITED])
    resume_paused.main(["--bench", str(tmp_path)])
    assert "the endpoint is still refusing -- nothing resumed" in capsys.readouterr().out


def test_a_run_at_its_ceiling_is_complete_and_never_resumed(tmp_path, capsys, monkeypatch):
    """§228/§213: 16 из 105 прогонов, дошедших до потолка, записаны как крах разработчика. Они
    ЗАВЕРШЕНЫ, и возобновление такого стоило `freeB3` лишних $0.1056."""
    monkeypatch.setattr(resume_paused, "endpoint_answers", lambda *a, **k: True)
    _probe(tmp_path, "atceiling", [_usage(1.0041), PAUSE, EXITED])
    resume_paused.main(["--bench", str(tmp_path)])
    assert "no probe is paused and owed work" in capsys.readouterr().out


def test_the_resume_ceiling_stops_a_loop_against_a_dead_provider(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(resume_paused, "endpoint_answers", lambda *a, **k: True)
    _probe(tmp_path, "p3", [_usage(0.2), PAUSE, EXITED, EXITED, EXITED, EXITED])
    resume_paused.main(["--bench", str(tmp_path), "--max-resumes", "2"])
    out = capsys.readouterr().out
    assert "at the --max-resumes ceiling" in out, out
    assert "not paid for in a loop" in out, out


def test_the_lane_comes_from_the_instrument_not_from_a_default(tmp_path):
    """Возобновление на ДРУГОЙ полосе — другое измерение: §262 намерил 3 % между полосами, §314 —
    порядок между ширинами."""
    _probe(tmp_path, "p4", [_usage(0.1), PAUSE, EXITED], lane="22-32,70-80")
    assert resume_paused.probe_lane(str(tmp_path), "p4") == "22-32,70-80"
    assert resume_paused.probe_lane(str(tmp_path), "nosuch") == "0-10,48-58"
