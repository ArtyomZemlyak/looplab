"""§372. Процентиль по шести точкам — это ранг, и называть его процентилем значит завышать.

Первое настоящее применение `outlier_check` (§371) выдало: *«share_deep_research=26.96 at the 0th
pct (corpus median 37.21)»* — по корпусу из ШЕСТИ законченных прогонов `pagerank`. При малом `n`
полоса p5..p95 — это почти весь размах, «снаружи» значит «минимум или максимум», и ничего тоньше;
«at the 0th pct» обещает разрешение, которого у выборки нет.

Порог не выдуман: это то `n`, при котором 5 % выборки перестают быть меньше одной точки, то есть
1/0.05 = 20. Ниже него печатается ранг («the lowest of 6»), а в подвале говорится, что попасть туда
— обычная судьба двоих из любых `n`, а не редкость.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1] / "benchmarks"
sys.path.insert(0, str(BENCH))

import outlier_check  # noqa: E402


def _run_task(root, probe, task, pairs):
    """`pairs`: [(phase, cost)] -- обе фазы обязаны быть и в корпусе, и у живой пробы, иначе ключа
    для сравнения просто нет и «снаружи полосы» никогда не наступает."""
    d = root / probe / "runs" / task / "run"
    d.mkdir(parents=True)
    (d / "events.jsonl").write_text("".join(
        json.dumps({"v": 1, "seq": i, "ts": float(i), "type": "llm_usage",
                    "data": {"cost": c}}) + "\n"
        for i, (_p, c) in enumerate(pairs)), encoding="utf-8")
    # ФОРМА, КОТОРУЮ ЧИТАЕТ `phase_series`: спан зовётся `generation`, а фаза лежит в
    # `attributes.phase`. Первая фикстура клала имя фазы в `name` -- инструмент не видел ни одного
    # спана, все доли выходили None, и «ничего снаружи полосы» печаталось над пустотой.
    (d / "spans.jsonl").write_text("".join(
        json.dumps({"kind": "agent", "name": "generation", "start": float(i),
                    "attributes": {"cost": c, "phase": ph}}) + "\n"
        for i, (ph, c) in enumerate(pairs)), encoding="utf-8")


CORPUS = [("propose", 0.1)] * 5 + [("plan", 0.1)] * 5      # plan = 50 %
LIVE   = [("plan", 0.1)]                                   # plan = 100 %, the highest of any n


def test_the_threshold_is_where_five_per_cent_is_one_point():
    assert outlier_check.RANK_NOT_PERCENTILE == 20, outlier_check.RANK_NOT_PERCENTILE


def test_a_small_corpus_prints_a_rank(tmp_path, monkeypatch, capsys):
    for i in range(6):
        _run_task(tmp_path, f"done{i}", "pagerank", CORPUS)
    _run_task(tmp_path, "live", "pagerank", LIVE)
    monkeypatch.setattr(outlier_check.lanes, "probes",
                        lambda root: [{"probe": "live", "lane": "0-10", "pid": 1}])
    outlier_check.main(["--root", str(tmp_path)])
    out = capsys.readouterr().out
    assert "of 6" in out, out
    assert "0th pct" not in out, "процентиль по шести точкам снова выдан за процентиль: " + out


def test_a_small_corpus_says_what_being_flagged_means(tmp_path, monkeypatch, capsys):
    for i in range(6):
        _run_task(tmp_path, f"done{i}", "pagerank", CORPUS)
    _run_task(tmp_path, "live", "pagerank", LIVE)
    monkeypatch.setattr(outlier_check.lanes, "probes",
                        lambda root: [{"probe": "live", "lane": "0-10", "pid": 1}])
    outlier_check.main(["--root", str(tmp_path)])
    out = capsys.readouterr().out
    assert "is a RANK, not a rarity" in out, out
    assert "2 of any n land there" in out, out


def test_a_large_corpus_keeps_the_percentile(tmp_path, monkeypatch, capsys):
    """Там, где выборка это позволяет, процентиль — правильное слово, и подвал молчит."""
    for i in range(25):
        _run_task(tmp_path, f"done{i}", "pagerank", CORPUS)
    _run_task(tmp_path, "live", "pagerank", LIVE)
    monkeypatch.setattr(outlier_check.lanes, "probes",
                        lambda root: [{"probe": "live", "lane": "0-10", "pid": 1}])
    outlier_check.main(["--root", str(tmp_path)])
    out = capsys.readouterr().out
    assert "pct" in out, out
    assert "is a RANK, not a rarity" not in out, out
