"""§371. Умолчание, верное когда-то, молча стало неверным.

`outlier_check` — инструмент, заведённый ровно для того, чтобы обход перестал отвечать «а это
необычно?» разовым запросом. Его `--task` по умолчанию стоял `edge_expansion`: верно, когда корпус
был из 118 таких прогонов, и молча неверно в день, когда все идущие пробы — `pagerank`. Тогда он
отказывал КАЖДОЙ пробе со словами «re-run with --task pagerank», и оператор должен был это знать.

Он и так знает: `probe_task` читает собственное дерево пробы, и цикл ниже уже зовёт его, чтобы
объяснить отказ. Взять оттуда же задачу — то же чтение, на шаг раньше. Явный `--task` по-прежнему
побеждает; пробы на двух задачах разом называются и отвергаются, а не сравниваются молча с одной.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1] / "benchmarks"
TOOL = BENCH / "outlier_check.py"
sys.path.insert(0, str(BENCH))

import outlier_check  # noqa: E402


def test_the_default_is_no_longer_a_task_name():
    """Умолчание-имя задачи — это и был дефект: оно не может стареть, если его нет."""
    src = TOOL.read_text(encoding="utf-8")
    assert 'ap.add_argument("--task", default=None)' in src, "умолчание снова прибито к задаче"
    assert 'default="edge_expansion"' not in src, "старое умолчание вернулось"


def test_the_task_is_read_from_the_probe_tree(tmp_path):
    """Дерево без `events.jsonl` задачей не считается: каталог могли создать и ничего не запустить.
    Первая фикстура так и промахнулась -- создала каталог и ждала имени."""
    run = tmp_path / "p1" / "runs" / "pagerank" / "run"
    run.mkdir(parents=True)
    assert outlier_check.probe_task(str(tmp_path), "p1") is None
    (run / "events.jsonl").write_text("{}\n", encoding="utf-8")
    assert outlier_check.probe_task(str(tmp_path), "p1") == "pagerank"


def test_a_probe_with_no_task_tree_reads_nothing(tmp_path):
    (tmp_path / "p1").mkdir(parents=True)
    assert outlier_check.probe_task(str(tmp_path), "p1") is None


def test_two_tasks_at_once_are_named_and_refused():
    """Корпус — на задачу. Сравнить пробу с чужим корпусом хуже, чем отказаться."""
    src = TOOL.read_text(encoding="utf-8")
    assert "more than one task" in src and "a corpus is per task" in src, src[:0]
    assert "return 2" in src.split("more than one task")[1][:200], "отказ не возвращает ошибку"


def test_an_explicit_task_still_wins():
    src = TOOL.read_text(encoding="utf-8")
    assert "if args.task is None:" in src, "явный --task перестал побеждать"


def test_the_live_tool_compares_without_being_told(tmp_path):
    """Тот самый отказ, ради которого правка: раньше без флага сравнивалось НОЛЬ проб."""
    got = subprocess.run([sys.executable, str(TOOL)], capture_output=True, text=True, timeout=900)
    if "no bench probe running" in got.stdout:
        return
    assert "NOT COMPARED" not in got.stdout, got.stdout[:400]
    assert "running probe(s) against" in got.stdout, got.stdout[:400]


def _run_task(root, probe, task, costs):
    d = root / probe / "runs" / task / "run"
    d.mkdir(parents=True)
    (d / "events.jsonl").write_text("".join(
        json.dumps({"v": 1, "seq": i, "ts": float(i), "type": "llm_usage", "data": {"cost": c}}) + "\n"
        for i, c in enumerate(costs)), encoding="utf-8")


def test_two_running_tasks_are_refused_by_behaviour(tmp_path, monkeypatch, capsys):
    """Пришпилки в исходнике мало: мутация «сравнить обе с одной» её не трогает. Нужен прогон."""
    _run_task(tmp_path, "a", "pagerank", [0.1])
    _run_task(tmp_path, "b", "edge_expansion", [0.1])
    monkeypatch.setattr(outlier_check.lanes, "probes",
                        lambda root: [{"probe": "a", "lane": "0-10", "pid": 1},
                                      {"probe": "b", "lane": "11-21", "pid": 2}])
    rc = outlier_check.main(["--root", str(tmp_path)])
    err = capsys.readouterr().err
    assert rc == 2, rc
    assert "more than one task" in err and "pagerank" in err and "edge_expansion" in err, err


def test_one_running_task_needs_no_flag(tmp_path, monkeypatch, capsys):
    for i in range(6):
        _run_task(tmp_path, f"done{i}", "pagerank", [0.1] * 10)
    _run_task(tmp_path, "live", "pagerank", [0.1])
    monkeypatch.setattr(outlier_check.lanes, "probes",
                        lambda root: [{"probe": "live", "lane": "0-10", "pid": 1}])
    outlier_check.main(["--root", str(tmp_path)])
    out = capsys.readouterr().out
    assert "NOT COMPARED" not in out, out
    assert "finished pagerank runs" in out, out
