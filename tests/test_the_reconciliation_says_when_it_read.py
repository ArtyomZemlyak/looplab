"""§362. Две половины сверки читаются с разрывом в секунды, и отчёт об этом молчал.

Порядок здесь намеренный и записан в самом файле: СПАНЫ ПЕРВЫМИ, счётчик вторым, — тогда счётчик
между чтениями может только ПРИБАВИТЬ, и разрыв неотрицателен по построению. Но на странице обе
цифры стояли рядом, как будто сняты одновременно.

Чего это стоит. 2026-09-08 я сравнил总 метра из `check_money` одного обхода с тратой пробы, которую
`pulse` напечатал СЛЕДУЮЩЕЙ командой того же обхода, и прочитал разницу как замершую пробу. Померил
обе величины разом: дерево и метр сходятся до $0.000002 (это преднастроечный вызов). Разрыва не
было — было время между чтениями.

Теперь на странице стоит, во сколько сняты спаны и через сколько снят счётчик. Это ровно то окно,
в котором двум цифрам законно расходиться.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1] / "benchmarks"
CHECK = BENCH / "check_money.py"


def test_the_report_names_both_read_times():
    got = subprocess.run([sys.executable, str(CHECK)], capture_output=True, text=True, timeout=900)
    if "no meter on" in got.stderr:
        return
    line = [l for l in got.stdout.splitlines() if l.startswith("read ")]
    assert line, got.stdout[:300]
    assert re.search(r"spans at \d\d:\d\d:\d\d, counter [0-9.]+s later", line[0]), line[0]


def test_the_window_is_printed_before_the_two_numbers_it_explains():
    """Оговорка после цифр — это сноска, которую читают, уже сделав вывод."""
    got = subprocess.run([sys.executable, str(CHECK)], capture_output=True, text=True, timeout=900)
    if "no meter on" in got.stderr:
        return
    out = got.stdout.splitlines()
    idx = {l.split()[0]: i for i, l in enumerate(out[:6]) if l.split()}
    assert idx.get("read", 99) < idx.get("meter", 99) < idx.get("spans", 99), out[:6]


def test_the_order_of_the_two_reads_is_still_spans_first():
    """Если счётчик снимут первым, разрыв уйдёт в минус на любой завершившейся между чтениями
    записи -- §112 измерил это как residue $-0.003329."""
    src = CHECK.read_text(encoding="utf-8")
    i_spans = src.index("s_cost, s_calls = spans_by_probe(")
    i_counter = src.index("live = _counter(a.port)")
    assert i_spans < i_counter, "счётчик снимается раньше спанов"
    assert src.index("t_spans = time.time()") < i_spans, "отметка снята не перед спанами"
    assert i_spans < src.index("t_counter = time.time()") < i_counter, "отметка счётчика не на месте"


def test_the_stamp_is_taken_not_recomputed_at_print_time():
    """Печатать `time.time()` в строке отчёта значило бы назвать временем чтения момент печати."""
    src = CHECK.read_text(encoding="utf-8")
    at = src.index('print(f"read    spans at')
    printed = src[at:src.index("\n", src.index("GAINED between", at))]
    # ИМЕННО `localtime(t_spans)`: строка и после подмены содержит `t_spans` в разности
    # `t_counter - t_spans`, так что проверка «упоминается» пропускает время печати вместо
    # времени чтения. Мутация `time.localtime()` уходила зелёной ровно на этом.
    assert "time.localtime(t_spans)" in printed, printed
    assert "t_counter - t_spans" in printed, printed
