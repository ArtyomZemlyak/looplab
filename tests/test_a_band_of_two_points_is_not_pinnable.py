"""§364. Проверка советовала пришпилить полосу, которую её же две пробы не могут провалить.

`pgr2` дала `pagerank` вторую точку, и `check_test_tracks_train` тут же посоветовал: «UNPINNED
task(s): pagerank -- add the measured band to TEST_TRAIN_BANDS with the date». При n=2 полоса ЕСТЬ
её две точки: пришпилить их — значит завести правило, которое эти две пробы не провалят никогда.
Это §330 в самом малом масштабе, и комментарий над `TEST_TRAIN_BANDS` уже хранит запись о том, как
то же случилось при одной пробе (0.99296 вне 0.993-0.993 по одному округлению).

Планка не выдумана: это САМАЯ ТОНКАЯ из уже пришпиленных полос этого файла — 118, 11 и 10 проб,
значит 10, — и считается из таблицы, а не набирается руками, чтобы не разойтись с тем, что файл
действительно делает.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

BENCH = Path(__file__).resolve().parents[1] / "benchmarks"
sys.path.insert(0, str(BENCH))

import sweep_claims  # noqa: E402


def test_the_bar_is_computed_from_the_table_not_typed():
    assert sweep_claims.MIN_PROBES_TO_PIN == min(
        n for _lo, _hi, n in sweep_claims.TEST_TRAIN_BANDS.values())
    assert sweep_claims.MIN_PROBES_TO_PIN >= sweep_claims.MIN_PROBES_FOR_A_BAND


def test_pagerank_is_not_advised_to_be_pinned_at_two_probes():
    src = (BENCH / "sweep_claims.py").read_text(encoding="utf-8")
    assert "TOO THIN TO PIN" in src, "различие между «не пришпилено» и «пришпиливать рано» исчезло"
    assert "a rule its own probes cannot fail" in src, "причина не доходит до страницы"


def test_the_thin_and_the_narrow_cases_are_different_sentences():
    """«Одна проба» и «две пробы» — разные состояния: первое нельзя даже посчитать, второе можно
    посчитать и нельзя пришпилить. Одна фраза на оба лишила бы оператора действия."""
    src = (BENCH / "sweep_claims.py").read_text(encoding="utf-8")
    assert "too few probes for a band" in src and "band computable but TOO THIN TO PIN" in src


def test_every_unpinned_task_still_fails_the_claim():
    """Первый заход §364 позволил тонкой задаче ПРОЙТИ — и тест §337, который эту проверку и
    сторожит, покраснел. Правильно: непришпиленная задача это та, которую НИЧТО не судит, а зелёное
    «TEST tracks TRAIN, per task» при несудимой задаче — ровно то молчание, ради которого §337 и
    заведён. §364 меняет СОВЕТ, а не вердикт."""
    src = (BENCH / "sweep_claims.py").read_text(encoding="utf-8")
    assert "return not loud and not unpinned and not narrow, detail" in src, \
        "тонкая полоса снова проходит молча"


@pytest.mark.corpus
def test_the_live_check_holds_and_names_pagerank():
    ok, said = sweep_claims.check_test_tracks_train("/var/tmp/looplab-bench")
    if "cannot be driven" in said or "no probe" in said:
        return
    assert "pagerank" in said, said
    if "n=2" in said:
        assert "TOO THIN TO PIN" in said and "UNPINNED task(s): pagerank" in said, said
