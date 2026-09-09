"""§399. «Ни разу не потянул рычаг» и «ни разу о нём не подумал» — разные находки.

Обход печатал «NEVER reached for Cython: pde_heat1d (0 of 12)», и это читается как слепота. Измерение
говорит обратное: Cython назван в СОБСТВЕННЫХ рассуждениях цикла в **11 из 12** проб, а `remPde` прямо
пишет, почему его отбросили: «@njit or a .pyx extension would add nothing because the FFT is already
native code and the Python overhead per instance is» пренебрежим. Задача упирается в FFT, цикл это
вывел и двенадцать раз сдал numba.

Отклики на две находки противоположны — одна про ЗАДАЧУ, другая про ЦИКЛ, — поэтому строка обязана
их различать. Болтовня не считается: список пакетов в `run_started` называет всё, что установлено на
коробке, и по нему «рассмотрено» значило бы «существует».
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1] / "benchmarks"
sys.path.insert(0, str(BENCH))

import sweep_claims  # noqa: E402


def _events(root, probe, task, rows):
    run = root / "model-probes" / probe / "runs" / task / "run"
    run.mkdir(parents=True)
    (run / "events.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def test_reasoning_that_names_cython_counts(tmp_path):
    _events(tmp_path, "a", "t", [
        {"v": 1, "type": "research_completed", "ts": 1.0,
         "data": {"question": "whether a .pyx extension would help"}}])
    assert sweep_claims.cython_named_in_reasoning(str(tmp_path), "t") == (1, 1)


def test_the_package_list_boilerplate_does_not_count(tmp_path):
    """`run_started` перечисляет всё установленное; по нему «рассмотрено» — пустое слово."""
    _events(tmp_path, "a", "t", [
        {"v": 1, "type": "run_started", "ts": 1.0,
         "data": {"packages": "cryptography cvxpy cython dace dask numba numpy"}}])
    assert sweep_claims.cython_named_in_reasoning(str(tmp_path), "t") == (0, 1)


def test_a_probe_is_counted_once_however_often_it_says_it(tmp_path):
    _events(tmp_path, "a", "t", [
        {"v": 1, "type": "hypothesis_added", "ts": 1.0, "data": {"statement": "Cython .pyx?"}},
        {"v": 1, "type": "reflection_note", "ts": 2.0, "data": {"text": "cimport numpy"}}])
    assert sweep_claims.cython_named_in_reasoning(str(tmp_path), "t") == (1, 1)


def test_every_spelling_of_the_lever_is_recognised(tmp_path):
    for i, word in enumerate(("Cython", ".pyx", "cimport")):
        _events(tmp_path, f"p{i}", "t", [
            {"v": 1, "type": "hypothesis_added", "ts": 1.0, "data": {"statement": f"try {word}"}}])
    assert sweep_claims.cython_named_in_reasoning(str(tmp_path), "t") == (3, 3)


def test_a_task_nobody_ran_is_zero_of_zero(tmp_path):
    (tmp_path / "model-probes").mkdir(parents=True)
    assert sweep_claims.cython_named_in_reasoning(str(tmp_path), "t") == (0, 0)


def test_the_sentence_distinguishes_weighed_from_blind(tmp_path):
    """Сентенция, а не греп (§395). Отдельной функцией — проверка вокруг неё зовёт
    `probe_summary.py` из корня стенда, и на фикстуре её было бы не прогнать."""
    _events(tmp_path, "weighed", "t", [
        {"v": 1, "type": "research_completed", "ts": 2.0,
         "data": {"question": "would a .pyx extension help?"}}])
    said = sweep_claims.never_shipped_sentence(str(tmp_path), ["t (0 of 1)"])
    assert "NEVER SHIPPED Cython" in said, said
    assert "NAMED in the loop's own reasoning in 1 of 1" in said, said
    assert "not blindness" in said, said


def test_a_task_that_never_names_it_says_so(tmp_path):
    _events(tmp_path, "blind", "t", [
        {"v": 1, "type": "hypothesis_added", "ts": 1.0, "data": {"statement": "try numba"}}])
    said = sweep_claims.never_shipped_sentence(str(tmp_path), ["t (0 of 1)"])
    assert "never named in the loop's reasoning either" in said, said


def test_two_tasks_are_reported_apart(tmp_path):
    """Одна взвесила рычаг, другая нет — обе должны получить свою половину предложения."""
    _events(tmp_path, "a", "weighed", [
        {"v": 1, "type": "research_completed", "ts": 1.0, "data": {"q": "a .pyx?"}}])
    _events(tmp_path, "b", "blind", [
        {"v": 1, "type": "hypothesis_added", "ts": 1.0, "data": {"statement": "numba"}}])
    said = sweep_claims.never_shipped_sentence(str(tmp_path), ["weighed (0 of 1)", "blind (0 of 1)"])
    assert "weighed (0 of 1), though it is NAMED" in said, said
    assert "blind (0 of 1), and never named" in said, said


def _bench_with_stub_summary(tmp_path, rows):
    """Стенд с ПОДСТАВНЫМ `probe_summary.py`: проверка зовёт его как подпроцесс и парсит JSON.

    Без этого сентенция проверялась в отрыве от места вызова, и мутация «перестать её печатать»
    приходила зелёной — тот же промах, что в §391, §393, §395 и §397: функция проверена, ВЫЗОВ нет.
    """
    tool = tmp_path / "looplab" / "benchmarks"
    tool.mkdir(parents=True)
    (tool / "probe_summary.py").write_text(
        "import json, sys\nprint(json.dumps(" + json.dumps(rows) + "))\n", encoding="utf-8")
    return tmp_path


def test_the_check_itself_carries_the_sentence(tmp_path):
    _events(tmp_path, "weighed", "t", [
        {"v": 1, "type": "research_completed", "ts": 1.0, "data": {"q": "a .pyx extension?"}}])
    _bench_with_stub_summary(tmp_path, [{"task": "t", "kernel_kind": "numba"}])
    ok, detail = sweep_claims.check_which_lever_the_loop_reaches_for(str(tmp_path))
    assert ok, detail
    assert "t numba 1" in detail, detail
    assert "NEVER SHIPPED Cython" in detail and "NAMED in the loop's own reasoning in 1 of 1" in detail, detail


def test_a_task_that_does_ship_cython_gets_no_sentence(tmp_path):
    """Задача, тянущая рычаг, не должна получать предложение про «ни разу не сдал»."""
    _events(tmp_path, "shipped", "t", [
        {"v": 1, "type": "research_completed", "ts": 1.0, "data": {"q": "a .pyx extension?"}}])
    _bench_with_stub_summary(tmp_path, [{"task": "t", "kernel_kind": "cython"}])
    ok, detail = sweep_claims.check_which_lever_the_loop_reaches_for(str(tmp_path))
    assert ok and "NEVER SHIPPED" not in detail, detail


def test_a_corpus_without_the_field_is_a_failure_not_a_silence(tmp_path):
    _bench_with_stub_summary(tmp_path, [{"task": "t"}])
    (tmp_path / "model-probes").mkdir(exist_ok=True)
    ok, detail = sweep_claims.check_which_lever_the_loop_reaches_for(str(tmp_path))
    assert ok is False and "kernel_kind" in detail, detail
