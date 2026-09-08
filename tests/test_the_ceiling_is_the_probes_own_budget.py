"""§359. Потолок бюджета был вшит в единицу, и никто его не читал.

`_paused` и `_at_ceiling` брали `budget: float = 1.0`, и все вызывающие брали умолчание. На этой
коробке все 125 проб с прибором несут `budget_usd: 1.00` — то есть допущение верно СЕГОДНЯ по
случайности истории, а не по правилу. `run_probe.sh` принимает бюджет шестым аргументом
(`BUDGET="${6:-1.00}"`) и пишет его в `INSTRUMENT.txt`.

Проба, запущенная на $0.50, истратила бы свои деньги и читалась бы как «PAUSED and owed work» при
потолке $0.99 — а `resume_paused` её возобновил бы. Это §213 дословно: те самые $0.1056, за которыми
отправили `freeB3`, — только вход другой, через число, которого никто не прочитал.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1] / "benchmarks"
sys.path.insert(0, str(BENCH))

import arm_fidelity  # noqa: E402


def _probe(tmp_path, name, *, budget=None, spend=0.0, last="pause"):
    d = tmp_path / name / "runs" / "t" / "run"
    d.mkdir(parents=True)
    if budget is not None:
        (tmp_path / name / "INSTRUMENT.txt").write_text(
            f"probe:          {name}\nbudget_usd:     {budget}\n", encoding="utf-8")
    rows = [{"v": 1, "seq": 1, "ts": 1.0, "type": "llm_usage", "data": {"cost": spend}}]
    if last:
        rows.append({"v": 1, "seq": 2, "ts": 2.0, "type": last, "data": {}})
    (d / "events.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return str(tmp_path)


def test_a_half_dollar_probe_that_spent_it_all_is_finished_not_owed(tmp_path):
    """Сердце §359: та самая проба, которую вшитая единица отправила бы тратить второй раз."""
    root = _probe(tmp_path, "half", budget="0.50", spend=0.50)
    assert arm_fidelity._at_ceiling(root, "half") is True
    assert arm_fidelity._paused(root, "half") is False


def test_the_same_probe_read_with_the_hard_coded_dollar_is_called_owed(tmp_path):
    """Воспроизведение дефекта: при бюджете, взятом за единицу, она «должна работу»."""
    root = _probe(tmp_path, "half", budget="0.50", spend=0.50)
    assert arm_fidelity._paused(root, "half", budget=1.0) is True


def test_a_dollar_probe_still_behaves_as_before(tmp_path):
    root = _probe(tmp_path, "full", budget="1.00", spend=1.00)
    assert arm_fidelity._at_ceiling(root, "full") is True
    root2 = _probe(tmp_path / "b", "half_spent", budget="1.00", spend=0.40)
    assert arm_fidelity._paused(root2, "half_spent") is True


def test_a_probe_with_no_instrument_keeps_the_old_default(tmp_path):
    """70 проб на коробке старше прибора. Умолчание — прежнее поведение, а не новое утверждение."""
    root = _probe(tmp_path, "old", budget=None, spend=0.99)
    assert arm_fidelity.probe_budget(root, "old") == 1.0
    assert arm_fidelity._at_ceiling(root, "old") is True


def test_a_malformed_or_zero_budget_falls_back(tmp_path):
    """Ноль или мусор в строке — не бюджет: делить на них значило бы объявить любую пробу
    законченной."""
    root = _probe(tmp_path, "zero", budget="0", spend=0.10)
    assert arm_fidelity.probe_budget(root, "zero") == 1.0
    root2 = _probe(tmp_path / "b", "junk", budget="not-a-number", spend=0.10)
    assert arm_fidelity.probe_budget(root2, "junk") == 1.0


def test_the_live_corpus_all_reads_one_dollar():
    """Якорь: сегодня допущение верно — и теперь это измерено, а не предположено."""
    import glob, re
    seen = set()
    for p in glob.glob("/var/tmp/looplab-bench/model-probes/*/INSTRUMENT.txt"):
        got = re.search(r"^budget_usd:\s*([0-9.]+)", open(p, errors="replace").read(), re.M)
        if got:
            seen.add(got.group(1))
    if seen:
        assert seen == {"1.00"}, seen


def test_a_budget_line_with_trailing_text_is_not_half_read(tmp_path):
    """`budget_usd: 0.50 USD` — строка, которую шаблон не принимает целиком. Прочитать из неё
    «0.50» значило бы додумать формат; берётся умолчание, и это видно."""
    d = tmp_path / "trail" / "runs" / "t" / "run"
    d.mkdir(parents=True)
    (tmp_path / "trail" / "INSTRUMENT.txt").write_text(
        "budget_usd:     0.50 USD\n", encoding="utf-8")
    assert arm_fidelity.probe_budget(str(tmp_path), "trail") == 1.0
