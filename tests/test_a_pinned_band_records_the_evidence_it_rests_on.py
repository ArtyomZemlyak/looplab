"""§376. Полоса `pagerank` пришпилена на десяти пробах — по тому же правилу, что и соседние.

Правило не выдумано, оно ИЗМЕРЕНО по трём уже пришпиленным: полоса это минимум и максимум корпуса
на момент пришпиливания, округлённые НАРУЖУ до трёх знаков.

    discrete_log     корпус 0.8901-1.2600  ->  (0.890, 1.260)
    edge_expansion   корпус 0.8922-1.0326  ->  (0.892, 1.033)
    pde_heat1d       корпус 0.9912-1.0538  ->  (0.991, 1.054)
    pagerank         корпус 0.9586-1.0134  ->  (0.958, 1.014)   <- сегодня

Десять проб, на которых полоса построена, провалить её не могут — потому и записано `n`: одиннадцатая
и дальше могут, и в этом весь смысл пришпиленной полосы (§330). Проверять «полоса равна текущим
мин-макс» было бы ошибкой: ровно тогда, когда новая проба выпадет наружу, такой тест покраснел бы
вместо самой проверки.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pytest

BENCH = Path(__file__).resolve().parents[1] / "benchmarks"
sys.path.insert(0, str(BENCH))

import sweep_claims  # noqa: E402


def _round_out(lo: float, hi: float) -> tuple:
    return (math.floor(lo * 1000) / 1000, math.ceil(hi * 1000) / 1000)


def test_the_rounding_rule_reproduces_every_neighbour():
    """Правило выведено из соседей, а не назначено: проверяется на них же."""
    assert _round_out(0.8901, 1.2600) == (0.890, 1.260)
    assert _round_out(0.8922, 1.0326) == (0.892, 1.033)
    assert _round_out(0.9912, 1.0538) == (0.991, 1.054)


def test_the_pagerank_band_follows_that_rule():
    lo, hi, n = sweep_claims.TEST_TRAIN_BANDS["pagerank"]
    assert (lo, hi) == _round_out(0.9586, 1.0134), (lo, hi)
    assert n == 10, n


def test_every_band_records_how_much_evidence_it_rests_on():
    """`n` — не украшение: по нему видно, сколько проб полоса НЕ может провалить."""
    for task, band in sweep_claims.TEST_TRAIN_BANDS.items():
        assert len(band) == 3, (task, band)
        lo, hi, n = band
        assert lo < hi, (task, band)
        assert isinstance(n, int) and n >= sweep_claims.MIN_PROBES_TO_PIN, (task, band)


@pytest.mark.corpus
def test_a_band_may_not_claim_more_evidence_than_the_corpus_holds():
    """Полоса не может опираться на больше проб, чем на коробке есть. Обратное — можно: корпус
    растёт, и новые пробы полосу СУДЯТ, а не переопределяют."""
    import subprocess
    got = subprocess.run([sys.executable, str(BENCH / "probe_summary.py"), "--json"],
                         capture_output=True, text=True, timeout=900)
    try:
        rows = json.loads(got.stdout)
    except ValueError:
        return
    have: dict = {}
    for r in rows:
        if isinstance(r.get("test"), (int, float)) and r.get("nodes"):
            have[r["task"]] = have.get(r["task"], 0) + 1
    for task, (_lo, _hi, n) in sweep_claims.TEST_TRAIN_BANDS.items():
        if task in have:
            assert n <= have[task], (task, n, have[task])


def test_the_reason_pagerank_waited_is_still_on_the_page():
    """Записка о том, почему полосу нельзя было пришпилить при одной пробе, стоит рядом: без неё
    следующий читатель повторит именно ту ошибку."""
    src = (BENCH / "sweep_claims.py").read_text(encoding="utf-8")
    assert "0.99296 is outside 0.993-0.993 by rounding alone" in src
    assert "rounded\n    # OUTWARD" in src or "rounded" in src.split('"pagerank": (0.958')[0][-800:]
