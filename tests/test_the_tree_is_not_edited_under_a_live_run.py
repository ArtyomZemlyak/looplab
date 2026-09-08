"""`run_probe.sh` записал, чего стоит правка файла под работающим прогоном. Для дерева правила не было.

§336. Заголовок `run_probe.sh` помнит: файл правили, пока по нему шли четыре пробы, и `remEEctl1`
кончилась строкой `run_probe.sh: line 291: -c: command not found` — bash читает скрипт ПО СМЕЩЕНИЮ
по мере исполнения. Python не защищён, а лишь тише: прогон импортирует основную часть `looplab` на
старте, но каждый ленивый импорт после этого читает то, что лежит на диске СЕЙЧАС, — и слияние
посреди прогона смешивает две ревизии внутри одного измерения, ничего не сказав в журнале.

Проверка совпадает по argv интерпретатора, а не по рабочему каталогу: проба запускается как
`taskset … python -m looplab.cli`, её cwd — корень стенда, а привязка к этим файлам идёт через
модуль.
"""
from __future__ import annotations

import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1] / "benchmarks"
sys.path.insert(0, str(BENCH))

import pulse  # noqa: E402

ROOT = "/var/tmp/looplab-bench"
RUN = f"/opt/conda/bin/python -m looplab.cli run --task discrete_log --out {ROOT}/model-probes/x"
RESUME = f"/opt/conda/bin/python -m looplab.cli resume {ROOT}/model-probes/x/runs/t/run"


def test_a_running_probe_is_a_reader():
    assert pulse.readers_of_the_tree(ROOT, [{"pid": "10", "ppid": "1", "cmdline": RUN}]) == ["10"]


def test_a_resume_is_a_reader_too():
    assert pulse.readers_of_the_tree(ROOT, [{"pid": "11", "ppid": "1", "cmdline": RESUME}]) == ["11"]


def test_the_sweeps_own_tools_are_not_the_measurement():
    """`pulse` сам читает это дерево на каждом тике, и он же задаёт вопрос. Считать себя читателем
    значит запретить правку навсегда."""
    # Оба имени содержат "run" внутри слов — `--refuse-edits` пишется в дереве, а имя тест-файла
    # начинается с `test_a_running_probe`. Без явного исключения инструментов обхода они попадают
    # под то же правило, что и измерение, и запрет становится вечным.
    table = [{"pid": "12", "ppid": "1",
              "cmdline": f"/opt/conda/bin/python {ROOT}/looplab/benchmarks/pulse.py --refuse-edits "
                         f"--root {ROOT}/model-probes/currently-running"},
             {"pid": "13", "ppid": "1",
              "cmdline": f"/opt/conda/bin/python -m pytest {ROOT}/looplab/tests/"
                         "test_a_running_probe_is_not_compared_to_a_finished_one.py"}]
    assert pulse.readers_of_the_tree(ROOT, table) == []


def test_an_idle_shell_in_the_tree_is_not_a_reader():
    """Оболочка, чей cwd в дереве, ничего оттуда не исполняет — запрет по cwd остановил бы каждый
    обход, который туда зашёл."""
    table = [{"pid": "14", "ppid": "1", "cmdline": "/bin/bash -i"}]
    assert pulse.readers_of_the_tree(ROOT, table) == []


def test_a_run_outside_this_checkout_is_not_a_reader():
    """Фикстура, расходящаяся с дефектом: в argv есть слово `run`, процесс жив, но исполняется он
    из ЧУЖОГО каталога. Правило «всё, где встретилось run» запретило бы правку из-за соседнего
    проекта."""
    table = [{"pid": "15", "ppid": "1", "cmdline": "/bin/bash /var/tmp/other-project/run_nightly.sh"},
             {"pid": "16", "ppid": "1", "cmdline": "/usr/bin/python3 /home/jovyan/run_notebook.py"}]
    assert pulse.readers_of_the_tree(ROOT, table) == []


def test_the_flag_exits_three_and_says_why():
    src = (BENCH / "pulse.py").read_text(encoding="utf-8")
    assert "--refuse-edits" in src and "return 3" in src
    assert "would swap files under a live measurement" in src
