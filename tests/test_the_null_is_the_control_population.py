"""§374. Мощность симулировали по распределению, куда подмешаны сами плечи.

Популяция, из которой тянет плечо, — КОНТРОЛЬНАЯ: отгруженная карточка без флагов. Симулировать
нуль по всем прогонам задачи значит подмешать в него намеренные обработки. Померено 2026-09-09, и
это не в запас, а наоборот:

    все чемпионы edge_expansion   n=118  медиана 216.44  sd 68.28
    только отгруженная карточка   n= 70  медиана 216.66  sd 78.11

Каждое плечо ТЕСНЕЕ контроля (sd 29–52 против 78), так что смешивание УМЕНЬШАЕТ разброс, на который
делит мощность, и таблица просит меньше проб, чем плечу нужно. Мощность на 12 пробах: 0.07 по
контролю против 0.13 по смеси — вдвое оптимистичнее.

`card_sha256` один этого не решает: `--checker …` оставляет карточку той же и меняет то, чем её
судят, так что двенадцать прогонов делят sha контроля при другой обработке. Популяцию называют
ФЛАГИ.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

BENCH = Path(__file__).resolve().parents[1] / "benchmarks"
sys.path.insert(0, str(BENCH))

import arm_power  # noqa: E402


def _run(root, probe, task, metrics, spend, card_args=arm_power.SHIPPED_CARD):
    d = root / probe / "runs" / task / "run"
    d.mkdir(parents=True)
    rows = [{"v": 1, "seq": 0, "ts": 0.0, "type": "llm_usage", "data": {"cost": spend}}]
    rows += [{"v": 1, "seq": i + 1, "ts": float(i + 1), "type": "node_evaluated",
              "data": {"node_id": i, "metric": m}} for i, m in enumerate(metrics)]
    (d / "events.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    if card_args is not None:
        (root / probe / "INSTRUMENT.txt").write_text(
            f"task:           {task}\ncard_args:      {card_args}\n", encoding="utf-8")


def test_a_treatment_arm_is_not_in_the_null(tmp_path):
    _run(tmp_path, "ctl", "pagerank", [50.0], 1.0)
    _run(tmp_path, "arm", "pagerank", [90.0], 1.0, card_args="--exploit-best")
    assert arm_power.champions(str(tmp_path), "pagerank", live=[]) == [50.0]


def test_a_probe_with_no_instrument_is_not_assumed_to_be_control(tmp_path):
    """Неизвестная карточка — это не «отгруженная». Десять таких прогонов на коробке."""
    _run(tmp_path, "ctl", "pagerank", [50.0], 1.0)
    _run(tmp_path, "old", "pagerank", [70.0], 1.0, card_args=None)
    assert arm_power.champions(str(tmp_path), "pagerank", live=[]) == [50.0]


def test_the_checker_arm_shares_the_card_and_is_still_excluded(tmp_path):
    """`--checker` оставляет карточку той же: если фильтровать по sha, обработка пролезет."""
    _run(tmp_path, "ctl", "pagerank", [50.0], 1.0)
    _run(tmp_path, "chk", "pagerank", [80.0], 1.0, card_args="--checker /x/y.py")
    assert arm_power.champions(str(tmp_path), "pagerank", live=[]) == [50.0]


def test_the_whole_corpus_is_still_reachable_on_purpose(tmp_path):
    """Полная выборка иногда нужна (описать, что вообще производит стенд) -- но по запросу."""
    _run(tmp_path, "ctl", "pagerank", [50.0], 1.0)
    _run(tmp_path, "arm", "pagerank", [90.0], 1.0, card_args="--exploit-best")
    assert sorted(arm_power.champions(str(tmp_path), "pagerank", live=[],
                                      control_only=False)) == [50.0, 90.0]


def test_the_live_control_population_is_smaller_and_wider():
    """Якорь: смешивание УМЕНЬШАЕТ разброс, а не увеличивает -- поэтому таблица была оптимистичной."""
    import statistics
    root = "/var/tmp/looplab-bench/model-probes"
    ctl = arm_power.champions(root, "edge_expansion", live=[])
    everything = arm_power.champions(root, "edge_expansion", live=[], control_only=False)
    if not ctl or not everything:
        # SKIP, not `return`: this anchor reads the LIVE bench corpus, and a box without one
        # must report "not checked" rather than print a green dot for a check that never ran.
        pytest.skip("no edge_expansion champions on this box")
    assert len(ctl) < len(everything), (len(ctl), len(everything))
    assert statistics.pstdev(ctl) > statistics.pstdev(everything), \
        "контроль обязан быть ШИРЕ смеси -- иначе вывод §374 не тот"
