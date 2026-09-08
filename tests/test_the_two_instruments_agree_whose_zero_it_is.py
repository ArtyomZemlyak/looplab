"""§342. Живой прибор и считающий назвали ОДИН И ТОТ ЖЕ ноль по-разному, в противоположные стороны.

`remDL13`, узел 0, 2026-09-08. `compare_arms` прочитал его как *«the candidate's own code would not
build or import -- a real zero»*. `pulse` в ту же минуту напечатал *«RULER REFUSAL (evaluator_error)
-- the harness declined, the solver was never the question»*. В самой записи лежит отказ типизации
numba `nopython` внутри собственного `solver.py` кандидата: решатель БЫЛ вопросом и проиграл.

Причина в том, что словарь был напечатан дважды. `compare_arms` держит полное разбиение
`SOLVERS_FAULT | NOT_SOLVERS_FAULT` и правило «слово-причина — это сводка моста, а свидетельство
рядом — то, что случилось на самом деле»; `pulse` не держал ничего и звал отказом ЛЮБУЮ названную
причину. Разбиение теперь одно на двоих, и `pulse` его импортирует, а не переписывает.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1] / "benchmarks"
sys.path.insert(0, str(BENCH))
sys.path.insert(0, str(BENCH / "algotune"))

import compare_arms  # noqa: E402
import pulse as pulse_mod  # noqa: E402

NUMBA = ("Failed in nopython mode pipeline (step: nopython frontend) During: "
         "Pass nopython_type_inference")


def _events(tmp_path, reason, extra=""):
    tail = json.dumps({"speedup": 0.0, "eval_seconds": 4.0,
                       "no_speedup": {"reason": reason}}) + " " + extra
    rows = [{"v": 1, "seq": 1, "ts": 1.0, "type": "node_evaluated",
             "data": {"node_id": 0, "generation": 0, "metric": 0.0, "eval_seconds": 4.235,
                      "violations": [], "stdout_tail": tail}}]
    path = tmp_path / "events.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return str(path)


def test_a_zero_whose_own_code_would_not_compile_is_not_a_refusal(tmp_path):
    """Свидетельство побеждает слово-причину: `evaluator_error` с трассой numba — ноль кандидата."""
    got = pulse_mod.pulse(_events(tmp_path, "evaluator_error", NUMBA))
    assert got["bad"][0]["whose"] == "candidate", got["bad"][0]


def test_the_same_reason_without_that_evidence_stays_the_arenas(tmp_path):
    """`evaluator_error` сам по себе — отказ арены. Свидетельство, а не слово, делает разницу."""
    got = pulse_mod.pulse(_events(tmp_path, "evaluator_error", "the evaluator raised on a shape"))
    assert got["bad"][0]["whose"] == "arena", got["bad"][0]


def test_a_reason_the_partition_does_not_know_is_not_filed_under_either_half(tmp_path):
    """Причина вне обеих половин не попадает молча ни в одну. Умолчание — тот самый дефект, ради
    которого разбиение выписано целиком и проверяется в обе стороны."""
    got = pulse_mod.pulse(_events(tmp_path, "some_new_reason", NUMBA))
    assert got["bad"][0]["whose"] == "unclassified", got["bad"][0]


def test_a_reason_in_the_solvers_half_needs_no_evidence(tmp_path):
    got = pulse_mod.pulse(_events(tmp_path, "invalid_results"))
    assert got["bad"][0]["whose"] == "candidate", got["bad"][0]


def test_pulse_does_not_keep_its_own_copy_of_the_vocabulary():
    """Словарь напечатанный дважды — это и есть расхождение. `pulse` импортирует разбиение."""
    src = (BENCH / "pulse.py").read_text(encoding="utf-8")
    assert "import compare_arms" in src, src[:200]
    for word in compare_arms.BUILD_FAILURE_WORDS:
        assert f'"{word}"' not in src, f"{word} переписан в pulse вместо импорта"


def test_the_two_instruments_answer_alike_on_the_live_record():
    """Якорь в настоящих данных: узел 0 пробы remDL13, из-за которого расхождение и нашлось."""
    live = Path("/var/tmp/looplab-bench/model-probes/remDL13/runs/discrete_log/run/events.jsonl")
    if not live.is_file():
        return
    bad = [b for b in pulse_mod.pulse(str(live))["bad"] if b.get("reason")]
    if not bad:
        return
    assert bad[0]["whose"] == "candidate", bad[0]
    assert compare_arms.looks_like_the_candidates_own_build("Failed in nopython mode pipeline")


def test_the_sentence_itself_is_what_changes(tmp_path):
    """Расхождение было в ФОРМУЛИРОВКЕ, а не в данных: проверка, читающая классификацию из словаря,
    пропустила бы весь дефект целиком. Поэтому проверяется предложение, которое читает оператор."""
    z = pulse_mod.pulse(_events(tmp_path, "evaluator_error", NUMBA))["bad"][0]
    said = pulse_mod.zero_sentence(z)
    assert "the candidate EARNED this zero" in said, said
    assert "RULER REFUSAL" not in said, said
    assert "the solver was never the question" not in said, said


def test_the_arenas_refusal_still_reads_as_one(tmp_path):
    said = pulse_mod.zero_sentence(pulse_mod.pulse(_events(tmp_path, "no_problems"))["bad"][0])
    assert "RULER REFUSAL (no_problems)" in said, said


def test_an_unclassified_reason_says_so_out_loud(tmp_path):
    said = pulse_mod.zero_sentence(pulse_mod.pulse(_events(tmp_path, "some_new_reason"))["bad"][0])
    assert "NEITHER half" in said, said


def test_a_record_with_no_bridge_line_falls_back_to_the_stopwatch(tmp_path):
    """Дозаписи причины нет — мир до §323. Секундомер остаётся ЗАПАСНЫМ и называет себя."""
    path = tmp_path / "e2.jsonl"
    path.write_text(json.dumps({"v": 1, "seq": 1, "ts": 1.0, "type": "node_evaluated",
                                "data": {"node_id": 0, "generation": 0, "metric": 0.0,
                                         "eval_seconds": 0.4, "violations": []}}) + "\n",
                    encoding="utf-8")
    z = pulse_mod.pulse(str(path))["bad"][0]
    assert z["reason"] is None and z["whose"] == "unclassified", z
    assert "RULER REFUSAL -- the harness declined" in pulse_mod.zero_sentence(z)
