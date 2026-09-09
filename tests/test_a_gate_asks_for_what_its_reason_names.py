"""§344. Ворота спрашивали каталог, а их собственная причина говорила «ИЗМЕРЕННЫЕ базовые времена».

Пустой `benchmarks/algotune/.baseline_times/` удовлетворяет первому и не содержит ничего от второго.
Такой каталог завёлся в свежем рабочем дереве 2026-09-08: оба теста запустились, «настоящая»
карточка откатилась к той же формулировке по имени файла, что и копия в скрэтче, и
`test_the_two_are_not_the_same_card` упал с сообщением, что карточки одинаковы — что было ПРАВДОЙ и
не говорило ничего о коде. Проверено на чистом `origin/master`: `mkdir` превращает два пропуска в
два падения, `rmdir` возвращает обратно. Карточке нужен ФАЙЛ с временами — его и спрашиваем.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_the_card_says_when_it_lost_its_timings import has_measured_timings  # noqa: E402


def test_an_empty_directory_is_not_a_measurement(tmp_path):
    (tmp_path / ".baseline_times").mkdir()
    assert not has_measured_timings(tmp_path / ".baseline_times")


def test_a_missing_directory_is_not_a_measurement(tmp_path):
    assert not has_measured_timings(tmp_path / "nowhere")


def test_one_timing_file_is_enough(tmp_path):
    d = tmp_path / ".baseline_times"
    d.mkdir()
    (d / "edge_expansion__train__w22x1r3.json").write_text('{"i0": 1.0}', encoding="utf-8")
    assert has_measured_timings(d)


def test_a_sidecar_alone_is_not_a_timing(tmp_path):
    """`.provenance.json` записывает УСЛОВИЯ съёма, а не времена. Каталог, где лежит только он,
    карточку не наполнит — ровно та же ошибка «файл есть, измерения нет», этажом ниже."""
    d = tmp_path / ".baseline_times"
    d.mkdir()
    (d / "edge_expansion__train__w22x1r3.json.provenance.json").write_text("{}", encoding="utf-8")
    assert not has_measured_timings(d)


def test_the_live_bench_checkout_has_them():
    """Якорь: на этой машине времена есть, значит ворота НЕ должны пропускать тесты мимо.

    ПРЕДУСЛОВИЕ — «каталог непустой», а не «каталог существует». Голый `mkdir
    .baseline_times` — это ровно тот случай, ради которого написан `has_measured_timings`
    (см. его docstring: пустой каталог, всплывший в свежем worktree 2026-09-08), и в свежем
    контейнере, где бенч-данные никогда не выкачивались, он же и получается. Такое состояние
    ворота обязаны пропустить в skip, а этот якорь — не считать тревогой: он про то, что кэш
    ОПУСТЕЛ, то есть файлы были и пропали. Различаем по наличию хоть каких-то файлов: ни одного —
    здесь просто ничего не материализовали; файлы есть, а времён нет — вот это и есть тревога.
    """
    times = Path(__file__).resolve().parents[1] / "benchmarks" / "algotune" / ".baseline_times"
    if not times.is_dir() or not any(p.is_file() for p in times.iterdir()):
        return
    assert has_measured_timings(times), "живой кэш линеек опустел -- это уже другая тревога"


def test_the_gate_itself_uses_it():
    """Функция полезна, только если её спрашивают ворота. Возврат к `TIMES.is_dir()` — это
    возврат ровно того падения, ради которого §344 написан."""
    src = (Path(__file__).resolve().parent /
           "test_the_card_says_when_it_lost_its_timings.py").read_text(encoding="utf-8")
    gate = src.split("needs_algotune = pytest.mark.skipif(", 1)[1].split("reason=", 1)[0]
    assert "has_measured_timings(TIMES)" in gate, gate
    assert "TIMES.is_dir()" not in gate, gate
