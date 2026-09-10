"""§363. Потолок спрашивал «потрачено ли 99 %», а движок перед каждым узлом спрашивает другое.

Дословно из `run.log` пробы `pgr2`, 2026-09-08:

    Refused: LLM spend ceiling reached before opening a speculative Card build: $0.0654 of the
    $1.0000 set by `llm_budget_usd` remains, below the `node_open_budget_floor_usd` of $0.1000
    a new node needs. The run stops here rather than open work it cannot finish.

Проба встала на **$0.9594** — 95.9 % бюджета, заметно ниже планки в 99 %. От ярлыка «owed work» её
спасло только то, что движок написал рядом `run_finished`. Прогон, который ВСТАЛ НА ПАУЗУ с остатком
меньше пола, находится ровно в том же положении — открывать нечего, — и был бы возобновлён платить
второй раз. Это §213.

Поле записано в 2 снимках из 145 (оно новое), поэтому прогон без него откатывается к прежнему
правилу 99 % — старое поведение, а не новое утверждение.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

BENCH = Path(__file__).resolve().parents[1] / "benchmarks"
sys.path.insert(0, str(BENCH))

import arm_fidelity  # noqa: E402


def _probe(tmp_path, name, *, spend, last="pause", floor=None, budget="1.00"):
    run = tmp_path / name / "runs" / "t" / "run"
    run.mkdir(parents=True)
    (tmp_path / name / "INSTRUMENT.txt").write_text(f"budget_usd:     {budget}\n", encoding="utf-8")
    if floor is not None:
        (run / "config.snapshot.json").write_text(
            json.dumps({"engine": {"node_open_budget_floor_usd": floor}}), encoding="utf-8")
    rows = [{"v": 1, "seq": 1, "ts": 1.0, "type": "llm_usage", "data": {"cost": spend}}]
    if last:
        rows.append({"v": 1, "seq": 2, "ts": 2.0, "type": last, "data": {}})
    (run / "events.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return str(tmp_path)


def test_a_pause_with_less_than_the_floor_left_is_at_its_ceiling(tmp_path):
    """$0.9594 из $1.00 — 95.9 %, под планкой 99 %, но остаток $0.0406 меньше пола $0.10."""
    root = _probe(tmp_path, "pgr2like", spend=0.9594, floor=0.1)
    assert arm_fidelity._at_ceiling(root, "pgr2like") is True
    assert arm_fidelity._paused(root, "pgr2like") is False


def test_a_pause_with_the_floor_still_in_hand_owes_work(tmp_path):
    """Остаток $0.30 больше пола — открыть узел можно, значит работа действительно должна."""
    root = _probe(tmp_path, "early", spend=0.70, floor=0.1)
    assert arm_fidelity._at_ceiling(root, "early") is False
    assert arm_fidelity._paused(root, "early") is True


def test_the_old_ninety_nine_per_cent_rule_still_stands(tmp_path):
    root = _probe(tmp_path, "atceiling", spend=0.995, floor=None)
    assert arm_fidelity._at_ceiling(root, "atceiling") is True


def test_a_run_without_a_recorded_floor_falls_back(tmp_path):
    """143 снимка из 145 поля не несут: для них ничего не меняется."""
    root = _probe(tmp_path, "old", spend=0.9594, floor=None)
    assert arm_fidelity.node_open_floor(root, "old") is None
    assert arm_fidelity._at_ceiling(root, "old") is False


def test_a_zero_floor_is_off_not_a_floor(tmp_path):
    """`0 = off` — так это описывает сам движок; ноль не должен объявлять любую паузу законченной."""
    root = _probe(tmp_path, "zero", spend=0.20, floor=0)
    assert arm_fidelity.node_open_floor(root, "zero") is None
    assert arm_fidelity._at_ceiling(root, "zero") is False


def test_a_run_that_never_paused_is_not_at_a_ceiling(tmp_path):
    root = _probe(tmp_path, "running", spend=0.99, last=None, floor=0.1)
    assert arm_fidelity._at_ceiling(root, "running") is False


def test_the_live_probe_records_the_floor_the_engine_quoted():
    live = "/var/tmp/looplab-bench/model-probes"
    if not Path(live, "pgr2").is_dir():
        # SKIP, not `return`: this anchor reads the LIVE bench corpus, and a box without one
        # must report "not checked" rather than print a green dot for a check that never ran.
        pytest.skip("no pgr2 probe on this box")
    assert arm_fidelity.node_open_floor(live, "pgr2") == 0.1
