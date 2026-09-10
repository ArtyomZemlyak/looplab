"""§400. «Запиннена на n» — это то, ИЗ ЧЕГО полоса выведена, а не то, что она проверила.

Правило §330 записано в самом файле: полоса, выведенная из проб, которые судит, не может быть ими
провалена — поэтому у каждой полосы записано `n` на момент пиннинга. Чего строка не говорила: сколько
проб пришло ПОСЛЕ. Померено 2026-09-10 по собственному пулу проверки:

    discrete_log    запиннена на 11, сейчас 13 -> **2** судимых
    pde_heat1d      запиннена на 10, сейчас 11 -> **1**
    edge_expansion  запиннена на 118, сейчас 118 -> **0**
    pagerank        запиннена на 10, сейчас 10 -> **0**

Две полосы из четырёх до сих пор стоят ровно на тех пробах, из которых выведены. Это не дефект
полосы — это состояние доказательств; но читатель, видя «n=118», принимает его за 118 проб проверки,
тогда как это 118 проб ВЫВОДА и ноль проверки.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1] / "benchmarks"
sys.path.insert(0, str(BENCH))

import sweep_claims  # noqa: E402


def test_a_band_that_has_met_later_probes_reports_the_count():
    pinned = sweep_claims.TEST_TRAIN_BANDS["discrete_log"][2]
    got = dict((t, (j, n)) for t, j, n in
               sweep_claims.band_exposure({"discrete_log": [1.0] * (pinned + 2)}))
    assert got["discrete_log"] == (2, pinned), got


def test_a_band_standing_on_exactly_its_own_data_reports_zero():
    pinned = sweep_claims.TEST_TRAIN_BANDS["pagerank"][2]
    got = dict((t, j) for t, j, _n in
               sweep_claims.band_exposure({"pagerank": [1.0] * pinned}))
    assert got["pagerank"] == 0, got


def test_a_shrunken_corpus_is_not_reported_as_progress():
    """Проба может исчезнуть с диска; отрицательное «судимых» — это тоже «полоса ничего не судила»."""
    pinned = sweep_claims.TEST_TRAIN_BANDS["pagerank"][2]
    got = dict((t, j) for t, j, _n in
               sweep_claims.band_exposure({"pagerank": [1.0] * (pinned - 3)}))
    assert got["pagerank"] == -3, got


def test_an_unpinned_task_has_no_exposure_row():
    """У незапиннённой задачи нечему быть подверженным — она в другой ветке проверки."""
    assert sweep_claims.band_exposure({"brand_new_task": [1.0, 1.0]}) == []


def _bench(tmp_path, rows):
    tool = tmp_path / "looplab" / "benchmarks"
    tool.mkdir(parents=True)
    (tool / "probe_summary.py").write_text(
        "import json\nprint(json.dumps(" + json.dumps(rows) + "))\n", encoding="utf-8")
    return str(tmp_path)


def _row(probe, task, test, best):
    return {"probe": probe, "task": task, "test": test, "nodes": [best]}


def test_the_check_says_a_band_is_still_resting_on_its_own_data(tmp_path):
    """Сентенция ЧЕРЕЗ ВЫЗОВ (§399's lesson): проверка зовёт probe_summary подпроцессом."""
    pinned = sweep_claims.TEST_TRAIN_BANDS["pagerank"][2]
    rows = [_row(f"p{i}", "pagerank", 0.98, 1.0) for i in range(pinned)]
    _ok, detail = sweep_claims.check_test_tracks_train(_bench(tmp_path, rows))
    assert "STILL RESTING ON ITS OWN DATA" in detail, detail
    assert f"pagerank (pinned at {pinned}, judged 0 since)" in detail, detail
    assert "cannot have been failed by one" in detail, detail


def test_the_check_reports_a_band_that_has_judged_later_probes(tmp_path):
    pinned = sweep_claims.TEST_TRAIN_BANDS["pagerank"][2]
    rows = [_row(f"p{i}", "pagerank", 0.98, 1.0) for i in range(pinned + 3)]
    _ok, detail = sweep_claims.check_test_tracks_train(_bench(tmp_path, rows))
    assert "judged since pinning: pagerank +3" in detail, detail
    assert "STILL RESTING" not in detail, detail


def test_a_probe_outside_the_band_is_still_the_loud_part(tmp_path):
    """Подверженность не заслоняет главного: выход за полосу по-прежнему называется поимённо."""
    pinned = sweep_claims.TEST_TRAIN_BANDS["pagerank"][2]
    rows = [_row(f"p{i}", "pagerank", 0.98, 1.0) for i in range(pinned)]
    rows.append(_row("odd", "pagerank", 2.0, 1.0))
    _ok, detail = sweep_claims.check_test_tracks_train(_bench(tmp_path, rows))
    assert "OUTSIDE the pinned band: odd on pagerank" in detail, detail
