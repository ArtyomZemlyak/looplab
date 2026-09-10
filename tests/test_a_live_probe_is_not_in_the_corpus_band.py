"""§360. Живая проба попала в полосу, собранную из законченных, и перевернула вердикт.

`before_pct` и `after_pct` — доли собственной траты пробы, и обе движутся всю её жизнь: `before_pct`
равен 100 % на первом узле и падает с каждым потраченным долларом, `after_pct` растёт между узлами
и схлопывается на каждом (`probe_summary` пишет второе дословно, а §347 — запись о том, как я всё
равно построил на нём тревогу).

Поймано на живом: 2026-09-08 идущая `pgr2` потратила $0.4925 при первом узле на $0.3877, то есть её
`before_pct` читался как **79 %** — и полоса `pagerank`, шириной в одну пробу до того утра,
перевернулась в «OUTSIDE the pinned band: pagerank median 79 % outside 20-60». К потолку в $1 та же
проба читалась бы примерно как 39 %.

Живость берётся `os.sched_getaffinity` через `lanes.probes()`, а НЕ по терминальному событию:
`freeB3` и `remDL` не несут `run_finished` и остановились недели назад — «не закончена» выбросила бы
из корпуса две законные исторические пробы.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

BENCH = Path(__file__).resolve().parents[1] / "benchmarks"
sys.path.insert(0, str(BENCH))

import sweep_claims  # noqa: E402


def test_a_running_probe_is_named_live(monkeypatch):
    monkeypatch.setattr(sweep_claims.lanes, "probes",
                        lambda *a, **k: [{"probe": "pgr2", "cpus": {1}}])
    assert "pgr2" in sweep_claims.live_probes("/var/tmp/looplab-bench")


def test_an_idle_box_holds_nobody_out(monkeypatch):
    monkeypatch.setattr(sweep_claims.lanes, "probes", lambda *a, **k: [])
    assert sweep_claims.live_probes("/var/tmp/looplab-bench") == set()


def test_liveness_is_the_process_not_a_terminal_event():
    """`freeB3` и `remDL` не несут `run_finished`, но давно не двигаются. Правило «не закончена»
    выбросило бы их из корпуса; правило «есть процесс» — нет."""
    import arm_fidelity
    root = "/var/tmp/looplab-bench/model-probes"
    if not Path(root, "freeB3").is_dir():
        # SKIP, not `return`: this anchor reads the LIVE bench corpus, and a box without one
        # must report "not checked" rather than print a green dot for a check that never ran.
        pytest.skip("no freeB3 probe on this box")
    assert arm_fidelity.is_finished(root, "freeB3") is False
    assert "freeB3" not in sweep_claims.live_probes("/var/tmp/looplab-bench")


def test_both_waste_checks_hold_the_live_probe_out():
    """Оба конца пары читают долю собственной траты, и оба должны держать живое вне корпуса."""
    src = (BENCH / "sweep_claims.py").read_text(encoding="utf-8")
    assert src.count("live = live_probes(bench)") == 2, "одна из двух проверок снова берёт живое"
    assert src.count('r.get("probe") not in live') == 2, "фильтр применён не в обеих"


def test_the_holdout_is_said_not_silent():
    """Проверка, у которой корпус молча меняет размер, каждый обход печатает другую полосу без
    строки, объясняющей почему."""
    src = (BENCH / "sweep_claims.py").read_text(encoding="utf-8")
    assert src.count("live probe(s) held out") == 2, "оговорка не доходит до обеих строк"
    assert src.count("+ aside)") == 2, "оговорка посчитана и не напечатана"
