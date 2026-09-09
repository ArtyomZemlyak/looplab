"""§395. Доля накладных расходов зависела от того, какой режим записал строку ПОСЛЕДНИМ.

Проверка «ускорение делится на время референса» брала из журнала самопроверки последнюю строку по
задаче — какой бы режим её ни снял. А знаменатели у режимов РАЗНЫЕ: `pde_heat1d` кэширует 146.49 мс
широко против 78.32 мс последовательно. Отсюда: утром 2026-09-09 обход печатал
«pde_heat1d: 3 % harness … bounds the FIXED part at 20.0 %», а днём, после двух широких чтений, —
«47 % harness … 1.3 %». Та же задача, та же коробка, то же утверждение, разница в пятнадцать раз, и
ничто не сказало, что под ним сменился режим.

Пара обязана держаться: граница делит на лучший счёт ПРОБЫ, а пробы бегут широко. Последовательный
знаменатель против широкого счёта — две разные величины в одной дроби.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1] / "benchmarks"
sys.path.insert(0, str(BENCH))

import sweep_claims  # noqa: E402
import ruler_check  # noqa: E402


def _log(tmp_path, rows):
    p = tmp_path / "log.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return p


def _row(task, regime, cached, solver, stamp):
    return {"task": task, "regime": regime, "cached_ms": cached, "solver_ms": solver,
            "stamp": stamp, "subset": "test"}


def test_a_serial_reading_written_last_does_not_become_the_answer(tmp_path):
    """Опровергатель: последовательная строка идёт ПОСЛЕ широкой, и всё равно не побеждает."""
    path = _log(tmp_path, [
        _row("pde_heat1d", "w22x1r3", 146.49, 78.08, "2026-09-09T17:16:46"),
        _row("pde_heat1d", "lane22r3", 78.32, 76.0, "2026-09-09T18:00:00"),
    ])
    got = sweep_claims.denominator_halves(path)
    assert got["pde_heat1d"][0] == 146.49, got
    assert got["pde_heat1d"][2] == "2026-09-09T17:16:46", got


def test_the_newest_reading_IN_THE_REGIME_still_wins(tmp_path):
    """Внутри режима — по-прежнему последняя: свежесть не отменяется, отменяется смешение."""
    path = _log(tmp_path, [
        _row("t", "w22x1r3", 100.0, 50.0, "2026-09-01T00:00:00"),
        _row("t", "w22x1r3", 120.0, 60.0, "2026-09-02T00:00:00"),
    ])
    assert sweep_claims.denominator_halves(path)["t"][0] == 120.0


def test_the_double_underscore_spelling_is_the_same_regime(tmp_path):
    """`__w22x1r3` и `w22x1r3` — одно и то же; две записи не должны разъезжаться по написанию."""
    path = _log(tmp_path, [_row("t", "__w22x1r3", 90.0, 45.0, "2026-09-03T00:00:00")])
    assert sweep_claims.denominator_halves(path)["t"][0] == 90.0


def test_a_reading_with_no_regime_is_not_adopted(tmp_path):
    """Оба режима существовали, когда писались такие строки: приписать её некуда."""
    path = _log(tmp_path, [_row("t", None, 90.0, 45.0, "2026-09-03T00:00:00")])
    assert sweep_claims.denominator_halves(path) == {}


def test_the_serial_regime_can_be_asked_for_explicitly(tmp_path):
    """Число само по себе не «неправильное» — оно про другой режим, и спросить его можно."""
    path = _log(tmp_path, [
        _row("t", "w22x1r3", 146.49, 78.08, "2026-09-09T17:00:00"),
        _row("t", "lane22r3", 78.32, 76.0, "2026-09-09T18:00:00"),
    ])
    got = sweep_claims.denominator_halves(path, regime=ruler_check.SERIAL_REGIME)
    assert got["t"][0] == 78.32, got


def test_a_row_whose_halves_are_impossible_is_skipped(tmp_path):
    """`solver >= cached` — не измерение: разность стала бы отрицательной долей."""
    path = _log(tmp_path, [_row("t", "w22x1r3", 50.0, 50.0, "s"),
                           _row("u", "w22x1r3", 50.0, 60.0, "s")])
    assert sweep_claims.denominator_halves(path) == {}


def test_the_reported_line_names_the_regime_it_used(tmp_path):
    """СЕНТЕНЦИЯ, а не греп по файлу.

    Первая версия искала строку в модуле — а печатающих мест ДВА, и мутация, убравшая режим из
    первого, проходила: второе упоминание держало греп зелёным. §358/§391/§393 в четвёртый раз;
    проверяется то, что возвращает сама проверка.
    """
    bench = tmp_path / "bench"
    log = bench / "looplab" / "benchmarks" / "algotune"
    log.mkdir(parents=True)
    (log / "ruler_selfcheck_log.jsonl").write_text(
        json.dumps(_row("t", "w22x1r3", 100.0, 60.0, "2026-09-09T00:00:00")) + "\n",
        encoding="utf-8")
    run = bench / "model-probes" / "p1" / "runs" / "t" / "run"
    run.mkdir(parents=True)
    (run / "events.jsonl").write_text("", encoding="utf-8")
    (bench / "model-probes" / "p1" / "final.json").write_text(
        json.dumps({"subset": "test", "speedup": 50.0}), encoding="utf-8")
    _ok, detail = sweep_claims.check_denominator_composition(str(bench))
    assert "40 % harness" in detail, detail
    assert ruler_check.CAMPAIGN_REGIME in detail, detail


def test_the_no_score_branch_names_the_regime_too(tmp_path):
    """Второе печатающее место — то самое, из-за которого греп по файлу ничего не ловил."""
    bench = tmp_path / "bench"
    log = bench / "looplab" / "benchmarks" / "algotune"
    log.mkdir(parents=True)
    (log / "ruler_selfcheck_log.jsonl").write_text(
        json.dumps(_row("t", "w22x1r3", 100.0, 60.0, "2026-09-09T00:00:00")) + "\n",
        encoding="utf-8")
    (bench / "model-probes").mkdir(parents=True)          # no probe, so no score to bound with
    _ok, detail = sweep_claims.check_denominator_composition(str(bench))
    assert "no score here" in detail, detail
    assert ruler_check.CAMPAIGN_REGIME in detail, detail


def test_the_live_log_answers_in_the_campaign_regime_for_every_task():
    """Якорь: если задача останется без широкого чтения, проверка обязана сказать это, а не молчать."""
    import os
    # DRIFT_LOG is relative to the BENCH ROOT, not to the checkout -- the first version of
    # this anchor pointed at the checkout, found nothing and SKIPPED, which is an anchor
    # that measures nothing while looking green.
    path = Path("/var/tmp/looplab-bench") / sweep_claims.DRIFT_LOG
    if not os.path.exists(path):
        import pytest
        pytest.skip("no drift log on this box")
    got = sweep_claims.denominator_halves(path)
    assert set(got) >= {"pde_heat1d", "edge_expansion", "discrete_log", "pagerank"}, sorted(got)
    # и знаменатель широкого режима у pde_heat1d — 146.5, а не 78.3
    assert abs(got["pde_heat1d"][0] - 146.49) < 0.1, got["pde_heat1d"]
