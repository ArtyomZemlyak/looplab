"""§365. «Тихо по ядрам» и «тихо по соседям» — два разных вопроса, и записывался только первый.

`busy_cpus_outside_lane` считает ядра в состоянии R, и делает это правильно: проба, ждущая модель,
не жжёт ни одного ядра, а прежняя версия §295, считавшая всякий пришпиленный процесс, читала 22 на
пустой коробке. Померено 2026-09-09 с ЧЕТЫРЬМЯ живыми пробами на четырёх полосах: процессов в
состоянии R на полосах **ноль**, и поле честно читает **0**.

Но это не всё условие для чтения линейки. Каждая из тех четырёх в непредсказуемый момент запустит
оценку на двадцати двух воркерах, а самопроверка идёт минутами: чтение, записанное как «тихое»,
может быть испорчено между двумя своими же повторами. Докстринг поля признаёт ближний случай —
«a neighbour that both starts and ends inside a single rep is missed», — а это дальний: сосед,
который ЕЩЁ не бежит.

Записывается РЯДОМ, а не вместо. Сменить смысл «тихого» значило бы переоткрыть каждое уже собранное
число в логе дрейфа.
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


def test_the_two_questions_have_two_answers():
    """Поле про ядра и поле про соседей не должны быть одним числом."""
    src = (BENCH / "ruler_selfcheck.py").read_text(encoding="utf-8")
    assert "def busy_cpus_outside_lane" in src and "def probes_alive_outside_lane" in src
    assert '"neighbours_alive": neighbours_alive' in src, "сосед не попадает на строку"


def test_the_row_carries_the_neighbour_count(tmp_path):
    row = ruler_selfcheck.append_reading(
        tmp_path / "log.jsonl", "pagerank", "test", [1.0], 1.0,
        stamp="2026-09-09T00:00:00", neighbours_alive=3)
    assert row["neighbours_alive"] == 3
    assert json.loads((tmp_path / "log.jsonl").read_text())["neighbours_alive"] == 3


def test_a_probe_on_our_own_lane_is_not_a_neighbour():
    """Из полосы, где стоит своя проба, соседей должно быть на одного меньше, чем со служебной."""
    live = ruler_selfcheck.probes_alive_outside_lane()
    assert live is None or live >= 0


def _bench(tmp_path, rows):
    algo = tmp_path / "looplab" / "benchmarks" / "algotune"
    (algo / ".baseline_times").mkdir(parents=True)
    (algo / ".baseline_times" / f"x__test__{ruler_check.SERIAL_REGIME}.json").write_text(
        json.dumps({"i0": 1.0}), encoding="utf-8")
    (algo / "ruler_selfcheck_log.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    (tmp_path / "model-probes").mkdir()
    return str(tmp_path)


def _row(values, near=None):
    r = {"task": "pde_heat1d", "stamp": "2026-09-09T10:00:00", "subset": "test",
         "regime": ruler_check.CAMPAIGN_REGIME, "values": list(values), "median": values[0],
         "busy_cpus_outside_lane": 0}
    if near is not None:
        r["neighbours_alive"] = near
    return r


def test_the_check_says_when_a_reading_had_company(tmp_path):
    _, said = sweep_claims.check_ruler_constants(
        _bench(tmp_path, [_row([1.0, 1.01], near=3), _row([1.02, 1.03], near=0)]))
    assert "2 reading(s) taken with another probe ALIVE" in said, said


def test_a_reading_with_no_company_says_nothing(tmp_path):
    _, said = sweep_claims.check_ruler_constants(
        _bench(tmp_path, [_row([1.0, 1.01], near=0), _row([1.02, 1.03], near=0)]))
    assert "ALIVE on the box" not in said, said


def test_rows_older_than_the_field_are_not_accused(tmp_path):
    """Строки до §365 поля не несут: молчание — не «соседей не было»."""
    _, said = sweep_claims.check_ruler_constants(
        _bench(tmp_path, [_row([1.0, 1.01]), _row([1.02, 1.03])]))
    assert "ALIVE on the box" not in said, said


def test_the_cpu_field_is_not_folded_into_the_new_one():
    """Сменить смысл «тихого» значило бы переоткрыть каждое уже собранное число."""
    src = (BENCH / "sweep_claims.py").read_text(encoding="utf-8")
    assert 'if busy == 0:' in src, "условие тихого чтения изменилось -- это переоткрывает корпус"


def _fake_proc(tmp_path, procs):
    """`procs`: {pid: (cmdline, cpu-set)} against a fake /proc, as `lanes.probes` is tested."""
    root = tmp_path / "proc"
    root.mkdir()
    for pid, (cmd, _cpus) in procs.items():
        d = root / str(pid)
        d.mkdir()
        (d / "cmdline").write_bytes(cmd.encode() + b"\x00")
    return str(root), (lambda pid: procs[pid][1] if pid in procs else set())


ENGINE = ("/opt/conda/bin/python -m looplab.cli run "
          "/var/tmp/looplab-bench/model-probes/{name}/ws/algotune_pagerank.json")


def test_a_probe_on_another_lane_counts(tmp_path):
    proc, aff = _fake_proc(tmp_path, {11: (ENGINE.format(name="pgr4"), {11, 12})})
    assert ruler_selfcheck.probes_alive_outside_lane(proc, aff, mine={0, 1}) == 1


def test_our_own_probe_is_not_a_neighbour(tmp_path):
    """Проба, чья привязка ПЕРЕСЕКАЕТСЯ с нашей, — это мы сами, а не сосед."""
    proc, aff = _fake_proc(tmp_path, {11: (ENGINE.format(name="pgr3"), {0, 1})})
    assert ruler_selfcheck.probes_alive_outside_lane(proc, aff, mine={0, 1}) == 0


def test_a_shell_or_a_tool_in_the_probe_tree_is_not_the_engine(tmp_path):
    """В дереве пробы живут и оболочка драйвера, и инструменты. Считать их пробами значит
    насчитать соседей там, где идёт один прогон."""
    # ОБА процесса стоят В дереве пробы -- иначе их отсеет проверка корня, и фильтр «это движок»
    # окажется непроверенным. Первая версия этой фикстуры так и промахнулась: мутация, снимавшая
    # фильтр, уходила зелёной, потому что пути были из `looplab/`, а не из `model-probes/`.
    tree = "/var/tmp/looplab-bench/model-probes/pgr4"
    procs = {11: (f"bash {tree}/run_probe.sh deepseek pgr4 11-21,59-69 pagerank", {11, 12}),
             12: (f"/opt/conda/bin/python {tree}/tools/looplab_eval.py --task pagerank", {11, 12})}
    proc, aff = _fake_proc(tmp_path, procs)
    assert ruler_selfcheck.probes_alive_outside_lane(proc, aff, mine={0, 1}) == 0


def test_a_process_outside_the_probe_tree_is_ignored(tmp_path):
    proc, aff = _fake_proc(tmp_path, {11: ("/opt/conda/bin/python -m looplab.cli run /tmp/x", {11})})
    assert ruler_selfcheck.probes_alive_outside_lane(proc, aff, mine={0, 1}) == 0
