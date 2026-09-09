"""§370. «$1 за пробу» — цифра, из которой строится каждый план на стенде, и её никто не сверял.

Померено по корпусу: **143 из 149** проб с тратой ушли ЗА свой бюджет, медиана превышения $0.0096,
всего $1.54. Это не утечка и в основном не новость: движок сверяется с бюджетом ПЕРЕД тем, как
открыть работу, а уже летящий вызов доканчивается и заряжается. Не было другого — числа. Проба за
доллар стоит около $1.01, и корпус обошёлся примерно на процент дороже той арифметики, которую все
цитируют.

Граница между структурным и неправильным не выдумана здесь: это собственный
`node_open_budget_floor_usd` прогона (§363). Ниже него движок отказывается ОТКРЫВАТЬ работу, значит
превышение больше пола — это не один доканчивающийся вызов, а работа, которой не следовало
начинаться. За полом ровно одна проба: `freeB3` +$0.1056 — та самая двойная оплата §213.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1] / "benchmarks"
sys.path.insert(0, str(BENCH))

import sweep_claims  # noqa: E402


def _probe(tmp_path, name, spend, *, budget="1.00", floor=None):
    run = tmp_path / "model-probes" / name / "runs" / "t" / "run"
    run.mkdir(parents=True)
    (tmp_path / "model-probes" / name / "INSTRUMENT.txt").write_text(
        f"budget_usd:     {budget}\n", encoding="utf-8")
    if floor is not None:
        (run / "config.snapshot.json").write_text(
            json.dumps({"engine": {"node_open_budget_floor_usd": floor}}), encoding="utf-8")
    (run / "events.jsonl").write_text(
        json.dumps({"v": 1, "seq": 1, "ts": 1.0, "type": "llm_usage",
                    "data": {"cost": spend}}) + "\n", encoding="utf-8")
    return str(tmp_path)


def test_a_small_overshoot_is_structural_and_passes(tmp_path):
    """Один доканчивающийся вызов — это норма, а не тревога."""
    ok, said = sweep_claims.check_a_dollar_probe_costs_a_dollar(_probe(tmp_path, "a", 1.0096))
    assert ok, said
    assert "$0.0096 spent past the budgets" in said, said


def test_an_overshoot_past_the_floor_is_named(tmp_path):
    """§213 дословно: работа, которой не следовало начинаться."""
    ok, said = sweep_claims.check_a_dollar_probe_costs_a_dollar(_probe(tmp_path, "freeB3", 1.1056))
    assert not ok, said
    assert "PAST THE NODE-OPEN FLOOR" in said and "freeB3 +$0.1056" in said, said


def test_the_floor_comes_from_the_run_where_it_recorded_one(tmp_path):
    """Прогон со своим полом судится своим полом, а не умолчанием."""
    ok, _ = sweep_claims.check_a_dollar_probe_costs_a_dollar(
        _probe(tmp_path, "b", 1.15, floor=0.20))
    assert ok, "пол прогона ($0.20) больше превышения ($0.15) -- тревоги быть не должно"


def test_a_probe_inside_its_budget_adds_nothing(tmp_path):
    ok, said = sweep_claims.check_a_dollar_probe_costs_a_dollar(_probe(tmp_path, "c", 0.80))
    assert ok and "$0.0000 spent past" in said, said


def test_a_bigger_budget_is_not_judged_against_a_dollar(tmp_path):
    """Бюджет берётся из прибора самой пробы (§359), иначе двухдолларовая проба на $1.50 объявляется
    превысившей."""
    ok, said = sweep_claims.check_a_dollar_probe_costs_a_dollar(
        _probe(tmp_path, "big", 1.50, budget="2.00"))
    assert ok and "$0.0000 spent past" in said, said


def test_the_live_corpus_names_only_the_known_incident():
    ok, said = sweep_claims.check_a_dollar_probe_costs_a_dollar("/var/tmp/looplab-bench")
    if "0 probe(s)" in said:
        return
    if not ok:
        assert "freeB3" in said, said
        assert said.count("+$") <= 3, "новое превышение за полом -- разобрать: " + said
