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


def test_the_bands_are_pinned_not_derived(tmp_path, monkeypatch):
    """§330: a band computed from the probes it judges cannot be failed by them — so PINNED means a
    LITERAL table, and a verdict that follows the table.

    Review 2026-09-22, TST-05: this was `"BEFORE_FIRST_NODE_BANDS" in src and "cannot be failed by
    them" in src`, and the second literal is the comment that states the reason — a table computed
    from the corpus, with that comment above it, kept the test green."""
    import ast

    tree = ast.parse((BENCH / "sweep_claims.py").read_text(encoding="utf-8"))
    table = [n.value for n in tree.body if isinstance(n, ast.Assign) and any(
        isinstance(t, ast.Name) and t.id == "BEFORE_FIRST_NODE_BANDS" for t in n.targets)]
    assert len(table) == 1, "the bands must be bound exactly once, at module level"
    bands = ast.literal_eval(table[0])                  # raises on anything computed
    assert bands == sweep_claims.BEFORE_FIRST_NODE_BANDS, "rebound after the literal"
    assert bands and all(isinstance(lo, (int, float)) and isinstance(hi, (int, float)) and lo < hi
                         for lo, hi in bands.values())

    # ...and the verdict is the TABLE's: the same three probes pass under the pinned band and fail
    # once the table says otherwise, which a band re-derived from those probes never could.
    rows = [_row("edge_expansion", 30.0), _row("edge_expansion", 34.0), _row("edge_expansion", 38.0)]
    assert sweep_claims.check_waste_before_the_first_node(_bench(tmp_path / "a", rows))[0]
    monkeypatch.setitem(sweep_claims.BEFORE_FIRST_NODE_BANDS, "edge_expansion", (0.0, 10.0))
    ok, detail = sweep_claims.check_waste_before_the_first_node(_bench(tmp_path / "b", rows))
    assert not ok and "edge_expansion median 34 % outside 0-10" in detail, detail
