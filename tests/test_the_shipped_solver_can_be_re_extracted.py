"""§393. Хэш происхождения, чьё выведение записано прозой, умеет только обвинять.

`arm_a_retimed.json` хранит `solver_sha256` на задачу, а `_source` описывает извлечение словами:
«текст после `FILE IN CODE DIR solver.py:` до первой строки лога». Прогнал эту прозу 2026-09-09 —
все четыре хэша РАЗОШЛИСЬ, при том что все четыре числа строк (73, 30, 77, 32) совпали точно. Текст
отличался ровно завершающим переводом строки.

Такой хэш не отличает «источник уехал» от «ты извлёк иначе», а вся ценность записи — именно в этом
различии. Правило теперь код, а четыре записанные суммы — его тест.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "benchmarks" / "algotune"))

import arm_a_solver  # noqa: E402

import pytest  # noqa: E402

RECORDED = json.loads((REPO / "benchmarks" / "algotune" / "arm_a_retimed.json")
                      .read_text(encoding="utf-8"))["tasks"]

# THE CONSTANTS, AS LITERALS. Loaded from the same file they judge, they are a tautology -- the
# mutation "let the recheck become the constant" came back GREEN because both sides moved together.
# These four numbers are what every arm-A comparison in docs/56 rests on (§181, §219), so a change
# to them has to redden something, and this is the something.
CONSTANTS_181 = {"discrete_log": 1.4747, "edge_expansion": 0.9759,
                 "pagerank": 0.0, "pde_heat1d": 1.0259}


@pytest.mark.parametrize("task", sorted(RECORDED))
def test_every_recorded_hash_is_reproduced_by_the_rule(task):
    """Четыре независимых суммы — это четыре независимых опровергателя правила."""
    import os
    if not os.path.isdir(arm_a_solver.SNAPSHOT):
        pytest.skip("campaign snapshot not on this box")
    text, sha = arm_a_solver.shipped_solver(task)
    assert text is not None, task
    assert sha == RECORDED[task]["solver_sha256"], (task, sha)
    assert len(text.splitlines()) == RECORDED[task]["solver_lines"], task


def test_the_text_ends_with_exactly_one_newline(tmp_path):
    """Именно этим отличалось моё извлечение от записанного — и этого хватило на четыре промаха."""
    log = tmp_path / "A-t.log"
    log.write_text("noise\nFILE IN CODE DIR solver.py:\n\ndef solve(p):\n    return p\n\n\n"
                   "INFO - done\n", encoding="utf-8")
    text, _ = arm_a_solver.shipped_solver("t", str(tmp_path))
    assert text == "def solve(p):\n    return p\n", repr(text)


def test_the_LAST_marker_wins(tmp_path):
    """Кампания, переписавшая solver.py, пишет файл в лог не один раз; сдала она последний."""
    log = tmp_path / "A-t.log"
    log.write_text("FILE IN CODE DIR solver.py:\nFIRST = 1\nINFO - x\n"
                   "FILE IN CODE DIR solver.py:\nLAST = 2\nINFO - y\n", encoding="utf-8")
    text, _ = arm_a_solver.shipped_solver("t", str(tmp_path))
    assert text == "LAST = 2\n", repr(text)


def test_a_log_line_ends_the_solver(tmp_path):
    """Срез по первой строке лога, а не по концу файла: иначе в «решатель» уедет весь хвост."""
    log = tmp_path / "A-t.log"
    log.write_text("FILE IN CODE DIR solver.py:\nX = 1\n2026-09-09 12:00:00 more log\nY = 2\n",
                   encoding="utf-8")
    text, _ = arm_a_solver.shipped_solver("t", str(tmp_path))
    assert text == "X = 1\n", repr(text)


def test_a_missing_log_or_marker_answers_nothing_rather_than_guessing(tmp_path):
    assert arm_a_solver.shipped_solver("absent", str(tmp_path)) == (None, None)
    (tmp_path / "A-t.log").write_text("no marker here\n", encoding="utf-8")
    assert arm_a_solver.shipped_solver("t", str(tmp_path)) == (None, None)


def test_the_artefact_now_says_how_its_hash_is_derived():
    """§342: правило, которое нельзя перепроверить, — это утверждение, а не запись."""
    doc = json.loads((REPO / "benchmarks" / "algotune" / "arm_a_retimed.json")
                     .read_text(encoding="utf-8"))
    # THE KEY, not the string anywhere in the file: `arm_a_solver` also appears in a recheck's
    # `how`, so grepping the whole document passed with the pointer deleted (§358, §391).
    rule = doc.get("_solver_sha256_rule") or ""
    assert "arm_a_solver" in rule and "shipped_solver" in rule, rule
    assert "newline" in rule, "the rule does not mention the byte that made four hashes differ"


def test_a_recheck_is_recorded_beside_the_constant_and_never_instead_of_it():
    """§356 в файле, а не только в отчёте: перемер ДОБАВЛЯЕТСЯ, константа остаётся.

    Иначе «перемерили и записали» тихо переопределяет то, с чем сравнивались все числа в docs/56, и
    сравнение через месяц идёт уже против другой линейки, ничего об этом не сказав.
    """
    doc = json.loads((REPO / "benchmarks" / "algotune" / "arm_a_retimed.json")
                     .read_text(encoding="utf-8"))
    rechecks = doc.get("_rechecks") or []
    assert rechecks, "no recheck recorded"
    for r in rechecks:
        assert r.get("when") and r.get("lane") and r.get("regime"), r
        assert r.get("readings"), r
        for task, value in r["readings"].items():
            assert task in doc["tasks"], task
            # the recheck may agree or differ -- what it may NOT do is become the constant
            assert doc["tasks"][task]["speedup"] == CONSTANTS_181[task], (
                f"{task}: the constant moved to {doc['tasks'][task]['speedup']} -- a recheck is "
                f"RECORDED beside {CONSTANTS_181[task]}, never instead of it")
    assert doc["_taken"] == "2026-09-08", "the constants' own date moved with a recheck"
