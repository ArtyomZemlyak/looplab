"""§361. Правило «строка — не событие» живёт в трёх копиях, и две из них были неполны.

Движок пишет крах-атомарные пакеты: `type` несёт часовой `__looplab_event_batch_v1__`, а настоящие
события лежат в `data.events`. Померено по корпусу сегодня: **23 пакета в 23 прогонах**, внутри
17 `node_failed`, 17 `pause`, 6 `node_building`. Наивный читатель не видит ни одного.

Копий рассуждения три: `events_read`, `algotune/plot_corpus.py` и `algotune/plot_corpus_v2.py`.
Последняя ключуется ТОЛЬКО на списочное написание (`isinstance(type, list)`), хотя докстринг
`events_read` прямо предупреждает: «Two spellings exist ... Handle both, or the next writer change
silently re-hides the failures», — и разворачивает любую строку со списочным типом, теряя её, если
`data.events` там нет.

Двое читали события.jsonl вовсе без разворачивания:

* `sweep_claims.check_every_node_was_graded_on_train` — моя же проверка §348. Сегодня ни один пакет
  не несёт `node_evaluated`, и это ЕДИНСТВЕННАЯ причина, по которой её собственный цикл ещё не врал.
* `budget_gate_curve.py` — и там же вторая беда: весь разбор файла стоял в ОДНОМ `try`, так что одна
  рваная строка (норма для живого прогона) выбрасывала кривую ЦЕЛОЙ пробы, а не эту строку.

Обе переведены на общий читатель.
"""
from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

import pytest

BENCH = Path(__file__).resolve().parents[1] / "benchmarks"
sys.path.insert(0, str(BENCH))

import events_read  # noqa: E402

SENTINEL = events_read.SENTINEL


def test_both_spellings_of_the_sentinel_are_unrolled(tmp_path):
    p = tmp_path / "events.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in [
        {"type": [SENTINEL], "data": {"events": [{"type": "node_failed"}]}},
        {"type": SENTINEL, "data": {"events": [{"type": "pause"}]}},
    ]), encoding="utf-8")
    got = [e.get("type") for e in events_read.iter_events(str(p))]
    assert got == ["node_failed", "pause"], got


def test_a_row_that_only_looks_like_a_packet_is_not_swallowed(tmp_path):
    """Строка с часовым, но без `data.events`, — это что-то другое, и проглотить её значит
    потерять строку вместо того, чтобы развернуть."""
    p = tmp_path / "events.jsonl"
    p.write_text(json.dumps({"type": [SENTINEL], "data": {}}) + "\n", encoding="utf-8")
    got = list(events_read.iter_events(str(p)))
    assert len(got) == 1 and got[0]["type"] == [SENTINEL], got


def test_one_torn_line_costs_that_line_and_not_the_file(tmp_path):
    """Беда `budget_gate_curve`: разбор всего файла в одном `try` терял пробу целиком."""
    p = tmp_path / "events.jsonl"
    p.write_text('{"type": "llm_usage", "data": {"cost": 1.0}}\n{"type": "node_ev\n',
                 encoding="utf-8")
    got = list(events_read.iter_events(str(p)))
    assert len(got) == 1 and got[0]["type"] == "llm_usage", got


def _reads_events_by_type(src: str) -> bool:
    return "events.jsonl" in src and bool(re.search(r'get\("type"\)|\["type"\]', src))


def _all_event_paths_use_eventstore(src: str) -> bool:
    """Narrow AST exception for direct canonical reads, not a file-name allowlist.

    Checkpoint sidecars also have `type` fields. A file may use JSON for those
    and EventStore for events. Every event path literal must be inside the actual
    `EventStore(...).read_all()` call; an extra raw reader revokes the exception.
    This proves source structure, not arbitrary alias/data-flow correctness.
    """
    tree = ast.parse(src)
    bindings = {alias.asname or alias.name for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom) and node.module == "looplab.events.eventstore"
                for alias in node.names if alias.name == "EventStore"}
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    paths = [node for node in ast.walk(tree)
             if isinstance(node, ast.Constant) and node.value == "events.jsonl"]
    if not paths or not bindings:
        return False
    for path in paths:
        node, found = path, False
        while node in parents:
            node = parents[node]
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "read_all" and isinstance(node.func.value, ast.Call)
                    and isinstance(node.func.value.func, ast.Name)
                    and node.func.value.func.id in bindings):
                found = True
                break
        if not found:
            return False
    return True


def test_no_benchmark_tool_filters_events_by_type_without_the_rule():
    """Класс, а не два файла: новый наивный читатель должен краснеть здесь, а не через месяц в
    отчёте. Собственный читатель разрешён, только если он знает про часового."""
    offenders = []
    for path in sorted(BENCH.glob("**/*.py")):
        if path.name == "events_read.py":
            continue
        src = path.read_text(encoding="utf-8", errors="replace")
        if not _reads_events_by_type(src):
            continue
        if _all_event_paths_use_eventstore(src):
            continue
        # УПОМИНАНИЕ модуля недостаточно: файл может импортировать его для другого и читать
        # события собственным циклом рядом. Нужен ВЫЗОВ.
        # Оба публичных входа модуля годятся -- `read` это `list(iter_events)`; первая, слишком
        # узкая версия этой проверки поймала `probe_summary.py`, который читает правильно.
        if "events_read.iter_events(" in src or "events_read.read(" in src:
            continue
        # НЕТ послабления «у него свой читатель». Первая версия этого теста разрешала файл,
        # где встречается имя часового, — и пропустила `plot_corpus.py`, у которого часовой стоит
        # только в ДОКСТРИНГЕ, а правило в коде неполное. Проверка, ищущая фрагмент, который есть и
        # в описании, не может провалиться по делу (§297 про то же).
        offenders.append(path.name)
    assert not offenders, f"читают события по типу мимо общего правила: {offenders}"


@pytest.mark.parametrize("name", ["external_asha_smoke.py", "external_deadline_smoke.py",
                                 "external_live_monitor_smoke.py"])
def test_canonical_reader_does_not_hide_an_added_raw_event_reader(name):
    source = (BENCH / name).read_text(encoding="utf-8")
    assert _all_event_paths_use_eventstore(source)
    raw = '\nrows = [json.loads(line) for line in Path("events.jsonl").read_text().splitlines()]\n'
    assert not _all_event_paths_use_eventstore(source + raw)
    assert not _all_event_paths_use_eventstore(source.replace('.read_all()', '.read_text()'))
    assert not _all_event_paths_use_eventstore(source.replace(
        'from looplab.events.eventstore import EventStore', '# no canonical reader import'))


def test_the_corpus_really_holds_packets():
    """Якорь: если пакеты исчезнут, эта защита станет теорией — и об этом стоит узнать."""
    import glob
    n = 0
    for p in glob.glob("/var/tmp/looplab-bench/model-probes/*/runs/*/*/events.jsonl"):
        with open(p, errors="replace") as fh:
            n += sum(1 for l in fh if SENTINEL in l)
    if not glob.glob("/var/tmp/looplab-bench/model-probes/*/runs/*/*/events.jsonl"):
        # SKIP, not `return`: this anchor reads the LIVE bench corpus, and a box without one
        # must report "not checked" rather than print a green dot for a check that never ran.
        pytest.skip("no probe packets on this box")
    assert n >= 20, f"корпус держал 23 пакета, сейчас {n} -- проверить, не сменился ли формат"
