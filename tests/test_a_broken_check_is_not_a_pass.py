"""§404. Сломанный прибор УЛУЧШАЛ оценку обхода, а мёртвые приборы давали зелёный выход.

Проверка, которая бросает, называлась `UNCHECKABLE` и после этого `continue` — из счёта устаревших
и больше ниоткуда. Знаменатель оставался `len(CLAIMS)`, то есть строка продолжала говорить
«of 21 CHECKED» про утверждения, которых никто не проверял, а утверждение, бывшее STALE до поломки
своей проверки, просто выпадало из счёта.

Прогнано, а не рассуждено:

    здоровый обход        выход 1   «14 of 21 checked claim(s) no longer hold»
    одна проверка бросает выход 1   «13 of 21 …»            <- поломка УЛУЧШИЛА заголовок
    все проверки бросают  выход **0**  «0 of 21 checked claim(s) no longer hold»

Последняя строка — зелёный результат от приборов, которые не померили НИЧЕГО: ровно тот отказ,
ради которого этот файл и существует, внутри самого файла.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BENCH = REPO / "benchmarks"
sys.path.insert(0, str(BENCH))

import sweep_claims  # noqa: E402


def test_the_denominator_is_what_was_checked_not_what_was_listed():
    said = sweep_claims.verdict_line(stale=13, checked=20, broken=1)
    assert "13 of 20 checked" in said, said
    assert "1 claim(s) UNCHECKABLE" in said, said


def test_a_healthy_sweep_says_nothing_about_uncheckable():
    said = sweep_claims.verdict_line(stale=14, checked=21, broken=0)
    assert said == "14 of 21 checked claim(s) no longer hold", said


def test_every_check_broken_cannot_read_as_clean():
    said = sweep_claims.verdict_line(stale=0, checked=0, broken=21)
    assert "0 of 0 checked" in said, said
    assert "21 claim(s) UNCHECKABLE" in said, said
    assert "nobody measured them" in said, said


def test_a_broken_instrument_fails_the_run():
    """Мёртвый прибор не менее срочен, чем устаревшее утверждение."""
    assert sweep_claims.sweep_exit_code(stale=0, broken=1) == 1
    assert sweep_claims.sweep_exit_code(stale=3, broken=0) == 1
    assert sweep_claims.sweep_exit_code(stale=3, broken=2) == 1
    assert sweep_claims.sweep_exit_code(stale=0, broken=0) == 0


def _run_with(tmp_path, injected: str):
    """Копия инструмента с внедрённым `raise`, прогнанная целиком."""
    src = (BENCH / "sweep_claims.py").read_text(encoding="utf-8")
    lines, out = src.split("\n"), []
    for line in lines:
        out.append(line)
        if line.startswith(injected) and line.rstrip().endswith(":"):
            out.append("    raise RuntimeError('driven by the test')")
    copy = tmp_path / "sweep_claims.py"
    copy.write_text("\n".join(out), encoding="utf-8")
    for name in ("ruler_check.py", "events_read.py", "arm_fidelity.py", "lanes.py",
                 "check_money.py", "probe_summary.py"):
        target = BENCH / name
        if target.is_file():
            (tmp_path / name).write_text(target.read_text(encoding="utf-8"), encoding="utf-8")
    got = subprocess.run([sys.executable, str(copy), "--bench", str(tmp_path / "nobench")],
                         capture_output=True, text=True, timeout=900)
    return got.returncode, got.stdout


def test_every_check_raising_exits_non_zero_end_to_end(tmp_path):
    """Через ВЫЗОВ, не только через функцию (§399's lesson): инструмент запускается целиком."""
    code, out = _run_with(tmp_path, "def check_")
    assert "UNCHECKABLE" in out, out[-400:]
    assert "of 0 checked" in out, out[-400:]
    assert code != 0, (code, out[-400:])


def test_a_bench_that_does_not_exist_is_not_silently_clean(tmp_path):
    """Отдельно от поломок: пустой стенд обязан дать устаревшие или несчитаемые, но не тишину."""
    code, out = _run_with(tmp_path, "def nothing_matches_this")
    assert "claim(s)" in out, out[-400:]
    assert code != 0, (code, out[-400:])


def test_the_tally_counts_holds_stales_and_breaks_apart(monkeypatch, capsys):
    """Счётчики порознь, через `main`: без прогона, где проверки УДАЮТСЯ, мутация «не считать
    удавшиеся» проходила зелёной — все мои сквозные прогоны шли по пустому стенду, где не удаётся
    ничто."""
    monkeypatch.setattr(sweep_claims, "CLAIMS", [
        ("a holds", lambda bench: (True, "fine")),
        ("b stale", lambda bench: (False, "moved")),
        ("c stale too", lambda bench: (False, "moved")),
        ("d raises", lambda bench: (_ for _ in ()).throw(RuntimeError("driven"))),
    ])
    code = sweep_claims.main(["--bench", "/nonexistent"])
    out = capsys.readouterr().out
    assert "2 of 3 checked claim(s) no longer hold" in out, out
    assert "1 claim(s) UNCHECKABLE" in out, out
    assert "UNCHECKABLE  d raises" in out, out
    assert "RuntimeError: driven" in out, out
    assert code == 1


def test_a_sweep_where_everything_holds_exits_zero(monkeypatch, capsys):
    """Обратная сторона: чинить надо так, чтобы чистый обход всё ещё мог быть чистым."""
    monkeypatch.setattr(sweep_claims, "CLAIMS", [("a", lambda bench: (True, "fine"))])
    code = sweep_claims.main(["--bench", "/nonexistent"])
    out = capsys.readouterr().out
    assert "0 of 1 checked claim(s) no longer hold" in out, out
    assert "UNCHECKABLE" not in out, out
    assert code == 0
