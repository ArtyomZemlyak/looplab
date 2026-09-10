"""§406. Решение оставить панель предвидения слепой стоит на цифре ПАРЫ, а проверка следила за половинами.

Источник несёт и цифру, и линию пересмотра дословно (`looplab/engine/proposal_cues.py`, сверено
2026-09-10):

    «`foresight_rank` and `hyp_prioritize` are 2.4 % of spend between them and stay blind. That is a
     decision, not an oversight … Revisit if either grows past a few per cent.»

Линия пересмотра — на фазу, а ДОКАЗАТЕЛЬСТВО — про пару. Проверка судила каждую фазу против 3 %
отдельно: 2.1 % и 1.2 % обе проходят, строка читалась «both under 3 % of spend». А пара сегодня —
**3.3 %**, примерно на 38 % выше числа, на котором решение принято. Никто не сказал, потому что никто их не
складывал: основание решения уехало, пока проверка смотрела на половины.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BENCH = REPO / "benchmarks"
sys.path.insert(0, str(BENCH))

import sweep_claims  # noqa: E402


def test_the_quote_the_threshold_rests_on_is_still_in_the_source():
    """Выдуманные цитаты — сверяй дословно. Уедет фраза — уедет и основание проверки."""
    src = (REPO / "looplab" / "engine" / "proposal_cues.py").read_text(encoding="utf-8")
    assert "Revisit if either grows past a few per cent." in src, "the revisit line is gone"
    assert "2.4 % of spend between them" in src, "the recorded pair figure is gone"


def test_the_pair_is_added_up_and_compared_with_the_recorded_figure():
    said = sweep_claims.blind_pair_sentence({"foresight_rank": 2.1, "hyp_prioritize": 1.2})
    assert "together they are 3.3 % of spend" in said, said
    assert "2.4 % recorded when the decision was made" in said, said
    # +37, not the +38 the live sweep prints: the live shares carry full precision (2.14 + 1.22),
    # this fixture carries the DISPLAYED ones. Asserting a number measured in another context is the
    # same mistake as an invented quote -- 2.1 + 1.2 is 37.5 % over 2.4, and 37.49999999999999
    # formats as +37.
    assert "+37 %" in said, said
    assert "the decision's own basis has grown" in said, said


def test_a_pair_still_under_the_recorded_figure_says_so_without_the_alarm():
    said = sweep_claims.blind_pair_sentence({"foresight_rank": 1.0, "hyp_prioritize": 0.5})
    assert "together they are 1.5 %" in said, said
    assert "has grown" not in said, said


def test_phases_that_are_not_the_pair_do_not_count():
    """`plan` слеп не был и в паре не состоит: сложение должно брать ровно две фазы."""
    said = sweep_claims.blind_pair_sentence({"plan": 9.3, "foresight_rank": 2.1})
    assert "together they are 2.1 %" in said, said


def test_no_blind_phase_means_no_sentence():
    assert sweep_claims.blind_pair_sentence({}) == ""
    assert sweep_claims.blind_pair_sentence({"plan": 9.3}) == ""


def test_the_live_check_carries_the_pair(tmp_path):
    """Через ВЫЗОВ, а не только через функцию (§399's lesson)."""
    import os
    if not os.path.isdir("/var/tmp/looplab-bench/model-probes"):
        import pytest
        pytest.skip("no bench on this box")
    _ok, detail = sweep_claims.check_money_cue_reaches_the_choosers("/var/tmp/looplab-bench")
    assert "STILL BLIND" in detail, detail
    assert "together they are" in detail and "recorded when the decision was made" in detail, detail
