"""§378. Одиннадцать проб `pde_heat1d` не написали ни одного `.pyx` — и это не возраст.

`kernel_kind` (§377) отделил Cython от numba, и первое, что он показал, — асимметрия, которой не было
ни на одной странице: на `pde_heat1d` цикл не потянулся к Cython НИ РАЗУ, тогда как на остальных
задачах тянется в большинстве прогонов.

Очевидное объяснение — «пробы pde самые старые» — **опровергнуто корпусом**: в ту же раннюю эпоху
(по 2026-09-01) `edge_expansion` писал `.pyx` в 26 прогонах из 27, `discrete_log` в 5 из 8, а
`pde_heat1d` — в 0 из 11. Дело в задаче, а не в эпохе.

Проверка НЕ падает на задаче, которая к Cython не тянется: numba-чемпионы `pde_heat1d` держат самую
высокую numba-медиану корпуса (117.74), а его референс — `scipy.integrate.solve_ivp`, где работа
внутри SciPy, а не в питоновском цикле. Отказаться от рычага, которому не за что зацепиться, —
защитимый ответ. Неправильно было бы не знать.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

BENCH = Path(__file__).resolve().parents[1] / "benchmarks"
sys.path.insert(0, str(BENCH))

import sweep_claims  # noqa: E402

STUB = '''#!/usr/bin/env python3
import json
print(json.dumps({PAYLOAD}))
'''


def _bench(tmp_path, rows):
    tools = tmp_path / "looplab" / "benchmarks"
    tools.mkdir(parents=True)
    (tools / "probe_summary.py").write_text(STUB.replace("{PAYLOAD}", json.dumps(rows)),
                                            encoding="utf-8")
    return str(tmp_path)


def _row(task, kind):
    return {"probe": f"p{task}{kind}", "task": task, "kernel_kind": kind}


def test_the_counts_are_reported_per_task(tmp_path):
    bench = _bench(tmp_path, [_row("a", "cython"), _row("a", "numba"), _row("b", "numba")])
    ok, said = sweep_claims.check_which_lever_the_loop_reaches_for(bench)
    assert ok, said
    assert "a cython 1/numba 1" in said and "b numba 1" in said, said


def test_a_task_that_never_reaches_for_cython_is_named(tmp_path):
    bench = _bench(tmp_path, [_row("pde_heat1d", "numba"), _row("edge_expansion", "cython")])
    _, said = sweep_claims.check_which_lever_the_loop_reaches_for(bench)
    assert "NEVER reached for Cython: pde_heat1d (0 of 1)" in said, said


def test_naming_it_is_not_failing_it(tmp_path):
    """Отказ от рычага, которому не за что зацепиться, — ответ, а не промах. Проверка сообщает."""
    bench = _bench(tmp_path, [_row("pde_heat1d", "numba")])
    ok, said = sweep_claims.check_which_lever_the_loop_reaches_for(bench)
    assert ok, said
    assert "NEVER reached for Cython" in said, said


def test_a_corpus_without_the_field_is_a_failure_not_a_silence(tmp_path):
    """Поле §377 могло исчезнуть при правке — тогда сказать про рычаг нечего, и молчать нельзя."""
    rows = [{"probe": "p", "task": "a"}]
    ok, said = sweep_claims.check_which_lever_the_loop_reaches_for(_bench(tmp_path, rows))
    assert not ok and "the field §377 added is missing" in said, said


@pytest.mark.corpus
def test_the_live_corpus_shows_the_asymmetry():
    ok, said = sweep_claims.check_which_lever_the_loop_reaches_for("/var/tmp/looplab-bench")
    if "cannot be driven" in said:
        return
    assert ok, said
    assert "pde_heat1d" in said, said
