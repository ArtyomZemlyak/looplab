"""§340. Читание, не назвавшее свой режим, датируется — не додумывается.

`sweep_claims.check_ruler_constants` собирало пул строкой `row.get("regime") or CAMPAIGN_REGIME`:
любая строка лога, не назвавшая режим, молча попадала в широкий. Самые уверенные на вид строки
отчёта — `discrete_log: 4 quiet wide read(s) mean 1.0192 +-0.0034` и такая же у edge_expansion —
оказались ЧЕТЫРЬМЯ ДОДУМАННЫМИ читаниями и НУЛЁМ записанных. На pde_heat1d подмешивание сдвинуло
вердикт (1.0331 +-0.0068 по 8 против 1.0427 +-0.0094 по 4) и вдвое ужало стандартную ошибку —
полоса затянулась свидетельством, которого не брали.

Приписать режим теперь может только факт: строка должна быть РАНЬШЕ любого следа последовательного
режима на этой машине (mtime файла кэша либо первая размеченная строка самого лога — берётся более
ранний из двух, чтобы копия файла или поздно добавленное поле делали правило только строже).
Всё, что позже, выбрасывается и пересчитывается: на строке нет ничего, по чему её приписать.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1] / "benchmarks"
sys.path.insert(0, str(BENCH))

import ruler_check  # noqa: E402
import sweep_claims  # noqa: E402

WIDE, SERIAL = ruler_check.CAMPAIGN_REGIME, ruler_check.SERIAL_REGIME


def _bench(tmp_path, rows, serial_cache_mtime):
    """A bench root holding a drift log and one serial cache file with a chosen mtime."""
    algo = tmp_path / "looplab" / "benchmarks" / "algotune"
    (algo / ".baseline_times").mkdir(parents=True)
    cache = algo / ".baseline_times" / f"queens_with_obstacles__test__{SERIAL}.json"
    cache.write_text(json.dumps({"i0": 1.0}), encoding="utf-8")
    os.utime(cache, (serial_cache_mtime, serial_cache_mtime))
    (algo / "ruler_selfcheck_log.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    (tmp_path / "model-probes").mkdir()
    return str(tmp_path)


def _row(task, stamp, values, regime=None):
    row = {"task": task, "stamp": stamp, "subset": "test", "median": values[len(values) // 2],
           "values": values, "busy_cpus_outside_lane": 0}
    if regime:
        row["regime"] = regime
    return row


# 2026-09-06T17:32:25 -- the first serial cache file this box ever wrote.
CUTOFF = __import__("datetime").datetime.fromisoformat("2026-09-06T17:32:25").timestamp()
BEFORE, AFTER = "2026-09-06T12:53:02", "2026-09-06T21:05:14"


def test_an_untagged_reading_after_both_regimes_existed_is_not_called_wide(tmp_path):
    """Строка без режима, взятая когда оба режима уже были, приписана быть не может."""
    bench = _bench(tmp_path, [_row("pde_heat1d", AFTER, [2.0, 2.0, 2.0, 2.0])], CUTOFF)
    ok, said = sweep_claims.check_ruler_constants(bench)
    assert "2.0000" not in said, f"додуманное читание попало в широкий пул: {said}"
    assert "NONE attributable" in said and "4 reading(s) recorded" in said, said


def test_an_untagged_reading_from_before_the_serial_regime_is_labelled_inferred(tmp_path):
    """Строка до появления последовательного режима приписывается широкому — но НАЗЫВАЕТСЯ."""
    bench = _bench(tmp_path, [_row("pde_heat1d", BEFORE, [3.0, 3.0, 3.0, 3.0])], CUTOFF)
    ok, said = sweep_claims.check_ruler_constants(bench)
    assert "3.0000" in said, said
    assert "INFERRED" in said, f"додуманное читание выдано за записанное: {said}"


def test_the_inferred_count_is_in_readings_not_in_sittings(tmp_path):
    """«1 of them INFERRED» рядом с «4 quiet wide read(s)» читается как одно сомнительное из
    четырёх, когда все четыре — одна и та же неразмеченная посадка. Единица счёта должна совпадать
    с единицей, которую называет соседнее число."""
    bench = _bench(tmp_path, [_row("pde_heat1d", BEFORE, [3.0, 3.0, 3.0, 3.0])], CUTOFF)
    _, said = sweep_claims.check_ruler_constants(bench)
    assert "4 quiet wide read(s)" in said and "4 of them INFERRED" in said, said


def test_a_tagged_reading_carries_no_doubt(tmp_path):
    """Размеченная строка ничего не додумывает — на ней нет ни INFERRED, ни DROPPED."""
    bench = _bench(tmp_path, [_row("pde_heat1d", AFTER, [4.0, 4.0], regime=WIDE)], CUTOFF)
    _, said = sweep_claims.check_ruler_constants(bench)
    assert "4.0000" in said and "INFERRED" not in said and "DROPPED" not in said, said


def test_the_cutoff_takes_the_earlier_of_the_two_traces(tmp_path):
    """След режима ищется в двух независимых местах: mtime файла кэша и первая размеченная строка
    самого лога. Берётся более ранний — копия файла или поздно добавленное поле могут сделать
    правило только строже, но не пропустить неприписуемое читание."""
    rows = [_row("pde_heat1d", BEFORE, [5.0, 5.0]),
            _row("pagerank", "2026-09-06T09:00:00", [1.0, 1.0], regime=SERIAL)]
    # Файл кэша написан ПОЗЖЕ, чем лог впервые назвал последовательный режим.
    bench = _bench(tmp_path, rows, CUTOFF)
    _, said = sweep_claims.check_ruler_constants(bench)
    assert "5.0000" not in said, f"взята поздняя из двух границ: {said}"
    assert "NONE attributable" in said, said
