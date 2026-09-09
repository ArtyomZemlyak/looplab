"""§394. Константу судили по ТОЧНОСТИ СРЕДНЕГО, а вопрос был про разброс чтений.

Старое правило: `|mean - quoted| > 2 * SEM и |delta| > 2 %`. Его же комментарий обещал, что тест по
SEM «не даёт прибору с разбросом ±4 % докладывать снос в 3 % через сидение». Не даёт — только
откладывает: SEM падает как 1/√n, а разброс прибора не падает. С накоплением сидений ЛЮБАЯ
константа, не равная в точности среднему, однажды объявляется уехавшей.

`pde_heat1d` и есть этот случай: 12 сидений, среднее 1.0416, SEM 0.0088, список 0.9958 — помечено
MOVED на +4.6 %, при том что медианы сидений идут от 0.9897 до 1.1013 (sd 0.0319), то есть
константа лежит ВНУТРИ обычного чтения. В тот же день: разброс внутри одного сидения 6.2 % (медиана
по 11 сидениям), семь свежих чтений — 0.9716…1.0556.

Вердикт теперь по РАЗБРОСУ (2 sd медиан сидений) и допуску. SEM по-прежнему считается и печатается —
знать, насколько точно известно среднее, полезно; просто решает не он.
"""
from __future__ import annotations

import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1] / "benchmarks"
sys.path.insert(0, str(BENCH))

import sweep_claims  # noqa: E402

# Медианы сидений `pde_heat1d`, как они лежат в журнале самопроверки на 2026-09-09.
PDE = [1.1013, 1.0676, 0.9897, 1.0057, 1.0198, 1.0387, 1.0195, 1.0164, 1.0649, 1.0818,
       1.0471, 1.0273]


def test_a_constant_inside_the_readings_own_spread_has_not_moved():
    """Главный опровергатель: по старому правилу это было MOVED на +4.6 %."""
    moved, mean, scatter, sem = sweep_claims.constant_moved(PDE, 0.9958)
    assert moved is False, (mean, scatter, sem)
    assert min(PDE) < 0.9958 < max(PDE), "the fixture stopped being the case it is about"
    # и старое правило на этих же числах сказало бы «уехала»
    assert abs(mean - 0.9958) > 2 * sem, "the SEM test would not have fired -- fixture is wrong"


def test_a_constant_outside_the_spread_has_moved():
    moved, mean, scatter, _ = sweep_claims.constant_moved(PDE, 0.80)
    assert moved is True, (mean, scatter)


def test_more_readings_do_not_make_a_constant_easier_to_condemn():
    """Ровно тот дефект: SEM падает с √n, разброс — нет.

    Одно и то же распределение, четыре сидения против сорока: вердикт обязан совпасть. По старому
    правилу сорок сидений осуждают константу, которую четыре оправдывали.
    """
    few = PDE[:4]
    many = (PDE * 10)[:40]
    quoted = 0.9958
    assert sweep_claims.constant_moved(few, quoted)[0] is False
    assert sweep_claims.constant_moved(many, quoted)[0] is False
    # и покажем, что старое правило здесь расходится само с собой
    import statistics
    for vals, verdict in ((few, False), (many, True)):
        sem = statistics.stdev(vals) / (len(vals) ** 0.5)
        old = abs(statistics.fmean(vals) - quoted) > 2 * sem and \
            abs(statistics.fmean(vals) - quoted) / quoted > 0.02
        assert old is verdict, (len(vals), old)


def test_a_tight_instrument_still_reports_a_real_drift():
    """Починка не ослепляет проверку: у трёх других задач разброс ±0.005-0.006."""
    tight = [1.000, 1.004, 0.998, 1.002, 1.001, 0.999]
    moved, _mean, scatter, _ = sweep_claims.constant_moved(tight, 0.95)
    assert scatter < 0.01 and moved is True, (scatter,)


def test_a_difference_too_small_to_act_on_is_not_a_drift():
    """Вторая половина правила остаётся: допуск в 2 % ниже уровня действия."""
    tight = [1.000, 1.004, 0.998, 1.002, 1.001, 0.999]
    assert sweep_claims.constant_moved(tight, 0.995)[0] is False


def test_one_reading_is_a_point_not_a_band():
    assert sweep_claims.constant_moved([1.05], 0.99) == (None, None, None, None)
    assert sweep_claims.constant_moved([], 0.99) == (None, None, None, None)


def test_the_line_names_the_scatter_and_the_means_precision_separately():
    src = (BENCH / "sweep_claims.py").read_text(encoding="utf-8")
    assert "scatter +-" in src and "mean known to +-" in src, "one of the two numbers is unnamed"
