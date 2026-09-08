"""§345. Пустое место после даты читалось как «отмечать нечего», а значило «никто не записал».

`ruler_check` печатает условия съёма из сайдкара `.provenance.json` (§297) в скобках. У записи без
сайдкара скобок не было ВООБЩЕ — ровно как у записи, снятой на тихой коробке, где отмечать нечего.
Это противоположные состояния: «измерено при условиях, которые я могу показать» и «под чем это
снято, не записал никто».

Померено 2026-09-08: 9 из 56 записей не несут условий, и все восемь линеек, которыми делятся ШИРОКИЕ
оценки на этой коробке (`discrete_log`, `edge_expansion`, `pde_heat1d`, `pagerank`), — из этих
девяти. Широкая линейка `pde_heat1d` заодно та самая, что самопроверка находит на 3.7 % в стороне.
Связь не доказана; это причина, по которой пустое место должно перестать читаться как чистый лист.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1] / "benchmarks"
CHECK = BENCH / "ruler_check.py"


def _dir(tmp_path, entries):
    """`entries`: (name, times, provenance-or-None)."""
    d = tmp_path / ".baseline_times"
    d.mkdir()
    for name, times, prov in entries:
        (d / name).write_text(json.dumps(times), encoding="utf-8")
        if prov is not None:
            (d / (name + ".provenance.json")).write_text(json.dumps(prov), encoding="utf-8")
    return d


def _run(d):
    return subprocess.run([sys.executable, str(CHECK), str(d)],
                          capture_output=True, text=True, timeout=600)


TIMES = {f"i{i}": 1.0 + i / 1000 for i in range(100)}
QUIET = {"eval_workers": 1, "loadavg": [10.4, 9.0, 8.0]}


def test_an_entry_with_no_sidecar_says_so(tmp_path):
    d = _dir(tmp_path, [("pde_heat1d__test__w22x1r3.json", TIMES, None)])
    out = _run(d).stdout
    assert "conditions NOT recorded" in out, out


def test_an_entry_with_a_sidecar_does_not_say_so(tmp_path):
    d = _dir(tmp_path, [("pde_heat1d__test__lane22r3.json", TIMES, QUIET)])
    out = _run(d).stdout
    assert "conditions NOT recorded" not in out, out
    assert "load 10.4" in out, out


def test_the_blank_and_the_quiet_reading_are_not_the_same_line(tmp_path):
    """Суть: две записи в одном файле — одна с условиями, другая без — должны выглядеть РАЗНО.
    Если строки совпадут, различие, ради которого §297 писал сайдкары, исчезнет с экрана."""
    d = _dir(tmp_path, [("pde_heat1d__test__w22x1r3.json", TIMES, None),
                        ("pde_heat1d__test__lane22r3.json", TIMES, QUIET)])
    lines = [l for l in _run(d).stdout.splitlines()
             if l.startswith("pde_heat1d")]      # строки таблицы, не сводка под ней
    assert len(lines) == 2, lines
    tails = {l.split("100")[-1].split(":")[-1] for l in lines}
    assert len(tails) == 2, f"обе записи выглядят одинаково: {lines}"


def test_the_count_is_summarised_and_the_files_named(tmp_path):
    """Читатель, просматривающий пятьдесят шесть строк, пустые места не пересчитает."""
    d = _dir(tmp_path, [("pde_heat1d__test__w22x1r3.json", TIMES, None),
                        ("discrete_log__test__w22x1r3.json", TIMES, None),
                        ("pagerank__test__w22x1r3.json", TIMES, QUIET)])
    out = _run(d).stdout
    assert "2 of 3 entries record no conditions" in out, out
    assert "pde_heat1d__test__w22x1r3" in out and "discrete_log__test__w22x1r3" in out, out


def test_the_load_spread_is_printed_and_not_judged(tmp_path):
    """Высокая загрузка ПРИСУЩА широкому режиму — двадцать два воркера мелют одновременно. Порог,
    выдуманный здесь, объявил бы дефектом нормальную работу; печатается разброс, а не вердикт."""
    d = _dir(tmp_path, [("a__test__w22x1r3.json", TIMES, {"eval_workers": "auto",
                                                          "loadavg": [1817.0, 900.0, 400.0]}),
                        ("b__test__lane22r3.json", TIMES, QUIET),
                        ("c__test__w22x1r3.json", TIMES, None)])
    out = _run(d).stdout
    assert "span load 10.4 to 1817.0" in out, out
    assert "printed, not judged" in out, out


def test_the_plural_belongs_to_the_total(tmp_path):
    """«1 of 2 entry» — вот что печатает множественное число, привязанное к числителю."""
    d = _dir(tmp_path, [("a__test__w22x1r3.json", TIMES, None),
                        ("b__test__lane22r3.json", TIMES, QUIET)])
    assert "1 of 2 entries record no conditions" in _run(d).stdout, _run(d).stdout
