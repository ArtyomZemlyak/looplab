"""§354. Прибор, которому дали дерево не той формы, перечислял пустые строки вместо отказа.

Runs-root кампании — по каталогу на ЗАДАЧУ, внутри `run/`. Корпус проб — по каталогу на ПРОБУ,
внутри `runs/<task>/run/`. Получив второе, `compare_arms` принял 144 имени проб за имена задач и
напечатал 144 строки `(incomplete)`: чисел он не выдумал — и ровно поэтому читается это как
«сравнение прошло и ничего не нашло», а не как «сравнение здесь неприменимо». Разница лежит на
диске и стоит одного glob.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1] / "benchmarks"
sys.path.insert(0, str(BENCH / "algotune"))

import compare_arms  # noqa: E402


def test_a_probe_corpus_is_recognised(tmp_path):
    for probe in ("remDL13", "accEE"):
        (tmp_path / probe / "runs" / "discrete_log" / "run").mkdir(parents=True)
    assert compare_arms.runs_root_is_a_probe_tree(tmp_path) == ["accEE", "remDL13"]


def test_a_campaign_runs_root_is_not_mistaken_for_one(tmp_path):
    """Каталог задачи с `run/` внутри — это кампания, и отказывать здесь нельзя."""
    for task in ("discrete_log", "edge_expansion"):
        (tmp_path / task / "run").mkdir(parents=True)
    assert compare_arms.runs_root_is_a_probe_tree(tmp_path) == []


def test_a_ruler_directory_does_not_make_it_a_probe_corpus(tmp_path):
    """`_ruler` живёт рядом с пробами и задачей не является."""
    (tmp_path / "_ruler" / "runs" / "discrete_log" / "run").mkdir(parents=True)
    (tmp_path / "discrete_log" / "run").mkdir(parents=True)
    assert compare_arms.runs_root_is_a_probe_tree(tmp_path) == []


def test_an_empty_root_is_not_accused(tmp_path):
    assert compare_arms.runs_root_is_a_probe_tree(tmp_path) == []


def test_the_tool_refuses_instead_of_listing_nothing(tmp_path):
    """Отказ, а не 144 строки `(incomplete)`."""
    for probe in ("remDL13", "accEE"):
        (tmp_path / "probes" / probe / "runs" / "discrete_log" / "run").mkdir(parents=True)
    (tmp_path / "algotune" / "reports").mkdir(parents=True)
    (tmp_path / "algotune" / "reports" / "agent_summary.json").write_text("{}", encoding="utf-8")
    got = subprocess.run(
        [sys.executable, str(BENCH / "algotune" / "compare_arms.py"),
         "--algotune-root", str(tmp_path / "algotune"),
         "--runs-root", str(tmp_path / "probes")],
        capture_output=True, text=True, timeout=600)
    assert got.returncode == 2, got.stdout + got.stderr
    assert "REFUSING" in got.stdout and "PROBE tree(s)" in got.stdout, got.stdout
    assert "(incomplete)" not in got.stdout, got.stdout
    assert "probe_summary.py" in got.stdout, "отказ обязан назвать, чем это читать"


def test_a_tree_holding_only_the_ruler_is_not_a_probe_corpus(tmp_path):
    """`_ruler` лежит среди проб и имеет ту же форму, но пробой не является. Дерево, где кроме
    него ничего нет, — не корпус проб, и отказывать по нему нельзя."""
    (tmp_path / "_ruler" / "runs" / "discrete_log" / "run").mkdir(parents=True)
    assert compare_arms.runs_root_is_a_probe_tree(tmp_path) == []
