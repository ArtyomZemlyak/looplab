"""§380. Число, которым сайзят каждое плечо, было умолчанием, а не чтением.

`--effect` по умолчанию 23.5 — цифра §186, померенная по 69 прогонам `edge_expansion` с двумя и
более узлами. Сегодня таких прогонов **118**, и тот же раздел даёт **+29.86**: константа устарела на
четверть, и никто об этом не говорил, потому что она была умолчанием флага, а не измерением.

Пересчитал с различием §377 — и оно здесь **НЕ работает**:

    узел 0 cython  n=50  медиана узла 0 170.07  медиана чемпиона 220.75
    узел 0 numba   n=16  медиана узла 0  27.67  медиана чемпиона 205.47
    узел 0 plain   n=52  медиана узла 0  22.97  медиана чемпиона 189.78

Слитно, как считал §186 (любое ядро против никакого), разница +29.86; с выделенным Cython — +30.96,
около одного очка. Восьмикратный разрыв §377 реален между видами ЧЕМПИОНОВ и в эффект узла 0 не
переходит: прогон, открывшийся numba, обычно позже сдаёт Cython. Подозрение §377 было моим, и оно
не подтвердилось — поправка стоит рядом с числом.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1] / "benchmarks"
sys.path.insert(0, str(BENCH))

import arm_power  # noqa: E402


def _run(root, probe, task, metrics, node0_files, later_files=None):
    """`later_files` — узел 1, который СПОРИТ с узлом 0.

    Без него фикстура не расходится с багом "читать последний узел": на диске лежит один node_0, и
    неверный код даёт тот же ответ, что верный. Мутация приходила ЗЕЛЁНОЙ, пока каждый прогон не
    получил второй узел противоположного вида (§373: фикстура держит все правила, кроме своей).
    """
    run = root / probe / "runs" / task / "run"
    (run / "nodes" / "node_0").mkdir(parents=True)
    (run / "nodes" / "node_1").mkdir(parents=True)
    for name, body in (later_files or {"solver.py": "# node 1\n"}).items():
        (run / "nodes" / "node_1" / name).write_text(body, encoding="utf-8")
    (run / "events.jsonl").write_text("".join(
        json.dumps({"v": 1, "seq": i, "ts": float(i), "type": "node_evaluated",
                    "data": {"node_id": i, "metric": m}}) + "\n"
        for i, m in enumerate(metrics)), encoding="utf-8")
    for name, body in node0_files.items():
        (run / "nodes" / "node_0" / name).write_text(body, encoding="utf-8")


def test_a_kernel_at_node_zero_is_read_from_the_node_not_the_champion(tmp_path):
    """Читается КОД УЗЛА 0, а не чемпиона: §186 про то, чем прогон открылся."""
    plain = {"solver.py": "def solve(p): return p\n"}
    # Каждый узел 1 противоречит своему узлу 0: иначе "прочитать последний узел" даёт тот же ответ.
    _run(tmp_path, "a", "t", [10.0, 200.0, 5.0], {"solver.py": "cimport numpy\n"}, later_files=plain)
    _run(tmp_path, "b", "t", [10.0, 210.0, 5.0], {"solver.py": "import numba\n"}, later_files=plain)
    _run(tmp_path, "c", "t", [10.0, 100.0, 5.0], plain, later_files={"solver.py": "cimport numpy\n"})
    _run(tmp_path, "d", "t", [10.0, 110.0, 5.0], plain, later_files={"k.pyx": "# cython\n"})
    eff, nk, nn = arm_power.node0_kernel_effect(str(tmp_path), "t")
    assert (nk, nn) == (2, 2), (nk, nn)
    # Каждый прогон кончается регрессией (5.0): считается ЛУЧШИЙ узел, а не последний.
    assert abs(eff - (205.0 - 105.0)) < 1e-9, eff


def test_a_pyx_beside_node_zero_counts_as_a_kernel(tmp_path):
    pyx = {"solver.py": "print(1)\n", "k.pyx": "# cython\n"}
    _run(tmp_path, "a", "t", [1.0, 9.0, 0.5], pyx, later_files={"solver.py": "print(1)\n"})
    _run(tmp_path, "b", "t", [1.0, 9.0, 0.5], pyx, later_files={"solver.py": "print(1)\n"})
    _run(tmp_path, "c", "t", [1.0, 1.0, 0.5], {"solver.py": "print(1)\n"}, later_files=pyx)
    _run(tmp_path, "d", "t", [1.0, 1.0, 0.5], {"solver.py": "print(1)\n"}, later_files=pyx)
    eff, nk, _ = arm_power.node0_kernel_effect(str(tmp_path), "t")
    assert nk == 2 and eff == 8.0, (nk, eff)


def test_a_run_with_one_node_is_not_in_the_split(tmp_path):
    """§186 считал по прогонам с ДВУМЯ и более узлами: эффект про то, что было после первого."""
    _run(tmp_path, "solo", "t", [50.0], {"solver.py": "cimport numpy\n"})
    _, nk, nn = arm_power.node0_kernel_effect(str(tmp_path), "t")
    assert (nk, nn) == (0, 0), (nk, nn)


def test_too_few_runs_on_a_side_refuse_a_number(tmp_path):
    """Одна проба с ядром и одна без — не разница, а две точки."""
    _run(tmp_path, "a", "t", [1.0, 9.0, 0.5], {"solver.py": "cimport numpy\n"})
    _run(tmp_path, "b", "t", [1.0, 1.0, 0.5], {"solver.py": "print(1)\n"})
    eff, _, _ = arm_power.node0_kernel_effect(str(tmp_path), "t")
    assert eff is None, eff


def test_the_tool_says_when_the_default_has_drifted():
    src = (BENCH / "arm_power.py").read_text(encoding="utf-8")
    assert "this corpus now gives" in src, "устаревшее умолчание снова молчит"
    assert "abs(measured - args.effect) > 2.0" in src, "порог расхождения исчез"
    # НЕ подменяет: оператор выбирает, чем сайзить.
    block = src.split("measured, n_k, n_n = node0_kernel_effect")[1].split("print(f\"effect")[0]
    assert "args.effect =" not in block, "умолчание подменено молча"
