"""§379. Пять якорей стоили 675 с одной выборки; шестой по медленности тест — 6.5 с.

Замерено `--durations` 2026-09-09:

    381.02s  test_the_live_sweep_prints_both        (гоняла ВЕСЬ обход ради одной строки)
     79.07s  test_the_live_check_holds_and_names_pagerank
     74.15s  test_a_band_may_not_claim_more_evidence_than_the_corpus_holds
     71.23s  test_the_live_corpus_shows_the_asymmetry
     70.34s  test_the_live_corpus_separates_them
      6.55s  <следующий по медленности тест выборки>

Все пять — мои, из последних обходов. Якоря нужны: настоящее чтение 150 деревьев ловит расхождение,
которого фикстура не поймает. Но 381 с ради одной строки — не более сильная проверка, а более
медленная: утверждение принадлежит ОДНОЙ проверке, её и надо звать.

Маркер `corpus` заведён отдельно от `live`: тот, по собственному описанию, значит «нужна доступная
модель», а не «читает корпус». Смешать их значило бы выключать вместе с медленными тестами ещё и
сетевые — две разные причины пропуска под одним именем.
"""
from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"


def test_the_marker_is_registered_and_distinct_from_live():
    cfg = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    markers = cfg["tool"]["pytest"]["ini_options"]["markers"]
    names = [m.split(":")[0] for m in markers]
    assert "corpus" in names and "live" in names, names
    corpus = next(m for m in markers if m.startswith("corpus:"))
    live = next(m for m in markers if m.startswith("live:"))
    assert "endpoint" in live and "endpoint" not in corpus, (live, corpus)


def test_no_test_spawns_the_whole_sweep_to_read_one_line():
    """Тот самый 381-секундный тест. Двадцать одна проверка ради строки одной из них."""
    guilty = []
    for path in sorted(TESTS.glob("test_*.py")):
        src = path.read_text(encoding="utf-8", errors="replace")
        if re.search(r"subprocess\.run\(\s*\[\s*sys\.executable[^]]*sweep_claims\.py", src):
            guilty.append(path.name)
    assert not guilty, f"гоняют весь обход из теста: {guilty}"


def test_every_slow_corpus_anchor_carries_the_marker():
    """Пять замеренных якорей. Список именной, а не эвристический: эвристика «читает /var/tmp»
    накрывает 58 файлов, из которых медленны пять."""
    anchors = {
        "test_a_band_of_two_points_is_not_pinnable.py": "test_the_live_check_holds_and_names_pagerank",
        "test_a_pinned_band_records_the_evidence_it_rests_on.py":
            "test_a_band_may_not_claim_more_evidence_than_the_corpus_holds",
        "test_which_lever_the_loop_reaches_for.py": "test_the_live_corpus_shows_the_asymmetry",
        "test_which_kernel_not_just_whether.py": "test_the_live_corpus_separates_them",
        "test_both_halves_of_the_reference_claim_are_driven.py": "test_the_live_check_prints_both",
    }
    for filename, func in anchors.items():
        src = (TESTS / filename).read_text(encoding="utf-8")
        assert f"@pytest.mark.corpus\ndef {func}(" in src, (filename, func)


def test_the_anchors_still_read_the_live_box_and_are_not_hollowed_out():
    """Отметить медленный тест маркером и выпотрошить — это не ускорение, а потеря якоря."""
    src = (TESTS / "test_both_halves_of_the_reference_claim_are_driven.py").read_text(
        encoding="utf-8")
    # ТОЛЬКО ТЕЛО ФУНКЦИИ: срез до конца файла захватывал соседние тесты, где та же строка есть,
    # и мутация «выпотрошить якорь» уходила зелёной.
    body = src.split("def test_the_live_check_prints_both")[1].split("\ndef ")[0]
    assert "/var/tmp/looplab-bench" in body, body[:300]
    assert "by CALLS rather than imports" in body, body[:300]
