"""§346. Последний незаписанный вход самопроверки — эталон, ради которого она и названа.

На строке чтения уже есть всё остальное: полоса (§266), занятые ядра вне неё (§295), режим (§329),
обе половины знаменателя (§319). Не было ровно того файла, против которого мерили. Самопроверка
зовётся «эталон против себя», а эталон берёт `sorted(glob)[0]` из рабочего каталога какой-то пробы —
кандидат, переписавший свою выложенную копию, сдвинул бы константу, и на строке не осталось бы
ничего, что это объясняет.

Померено 2026-09-08, чтобы запись начиналась с известного состояния: 11 выложенных копий для
pde_heat1d, 13 для discrete_log, 119 для edge_expansion, 1 для pagerank — и по ОДНОЙ различной
версии на задачу. Сегодня это ничего не меняет; потому и стоит записать сейчас.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1] / "benchmarks"
sys.path.insert(0, str(BENCH))

import ruler_selfcheck  # noqa: E402
import sweep_claims  # noqa: E402


def _staged(root: Path, probe: str, task: str, body: str) -> Path:
    p = root / probe / "ws" / task / f"reference_{task}.py"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


BODY = "class Ref(Task):\n    def solve(self, problem):\n        return problem\n"


def test_the_module_and_its_hash_are_reported(tmp_path):
    got = _staged(tmp_path, "remPde", "pde_heat1d", BODY)
    path, sha = ruler_selfcheck.reference_module("pde_heat1d", str(tmp_path))
    assert path == str(got)
    assert sha == hashlib.sha256(BODY.encode()).hexdigest()[:12]


def test_a_changed_module_changes_the_hash(tmp_path):
    _staged(tmp_path, "remPde", "pde_heat1d", BODY)
    _, before = ruler_selfcheck.reference_module("pde_heat1d", str(tmp_path))
    _staged(tmp_path, "remPde", "pde_heat1d", BODY + "# a candidate edited its staged copy\n")
    _, after = ruler_selfcheck.reference_module("pde_heat1d", str(tmp_path))
    assert before != after, "переписанная выложенная копия не видна по хешу"


def test_the_choice_is_deterministic_not_filesystem_order(tmp_path):
    """`sorted(glob)[0]` — не «какой попадётся». Две пробы, один и тот же ответ на оба порядка."""
    _staged(tmp_path, "zzz", "pde_heat1d", BODY + "# zzz\n")
    _staged(tmp_path, "aaa", "pde_heat1d", BODY + "# aaa\n")
    first, _ = ruler_selfcheck.reference_module("pde_heat1d", str(tmp_path))
    assert "/aaa/" in first, first


def test_the_row_carries_both_fields(tmp_path):
    row = ruler_selfcheck.append_reading(
        tmp_path / "log.jsonl", "pde_heat1d", "test", [1.0, 1.1], 1.05,
        stamp="2026-09-08T15:00:00", reference_sha="0be2185cd8b9", reference_from="accPde")
    assert row["reference_sha"] == "0be2185cd8b9" and row["reference_from"] == "accPde"
    written = json.loads((tmp_path / "log.jsonl").read_text(encoding="utf-8").strip())
    assert written["reference_sha"] == "0be2185cd8b9", written


# --- and the check that reads the series -------------------------------------------------------

TIMES = {"i0": 1.0}


def _bench(tmp_path, rows):
    algo = tmp_path / "looplab" / "benchmarks" / "algotune"
    (algo / ".baseline_times").mkdir(parents=True)
    cache = algo / ".baseline_times" / f"x__test__{ruler_selfcheck_serial()}.json"
    cache.write_text(json.dumps(TIMES), encoding="utf-8")
    (algo / "ruler_selfcheck_log.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    (tmp_path / "model-probes").mkdir()
    return str(tmp_path)


def ruler_selfcheck_serial():
    import ruler_check
    return ruler_check.SERIAL_REGIME


def _row(sha, values):
    import ruler_check
    return {"task": "pde_heat1d", "stamp": "2026-09-08T00:00:00", "subset": "test",
            "median": values[0], "values": values, "busy_cpus_outside_lane": 0,
            "regime": ruler_check.CAMPAIGN_REGIME, "reference_sha": sha}


def test_two_references_in_one_pool_are_not_one_series(tmp_path):
    """«Эталон против себя» — одна серия ровно пока эталон один файл. Смешанные чтения дают
    среднее, не измеренное нигде: ошибка §317 с другим ключом."""
    bench = _bench(tmp_path, [_row("aaaaaaaaaaaa", [1.00, 1.01]),
                              _row("bbbbbbbbbbbb", [1.06, 1.07])])
    _, said = sweep_claims.check_ruler_constants(bench)
    assert "2 DIFFERENT reference module(s)" in said, said
    assert "aaaaaaaaaaaa" in said and "bbbbbbbbbbbb" in said, said


def test_one_reference_across_the_pool_says_nothing(tmp_path):
    bench = _bench(tmp_path, [_row("aaaaaaaaaaaa", [1.00, 1.01]),
                              _row("aaaaaaaaaaaa", [1.02, 1.03])])
    _, said = sweep_claims.check_ruler_constants(bench)
    assert "DIFFERENT reference" not in said, said


def test_rows_written_before_the_field_are_counted_apart(tmp_path):
    """«Не записано» — это не «то же, что у остальных»."""
    bench = _bench(tmp_path, [_row(None, [1.00, 1.01]), _row(None, [1.02, 1.03])])
    _, said = sweep_claims.check_ruler_constants(bench)
    assert "no reading names the reference it used" in said, said
