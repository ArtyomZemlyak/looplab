"""§357. Возраст лога мерился часами, снятыми ДО работы, и уходил в минус.

`pulse` брал `time.time()` один раз наверху и им считал возраст каждой пробы. Между этим снимком и
`stat` он читает леджер метра (20 МБ), дважды обходит `/proc` и глобит деревья проб, — то есть
возраст занижался на всю преамбулу. У пробы, которая пишет непрерывно, он ушёл в минус, и таблица
напечатала `-0s`. Померено живьём на `pgr2`.

Напечатанное безобидно, правило — нет: `age > STALL_TIMEOUT` это единственное, что объявляет пробу
вставшей, и возраст, способный быть отрицательным, — это возраст, способный ошибаться в сторону
«свежо» на сколько угодно. Лог, датированный БУДУЩИМ (шаг часов, файл, скопированный с другой
коробки, mtime, сохранённый `cp -p`), читался бы как вечно свежий и потолок не пробил бы никогда.
Поэтому не зажимается молча, а называется.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1] / "benchmarks"
sys.path.insert(0, str(BENCH))
sys.path.insert(0, str(BENCH / "algotune"))

import pulse as pulse_mod  # noqa: E402


def _log(tmp_path, mtime=None):
    p = tmp_path / "events.jsonl"
    p.write_text("{}\n", encoding="utf-8")
    if mtime is not None:
        os.utime(p, (mtime, mtime))
    return str(p)


def test_the_clock_is_read_after_the_stat(tmp_path):
    """Единственное, что делает возраст неотрицательным для растущего файла."""
    path = _log(tmp_path, mtime=time.time())
    assert pulse_mod.log_age(path) >= 0.0


def test_a_stale_clock_is_what_produced_the_negative(tmp_path):
    """Воспроизведение дефекта: часы, снятые за секунду ДО записи файла, дают отрицательный
    возраст — ровно то, что печаталось как `-0s`."""
    now = time.time()
    path = _log(tmp_path, mtime=now + 1.0)
    assert pulse_mod.log_age(path, now) < 0.0


def test_an_injected_clock_is_used_as_given(tmp_path):
    """Тест владеет своими часами: `--now` не должен подменяться свежим временем."""
    path = _log(tmp_path, mtime=1000.0)
    assert pulse_mod.log_age(path, 1500.0) == 500.0


def test_a_log_dated_in_the_future_is_named_not_clamped():
    """Зажать в ноль значило бы объявить нормой файл, датированный будущим, — а он не пробьёт
    потолок простоя никогда."""
    assert pulse_mod.format_age(-5.0) == "  AHEAD!"
    assert "AHEAD" not in pulse_mod.format_age(-0.4), "полсекунды дрейфа — не тревога"


def test_an_ordinary_age_prints_as_before(tmp_path):
    assert pulse_mod.format_age(120.0).strip() == "120s"
    assert pulse_mod.format_age(-0.4).strip() == "0s"


def test_the_table_uses_the_helper():
    src = (BENCH / "pulse.py").read_text(encoding="utf-8")
    assert "age = log_age(found[0], args.now)" in src, "таблица снова считает возраст сама"
    assert "{format_age(age)}" in src, "таблица снова печатает сырой возраст"
