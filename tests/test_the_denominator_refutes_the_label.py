"""§352. У чтения есть второй прибор на нём самом — знаменатель, на который оно делило.

§350 починил ИСТОЧНИК режима, но не мог вычистить уже записанное. Четыре строки от 2026-09-07T01:58
—02:02 (все четыре задачи обхода) стояли с меткой `lane22r3`, а их собственный `cached_ms` — это
медиана ШИРОКОГО кэша. Они лежали в ПОСЛЕДОВАТЕЛЬНОМ пуле, и их удаление сдвинуло разрыв
«последовательный против широкого» на 1–1.8 пункта на задачу:

    discrete_log  -1.5 % -> -2.5 %      pde_heat1d  -3.4 % -> -5.2 %
    pagerank      -0.0 % -> +0.1 %      edge_expansion: осталось меньше двух чтений

Кэши двух режимов не похожи ничем: 146.5 мс против 78.2 у pde_heat1d, 2.18 против 1.47 у
discrete_log. Поэтому опознание ПОЛОЖИТЕЛЬНОЕ: не «не совпало со своим», что срабатывало бы на
каждой строке старше перемина кэша, а «совпало с ЧУЖИМ с точностью 2 %».
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1] / "benchmarks"
sys.path.insert(0, str(BENCH))

import ruler_check  # noqa: E402
import sweep_claims  # noqa: E402

WIDE, SERIAL = ruler_check.CAMPAIGN_REGIME, ruler_check.SERIAL_REGIME
MEDIANS = {("pde_heat1d", WIDE): 146.49, ("pde_heat1d", SERIAL): 78.16}


def _row(regime, cached_ms, values=(1.0, 1.0), stamp="2026-09-09T10:00:00"):
    return {"task": "pde_heat1d", "stamp": stamp, "subset": "test",
            "regime": regime, "cached_ms": cached_ms, "values": list(values),
            "median": values[0], "busy_cpus_outside_lane": 0}


def test_a_serial_label_over_a_wide_denominator_is_refuted():
    got = sweep_claims._label_contradicted_by_its_own_denominator(_row(SERIAL, 146.49), MEDIANS)
    assert got == WIDE, got


def test_a_label_that_matches_its_own_cache_stands():
    assert sweep_claims._label_contradicted_by_its_own_denominator(
        _row(SERIAL, 78.16), MEDIANS) is None
    assert sweep_claims._label_contradicted_by_its_own_denominator(
        _row(WIDE, 146.49), MEDIANS) is None


def test_a_number_belonging_to_neither_cache_is_not_an_accusation():
    """«Не совпало со своим» срабатывало бы на каждой строке старше перемина кэша — законной
    истории. Обвинение требует ПОЛОЖИТЕЛЬНОГО опознания чужого знаменателя."""
    assert sweep_claims._label_contradicted_by_its_own_denominator(
        _row(SERIAL, 999.0), MEDIANS) is None


def test_a_row_without_a_denominator_says_nothing():
    assert sweep_claims._label_contradicted_by_its_own_denominator(
        _row(SERIAL, None), MEDIANS) is None
    assert sweep_claims._label_contradicted_by_its_own_denominator(
        _row(None, 146.49), MEDIANS) is None


def test_the_other_regime_must_exist_to_be_identified():
    assert sweep_claims._label_contradicted_by_its_own_denominator(
        _row(SERIAL, 146.49), {("pde_heat1d", SERIAL): 78.16}) is None


# --- and the pool that uses it -----------------------------------------------------------------

def _bench(tmp_path, rows, caches):
    algo = tmp_path / "looplab" / "benchmarks" / "algotune"
    (algo / ".baseline_times").mkdir(parents=True)
    for (task, regime), ms in caches.items():
        (algo / ".baseline_times" / f"{task}__test__{regime}.json").write_text(
            json.dumps({f"i{i}": ms for i in range(100)}), encoding="utf-8")
    (algo / "ruler_selfcheck_log.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    (tmp_path / "model-probes").mkdir()
    return str(tmp_path)


def test_a_refuted_reading_never_reaches_the_serial_mean(tmp_path):
    """Тот самый случай: широкое чтение с последовательной меткой в последовательном пуле."""
    bench = _bench(tmp_path,
                   [_row(SERIAL, 78.16, values=(1.00, 1.01)),
                    _row(SERIAL, 78.16, values=(1.02, 1.03)),
                    _row(SERIAL, 146.49, values=(9.00, 9.01))],
                   {("pde_heat1d", WIDE): 146.49, ("pde_heat1d", SERIAL): 78.16})
    _, said = sweep_claims.check_ruler_constants(bench)
    assert "9.0000" not in said, f"опровергнутое чтение попало в среднее: {said}"
    assert "2 reading(s) whose own cached_ms belongs to the OTHER regime" in said, said


def test_a_clean_log_carries_no_accusation(tmp_path):
    bench = _bench(tmp_path, [_row(SERIAL, 78.16, values=(1.00, 1.01)),
                              _row(SERIAL, 78.16, values=(1.02, 1.03))],
                   {("pde_heat1d", WIDE): 146.49, ("pde_heat1d", SERIAL): 78.16})
    _, said = sweep_claims.check_ruler_constants(bench)
    assert "OTHER regime" not in said, said


def test_two_caches_that_happen_to_agree_do_not_convict_a_correct_row():
    """Опознание чужого знаменателя работает потому, что кэши двух режимов далеки друг от друга.
    Там, где они СОШЛИСЬ, «совпало с чужим» верно и про свой — и обвинять по нему нельзя: строку
    оправдывает её собственный кэш, и проверка на него обязана идти ПЕРВОЙ."""
    close = {("pde_heat1d", WIDE): 100.0, ("pde_heat1d", SERIAL): 100.5}
    assert sweep_claims._label_contradicted_by_its_own_denominator(
        _row(SERIAL, 100.5), close) is None
    assert sweep_claims._label_contradicted_by_its_own_denominator(
        _row(WIDE, 100.0), close) is None


def test_a_row_older_than_the_regime_aware_denominator_is_not_accused():
    """§353. До 2026-09-08T18:30 `_cached_median_ms` брал ШИРОКИЙ ключ при любом вызове, значит
    правильное ПОСЛЕДОВАТЕЛЬНОЕ чтение записывало широкий `cached_ms`. Обвинить его — значит
    осудить строку за поле, которое было широким по построению. Первое обвинение §352 было ровно
    таким, и его пришлось отозвать."""
    old = _row(SERIAL, 146.49, stamp="2026-09-07T01:58:15")
    assert sweep_claims._label_contradicted_by_its_own_denominator(old, MEDIANS) is None
    new = _row(SERIAL, 146.49, stamp="2026-09-09T10:00:00")
    assert sweep_claims._label_contradicted_by_its_own_denominator(new, MEDIANS) == WIDE


def test_the_recorded_denominator_follows_the_regime_that_ran(tmp_path, monkeypatch):
    """§353. Поле зовётся «на что это чтение делило». До правки оно было широкой медианой при
    любом прогоне: последовательное чтение `edge_expansion` записывало 45.48 мс там, где его
    собственный кэш говорит 28.21."""
    import ruler_selfcheck
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "pde_heat1d__test__w22x1r3.json").write_text(
        json.dumps({f"i{i}": 146.36 for i in range(3)}), encoding="utf-8")
    (cache / "pde_heat1d__test__lane22r3.json").write_text(
        json.dumps({f"i{i}": 78.16 for i in range(3)}), encoding="utf-8")
    monkeypatch.setattr(ruler_selfcheck, "baseline_dir", lambda: str(cache))
    assert ruler_selfcheck._cached_median_ms("pde_heat1d", "test", "lane22r3") == 78.16
    assert ruler_selfcheck._cached_median_ms("pde_heat1d", "test", "w22x1r3") == 146.36


def test_the_caller_hands_the_regime_to_the_denominator():
    """Умолчание — это и был дефект: все вызывающие брали его, и никто не замечал."""
    src = (Path(__file__).resolve().parents[1] / "benchmarks" / "ruler_selfcheck.py").read_text(
        encoding="utf-8")
    assert "_cached_median_ms(args.task, args.subset, ran_regime)" in src, \
        "знаменатель снова не знает, в каком режиме шёл прогон"
