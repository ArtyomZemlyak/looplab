"""Семь с половиной минут без вызовов и без роста лога — и вызов всё это время шёл.

§335. `remDL13` стояла с одинаковым возрастом лога и последнего ЗАВЕРШЁННОГО вызова, без единого
воркера оценки на своей полосе и без процессов в состоянии R — картина, неотличимая от зависания.
Различитель: у движка было одно УСТАНОВЛЕННОЕ соединение с метром, то есть шла длинная стриминговая
генерация. Леджер записывает вызов, когда тот КОНЧАЕТСЯ, поэтому «возраст последнего вызова» вызов
в полёте увидеть не может — а сокет может.

Читается из `/proc/<pid>/fd` против `/proc/net/tcp`: две директории и никакой сети. `ss` не
используется намеренно — список записал, что на этой коробке он врёт.
"""
from __future__ import annotations

import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1] / "benchmarks"
sys.path.insert(0, str(BENCH))

import pulse  # noqa: E402

# `/proc/net/tcp`'s columns: sl, local, rem_address, st, …, inode at index 9.
HEADER = ("  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  "
          "timeout inode\n")


def _proc(tmp_path, pid: str, inodes: list, rows: list) -> str:
    fd = tmp_path / pid / "fd"
    fd.mkdir(parents=True)
    for i, ino in enumerate(inodes):
        (tmp_path / pid / f"sock{i}").write_text("", encoding="utf-8")
        (fd / str(i)).symlink_to(tmp_path / pid / f"sock{i}")
        # os.readlink must return "socket:[N]"; a symlink cannot, so the test writes the table and
        # patches the reader's view through a dangling link with that name instead.
    (tmp_path / "net").mkdir(exist_ok=True)
    (tmp_path / "net" / "tcp").write_text(HEADER + "".join(rows), encoding="utf-8")
    return str(tmp_path)


def _row(inode: str, port_hex: str, state: str) -> str:
    return (f"   0: 0100007F:1234 0100007F:{port_hex} {state} 00000000:00000000 00:00000000 "
            f"00000000  1000        0 {inode} 1 0000 20 0 0 10 -1\n")


def _link(tmp_path, pid: str, fd: str, target: str) -> None:
    """A dangling symlink whose TARGET is the text `/proc` would show (`socket:[N]`)."""
    d = tmp_path / pid / "fd"
    d.mkdir(parents=True, exist_ok=True)
    (d / fd).symlink_to(target)


def test_an_established_socket_to_the_meter_is_a_call_in_flight(tmp_path):
    _link(tmp_path, "77", "3", "socket:[9001]")
    (tmp_path / "net").mkdir()
    (tmp_path / "net" / "tcp").write_text(HEADER + _row("9001", "2261", "01"), encoding="utf-8")
    assert pulse.call_in_flight("77", port=0x2261, root=str(tmp_path)) is True


def test_a_socket_to_another_port_is_not(tmp_path):
    """Метр — 8801; соединение с чем угодно другим не говорит, что идёт генерация."""
    _link(tmp_path, "78", "3", "socket:[9002]")
    (tmp_path / "net").mkdir()
    (tmp_path / "net" / "tcp").write_text(HEADER + _row("9002", "0050", "01"), encoding="utf-8")
    assert pulse.call_in_flight("78", port=0x2261, root=str(tmp_path)) is False


def test_a_closed_socket_is_not_a_call(tmp_path):
    """Состояние 06 — TIME_WAIT: соединение было, вызов кончился. Это и есть «тишина»."""
    _link(tmp_path, "79", "3", "socket:[9003]")
    (tmp_path / "net").mkdir()
    (tmp_path / "net" / "tcp").write_text(HEADER + _row("9003", "2261", "06"), encoding="utf-8")
    assert pulse.call_in_flight("79", port=0x2261, root=str(tmp_path)) is False


def test_a_process_that_is_gone_answers_no(tmp_path):
    (tmp_path / "net").mkdir()
    (tmp_path / "net" / "tcp").write_text(HEADER, encoding="utf-8")
    assert pulse.call_in_flight("404", port=0x2261, root=str(tmp_path)) is False


def test_the_line_is_printed_only_for_a_stale_looking_probe():
    src = (BENCH / "pulse.py").read_text(encoding="utf-8")
    assert "a call is OPEN to the meter now" in src
    assert "call_age > 240 and call_in_flight" in src, "иначе строка печатается на каждом вызове"


def test_another_process_talking_to_the_meter_is_not_this_probe(tmp_path):
    """Фикстура, расходящаяся с дефектом: установленное соединение с метром В ТАБЛИЦЕ ЕСТЬ, но его
    inode принадлежит другому процессу — сверке метра, соседней пробе, моему же `smoke`. Проверка,
    которая смотрит только в `/proc/net/tcp` и не сверяет inode с дескрипторами ЭТОГО процесса,
    объявит вызов в полёте у всех сразу."""
    _link(tmp_path, "80", "3", "socket:[9005]")          # свой сокет — на порт 80
    (tmp_path / "net").mkdir()
    (tmp_path / "net" / "tcp").write_text(
        HEADER + _row("9005", "0050", "01") + _row("9999", "2261", "01"), encoding="utf-8")
    assert pulse.call_in_flight("80", port=0x2261, root=str(tmp_path)) is False


def test_the_open_call_is_younger_than_the_last_completed_one():
    """§339 правит формулировку §335. Новейшая строка леджера — последний ЗАВЕРШЁННЫЙ вызов;
    открытый начался ПОСЛЕ него, значит его возраст ограничен сверху, а не снизу. «Не менее 1373 с»
    превращало верхнюю границу в нижнюю и делало каждую долго молчавшую пробу хуже, чем позволяет
    свидетельство."""
    src = (BENCH / "pulse.py").read_text(encoding="utf-8")
    assert "so this one is younger than that" in src
    assert "for at least" not in src, "старая формулировка вернулась"


def test_a_phase_without_a_wall_prints_no_budget_clause():
    """Измерено на живой пробе: `deep_research`, `emit` и `Researcher·propose` пишут
    `time_budget_s = 0.0` — стены нет. Печатать «0 % от 0 с» значило бы выдумать ограничение."""
    src = (BENCH / "pulse.py").read_text(encoding="utf-8")
    assert "if budget:" in src and "0.0 means the phase was given no wall" in src
