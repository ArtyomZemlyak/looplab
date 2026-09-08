"""§349. Девять чтений, которые запись объявляет НЕВЕРНЫМИ, исключались случайно — не по правилу.

§299 отозвал §296–§298, найдя, что все они мерены под conda вместо венва стенда, и пометил девять
строк лога `interpreter: conda (WRONG -- see §299)`, а не удалил их. Проверка констант на это поле
не смотрела вовсе: девять строк не попадают в сегодняшний вердикт лишь потому, что старше поля
`busy_cpus_outside_lane`. Одна такая строка, написанная днём позже и со счётчиком занятости, ушла бы
прямо в среднее — неся ровно ту ошибку, ради записи которой §299 и существует.

Дословно оттуда, и это причина, по которой исключать надо по правилу:

    a wrong instrument reproduces its own error perfectly

Вторая половина: §299 проштамповал интерпретатор на САЙДКАРЕ линейки, а чтение его так и не
записывало — те девять помечены рукой задним числом. Теперь `append_reading` пишет `bench_python()`,
как сайдкар пишет свой, так что чтение и линейка, на которую оно делит, сравнимы по этому полю.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1] / "benchmarks"
sys.path.insert(0, str(BENCH))

import ruler_check  # noqa: E402
import ruler_selfcheck  # noqa: E402
import sweep_claims  # noqa: E402

WRONG = "conda (WRONG -- see §299)"


def _bench(tmp_path, rows):
    algo = tmp_path / "looplab" / "benchmarks" / "algotune"
    (algo / ".baseline_times").mkdir(parents=True)
    (algo / ".baseline_times" / f"x__test__{ruler_check.SERIAL_REGIME}.json").write_text(
        json.dumps({"i0": 1.0}), encoding="utf-8")
    (algo / "ruler_selfcheck_log.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    (tmp_path / "model-probes").mkdir()
    return str(tmp_path)


def _row(values, interpreter=None, stamp="2026-09-08T00:00:00"):
    row = {"task": "pde_heat1d", "stamp": stamp, "subset": "test", "median": values[0],
           "values": values, "busy_cpus_outside_lane": 0,
           "regime": ruler_check.CAMPAIGN_REGIME}
    if interpreter:
        row["interpreter"] = interpreter
    return row


def test_a_withdrawn_reading_never_reaches_the_mean(tmp_path):
    """Тот самый случай, который сегодня не наступает только по случайности: строка, помеченная
    WRONG, но со счётчиком занятости — то есть годная по всем ОСТАЛЬНЫМ признакам."""
    bench = _bench(tmp_path, [_row([1.00, 1.01]), _row([9.00, 9.01], interpreter=WRONG)])
    _, said = sweep_claims.check_ruler_constants(bench)
    assert "9.0000" not in said, f"отозванное чтение попало в среднее: {said}"
    assert "mean 1.0050" in said, f"среднее сдвинуто отозванным чтением: {said}"
    assert "2 reading(s) WITHDRAWN by the record" in said, said


def test_the_withdrawal_is_counted_not_silent(tmp_path):
    """Молча выбросить — значит показать среднее по меньшему числу чтений, чем сказано рядом."""
    bench = _bench(tmp_path, [_row([1.00, 1.01]), _row([1.02, 1.03], interpreter=WRONG)])
    _, said = sweep_claims.check_ruler_constants(bench)
    assert "2 quiet wide read(s)" in said, said
    assert "2 reading(s) WITHDRAWN" in said, said


def test_a_reading_under_the_bench_interpreter_is_kept(tmp_path):
    """Исключается то, что запись зовёт НЕВЕРНЫМ, а не всё, что называет интерпретатор."""
    good = "/var/tmp/looplab-bench/AlgoTune/.venv/bin/python"
    bench = _bench(tmp_path, [_row([1.00, 1.01], interpreter=good),
                              _row([1.02, 1.03], interpreter=good)])
    _, said = sweep_claims.check_ruler_constants(bench)
    assert "WITHDRAWN" not in said, said
    assert "4 quiet wide read(s)" in said, said


def test_a_reading_that_says_nothing_is_not_treated_as_wrong(tmp_path):
    """47 строк до §349 интерпретатора не называют. «Не записано» — не «неверно»: выбросить их
    значило бы отозвать корпус за то, чего он про себя не сказал."""
    bench = _bench(tmp_path, [_row([1.00, 1.01]), _row([1.02, 1.03])])
    _, said = sweep_claims.check_ruler_constants(bench)
    assert "WITHDRAWN" not in said and "4 quiet wide read(s)" in said, said


def test_a_new_reading_records_the_interpreter(tmp_path):
    """Вторая половина §299: сайдкар линейки интерпретатор пишет, чтение — нет."""
    row = ruler_selfcheck.append_reading(
        tmp_path / "log.jsonl", "pde_heat1d", "test", [1.0], 1.0,
        stamp="2026-09-08T16:00:00", interpreter="/venv/bin/python")
    assert row["interpreter"] == "/venv/bin/python"
    assert json.loads((tmp_path / "log.jsonl").read_text())["interpreter"] == "/venv/bin/python"


def test_the_caller_passes_the_bench_interpreter_not_its_own():
    """Наследовать интерпретатор — это ровно то, как §299 и случился, и падение было молчаливым."""
    src = (BENCH / "ruler_selfcheck.py").read_text(encoding="utf-8")
    assert "interpreter=bench_python()" in src, "чтение снова наследует чужой интерпретатор"
    assert "sys.executable" not in src.split("append_reading(args.record")[1][:400], src[:0]
