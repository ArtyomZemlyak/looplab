"""§72 сказал, что трата ПОСЛЕ последнего узла читается только рядом с тратой ДО первого. Проверялась
половина пары.

Прогнано 2026-09-08 по 141 пробе, дошедшей до узла: медиана **34 %** бюджета уходит до первой
оценки, максимум 91 %. И четыре худших — одна и та же задача: `remPde` 90.6 %, `remPde4` 85.2 %,
`remPde5` 83.3 %, `remPde2` 78.9 %, все `pde_heat1d`, все с единственным узлом. На этой задаче
доллар почти не покупает поиска — он уходит на дорогу до первой оценки.

Полосы запиннены (§330): полоса, посчитанная по тем же пробам, которые судит, не может быть
провалена.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1] / "benchmarks"
sys.path.insert(0, str(BENCH))

import sweep_claims  # noqa: E402

STUB = '''#!/usr/bin/env python3
import pathlib
print(pathlib.Path(__file__).with_name("rows.json").read_text())
'''


def _bench(tmp_path, rows) -> str:
    tools = tmp_path / "looplab" / "benchmarks"
    tools.mkdir(parents=True)
    (tools / "probe_summary.py").write_text(STUB, encoding="utf-8")
    (tools / "rows.json").write_text(json.dumps(rows), encoding="utf-8")
    return str(tmp_path)


def _row(task, before, reached=True):
    return {"probe": f"p{before}", "task": task, "before_pct": before, "reached_a_node": reached}


def test_a_task_inside_its_pinned_band_passes(tmp_path):
    rows = [_row("edge_expansion", 30.0), _row("edge_expansion", 34.0), _row("edge_expansion", 38.0)]
    ok, detail = sweep_claims.check_waste_before_the_first_node(_bench(tmp_path, rows))
    assert ok, detail
    assert "edge_expansion 34 %" in detail, detail


def test_a_task_whose_median_leaves_its_band_is_named(tmp_path):
    """Полоса `edge_expansion` — 20-45 %; медиана 60 % значит, что задача изменилась, а не проба."""
    rows = [_row("edge_expansion", 58.0), _row("edge_expansion", 60.0), _row("edge_expansion", 62.0)]
    ok, detail = sweep_claims.check_waste_before_the_first_node(_bench(tmp_path, rows))
    assert not ok, detail
    assert "OUTSIDE the pinned band: edge_expansion median 60 % outside 20-45" in detail, detail


def test_pde_heat1d_is_allowed_to_be_expensive_because_it_measurably_is(tmp_path):
    """Фикстура, расходящаяся с общей полосой: 68 % на `pde_heat1d` — это норма задачи (четыре из
    пяти худших проб корпуса на ней), и общая полоса объявила бы её поломкой."""
    rows = [_row("pde_heat1d", 66.0), _row("pde_heat1d", 68.0), _row("pde_heat1d", 90.0)]
    ok, detail = sweep_claims.check_waste_before_the_first_node(_bench(tmp_path, rows))
    assert ok, detail
    assert "pde_heat1d 68 %" in detail, detail


def test_a_probe_that_never_reached_a_node_is_not_in_the_median(tmp_path):
    """У пробы без единого узла «трата до первого узла» не определена: это весь её бюджет, и
    засчитать его значило бы двигать медиану каждым ранним падением."""
    rows = [_row("edge_expansion", 30.0), _row("edge_expansion", 34.0),
            {"probe": "dead", "task": "edge_expansion", "before_pct": 100.0,
             "reached_a_node": False}]
    _, detail = sweep_claims.check_waste_before_the_first_node(_bench(tmp_path, rows))
    assert "2 probe(s)" in detail, detail


def test_the_bands_are_pinned_not_derived():
    src = (BENCH / "sweep_claims.py").read_text(encoding="utf-8")
    assert "BEFORE_FIRST_NODE_BANDS" in src
    assert "cannot be failed by them" in src
